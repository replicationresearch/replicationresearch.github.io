"""Safety net for the harvest in scripts/fetch.py, built for the OJS 3.3 -> 3.5
upgrade (a new theme/markup can silently turn scraped fields into empty
strings without any single request failing).

Everything here is pure (no network, no writes): fetch.py hands in the data it
harvested this run and the data it harvested last time, and gets back a list
of blocking problems, a list of warnings and a Markdown report. fetch.py then
decides what to do with them (publish / refuse / dry run).

The rule behind every check: something the PREVIOUS harvest had must not
silently disappear or shrink. Content that is new is never a problem.
"""

import re

OJS_GENERATOR_RE = re.compile(r"Open Journal Systems\s+(\d+(?:\.\d+)*)", re.IGNORECASE)

# Fields of an article that exist today; empty-now-but-filled-before = broken.
ARTICLE_FIELDS = ("title", "authors", "abstractHtml", "doi", "datePublished",
                  "section", "citation", "referencesHtml", "keywords",
                  "licenseHtml", "galleys", "issueId")
# Long HTML fields that losing most of its text means a half-working selector.
SHRINK_FIELDS = ("abstractHtml", "referencesHtml")
SHRINK_RATIO = 0.3          # new text shorter than 30% of the old one
SHRINK_MIN_OLD = 300        # ...but only judge fields that were substantial


def parse_ojs_version(text):
    """'Open Journal Systems 3.3.0.21' -> '3.3.0.21' ('' if not present)."""
    m = OJS_GENERATOR_RE.search(text or "")
    return m.group(1) if m else ""


def major_minor(version):
    return ".".join((version or "").split(".")[:2])


def version_changed(old_version, new_version):
    """True when both versions are known and differ in major.minor (a 3.3.0.21
    -> 3.3.0.22 patch update is not an upgrade)."""
    return bool(old_version and new_version
                and major_minor(old_version) != major_minor(new_version))


def _filled(value):
    return bool(value) and (not isinstance(value, str) or bool(value.strip()))


def _length(value):
    return len(value) if isinstance(value, str) else 0


def check(old, new):
    """Compare the previous harvest (`old`) with this one (`new`).

    Both are dicts with the keys journal, issues, articles, pages, team,
    announcements (any of them missing/empty in `old` means "nothing to
    compare against", e.g. the very first run). Returns (problems, warnings).
    """
    problems, warnings = [], []

    # --- articles ----------------------------------------------------------
    old_articles = {a["urlPath"]: a for a in old.get("articles") or []}
    new_articles = {a["urlPath"]: a for a in new.get("articles") or []}
    for path in sorted(set(old_articles) - set(new_articles)):
        problems.append("article /%s (%s) is missing from this harvest"
                        % (path, old_articles[path].get("title", "")[:50]))
    for path, before in sorted(old_articles.items()):
        after = new_articles.get(path)
        if after is None:
            continue
        for field in ARTICLE_FIELDS:
            if _filled(before.get(field)) and not _filled(after.get(field)):
                problems.append("article /%s: '%s' was filled, is now empty"
                                % (path, field))
        for field in SHRINK_FIELDS:
            old_len, new_len = _length(before.get(field)), _length(after.get(field))
            if old_len >= SHRINK_MIN_OLD and 0 < new_len < old_len * SHRINK_RATIO:
                problems.append("article /%s: '%s' shrank from %d to %d characters"
                                % (path, field, old_len, new_len))
        for g_old in before.get("galleys") or []:
            if g_old.get("localPdf"):
                if not any(g.get("localPdf") for g in after.get("galleys") or []):
                    problems.append("article /%s: its mirrored PDF galley is gone"
                                    % path)
                break
        if len(after.get("authors") or []) < len(before.get("authors") or []):
            warnings.append("article /%s: author count dropped %d -> %d"
                            % (path, len(before["authors"]), len(after["authors"])))

    # --- issues ------------------------------------------------------------
    old_issues = {i["id"]: i for i in old.get("issues") or []}
    new_issues = {i["id"]: i for i in new.get("issues") or []}
    for iid in sorted(set(old_issues) - set(new_issues)):
        problems.append("issue %s (%s) is missing" % (iid, old_issues[iid].get("title", "")))
    for iid, before in old_issues.items():
        after = new_issues.get(iid)
        if after is None:
            continue
        n_before = sum(len(s["articles"]) for s in before.get("sections") or [])
        n_after = sum(len(s["articles"]) for s in after.get("sections") or [])
        if n_after < n_before:
            problems.append("issue %s lists %d articles, previously %d"
                            % (iid, n_after, n_before))
        for field in ("title", "cover", "datePublished"):
            if _filled(before.get(field)) and not _filled(after.get(field)):
                warnings.append("issue %s: '%s' was filled, is now empty" % (iid, field))

    # --- static pages ------------------------------------------------------
    old_pages = {p["slug"]: p for p in old.get("pages") or []}
    new_pages = {p["slug"]: p for p in new.get("pages") or []}
    for slug in sorted(set(old_pages) - set(new_pages)):
        problems.append("page /%s is missing" % slug)
    for slug, before in old_pages.items():
        after = new_pages.get(slug)
        if after is None:
            continue
        if _filled(before.get("html")) and not _filled(after.get("html")):
            problems.append("page /%s: content is now empty" % slug)
        elif (_length(before.get("html")) >= SHRINK_MIN_OLD
              and _length(after.get("html")) < _length(before.get("html")) * SHRINK_RATIO):
            problems.append("page /%s: content shrank from %d to %d characters"
                            % (slug, _length(before["html"]), _length(after["html"])))

    # --- journal-wide fields ----------------------------------------------
    old_j, new_j = old.get("journal") or {}, new.get("journal") or {}
    for field in ("name", "logo", "footerHtml", "homepageHtml", "issn", "nav",
                  "sidebarBlocks"):
        if _filled(old_j.get(field)) and not _filled(new_j.get(field)):
            problems.append("journal '%s' was filled, is now empty" % field)
    if len(new_j.get("nav") or []) < 0.7 * len(old_j.get("nav") or []):
        problems.append("journal navigation shrank from %d to %d entries"
                        % (len(old_j["nav"]), len(new_j.get("nav") or [])))

    # --- team / announcements ---------------------------------------------
    if (old.get("team") or {}).get("sections") and not (new.get("team") or {}).get("sections"):
        problems.append("editorial team no longer parses into sections")
    if old.get("announcements") and not new.get("announcements"):
        problems.append("announcements: %d before, none now" % len(old["announcements"]))
    elif len(new.get("announcements") or []) < len(old.get("announcements") or []):
        warnings.append("announcements: %d -> %d" % (len(old["announcements"]),
                                                     len(new["announcements"])))
    return problems, warnings


def report(old, new, problems, warnings, old_version="", new_version=""):
    """Markdown summary (also written to the GitHub run summary page)."""
    def count(data, key):
        return len(data.get(key) or [])

    def coverage(data, field):
        arts = data.get("articles") or []
        return "%d/%d" % (sum(1 for a in arts if _filled(a.get(field))), len(arts))

    lines = ["## OJS harvest check", ""]
    lines.append("- OJS version: %s -> **%s**%s"
                 % (old_version or "unknown", new_version or "unknown",
                    "  (UPGRADE: %s -> %s)" % (major_minor(old_version), major_minor(new_version))
                    if version_changed(old_version, new_version) else ""))
    lines += ["", "| dataset | previous | this run |", "|---|---|---|"]
    for key in ("issues", "articles", "pages", "announcements"):
        lines.append("| %s | %d | %d |" % (key, count(old, key), count(new, key)))
    lines += ["", "| article field filled | previous | this run |", "|---|---|---|"]
    for field in ARTICLE_FIELDS:
        lines.append("| %s | %s | %s |" % (field, coverage(old, field), coverage(new, field)))
    lines.append("")
    if problems:
        lines.append("### Blocking problems (%d)" % len(problems))
        lines += ["- " + p for p in problems]
    else:
        lines.append("### No blocking problems")
    if warnings:
        lines += ["", "### Warnings (%d)" % len(warnings)]
        lines += ["- " + w for w in warnings]
    return "\n".join(lines) + "\n"
