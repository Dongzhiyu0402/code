"""CSV report export tests (v0.9.0, P0-4).

Covers:
- ``CsvReporter.render_csv`` — UTF-8 BOM, ``\\r\\n`` line endings, the 10
  column header, ``;``-joined constraint ids, the「(已脱敏)」marker on masked
  items, and a header-only output for reports without drift items;
- CLI ``report --csv`` — writes a UTF-8-BOM file the CLI/Web share byte for
  byte, mutual exclusion with ``--json``/``--html`` (exit 2);
- Web ``GET /api/reports/{id}/csv`` — text/csv response with an attachment
  header whose content matches the CLI file byte-for-byte.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.core.csvreport import CsvReporter  # noqa: E402
from cfgdrift.core.masker import SensitiveMasker  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402

    WEB_OK = True
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore
    WEB_OK = False

_HEADER = "scan_id,severity,key_path,change_type,file,line,old_value,new_value,rule,constraint_violations"


def _data_doc(**overrides):
    doc = {
        "scan_id": 7,
        "baseline": {"name": "prod", "version": 3},
        "created_at": "2026-08-05T00:00:00+00:00",
        "mode": "manual",
        "summary": {"added": 0, "removed": 0, "modified": 2,
                    "type_changed": 0, "ignored": 0, "total": 2,
                    "max_severity": "CRITICAL"},
        "items": [
            {
                "key_path": "servers.web.port",
                "change_type": "modified",
                "severity": "WARN",
                "file": "cfg.json",
                "old_value": 8080,
                "new_value": 9090,
                "rule_id": 1,
                "line": 5,
                "masked": False,
                "constraint_violations": [
                    {"constraint_id": "http_port_range", "type": "range",
                     "message": "port out of range"},
                    {"constraint_id": "z_last", "type": "range",
                     "message": "another"},
                ],
            },
            {
                "key_path": "servers.web.auth_token",
                "change_type": "modified",
                "severity": "CRITICAL",
                "file": "cfg.json",
                "old_value": "s3cret",
                "new_value": "hunter2",
                "rule_id": None,
                "line": None,
                "masked": False,
            },
        ],
    }
    doc.update(overrides)
    return doc


class TestRenderCsv:
    def test_header_and_bom(self):
        text = CsvReporter.render_csv(_data_doc(items=[]))
        assert text.startswith("\ufeff")
        assert _HEADER in text
        # \r\n line endings (every \n is preceded by \r, never doubled).
        assert "\r\n" in text
        assert "\n" not in text.replace("\r\n", "")
        assert "\r\r" not in text

    def test_item_row_with_constraints_sorted_join(self):
        text = CsvReporter.render_csv(_data_doc())
        lines = text.split("\r\n")
        row = next(l for l in lines if l.startswith("7,WARN,servers.web.port,"))
        cells = row.split(",")
        assert cells[0] == "7"
        assert cells[1] == "WARN"
        assert cells[2] == "servers.web.port"
        assert cells[3] == "modified"
        assert cells[4] == "cfg.json"
        assert cells[5] == "5"
        assert cells[6] == "8080"
        assert cells[7] == "9090"
        assert cells[8] == "1"
        # Deduplicated, sorted constraint ids joined with ';'.
        assert cells[9] == "http_port_range;z_last"

    def test_masked_marker_appended(self):
        doc = _data_doc()
        doc["items"][1]["old_value"] = "******"
        doc["items"][1]["new_value"] = "******"
        doc["items"][1]["masked"] = True
        text = CsvReporter.render_csv(doc)
        lines = text.split("\r\n")
        row = next(l for l in lines if l.startswith("7,CRITICAL,servers.web.auth_token,"))
        assert "******" in row
        assert "(已脱敏)" in row
        # The masked marker appears inside the value cells (not a new column).
        assert row.count("(已脱敏)") == 2

    def test_null_values_render_as_null(self):
        doc = _data_doc()
        doc["items"][0]["old_value"] = None
        text = CsvReporter.render_csv(doc)
        row = next(l for l in text.split("\r\n")
                   if l.startswith("7,WARN,servers.web.port,"))
        assert "null" in row

    def test_no_items_header_only(self):
        text = CsvReporter.render_csv(_data_doc(items=[]))
        lines = [l for l in text.lstrip("\ufeff").split("\r\n") if l]
        assert lines == [_HEADER]

    def test_masked_via_masker_pipeline(self):
        # Integration: mask the payload then render — the auth_token item is
        # masked and marked, values carry the (已脱敏) marker.
        doc = _data_doc()
        payload = {"code": 0, "data": doc, "message": "ok"}
        SensitiveMasker().mask_payload(payload)
        text = CsvReporter.render_csv(payload["data"])
        row = next(l for l in text.split("\r\n")
                   if l.startswith("7,CRITICAL,servers.web.auth_token,"))
        assert "(已脱敏)" in row
        assert "hunter2" not in row
        assert "s3cret" not in row


# ---------------------------------------------------------------------------
# CLI report --csv
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_home(tmp_path, monkeypatch):
    from cfgdrift.cli import main

    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("CFGDRIFT_HOME", home)
    store = Store(os.path.join(home, "cfgdrift.db"))
    bl = store.create_baseline("prod", "", str(tmp_path), "json",
                               {"cfg.json": {"a": {"token": "raw"}}}, {})
    payload = {
        "code": 0,
        "data": {
            "scan_id": None,
            "baseline": {"name": "prod", "version": 1},
            "created_at": "2026-08-05T00:00:00+00:00",
            "mode": "manual",
            "summary": {"added": 0, "removed": 0, "modified": 1,
                        "type_changed": 0, "ignored": 0, "total": 1,
                        "max_severity": "CRITICAL"},
            "items": [
                {
                    "key_path": "a.token", "change_type": "modified",
                    "severity": "CRITICAL", "file": "cfg.json",
                    "old_value": "raw", "new_value": "hunter2",
                    "rule_id": None, "line": 2, "masked": False,
                    "constraint_violations": [
                        {"constraint_id": "cred_rule", "type": "regex",
                         "message": "secret"}
                    ],
                }
            ],
        },
    }
    scan_id = store.add_scan(bl.id, "manual", payload)
    store.close()
    return main, home, scan_id


class TestCliCsv:
    def test_writes_bom_file(self, cli_home):
        main, home, scan_id = cli_home
        out = os.path.join(home, "r.csv")
        assert main(["report", "--scan-id", str(scan_id), "--csv", out]) == 0
        raw = open(out, "rb").read()
        assert raw.startswith(b"\xef\xbb\xbf")
        text = raw[3:].decode("utf-8")
        assert _HEADER in text
        row = next(l for l in text.split("\r\n") if l.startswith("1,CRITICAL,a.token,"))
        assert "(已脱敏)" in row
        assert "cred_rule" in row
        assert "hunter2" not in row

    def test_mutually_exclusive(self, cli_home):
        main, home, scan_id = cli_home
        out = os.path.join(home, "r.csv")
        assert main(["report", "--scan-id", str(scan_id), "--csv", out,
                     "--json", out + ".json"]) == 2
        assert main(["report", "--scan-id", str(scan_id), "--csv", out,
                     "--html", out + ".html"]) == 2


# ---------------------------------------------------------------------------
# Web CSV endpoint — byte-identical to the CLI file
# ---------------------------------------------------------------------------


@pytest.fixture()
def web_env(tmp_path):
    from cfgdrift.web.app import create_app

    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    store = Store(os.path.join(home, "cfgdrift.db"))
    bl = store.create_baseline("prod", "", str(tmp_path), "json",
                               {"cfg.json": {"a": {"token": "raw"}}}, {})
    payload = {
        "code": 0,
        "data": {
            "scan_id": None,
            "baseline": {"name": "prod", "version": 1},
            "created_at": "2026-08-05T00:00:00+00:00",
            "mode": "manual",
            "summary": {"added": 0, "removed": 0, "modified": 1,
                        "type_changed": 0, "ignored": 0, "total": 1,
                        "max_severity": "CRITICAL"},
            "items": [
                {
                    "key_path": "a.token", "change_type": "modified",
                    "severity": "CRITICAL", "file": "cfg.json",
                    "old_value": "raw", "new_value": "hunter2",
                    "rule_id": None, "line": 2, "masked": False,
                    "constraint_violations": [
                        {"constraint_id": "cred_rule", "type": "regex",
                         "message": "secret"}
                    ],
                }
            ],
        },
    }
    scan_id = store.add_scan(bl.id, "manual", payload)
    app = create_app(store, home=home)
    client = TestClient(app)
    yield client, store, home, scan_id
    store.close()


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebCsv:
    def test_endpoint_matches_cli(self, web_env, tmp_path, monkeypatch):
        from cfgdrift.cli import main

        client, store, home, scan_id = web_env
        monkeypatch.setenv("CFGDRIFT_HOME", home)
        r = client.get("/api/reports/%d/csv" % scan_id)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers.get("content-disposition", "")
        assert "report-%d.csv" % scan_id in r.headers["content-disposition"]
        assert r.text.startswith("\ufeff")

        out = os.path.join(home, "r.csv")
        assert main(["report", "--scan-id", str(scan_id), "--csv", out]) == 0
        with open(out, "r", encoding="utf-8", newline="") as fh:
            cli_text = fh.read()
        # Web body and CLI file are byte-identical (same renderer + masking).
        assert cli_text == r.text

    def test_missing_scan_404(self, web_env):
        client, _, _, _ = web_env
        r = client.get("/api/reports/9999/csv")
        assert r.status_code == 404
