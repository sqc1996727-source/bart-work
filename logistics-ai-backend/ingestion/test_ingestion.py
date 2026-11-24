"""Isolated parser, persistence, authorization, and file-safety checks."""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from ingestion.handler import (
    MAX_IMAGE_BYTES, configure, handle_commit, handle_delete_attachment,
    handle_get_attachment, handle_list_attachments, handle_preview,
    handle_upload_attachment,
)
from ingestion.parser import ImportValidationError, parse_workbook


STATE_KEYS = (
    "julong_demo_waybills_v2", "julong_demo_recipients_v2",
    "julong_demo_dispatch_v5", "julong_demo_month_v1",
    "julong_demo_fees_v1", "julong_demo_receipts_v1",
    "julong_demo_bills_v1", "julong_demo_diffs_v1",
    "julong_demo_delivery_v1", "julong_demo_ai_v1",
)
OWNER = {"id": "owner-id", "tenant_id": "tenant-a", "name": "Owner", "role": "owner_finance"}
DISPATCHER = {"id": "dispatch-id", "tenant_id": "tenant-a", "name": "Dispatcher", "role": "dispatcher"}
EMPLOYEE = {"id": "employee-id", "tenant_id": "tenant-a", "name": "Employee", "role": "employee"}
OTHER_EMPLOYEE = {"id": "other-employee-id", "tenant_id": "tenant-a", "name": "Other", "role": "employee"}


def _full_csv(number="IMPORT-001", amount="125.40"):
    return (
        "运单编号,发货日期,收货人,联系电话,收货地址,到站,货物名称,数量,重量,运费,承运方,备注,进仓费\n"
        f"{number},2026-11-12,李测试,13800000000,测试路1号,上海奉贤,电子元件,2件,1.25,{amount},测试承运,隔离验收,8.00\n"
    ).encode("utf-8-sig")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)


def _tiny_png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x11\x22\x33\xff")
    return signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", pixels) + _chunk(b"IEND", b"")


def _tiny_jpeg() -> bytes:
    # A minimal structurally valid 1x1 baseline frame for signature testing.
    sof = b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + sof + sos + b"\x00\xff\xd9"


def _tiny_webp() -> bytes:
    payload = b"\x2f\x00\x00\x00\x00"  # VP8L header, 1x1 image
    chunk = b"VP8L" + len(payload).to_bytes(4, "little") + payload + b"\x00"
    riff_body = b"WEBP" + chunk
    return b"RIFF" + len(riff_body).to_bytes(4, "little") + riff_body


def _xlsx_with_rows(rows):
    strings = []
    indexes = {}
    xml_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row):
            value = str(value)
            if value not in indexes:
                indexes[value] = len(strings)
                strings.append(value)
            col = ""
            n = col_index + 1
            while n:
                n, rem = divmod(n - 1, 26)
                col = chr(65 + rem) + col
            cells.append(f'<c r="{col}{row_index}" t="s"><v>{indexes[value]}</v></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    shared = "".join(f"<si><t>{s}</t></si>" for s in strings)
    files = {
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Waybills" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
    }
    out = __import__("io").BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return out.getvalue()


def _xlsx_numeric_date_fixture():
    import io
    headers = ["number", "date", "receiver", "phone", "address", "destination", "cargo",
               "qty", "weight", "amount", "carrier"]
    values = ["", "", "Test Receiver", "13800000001", "Test Address", "Synthetic City",
              "Synthetic Cargo", "1件", "", "", "Synthetic Carrier"]
    shared, indexes = [], {}

    def shared_cell(ref, value):
        if value not in indexes:
            indexes[value] = len(shared)
            shared.append(value)
        return f'<c r="{ref}" t="s"><v>{indexes[value]}</v></c>'

    header_cells = "".join(shared_cell(chr(65 + i) + "1", value) for i, value in enumerate(headers))
    row_cells = []
    for i, value in enumerate(values):
        ref = chr(65 + i) + "2"
        if i == 0:
            row_cells.append(f'<c r="{ref}" s="1"><v>1312</v></c>')
        elif i == 1:
            serial = (datetime.date(2026, 11, 12) - datetime.date(1899, 12, 30)).days
            row_cells.append(f'<c r="{ref}" s="2"><v>{serial}</v></c>')
        elif i == 8:
            row_cells.append(f'<c r="{ref}"><v>1.25</v></c>')
        elif i == 9:
            row_cells.append(f'<c r="{ref}"><v>125.4</v></c>')
        else:
            row_cells.append(shared_cell(ref, value))
    files = {
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Waybills" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{"".join(f"<si><t>{x}</t></si>" for x in shared)}</sst>',
        "xl/styles.xml": '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="000000"/></numFmts><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="164"/><xf numFmtId="14"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">{header_cells}</row><row r="2">{"".join(row_cells)}</row></sheetData></worksheet>',
    }
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return out.getvalue()


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="julong-ingestion-")
        self.data_dir = Path(self.temp.name) / "data"
        self.db_path = self.data_dir / "julong.sqlite3"
        self.data_dir.mkdir(mode=0o700)
        self._init_db()
        configure(db_factory=self.db, data_dir=self.data_dir, audit_fn=self.audit,
                  state_keys=STATE_KEYS, now_fn=lambda: datetime.datetime.now().astimezone().isoformat(timespec="seconds"))

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def db(self):
        con = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
        finally:
            con.close()

    def _init_db(self):
        with self.db() as con:
            con.executescript("""
                CREATE TABLE users(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE work_state(id INTEGER PRIMARY KEY, rev INTEGER NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, user_id TEXT, user_name TEXT, action TEXT, entity_type TEXT, entity_id TEXT, details TEXT);
            """)
            for user in (OWNER, DISPATCHER, EMPLOYEE, OTHER_EMPLOYEE):
                con.execute("INSERT INTO users(id,tenant_id,name,role) VALUES(?,?,?,?)",
                            (user["id"], user["tenant_id"], user["name"], user["role"]))
            payload = {key: "[]" for key in STATE_KEYS}
            payload[STATE_KEYS[0]] = json.dumps([
                {"number": "OWN-001", "created_by": EMPLOYEE["id"]},
                {"number": "OTHER-001", "created_by": OTHER_EMPLOYEE["id"]},
            ])
            con.execute("INSERT INTO work_state VALUES(1,1,'2026-11-12T00:00:00+08:00',?,?)",
                        (OWNER["id"], json.dumps(payload, ensure_ascii=False)))

    def audit(self, con, user, action, entity_type, entity_id, details=None):
        con.execute("INSERT INTO audit(at,user_id,user_name,action,entity_type,entity_id,details) VALUES(?,?,?,?,?,?,?)",
                    ("2026-11-12T00:00:00+08:00", user["id"], user["name"], action,
                     entity_type, entity_id, json.dumps(details or {}, ensure_ascii=False)))

    def test_csv_and_xlsx_parse_full_v9_contract(self):
        parsed = parse_workbook("full.csv", "text/csv", _full_csv())
        self.assertEqual(parsed[0]["number"], "IMPORT-001")
        self.assertEqual(parsed[0]["date"], "2026-11-12")
        self.assertEqual(parsed[0]["amount"], "125.40")
        self.assertEqual(parsed[0]["feeWarehouse"], "8.00")
        self.assertEqual(parsed[0]["source_row"], 2)

        headers = ["number", "date", "receiver", "phone", "address", "destination", "cargo",
                   "qty", "weight", "amount", "carrier", "remark"]
        values = ["XLSX-001", "2026-11-12", "Test Receiver", "13800000001", "Test Address",
                  "Synthetic City", "Synthetic Cargo", "1", "0.75", "44.10", "Synthetic Carrier", ""]
        data = _xlsx_with_rows([headers, values])
        xlsx_rows = parse_workbook("workbook.xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", data)
        self.assertEqual(xlsx_rows[0]["number"], "XLSX-001")
        self.assertEqual(xlsx_rows[0]["amount"], "44.10")

        numeric_rows = parse_workbook("numeric.xlsx",
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                      _xlsx_numeric_date_fixture())
        self.assertEqual(numeric_rows[0]["number"], "001312")
        self.assertEqual(numeric_rows[0]["date"], "2026-11-12")
        self.assertEqual(numeric_rows[0]["amount"], "125.40")

    def test_nine_column_export_is_preview_error_and_bad_rows_are_rejected(self):
        nine = ("到站,货物名称,数量,重量,运费,进仓费,单号,发货日期,备注\n"
                "城市,货,1,1.2,10,2,WB-1,2026-11-12,仅九列\n").encode()
        status, body = handle_preview(OWNER, "nine.csv", "text/csv", nine)
        self.assertEqual(status, 422)
        self.assertEqual({e["field"] for e in body["errors"] if e["code"] == "missing_column"},
                         {"receiver", "phone", "address", "carrier"})

        negative = _full_csv(amount="-1")
        status, body = handle_preview(OWNER, "bad.csv", "text/csv", negative)
        self.assertEqual(status, 422)
        self.assertIn("invalid_money", {e["code"] for e in body["errors"]})
        duplicate = _full_csv("DUP-1") + _full_csv("DUP-1").splitlines(keepends=True)[1]
        status, body = handle_preview(OWNER, "duplicate.csv", "text/csv", duplicate)
        self.assertEqual(status, 422)
        self.assertIn("duplicate_waybill_in_file", {e["code"] for e in body["errors"]})

    def test_import_rejects_legacy_xls_mime_mismatch_macros_and_formulas(self):
        valid_rows = [["number", "date", "receiver", "phone", "address", "destination", "cargo",
                       "qty", "weight", "amount", "carrier"],
                      ["XLSX-SECURITY-001", "2026-11-12", "Receiver", "13800000001", "Address",
                       "City", "Cargo", "1", "1", "10.00", "Carrier"]]
        valid = _xlsx_with_rows(valid_rows)
        with self.assertRaises(ImportValidationError) as legacy:
            parse_workbook("legacy.xls", "application/vnd.ms-excel", valid)
        self.assertIn("unsupported_file_type", {e["code"] for e in legacy.exception.errors})
        with self.assertRaises(ImportValidationError) as wrong_mime:
            parse_workbook("file.csv", "application/pdf", _full_csv())
        self.assertIn("unsupported_file_type", {e["code"] for e in wrong_mime.exception.errors})

        import io
        with ZipFile(io.BytesIO(valid), "r") as source:
            members = {name: source.read(name) for name in source.namelist()}
        macro_bytes = io.BytesIO()
        with ZipFile(macro_bytes, "w", ZIP_DEFLATED) as target:
            for name, payload in members.items():
                target.writestr(name, payload)
            target.writestr("xl/vbaProject.bin", b"synthetic-macro")
        with self.assertRaises(ImportValidationError) as macro:
            parse_workbook("macro.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           macro_bytes.getvalue())
        self.assertIn("macro_workbook_unsupported", {e["code"] for e in macro.exception.errors})

        sheet_name = "xl/worksheets/sheet1.xml"
        members[sheet_name] = members[sheet_name].replace(
            b'<c r="A2" t="s"><v>', b'<c r="A2" t="s"><f>1+1</f><v>', 1)
        formula_bytes = io.BytesIO()
        with ZipFile(formula_bytes, "w", ZIP_DEFLATED) as target:
            for name, payload in members.items():
                target.writestr(name, payload)
        with self.assertRaises(ImportValidationError) as formula:
            parse_workbook("formula.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           formula_bytes.getvalue())
        self.assertIn("formula_cells_unsupported", {e["code"] for e in formula.exception.errors})

    def test_preview_commit_is_owner_only_atomic_and_idempotent(self):
        self.assertEqual(handle_preview(EMPLOYEE, "full.csv", "text/csv", _full_csv())[0], 403)
        status, preview = handle_preview(OWNER, "../customer.csv", "text/csv", _full_csv())
        self.assertEqual(status, 200)
        self.assertEqual(preview["row_count"], 1)
        self.assertEqual(preview["filename"], "customer.csv")
        self.assertEqual(preview["source_sha256"], hashlib.sha256(_full_csv()).hexdigest())
        first_status, first = handle_commit(OWNER, preview["preview_id"], "import-key-001")
        self.assertEqual(first_status, 201)
        second_status, second = handle_commit(OWNER, preview["preview_id"], "import-key-001")
        self.assertEqual(second_status, first_status)
        self.assertEqual(second, first)
        self.assertEqual(handle_commit(OWNER, preview["preview_id"], "different-key")[0], 409)
        with self.db() as con:
            state = json.loads(con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()[0])
            rows = json.loads(state[STATE_KEYS[0]])
            imported = next(item for item in rows if item["number"] == "IMPORT-001")
            audit_row = con.execute("SELECT action,details FROM audit WHERE action='waybill.import'").fetchone()
        self.assertEqual(imported["created_by"], OWNER["id"])
        self.assertEqual(imported["amount"], "125.40")
        self.assertEqual(len([item for item in rows if item["number"] == "IMPORT-001"]), 1)
        self.assertEqual(audit_row["action"], "waybill.import")
        details = json.loads(audit_row["details"])
        self.assertEqual(details["source_sha256"], preview["source_sha256"])
        self.assertEqual(details["source_rows"], [2])
        self.assertNotIn("李测试", audit_row["details"])

    def test_preview_conflict_and_existing_numbers_do_not_partially_import(self):
        status, existing = handle_preview(OWNER, "existing.csv", "text/csv", _full_csv("OWN-001"))
        self.assertEqual(status, 409)
        self.assertIn("waybill_already_exists", {e["code"] for e in existing["errors"]})

        status, preview = handle_preview(OWNER, "valid.csv", "text/csv", _full_csv("IMPORT-ATOMIC"))
        self.assertEqual(status, 200)
        with self.db() as con:
            con.execute("UPDATE work_state SET rev=rev+1 WHERE id=1")
        commit_status, response = handle_commit(OWNER, preview["preview_id"], "stale-key-002")
        self.assertEqual(commit_status, 409)
        self.assertEqual(response["error"], "revision_conflict")
        with self.db() as con:
            state = json.loads(con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()[0])
            numbers = [item["number"] for item in json.loads(state[STATE_KEYS[0]])]
        self.assertNotIn("IMPORT-ATOMIC", numbers)

    def test_upload_validates_image_content_and_enforces_ticket_acl(self):
        png = _tiny_png()
        status, upload = handle_upload_attachment(EMPLOYEE, "OWN-001", "../../proof.png", "image/png", png)
        self.assertEqual(status, 201)
        item = upload["item"]
        self.assertEqual(item["filename"], "proof.png")
        self.assertEqual(item["sha256"], hashlib.sha256(png).hexdigest())
        self.assertEqual(handle_upload_attachment(EMPLOYEE, "OTHER-001", "proof.png", "image/png", png)[0], 404)
        self.assertEqual(handle_upload_attachment(EMPLOYEE, "OWN-001", "fake.png", "image/png", b"\x89PNG\r\n\x1a\nnot-png-data")[0], 415)
        self.assertEqual(handle_upload_attachment(EMPLOYEE, "OWN-001", "spoof.jpg", "image/jpeg", png)[0], 415)
        self.assertEqual(handle_upload_attachment(EMPLOYEE, "OWN-001", "script.svg", "image/svg+xml", b"<svg/>")[0], 415)
        webp = _tiny_webp()
        webp_status, webp_upload = handle_upload_attachment(EMPLOYEE, "OWN-001", "proof.webp", "image/webp", webp)
        self.assertEqual(webp_status, 201)
        self.assertEqual(webp_upload["item"]["content_type"], "image/webp")
        self.assertEqual(handle_upload_attachment(EMPLOYEE, "OWN-001", "bad.webp", "image/webp",
                                                  b"RIFF\x08\x00\x00\x00WEBPVP8L")[0], 415)
        self.assertEqual(handle_upload_attachment(EMPLOYEE, "OWN-001", "oversize.png", "image/png", b"x" * (MAX_IMAGE_BYTES + 1))[0], 413)
        self.assertEqual(handle_upload_attachment(DISPATCHER, "OTHER-001", "proof.jpg", "image/jpeg", _tiny_jpeg())[0], 201)

        status, listed = handle_list_attachments(EMPLOYEE, "OWN-001")
        self.assertEqual(status, 200)
        self.assertEqual({row["id"] for row in listed["items"]}, {item["id"], webp_upload["item"]["id"]})
        self.assertEqual(handle_list_attachments(EMPLOYEE, "OTHER-001")[0], 404)
        self.assertEqual(handle_list_attachments(DISPATCHER, "OTHER-001")[0], 200)

        status, headers, content = handle_get_attachment(EMPLOYEE, item["id"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(content, png)
        self.assertEqual(handle_get_attachment(EMPLOYEE, "../" + item["id"])[0], 404)
        other_attachment = handle_list_attachments(DISPATCHER, "OTHER-001")[1]["items"][0]
        self.assertEqual(handle_get_attachment(EMPLOYEE, other_attachment["id"])[0], 404)

        with self.db() as con:
            path_row = con.execute("SELECT stored_path FROM attachments WHERE id=?", (item["id"],)).fetchone()
            audit_row = con.execute("SELECT action,details FROM audit WHERE entity_id=?", (item["id"],)).fetchone()
        self.assertTrue(path_row["stored_path"].startswith("attachments/"))
        self.assertNotIn("proof.png", path_row["stored_path"])
        self.assertEqual(audit_row["action"], "attachment.upload")
        self.assertIn(item["sha256"], audit_row["details"])
        stored_file = self.data_dir / path_row["stored_path"]
        self.assertEqual(stat.S_IMODE(stored_file.stat().st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE(stored_file.parent.stat().st_mode) & 0o077, 0)

        stored_file.write_bytes(b"tampered")
        self.assertEqual(handle_get_attachment(EMPLOYEE, item["id"])[0], 500)
        stored_file.write_bytes(png)

        self.assertEqual(handle_delete_attachment(EMPLOYEE, other_attachment["id"])[0], 404)
        self.assertEqual(handle_delete_attachment(EMPLOYEE, item["id"])[0], 200)
        self.assertEqual(handle_get_attachment(EMPLOYEE, item["id"])[0], 404)
        self.assertEqual(handle_delete_attachment(EMPLOYEE, webp_upload["item"]["id"])[0], 200)

    def test_demo_backup_and_isolated_restore_preserve_attachment_bytes_and_metadata(self):
        png = _tiny_png()
        status, uploaded = handle_upload_attachment(EMPLOYEE, "OWN-001", "receipt.png", "image/png", png)
        self.assertEqual(status, 201)
        attachment_id = uploaded["item"]["id"]
        backup_dir = Path(self.temp.name) / "backup"
        restored_dir = Path(self.temp.name) / "restored"
        env = os.environ.copy()
        env["DATA_DIR"] = str(self.data_dir)
        backup = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "backup_demo.py"),
                                 str(backup_dir)], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(backup.returncode, 0, backup.stderr)
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        file_items = {item["path"]: item for item in manifest["files"]}
        with self.db() as con:
            stored_path = con.execute("SELECT stored_path FROM attachments WHERE id=?", (attachment_id,)).fetchone()[0]
        self.assertIn(stored_path, file_items)

        restore = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "restore_demo_to_new_dir.py"),
                                  str(backup_dir), str(restored_dir)], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(restore.returncode, 0, restore.stderr)
        restored_file = restored_dir / stored_path
        self.assertEqual(restored_file.read_bytes(), png)
        file_meta = file_items[stored_path]
        self.assertEqual(file_meta["sha256"], hashlib.sha256(png).hexdigest())
        self.assertEqual(file_meta["size_bytes"], len(png))
        with sqlite3.connect(restored_dir / "julong.sqlite3") as con:
            self.assertEqual(con.execute("PRAGMA quick_check").fetchone()[0], "ok")
            restored_row = con.execute("SELECT sha256,size_bytes FROM attachments WHERE id=?",
                                        (attachment_id,)).fetchone()
        self.assertEqual(restored_row, (hashlib.sha256(png).hexdigest(), len(png)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
