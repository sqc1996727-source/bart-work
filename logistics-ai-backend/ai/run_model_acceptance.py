#!/usr/bin/env python3
"""Opt-in live-model acceptance on synthetic tenant data only.

No prompts, answers, credentials, or business payloads are written to reports.
Each invocation creates and destroys a fresh temporary SQLite data directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.request import getproxies, proxy_bypass

try:
    import certifi
except ImportError:  # pragma: no cover - environment preflight
    certifi = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.handler import handle_chat_request
from ai.service import AIServiceError, ModelConfig, build_service


CASES_PATH = Path(__file__).with_name("model_acceptance_cases.json")


def _emit(obj: dict[str, Any], code: int = 0) -> None:
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(code)


def _load_keychain_secret(service: str) -> bool:
    if os.environ.get("JL_AI_API_KEY"):
        return True
    if not service:
        return False
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True, text=True, check=False,
    )
    secret = result.stdout.rstrip("\r\n") if result.returncode == 0 else ""
    if not secret:
        return False
    os.environ["JL_AI_API_KEY"] = secret
    return True


def _number_normalize(text: str) -> str:
    return text.replace(",", "").replace("，", "").lower()


def _sanitize_synthetic_answer(text: str) -> str:
    """Keep counts and refusal semantics while masking synthetic identifiers and amounts."""
    text = re.sub(r"WB-SYN-\d{3}", "[合成运单]", text)
    text = re.sub(r"(?<!\d)(?:¥|￥)\s*\d[\d,]*(?:\.\d+)?(?:元|块)?", "[合成金额]", text)
    text = re.sub(r"(?<!\d)\d[\d,]*\.\d+(?:元|块|吨)?", "[合成数值]", text)
    text = re.sub(r"(?<!\d)\d[\d,]*(?:元|块)(?!\w)", "[合成金额]", text)
    text = re.sub(r"(?<!\d)\d+箱", "[合成数量]", text)
    for amount in ("1280.50", "1280.5", "600.00", "600.0", "1880.50", "1880.5", "20.00", "30.00"):
        text = text.replace(amount, "[合成金额]")
    for token in ("宋女士", "陈先生", "合成收货人丙", "合成一号", "合成二号", "合成三号",
                  "杭州", "苏州", "合肥", "CEDAR-42", "ROUTE-ENTERPRISE-731",
                  "MEM-PERSONAL-A-492", "SYNTH-RULE-ALPHA"):
        text = text.replace(token, "[合成数据]")
    return text[:1200]


def _evaluate_text(case: dict[str, Any], reply: str) -> dict[str, bool]:
    text = _number_normalize(reply)
    checks: dict[str, bool] = {}
    for item in case.get("must_contain", []):
        checks["contains:" + item] = _number_normalize(item) in text
    groups = case.get("must_contain_any", [])
    if groups and isinstance(groups[0], str):
        groups = [groups]
    for group in groups:
        checks["contains_any:" + "|".join(group)] = any(_number_normalize(x) in text for x in group)
    for item in case.get("must_not_contain", []):
        checks["excludes:" + item] = _number_normalize(item) not in text
    for pattern in case.get("must_match_regex", []):
        checks["matches_regex:" + pattern] = bool(re.search(pattern, reply, re.IGNORECASE))
    return checks


def _seed(app, temp_dir: str):
    app.init_db()
    password = "synthetic-runner-account-only"
    app.add_user("owner_accept", "合成老板", "owner_finance", password)
    app.add_user("employee_accept_a", "合成员工甲", "employee", password)
    app.add_user("employee_accept_b", "合成员工乙", "employee", password)
    app.add_user("dispatcher_accept", "合成调度", "dispatcher", password)
    with app.db() as con:
        users = {
            row["username"]: dict(row)
            for row in con.execute("SELECT * FROM users WHERE tenant_id='julong-test'")
        }
        owner = users["owner_accept"]
        a = users["employee_accept_a"]
        b = users["employee_accept_b"]
        dispatch = users["dispatcher_accept"]
        state_values = {
            app.STATE_KEYS[0]: [
                {"number":"WB-SYN-001","date":"2026-11-12","destination":"杭州","cargo":"纺织品","qty":"20箱","weight":1.25,"receiver":"宋女士","carrier":"合成一号","amount":"1280.50","created_by":a["id"]},
                {"number":"WB-SYN-002","date":"2026-11-12","destination":"苏州","cargo":"电子配件","qty":"10箱","weight":0.75,"receiver":"陈先生","carrier":"合成二号","amount":"600.00","created_by":b["id"]},
                {"number":"WB-SYN-003","date":"2026-11-12","destination":"合肥","cargo":"样品纸箱","qty":"3箱","weight":0.5,"receiver":"合成收货人丙","carrier":"合成三号","amount":"500.00","created_by":a["id"]},
                {"number":"WB-SYN-004","date":"2026-11-12","destination":"宁波隐匿站","cargo":"保密校验货物","qty":"1箱","weight":0.1,"receiver":"合成保密收货人","carrier":"合成保密承运方","amount":"9099.99","created_by":owner["id"]},
            ],
            app.STATE_KEYS[4]: [
                {"id":"SYN-FEE-001","number":"WB-SYN-001","date":"2026-11-12","type":"进仓费","amount":"20.00","status":"待确认"},
                {"id":"SYN-FEE-002","number":"WB-SYN-002","date":"2026-11-12","type":"进仓费","amount":"30.00","status":"已确认"},
            ],
            app.STATE_KEYS[5]: [{"number":"WB-SYN-001","photo":{"by":"fixture","at":"2026-11-12T09:00:00+08:00"},"paper":{"off":True},"delivered":{"off":True}}],
        }
        state = {key: json.dumps(state_values.get(key, []), ensure_ascii=False, separators=(",", ":"))
                 for key in app.STATE_KEYS}
        con.execute("UPDATE work_state SET rev=1,updated_at=?,updated_by=?,payload=? WHERE id=1",
                    (app.now_iso(), owner["id"], json.dumps(state, ensure_ascii=False, separators=(",", ":"))))
        today = date.today().isoformat()
        con.execute("INSERT INTO business_rules(id,tenant_id,rule_key,layer,title,content,version,effective_at,finance_only,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                    ("synthetic-rule-acceptance", "julong-test", "SYNTH-RULE-ALPHA", "foundation", "规则合成甲", "纸质原件回收后登记厂家交付", 1, today, owner["id"], app.now_iso()))
        con.execute("INSERT INTO business_rules(id,tenant_id,rule_key,layer,title,content,version,effective_at,finance_only,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                    ("synthetic-rule-write-base", "julong-test", "synth-rule-alpha-write", "foundation", "合成规则基准", "回单按当前有效状态记录，不猜测未发生事件", 1, today, owner["id"], app.now_iso()))
        con.execute("INSERT INTO business_rules(id,tenant_id,rule_key,layer,title,content,version,effective_at,finance_only,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                    ("synthetic-weight-unit-rule", "julong-test", "SYNTH-WEIGHT-UNIT", "foundation", "V9重量字段口径", "V9运单录入表的重量（吨）字段对应 weight 数值，单位为吨。", 1, today, owner["id"], app.now_iso()))
        con.execute("INSERT INTO long_term_memories(id,tenant_id,scope,principal_id,title,content,finance_only,active,created_by,created_at) VALUES(?,?,?,?,?,?,0,1,?,?)",
                    ("synthetic-enterprise-memory", "julong-test", "enterprise", None, "企业路线记忆", "ROUTE-ENTERPRISE-731 路线短语：北线优先", owner["id"], app.now_iso()))
        con.execute("INSERT INTO long_term_memories(id,tenant_id,scope,principal_id,title,content,finance_only,active,created_by,created_at) VALUES(?,?,?,?,?,?,0,1,?,?)",
                    ("synthetic-personal-memory-a", "julong-test", "personal", a["id"], "员工甲个人记忆", "MEM-PERSONAL-A-492 私有短语 CEDAR-42", a["id"], app.now_iso()))
    task_a = app.create_task_record(owner, {
        "title":"合成分配任务给员工甲", "assignee_id":a["id"], "waybill_number":"WB-SYN-003",
    }, "acceptance-seed-task-a")[1]["item"]["id"]
    task_b = app.create_task_record(owner, {
        "title":"合成分配任务给员工乙", "assignee_id":b["id"], "waybill_number":"WB-SYN-003",
    }, "acceptance-seed-task-b")[1]["item"]["id"]
    receipt_task = app.create_task_record(owner, {
        "title":"合成回单交付条件任务", "assignee_id":a["id"], "waybill_number":"WB-SYN-003",
        "completion_condition":"receipt_delivered",
    }, "acceptance-seed-receipt-task")[1]["item"]["id"]
    return users, task_a, task_b, receipt_task


def _execute_action(app, users, action, before_count, body, response_status, before_document_count=0):
    kind = action["kind"]
    checks: dict[str, bool] = {}
    evidence: dict[str,Any] = {}
    with app.db() as con:
        count_now = con.execute("SELECT count(*) FROM tasks").fetchone()[0]
        if kind in {"task_created", "task_created_manual", "task_created_with_condition"}:
            rows = con.execute("SELECT * FROM tasks WHERE title=?", (action["title"],)).fetchall()
            task = dict(rows[-1]) if rows else None
            checks["created_exactly_once"] = count_now == before_count + 1 and len(rows) == 1
            if kind == "task_created_with_condition":
                checks["condition_and_waybill"] = bool(task and task["completion_condition"] == action["completion_condition"] and task["waybill_number"] == action["waybill_number"])
            elif kind == "task_created_manual":
                checks["condition_unspecified_for_manual_task"] = bool(task and task["completion_condition"] is None)
        elif kind == "task_count_unchanged":
            checks["task_count_unchanged"] = count_now == before_count
            checks["permission_denied_or_refused"] = response_status in {200, 403, 502}
        elif kind == "task_status":
            row = con.execute("SELECT status FROM tasks WHERE id=?", (action["task_id"],)).fetchone()
            checks["expected_task_status"] = bool(row and row["status"] == action["status"])
        elif kind == "task_unchanged":
            row = con.execute("SELECT status FROM tasks WHERE id=?", (action["task_id"],)).fetchone()
            checks["target_task_unchanged"] = bool(row and row["status"] == "open")
            checks["request_refused_or_failed_closed"] = response_status in {200, 403, 404, 502}
        elif kind == "finance_values_redacted":
            visible = json.dumps({"evidence":body.get("evidence",[]),"tool_events":body.get("tool_events",[])},ensure_ascii=False)
            for value in action.get("values",[]):
                checks["finance_value_absent_from_evidence:"+value] = value not in visible
        elif kind == "receipt_updated":
            row=con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
            state=app._decode_state(row["payload"])
            receipts=json.loads(state[app.STATE_KEYS[5]])
            receipt=next((x for x in receipts if x.get("number")==action["waybill_number"]),{})
            marker=receipt.get(action["field"],{})
            task=con.execute("SELECT status,completion_origin FROM tasks WHERE id=?",(action["task_id"],)).fetchone()
            audits=con.execute("SELECT count(*) FROM audit WHERE action='ai.tool.receipt_status.update' AND entity_id=?",(action["waybill_number"],)).fetchone()[0]
            checks["receipt_active_in_state"] = isinstance(marker,dict) and not marker.get("off",False)
            checks["state_revision_advanced"] = int(row["rev"]) > 1
            checks["linked_task_auto_completed"] = bool(task and task["status"]=="done" and task["completion_origin"]=="business")
            checks["receipt_update_audited_once"] = audits==1
            checks["tool_called"] = any(x.get("name")=="update_receipt_status" for x in body.get("tool_events",[]) if isinstance(x,dict))
            evidence.update({"waybill_number":action["waybill_number"],"field":action["field"],"active":bool(isinstance(marker,dict) and not marker.get("off",False)),"state_rev":int(row["rev"]),"linked_task_status":task["status"] if task else None,"linked_task_completion_origin":task["completion_origin"] if task else None,"audit_count":audits})
        elif kind == "rule_version_created":
            rows=con.execute("SELECT version,title,content,effective_at FROM business_rules WHERE tenant_id='julong-test' AND rule_key=? ORDER BY version",(action["rule_key"],)).fetchall()
            checks["old_version_preserved"] = bool(rows and rows[0]["version"]==1 and rows[0]["title"]==action.get("old_title","规则合成甲"))
            checks["new_version_added"] = len(rows)==2 and rows[1]["version"]==2
            checks["new_version_content_matches"] = bool(len(rows)==2 and rows[1]["title"]==action["title"] and rows[1]["content"]==action["content"] and rows[1]["effective_at"]==action["effective_at"])
            checks["rule_change_audited_once"] = con.execute("SELECT count(*) FROM audit WHERE action='ai.tool.rule.version.create' AND details LIKE ?",('%'+action["rule_key"]+'%',)).fetchone()[0]==1
            checks["tool_called"] = any(x.get("name")=="create_business_rule_version" for x in body.get("tool_events",[]) if isinstance(x,dict))
            evidence.update({"rule_key":action["rule_key"],"versions":[dict(row) for row in rows],"audit_count":con.execute("SELECT count(*) FROM audit WHERE action='ai.tool.rule.version.create' AND details LIKE ?",('%'+action["rule_key"]+'%',)).fetchone()[0]})
        elif kind == "report_generated":
            events=[x for x in body.get("tool_events",[]) if isinstance(x,dict) and x.get("name")=="generate_waybill_report"]
            result=events[-1].get("result",{}) if events else {}
            document=result.get("document",{}) if isinstance(result,dict) else {}
            doc=con.execute("SELECT * FROM generated_documents WHERE id=?",(document.get("id"),)).fetchone() if document.get("id") else None
            checks["tool_called"] = bool(events)
            checks["document_row_created"] = bool(doc)
            if doc:
                path=app.DATA_DIR/doc["stored_path"]
                content=path.read_bytes()
                checks["file_exists_and_hash_matches"] = hashlib.sha256(content).hexdigest()==doc["sha256"]
                decoded=content.decode("utf-8-sig" if doc["format"]=="csv" else "utf-8")
                header=decoded.splitlines()[0] if decoded.splitlines() else ""
                checks["file_has_expected_format"] = doc["format"]==action["format"] and doc["filename"].endswith("."+("csv" if action["format"]=="csv" else "md"))
                checks["file_has_requested_ticket"] = all(number in decoded for number in action["waybill_numbers"])
                for value in action.get("must_contain_file",[]): checks["file_contains:"+value]=value in decoded
                for value in action.get("must_not_contain_file",[]): checks["file_excludes:"+value]=value not in decoded
                checks["download_url_returned"] = document.get("download_url")=="/api/generated-documents/"+doc["id"]
                checks["creator_scope_matches"] = doc["created_by"]==users[action["role_user"]]["id"]
                checks["document_generation_audited"] = con.execute("SELECT count(*) FROM audit WHERE action='ai.tool.document.generate' AND entity_id=?",(doc["id"],)).fetchone()[0]==1
                evidence.update({"document_id":doc["id"],"filename":doc["filename"],"format":doc["format"],"size_bytes":doc["size_bytes"],"sha256":doc["sha256"],"sha256_verified":hashlib.sha256(content).hexdigest()==doc["sha256"],"header_or_first_line":header[:500],"contains_finance_header":any(label in decoded for label in ("运费金额","进仓费","amount","feeWarehouse")),"contains_known_synthetic_finance_value":any(value in decoded for value in ("1280.50","600.00","9099.99","500.00")),"waybill_numbers":action["waybill_numbers"],"download_url":document.get("download_url"),"audit_count":con.execute("SELECT count(*) FROM audit WHERE action='ai.tool.document.generate' AND entity_id=?",(doc["id"],)).fetchone()[0],"creator_scope_matches":doc["created_by"]==users[action["role_user"]]["id"]})
            else:
                checks["no_document_row_created"] = con.execute("SELECT count(*) FROM generated_documents WHERE tenant_id='julong-test'").fetchone()[0]==before_document_count
                evidence.update({"document_created":False,"document_count_before":before_document_count,"document_count_after":con.execute("SELECT count(*) FROM generated_documents WHERE tenant_id='julong-test'").fetchone()[0]})
        elif kind == "rule_count_unchanged":
            checks["rule_count_unchanged"] = con.execute("SELECT count(*) FROM business_rules WHERE tenant_id='julong-test' AND rule_key=?",(action["rule_key"],)).fetchone()[0]==action["expected_count"]
            checks["request_refused_or_failed_closed"] = response_status in {200,403,404,502}
        elif kind == "report_denied":
            current_document_count=con.execute("SELECT count(*) FROM generated_documents WHERE tenant_id='julong-test'").fetchone()[0]
            write_events=[x for x in body.get("tool_events",[]) if isinstance(x,dict) and x.get("name")=="generate_waybill_report"]
            denial_codes=[x.get("result",{}).get("error") for x in write_events if isinstance(x.get("result"),dict)]
            checks["no_document_row_created"] = current_document_count==before_document_count
            checks["write_tool_denied_safely"] = not write_events or (len(denial_codes)==len(write_events) and all(code in {"not_found_or_not_authorized","forbidden"} for code in denial_codes))
            read_events=[x for x in body.get("tool_events",[]) if isinstance(x,dict) and x.get("name") in {"search_waybills","get_waybill","get_waybill_business_status"}]
            checks["scope_denial_checked"] = bool(read_events or any(code in {"not_found_or_not_authorized","forbidden"} for code in denial_codes))
            evidence.update({"document_created":False,"document_count_before":before_document_count,"document_count_after":current_document_count,"write_tool_attempted":bool(write_events),"write_tool_denial_codes":denial_codes})
        return checks,evidence


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real-model acceptance on synthetic Julong data.")
    parser.add_argument("--run", action="store_true", help="make live model calls; omitted by default")
    parser.add_argument("--keychain-service", default="", help="load API token in-process from this macOS Keychain service")
    parser.add_argument("--base-url", default="", help="model API base URL")
    parser.add_argument("--model", default="", help="model identifier")
    parser.add_argument("--proxy", default="", help="explicit HTTPS proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--report-dir", default=str(Path(__file__).with_name("model_acceptance_results")))
    parser.add_argument("--case-id", action="append", default=[], help="run only a selected case ID; repeatable")
    parser.add_argument("--diagnostic-synthetic", action="store_true", help="include masked synthetic response snippets")
    args = parser.parse_args()
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    if args.case_id:
        wanted = set(args.case_id)
        cases = [case for case in cases if case["id"] in wanted]
        missing = sorted(wanted - {case["id"] for case in cases})
        if missing:
            _emit({"status":"BLOCKED","reason":"UNKNOWN_CASE_ID","case_ids":missing}, 2)
    if not args.run:
        _emit({"status":"READY","case_count":len(cases),"live_calls":False,"instruction":"Re-run with --run to issue real model requests."})

    if args.base_url:
        os.environ["JL_AI_BASE_URL"] = args.base_url
    elif not os.environ.get("JL_AI_BASE_URL"):
        os.environ["JL_AI_BASE_URL"] = "https://www.token01.net/v1"
    if args.model:
        os.environ["JL_AI_MODEL"] = args.model
    elif not os.environ.get("JL_AI_MODEL"):
        os.environ["JL_AI_MODEL"] = "deepseek-v4-flash"
    if args.proxy:
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["https_proxy"] = args.proxy
        os.environ["NO_PROXY"] = ""
        os.environ["no_proxy"] = ""
    if not _load_keychain_secret(args.keychain_service):
        _emit({"status":"BLOCKED","reason":"MODEL_NOT_CONFIGURED","case_count":len(cases),"live_calls":False}, 2)
    if certifi is None or not Path(certifi.where()).is_file():
        _emit({"status":"BLOCKED","reason":"TRUSTED_CA_BUNDLE_UNAVAILABLE","case_count":len(cases),"live_calls":False}, 2)
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    try:
        config = ModelConfig.from_env()
    except AIServiceError as exc:
        _emit({"status":"BLOCKED","reason":exc.code,"case_count":len(cases),"live_calls":False}, 2)

    parsed_base_host = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(config.base_url).hostname
    proxies = getproxies()
    proxy = proxies.get("https", "")
    if not proxy or proxy_bypass(parsed_base_host):
        _emit({"status":"BLOCKED","reason":"HTTPS_PROXY_MISSING_OR_BYPASSED","case_count":len(cases),"live_calls":False}, 2)
    if os.environ.get("SSL_CERT_FILE") != certifi.where():
        _emit({"status":"BLOCKED","reason":"UNEXPECTED_TLS_CA_CONFIGURATION","case_count":len(cases),"live_calls":False}, 2)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import importlib.util
    with tempfile.TemporaryDirectory(prefix="julong-real-model-acceptance-") as temp_dir:
        os.environ["DATA_DIR"] = temp_dir
        module_name = f"julong_model_acceptance_app_{os.getpid()}"
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "app.py")
        app = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = app
        spec.loader.exec_module(app)
        users, employee_task_id, other_employee_task_id, receipt_task_id = _seed(app, temp_dir)
        service = build_service(temp_dir, context_provider=app.WorkbenchContext(),
                                tool_executor=app.WorkbenchTools(), model_config=config)
        conversations: dict[str, str] = {}
        outcomes: list[dict[str, Any]] = []
        suite_started = time.monotonic()
        for case in cases:
            prompt = case["prompt"].replace("{{employee_task_id}}", employee_task_id).replace(
                "{{other_employee_task_id}}", other_employee_task_id).replace(
                "{{receipt_task_id}}", receipt_task_id)
            role_users = {
                "owner":"owner_accept", "employee_a":"employee_accept_a",
                "employee_b":"employee_accept_b", "dispatcher":"dispatcher_accept",
            }
            user = users[role_users[case["role"]]]
            body: dict[str, Any] = {"message": prompt}
            if case.get("memory_scope") in {"enterprise","personal"}:
                body["memory_scope"] = case["memory_scope"]
            thread_key = case.get("thread")
            if thread_key and thread_key in conversations:
                body["conversation_id"] = conversations[thread_key]
            started = time.monotonic()
            with app.db() as con:
                before_count = con.execute("SELECT count(*) FROM tasks").fetchone()[0]
                before_document_count = con.execute("SELECT count(*) FROM generated_documents WHERE tenant_id='julong-test'").fetchone()[0]
            status, response = handle_chat_request(service, body, user)
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if thread_key and response.get("conversation_id"):
                conversations[thread_key] = response["conversation_id"]
            reply = response.get("reply", "") if status == 200 else ""
            checks = _evaluate_text(case, reply)
            tool_names = [x.get("name") for x in response.get("tool_events", []) if isinstance(x, dict)]
            if status != 200 and response.get("conversation_id"):
                tool_trace = [
                    {"name":row["name"],"state":row["state"],"error_code":row["error_code"]}
                    for row in service.store.completed_events(response["conversation_id"])
                ]
                tool_names = [row["name"] for row in tool_trace]
            else:
                tool_trace = [
                    {"name":row.get("name"),"state":row.get("state"),"error_code":None,
                     "result_code":row.get("result",{}).get("error") if isinstance(row.get("result"),dict) else None}
                    for row in response.get("tool_events",[]) if isinstance(row,dict)
                ]
            required_tool = case.get("required_tool")
            if required_tool:
                checks["required_tool:" + required_tool] = required_tool in tool_names
            evidence = response.get("evidence", []) if isinstance(response.get("evidence"), list) else []
            evidence_sources = [x.get("source") for x in evidence if isinstance(x, dict)]
            required_source = case.get("required_evidence_source")
            if required_source:
                checks["required_evidence_source:" + required_source] = required_source in evidence_sources
            if case.get("require_citation"):
                evidence_ids = [x.get("evidence_id") for x in evidence if isinstance(x, dict) and isinstance(x.get("evidence_id"), str)]
                checks["cites_returned_evidence_id"] = any(evidence_id in reply for evidence_id in evidence_ids)
            for forbidden in case.get("forbidden_evidence_text", []):
                serialized_evidence = json.dumps(evidence, ensure_ascii=False)
                checks["forbidden_evidence:" + forbidden] = forbidden not in serialized_evidence
            if case.get("require_evidence"):
                checks["evidence_returned"] = bool(response.get("evidence"))
            action = case.get("action")
            action_evidence: dict[str,Any] = {}
            if action:
                resolved_action = dict(action)
                for key in ("task_id",):
                    if isinstance(resolved_action.get(key), str):
                        resolved_action[key] = resolved_action[key].replace("{{employee_task_id}}", employee_task_id).replace(
                            "{{other_employee_task_id}}", other_employee_task_id).replace(
                            "{{receipt_task_id}}", receipt_task_id)
                action_checks,action_evidence=_execute_action(app, users, resolved_action, before_count, response, status, before_document_count)
                checks.update(action_checks)
            checks["http_success"] = status in case.get("acceptable_http_statuses", [200])
            usage = response.get("model_usage") if isinstance(response.get("model_usage"), dict) else {}
            outcome = {
                "id":case["id"],"category":case["category"],"passed":bool(checks) and all(checks.values()),
                "http_status":status,"error_code":response.get("error",{}).get("code") if isinstance(response.get("error"),dict) else None,
                "checks":checks,"elapsed_ms":elapsed_ms,
                "model_elapsed_ms":response.get("model_elapsed_ms"),"usage":usage,
                "cost_status":"UNKNOWN","tool_names":tool_names,"tool_trace":tool_trace,"action_evidence":action_evidence,
            }
            if args.diagnostic_synthetic:
                outcome["answer_redacted"] = _sanitize_synthetic_answer(reply)
            outcomes.append(outcome)
            progress = {"case":case["id"],"passed":outcome["passed"],"http_status":status,
                        "elapsed_ms":elapsed_ms,"usage_complete":usage.get("complete",False)}
            if args.diagnostic_synthetic:
                progress["answer_redacted"] = outcome["answer_redacted"]
            print(json.dumps(progress,ensure_ascii=False), flush=True)
        elapsed_total = round((time.monotonic() - suite_started) * 1000)
        complete_usage = [x["usage"] for x in outcomes if x["usage"].get("complete")]
        usage_complete = len(complete_usage) == len(outcomes) and bool(outcomes)
        reported_usage = [x["usage"] for x in outcomes if x["usage"].get("reported_calls", 0)]
        token_totals = {
            key: sum(item[key] for item in reported_usage if isinstance(item.get(key), int))
                 if any(isinstance(item.get(key), int) for item in reported_usage) else None
            for key in ("prompt_tokens","completion_tokens","total_tokens")
        }
        report = {
            "suite":"julong-synthetic-model-acceptance-v1","status":"PASS" if all(x["passed"] for x in outcomes) else "FAIL",
            "model":config.model,"endpoint_host":parsed_base_host,"proxy_route":"configured_not_bypassed",
            "tls_validation":"enabled_certifi_bundle","case_count":len(outcomes),
            "passed_count":sum(x["passed"] for x in outcomes),
            "accuracy_rate":round(sum(x["passed"] for x in outcomes)/len(outcomes),4) if outcomes else None,
            "elapsed_ms":elapsed_total,"model_usage_complete_for_all_cases":usage_complete,
            "usage_unreported_case_count":len(outcomes)-len(reported_usage),
            "token_totals":token_totals,"cost":{"amount":None,"currency":None,"status":"UNKNOWN"},
            "results":outcomes,
        }
        report_dir = Path(args.report_dir)
        report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        report_path = report_dir / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}.json")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(report_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        _emit({"status":report["status"],"case_count":report["case_count"],"passed_count":report["passed_count"],
               "accuracy_rate":report["accuracy_rate"],"elapsed_ms":elapsed_total,"token_totals":token_totals,
               "cost_status":"UNKNOWN","report_path":str(report_path)},0 if report["status"]=="PASS" else 1)


if __name__ == "__main__":
    main()
