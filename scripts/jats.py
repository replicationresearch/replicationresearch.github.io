"""JATS XML (NISO Z39.96, v1.3, Journal Publishing tag set) for an article of the mirror.

WIP - derived, not the version of record. The PDF stays canonical.

Inputs, in order of preference, for the *body* of the article:

  1. the manuscript's Markdown (overrides/<id>.md) - structured, so headings,
     tables, figures, footnotes and equations survive; it goes through the
     same parser as the HTML view (manuscript_fulltext.build_fulltext, in its
     "jats" mode that keeps the real heading levels), and that HTML is what
     gets translated to JATS;
  2. the HTML full text the build already derived from the PDF (pdf_fulltext)
     - works, but tables/figures are only as good as the extraction;
  3. nothing: a metadata-only JATS (front matter + references).

The *front matter* (title, authors, ORCIDs, affiliations, DOI, dates, abstract,
keywords, licence, references) comes from the article record (data/articles.json
+ the editors' sheet extras), never from the body. Declarations found in the
body are moved to where JATS wants them: funding -> <funding-group>, conflicts
of interest -> <author-notes>, acknowledgements -> <ack>, the CRediT statement
-> a <role> on each author (when it can be matched) plus a <fn fn-type="con">.

The Word (.docx) exports are not read: the Markdown export of the same
Google Doc carries everything the .docx does for this purpose.

Used by build.py (articles with an overrides/<id>.jats marker file) and from
the command line:

    python3 scripts/jats.py 10085 [outdir]      # builds into _jats/10085/

If the JATS DTD is available locally (environment variable JATS_DTD, the path
of JATS-journalpublishing1-3-mathml3.dtd), the file is also validated against it.
"""

import copy
import os
import re
import sys

from bs4 import BeautifulSoup, Comment, NavigableString
from lxml import etree

XLINK = "http://www.w3.org/1999/xlink"
MML = "http://www.w3.org/1998/Math/MathML"
ALI = "http://www.niso.org/schemas/ali/1.0/"
NSMAP = {"xlink": XLINK, "mml": MML, "ali": ALI}

DOCTYPE = ('<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Publishing DTD with '
           'MathML3 v1.3 20210610//EN" '
           '"https://jats.nlm.nih.gov/publishing/1.3/JATS-journalpublishing1-3-mathml3.dtd">')

_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
PARTICLES = {"van", "von", "de", "der", "den", "da", "di", "du", "la", "le", "del", "della",
             "bin", "al", "el", "ter", "ten", "dos", "das", "do", "st."}

# elements whose children are all block-level: safe to indent (never p/title/td ...)
STRUCT = {"article", "front", "back", "body", "journal-meta", "journal-title-group",
          "article-meta", "article-categories", "subj-group", "title-group", "contrib-group",
          "contrib", "name", "pub-date", "history", "permissions", "license", "kwd-group",
          "abstract", "sec", "list", "list-item", "table-wrap", "table-wrap-foot", "table",
          "thead", "tbody", "tr", "fig", "caption", "disp-formula", "ref-list", "ref",
          "fn-group", "fn", "custom-meta-group", "custom-meta", "author-notes",
          "funding-group", "award-group", "ack", "aff"}

CREDIT = [  # (regex on the author's own wording, CRediT term, URL slug)
    (r"conceptuali", "Conceptualization", "conceptualization"),
    (r"data curation", "Data curation", "data-curation"),
    (r"formal analysis", "Formal analysis", "formal-analysis"),
    (r"funding", "Funding acquisition", "funding-acquisition"),
    (r"investigation", "Investigation", "investigation"),
    (r"methodolog", "Methodology", "methodology"),
    (r"project admin", "Project administration", "project-administration"),
    (r"resource", "Resources", "resources"),
    (r"software", "Software", "software"),
    (r"supervis", "Supervision", "supervision"),
    (r"validation", "Validation", "validation"),
    (r"visuali", "Visualization", "visualization"),
    (r"writing.*(original|draft|first)", "Writing – original draft", "writing-original-draft"),
    (r"writing.*review|editing", "Writing – review & editing", "writing-review-editing"),
]

_SUP = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+"))


def _clean(text):
    return _BAD_XML.sub("", text)


def _ncname(text, prefix):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")
    return "%s-%s" % (prefix, s[:60] or "x")


def _sub(parent, tag, text=None, **attrs):
    el = etree.SubElement(parent, tag)
    for k, v in attrs.items():
        if v is not None:
            el.set(k, str(v))
    if text:
        el.text = _clean(text)
    return el


def _xlink(el, href):
    el.set("{%s}href" % XLINK, href)


def _append_text(parent, text):
    text = _clean(text)
    if not text:
        return
    if len(parent):
        last = parent[-1]
        last.tail = (last.tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def _text(el):
    return "".join(el.itertext())


# ---------------------------------------------------------------------------
# HTML -> JATS inline / block content
# ---------------------------------------------------------------------------

def _mathml(parent, node):
    """<math> (html.parser tags) -> <mml:math> under parent."""
    def conv(src, dst):
        for ch in src.children:
            if isinstance(ch, NavigableString):
                if not isinstance(ch, Comment):
                    _append_text(dst, str(ch))
                continue
            el = etree.SubElement(dst, "{%s}%s" % (MML, ch.name))
            for k, v in ch.attrs.items():
                if ":" in k or k.startswith("xmlns"):
                    continue
                el.set(k, " ".join(v) if isinstance(v, list) else str(v))
            conv(ch, el)
    m = etree.SubElement(parent, "{%s}math" % MML)
    for k, v in node.attrs.items():
        if k in ("display", "alttext"):
            m.set(k, " ".join(v) if isinstance(v, list) else str(v))
    conv(node, m)
    return m


class _Ctx:
    def __init__(self, footnote_ids):
        self.fn = footnote_ids          # {"3": "fn3", ...}
        self.link_depth = 0


def _inline(parent, node, ctx):
    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            _append_text(parent, str(ch))
            continue
        cls = ch.get("class") or []
        name = ch.name
        if "cite-tooltip" in cls or name in ("script", "style", "button"):
            continue
        if "cite-ref" in cls:                             # in-text citation -> <xref ref-type="bibr">
            tip = ch.find(class_="rid")
            label = "".join(str(c) for c in ch.children if isinstance(c, NavigableString))
            if tip is not None and tip.get("data-rid") and label.strip():
                _sub(parent, "xref", label, **{"ref-type": "bibr", "rid": tip["data-rid"]})
            else:
                _append_text(parent, label)
            continue
        if name == "sup" and "fulltext-footref" in cls:
            a = ch.find("a")
            n = a.get_text().strip() if a else ch.get_text().strip()
            if n in ctx.fn:
                _sub(parent, "xref", n, **{"ref-type": "fn", "rid": ctx.fn[n]})
            else:
                _sub(parent, "sup", n)
            continue
        if name in ("em", "i"):
            _inline(_sub(parent, "italic"), ch, ctx)
        elif name in ("strong", "b"):
            _inline(_sub(parent, "bold"), ch, ctx)
        elif name in ("sup", "sub"):
            _inline(_sub(parent, name), ch, ctx)
        elif name == "u":
            _inline(_sub(parent, "underline"), ch, ctx)
        elif name == "code":
            _inline(_sub(parent, "monospace"), ch, ctx)
        elif name == "br":
            if parent.tag in ("td", "th"):
                _sub(parent, "break")
            else:
                _append_text(parent, " ")
        elif name == "math":
            _mathml(_sub(parent, "inline-formula"), ch)
        elif name == "a":
            href = (ch.get("href") or "").strip()
            if re.match(r"^(https?://|mailto:|ftp://)", href) and not ctx.link_depth:
                el = _sub(parent, "ext-link", **{"ext-link-type": "uri"})
                _xlink(el, href)
                ctx.link_depth += 1
                _inline(el, ch, ctx)
                ctx.link_depth -= 1
            else:                                         # internal anchor / nested link: text only
                _inline(parent, ch, ctx)
        else:
            _inline(parent, ch, ctx)


def _split_label(node, kind):
    """Caption "Table 1. Title": drop the label from the node's first text, return it."""
    first = next((c for c in node.children if isinstance(c, NavigableString) and str(c).strip()), None)
    m = re.match(r"^\s*(%s\s+[A-Z]?\d+[A-Za-z]?)\.\s*" % kind, str(first)) if first is not None else None
    if not m:
        return "", node
    first.replace_with(NavigableString(str(first)[m.end():]))
    return m.group(1), node


def _table(parent, tnode, ctx):
    t = _sub(parent, "table")
    for sect_name in ("thead", "tbody"):
        sect = tnode.find(sect_name)
        if not sect:
            continue
        out = _sub(t, sect_name)
        for tr in sect.find_all("tr", recursive=False):
            row = _sub(out, "tr")
            cells = tr.find_all(["th", "td"], recursive=False)
            filled = [c for c in cells if c.get_text().strip()]
            if (sect_name == "tbody" and len(cells) > 1 and len(filled) == 1 and filled[0] is cells[0]
                    and cells[0].find(["strong", "b"])):
                # a section heading inside the table body ("Random intercept"): one header cell
                th = _sub(row, "th", colspan=len(cells), align="left")
                _inline(th, cells[0], ctx)
                continue
            for cell in cells:
                c = _sub(row, cell.name)
                for attr in ("colspan", "rowspan"):
                    if cell.get(attr):
                        c.set(attr, str(cell[attr]))
                m = re.search(r"text-align:\s*(left|right|center)", cell.get("style") or "")
                if m:
                    c.set("align", m.group(1))
                _inline(c, cell, ctx)
    return t


def _note_text(p):
    return p.get_text().strip()


def _blocks(sec_root, container, ctx, counters, fig_names):
    """Translate the children of `container` (BeautifulSoup) into JATS under sec_root."""
    stack = [(0, sec_root, False)]        # (heading level, element, run-in?)

    def cur():
        return stack[-1][1]

    nodes = [n for n in container.children if not isinstance(n, (Comment, NavigableString))]
    i = 0
    while i < len(nodes):
        node = nodes[i]
        i += 1
        name, cls = node.name, node.get("class") or []
        if name in ("h2", "h3", "h4", "h5", "h6"):
            if node.get("data-runin"):
                while len(stack) > 1 and stack[-1][2]:
                    stack.pop()
                level, runin = stack[-1][0] + 1, True
            else:
                runin = False
                level = int(node.get("data-level") or (int(name[1]) - 2))
                top = stack[-1]
                if len(stack) > 1 and top[0] == level and not top[2] and len(top[1]) == 1:
                    level += 1        # previous heading of the same level has no content: this is its child
                else:
                    while len(stack) > 1 and stack[-1][0] >= level:
                        stack.pop()
            sec = _sub(cur(), "sec", id=_ncname(node.get("id") or node.get_text(), "sec"))
            _sub(sec, "title").text = _clean(" ".join(node.get_text().split()))
            # a heading that swallowed its paragraph ("### **Title.** text") counts like a
            # run-in: a following bold run-in title is its sibling, not its child
            stack.append((level, sec, runin or bool(node.get("data-blob"))))
        elif name == "p":
            _inline(_sub(cur(), "p"), node, ctx)
        elif name in ("ul", "ol"):
            lst = _sub(cur(), "list", **{"list-type": "bullet" if name == "ul" else "order"})
            for li in node.find_all("li", recursive=False):
                _inline(_sub(_sub(lst, "list-item"), "p"), li, ctx)
        elif name == "figure" and "fulltext-table" in cls:
            counters["t"] += 1
            tw = _sub(cur(), "table-wrap", id="tbl%d" % counters["t"])
            cap = node.find("figcaption")
            if cap:
                label, cap = _split_label(cap, "Table")
                if label:
                    _sub(tw, "label", label)
                _inline(_sub(_sub(tw, "caption"), "title"), cap, ctx)
            tbl = node.find("table")
            if tbl:
                _table(tw, tbl, ctx)
            notes = node.find_all("p", class_="table-note")
            if notes:
                foot = _sub(tw, "table-wrap-foot")
                for p in notes:
                    _inline(_sub(foot, "p"), p, ctx)
        elif name == "figure":
            counters["f"] += 1
            fig = _sub(cur(), "fig", id="fig%d" % counters["f"])
            cap = node.find("figcaption")
            capel = None
            plain_cap = ""
            if cap:
                plain_cap = " ".join(cap.get_text().split())
                label, cap = _split_label(cap, "Figure")
                if label:
                    _sub(fig, "label", label)
                capel = _sub(fig, "caption")
                _inline(_sub(capel, "p"), cap, ctx)
            attrib = None
            # "Note. ..." paragraph(s) right below the figure belong to it
            while i < len(nodes) and nodes[i].name == "p" and re.match(r"^\s*Notes?\b", _note_text(nodes[i])):
                note = nodes[i]
                i += 1
                text = _note_text(note)
                if re.search(r"(?i)\bcourtesy\b|\bcredit\b|\badapted from\b|\breprinted\b", text) and attrib is None:
                    first = next((c for c in note.children if isinstance(c, NavigableString) and str(c).strip()), None)
                    if first is not None:
                        first.replace_with(NavigableString(re.sub(r"^\s*Notes?\.?\s*", "", str(first))))
                    attrib = note
                else:
                    if capel is None:
                        capel = _sub(fig, "caption")
                    _inline(_sub(capel, "p"), note, ctx)
            img = node.find("img")
            if img and img.get("src"):
                fname = img["src"].rsplit("/", 1)[-1]
                ext = fname.rsplit(".", 1)[-1].lower()
                g = _sub(fig, "graphic", mimetype="image",
                         **{"mime-subtype": "jpeg" if ext in ("jpg", "jpeg") else ext})
                _xlink(g, fname)
                fig_names.append(fname)
                alt = plain_cap or img.get("alt") or ""
                if alt:
                    _sub(g, "alt-text", alt)
            if attrib is not None:
                _inline(_sub(fig, "attrib"), attrib, ctx)
        elif name == "div" and "fulltext-equation-block" in cls:
            counters["e"] += 1
            eq = _sub(cur(), "disp-formula", id="eq%d" % counters["e"])
            num = node.find(class_="eq-number")
            if num and num.get_text().strip():
                _sub(eq, "label", num.get_text().strip())
            m = node.find("math")
            if m:
                _mathml(eq, m)
        elif name == "section" and "fulltext-footnotes" in cls:
            continue                                  # goes to <back><fn-group>
        else:
            if node.get_text().strip():
                _inline(_sub(cur(), "p"), node, ctx)


# ---------------------------------------------------------------------------
# Post-processing of the body tree
# ---------------------------------------------------------------------------

def _is_terminal(text):
    return bool(re.search(r"[.?!:;)\]\"”’]\s*$", text))


def _merge_split_paragraphs(body):
    """A figure/table the Markdown put in the middle of a sentence: rejoin the
    paragraph around it ("...being in the correction group" <fig/> "increased the odds...")."""
    for parent in list(body.iter("body", "sec")):
        kids = list(parent)
        k = 0
        while k < len(kids):
            if kids[k].tag == "p":
                j = k + 1
                while j < len(kids) and kids[j].tag in ("fig", "table-wrap"):
                    j += 1
                if (j > k + 1 and j < len(kids) and kids[j].tag == "p"
                        and not _is_terminal(_text(kids[k]))
                        and _text(kids[j]).lstrip()[:1].islower()):
                    prev, nxt = kids[k], kids[j]
                    _append_text(prev, " " + (nxt.text or ""))
                    for ch in list(nxt):
                        prev.append(ch)
                    parent.remove(nxt)
                    kids = list(parent)
                    continue
            k += 1


def _unwrap(el):
    """Replace el by its content (text and children) inside its parent."""
    parent = el.getparent()
    idx = list(parent).index(el)
    prev = parent[idx - 1] if idx else None
    text = el.text or ""
    tail = el.tail or ""
    kids = list(el)
    if prev is None:
        parent.text = (parent.text or "") + text
    else:
        prev.tail = (prev.tail or "") + text
    pos = idx
    for ch in kids:
        parent.insert(pos, ch)
        pos += 1
    after = parent[pos - 1] if kids else prev
    if after is None:
        parent.text = (parent.text or "") + tail
    else:
        after.tail = (after.tail or "") + tail
    parent.remove(el)


def _tidy_italics(root):
    """Repair emphasis the Markdown conversion put in odd places."""
    for it in list(root.iter("italic")):
        if it.getparent() is None:
            continue
        txt = it.text or ""
        if len(it):
            continue
        parent = it.getparent()
        idx = list(parent).index(it)
        prev = parent[idx - 1] if idx else None
        prev_text = (parent.text if prev is None else prev.tail) or ""
        # (a) italic that holds only punctuation: "(δ<i>),</i>" -> "(<i>δ</i>),"
        if txt and re.fullmatch(r"[\s)\],.;:]+", txt):
            m = re.match(r"^(.*\()([^\W\d_])$", prev_text, re.S)
            if m:
                if prev is None:
                    parent.text = m.group(1)
                else:
                    prev.tail = m.group(1)
                new = etree.Element("italic")
                new.text = m.group(2)
                new.tail = txt + (it.tail or "")
                parent.insert(idx, new)
                parent.remove(it)
            else:
                _unwrap(it)
            continue
        # (b) a package name followed by its version: "<i>effectsize (Version 1.0.3;</i>"
        m = re.match(r"^([\w.\-]+)(\s+\((?:Version|version|v)\s*[\d.]+;?\s*)$", txt)
        if m:
            it.text = m.group(1)
            it.tail = m.group(2) + (it.tail or "")
            continue
        # (c) one italic letter in the middle of a word: "Šidák <i>c</i>orrection"
        if len(txt) == 1 and txt.isalpha() and (it.tail or "")[:1].isalpha() and prev_text[-1:].isalpha():
            _unwrap(it)


def _replace_slot(el, child, parts):
    """Replace the text slot (el.text if child is None, else child.tail) by parts (str | Element)."""
    if child is None:
        el.text = None
        prev, pos = None, 0
    else:
        child.tail = None
        prev, pos = child, list(el).index(child) + 1
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            if prev is None:
                el.text = (el.text or "") + buf
            else:
                prev.tail = (prev.tail or "") + buf
            buf = ""

    for part in parts:
        if isinstance(part, str):
            buf += part
        else:
            flush()
            el.insert(pos, part)
            pos += 1
            prev = part
    flush()


_NOTATION = re.compile(
    r"(?P<ref>\b(?P<kind>Figures?|Fig\.|Tables?)\s+(?P<num>[A-Z]?\d+))"
    r"|(?P<eta>η2p|ηp2)|(?P<bf>BF(?:10|01))|(?P<sup>(?<=\d)[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)")


def _link_text(root, labels):
    """Figure/table mentions -> <xref>; "η2p", "BF10", "10⁹" -> proper sub/superscripts."""
    skip_in = {"xref", "ext-link", "inline-formula", "label", "alt-text", "attrib", "mixed-citation",
               "pub-id", "caption", "kwd", "article-title"}

    def make(m):
        if m.group("ref"):
            key = m.group("num")
            kind = "table" if m.group("kind").startswith("Table") else "fig"
            rid = labels.get((kind, key))
            if rid:
                x = etree.Element("xref")
                x.set("ref-type", kind)
                x.set("rid", rid)
                x.text = m.group("ref")
                return [x]
            return [m.group(0)]
        if m.group("eta"):
            a, b = etree.Element("sup"), etree.Element("sub")
            a.text, b.text = "2", "p"
            return ["η", a, b] if m.group("eta") == "η2p" else ["η", b, a]
        if m.group("bf"):
            b = etree.Element("sub")
            b.text = m.group("bf")[2:]
            return ["BF", b]
        s = etree.Element("sup")
        s.text = "".join(_SUP.get(c, c) for c in m.group("sup"))
        return [s]

    def split(text):
        parts, last = [], 0
        for m in _NOTATION.finditer(text):
            parts.append(text[last:m.start()])
            parts.extend(make(m))
            last = m.end()
        parts.append(text[last:])
        return parts

    for el in list(root.iter("p", "title", "td", "th", "italic", "bold")):
        if any(a.tag in skip_in for a in el.iterancestors()) or el.tag in skip_in:
            continue
        if el.tag in ("italic", "bold") and not any(a.tag in ("p", "td", "th", "title") for a in el.iterancestors()):
            continue
        # tails first, back to front, then the element's own text
        for child in reversed(list(el)):
            if child.tag in ("p", "sec", "fig", "table-wrap", "list"):
                continue
            if child.tail and _NOTATION.search(child.tail):
                _replace_slot(el, child, split(child.tail))
        if el.text and _NOTATION.search(el.text):
            _replace_slot(el, None, split(el.text))


def _checkbox_lists(body):
    """"□ success / X failure / ..." typed as paragraphs -> a <list>; returns the ticked option."""
    outcome = ""
    box = re.compile(r"^\s*([□☐☒☑]|X\s)")
    for parent in list(body.iter("body", "sec")):
        kids = list(parent)
        k = 0
        while k < len(kids):
            if kids[k].tag == "p" and box.match(_text(kids[k])):
                j = k
                while j < len(kids) and kids[j].tag == "p" and box.match(_text(kids[j])):
                    j += 1
                if j - k >= 2:
                    lst = etree.Element("list")
                    lst.set("list-type", "simple")
                    for p in kids[k:j]:
                        text = _text(p).strip()
                        ticked = bool(re.match(r"^(X\s|☒|☑)", text))
                        item = etree.SubElement(lst, "list-item")
                        _sub(item, "label", "☒" if ticked else "☐")
                        body_text = re.sub(r"^\S\s*", "", text, count=1)
                        _sub(item, "p", body_text)
                        if ticked:
                            outcome = body_text
                    parent.insert(list(parent).index(kids[k]), lst)
                    for p in kids[k:j]:
                        parent.remove(p)
                    kids = list(parent)
                    continue
            k += 1
    return outcome


def _credit_roles(statement_el, authors):
    """{author index: [(term, slug)]} from "Name: role, role; Name2: ..." (best effort)."""
    paras = [_text(p).strip() for p in statement_el.iter("p")]
    text = "\n".join(p for p in paras if p)
    text = re.sub(r"(?i)^the authors made the following contributions\.?\s*", "", text)
    out = {}
    for entry in re.split(r";\s+|\n+", text):
        m = re.match(r"^\s*([^:]{3,60}):\s*(.+?)\s*$", entry)
        if not m:
            continue
        who, roles_text = m.group(1), m.group(2)
        idx = next((i for i, a in enumerate(authors)
                    if split_name(a["name"])[1].lower() in who.lower()), None)
        if idx is None:
            continue
        terms = []
        for role in re.split(r",\s*(?=[A-Z])", roles_text):
            for pat, term, slug in CREDIT:
                if re.search(pat, role.strip(" ."), re.I):
                    if (term, slug) not in terms:
                        terms.append((term, slug))
                    break
        if terms:
            out[idx] = terms
    return out


def _extract_declarations(body, authors):
    """Pull the declaration sections out of the body.

    Returns {"funding": str, "coi": [p], "ack": sec, "contrib": sec, "roles": {...}}.
    Everything else stays in the body (with sec-type where the kind is clear).
    """
    found = {"funding": "", "coi": None, "ack": None, "contrib": None, "roles": {}}
    for sec in list(body.iter("sec")):
        title = (sec.findtext("title") or "").strip()
        if re.match(r"(?i)^acknowledg", title):
            kind = "ack"
        elif re.match(r"(?i)^funding", title):
            kind = "funding"
        elif re.match(r"(?i)^(potential\s+)?(conflicts?\s+of\s+interests?|competing\s+interests?)", title):
            kind = "coi"
        elif re.match(r"(?i)^(author\s+)?contributions?(\s+statement)?$|^credit", title):
            kind = "contrib"
        else:
            if re.search(r"(?i)data\s+availab|open research practices", title):
                sec.set("sec-type", "data-availability")
            elif re.search(r"(?i)\bAI\b.*(use|statement)|generative", title):
                sec.set("sec-type", "ai-use-statement")
            elif re.search(r"(?i)\bethic", title):
                sec.set("sec-type", "ethics-statement")
            continue
        if sec.find("sec") is not None:
            continue                                  # a parent section: leave alone
        sec.getparent().remove(sec)
        sec.tail = None
        if kind == "funding":
            found["funding"] = " ".join(" ".join(_text(p).split()) for p in sec.iter("p"))
        elif kind == "coi":
            found["coi"] = list(sec.iter("p"))
        elif kind == "ack":
            found["ack"] = sec
        else:
            found["contrib"] = sec
            found["roles"] = _credit_roles(sec, authors)
    # sections that only had declarations in them ("Declarations") disappear
    changed = True
    while changed:
        changed = False
        for sec in list(body.iter("sec")):
            if len(sec) == 1 and sec[0].tag == "title" and re.match(r"(?i)^declarations?$",
                                                                     (sec[0].text or "").strip()):
                sec.getparent().remove(sec)
                changed = True
    return found


def _fn_to_tables(root):
    """A footnote cited only inside one table belongs to that table's foot."""
    fg = root.find("back/fn-group")
    if fg is None:
        return
    for fn in list(fg.findall("fn")):
        rid = fn.get("id")
        if not rid:
            continue
        xrefs = root.xpath("//xref[@rid=$r]", r=rid)
        tws = {tuple(x.xpath("ancestor::table-wrap")) for x in xrefs}
        if len(xrefs) and len(tws) == 1 and next(iter(tws)):
            tw = next(iter(tws))[0]
            foot = tw.find("table-wrap-foot")
            if foot is None:
                foot = etree.SubElement(tw, "table-wrap-foot")
            new_id = "%sfn%d" % (tw.get("id"), len(foot.findall("fn")) + 1)
            fn.set("id", new_id)
            for x in xrefs:
                x.set("rid", new_id)
            fg.remove(fn)
            foot.append(fn)
    if not len(fg):
        fg.getparent().remove(fg)


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

_DOI = re.compile(r"https?://(?:dx\.)?doi\.org/(10\.[^\s<>\"]+)", re.I)
_YEAR = re.compile(r"^(?P<auth>.+?)\s*\((?P<year>\d{4}[a-z]?|n\.d\.)(?:,[^)]*)?\)\.\s*(?P<rest>.*)$", re.S)
_AUTHOR_SPLIT = re.compile(r"((?<=\.),\s+(?:&\s+)?|,\s+&\s+|\s+&\s+)")
_GIVEN = re.compile(r"(?:[A-ZÀ-Ý][A-Za-zà-ÿ]*\.?[\s\-]*)+")


def _ref_segments(p):
    segs = []
    for ch in p.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            kind, val = "t", (str(ch),)
        elif ch.name in ("em", "i"):
            kind, val = "i", (ch.get_text(),)
        elif ch.name == "a":
            kind, val = "a", (ch.get_text(), (ch.get("href") or "").strip())
        else:
            kind, val = "t", (ch.get_text(),)
        if kind == "t" and segs and segs[-1][0] == "t":
            segs[-1] = ("t", segs[-1][1] + val[0])
        else:
            segs.append((kind,) + val)
    return segs


def _person_group(mc, auth):
    """<person-group> with <string-name>s; False if the author list doesn't parse."""
    if "..." in auth or ". . ." in auth:
        return False
    parts = _AUTHOR_SPLIT.split(auth.strip())
    names = parts[0::2]
    seps = parts[1::2]
    parsed = []
    for n in names:
        n = n.strip()
        if not n:
            return False
        if "," in n:
            sur, giv = n.split(",", 1)
            giv = giv.strip()
            if not sur.strip() or not _GIVEN.fullmatch(giv):
                return False
            parsed.append(("name", sur.strip(), giv))
        else:
            parsed.append(("collab", n, ""))
    pg = _sub(mc, "person-group", **{"person-group-type": "author"})
    for k, item in enumerate(parsed):
        if item[0] == "name":
            sn = _sub(pg, "string-name")
            _sub(sn, "surname", item[1])
            _append_text(sn, ", ")
            _sub(sn, "given-names", item[2])
        else:
            _sub(pg, "collab", item[1])
        if k < len(seps):
            _append_text(pg, seps[k])
    return True


def _mixed_citation(parent, p, ctx):
    """<mixed-citation> with the parts APA references have tagged (authors, year, title,
    source, volume, pages, DOI). Anything that does not parse stays plain text."""
    mc = _sub(parent, "mixed-citation")
    segs = _ref_segments(p)
    full_text = " ".join(s[1] for s in segs)
    try:
        first = segs[0] if segs and segs[0][0] == "t" else None
        m = _YEAR.match(first[1]) if first else None
        if not m:
            raise ValueError("no author-year")
        if not _person_group(mc, m.group("auth")):
            _append_text(mc, m.group("auth"))
        _append_text(mc, " (")
        _sub(mc, "year", m.group("year"))
        _append_text(mc, "). ")
        segs = [("t", m.group("rest"))] + segs[1:]
        ptype, source_seen, volume_seen, pages_done = "other", False, False, False
        k = 0
        while k < len(segs):
            seg = segs[k]
            kind = seg[0]
            if kind == "t":
                s = seg[1]
                if k == 0 and s.strip() and len(segs) > 1 and segs[1][0] == "i":
                    title = s.rstrip()
                    end = ""
                    while title and title[-1] == ".":
                        title, end = title[:-1], "." + end
                    _sub(mc, "article-title", title)
                    _append_text(mc, end + s[len(s.rstrip()):])
                    k += 1
                    continue
                if volume_seen and not pages_done:
                    pm = re.match(r"^(\()(\d+[\w-]*)(\))(,\s*)([A-Za-z]?\d+[\w-]*)(?:([–\-])(\d+[\w-]*))?(.*)$", s, re.S)
                    em = re.match(r"^(,\s*)(Article\s+)(\S+?)([.,].*)$", s, re.S)
                    if pm:
                        _append_text(mc, "(")
                        _sub(mc, "issue", pm.group(2))
                        _append_text(mc, ")" + pm.group(4))
                        _sub(mc, "fpage", pm.group(5))
                        if pm.group(7):
                            _append_text(mc, pm.group(6))
                            _sub(mc, "lpage", pm.group(7))
                        s, pages_done = pm.group(8), True
                    elif em:
                        _append_text(mc, em.group(1) + em.group(2))
                        _sub(mc, "elocation-id", em.group(3))
                        s, pages_done = em.group(4), True
                pos = 0                                   # a plain-text DOI link
                for dm in _DOI.finditer(s):
                    _append_text(mc, s[pos:dm.start()])
                    _sub(mc, "pub-id", dm.group(1).rstrip(".,;"), **{"pub-id-type": "doi"})
                    pos = dm.end()
                _append_text(mc, s[pos:])
            elif kind == "i":
                txt = seg[1]
                if not source_seen:
                    _sub(mc, "source", txt)
                    source_seen = True
                    if k == 0 or segs[0][1].strip() == "":
                        ptype = "book"
                elif not volume_seen and re.fullmatch(r"\d+[A-Za-z]?", txt.strip()):
                    _sub(mc, "volume", txt)
                    volume_seen = True
                    ptype = "journal"
                else:
                    _sub(mc, "italic", txt)
            else:                                         # link
                txt, href = seg[1], seg[2]
                dm = _DOI.search(href) or _DOI.search(txt)
                if dm:
                    _sub(mc, "pub-id", dm.group(1).rstrip(".,;"), **{"pub-id-type": "doi"})
                elif re.match(r"^https?://", href):
                    el = _sub(mc, "ext-link", txt, **{"ext-link-type": "uri"})
                    _xlink(el, href)
                else:
                    _append_text(mc, txt)
            k += 1
        low = full_text.lower()
        if re.search(r"computer software|r package|\[software\]|\(version", low):
            ptype = "software"
        elif re.search(r"psyarxiv|arxiv|osf preprints|ssrn|biorxiv|metaarxiv|socarxiv|preprint", low):
            ptype = "preprint"
        elif re.search(r"proceedings|conference", low):
            ptype = "confproc"
        mc.set("publication-type", ptype)
    except Exception:  # noqa: BLE001 - keep the reference, untagged
        for ch in list(mc):
            mc.remove(ch)
        mc.text = None
        mc.attrib.pop("publication-type", None)
        _inline(mc, p, ctx)
    return mc


def _html_paras(html):
    soup = BeautifulSoup(html or "", "html.parser")
    paras = soup.find_all("p")
    return paras if paras else ([soup] if soup.get_text().strip() else [])


def reference_paragraphs(art):
    """The usable reference <p>s (an empty "#" left over from the conversion is dropped)."""
    return [p for p in _html_paras(art.get("referencesHtml"))
            if len(re.findall(r"\w", p.get_text())) >= 8]


# ---------------------------------------------------------------------------
# Front and back matter
# ---------------------------------------------------------------------------

def split_name(full):
    parts = full.split()
    if len(parts) < 2:
        return "", full.strip()
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in PARTICLES:
        i -= 1
    return " ".join(parts[:i]), " ".join(parts[i:])


def _front(root, art, site_url, journal, decl, outcome):
    front = _sub(root, "front")
    jm = _sub(front, "journal-meta")
    _sub(jm, "journal-id", "replicationresearch", **{"journal-id-type": "publisher-id"})
    tg = _sub(jm, "journal-title-group")
    _sub(tg, "journal-title", journal.get("name") or "Replication Research")
    if journal.get("issn"):
        _sub(jm, "issn", journal["issn"], **{"publication-format": "electronic"})
    if journal.get("publisherName"):
        _sub(_sub(jm, "publisher"), "publisher-name", journal["publisherName"])

    am = _sub(front, "article-meta")
    if art.get("doi"):
        _sub(am, "article-id", art["doi"], **{"pub-id-type": "doi"})
    _sub(am, "article-id", art["urlPath"], **{"pub-id-type": "publisher-id"})
    cats = _sub(am, "article-categories")
    if art.get("section"):
        g = _sub(cats, "subj-group", **{"subj-group-type": "heading"})
        _sub(g, "subject", art["section"])
    for c in art.get("categories") or []:
        g = _sub(cats, "subj-group", **{"subj-group-type": "discipline"})
        _sub(g, "subject", c)
    ttl = _sub(am, "title-group")
    _sub(ttl, "article-title", art["title"])
    if art.get("subtitle"):
        _sub(ttl, "subtitle", art["subtitle"])

    authors = art.get("authors") or []
    affs = []
    for a in authors:
        aff = (a.get("affiliation") or "").strip()
        if aff and aff not in affs:
            affs.append(aff)
    if authors:
        cg = _sub(am, "contrib-group")
        for n_author, a in enumerate(authors):
            c = _sub(cg, "contrib", **{"contrib-type": "author"})
            if a.get("corresponding"):
                c.set("corresp", "yes")
            if a.get("orcid"):
                _sub(c, "contrib-id", a["orcid"], **{"contrib-id-type": "orcid"})
            given, sur = split_name(a["name"])
            n = _sub(c, "name")
            _sub(n, "surname", sur)
            if given:
                _sub(n, "given-names", given)
            for term, slug in decl["roles"].get(n_author, []):
                _sub(c, "role", term, **{
                    "vocab": "credit", "vocab-term": term,
                    "vocab-term-identifier": "https://credit.niso.org/contributor-roles/%s/" % slug})
            aff = (a.get("affiliation") or "").strip()
            if aff:
                _sub(c, "xref", str(affs.index(aff) + 1), **{"ref-type": "aff",
                                                               "rid": "aff%d" % (affs.index(aff) + 1)})
        for i, aff in enumerate(affs, 1):
            _sub(_sub(cg, "aff", id="aff%d" % i), "institution", aff)

    if decl["coi"]:
        an = _sub(am, "author-notes")
        fn = _sub(an, "fn", **{"fn-type": "coi-statement"})
        for p in decl["coi"]:
            fn.append(copy.deepcopy(p))

    date = art.get("datePublished") or ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date)
    if m:
        pd = _sub(am, "pub-date", **{"publication-format": "electronic", "date-type": "pub",
                                      "iso-8601-date": date})
        _sub(pd, "day", m.group(3))
        _sub(pd, "month", m.group(2))
        _sub(pd, "year", m.group(1))
    vol = re.search(r"Vol\.?\s*(\d+)", art.get("issueTitle") or "")
    if vol:
        _sub(am, "volume", vol.group(1))
    if art.get("elocationId"):
        _sub(am, "elocation-id", art["elocationId"])
    history = [("received", art.get("submissionDate") or ""), ("accepted", art.get("acceptedDate") or "")]
    dates = [(t, d, re.match(r"^(\d{4})-(\d{2})-(\d{2})$", d)) for t, d in history]
    dates = [x for x in dates if x[2]]
    if dates:
        h = _sub(am, "history")
        for dtype, d, dm in dates:
            e = _sub(h, "date", **{"date-type": dtype, "iso-8601-date": d})
            _sub(e, "day", dm.group(3))
            _sub(e, "month", dm.group(2))
            _sub(e, "year", dm.group(1))

    lic = BeautifulSoup(art.get("licenseHtml") or "", "html.parser")
    lic_a = lic.find("a", href=re.compile(r"creativecommons\.org"))
    stmt = lic.find("p").get_text(" ", strip=True) if lic.find("p") else ""
    if stmt or lic_a:
        perms = _sub(am, "permissions")
        if stmt:
            _sub(perms, "copyright-statement", stmt)
            y = re.search(r"\b(20\d\d)\b", stmt)
            if y:
                _sub(perms, "copyright-year", y.group(1))
            holder = re.search(r"\b20\d\d\s+(.+)$", stmt)
            if holder:
                _sub(perms, "copyright-holder", holder.group(1).strip())
        if lic_a:
            lc = _sub(perms, "license", **{"license-type": "open-access"})
            _xlink(lc, lic_a["href"])
            _sub(lc, "{%s}license_ref" % ALI, lic_a["href"])
            ver = re.search(r"/licenses/([\w-]+)/([\d.]+)", lic_a["href"])
            _sub(lc, "license-p", "This is an open-access article distributed under the terms of "
                 "the Creative Commons %s %s International License."
                 % (ver.group(1).upper(), ver.group(2))
                 if ver else "This is an open-access article.")

    if site_url:
        _xlink(_sub(am, "self-uri", **{"content-type": "mirror"}), "%s/articles/%s/" % (site_url, art["urlPath"]))
    if art.get("ojsUrl"):
        _xlink(_sub(am, "self-uri", **{"content-type": "canonical"}), art["ojsUrl"])

    paras = _html_paras(art.get("abstractHtml"))
    if paras:
        ab = _sub(am, "abstract")
        ctx = _Ctx({})
        for p in paras:
            _inline(_sub(ab, "p"), p, ctx)
    if art.get("keywords"):
        kg = _sub(am, "kwd-group", **{"kwd-group-type": "author"})
        for k in art["keywords"]:
            _sub(kg, "kwd", k)
    if outcome:
        kg = _sub(am, "kwd-group", **{"kwd-group-type": "replication-outcome"})
        _sub(kg, "kwd", outcome)
    if decl["funding"]:
        fg = _sub(am, "funding-group")
        _sub(fg, "funding-statement", decl["funding"])
    return front


def _back(root, art, footnotes_html, ctx, decl, ref_paras):
    back = _sub(root, "back")
    if decl["ack"] is not None:
        ack = _sub(back, "ack")
        _sub(ack, "title", "Acknowledgements")
        for p in decl["ack"].iter("p"):
            ack.append(copy.deepcopy(p))
    fns = []
    if footnotes_html is not None and ctx.fn:
        for li in footnotes_html.find_all("li"):
            n = str(li.get("value") or "")
            if n not in ctx.fn:
                continue
            for bk in li.find_all("a", class_="footnote-backref"):
                bk.decompose()
            fns.append((n, li))
    if fns or decl["contrib"] is not None:
        fg = _sub(back, "fn-group")
        if decl["contrib"] is not None:
            fn = _sub(fg, "fn", **{"fn-type": "con"})
            for p in decl["contrib"].iter("p"):
                fn.append(copy.deepcopy(p))
        for n, li in fns:
            fn = _sub(fg, "fn", id=ctx.fn[n])
            _sub(fn, "label", n)
            _inline(_sub(fn, "p"), li, ctx)
    if ref_paras:
        rl = _sub(back, "ref-list")
        _sub(rl, "title", "References")
        c = _Ctx({})
        for i, p in enumerate(ref_paras, 1):
            r = _sub(rl, "ref", id="ref%d" % i)
            _mixed_citation(r, p, c)
    return back


def _indent(el, level=0):
    if el.tag in STRUCT and len(el) and not (el.text or "").strip():
        pad = "\n" + "  " * (level + 1)
        el.text = pad
        for i, ch in enumerate(el):
            ch.tail = pad if i < len(el) - 1 else "\n" + "  " * level
            _indent(ch, level + 1)
    elif len(el) and el.tag not in STRUCT:
        for ch in el:
            if isinstance(ch.tag, str) and ch.tag in STRUCT:
                _indent(ch, level + 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pick_body(slug, overrides_dir, fulltext_html, fig_prefix):
    """(html, figures, source) for the body: Markdown > the PDF-derived HTML > none."""
    md_path = os.path.join(overrides_dir, slug + ".md")
    if os.path.exists(md_path):
        from manuscript_fulltext import build_fulltext
        with open(md_path, encoding="utf-8") as fh:
            res = build_fulltext(fh.read(), fig_prefix, jats=True)
        if res["html"].strip():
            return res["html"], res["figures"], "markdown"
    if fulltext_html and fulltext_html.strip():
        return fulltext_html, [], "pdf-html"
    return "", [], "none"


def _linked_body(body_html, ref_paras, link):
    """The body HTML with in-text citations marked up (cite-ref spans that carry the
    id of the reference they belong to)."""
    soup = BeautifulSoup(body_html or "", "html.parser")
    for tip in soup.find_all(class_="cite-tooltip"):
        tip.decompose()
    for ref in soup.find_all(class_="cite-ref"):
        ref.unwrap()
    html = str(soup)
    if link and ref_paras:
        refs = "".join('<p><span class="rid" data-rid="ref%d"></span>%s</p>' % (i, p.decode_contents())
                       for i, p in enumerate(ref_paras, 1))
        try:
            html = link(html, refs)
        except Exception as e:  # noqa: BLE001 - citations stay plain text
            print("  JATS: in-text citation linking failed (%s)" % e, file=sys.stderr)
    return BeautifulSoup(html, "html.parser")


def build_jats(art, body_html="", site_url="", journal=None, source="markdown", link=None):
    """-> (xml_bytes, [graphic file names referenced])."""
    journal = journal or {}
    ref_paras = reference_paragraphs(art)
    soup = _linked_body(body_html, ref_paras, link)
    foot = soup.find("section", class_="fulltext-footnotes")
    ctx = _Ctx({})
    if foot:
        for li in foot.find_all("li"):
            n = str(li.get("value") or "")
            if n:
                ctx.fn[n] = "fn" + n

    body = etree.Element("body")
    names, counters = [], {"t": 0, "f": 0, "e": 0}
    if soup.get_text().strip() or soup.find("figure"):
        _blocks(body, soup, ctx, counters, names)
    _merge_split_paragraphs(body)
    outcome = _checkbox_lists(body)
    decl = _extract_declarations(body, art.get("authors") or [])
    if not len(body):
        _sub(body, "p", "Full text: see the PDF at the journal website.")

    root = etree.Element("article", nsmap=NSMAP)
    root.set("article-type", "research-article")
    root.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    root.set("dtd-version", "1.3")
    root.addprevious(etree.Comment(
        " Derived from the Replication Research mirror (%s). WIP - not the version of record: "
        "the PDF at the journal website is canonical. " % source))
    _front(root, art, site_url, journal, decl, outcome)
    root.append(body)
    _back(root, art, foot, ctx, decl, ref_paras)

    _fn_to_tables(root)
    _tidy_italics(root)
    labels = {}
    for el in root.iter("fig", "table-wrap"):
        m = re.search(r"([A-Z]?\d+)", el.findtext("label") or "")
        if m:
            labels[("fig" if el.tag == "fig" else "table", m.group(1))] = el.get("id")
    _link_text(root, labels)
    _indent(root)
    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", doctype=DOCTYPE)
    return xml, names


def validate_dtd(xml_bytes, dtd_path=None):
    """Problems reported by the JATS DTD, or [] (also [] if no DTD is available locally)."""
    dtd_path = dtd_path or os.environ.get("JATS_DTD")
    if not dtd_path or not os.path.exists(dtd_path):
        return []
    dtd = etree.DTD(dtd_path)
    doc = etree.fromstring(xml_bytes)
    dtd.validate(doc)
    return ["DTD line %s: %s" % (e.line, e.message[:160]) for e in dtd.error_log.filter_from_errors()]


def check(xml_bytes, figure_names=(), available_figures=()):
    """Structural checks (plus the DTD if available locally). -> [problem strings]."""
    problems = []
    root = etree.fromstring(xml_bytes)
    ids = {}
    for el in root.iter():
        i = el.get("id")
        if i:
            ids[i] = ids.get(i, 0) + 1
    problems += ["duplicate id %s" % k for k, v in ids.items() if v > 1]
    for el in root.iter("xref"):
        if el.get("rid") not in ids:
            problems.append("xref to missing id %s" % el.get("rid"))
    for n in figure_names:
        if available_figures and n not in available_figures:
            problems.append("graphic %s has no file" % n)
    if not (root.findtext(".//article-title") or "").strip():
        problems.append("no title")
    if not root.findall(".//contrib"):
        problems.append("no authors")
    if root.find(".//article-id[@pub-id-type='doi']") is None:
        problems.append("no DOI")
    if root.xpath("//ext-link//ext-link"):
        problems.append("nested ext-link")
    if root.xpath("//p//break"):
        problems.append("<break/> inside <p>")
    problems += validate_dtd(xml_bytes)
    return problems


# ---------------------------------------------------------------------------
# Command line: python3 scripts/jats.py <urlPath> [outdir]
# ---------------------------------------------------------------------------

def main(argv):
    import json
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root_dir, "scripts"))
    slug = argv[1]
    out = argv[2] if len(argv) > 2 else os.path.join(root_dir, "_jats", slug)
    with open(os.path.join(root_dir, "data", "articles.json"), encoding="utf-8") as fh:
        art = next(a for a in json.load(fh) if a["urlPath"] == slug)
    with open(os.path.join(root_dir, "data", "journal.json"), encoding="utf-8") as fh:
        journal = json.load(fh)
    from manuscript_override import parse_references
    md_path = os.path.join(root_dir, "overrides", slug + ".md")
    if os.path.exists(md_path):
        with open(md_path, encoding="utf-8") as fh:
            refs = parse_references(fh.read())
        if refs:
            art["referencesHtml"] = refs
    html, figures, source = pick_body(slug, os.path.join(root_dir, "overrides"), "", slug + "-")
    import build
    xml, names = build_jats(art, html, "https://www.replicationresearch.org", journal, source,
                            link=build.link_citations)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, slug + ".xml"), "wb") as fh:
        fh.write(xml)
    for name, data in figures:
        with open(os.path.join(out, slug + "-" + name), "wb") as fh:
            fh.write(data)
    problems = check(xml, names, {slug + "-" + n for n, _ in figures})
    print("%s: %d bytes, body from %s, %d graphics, problems: %s"
          % (slug, len(xml), source, len(names), problems[:12] or "none"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
