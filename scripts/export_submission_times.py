"""Local export: how long each submission took from submission to its first
editorial action, acceptance, rejection and publication.

Reads the editors' Google Sheet (public CSV export, no API key) and writes
exports/submission_times.xlsx - git-ignored, never part of the site build or
the mirror. No names, titles or notes are exported: only the sheet's row
number, the outcome, the dates and the durations.

    python3 scripts/export_submission_times.py

The sheet records ONE end date per submission ("Published/Rejected": the day
it was published, rejected or withdrawn), so:
  * days_to_publication / days_to_rejection / days_to_withdrawal come from it;
  * days_to_first_action is known for desk rejections only (the rejection
    IS the first action) - unless the sheet gains a "First Action" column;
  * days_to_acceptance needs an "Accepted" column the sheet doesn't have yet.
Add columns whose header contains "first action"/"first decision" and
"accept" (dates as DD.MM.YYYY) and this script picks them up automatically.
"""

import csv
import datetime
import io
import json
import os
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch  # noqa: E402  (sheet URL, date parser, status classifier)

OUT_DIR = os.path.join(fetch.ROOT, "exports")
OUT_FILE = os.path.join(OUT_DIR, "submission_times.xlsx")


def _date(raw):
    try:
        return datetime.date.fromisoformat(fetch._parse_sheet_date(raw or ""))
    except ValueError:
        return None


def _days(start, end):
    return (end - start).days if start and end and end >= start else None


def _column(row, *needles):
    for header, value in row.items():
        if header and any(n in header.lower() for n in needles):
            return value
    return ""


def _published_dates():
    """{bare DOI: date} from the mirrored articles - covers a published row
    whose "Published/Rejected" cell the editors haven't filled in yet."""
    path = os.path.join(fetch.DATA_DIR, "articles.json")
    try:
        with open(path, encoding="utf-8") as fh:
            articles = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {fetch._bare_doi(a.get("doi")): _date(a.get("datePublished"))
            for a in articles if a.get("doi")}


def build_rows(sheet_rows, today):
    rows = []
    published_on = _published_dates()
    for row in sheet_rows:
        submitted = _date(row.get("Submission Date"))
        if not submitted:
            continue
        status = fetch.submission_status(row)
        ended = _date(row.get("Published/Rejected"))
        if ended is None and status == "published":
            ended = published_on.get(fetch._bare_doi(row.get("Published DOI")))
        stage = (row.get("Stage") or "").strip().lower()
        desk = status == "declined" and "desk" in stage
        first_action = _date(_column(row, "first action", "first decision"))
        if first_action is None and desk:
            first_action = ended
        accepted = _date(_column(row, "accept"))
        published = ended if status == "published" else None
        rejected = ended if status == "declined" else None
        withdrawn = ended if status == "withdrawn" else None
        rows.append({
            "submission_no": int(row["ID"]) if (row.get("ID") or "").strip().isdigit() else None,
            "outcome": {"published": "published", "declined": "rejected (desk)" if desk
                        else "rejected", "withdrawn": "withdrawn",
                        "underReview": "in progress"}[status],
            "date_submitted": submitted,
            "date_first_action": first_action,
            "date_accepted": accepted,
            "date_rejected": rejected,
            "date_withdrawn": withdrawn,
            "date_published": published,
            "days_to_first_action": _days(submitted, first_action),
            "days_to_acceptance": _days(submitted, accepted),
            "days_to_rejection": _days(submitted, rejected),
            "days_to_withdrawal": _days(submitted, withdrawn),
            "days_to_publication": _days(submitted, published),
            "days_open_so_far": _days(submitted, today) if status == "underReview" else None,
            "reviewer_invitations": _num(row.get("Reviewer invitations")),
            "reviewers_accepted": _num(row.get("Reviewers Accepted")),
            "finished_reviews": _num(row.get("Finished Reviews")),
        })
    rows.sort(key=lambda r: (r["date_submitted"], r["submission_no"] or 0))
    return rows


def _num(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


NOTES = [
    "Submission times, Replication Research (R2)",
    "",
    "Source: the editors' Submissions sheet (snapshot taken {today}). One row per submission; "
    "no names, titles or editorial notes are included.",
    "",
    "outcome: published | rejected | rejected (desk) | withdrawn | in progress.",
    "days_to_*: calendar days from date_submitted to the date named; empty = not applicable "
    "or not recorded.",
    "days_open_so_far: for in-progress submissions only, days from submission to {today}.",
    "",
    "Limits of the source:",
    "- The sheet stores a single end date per submission (published / rejected / withdrawn).",
    "- date_first_action is therefore known for desk rejections only (the rejection is the "
    "first action) and empty otherwise.",
    "- date_accepted / days_to_acceptance are empty: the sheet has no acceptance date.",
    "  Adding 'First Action' and 'Accepted' date columns (DD.MM.YYYY) to the sheet fills "
    "these automatically on the next export.",
    "- Medians below are over very few submissions - treat them as descriptive, not as estimates.",
]


def summary(rows):
    out = []
    for label, col in (("Submission to first action", "days_to_first_action"),
                       ("Submission to acceptance", "days_to_acceptance"),
                       ("Submission to rejection", "days_to_rejection"),
                       ("Submission to withdrawal", "days_to_withdrawal"),
                       ("Submission to publication", "days_to_publication")):
        vals = sorted(r[col] for r in rows if r[col] is not None)
        out.append((label, len(vals),
                    vals[len(vals) // 2] if vals else None,
                    sum(vals) / len(vals) if vals else None,
                    vals[0] if vals else None, vals[-1] if vals else None))
    return out


def main():
    try:
        sheet_rows = list(csv.DictReader(io.StringIO(fetch.get(fetch.SHEET_CSV_URL).text)))
    except Exception as e:  # noqa: BLE001
        sys.exit("Could not read the editors' sheet: %s" % e)
    today = datetime.date.today()
    rows = build_rows(sheet_rows, today)
    if not rows:
        sys.exit("No submission rows found in the sheet.")

    wb = Workbook()
    ws = wb.active
    ws.title = "Submissions"
    headers = list(rows[0])
    ws.append(headers)
    for r in rows:
        ws.append([r[h] for h in headers])
    head_fill = PatternFill("solid", fgColor="1C2733")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = max(12, min(22, len(h) + 2))
        if h.startswith("date_"):
            for cell in ws[get_column_letter(i)][1:]:
                cell.number_format = "yyyy-mm-dd"
    ws.freeze_panes = "C2"
    ws.row_dimensions[1].height = 32

    ss = wb.create_sheet("Summary")
    ss.append(["Interval", "n", "median days", "mean days", "min", "max"])
    for line in summary(rows):
        ss.append([line[0], line[1], line[2],
                   round(line[3], 1) if line[3] is not None else None, line[4], line[5]])
    for cell in ss[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
    ss.column_dimensions["A"].width = 30

    sn = wb.create_sheet("Notes")
    for line in NOTES:
        sn.append([line.format(today=today.isoformat())])
    sn["A1"].font = Font(bold=True, size=13)
    sn.column_dimensions["A"].width = 120
    for row in sn.iter_rows():
        row[0].alignment = Alignment(wrap_text=True, vertical="top")

    os.makedirs(OUT_DIR, exist_ok=True)
    wb.save(OUT_FILE)
    print("Wrote %d submissions to %s" % (len(rows), OUT_FILE))
    for line in summary(rows):
        print("  %-28s n=%d  median=%s" % (line[0], line[1], line[2]))


if __name__ == "__main__":
    main()
