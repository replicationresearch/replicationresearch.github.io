"""The HTML full-text view built straight from a copy-edited manuscript's
Markdown export (Google Docs "Download as Markdown"), as an alternative to
extracting it from the PDF (scripts/pdf_fulltext.py).

Why: text extraction from a PDF depends on how the PDF happened to be laid out
(tables split across pages, captions above or below figures, running headers,
footnotes at the page bottom ...). The Markdown holds the same manuscript as
structured text: real headings, paragraphs, tables, figures (embedded as base64
images), footnotes and equations - so the result does not depend on the layout.

An article uses it when overrides/<urlPath>.fulltext-md exists next to its
overrides/<urlPath>.md; every other article keeps the PDF-based view.

Handled: headings, paragraphs (hard line breaks), bold/italic/links, bullet
lists, tables with captions and notes (shared with manuscript_override.py),
figures with APA ("Figure 2" + italic title above the image) or journal
(caption below) captions, footnotes ([^1]), inline/display LaTeX equations
(rendered as MathML, which browsers draw natively). The title block, abstract
boxes, "Open Science Badges", "Submission Data" and the reference list are not
part of the body (the page shows them elsewhere).

Best-effort: unknown constructs become plain paragraphs, never an exception.
"""

import base64
import html
import re
import sys

from manuscript_override import (NOTE_RE, TABLE_CAPTION_RE, _md_inline,
                                 iter_tables)

STOP_HEADINGS = {"references", "open science badges", "submission data",
                 "bibliography", "literature"}
FIG_CAPTION_RE = re.compile(
    r"^(?:#{1,6}\s*)?\*\*(?:Figure|Fig\.)\s+(\d+)[A-Za-z]?\s*\\?\.?\*\*\s*\\?\.?\s*(.*)$",
    re.IGNORECASE)
IMAGE_LINE_RE = re.compile(r"^\s*\**\s*(?:!\[[^\]]*\]\[(image\d+)\]\s*)+\**\s*$")
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\[(image\d+)\]")
IMAGE_DEF_RE = re.compile(r"^\[(image\d+)\]:\s*<data:image/(\w+);base64,([A-Za-z0-9+/=\s]+)>\s*$")
FOOTDEF_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)$")
BULLET_RE = re.compile(r"^\s*[\*\-\+]\s+(.*)$")
EQUATION_LINE_RE = re.compile(r"^\s*(?:\$\{\}\$)*\$(.+)\$\s*(\(\d+\))?\s*$")
_slug = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# LaTeX subset (what the Google Docs equation editor exports) -> MathML
# ---------------------------------------------------------------------------

GREEK = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
         "eta": "η", "theta": "θ", "lambda": "λ", "mu": "μ", "nu": "ν", "pi": "π",
         "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "chi": "χ", "psi": "ψ",
         "omega": "ω", "Delta": "Δ", "Sigma": "Σ", "Omega": "Ω"}
OPERATORS = {"times": "×", "cdot": "·", "pm": "±", "leq": "≤", "geq": "≥",
             "neq": "≠", "approx": "≈", "sim": "∼", "infty": "∞", "div": "÷"}


class _Tex:
    def __init__(self, src):
        self.s, self.i = src, 0

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def group(self):
        """{...} -> MathML sequence."""
        if self.peek() != "{":
            return self.atom()
        self.i += 1
        nodes = self.sequence("}")
        self.i += 1
        return "".join(nodes)

    def atom(self):
        c = self.peek()
        if not c:
            return ""
        self.i += 1
        return self.token(c)

    def token(self, c):
        if c.isdigit():
            j = self.i
            while j < len(self.s) and (self.s[j].isdigit() or self.s[j] == "."):
                j += 1
            num = c + self.s[self.i:j]
            self.i = j
            return "<mn>%s</mn>" % num
        if c.isalpha():
            j = self.i
            while j < len(self.s) and self.s[j].isalpha():
                j += 1
            word = c + self.s[self.i:j]
            self.i = j
            return "<mi>%s</mi>" % html.escape(word)
        return "<mo>%s</mo>" % html.escape(c)

    def command(self):
        j = self.i
        while j < len(self.s) and self.s[j].isalpha():
            j += 1
        name = self.s[self.i:j]
        if not name:                      # "\ " "\," etc. - a space
            self.i += 1
            return ""
        self.i = j
        if name == "frac":
            num, den = self.group(), self.group()
            return "<mfrac><mrow>%s</mrow><mrow>%s</mrow></mfrac>" % (num, den)
        if name == "sqrt":
            return "<msqrt><mrow>%s</mrow></msqrt>" % self.group()
        if name in ("left", "right"):
            d = self.peek()
            self.i += 1 if d else 0
            return "" if d == "." else "<mo>%s</mo>" % html.escape(d)
        if name in ("text", "mathrm"):
            return "<mtext>%s</mtext>" % self.group().replace("<mi>", "").replace("</mi>", "")
        if name in GREEK:
            return "<mi>%s</mi>" % GREEK[name]
        if name in OPERATORS:
            return "<mo>%s</mo>" % OPERATORS[name]
        return "<mi>%s</mi>" % html.escape(name)

    def sequence(self, stop=""):
        nodes = []
        while self.i < len(self.s) and self.peek() != stop:
            c = self.peek()
            if c == "\\":
                self.i += 1
                node = self.command()
            elif c == "{":
                node = self.group()
                if not node:                      # "{}" placeholder: nothing to draw
                    continue
                node = "<mrow>%s</mrow>" % node
            elif c in "_^":
                self.i += 1
                sub = self.group()
                nodes = self._attach(nodes, c, sub)
                continue
            elif c.isspace():
                self.i += 1
                continue
            else:
                self.i += 1
                node = self.token(c)
            if node:
                nodes.append(node)
        return nodes

    @staticmethod
    def _attach(nodes, kind, script):
        if not script:
            return nodes
        if not nodes:
            return [("<msub>" if kind == "_" else "<msup>") + "<mrow></mrow><mrow>%s</mrow>"
                    % script + ("</msub>" if kind == "_" else "</msup>")]
        base = nodes.pop()
        if base.startswith("<msub>") and kind == "^":     # SD_1^2 -> msubsup
            inner = base[len("<msub>"):-len("</msub>")]
            nodes.append("<msubsup>%s<mrow>%s</mrow></msubsup>" % (inner, script))
        elif base.startswith("<msup>") and kind == "_":
            inner = base[len("<msup>"):-len("</msup>")]
            b, sup = re.match(r"(<m[a-z]+>.*?</m[a-z]+>|<mrow>.*?</mrow>)(<mrow>.*</mrow>)$", inner, re.S).groups()
            nodes.append("<msubsup>%s<mrow>%s</mrow>%s</msubsup>" % (b, script, sup))
        else:
            tag = "msub" if kind == "_" else "msup"
            nodes.append("<%s>%s<mrow>%s</mrow></%s>" % (tag, base, script, tag))
        return nodes


def latex_to_mathml(src, display=False):
    try:
        body = "".join(_Tex(src).sequence())
    except Exception:  # noqa: BLE001 - an odd formula must not break the page
        return html.escape(src)
    return ('<math xmlns="http://www.w3.org/1998/Math/MathML"%s>%s</math>'
            % (' display="block"' if display else "", body))


# ---------------------------------------------------------------------------
# Inline text
# ---------------------------------------------------------------------------

def _inline(text, footnotes, math_store):
    """Markdown inline text -> HTML: footnote refs, math, links, emphasis."""
    def math_repl(m):
        math_store.append(latex_to_mathml(m.group(1)))
        return "\x00MATH%d\x00" % (len(math_store) - 1)
    text = re.sub(r"(?<!\\)\$([^$]+?)(?<!\\)\$", math_repl, text)

    def foot_repl(m):
        n = m.group(1)
        return "\x00FOOT%s\x00" % n
    text = re.sub(r"\[\^(\d+)\]", foot_repl, text)
    out = _md_inline(text)
    out = re.sub(r"\x00FOOT(\d+)\x00", lambda m: _footref(m.group(1), footnotes), out)
    return re.sub(r"\x00MATH(\d+)\x00", lambda m: math_store[int(m.group(1))], out)


def _footref(n, footnotes):
    body = footnotes.get(n, "")
    tip = '<span class="cite-tooltip">%s</span>' % body if body else ""
    return ('<sup class="fulltext-footref"><a href="#fn-%s" id="fnref-%s">%s</a>%s</sup>'
            % (n, n, n, tip))


def _heading_id(text, seen):
    base = _slug.sub("-", text.lower()).strip("-") or "section"
    hid, k = base, 1
    while hid in seen:
        k += 1
        hid = "%s-%d" % (base, k)
    seen.add(hid)
    return hid


# ---------------------------------------------------------------------------
# The body
# ---------------------------------------------------------------------------

def _plain_heading(text):
    text = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)
    return re.sub(r"\s+", " ", text.replace("**", "").replace("*", "")).strip()


def _body_range(lines):
    """(start, end) line indexes of the article body."""
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("| CITATION.") or line.startswith("| LAY SUMMARY."):
            start = i
    first = None
    for i in range(start, len(lines)):
        m = re.match(r"^#{1,6}\s+(.*)$", lines[i])
        if m and _plain_heading(m.group(1)):
            first = i
            break
    if first is None:
        return 0, 0
    end = len(lines)
    for i in range(first, len(lines)):
        m = re.match(r"^#{1,6}\s+(.*)$", lines[i])
        if m and _plain_heading(m.group(1)).lower() in STOP_HEADINGS:
            end = i
            break
    return first, end


RUNIN_RE = re.compile(r"^\*\*([^*]{2,80}?)\*\*\s*(.*)$")


def build_fulltext(md_text, fig_url_prefix="", jats=False):
    """{"html": str, "figures": [(filename, bytes)]} for the manuscript body.

    jats=True (scripts/jats.py) keeps the document structure the HTML view
    flattens: headings carry their real Markdown level (data-level), a heading
    that swallowed its whole first paragraph ("### **Title.** Text ...") is
    split into heading + paragraph, and a paragraph that starts with a bold
    run-in title ("**Title.** Text ...", or a bold line on its own) becomes a
    sub-heading (data-runin) - the JATS builder nests those under the section
    they appear in.
    """
    lines = md_text.splitlines()
    images, footnotes = {}, {}
    for line in lines:
        m = IMAGE_DEF_RE.match(line)
        if m:
            try:
                images[m.group(1)] = (m.group(2), base64.b64decode(re.sub(r"\s+", "", m.group(3))))
            except Exception:  # noqa: BLE001
                pass
        m = FOOTDEF_RE.match(line)
        if m:
            pass
    # footnote bodies keep their markup: build them properly (escape only once)
    footnotes = {FOOTDEF_RE.match(l).group(1): _md_inline(FOOTDEF_RE.match(l).group(2))
                 for l in lines if FOOTDEF_RE.match(l)}

    start, end = _body_range(lines)
    tables = {s: (e, h) for s, e, _, h in iter_tables(lines, start, end)}
    math_store, parts, figures, seen = [], [], [], set()
    para, bullets = [], []
    pending = {}                 # figure caption waiting for its image
    fig_n = [0]

    def inline(t):
        return _inline(t, footnotes, math_store)

    def flush_para():
        if para:
            text = "<br>".join(inline(p.strip()) for p in para if p.strip())
            note = NOTE_RE.match(para[0].strip())
            if text.strip():
                parts.append('<p%s>%s</p>' % (' class="table-note"' if note else "", text))
        para.clear()

    def flush_bullets():
        if bullets:
            parts.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(b) for b in bullets))
        bullets.clear()

    def flush_all():
        flush_para()
        flush_bullets()

    def emit_figure(caption, refs):
        for ref in refs:
            if ref not in images:
                continue
            ext, data = images[ref]
            fig_n[0] += 1
            name = "fig-%d.%s" % (fig_n[0], "jpg" if ext == "jpeg" else ext)
            figures.append((name, data))
            cap = '<figcaption>%s</figcaption>' % inline(caption) if caption else ""
            alt = html.escape(re.sub(r"<[^>]+>", "", inline(caption))[:80]) if caption else "Figure"
            parts.append(
                '<figure class="fulltext-figure"><button class="figure-zoom" type="button" '
                'aria-label="Enlarge figure"><img src="%s%s" loading="lazy" alt="%s"></button>%s</figure>'
                % (fig_url_prefix, name, alt, cap))

    def read_caption(i, m):
        """Figure caption at line i -> (text, next index)."""
        num, rest = m.group(1), m.group(2).strip()
        text = "Figure %s." % num
        if rest:
            return "%s %s" % (text, rest.strip()), i + 1
        j, title = i + 1, []
        while j < len(lines) and len(title) < 3:
            t = lines[j].strip()
            if not t and not title:
                j += 1
                continue
            if not t or IMAGE_REF_RE.search(t) or t.startswith("|"):
                break
            title.append(t.lstrip("#").strip().strip("*").strip())
            j += 1
        return ("%s %s" % (text, " ".join(title))).strip(), j

    i = start
    while i < end:
        line = lines[i]
        stripped = line.strip()
        if i in tables:
            flush_all()
            e, table_html = tables[i]
            parts.append(table_html)
            i = e
            continue
        if not stripped:
            flush_para()
            flush_bullets()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_all()
            raw = m.group(2)
            rest = ""
            if jats:
                raw = re.sub(r"^(?:#{1,6}\s*)+", "", raw)        # "# ## **X**": nested markers
                rm = re.match(r"^\*\*(.+?)\*\*\s+(\S.{30,})$", raw)
                if rm:                                          # heading swallowed its paragraph
                    raw, rest = rm.group(1), rm.group(2)
                    raw = raw.strip().rstrip(".").rstrip("\\").rstrip(".")
            text = _plain_heading(raw)
            if text and not TABLE_CAPTION_RE.match(stripped) and not FIG_CAPTION_RE.match(stripped):
                level = 3 if len(m.group(1)) == 1 else 4
                extra = (' data-level="%d"%s' % (len(m.group(1)), ' data-blob="1"' if rest else "")
                         if jats else "")
                parts.append('<h%d id="%s"%s>%s</h%d>' % (level, _heading_id(text, seen), extra,
                                                         html.escape(text), level))
                if rest:
                    para.append(rest)
                    flush_para()
                i += 1
                continue
            if not text:
                i += 1
                continue
        m = FIG_CAPTION_RE.match(stripped)
        if m:
            flush_all()
            pending["caption"], i = read_caption(i, m)
            continue
        if IMAGE_LINE_RE.match(line):
            flush_all()
            refs = IMAGE_REF_RE.findall(line)
            caption = pending.pop("caption", "")
            if not caption:           # journal style: caption follows the image
                j = i + 1
                while j < end and not lines[j].strip():
                    j += 1
                cm = FIG_CAPTION_RE.match(lines[j].strip()) if j < end else None
                if cm:
                    caption, i = read_caption(j, cm)
                    emit_figure(caption, refs)
                    continue
            emit_figure(caption, refs)
            i += 1
            continue
        eq = EQUATION_LINE_RE.match(line)
        if eq:
            flush_all()
            number = eq.group(2) or ""
            parts.append('<div class="fulltext-equation-block">%s%s</div>'
                         % (latex_to_mathml(eq.group(1), display=True),
                            '<span class="eq-number">%s</span>' % html.escape(number) if number else ""))
            i += 1
            continue
        if stripped.startswith("|"):           # a table without a caption
            flush_all()
            j = i
            while j < end and lines[j].strip().startswith("|"):
                j += 1
            try:
                from manuscript_override import _table_to_html
                parts.append(_table_to_html(lines[i:j], 0, ""))
            except Exception as e:  # noqa: BLE001
                print("  uncaptioned table skipped: %s" % e, file=sys.stderr)
            i = j
            continue
        m = BULLET_RE.match(line)
        if m:
            flush_para()
            bullets.append(m.group(1).strip())
            i += 1
            continue
        if bullets:
            flush_bullets()
        if jats:
            rm = RUNIN_RE.match(stripped)
            if rm and not NOTE_RE.match(stripped) and not re.match(r"(?i)^(figure|table)\b|^(h|hypothesis\s?)\d+\W*$", rm.group(1).strip()):
                title = _plain_heading(rm.group(1)).rstrip(" .")
                rest = rm.group(2).strip()
                if title and (not rest or rm.group(1).rstrip().rstrip("\\").endswith(".")):
                    flush_all()
                    parts.append('<h4 id="%s" data-runin="1">%s</h4>'
                                 % (_heading_id(title, seen), html.escape(title)))
                    if not rest:
                        i += 1
                        continue
                    line = rest
        # a pending figure caption that no image followed: show it as text
        if pending.get("caption"):
            para.append(pending.pop("caption"))
        para.append(line)
        i += 1
        # In these exports every line is its own paragraph (most lines end in
        # two spaces anyway); a line only continues into the next one if that
        # starts in lower case - a real manual line break inside a sentence.
        nxt = lines[i].strip() if i < end else ""
        if not (line.endswith("  ") and nxt[:1].islower()):
            flush_para()
    flush_all()

    if footnotes:
        items = "".join(
            '<li id="fn-%s" value="%s">%s <a class="footnote-backref" href="#fnref-%s" '
            'aria-label="Back to text">↩</a></li>' % (n, n, body, n)
            for n, body in sorted(footnotes.items(), key=lambda kv: int(kv[0])))
        parts.append('<section class="fulltext-footnotes" aria-label="Footnotes"><ol>%s</ol></section>' % items)

    html_out = "".join(parts)
    # footnote markers inside table cells (tables are rendered by
    # manuscript_override, which knows nothing about footnotes)
    html_out = re.sub(r"\[\^(\d+)\]",
                      lambda m: _footref(m.group(1), footnotes) if m.group(1) in footnotes else m.group(0),
                      html_out)
    try:
        from pdf_fulltext import _autolink_urls
        html_out = _autolink_urls(html_out)
    except Exception:  # noqa: BLE001
        pass
    return {"html": html_out, "figures": figures}
