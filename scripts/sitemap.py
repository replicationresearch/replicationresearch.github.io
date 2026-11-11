"""sitemap.xml and robots.txt for the built site.

Called at the end of scripts/build.py. The sitemap lists every page of the
built site (each directory holding an index.html, except the pdf.js viewer
and other static assets) plus the mirrored article PDFs, so search engines
find all of it without crawling; robots.txt allows everything and points to
the sitemap. Both are plain files in the site root, which is where GitHub
Pages (at the custom domain's root) serves them.

lastmod is only given where the data really has a date (article/issue
publication dates, announcement dates) - an invented "modified today" on every
page would teach search engines to ignore the field.
"""

import os
import re
from xml.sax.saxutils import escape

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _date(value):
    m = ISO_DATE.match(value or "")
    return m.group(0) if m else ""


def collect_urls(out_dir, site_url, base, lastmods):
    """[(absolute url, lastmod or "")] for all pages and PDFs, sorted.
    `lastmods` maps a path inside the site ("articles/ie/") to a date."""
    root = site_url.rstrip("/") + base
    urls = []
    for dirpath, dirnames, filenames in os.walk(out_dir):
        rel = os.path.relpath(dirpath, out_dir).replace(os.sep, "/")
        rel = "" if rel == "." else rel
        if rel == "":
            dirnames[:] = [d for d in dirnames if d != "static"]
        elif rel == "assets":
            dirnames[:] = ["pdf"] if "pdf" in dirnames else []   # only the PDFs
            continue
        if rel == "assets/pdf":
            for name in sorted(filenames):
                if name.lower().endswith(".pdf"):
                    path = "assets/pdf/" + name
                    urls.append((root + path, lastmods.get(path, "")))
            continue
        if "index.html" in filenames:
            path = rel + "/" if rel else ""
            urls.append((root + path, _date(lastmods.get(path, ""))))
    return sorted(urls)


def write_sitemap_and_robots(out_dir, site_url, base="/", lastmods=None):
    urls = collect_urls(out_dir, site_url, base, lastmods or {})
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in urls:
        lines.append("  <url><loc>%s</loc>%s</url>" % (
            escape(loc), "<lastmod>%s</lastmod>" % lastmod if lastmod else ""))
    lines.append("</urlset>")
    with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out_dir, "robots.txt"), "w", encoding="utf-8") as fh:
        fh.write("User-agent: *\nAllow: /\n\nSitemap: %s\n"
                 % (site_url.rstrip("/") + base + "sitemap.xml"))
    return len(urls)
