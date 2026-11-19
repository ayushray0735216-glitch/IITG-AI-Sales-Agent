"""Local Scout workbook importer. Writes only to a standalone review queue."""
import argparse
import hashlib
import json
import posixpath
import re
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


def rows(path):
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    def read_xml(book, member):
        if book.getinfo(member).file_size > 25 * 1024 * 1024:
            raise ValueError("The workbook is too large.")
        return ET.fromstring(book.read(member))

    with ZipFile(path) as book:
        worksheet_path = "xl/worksheets/sheet1.xml"
        if "xl/workbook.xml" in book.namelist():
            workbook = read_xml(book, "xl/workbook.xml")
            relationships = read_xml(book, "xl/_rels/workbook.xml.rels")
            worksheets = {
                rel.attrib["Id"]: rel.attrib["Target"]
                for rel in relationships
                if rel.get("Type", "").endswith("/worksheet")
            }
            relation_id = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            for entry in workbook.findall("s:sheets/s:sheet", ns):
                target = worksheets.get(entry.get(relation_id))
                if target:
                    worksheet_path = posixpath.normpath(
                        target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target)
                    )
                    break
            else:
                raise ValueError("The workbook does not contain a worksheet.")
        sheet = read_xml(book, worksheet_path)
        shared = []
        if "xl/sharedStrings.xml" in book.namelist():
            strings = read_xml(book, "xl/sharedStrings.xml")
            shared = ["".join(t.text or "" for t in item.findall(".//s:t", ns)) for item in strings]
    for row in sheet.findall("s:sheetData/s:row", ns):
        cells = {}
        for cell in row:
            column = re.match(r"[A-Z]+", cell.attrib["r"]).group()
            value = cell.findtext("s:v", default="", namespaces=ns)
            if cell.get("t") == "s":
                value = shared[int(value)]
            elif cell.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in cell.findall(".//s:t", ns))
            cells[column] = value.strip()
        yield cells


def load(path, db):
    sheet = iter(rows(path))
    headers = next(sheet, {})
    if not {"Name", "Job Title", "Company", "Email", "LinkedIn"} <= set(headers.values()):
        raise ValueError("First worksheet does not have the Scout export headers.")
    counts = dict(read=0, added=0, existing=0, missing_identity=0, missing_email=0)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS scout_review (
            source_key TEXT PRIMARY KEY, data TEXT NOT NULL
        )""")
        for cells in sheet:
            record = {name: cells.get(col, "") for col, name in headers.items() if name}
            if not any(record.values()):
                continue
            counts["read"] += 1
            counts["missing_email"] += not record["Email"]
            profile = urlsplit(record["LinkedIn"])
            slug = re.fullmatch(r"/in/([a-zA-Z0-9_%.-]+)/?", profile.path)
            scout_id = record.get("Prospect ID")
            if scout_id:
                key = "id:" + scout_id
            elif slug and profile.hostname in {"linkedin.com", "www.linkedin.com"}:
                key = "linkedin:" + slug[1].lower()
            else:
                counts["missing_identity"] += 1
                continue
            payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
            inserted = conn.execute(
                "INSERT OR IGNORE INTO scout_review VALUES (?, ?)", (key, payload)
            ).rowcount
            if not inserted:
                conn.execute("UPDATE scout_review SET data = ? WHERE source_key = ? AND data <> ?",
                             (payload, key, payload))
            counts["added" if inserted else "existing"] += 1
    return counts, list(headers.values())


def parse_lead_workbook(workbook):
    """Read CRM lead rows and retain every named column from the first worksheet."""
    try:
        sheet = iter(rows(workbook))
        headers = {column: name for column, name in next(sheet, {}).items() if name}
        header_columns = {name.casefold(): column for column, name in headers.items()}
        if "name" not in header_columns:
            raise ValueError("The first worksheet must have a Name column in the header row.")
        if len(headers) != len(header_columns):
            raise ValueError("Use a different heading for each workbook column.")

        leads = []
        skipped = 0
        for cells in sheet:
            fields = {name: cells.get(column, "") for column, name in headers.items()}
            if not any(fields.values()):
                continue

            def field(name):
                return cells.get(header_columns.get(name.casefold()), "")

            name = field("Name")
            if not name:
                skipped += 1
                continue

            email, company = field("Email"), field("Company")
            linkedin = field("LinkedIn")
            try:
                url = urlsplit(linkedin if "://" in linkedin else "https://" + linkedin)
                slug = re.fullmatch(r"/in/([a-zA-Z0-9_%.-]+)/?", url.path)
                linkedin_id = (
                    slug[1].casefold()
                    if slug and (url.hostname or "").removeprefix("www.") == "linkedin.com"
                    else ""
                )
            except ValueError:
                linkedin_id = ""
            if linkedin_id:
                identity = ["linkedin", linkedin_id]
            elif email:
                identity = ["email", email.casefold()]
            else:
                identity = ["name and company", name.casefold(), company.casefold()]
            identity_hash = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
            leads.append({
                "Name": name,
                "Company": company,
                "Job Role": field("Job Title") or field("Job Role"),
                "Industry": field("Industry"),
                "Email": email,
                "Phone": field("Phone"),
                "Lead Import Key": f"import:{identity[0]}:{identity_hash}",
                "Workbook Profile": json.dumps(fields, ensure_ascii=False),
            })
    except (BadZipFile, ET.ParseError, KeyError, IndexError, AttributeError) as exc:
        raise ValueError("Choose a valid .xlsx workbook with the leads on the first worksheet.") from exc

    if not leads:
        raise ValueError("The workbook has no leads with a name.")
    return leads, skipped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook")
    parser.add_argument("review_database")
    args = parser.parse_args()
    counts, headers = load(args.workbook, args.review_database)
    print(json.dumps({"counts": counts, "headers": headers}, indent=2))
