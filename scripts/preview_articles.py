#!/usr/bin/env python3
"""Preview unpublished articles on the real website, before they exist in OJS.

For each article give the copy-edited manuscript's Markdown export (metadata,
tables, references) and its PDF (what OJS will serve as the PDF galley, and
what the HTML tab is extracted from):

    python3 scripts/preview_articles.py --out preview \\
        hussey="manuscript.md|manuscript.pdf" kaicher="k.md|k.pdf"

It copies the site sources into a scratch workspace, adds the articles to the
copy's data (so they get the full article page: sidebar, badges, PDF viewer,
HTML tab, citation tooltips, ...), runs the real build there and writes the
result to <out>/site/. Nothing in the repository's data/ or _site/ is touched.
View it with:  python3 -m http.server 8740 --directory <out>/site
(then http://localhost:8740/articles/<id>/).

Also writes <out>/report.md: per article what was found in the manuscript
(metadata, tables, references) and what ended up on the page, so problems are
visible before publishing.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from manuscript_meta import parse_manuscript, plain  # noqa: E402
from manuscript_override import parse_references, parse_tables  # noqa: E402

COPY = ("scripts", "templates", "static", "data", "assets", "overrides", "requirements.txt")
SECTIONS = {"replication": "Replications (different data)",
            "reproduction": "Reproductions (same data)"}


def article_record(art_id, meta, pdf_name):
    doi = meta["doi"]
    names = [a["name"] for a in meta["authors"]]
    section = SECTIONS.get(meta["articleType"].lower(), meta["articleType"] or "Articles")
    citation = meta["citation"]
    if not doi:
        citation = re.sub(r"\s*https://doi\.org/\[DOI to follow\]\s*$", "", citation)
    published = meta["publicationDate"] or datetime.date.today().isoformat()
    return {
        "abstractHtml": "<p>%s</p>" % meta["abstract"].replace("&", "&amp;").replace("<", "&lt;"),
        "authors": [{"name": a["name"], "affiliation": a["affiliation"], "orcid": a["orcid"]}
                    for a in meta["authors"]],
        "bibtexText": "", "bibtexUrl": "", "risText": "", "risUrl": "",
        "categories": [meta["discipline"]] if meta["discipline"] else [],
        "citation": citation,
        "datePublished": published,
        "doi": doi,
        "doiUrl": "https://doi.org/" + doi if doi else "",
        "galleys": [{"downloadUrl": "#", "viewUrl": "#", "isPdf": True, "label": "PDF",
                     "localPdf": pdf_name}],
        "issueId": "vol2", "issueTitle": "Vol. 2 (2026)",
        "keywords": meta["keywords"],
        "licenseHtml": ("<p>Copyright (c) 2026 %s</p>\n<p>This work is licensed under a "
                        '<a href="https://creativecommons.org/licenses/by/4.0/" rel="license">'
                        "Creative Commons Attribution 4.0 International License</a>.</p>"
                        % ", ".join(names)),
        "ojsUrl": "https://ejournals.uni-muenster.de/replicationresearch",
        "referencesHtml": "",
        "section": section,
        "submissionId": art_id,
        "subtitle": "",
        "title": meta["title"],
        "urlPath": art_id,
    }


def build_workspace(work, specs, md_fulltext=True):
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    for name in COPY:
        src = os.path.join(ROOT, name)
        dst = os.path.join(work, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "fulltext"))
        elif os.path.exists(src):
            shutil.copy2(src, dst)

    data = os.path.join(work, "data")
    articles = json.load(open(os.path.join(data, "articles.json"), encoding="utf-8"))
    extras = json.load(open(os.path.join(data, "published_extras.json"), encoding="utf-8"))
    report = []
    for art_id, md_path, pdf_path in specs:
        text = open(md_path, encoding="utf-8").read()
        meta = parse_manuscript(text)
        pdf_name = "%s.pdf" % art_id
        shutil.copyfile(pdf_path, os.path.join(work, "assets", "pdf", pdf_name))
        shutil.copyfile(md_path, os.path.join(work, "overrides", "%s.md" % art_id))
        if md_fulltext:        # HTML view from the Markdown instead of the PDF
            open(os.path.join(work, "overrides", "%s.fulltext-md" % art_id), "w").close()
        rec = article_record(art_id, meta, pdf_name)
        articles.append(rec)
        entry = {"codeUrl": "", "dataUrl": "", "materialsUrl": "", "peerReviewUrl": "",
                 "preregisteredUrl": "", "reproCertUrl": "",
                 "laySummary": meta["laySummary"],
                 "submissionDate": meta["submissionDate"]}
        entry.update(meta["links"])
        extras[(meta["doi"] or "").lower()] = entry
        report.append((art_id, meta, text))
    issues = json.load(open(os.path.join(data, "issues.json"), encoding="utf-8"))
    for issue in issues:
        if issue["id"] == "vol2":
            for art_id, meta, _ in report:
                section = SECTIONS.get(meta["articleType"].lower(), meta["articleType"])
                for sec in issue["sections"]:
                    if sec["title"] == section:
                        sec["articles"].append(art_id)
                        break
                else:
                    issue["sections"].append({"title": section, "articles": [art_id]})
    json.dump(articles, open(os.path.join(data, "articles.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(extras, open(os.path.join(data, "published_extras.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(issues, open(os.path.join(data, "issues.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return report


def page_report(work, art_id, meta, md_text):
    page_path = os.path.join(work, "_site", "articles", art_id, "index.html")
    html = open(page_path, encoding="utf-8").read()
    body = html[html.find('id="fulltext-body"'):] if 'id="fulltext-body"' in html else ""
    heads = re.findall(r'<h([34]) id="[^"]*">([^<]*)', body)
    n_fig = len(re.findall(r'<figure class="fulltext-figure"', body))
    n_tbl = len(re.findall(r"<table>", body))
    md_tables = parse_tables(md_text)
    md_refs = parse_references(md_text).count("<p>")
    lines = ["## %s" % art_id, "",
             "- title: %s" % meta["title"],
             "- DOI: %s" % (meta["doi"] or "(none yet)"),
             "- authors: %s" % "; ".join("%s (%s)" % (a["name"], a["affiliation"][:40]) for a in meta["authors"]),
             "- keywords: %s" % ", ".join(meta["keywords"]),
             "- badge links found in manuscript: %s" % (", ".join(meta["links"]) or "none"),
             "- tables in manuscript markdown: %d -> real tables on page: %d" % (len(md_tables), n_tbl),
             "- references in markdown: %d" % md_refs,
             "- figures rendered in HTML view: %d" % n_fig,
             "- HTML-view headings (%d): %s" % (len(heads), " | ".join(h[1] for h in heads)),
             "- citation tooltips: %d" % html.count('class="cite-ref"'),
             "- footnote references: %d" % html.count("fulltext-footref"),
             ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "preview"))
    ap.add_argument("--pdf-fulltext", action="store_true",
                    help="extract the HTML view from the PDF instead of the Markdown")
    ap.add_argument("articles", nargs="+", help='id="manuscript.md|manuscript.pdf"')
    args = ap.parse_args()
    specs = []
    for item in args.articles:
        art_id, _, paths = item.partition("=")
        md_path, _, pdf_path = paths.partition("|")
        if not (art_id and os.path.exists(md_path) and os.path.exists(pdf_path)):
            sys.exit("bad spec %r - use id=\"manuscript.md|manuscript.pdf\" with existing files" % item)
        specs.append((art_id, md_path, pdf_path))

    out = os.path.abspath(args.out)
    work = os.path.join(out, "work")
    report = build_workspace(work, specs, md_fulltext=not args.pdf_fulltext)
    subprocess.run([sys.executable, os.path.join(work, "scripts", "build.py")], cwd=work, check=True)
    parts = ["# Article preview report", "", "Built %s." % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
    for art_id, meta, text in report:
        parts.append(page_report(work, art_id, meta, text))
    site = os.path.join(out, "site")
    if os.path.exists(site):
        shutil.rmtree(site)
    shutil.move(os.path.join(work, "_site"), site)
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    print("\n".join(parts))
    print("Preview site: %s\nView: python3 -m http.server 8740 --directory \"%s\"" % (site, site))


if __name__ == "__main__":
    main()
