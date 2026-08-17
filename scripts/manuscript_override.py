"""Per-article Markdown override for the two hardest things to get right
out of a PDF: tables and references. Drop a copy-edited manuscript export
(Google Docs' own Markdown export format) at overrides/<urlPath>.md and its
tables/references take over from what scripts/pdf_fulltext.py's PDF
extraction and scripts/fetch.py's OJS scrape would otherwise produce for
that one article - everything else about the article page (headings,
prose, figures, footnotes) still comes from the PDF as before. No override
file for an article -> the existing pipeline runs completely unchanged.

Deliberately NOT a general Markdown parser: only the constructs actually
observed in a real Google-Docs-exported manuscript are handled -
`[text](url)` links, `**bold**`, `*italic*`, and the `\\.`/`\\+`/`\\-`-style
backslash-escaping that export produces. Everything here is best-effort,
matching pdf_fulltext.py's own philosophy: a malformed table or reference
paragraph is skipped with a stderr warning, never raised - a broken
override file must never break the site build.
"""

import html
import os
import re
import sys

# ---------------------------------------------------------------------------
# Minimal inline Markdown -> HTML, scoped to what a Google-Docs export uses.
# ---------------------------------------------------------------------------

_ESCAPE_RE = re.compile(r"\\([.\-+_*\[\]()`!#])")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")


def _md_inline(text):
    """Unescape Google-Docs export artifacts, then convert the limited
    Markdown subset used in these manuscripts to HTML. Order matters:
    unescaping happens on raw text before html.escape (backslash isn't
    touched by html.escape either way); escaping happens before any real
    tag is inserted, so a literal '&' or '<' in an author name or title is
    handled correctly and the tags just inserted are never re-escaped.
    Bold is converted before italic so '**' isn't half-eaten by the '*'
    pattern. No bare-URL autolinking here (unlike pdf_fulltext.py's own
    _autolink_urls) - every reference in the one real file this was built
    against already uses proper [text](url) link syntax, and naively
    autolinking bare URLs after _LINK_RE has already run risks matching
    the URL sitting inside an just-inserted href="..." attribute and
    double-wrapping it."""
    text = _ESCAPE_RE.sub(r"\1", text)
    text = html.escape(text)
    text = _LINK_RE.sub(lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


# ---------------------------------------------------------------------------
# Tables: "**Table N\.** Caption" followed by a GFM pipe table.
# ---------------------------------------------------------------------------

TABLE_CAPTION_RE = re.compile(r"^\*\*Table\s+(\d+)[A-Za-z]?\\?\.\*\*\s*(.*)$",
                               re.IGNORECASE)


def _split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _align_style(sep_cell):
    sep_cell = sep_cell.strip()
    left, right = sep_cell.startswith(":"), sep_cell.endswith(":")
    if left and right:
        return ' style="text-align:center"'
    if right:
        return ' style="text-align:right"'
    if left:
        return ' style="text-align:left"'
    return ""


def _table_to_html(table_lines, num, caption_rest):
    """One <figure class="fulltext-figure fulltext-table"> matching the
    exact visual convention pdf_fulltext.py's own _figure_html(...,
    caption_first=True) already uses for PDF-rendered tables, so an
    overridden table and a PDF-rendered one look consistent."""
    header_cells = _split_row(table_lines[0])
    align_cells = _split_row(table_lines[1]) if len(table_lines) > 1 else []
    aligns = ([_align_style(c) for c in align_cells] if align_cells
              else [""] * len(header_cells))

    def cell_style(i):
        return aligns[i] if i < len(aligns) else ""

    thead = "<tr>" + "".join(
        "<th%s>%s</th>" % (cell_style(i), _md_inline(c))
        for i, c in enumerate(header_cells)) + "</tr>"

    body_rows = []
    for row_line in table_lines[2:]:
        cells = _split_row(row_line)
        tds = "".join("<td%s>%s</td>" % (cell_style(i), _md_inline(c))
                       for i, c in enumerate(cells))
        body_rows.append("<tr>%s</tr>" % tds)

    table_html = "<table><thead>%s</thead><tbody>%s</tbody></table>" % (
        thead, "".join(body_rows))
    return ('<figure class="fulltext-figure fulltext-table">'
            '<figcaption>Table %d. %s</figcaption>%s</figure>'
            % (num, _md_inline(caption_rest), table_html))


def parse_tables(md_text):
    """{table_number: html} for every '**Table N\\.** caption' + following
    GFM pipe table found in md_text. A single malformed table (bad pipe
    count, no table found right after a caption) is skipped with a stderr
    warning - every other table in the same file still parses."""
    lines = md_text.splitlines()
    tables, i = {}, 0
    while i < len(lines):
        m = TABLE_CAPTION_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        num, caption_rest = int(m.group(1)), m.group(2).strip()
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        table_lines = []
        while j < len(lines) and lines[j].strip().startswith("|"):
            table_lines.append(lines[j])
            j += 1
        i = j
        if len(table_lines) < 2:
            continue  # caption with no table right after it - skip
        try:
            tables[num] = _table_to_html(table_lines, num, caption_rest)
        except Exception as e:  # noqa: BLE001 - one bad table must not break the rest
            print("  override table %d failed to parse: %s" % (num, e), file=sys.stderr)
    return tables


# ---------------------------------------------------------------------------
# References: "# References" section, one reference per blank-line-
# separated paragraph.
# ---------------------------------------------------------------------------

REFERENCES_HEADING_RE = re.compile(r"^#{1,6}\s+\*{0,2}References\*{0,2}\s*$",
                                    re.IGNORECASE)
NEXT_HEADING_RE = re.compile(r"^#{1,6}\s")


def parse_references(md_text):
    """A bare <p>...</p><p>...</p>... string, one per reference - matches
    the exact shape article["referencesHtml"] already has today (a flat
    sequence of <p> tags scraped from OJS's own references block, no
    wrapping container), so templates/article.html needs zero changes to
    accept either source. "" if no References heading is found. A single
    bad reference paragraph is skipped with a stderr warning, not fatal."""
    lines = md_text.splitlines()
    start = next((i + 1 for i, l in enumerate(lines)
                  if REFERENCES_HEADING_RE.match(l.strip())), None)
    if start is None:
        return ""
    end = next((i for i in range(start, len(lines))
                if NEXT_HEADING_RE.match(lines[i].strip())), len(lines))

    paragraphs, current = [], []
    for line in lines[start:end]:
        stripped = line.strip()
        # A blank line, OR a lone underscore-rule line (a Google-Docs
        # export artifact that sits between the last reference and the
        # next section, written as backslash-escaped underscores) - either
        # ends the current reference paragraph.
        is_break = not stripped or set(stripped.replace("\\", "")) <= {"_"}
        if is_break:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))

    out = []
    for p in paragraphs:
        try:
            out.append("<p>%s</p>" % _md_inline(p))
        except Exception as e:  # noqa: BLE001
            print("  override reference paragraph failed to parse: %s" % e,
                  file=sys.stderr)
    return "".join(out)


# ---------------------------------------------------------------------------
# Loading + applying an override.
# ---------------------------------------------------------------------------

def load_override(overrides_dir, url_path):
    """{"referencesHtml": str, "tables": {n: html}} or None if no override
    file exists for this article, it can't be read, or nothing usable
    parsed from it. Never raises."""
    path = os.path.join(overrides_dir, os.path.basename(url_path) + ".md")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception as e:  # noqa: BLE001
        print("  override %s: could not read file: %s" % (path, e), file=sys.stderr)
        return None

    references_html, tables = "", {}
    try:
        references_html = parse_references(text)
    except Exception as e:  # noqa: BLE001
        print("  override %s: references parse failed: %s" % (path, e), file=sys.stderr)
    try:
        tables = parse_tables(text)
    except Exception as e:  # noqa: BLE001
        print("  override %s: table parse failed: %s" % (path, e), file=sys.stderr)

    if not references_html and not tables:
        return None
    return {"referencesHtml": references_html, "tables": tables}


FULLTEXT_TABLE_BLOCK_RE = re.compile(
    r'<figure class="fulltext-figure fulltext-table">.*?</figure>', re.DOTALL)
CAPTION_NUM_RE = re.compile(r"<figcaption>\s*Table\s*(\d+)[A-Za-z]?\s*[.:]",
                             re.IGNORECASE)


def apply_table_overrides(fulltext_html, tables_by_number, url_path=""):
    """Swap each PDF-rendered <figure class="fulltext-figure fulltext-
    table">...</figure> block in fulltext_html for its matching override
    table, matched by the table number in the block's OWN <figcaption>
    text (not by rendering order - pdf_fulltext.py's own table-numbering
    counter isn't guaranteed to equal the manuscript's printed number if a
    table ever fails to render). A table number named in the override but
    never found in the rendered fulltext is logged and otherwise ignored;
    a table rendered by the PDF pipeline but not covered by the override
    is left exactly as the PDF pipeline produced it."""
    if not tables_by_number or not fulltext_html:
        return fulltext_html
    matched = set()

    def repl(m):
        block = m.group(0)
        cm = CAPTION_NUM_RE.search(block)
        if not cm:
            return block
        num = int(cm.group(1))
        if num in tables_by_number:
            matched.add(num)
            return tables_by_number[num]
        return block

    try:
        out = FULLTEXT_TABLE_BLOCK_RE.sub(repl, fulltext_html)
    except Exception as e:  # noqa: BLE001 - never break the build
        print("  %s: table override swap failed: %s" % (url_path, e), file=sys.stderr)
        return fulltext_html

    unused = set(tables_by_number) - matched
    if unused:
        print("  %s: override table(s) %s not found in PDF-rendered fulltext, skipped"
              % (url_path, sorted(unused)), file=sys.stderr)
    return out
