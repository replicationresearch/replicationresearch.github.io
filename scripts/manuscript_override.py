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

_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")  # CommonMark: backslash + any ASCII punctuation
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
    text = text.replace("\\*", "\x01")        # an escaped "G\*Power" must not open italics
    text = _ESCAPE_RE.sub(r"\1", text)
    text = html.escape(text)
    text = _LINK_RE.sub(lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    return text.replace("\x01", "*")


# ---------------------------------------------------------------------------
# Tables: a bold "Table N" caption line followed by a GFM pipe table. The
# manuscripts write it in several ways, all handled:
#   **Table 1\.** Caption          **Table 1**. Caption
#   **Table 3**  + italic caption on the next line   (APA style)
#   ## **Table 1** + ## *Caption*                    (headings)
# A *Note.* / *Source* paragraph right after the table is kept under it.
# ---------------------------------------------------------------------------

TABLE_CAPTION_RE = re.compile(
    r"^(?:#{1,6}\s*)?\*\*Table\s+(\d+)[A-Za-z]?\s*\\?\.?\*\*\s*\\?\.?\s*(.*)$",
    re.IGNORECASE)
_PIPE = re.compile(r"(?<!\\)\|")
NOTE_RE = re.compile(r"^\*{0,2}(Note|Notes|Source)\b", re.IGNORECASE)


def _split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip() for c in _PIPE.split(line)]


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


def _join_wrapped_rows(raw_lines):
    """Google Docs puts a line break inside a cell as a real newline, so one
    row can span several lines; a row is complete once a line ends with '|'."""
    rows, current = [], ""
    for line in raw_lines:
        current = (current + " " + line.strip()) if current else line.strip()
        if current.endswith("|") and not current.endswith("\\|"):
            rows.append(current)
            current = ""
    if current:
        rows.append(current)
    return rows


def _table_to_html(table_lines, num, caption_rest, notes=()):
    """One <figure class="fulltext-figure fulltext-table"> matching the
    exact visual convention pdf_fulltext.py's own _figure_html(...,
    caption_first=True) already uses for PDF-rendered tables, so an
    overridden table and a PDF-rendered one look consistent."""
    rows = _join_wrapped_rows(table_lines)
    header_cells = _split_row(rows[0])
    ncols = len(header_cells)
    align_cells = _split_row(rows[1]) if len(rows) > 1 else []
    aligns = ([_align_style(c) for c in align_cells] if align_cells
              else [""] * ncols)

    def cell_style(i):
        return aligns[i] if i < len(aligns) else ""

    # An empty header cell right after a filled one is a column-spanning
    # group header ("Employers who participated only in the FE | | |").
    ths, i = [], 0
    while i < ncols:
        span = 1
        if header_cells[i]:
            while i + span < ncols and not header_cells[i + span]:
                span += 1
        attr = ' colspan="%d"' % span if span > 1 else ""
        style = ' style="text-align:center"' if span > 1 else cell_style(i)
        ths.append("<th%s%s>%s</th>" % (attr, style, _md_inline(header_cells[i])))
        i += span
    thead = "<tr>%s</tr>" % "".join(ths)

    body_rows, notes = [], list(notes)
    for row in rows[2:]:
        cells = _split_row(row)
        filled = [c for c in cells if c]
        if ncols > 1 and len(filled) == 1 and cells[0] and (len(cells) == 1 or len(cells[0]) > 60):
            notes.insert(0, cells[0])            # "Source: ..." spanning the table
            continue
        cells += [""] * (ncols - len(cells))
        tds = "".join("<td%s>%s</td>" % (cell_style(i), _md_inline(c))
                       for i, c in enumerate(cells))
        body_rows.append("<tr>%s</tr>" % tds)

    table_html = "<table><thead>%s</thead><tbody>%s</tbody></table>" % (
        thead, "".join(body_rows))
    note_html = "".join('<p class="table-note">%s</p>' % _md_inline(n) for n in notes)
    caption = ('<figcaption>Table %d. %s</figcaption>' % (num, _md_inline(caption_rest))
               if num else "")
    return ('<figure class="fulltext-figure fulltext-table">%s%s%s</figure>'
            % (caption, table_html, note_html))


def iter_tables(lines, start=0, end=None):
    """Yield (first line index, index after the table block, number, html) for
    every bold "Table N" caption + following GFM pipe table in lines[start:end].
    A single malformed table (no table found after a caption) is skipped with a
    stderr warning - every other table still parses."""
    end = len(lines) if end is None else end
    i = start
    while i < end:
        m = TABLE_CAPTION_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        first = i
        num, caption_rest = int(m.group(1)), m.group(2).strip()
        j = i + 1
        if not caption_rest:
            # look ahead: caption lines, then the table (give up after a few lines)
            k, cap = j, []
            while k < end and k < j + 8 and not lines[k].strip().startswith("|"):
                text = lines[k].strip().lstrip("#").strip()
                if text:
                    cap.append(text.strip("*").strip())
                k += 1
            if k < end and lines[k].strip().startswith("|"):
                caption_rest, j = " ".join(cap), k
        while j < end and not lines[j].strip():
            j += 1
        table_lines = []
        while j < end and (lines[j].strip().startswith("|")
                           or (table_lines and not table_lines[-1].rstrip().endswith("|")
                               and lines[j].strip())):
            table_lines.append(lines[j])
            j += 1
        notes = []
        k = j
        while k < end and not lines[k].strip():
            k += 1
        if table_lines and k < end and NOTE_RE.match(lines[k].strip()):
            notes.append(lines[k].strip())
            j = k + 1
        i = j
        if len(table_lines) < 2:
            continue  # caption with no table right after it - skip
        try:
            yield first, j, num, _table_to_html(table_lines, num, caption_rest, notes)
        except Exception as e:  # noqa: BLE001 - one bad table must not break the rest
            print("  override table %d failed to parse: %s" % (num, e), file=sys.stderr)


def parse_tables(md_text):
    """{table_number: html} for every captioned table in md_text."""
    return {num: html for _, _, num, html in iter_tables(md_text.splitlines())}


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
        # A Markdown hard break (two trailing spaces) also ends a reference:
        # some exports list one reference per line with no blank lines.
        if line.endswith("  "):
            paragraphs.append(" ".join(current))
            current = []
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
