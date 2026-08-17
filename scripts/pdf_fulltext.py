"""Extract an article PDF's body as HTML for the article page's Full Text
section: structured text (headings from bookmarks/bold spans, de-hyphenated
paragraphs), figures rendered to PNG (caption-anchored clip rendering, so
vector charts survive), and display equations rendered to PNG.

Everything here is best-effort on purpose: extraction must never break a
build, so extract_fulltext() swallows its own failures and returns empty
results, leaving the article page with just the pdf.js viewer as before.
"""

import html
import re
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# Placed images smaller than this are page furniture (ORCID icons, logos,
# license badges), not figures.
MIN_FIG_W, MIN_FIG_H = 100, 60

# Tables can be short (few rows), so their minimum height is lower than a
# figure's.
MIN_TABLE_W, MIN_TABLE_H = 100, 40

# A period/colon right after the number is required so a body sentence that
# happens to start with "Table 3 reports..." doesn't get mistaken for the
# caption "Table 3.".
CAPTION_RE = re.compile(r"^(Figure|Fig\.|Table)\s*\d+[a-zA-Z]?\s*[.:]", re.IGNORECASE)
PAGE_NUM_RE = re.compile(r"^\d{1,3}$")
MATH_FONT_RE = re.compile(r"CM[A-Z]|Math|Symbol|MSAM|MSBM|MJX")
BOLD_FLAG = 16
ZWSP = "​"    # a zero-width space PyMuPDF sometimes emits
                    # as its own span right after a bullet glyph

# Broad set used only to keep a bulleted line from being misread as a
# heading - false negatives here are cheap, so dashes/asterisks/middle-dot
# stay included even though they're unreliable as a "this line STARTS a new
# bullet item" signal (see BULLET_ITEM_CHARS below, used for that instead).
BULLET_CHARS = "•‣▪✦◦●·*–—-"

# Strict subset of BULLET_CHARS used only to decide "does this line start a
# NEW bullet item" - deliberately excludes '-', '–', '—', '·', '*'. Those
# land at the start of an ordinary line-wrapped continuation purely by
# coincidence of reflow (confirmed in practice: "Writing – Original Draft"
# wraps so the continuation line starts with the en dash) - treating them as
# list markers would fragment ordinary prose into fake list items.
BULLET_ITEM_CHARS = "•‣▪✦◦●"

# A bold run at a line's very start, immediately naming a label ("Haining
# Wang:", "Manipulations and measures:") - the signal for a forced paragraph
# break mid-block. No period before the colon (so it doesn't fire past a
# sentence boundary); colon within roughly the first 80 chars (a label
# phrase, not an arbitrary later colon in body prose).
_LABEL_COLON_RE = re.compile(r"^[^.:]{1,80}:")

# Footnotes: a footnote's own leading number (e.g. "1" at the start of a
# footnote block at the bottom of the page) is typically NOT
# superscript-flagged in practice (confirmed against this corpus - only
# the in-text reference mark tends to be), so detection relies on font
# size relative to body_size plus page position, not PyMuPDF's
# superscript span flag.
FOOTNOTE_MARKER_RE = re.compile(r"^(\d{1,3})[.\)]?$")
FOOTNOTE_MARKER_SIZE_DELTA = 2.5    # a leading/reference marker span must be
                                    # at least this much smaller than body_size
FOOTNOTE_BODY_DELTA_MIN = 0.5       # a footnote block's own body text is
FOOTNOTE_BODY_DELTA_MAX = 4.0       # this much smaller than body_size
FOOTNOTE_Y_FRAC = 0.55              # ...and starts in the bottom ~45% of the
                                    # page (excludes author/affiliation
                                    # superscripts near the top of page 1,
                                    # which can otherwise look identical)

# Once one of these headings is reached, everything from there to the end of
# the document is skipped: the reference list is scraped from OJS and shown
# in its own section on the article page (so the PDF's copy would just
# duplicate it), and an "Open Science Badges" section - along with whatever
# running-footer boilerplate (license line, DOI, journal tagline) trails
# after it on the PDF's last page - duplicates the sidebar's own badges.
EXCLUDED_SECTION_HEADINGS = {"references", "bibliography", "literature", "literatur",
                             "open science badges"}


def _norm(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def _furniture_key(text):
    """Normalization for header/footer detection: running footers often
    carry the page number inside the same block ("... | 7"), which would
    make every page's footer unique - strip leading/trailing digits and
    separator punctuation before comparing."""
    t = _norm(text)
    t = re.sub(r"^[\s\d|·•—–-]+", "", t)
    t = re.sub(r"[\s\d|·•—–-]+$", "", t)
    return t


def _assemble_block(block, span_join):
    """Join a text block's lines into one de-hyphenated paragraph string,
    using span_join(spans) to turn each line's own spans into text."""
    text = ""
    for line in block["lines"]:
        piece = span_join(line["spans"]).strip()
        if not piece:
            continue
        if text.endswith("-") and text[-2:-1].isalpha() and piece[:1].islower():
            text = text[:-1] + piece       # re-join a line-break hyphenation
        elif text:
            text += " " + piece
        else:
            text = piece
    return re.sub(r"\s+", " ", text).strip()


def _plain_join(spans):
    """Join one line's spans verbatim - today's behavior, used wherever
    footnote-aware markup isn't needed (furniture/caption matching etc,
    where exact inter-span spacing doesn't affect the comparison)."""
    return "".join(span["text"] for span in spans)


def _spaced_join(spans):
    """Like _plain_join, but inserts a space at a style boundary (size or
    bold changes) when neither side already has whitespace there and both
    boundary characters are alphanumeric. PyMuPDF doesn't reliably carry a
    separating space at such boundaries - e.g. a footnote's small marker
    span immediately followed by its body-text span with no leading
    space, which otherwise glues the marker onto the next word
    ("1The incentive to..."). Punctuation-adjacent and same-style splits
    are left untouched, so real words broken across spans by kerning
    alone aren't affected."""
    out = ""
    prev_style = None
    for span in spans:
        s = span["text"]
        if not s:
            continue
        style = (round(span["size"], 1), bool(span["flags"] & BOLD_FLAG))
        if (out and prev_style is not None and style != prev_style
                and not out[-1].isspace() and not s[:1].isspace()
                and out[-1].isalnum() and s[:1].isalnum()):
            out += " "
        out += s
        prev_style = style
    return out


def _block_text(block):
    """Join a text block's lines into one de-hyphenated paragraph string."""
    return _assemble_block(block, _plain_join)


def _span_stats(block):
    """(dominant size, dominant-bold?, math-char share) for a text block."""
    by_style = {}
    math_chars = total_chars = 0
    for line in block["lines"]:
        for span in line["spans"]:
            n = len(span["text"].strip())
            if not n:
                continue
            key = (round(span["size"], 1), bool(span["flags"] & BOLD_FLAG))
            by_style[key] = by_style.get(key, 0) + n
            total_chars += n
            if MATH_FONT_RE.search(span["font"]):
                math_chars += n
    if not by_style:
        return 0.0, False, 0.0
    (size, bold), _ = max(by_style.items(), key=lambda kv: kv[1])
    return size, bold, math_chars / total_chars


def _line_raw_text(line):
    """Whitespace-normalized text of one line - the per-LINE analogue of
    _block_text(), needed because _split_block_segments() inspects lines
    individually rather than a whole block's assembled text."""
    return re.sub(r"\s+", " ", _plain_join(line["spans"])).strip()


def _is_heading_line(line, body_size, toc_titles):
    """Line-level analogue of pass 2's block-level heading formula - lets a
    heading-styled LINE be recognized even when it's trapped inside a
    larger block whose block-wide DOMINANT style (by character count) is
    something else entirely (confirmed case: a heading glued onto the end
    of a preceding bullet list, whose block-wide dominant style is the
    bullets' own plain body text)."""
    text = _line_raw_text(line)
    if not text:
        return False
    size, bold, _ = _span_stats({"lines": [line]})
    norm = _norm(text)
    return ((bold and size >= body_size + 1) or norm in toc_titles) \
        and len(text) < 120 \
        and text.lstrip(ZWSP)[:1] not in BULLET_CHARS \
        and not text.rstrip().endswith(".")


def _bullet_marker_len(line):
    """Number of leading spans in `line` that make up a bullet-list marker
    (the glyph span, plus any purely-whitespace span before the body-text
    span starts) - 0 if the line doesn't open with one. Restricted to
    BULLET_ITEM_CHARS, not the broader BULLET_CHARS - see that constant's
    comment for why."""
    spans = line["spans"]
    idx, saw_marker = 0, False
    while idx < len(spans):
        t = spans[idx]["text"].strip(ZWSP).strip()
        if t == "":
            idx += 1
            continue
        if not saw_marker and len(t) == 1 and t in BULLET_ITEM_CHARS:
            saw_marker = True
            idx += 1
            continue
        break
    return idx if saw_marker else 0


def _strip_bullet_marker(seg_lines):
    """Block-shaped {"lines": [...]} for a bullet_item segment's lines,
    with the FIRST line's leading marker span(s) dropped before rendering
    its <li> - the bullet glyph sits in its own span (e.g. '●' followed by
    a zero-width-space span), ahead of the body-text span, so this is just
    skipping leading spans rather than text-splicing. Any continuation
    lines (a multi-line bullet item wrapping without their own marker)
    pass through unchanged."""
    first, rest = seg_lines[0], seg_lines[1:]
    n = _bullet_marker_len(first)
    if n:
        first = {"spans": first["spans"][n:]}
    return {"lines": [first] + rest}


def _is_label_start(line):
    """True if `line` opens with a bold run immediately naming a label -
    the signal for a forced paragraph break even mid-block, so back-to-back
    "Label: ..." runs (CRediT author-contribution entries, Transparency
    Statement sub-sections) aren't glued into one paragraph. Requires the
    line's OWN first span to be bold, not just bold text anywhere in the
    line."""
    spans = line["spans"]
    if not spans or not spans[0]["text"].strip():
        return False
    if not (spans[0]["flags"] & BOLD_FLAG):
        return False
    return bool(_LABEL_COLON_RE.match(_line_raw_text(line)))


def _split_block_segments(block, body_size, toc_titles):
    """Split a text block's lines into ordered ('heading'|'bullet_item'|
    'paragraph', [line, ...]) segments, so pass 2 can emit one HTML element
    per segment instead of assuming the whole block is one element. A
    blank line is a hard break (dropped, forces the next line into a new
    segment); a line matching the heading formula becomes its own heading
    segment even mid-block; a bullet-marker-led line always starts a NEW
    bullet_item (consecutive markers are separate items; continuation
    lines with no marker extend the current item); a bold "Label:" line
    forces a new paragraph; anything else continues the open segment."""
    segments = []
    cur_kind, cur_lines = None, []

    def flush():
        nonlocal cur_kind, cur_lines
        if cur_lines:
            segments.append((cur_kind, cur_lines))
        cur_kind, cur_lines = None, []

    for line in block["lines"]:
        raw = _line_raw_text(line)
        if not raw:
            flush()
            continue
        if _is_heading_line(line, body_size, toc_titles):
            flush()
            segments.append(("heading", [line]))
            continue
        if _bullet_marker_len(line):
            flush()
            cur_kind, cur_lines = "bullet_item", [line]
            continue
        if _is_label_start(line):
            flush()
            cur_kind, cur_lines = "paragraph", [line]
            continue
        if cur_kind is None:
            cur_kind = "paragraph"
        cur_lines.append(line)

    flush()
    return segments


def _body_font_size(doc):
    sizes = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    key = round(span["size"], 1)
                    sizes[key] = sizes.get(key, 0) + len(span["text"])
    return max(sizes.items(), key=lambda kv: kv[1])[0] if sizes else 11.0


def _furniture_texts(doc):
    """Normalized texts of running headers/footers: any block text repeated
    on 3+ pages is page furniture, not content."""
    counts = {}
    for page in doc:
        seen = set()
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            t = _furniture_key(_block_text(block))
            if t and t not in seen:
                seen.add(t)
                counts[t] = counts.get(t, 0) + 1
    return {t for t, n in counts.items() if n >= 3}


def _leading_marker(block, body_size):
    """If a text block's very first span is a short digit run clearly
    smaller than body text, return the digit string (the footnote's own
    marker) - else None."""
    lines = block.get("lines") or []
    if not lines or not lines[0]["spans"]:
        return None
    first = lines[0]["spans"][0]
    if not first["text"] or first["size"] > body_size - FOOTNOTE_MARKER_SIZE_DELTA:
        return None
    m = FOOTNOTE_MARKER_RE.match(first["text"].strip())
    return m.group(1) if m else None


def _footnote_markers(doc, body_size):
    """{page_index: {marker_digit_str, ...}} for blocks that look like
    footnotes: a small leading marker, non-bold body text sized notably
    below body_size, sitting in the bottom ~45% of the page. Mirrors the
    _body_font_size/_furniture_texts whole-document prepass idiom.
    Footnote identity is page-scoped (not global) since that's how
    footnotes actually work, and it keeps IDs collision-free even if two
    different pages both happen to have a footnote "1"."""
    per_page = {}
    for pno, page in enumerate(doc):
        y_min = page.rect.height * FOOTNOTE_Y_FRAC
        found = set()
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0 or block["bbox"][1] < y_min:
                continue
            marker = _leading_marker(block, body_size)
            if marker is None:
                continue
            size, bold, _ = _span_stats(block)
            if bold or not (body_size - FOOTNOTE_BODY_DELTA_MAX
                             <= size <= body_size - FOOTNOTE_BODY_DELTA_MIN):
                continue
            found.add(marker)
        if found:
            per_page[pno] = found
    return per_page


FOOTREF_MARKER_RE = re.compile(r"\x00FNREF:(\d+):(\d+)\x00")


def _paragraph_html(block, footnote_ids, page_index, body_size,
                     skip_leading_marker=False, unambiguous_marker_page=None):
    """HTML for a paragraph/footnote-body block: the same line/de-hyphenation
    assembly as _block_text(), corrected via _spaced_join, plus two
    footnote-aware behaviors: (1) a small digit span whose value is a
    CONFIRMED footnote number for this page becomes a \\x00FNREF:page:n\\x00
    marker - a footnote's own body text (needed for its in-text reference's
    hover tooltip) isn't known yet at this point in the single forward pass,
    so the final <sup> markup is resolved later, once every footnote on
    every page has been collected (see _resolve_footrefs). If the marker
    isn't a footnote on ITS OWN page but IS an unambiguous (single-page)
    footnote on an adjacent page, the reference is still recognized and the
    marker embeds that page instead - covers a footnote whose body spilled
    onto the next page, so its reference mark and its definition end up on
    different pages. NUL/digits/colons are untouched by html.escape(), so
    the marker survives being embedded here and escaped normally. Any
    other small digit (ordinals, unrelated superscripts) is left as plain
    text, unchanged from before footnote support existed. (2) if
    skip_leading_marker, the block's own leading marker span is dropped
    from the output (used when rendering a footnote's own body - its
    number is shown separately via the footnote list's <li value=>).
    """
    unambiguous_marker_page = unambiguous_marker_page or {}
    first_line_spans = block["lines"][0]["spans"] if block["lines"] else None

    def join(spans):
        if skip_leading_marker and spans is first_line_spans:
            nonempty = [s for s in spans if s["text"]]
            if (nonempty and nonempty[0]["size"] <= body_size - FOOTNOTE_MARKER_SIZE_DELTA
                    and FOOTNOTE_MARKER_RE.match(nonempty[0]["text"].strip())):
                spans = spans[spans.index(nonempty[0]) + 1:]
        out = ""
        prev_style = None
        for span in spans:
            s = span["text"]
            if not s:
                continue
            style = (round(span["size"], 1), bool(span["flags"] & BOLD_FLAG))
            stripped = s.strip()
            target_page = None
            if (span["size"] <= body_size - FOOTNOTE_MARKER_SIZE_DELTA
                    and re.match(r"^\d{1,3}$", stripped)):
                if stripped in footnote_ids:
                    target_page = page_index
                else:
                    candidate = unambiguous_marker_page.get(stripped)
                    if candidate is not None and abs(candidate - page_index) <= 1:
                        target_page = candidate
            is_ref = target_page is not None
            if (out and prev_style is not None and style != prev_style
                    and not out[-1].isspace() and not s[:1].isspace()
                    and out[-1].isalnum() and s[:1].isalnum()):
                out += " "
            if is_ref:
                out += "\x00FNREF:%d:%s\x00" % (target_page, stripped)
            else:
                out += s
            prev_style = style
        return out

    raw = _assemble_block(block, join)
    return html.escape(raw)


def _resolve_footrefs(html_text, footnote_bodies):
    """Replace every \\x00FNREF:page:n\\x00 marker left by _paragraph_html
    with its final <sup> markup, once every footnote's body text is known
    (footnote_bodies: {(page_index, marker_str): body_html}). Mirrors the
    in-text citation reference's hover-tooltip pattern (.cite-tooltip) so
    hovering a footnote number works the same way as hovering a citation."""
    def repl(m):
        page_index, marker = int(m.group(1)), m.group(2)
        fid = "fn-%d-%s" % (page_index, marker)
        refid = "fnref-%d-%s" % (page_index, marker)
        body = footnote_bodies.get((page_index, marker), "")
        tooltip = ('<span class="cite-tooltip">%s</span>' % body) if body else ""
        return ('<sup class="fulltext-footref"><a href="#%s" id="%s">%s</a>%s</sup>'
                % (fid, refid, html.escape(marker), tooltip))
    return FOOTREF_MARKER_RE.sub(repl, html_text)


# No capturing group, deliberately: re.split() on a pattern with no group
# discards the matched delimiters, leaving parts as pure text segments
# (length N+1) that line up 1:1 with findall()'s N tag matches - the same
# idiom build.py's link_citations() uses for the same reason. A capturing
# group would make split() interleave the tags back INTO parts, breaking
# the zip-based reconstruction below.
_TAG_SPLIT_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _autolink_urls(html_text):
    """Wrap bare http(s) URLs in the extracted text with <a href> - PDFs
    routinely contain plain-text URLs (data-availability statements,
    footnote citations) with no markup of their own. Only ever runs on
    text between tags, never inside an existing tag's attributes, via the
    same split-on-tags trick build.py's link_citations() uses. No
    target/rel needed - base.html's decorateExternalLinks() already adds
    those to every external link on the page once it loads, this fulltext
    subtree included. Called once at the very end of extraction, after
    footnote markers are fully resolved, so it can't touch them."""
    def linkify(text):
        def repl(m):
            url = m.group(0)
            trail = ""
            while url and url[-1] in ".,;:!?)]}'\"":
                trail = url[-1] + trail
                url = url[:-1]
            return '<a href="%s">%s</a>%s' % (url, url, trail)
        return _URL_RE.sub(repl, text)
    tags = _TAG_SPLIT_RE.findall(html_text)
    parts = _TAG_SPLIT_RE.split(html_text)
    out = [linkify(parts[0])]
    for tag, part in zip(tags, parts[1:]):
        out.append(tag)
        out.append(linkify(part))
    return "".join(out)


def _mostly_inside(rect, region, frac=0.5):
    """True if most of rect's own area falls inside region - used instead
    of a bare .intersects() check for "is this text living inside a
    rendered figure/table" decisions, since a normal paragraph can simply
    END right where a figure/table begins: its block's bounding box then
    grazes the region by a few points at one edge, which .intersects()
    alone would count as a match and wrongly drop the ENTIRE paragraph
    (confirmed against a real PDF: a sentence ending just before Figure 1
    was silently dropped this way, including the footnote reference it
    carried)."""
    area = rect.width * rect.height
    if area <= 0:
        return False
    overlap = rect & region
    return (overlap.width * overlap.height) > frac * area


def _render_clip(page, rect, zoom=2.0):
    rect = rect & page.rect            # never render outside the page
    if rect.is_empty or rect.width < 20 or rect.height < 15:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect)
    return pix.tobytes("png")


def _figure_rect_above(page, caption_rect, text_blocks, furniture):
    """The figure region belonging to a caption: image blocks and larger
    vector drawings in the band between the caption's top and the bottom of
    the nearest body-text block above it."""
    top_limit = 0.0
    for tb in text_blocks:
        t = _furniture_key(_block_text(tb))
        if not t or t in furniture:
            continue
        if tb["bbox"][3] <= caption_rect.y0 - 4 and tb["bbox"][3] > top_limit:
            # a text block fully above the caption; but only count it as the
            # band's upper bound if it is NOT part of the figure itself
            # (axis labels are small blocks) - use width as a cheap proxy:
            if (tb["bbox"][2] - tb["bbox"][0]) > page.rect.width * 0.5:
                top_limit = tb["bbox"][3]
    band = fitz.Rect(page.rect.x0, top_limit, page.rect.x1, caption_rect.y0)

    pieces = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] == 1 and fitz.Rect(block["bbox"]).intersects(band):
            pieces.append(fitz.Rect(block["bbox"]))
    for drawing in page.get_drawings():
        r = drawing["rect"]
        if r.width >= 40 and r.height >= 25 and r.intersects(band):
            pieces.append(r)
    if not pieces:
        return None
    rect = pieces[0]
    for r in pieces[1:]:
        rect |= r
    rect = rect & band
    if rect.width < MIN_FIG_W or rect.height < MIN_FIG_H:
        return None
    padded = rect + (-4, -4, 4, 4)     # a little breathing room ...
    # ... but never let it cross into the caption below or the body text
    # above - band's own bounds (top_limit/caption_rect.y0) are already
    # the correct edges; padding past them clips in a sliver of whichever
    # text sits just outside the real figure (confirmed in practice: a
    # figure's image was showing the first line of its own caption at the
    # bottom edge).
    padded.y0 = max(padded.y0, top_limit)
    padded.y1 = min(padded.y1, caption_rect.y0)
    return padded


def _table_rect_below(page, caption_rect, text_blocks, all_blocks, furniture,
                       body_size, caption_tops):
    """The table region belonging to a caption: unlike Figures (image above,
    caption below), this journal's Tables have their caption ABOVE the
    table content, so the region to capture is BELOW the caption - down to
    the next caption/heading/furniture block, defaulting to the bottom of
    the page itself if none of those is found first (a long table can
    legitimately run most of the way down a page - the running footer,
    reliably caught by the furniture check below since it repeats on
    every page, is what actually stops the band in practice, not an
    arbitrary height cap). Unions ALL block types (text, image, drawing)
    in that band, not just images/large drawings as _figure_rect_above
    does - a plain-text table with no drawn gridlines (seen in practice in
    this corpus) must be captured via its own text blocks' bounding
    boxes."""
    bottom_limit = page.rect.y1
    for y in caption_tops:
        if caption_rect.y1 + 4 < y < bottom_limit:
            bottom_limit = y
    for tb in text_blocks:
        top = tb["bbox"][1]
        if top <= caption_rect.y1 + 4 or top >= bottom_limit:
            continue
        text = _block_text(tb)
        t = _furniture_key(text)
        is_furniture = bool(t) and t in furniture
        size, bold, _ = _span_stats(tb)
        is_heading_like = (bold and size >= body_size + 1 and len(text) < 120
                           and not text.rstrip().endswith("."))
        if is_furniture or is_heading_like:
            bottom_limit = top

    band = fitz.Rect(page.rect.x0, caption_rect.y1 + 2, page.rect.x1, bottom_limit)
    if band.height < 4:
        return None

    pieces = [fitz.Rect(b["bbox"]) & band for b in all_blocks
              if fitz.Rect(b["bbox"]).intersects(band)]
    for drawing in page.get_drawings():
        r = drawing["rect"]
        if r.intersects(band):
            pieces.append(r & band)
    if not pieces:
        return None
    rect = pieces[0]
    for r in pieces[1:]:
        rect |= r
    if rect.width < MIN_TABLE_W or rect.height < MIN_TABLE_H:
        return None
    return rect + (-4, -4, 4, 4)       # a little breathing room


def _table_plain_text(page, rect):
    """A readable plain-text approximation of a table region's content,
    for a "copy table text" convenience button on the image a table
    otherwise renders as - NOT true table reconstruction (see this
    module's docstring/_table_rect_below for why that stays out of scope),
    just enough structure to be useful pasted into a spreadsheet or plain
    text: text blocks in the region clustered into rows by y-proximity,
    ordered left-to-right within a row, joined with " | "."""
    blocks = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        r = fitz.Rect(block["bbox"])
        if not r.intersects(rect):
            continue
        text = _block_text(block)
        if text:
            blocks.append((r.y0, r.x0, text))
    if not blocks:
        return ""
    blocks.sort(key=lambda b: (b[0], b[1]))
    rows, row_y, row_texts = [], None, []
    for y0, x0, text in blocks:
        if row_y is None or y0 - row_y > 4:
            if row_texts:
                rows.append(row_texts)
            row_y, row_texts = y0, [text]
        else:
            row_texts.append(text)
    if row_texts:
        rows.append(row_texts)
    return "\n".join(" | ".join(r) for r in rows)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _heading_id(text, seen):
    """Stable slug for a heading's id attribute, so the full-text table of
    contents can link/jump to it - deduplicated against `seen` (shared
    across the whole document, populated by both places a heading gets
    emitted) by appending -2, -3, ... on repeats (e.g. two sections both
    titled "Method")."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-") or "section"
    base, n = slug, 2
    while slug in seen:
        slug = "%s-%d" % (base, n)
        n += 1
    seen.add(slug)
    return slug


def _figure_html(url, caption, caption_first=False, copy_text=None):
    """caption_first=True renders the caption BEFORE the image (this
    journal's Table convention); the default (False) renders it after
    (this journal's Figure convention). copy_text, when given (tables
    only - a table image has no selectable text of its own, unlike a
    genuine chart image where a caption is enough), adds a small "Copy
    table text" button next to the caption - a row-clustered plain-text
    approximation of the table's cells (see _table_plain_text), not true
    table reconstruction, just enough to be useful pasted elsewhere."""
    cap = ('<figcaption>%s</figcaption>' % html.escape(caption)) if caption else ""
    alt = html.escape(caption[:80]) if caption else "Figure"
    img = ('<button class="figure-zoom" type="button" aria-label="Enlarge figure">'
           '<img src="%s" loading="lazy" alt="%s"></button>' % (url, alt))
    copy_btn = ""
    if copy_text:
        copy_btn = ('<button class="copy-btn table-copy-btn" type="button" '
                    'data-copy-text="%s">Copy table text</button>'
                    % html.escape(copy_text))
    css_class = "fulltext-figure fulltext-table" if caption_first else "fulltext-figure"
    body = (cap + copy_btn + img) if caption_first else (img + cap + copy_btn)
    return '<figure class="%s">%s</figure>' % (css_class, body)


def extract_fulltext(pdf_path, fig_url_prefix):
    """{"html": str, "figures": [(filename, png_bytes), ...]}.

    fig_url_prefix is prepended to figure file names in <img src> URLs,
    e.g. "/r2/assets/fulltext/9577-"; the returned filenames carry the same
    suffixes ("fig-1.png", ...) for the caller to write to disk.
    """
    if fitz is None:
        return {"html": "", "figures": []}
    try:
        result = _extract(pdf_path, fig_url_prefix, ignore_toc=False)
        if not result["html"]:
            # Bookmarked start heading never matched a text block - retry
            # from the top of the document instead of returning nothing.
            result = _extract(pdf_path, fig_url_prefix, ignore_toc=True)
        return result
    except Exception as e:  # noqa: BLE001 - never break the site build
        print("  full-text extraction failed for %s: %s" % (pdf_path, e),
              file=sys.stderr)
        return {"html": "", "figures": []}


def _extract(pdf_path, fig_url_prefix, ignore_toc):
    doc = fitz.open(pdf_path)
    body_size = _body_font_size(doc)
    furniture = _furniture_texts(doc)
    footnote_numbers = _footnote_markers(doc, body_size)
    # A footnote number that's only ever a candidate on ONE page can be
    # safely matched from an adjacent page too - covers a footnote whose
    # reference mark and body text land on different pages because the
    # body spilled onto the next page. A number that's a candidate on
    # multiple pages stays page-scoped (the whole point of the original
    # design), since a global match would be genuinely ambiguous there.
    marker_pages = {}
    for pno, markers in footnote_numbers.items():
        for m in markers:
            marker_pages.setdefault(m, []).append(pno)
    unambiguous_marker_page = {m: pages[0] for m, pages in marker_pages.items()
                               if len(pages) == 1}
    toc = [] if ignore_toc else doc.get_toc()
    toc_titles = {_norm(t[1]): t[0] for t in toc}
    first_heading = _norm(toc[0][1]) if toc else None

    parts = []
    figures = []
    footnotes = []
    started = first_heading is None    # no bookmarks -> include everything
    in_excluded_section = False
    fig_n = 0
    table_n = 0
    seen_heading_ids = set()

    for page_index, page in enumerate(doc):
        d = page.get_text("dict")
        text_blocks = [b for b in d["blocks"] if b["type"] == 0]

        # Pass 1: find captions and their figure/table regions, and
        # standalone large images, so pass 2 can skip any text living
        # inside one (axis labels, legends, table cells) - those are
        # already part of the rendering. Figure captions sit ABOVE their
        # image in this journal; Table captions sit BELOW theirs - above
        # and below searches are genuinely different, not interchangeable.
        fig_rects = {}                 # caption block index -> clip rect
        table_rects = {}                # caption block index -> clip rect
        caption_tops = sorted(fitz.Rect(b["bbox"]).y0 for b in text_blocks
                               if CAPTION_RE.match(_block_text(b)))
        for i, block in enumerate(text_blocks):
            text = _block_text(block)
            m = CAPTION_RE.match(text)
            if not m:
                continue
            if m.group(1).lower().startswith("table"):
                rect = _table_rect_below(page, fitz.Rect(block["bbox"]),
                                         text_blocks, d["blocks"], furniture,
                                         body_size, caption_tops)
                if rect is not None:
                    table_rects[i] = rect
            else:
                rect = _figure_rect_above(page, fitz.Rect(block["bbox"]),
                                          text_blocks, furniture)
                if rect is not None:
                    fig_rects[i] = rect

        standalone = []
        for block in d["blocks"]:
            if block["type"] != 1:
                continue
            rect = fitz.Rect(block["bbox"])
            if rect.width < MIN_FIG_W or rect.height < MIN_FIG_H:
                continue
            if any(rect.intersects(r) for r in fig_rects.values()) or \
                    any(rect.intersects(r) for r in table_rects.values()):
                continue
            standalone.append(rect + (-4, -4, 4, 4))

        # Pass 2: emit content in reading order.
        for i, block in enumerate(text_blocks):
            text = _block_text(block)
            norm = _norm(text)
            if not norm or _furniture_key(text) in furniture \
                    or PAGE_NUM_RE.match(text):
                continue

            size, bold, math_share = _span_stats(block)
            rect = fitz.Rect(block["bbox"])
            is_heading = ((bold and size >= body_size + 1)
                          or norm in toc_titles) and len(text) < 120 \
                and text[:1] not in BULLET_CHARS \
                and not text.rstrip().endswith(".")   # sentences aren't headings

            if not started:
                if is_heading and norm == first_heading:
                    started = True
                else:
                    continue

            if is_heading:
                in_excluded_section = norm in EXCLUDED_SECTION_HEADINGS
            if in_excluded_section:
                continue               # references/Open Science Badges - see
                                        # EXCLUDED_SECTION_HEADINGS's comment

            # Text living inside a rendered figure/table (axis labels,
            # legends, table cells) - moved ahead of the footnote check
            # below so a table's own row-number column (small font, low on
            # the page - otherwise indistinguishable from a real footnote
            # marker) never reaches it in the first place. A caption block
            # itself (i is a key in fig_rects/table_rects) is exempted -
            # the small breathing-room padding on those rects can make a
            # caption's own bbox graze its own region, which must NOT skip
            # it before it reaches its own render branch below. Uses
            # _mostly_inside rather than a bare intersects() check - a
            # normal paragraph that simply ends right where a figure/table
            # begins only grazes the region by a few points, which
            # intersects() alone would wrongly treat as "inside" and drop
            # the whole paragraph.
            if i not in fig_rects and i not in table_rects and (
                    any(_mostly_inside(rect, r) for r in fig_rects.values())
                    or any(_mostly_inside(rect, r) for r in table_rects.values())):
                continue

            page_footnote_ids = footnote_numbers.get(page_index, ())
            marker = _leading_marker(block, body_size)
            if (not is_heading and marker is not None and marker in page_footnote_ids
                    and not bold
                    and body_size - FOOTNOTE_BODY_DELTA_MAX <= size <= body_size - FOOTNOTE_BODY_DELTA_MIN
                    and rect.y0 >= page.rect.height * FOOTNOTE_Y_FRAC):
                body_html = _paragraph_html(block, page_footnote_ids, page_index,
                                             body_size, skip_leading_marker=True,
                                             unambiguous_marker_page=unambiguous_marker_page)
                fid = "fn-%d-%s" % (page_index, marker)
                refid = "fnref-%d-%s" % (page_index, marker)
                footnotes.append((page_index, marker, fid, refid, body_html))
                continue

            if i in fig_rects:
                png = _render_clip(page, fig_rects[i])
                if png:
                    fig_n += 1
                    name = "fig-%d.png" % fig_n
                    figures.append((name, png))
                    parts.append(_figure_html(fig_url_prefix + name, text))
                    continue
                # fall through: keep the caption as plain text

            if i in table_rects:
                png = _render_clip(page, table_rects[i])
                if png:
                    table_n += 1
                    name = "table-%d.png" % table_n
                    figures.append((name, png))
                    # table_rects[i] has the same +/-4pt breathing-room
                    # padding _render_clip's image wants but the plain-text
                    # extraction doesn't - undo it so the caption itself
                    # (just above) or a following heading (just below)
                    # can't be pulled in as a stray extra "row".
                    copy_text = _table_plain_text(page, table_rects[i] + (4, 4, -4, -4))
                    parts.append(_figure_html(fig_url_prefix + name, text,
                                              caption_first=True,
                                              copy_text=copy_text))
                    continue
                # fall through: keep the caption as plain text (e.g. a
                # table with no drawings and no text below it detected -
                # extremely unlikely, but keep the existing best-effort
                # fallback rather than dropping the caption)

            if math_share > 0.5 and not is_heading:
                png = _render_clip(page, rect + (-2, -2, 2, 2))
                if png:
                    fig_n += 1
                    name = "fig-%d.png" % fig_n
                    figures.append((name, png))
                    parts.append('<p class="fulltext-equation">'
                                 '<img src="%s%s" loading="lazy" alt="%s"></p>'
                                 % (fig_url_prefix, name,
                                    html.escape(text[:80])))
                    continue

            if is_heading:
                level = toc_titles.get(norm, 1)
                tag = "h3" if level <= 1 else "h4"
                hid = _heading_id(text, seen_heading_ids)
                parts.append('<%s id="%s">%s</%s>' % (tag, hid, html.escape(text), tag))
                continue

            # A block isn't necessarily ONE paragraph: a bulleted list or a
            # run of bold "Label: ..." sub-sections can share one PyMuPDF
            # block with no structural markup of their own - split it into
            # heading/bullet_item/paragraph segments instead of assembling
            # the whole block as a single flat paragraph (see
            # _split_block_segments's docstring for the confirmed real
            # cases this recovers).
            in_list = False
            for kind, seg_lines in _split_block_segments(block, body_size, toc_titles):
                if kind == "bullet_item":
                    if not in_list:
                        parts.append("<ul>")
                        in_list = True
                    parts.append("<li>%s</li>" % _paragraph_html(
                        _strip_bullet_marker(seg_lines), page_footnote_ids,
                        page_index, body_size,
                        unambiguous_marker_page=unambiguous_marker_page))
                    continue
                if in_list:
                    parts.append("</ul>")
                    in_list = False
                if kind == "heading":
                    seg_text = _line_raw_text(seg_lines[0])
                    seg_norm = _norm(seg_text)
                    if seg_norm in EXCLUDED_SECTION_HEADINGS:
                        in_excluded_section = True
                    if in_excluded_section:
                        break
                    seg_level = toc_titles.get(seg_norm, 1)
                    seg_tag = "h3" if seg_level <= 1 else "h4"
                    seg_hid = _heading_id(seg_text, seen_heading_ids)
                    parts.append('<%s id="%s">%s</%s>'
                                 % (seg_tag, seg_hid, html.escape(seg_text), seg_tag))
                else:
                    parts.append("<p>%s</p>" % _paragraph_html(
                        {"lines": seg_lines}, page_footnote_ids, page_index,
                        body_size, unambiguous_marker_page=unambiguous_marker_page))
            if in_list:
                parts.append("</ul>")

        if started and not in_excluded_section:
            for rect in standalone:
                png = _render_clip(page, rect)
                if png:
                    fig_n += 1
                    name = "fig-%d.png" % fig_n
                    figures.append((name, png))
                    parts.append(_figure_html(fig_url_prefix + name, ""))

    footnote_bodies = {(page_index, marker): body_html
                        for page_index, marker, fid, refid, body_html in footnotes}
    html_out = _resolve_footrefs("".join(parts), footnote_bodies)

    if footnotes:
        # Block-encounter order isn't reliably top-to-bottom for footnotes
        # stacked at a page's bottom, so the <li value="n"> attribute alone
        # (which only sets the displayed number, not DOM order) isn't
        # enough - sort so the list actually reads 1, 2, 3, ... too.
        ordered_footnotes = sorted(footnotes, key=lambda f: (f[0], int(f[1])))
        items = "".join(
            '<li id="%s" value="%s">%s '
            '<a class="footnote-backref" href="#%s" aria-label="Back to text">↩</a></li>'
            % (fid, marker, body_html, refid)
            for page_index, marker, fid, refid, body_html in ordered_footnotes)
        html_out += ('<section class="fulltext-footnotes" aria-label="Footnotes">'
                     '<ol>%s</ol></section>' % items)

    html_out = _autolink_urls(html_out)

    return {"html": html_out, "figures": figures}
