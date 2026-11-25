"""JATS XML (NISO Z39.96, v1.3) for an article of the mirror.

WIP - derived, not the version of record. The PDF stays canonical.

Inputs, in order of preference, for the *body* of the article:

  1. the manuscript's Markdown (overrides/<id>.md) - structured, so headings,
     tables, figures, footnotes and equations survive; it goes through the
     same parser as the HTML view (manuscript_fulltext.build_fulltext), and
     that HTML is what gets translated to JATS;
  2. the HTML full text the build already derived from the PDF (pdf_fulltext)
     - works, but tables/figures are only as good as the extraction;
  3. nothing: a metadata-only JATS (front matter + references).

The *front matter* (title, authors, ORCIDs, affiliations, DOI, dates, abstract,
keywords, licence, references) comes from the article record (data/articles.json
+ the editors' sheet extras + the Markdown reference list), never from the body.

The Word (.docx) exports are not read: the Markdown export of the same
Google Doc carries everything the .docx does for this purpose.

Used by build.py (articles with an overrides/<id>.jats marker file) and from
the command line:

    python3 scripts/jats.py 10085 [outdir]      # builds into _jats/10085/

Everything is best-effort per article: build_jats() raises, the caller decides.
"""

import os
import re
import sys

from bs4 import BeautifulSoup, Comment, NavigableString
from lxml import etree

XLINK = "http://www.w3.org/1999/xlink"
MML = "http://www.w3.org/1998/Math/MathML"
NSMAP = {"xlink": XLINK, "mml": MML}

DOCTYPE = ('<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving and '
           'Interchange DTD with MathML3 v1.3 20210610//EN" '
           '"https://jats.nlm.nih.gov/archiving/1.3/JATS-archivearticle1-3-mathml3.dtd">')

_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
PARTICLES = {"van", "von", "de", "der", "den", "da", "di", "du", "la", "le", "del", "della",
             "bin", "al", "el", "ter", "ten", "dos", "das", "do", "st."}

# elements whose children are all block-level: safe to indent (never p/title/td ...)
STRUCT = {"article", "front", "back", "body", "journal-meta", "journal-title-group",
          "article-meta", "article-categories", "subj-group", "title-group", "contrib-group",
          "contrib", "name", "pub-date", "history", "permissions", "license", "kwd-group",
          "abstract", "sec", "list", "list-item", "table-wrap", "table-wrap-foot", "table",
          "thead", "tbody", "tr", "fig", "caption", "disp-formula", "ref-list", "ref",
          "fn-group", "fn", "custom-meta-group", "custom-meta"}


def _clean(text):
    return _BAD_XML.sub("", text)


def _ncname(text, prefix):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")
    return "%s-%s" % (prefix, s or "x")


def _sub(parent, tag, text=None, **attrs):
    el = etree.SubElement(parent, tag)
    for k, v in attrs.items():
        if v is None:
            continue
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
        if ":" not in k and not k.startswith("xmlns") and k in ("display", "alttext"):
            m.set(k, " ".join(v) if isinstance(v, list) else str(v))
    conv(node, m)
    return m


class _Ctx:
    def __init__(self, footnote_ids):
        self.fn = footnote_ids          # {"3": "fn3", ...}
        self.n_eq = 0


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
        if name == "sup" and "fulltext-footref" in cls:
            a = ch.find("a")
            n = a.get_text().strip() if a else ch.get_text().strip()
            if n in ctx.fn:                     # JATS: <sup><xref ref-type="fn"/></sup>
                sup = _sub(parent, "sup")
                _sub(sup, "xref", n, **{"ref-type": "fn", "rid": ctx.fn[n]})
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
            _sub(parent, "break")
        elif name == "math":
            f = _sub(parent, "inline-formula")
            _mathml(f, ch)
        elif name == "a":
            href = (ch.get("href") or "").strip()
            if re.match(r"^(https?://|mailto:|ftp://)", href):
                el = _sub(parent, "ext-link", **{"ext-link-type": "uri"})
                _xlink(el, href)
                _inline(el, ch, ctx)
            else:
                _inline(parent, ch, ctx)        # internal anchor: keep the text only
        else:
            _inline(parent, ch, ctx)


def _split_label(node, kind):
    """Caption "Table 1. Title" -> ("Table 1", [title nodes copy-less: BeautifulSoup node])."""
    first = next((c for c in node.children if isinstance(c, NavigableString) and str(c).strip()), None)
    m = re.match(r"^\s*(%s\s+[\w.]+?)\.\s*" % kind, str(first)) if first is not None else None
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
            for cell in tr.find_all(["th", "td"], recursive=False):
                c = _sub(row, cell.name)
                for attr in ("colspan", "rowspan"):
                    if cell.get(attr):
                        c.set(attr, str(cell[attr]))
                m = re.search(r"text-align:\s*(left|right|center)", cell.get("style") or "")
                if m:
                    c.set("align", m.group(1))
                _inline(c, cell, ctx)
    return t


def _blocks(sec_root, container, ctx, counters, fig_dir_names):
    """Translate the children of `container` (BeautifulSoup) into JATS under
    sec_root (an lxml body/sec element); returns nothing."""
    stack = [(2, sec_root)]            # (heading level, element); body counts as level 2

    def cur():
        return stack[-1][1]

    for node in container.children:
        if isinstance(node, (Comment, NavigableString)):
            continue
        name, cls = node.name, node.get("class") or []
        if name in ("h3", "h4", "h2", "h5", "h6"):
            level = int(name[1])
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            sec = _sub(cur(), "sec", id=_ncname(node.get("id") or node.get_text(), "sec"))
            _sub(sec, "title").text = _clean(node.get_text().strip())
            stack.append((level, sec))
        elif name == "p":
            _inline(_sub(cur(), "p"), node, ctx)
        elif name == "ul" or name == "ol":
            lst = _sub(cur(), "list", **{"list-type": "bullet" if name == "ul" else "order"})
            for li in node.find_all("li", recursive=False):
                _inline(_sub(_sub(lst, "list-item"), "p"), li, ctx)
        elif name == "figure" and "fulltext-table" in cls:
            counters["t"] += 1
            tw = _sub(cur(), "table-wrap", id="tbl%d" % counters["t"])
            cap = node.find("figcaption")
            label = ""
            if cap:
                label, cap = _split_label(cap, "Table")
                if label:
                    _sub(tw, "label", label)
                c = _sub(tw, "caption")
                _inline(_sub(c, "title"), cap, ctx)
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
            label = ""
            if cap:
                label, cap = _split_label(cap, "Figure")
                if label:
                    _sub(fig, "label", label)
                c = _sub(fig, "caption")
                _inline(_sub(c, "p"), cap, ctx)
            img = node.find("img")
            if img and img.get("src"):
                g = _sub(fig, "graphic")
                fname = img["src"].rsplit("/", 1)[-1]
                _xlink(g, fname)
                fig_dir_names.append(fname)
                if img.get("alt"):
                    _sub(g, "alt-text", img["alt"])
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
            text = node.get_text().strip()
            if text:
                _inline(_sub(cur(), "p"), node, ctx)


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


def _html_paras(html):
    soup = BeautifulSoup(html or "", "html.parser")
    paras = soup.find_all("p")
    return paras if paras else ([soup] if soup.get_text().strip() else [])


def _front(root, art, site_url, journal):
    front = _sub(root, "front")
    jm = _sub(front, "journal-meta")
    _sub(jm, "journal-id", "replicationresearch", **{"journal-id-type": "publisher-id"})
    tg = _sub(jm, "journal-title-group")
    _sub(tg, "journal-title", journal.get("name") or "Replication Research")
    if journal.get("issn"):
        _sub(jm, "issn", journal["issn"])

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
        for a in authors:
            c = _sub(cg, "contrib", **{"contrib-type": "author"})
            if a.get("orcid"):
                _sub(c, "contrib-id", a["orcid"], **{"contrib-id-type": "orcid"})
            given, sur = split_name(a["name"])
            n = _sub(c, "name")
            _sub(n, "surname", sur)
            if given:
                _sub(n, "given-names", given)
            aff = (a.get("affiliation") or "").strip()
            if aff:
                _sub(c, "xref", str(affs.index(aff) + 1), **{"ref-type": "aff",
                                                               "rid": "aff%d" % (affs.index(aff) + 1)})
        for i, aff in enumerate(affs, 1):
            _sub(cg, "aff", aff, id="aff%d" % i)

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
    sub = art.get("submissionDate") or ""
    sm = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", sub)
    if sm:
        h = _sub(am, "history")
        d = _sub(h, "date", **{"date-type": "received", "iso-8601-date": sub})
        _sub(d, "day", sm.group(3))
        _sub(d, "month", sm.group(2))
        _sub(d, "year", sm.group(1))

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
        if lic_a:
            lc = _sub(perms, "license", **{"license-type": "open-access"})
            _xlink(lc, lic_a["href"])
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
    return front


def _back(root, art, footnotes_html, ctx):
    back = _sub(root, "back")
    if footnotes_html is not None and ctx.fn:
        fg = _sub(back, "fn-group")
        for li in footnotes_html.find_all("li"):
            n = str(li.get("value") or "")
            if n not in ctx.fn:
                continue
            for bk in li.find_all("a", class_="footnote-backref"):
                bk.decompose()
            fn = _sub(fg, "fn", id=ctx.fn[n])
            _sub(fn, "label", n)
            _inline(_sub(fn, "p"), li, ctx)
    refs = _html_paras(art.get("referencesHtml"))
    if refs:
        rl = _sub(back, "ref-list")
        _sub(rl, "title", "References")
        c = _Ctx({})
        for i, p in enumerate(refs, 1):
            r = _sub(rl, "ref", id="ref%d" % i)
            _inline(_sub(r, "mixed-citation"), p, c)
    return back


def _indent(el, level=0):
    if el.tag in STRUCT and len(el) and not (el.text or "").strip():
        pad = "\n" + "  " * (level + 1)
        el.text = pad
        for i, ch in enumerate(el):
            ch.tail = pad if i < len(el) - 1 else "\n" + "  " * level
            _indent(ch, level + 1)
    elif len(el) and not (el.tag in STRUCT):
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
            res = build_fulltext(fh.read(), fig_prefix)
        if res["html"].strip():
            return res["html"], res["figures"], "markdown"
    if fulltext_html and fulltext_html.strip():
        return fulltext_html, [], "pdf-html"
    return "", [], "none"


def build_jats(art, body_html="", site_url="", journal=None, source="markdown"):
    """-> (xml_bytes, [graphic file names referenced])."""
    journal = journal or {}
    root = etree.Element("article", nsmap=NSMAP)
    root.set("article-type", "research-article")
    root.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    root.set("dtd-version", "1.3")
    root.addprevious(etree.Comment(
        " Derived from the Replication Research mirror (%s). WIP - not the version of record: "
        "the PDF at the journal website is canonical. " % source))

    soup = BeautifulSoup(body_html or "", "html.parser")
    foot = soup.find("section", class_="fulltext-footnotes")
    ctx = _Ctx({})
    if foot:
        for li in foot.find_all("li"):
            n = str(li.get("value") or "")
            if n:
                ctx.fn[n] = "fn" + n
    _front(root, art, site_url, journal)
    body = _sub(root, "body")
    names = []
    counters = {"t": 0, "f": 0, "e": 0}
    if soup.get_text().strip() or soup.find("figure"):
        _blocks(body, soup, ctx, counters, names)
    _back(root, art, foot, ctx)
    if not len(body):                    # JATS wants a body even if we only have metadata
        _sub(body, "p", "Full text: see the PDF at the journal website.")
    _indent(root)
    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", doctype=DOCTYPE)
    return xml, names


def check(xml_bytes, figure_names=(), available_figures=()):
    """Structural checks the schema would otherwise do. -> [problem strings]."""
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
    if root.find(".//article-title") is None or not (root.findtext(".//article-title") or "").strip():
        problems.append("no title")
    if not root.findall(".//contrib"):
        problems.append("no authors")
    if root.find(".//article-id[@pub-id-type='doi']") is None:
        problems.append("no DOI")
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
    xml, names = build_jats(art, html, "https://www.replicationresearch.org", journal, source)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, slug + ".xml"), "wb") as fh:
        fh.write(xml)
    for name, data in figures:
        with open(os.path.join(out, slug + "-" + name), "wb") as fh:
            fh.write(data)
    problems = check(xml, names, {slug + "-" + n for n, _ in figures})
    print("%s: %d bytes, body from %s, %d graphics, problems: %s"
          % (slug, len(xml), source, len(names), problems or "none"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
