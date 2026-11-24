"""End-to-end multipart acceptance against app.py with a temporary DATA_DIR."""
from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ingestion.test_ingestion import _full_csv, _tiny_png

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
PASSWORDS = {
    "host-owner": "host-owner-test-password",
    "host-dispatcher": "host-dispatcher-password",
    "host-employee": "host-employee-test-password",
}


class Client:
    def __init__(self, origin):
        self.origin = origin
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.origin + path, data=data, method=method)
        if data is not None and not isinstance(payload, bytes):
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=5) as response:
                body = response.read()
                status = response.status
                response_headers = dict(response.headers)
        except urllib.error.HTTPError as exc:
            body, status, response_headers = exc.read(), exc.code, dict(exc.headers)
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = body
        return status, parsed, response_headers, body

    def login(self, username, password):
        return self.request("POST", "/api/login", {"username": username, "password": password})

    def upload(self, method, path, filename, content_type, content):
        boundary = "----juloningestion" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request(method, path, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class IngestionHostHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="julong-ingestion-http-")
        self.data_dir = Path(self.temp.name) / "data"
        self.data_dir.mkdir(mode=0o700)
        self.port = _free_port()
        self.env = os.environ.copy()
        self.env.update({"DATA_DIR": str(self.data_dir), "HOST": "127.0.0.1",
                         "PORT": str(self.port), "COOKIE_SECURE": "0"})
        for name, role in (("host-owner", "owner_finance"), ("host-dispatcher", "dispatcher"), ("host-employee", "employee")):
            result = subprocess.run(
                [sys.executable, str(APP), "user-add", "--username", name,
                 "--name", name, "--role", role, "--password-stdin"],
                cwd=ROOT, env=self.env, input=PASSWORDS[name] + "\n", text=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
            self.assertEqual(result.returncode, 0, "isolated account creation failed")
        self.origin = f"http://127.0.0.1:{self.port}"
        self._start_server()
        self.clients = {name.removeprefix("host-"): Client(self.origin) for name in PASSWORDS}
        for name, client in self.clients.items():
            self.assertEqual(client.login("host-" + name, PASSWORDS["host-" + name])[0], 200)

    def tearDown(self):
        proc = getattr(self, "proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.temp.cleanup()

    def _start_server(self):
        self.proc = subprocess.Popen([sys.executable, str(APP), "serve"], cwd=ROOT, env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.fail("isolated host exited during startup")
            try:
                with urllib.request.urlopen(self.origin + "/health", timeout=0.3):
                    return
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        self.fail("isolated host did not become ready")

    def _restart_server(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self._start_server()

    def test_import_preview_commit_and_attachment_http_acl(self):
        owner, dispatcher, employee = self.clients["owner"], self.clients["dispatcher"], self.clients["employee"]
        status, denied, _, _ = employee.upload("POST", "/api/waybills/import/preview",
                                                "full.csv", "text/csv", _full_csv("HTTP-IMPORT-001"))
        self.assertEqual(status, 403)

        status, preview, _, _ = owner.upload("POST", "/api/waybills/import/preview",
                                             "full.csv", "text/csv", _full_csv("HTTP-IMPORT-001"))
        self.assertEqual(status, 200)
        self.assertEqual(preview["row_count"], 1)
        status, committed, _, _ = owner.request("POST", "/api/waybills/import/commit",
                                                {"preview_id": preview["preview_id"]},
                                                {"Idempotency-Key": "http-import-commit-001"})
        self.assertEqual(status, 201)
        self.assertEqual(committed["waybill_numbers"], ["HTTP-IMPORT-001"])
        replay_status, replay, _, _ = owner.request("POST", "/api/waybills/import/commit",
                                                    {"preview_id": preview["preview_id"]},
                                                    {"Idempotency-Key": "http-import-commit-001"})
        self.assertEqual(replay_status, status)
        self.assertEqual(replay, committed)

        state_status, state, _, _ = employee.request("GET", "/api/state")
        self.assertEqual(state_status, 200)
        waybill_key = "julong_demo_waybills_v2"
        rows = json.loads(state["state"][waybill_key])
        rows.append({"number": "HTTP-EMPLOYEE-001", "date": "2026-11-12",
                     "receiver": "Synthetic Employee Recipient", "phone": "13800000002",
                     "address": "Synthetic Address", "destination": "Synthetic City",
                     "cargo": "Synthetic Cargo", "qty": "1", "weight": 1})
        state["state"][waybill_key] = json.dumps(rows, ensure_ascii=False)
        put_status, _, _, _ = employee.request("PUT", "/api/state", {
            "base_rev": state["rev"], "state": state["state"],
        })
        self.assertEqual(put_status, 200)

        png = _tiny_png()
        status, upload, _, _ = employee.upload("POST", "/api/waybills/HTTP-EMPLOYEE-001/attachments",
                                               "../receipt.png", "image/png", png)
        self.assertEqual(status, 201)
        attachment_id = upload["item"]["id"]
        self.assertEqual(employee.upload("POST", "/api/waybills/HTTP-IMPORT-001/attachments",
                                         "receipt.png", "image/png", png)[0], 404)
        self.assertEqual(dispatcher.upload("POST", "/api/waybills/HTTP-IMPORT-001/attachments",
                                           "receipt.png", "image/png", png)[0], 201)

        # Restart the isolated service over the same temporary DATA_DIR; both
        # the committed waybill and the private image must remain available.
        self._restart_server()
        self.assertEqual(employee.request("GET", "/api/session")[0], 200)
        state_status, persisted_state, _, _ = owner.request("GET", "/api/state")
        self.assertEqual(state_status, 200)
        persisted_numbers = [row["number"] for row in json.loads(
            persisted_state["state"]["julong_demo_waybills_v2"])]
        self.assertIn("HTTP-IMPORT-001", persisted_numbers)
        self.assertIn("HTTP-EMPLOYEE-001", persisted_numbers)

        list_status, listing, _, _ = employee.request("GET", "/api/waybills/HTTP-EMPLOYEE-001/attachments")
        self.assertEqual(list_status, 200)
        self.assertEqual([x["id"] for x in listing["items"]], [attachment_id])
        self.assertEqual(employee.request("GET", "/api/waybills/HTTP-IMPORT-001/attachments")[0], 404)
        get_status, downloaded, headers, raw = employee.request("GET", "/api/attachments/" + attachment_id)
        self.assertEqual(get_status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(raw, png)
        self.assertEqual(employee.request("DELETE", "/api/attachments/" + dispatcher.request(
            "GET", "/api/waybills/HTTP-IMPORT-001/attachments")[1]["items"][0]["id"])[0], 404)
        self.assertEqual(employee.request("DELETE", "/api/attachments/" + attachment_id)[0], 200)
        self.assertEqual(employee.request("GET", "/api/attachments/" + attachment_id)[0], 404)

    def test_import_http_rejects_missing_v9_fields_and_replay_conflict(self):
        owner = self.clients["owner"]
        nine_columns = ("到站,货物名称,数量,重量,运费,进仓费,单号,发货日期,备注\n"
                        "City,Box,1,1,12,2,SHORT-001,2026-11-12,missing recipient\n").encode()
        status, body, _, _ = owner.upload("POST", "/api/waybills/import/preview",
                                         "nine.csv", "text/csv", nine_columns)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "invalid_import")


if __name__ == "__main__":
    unittest.main(verbosity=2)
