"""Article metadata out of a copy-edited manuscript's Markdown export.

The editors keep each manuscript as a Google Docs export (docx and .md). Its
first page carries a fixed layout - a header table (DOI, article type,
discipline), title, authors with ORCID links and affiliations, then boxed
ABSTRACT / KEYWORDS / LAY SUMMARY / CITATION rows - and its end an "Open
Science Badges" and a "Submission Data" section. parse_manuscript() reads
those into the fields the website's article page needs, so an article can be
previewed (scripts/preview_articles.py) before it exists in OJS.

Best-effort like the rest of the manuscript tooling: anything it cannot find
comes back empty, never as an exception.
"""

import datetime
import re

_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_IMAGE = re.compile(r"!\[[^\]]*\](?:\[[^\]]*\]|\([^)]*\))")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
ORCID_IMG = r"\[!\[\]\[image\d+\]\]\((https://orcid\.org/[0-9X-]+)\)"

BADGE_FIELDS = (("code", "codeUrl"), ("materials", "materialsUrl"),
                ("data", "dataUrl"), ("preregist", "preregisteredUrl"),
                ("reproduc", "reproCertUrl"), ("review", "peerReviewUrl"))


def plain(text):
    """Markdown inline text -> plain text (links keep their text)."""
    text = _IMAGE.sub("", text or "")
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _ESCAPE.sub(r"\1", text)
    text = text.replace("**", "").replace("*", "")
    return re.sub(r"\s+", " ", text).strip()


def _box(lines, label):
    """Text of a boxed "| LABEL. text |" row, or ""."""
    for line in lines:
        m = re.match(r"^\|\s*%s\.\s*(.*?)\s*\|\s*$" % label, line)
        if m:
            return plain(m.group(1))
    return ""


def _title_and_authors(lines):
    """(title, author line, affiliation lines) from the lines after the header table."""
    i = 0
    while i < len(lines) and not lines[i].startswith("| :"):
        i += 1
    i += 1                                     # past the header table's divider row
    while i < len(lines) and not lines[i].strip():
        i += 1
    title = []
    while i < len(lines) and lines[i].strip():
        title.append(plain(lines[i]))
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    author_line = lines[i] if i < len(lines) else ""
    i += 1
    affiliations = []
    while i < len(lines) and not lines[i].startswith("|"):
        raw = lines[i]
        if re.match(r"^\s*(\[\*|\*?!\[)", raw):      # badge row: affiliations are over
            break
        if raw.strip():
            affiliations.append(plain(raw))
        i += 1
    return " ".join(title), author_line, affiliations


def _authors(author_line, affiliations):
    token = re.compile(
        r"\s*([^\d,\[\]*]+?)\s*(\d+(?:,\d+)*)?\s*(?:%s)?\s*(?:,\s*(?:and\s+)?|$)" % ORCID_IMG)
    out = []
    for m in token.finditer(author_line.strip()):
        name = m.group(1).strip()
        if not name:
            continue
        out.append({"name": name, "affs": (m.group(2) or "").split(",") if m.group(2) else [],
                    "orcid": m.group(3) or ""})
    # affiliation lines: "1Dept, Univ" / "1 Univ   2 Univ" / a lone unnumbered one
    numbered = {}
    for line in affiliations:
        parts = re.split(r"(?:^|\s)(\d)\s?(?=[A-Z])", line)
        for num, name in zip(parts[1::2], parts[2::2]):
            numbered[num] = name.strip()
    lone = affiliations[0] if affiliations and not numbered else ""
    for a in out:
        names = [numbered[n] for n in a.pop("affs") if n in numbered]
        a["affiliation"] = "; ".join(names) if names else lone
    return out


def _badge_links(text):
    """{field: url} from the "Open Science Badges" section ("**Open Code and Open
    Data**: url (code) and url (original data)")."""
    m = re.search(r"^# \*\*Open Science Badges\*\*(.*?)(?=^# |\Z)", text, re.S | re.M)
    out = {}
    if not m:
        return out
    for para in re.split(r"\n\s*\n", m.group(1)):
        lab = re.search(r"\*\*([^*]+)\*\*\s*:\s*(.*)", para, re.S)
        if not lab:
            continue
        label, rest = lab.group(1).lower(), lab.group(2)
        urls = []
        for link in _LINK.finditer(rest):
            urls.append(link.group(2).replace("\\", "").strip())
        if not urls:
            urls = [u.replace("\\", "") for u in re.findall(r"https?\\?://[^\s)\]]+", rest)]
        urls = [u.strip() for u in urls if u.strip()]
        # drop link-text duplicates of the same URL
        seen, uniq = set(), []
        for u in urls:
            u = u.replace("%20", "").rstrip(".,;")
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        fields = [f for key, f in BADGE_FIELDS if key in label]
        if not fields or not uniq:
            continue
        for idx, f in enumerate(fields):
            out[f] = uniq[idx] if len(uniq) == len(fields) else uniq[0]
    return out


def _date(text, label):
    m = re.search(r"\*\*%s:\*\*\s*(\d{1,2}\s+\w+\s+\d{4})" % label, text)
    if not m:
        return ""
    try:
        return datetime.datetime.strptime(m.group(1), "%d %B %Y").date().isoformat()
    except ValueError:
        return ""


def parse_manuscript(text):
    lines = text.splitlines()
    head = " ".join(lines[:6])
    doi = (re.search(r"10\.17879/replicationresearch-\d{4}-\w+", head) or [""])[0] \
        if re.search(r"10\.17879/replicationresearch-\d{4}-\w+", head) else ""
    m = re.search(r"Article type:\s*\**(.+?)\**\s+Discipline:\s*\**(.+?)\**\s{2,}", head + "  ")
    title, author_line, affs = _title_and_authors(lines)
    head_lines = lines[:60]
    handling = re.search(r"\*\*Handling editor:\*\*\s*(.+)", text)
    return {
        "doi": doi,
        "articleType": plain(m.group(1)) if m else "",
        "discipline": plain(m.group(2)) if m else "",
        "title": title,
        "authors": _authors(author_line, affs),
        "abstract": _box(head_lines, "ABSTRACT"),
        "keywords": [k.strip() for k in re.split(r"[;,]", _box(head_lines, "KEYWORDS")) if k.strip()],
        "laySummary": _box(head_lines, "LAY SUMMARY"),
        "citation": _box(head_lines, "CITATION"),
        "links": _badge_links(text),
        "submissionDate": _date(text, "Submission date"),
        "publicationDate": _date(text, "Publication date"),
        "handlingEditor": plain(handling.group(1)) if handling else "",
    }
