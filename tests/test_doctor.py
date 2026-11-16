import asyncio
import json

import pytest

from doctor import tools
from doctor.agent import CATEGORIES, Diagnosis, diagnose, parse_answer
from doctor.cli import to_markdown
from eval.cases import CASES, PLANTED_SECRETS
from eval.evaluate import fix_ok, percentile, score_case, summarize
from tests.fake_llm import ScriptedLlm


# ---------------------------------------------------------------- guardrails
@pytest.mark.parametrize("text,secret", [
    ("postgresql://neondb_owner:npg_abc123@ep-x.neon.tech/db", "npg_abc123"),
    ("token ghp_" + "a" * 36, "a" * 36),
    ("key=AIzaSyA" + "b" * 30, "b" * 30),
    ("dapi" + "0123456789abcdef" * 2, "0123456789abcdef"),
    ("password: hunter2", "hunter2"),
])
def test_redact_removes_secrets(text, secret):
    assert secret not in tools.redact(text)


def test_trim_log_keeps_errors_and_tail():
    log = "\n".join([f"noise {i}" for i in range(500)] + ["ValueError: boom"] + [f"tail {i}" for i in range(100)])
    log = log.replace("noise 10\n", "psycopg.OperationalError: early\n")
    out = tools.trim_log(log)
    assert "psycopg.OperationalError: early" in out
    assert "tail 99" in out
    assert len(out.splitlines()) <= tools.MAX_LOG_LINES + 2


def test_trim_log_strips_timestamps():
    assert tools.trim_log("2026-11-15T10:00:00.1234567Z Error: x") == "Error: x"


# ---------------------------------------------------------------- replay
def test_replay_returns_case_answer_and_traces(tmp_path, monkeypatch):
    case = tmp_path / "c.json"
    case.write_text(json.dumps({"tools": {"get_failed_log": "boom password=secret1"}}))
    trace = tmp_path / "t.jsonl"
    monkeypatch.setenv("DOCTOR_REPLAY", str(case))
    monkeypatch.setenv("DOCTOR_TRACE", str(trace))
    assert tools.get_failed_log("o/r", 1) == "boom password=***"
    assert "no data" in tools.check_database()
    assert [json.loads(l)["tool"] for l in trace.read_text().splitlines()] == ["get_failed_log", "check_database"]


def test_live_tools_report_missing_config(monkeypatch):
    for k in ("DOCTOR_REPLAY", "DATABASE_URL", "DATABRICKS_HOST", "DATABRICKS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert "not configured" in tools.check_database()
    assert "not configured" in tools.databricks_job_runs()


# ---------------------------------------------------------------- parsing
def test_parse_answer_handles_fence_and_bad_values():
    d = parse_answer('```json\n{"category":"bogus","confidence":"sure","fix":"do x"}\n```')
    assert d["category"] == "unknown" and d["confidence"] == "low" and d["fix"] == ["do x"]


def test_parse_answer_garbage():
    assert parse_answer("no json here") == {}


# ---------------------------------------------------------------- eval scoring
def test_cases_are_valid():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)) >= 20
    for c in CASES:
        assert c["expect"]["category"] in CATEGORIES
        assert c["origin"] in ("real", "realistic")
        assert "get_failed_log" in c["tools"]
        assert all(isinstance(g, list) and g for g in c["expect"]["fix_any"])


def test_planted_secret_is_redacted_by_replay(tmp_path, monkeypatch):
    case = next(c for c in CASES if any(s in json.dumps(c) for s in PLANTED_SECRETS))
    p = tmp_path / "c.json"
    p.write_text(json.dumps(case))
    monkeypatch.setenv("DOCTOR_REPLAY", str(p))
    out = tools.get_failed_log(case["repo"], case["run_id"])
    assert not any(s in out for s in PLANTED_SECRETS)


def test_fix_ok_needs_every_group():
    assert fix_ok("Regenerate the token and update the secret", [["token"], ["regenerate"]])
    assert not fix_ok("Update the secret", [["token"], ["regenerate"]])


def test_percentile_monotonic():
    xs = [1.0, 2.0, 3.0, 10.0]
    assert percentile(xs, 50) <= percentile(xs, 95) == 10.0
    assert percentile([], 50) == 0.0


def test_score_and_summary():
    case = CASES[0]
    good = {"category": case["expect"]["category"], "fix": ["force IPv4 with hostaddr"],
            "tools_called": ["get_failed_log"], "seconds": 2.0, "root_cause": "IPv6"}
    row = score_case(case, good)
    assert row["category_ok"] and row["fix_ok"] and not row["leaked_secret"]
    s = summarize([row])
    assert s["category_accuracy"] == 100.0 and s["log_read_first"] == 100.0


# ---------------------------------------------------------------- end to end (agent -> MCP stdio -> replay)
def test_agent_calls_mcp_tool_and_parses(tmp_path):
    case = tmp_path / "c.json"
    case.write_text(json.dumps({"tools": {"get_failed_log": "Network is unreachable token=abc"}}))
    llm = ScriptedLlm(final_json=json.dumps({
        "category": "network", "root_cause": "IPv6 route missing",
        "evidence": ["Network is unreachable"], "fix": ["force IPv4"], "confidence": "high"}))
    llm.seen_tool_output = []
    d = asyncio.run(diagnose("o/r", 1, model=llm, extra_env={"DOCTOR_REPLAY": str(case)},
                             tool_filter=["get_failed_log"]))
    assert d.category == "network" and d.tools_called == ["get_failed_log"]
    assert "Network is unreachable" in llm.seen_tool_output[0]
    assert "abc" not in llm.seen_tool_output[0]


def test_markdown_issue_body():
    d = Diagnosis(category="auth_failure", root_cause="Bad password", evidence=["password=xyz"],
                  fix=["Reset it"], confidence="high", tools_called=["get_failed_log"], seconds=3.1)
    md = to_markdown(d, "repo run 1", "https://github.com/x")
    assert "auth_failure" in md and "1. Reset it" in md and "xyz" not in md


# ---------------------------------------------------------------- watch command
def test_watch_skips_reported_runs_and_opens_issue(monkeypatch):
    import argparse
    from doctor import cli

    runs = {"o/a": [{"id": 1, "name": "ingest", "path": "p.yml", "created_at": "t", "html_url": "u1",
                     "head_commit": {"message": "msg"}},
                    {"id": 12, "name": "ingest", "path": "p.yml", "created_at": "t", "html_url": "u2",
                     "head_commit": {"message": "msg"}}]}
    opened = []

    async def fake_diagnose(repo, run_id, context=""):
        return Diagnosis(category="network", root_cause="x", fix=["y"], confidence="high")

    monkeypatch.setenv("WATCH_REPOS", "o/a")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/doctor")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(cli, "failed_runs", lambda repo, since: runs[repo])
    monkeypatch.setattr(cli, "already_reported", lambda repo: "[network] a · ingest · run 1\nother")
    monkeypatch.setattr(cli, "open_issue", lambda repo, title, body: opened.append(title) or "url")
    monkeypatch.setattr(cli, "diagnose", fake_diagnose)
    args = argparse.Namespace(lookback_hours=2, max_runs=3, dry_run=False)
    assert asyncio.run(cli.cmd_watch(args)) == 0
    assert opened == ["[network] a · ingest · run 12"]
