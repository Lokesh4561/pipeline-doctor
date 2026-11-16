"""Diagnostic tools the agent can call.

Each tool returns plain text that is short enough for an LLM to read.
Two backends:
  * live   - calls the GitHub, Neon (Postgres) and Databricks APIs
  * replay - returns recorded answers from an evaluation case file
             (set DOCTOR_REPLAY=/path/to/case.json)
"""
from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime, timezone

import requests

GITHUB_API = "https://api.github.com"
MAX_LOG_LINES = 120
ERROR_PATTERN = re.compile(
    r"(error|exception|traceback|failed|fatal|denied|refused|unreachable|timed? ?out|"
    r"429|403|401|404|500|503|quota|exceeded|invalid|not found|no such)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------- guardrails
_SECRET_PATTERNS = [
    (re.compile(r"(postgres(?:ql)?://[^:\s/]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "ghp_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), "AIza***"),
    (re.compile(r"\bdapi[0-9a-f]{20,}\b"), "dapi***"),
    (re.compile(r"(?i)(password|passwd|pwd|token|api[_-]?key|secret)(\s*[=:]\s*)\S+"), r"\1\2***"),
]


def redact(text: str) -> str:
    """Remove credentials before any text is shown to the LLM or put in an issue."""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def trim_log(text: str, max_lines: int = MAX_LOG_LINES) -> str:
    """Keep the lines that matter: every error-looking line plus the tail."""
    lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?", "", ln) for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    tail = lines[-(max_lines // 2):]
    errors = [ln for ln in lines[: -(max_lines // 2)] if ERROR_PATTERN.search(ln)]
    errors = errors[-(max_lines // 2):]
    return "\n".join(["[... earlier error lines ...]", *errors, "[... last lines ...]", *tail])


# ---------------------------------------------------------------- replay backend
def _replay(tool: str, **kwargs) -> str | None:
    path = os.getenv("DOCTOR_REPLAY")
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        case = json.load(f)
    trace = os.getenv("DOCTOR_TRACE")
    if trace:
        with open(trace, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tool": tool, "args": kwargs}) + "\n")
    answer = case.get("tools", {}).get(tool)
    if answer is None:
        return f"{tool}: no data available."
    return redact(answer if isinstance(answer, str) else json.dumps(answer, indent=1))


# ---------------------------------------------------------------- GitHub
def _gh_headers() -> dict:
    token = os.getenv("DOCTOR_GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gh(path: str, **params):
    r = requests.get(f"{GITHUB_API}{path}", headers=_gh_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r


def list_recent_runs(repo: str, status: str = "failure", limit: int = 5) -> str:
    """List recent GitHub Actions runs of a repository (owner/name).

    status: 'failure', 'success' or 'all'. Returns run id, workflow, time and commit message.
    """
    if (r := _replay("list_recent_runs", repo=repo, status=status)) is not None:
        return r
    params = {"per_page": max(1, min(limit, 20))}
    if status != "all":
        params["status"] = status
    runs = _gh(f"/repos/{repo}/actions/runs", **params).json().get("workflow_runs", [])
    if not runs:
        return f"No {status} runs found for {repo}."
    out = []
    for run in runs:
        msg = (run.get("head_commit") or {}).get("message", "").splitlines()[0][:80]
        out.append(
            f"run_id={run['id']} workflow='{run['name']}' conclusion={run.get('conclusion')} "
            f"created={run['created_at']} path={run.get('path')} commit='{msg}'"
        )
    return "\n".join(out)


def get_failed_log(repo: str, run_id: int) -> str:
    """Return the failed jobs and steps of a run plus the trimmed log of each failed job."""
    if (r := _replay("get_failed_log", repo=repo, run_id=run_id)) is not None:
        return r
    jobs = _gh(f"/repos/{repo}/actions/runs/{run_id}/jobs").json().get("jobs", [])
    failed = [j for j in jobs if j.get("conclusion") not in ("success", "skipped", None)]
    if not failed:
        return f"Run {run_id}: no failed jobs (the run may have been cancelled or failed before any job started, e.g. invalid workflow file)."
    parts = []
    for job in failed[:3]:
        steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
        try:
            log = _gh(f"/repos/{repo}/actions/jobs/{job['id']}/logs").text
        except requests.HTTPError as e:
            log = f"(could not download log: {e})"
        parts.append(f"JOB '{job['name']}' failed at step(s): {steps}\n{trim_log(log)}")
    return redact("\n\n".join(parts))


def get_workflow_file(repo: str, path: str) -> str:
    """Return the text of a workflow file, e.g. path='.github/workflows/pipeline.yml'."""
    if (r := _replay("get_workflow_file", repo=repo, path=path)) is not None:
        return r
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers={**_gh_headers(), "Accept": "application/vnd.github.raw+json"},
        timeout=30,
    )
    if r.status_code == 404:
        return f"{path} not found in {repo}."
    r.raise_for_status()
    return redact(r.text[:6000])


def recent_commits(repo: str, limit: int = 5) -> str:
    """List the latest commits (sha, date, message, changed files) to spot a breaking change."""
    if (r := _replay("recent_commits", repo=repo)) is not None:
        return r
    commits = _gh(f"/repos/{repo}/commits", per_page=max(1, min(limit, 10))).json()
    out = []
    for c in commits:
        detail = _gh(f"/repos/{repo}/commits/{c['sha']}").json()
        files = [f["filename"] for f in detail.get("files", [])][:8]
        msg = c["commit"]["message"].splitlines()[0][:80]
        out.append(f"{c['sha'][:7]} {c['commit']['author']['date']} '{msg}' files={files}")
    return "\n".join(out)


# ---------------------------------------------------------------- Neon / Postgres
def check_database() -> str:
    """Try to connect to the pipeline's Postgres database (DATABASE_URL) and report
    the exact error, or the newest data timestamp if FRESHNESS_SQL is set."""
    if (r := _replay("check_database")) is not None:
        return r
    url = os.getenv("DATABASE_URL")
    if not url:
        return "check_database: DATABASE_URL is not configured for the doctor."
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict

        extra = {}
        host = conninfo_to_dict(url).get("host")
        if host:  # GitHub runners have no IPv6 route; force IPv4 like the pipeline does
            try:
                extra["hostaddr"] = socket.getaddrinfo(host, 5432, socket.AF_INET)[0][4][0]
            except OSError:
                pass
        with psycopg.connect(url, connect_timeout=15, **extra) as conn:
            sql = os.getenv("FRESHNESS_SQL")
            if sql:
                value = conn.execute(sql).fetchone()[0]
                age = ""
                if isinstance(value, datetime):
                    v = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
                    age = f" ({(datetime.now(timezone.utc) - v).total_seconds() / 3600:.1f} hours ago)"
                return f"Database connection OK. Newest data: {value}{age}."
            return "Database connection OK."
    except Exception as e:  # report the real error text, it is the evidence
        return redact(f"Database connection FAILED: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- Databricks
def databricks_job_runs(limit: int = 5) -> str:
    """List recent Databricks job runs with their result and error message."""
    if (r := _replay("databricks_job_runs")) is not None:
        return r
    host, token = os.getenv("DATABRICKS_HOST"), os.getenv("DATABRICKS_TOKEN")
    if not host or not token:
        return "databricks_job_runs: DATABRICKS_HOST/DATABRICKS_TOKEN not configured."
    r = requests.get(
        f"{host.rstrip('/')}/api/2.1/jobs/runs/list",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": max(1, min(limit, 25)), "expand_tasks": "false"},
        timeout=30,
    )
    if r.status_code != 200:
        return redact(f"Databricks API returned HTTP {r.status_code}: {r.text[:300]}")
    out = []
    for run in r.json().get("runs", []):
        st = run.get("state", {})
        start = datetime.fromtimestamp(run.get("start_time", 0) / 1000, timezone.utc)
        out.append(
            f"run_id={run['run_id']} job='{run.get('run_name')}' start={start:%Y-%m-%d %H:%M}Z "
            f"state={st.get('life_cycle_state')} result={st.get('result_state')} "
            f"message='{st.get('state_message', '')[:200]}'"
        )
    return redact("\n".join(out) or "No Databricks runs found.")


ALL_TOOLS = [
    list_recent_runs,
    get_failed_log,
    get_workflow_file,
    recent_commits,
    check_database,
    databricks_job_runs,
]
