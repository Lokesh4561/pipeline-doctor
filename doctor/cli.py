"""Command line entry points.

  python -m doctor.cli diagnose --repo owner/name --run-id 123
  python -m doctor.cli watch            # used by the hourly GitHub Action
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

from doctor.agent import Diagnosis, diagnose
from doctor.tools import GITHUB_API, _gh_headers, redact

LABEL = "pipeline-doctor"
ICON = {"high": "🟢", "medium": "🟡", "low": "🔴"}


def to_markdown(d: Diagnosis, source: str, link: str = "") -> str:
    lines = [
        f"**Source:** {source}" + (f" · [open run]({link})" if link else ""),
        f"**Category:** `{d.category}` · **Confidence:** {ICON.get(d.confidence, '')} {d.confidence}",
        "",
        "### Root cause",
        d.root_cause or "_The agent could not determine the root cause._",
        "",
        "### Evidence",
        *[f"- `{e[:300]}`" for e in d.evidence],
        "",
        "### Suggested fix",
        *[f"{i}. {s}" for i, s in enumerate(d.fix, 1)],
        "",
        f"<sub>Pipeline Doctor (Google ADK + MCP) · tools used: {', '.join(d.tools_called) or 'none'}"
        f" · {d.seconds}s · AI-generated, check before applying.</sub>",
    ]
    return redact("\n".join(lines))


# ---------------------------------------------------------------- GitHub helpers
def failed_runs(repo: str, since: datetime) -> list[dict]:
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/actions/runs",
        headers=_gh_headers(),
        params={"status": "failure", "per_page": 20, "created": f">={since:%Y-%m-%dT%H:%M:%SZ}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("workflow_runs", [])


def already_reported(doctor_repo: str) -> str:
    """Titles of every issue the doctor opened before (open or closed), one per line."""
    titles, page = [], 1
    while page <= 5:
        r = requests.get(
            f"{GITHUB_API}/repos/{doctor_repo}/issues",
            headers=_gh_headers(),
            params={"labels": LABEL, "state": "all", "per_page": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        titles += [i["title"] for i in batch]
        if len(batch) < 100:
            break
        page += 1
    return "\n".join(titles)


def open_issue(doctor_repo: str, title: str, body: str) -> str:
    r = requests.post(
        f"{GITHUB_API}/repos/{doctor_repo}/issues",
        headers=_gh_headers(),
        json={"title": title, "body": body, "labels": [LABEL]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["html_url"]


def summary(text: str) -> None:
    print(text)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")


# ---------------------------------------------------------------- commands
async def cmd_diagnose(args) -> int:
    d = await diagnose(args.repo, args.run_id)
    print(to_markdown(d, f"{args.repo} run {args.run_id}"))
    return 0


async def cmd_watch(args) -> int:
    repos = [r.strip() for r in os.getenv("WATCH_REPOS", "").split(",") if r.strip()]
    doctor_repo = os.getenv("GITHUB_REPOSITORY")
    if not repos or not doctor_repo:
        print("Set WATCH_REPOS (comma separated owner/name) and GITHUB_REPOSITORY.")
        return 1
    since = datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)
    seen = {int(m) for m in re.findall(r"run (\d+)\s*$", already_reported(doctor_repo), re.MULTILINE)}
    todo = []
    for repo in repos:
        for run in failed_runs(repo, since):
            if run["id"] not in seen:
                todo.append((repo, run))
    summary(f"Found {len(todo)} new failed run(s) since {since:%Y-%m-%d %H:%M}Z.")

    for repo, run in todo[: args.max_runs]:
        msg = (run.get("head_commit") or {}).get("message", "").splitlines()[0][:80]
        context = (
            f"Workflow: {run['name']} ({run.get('path')})\nStarted: {run['created_at']}\n"
            f"Commit message: {msg}"
        )
        try:
            d = await diagnose(repo, run["id"], context)
        except Exception as e:  # quota or model errors: stop, try again next hour
            summary(f"Stopped: model call failed ({type(e).__name__}: {str(e)[:200]}).")
            break
        title = f"[{d.category}] {repo.split('/')[-1]} · {run['name']} · run {run['id']}"
        body = to_markdown(d, f"{repo} · {run['name']}", run["html_url"])
        url = open_issue(doctor_repo, title, body) if not args.dry_run else "(dry run)"
        summary(f"### {title}\n{url}\n\n{body}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="doctor")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnose", help="diagnose one failed run")
    d.add_argument("--repo", required=True)
    d.add_argument("--run-id", required=True)
    w = sub.add_parser("watch", help="diagnose new failures in WATCH_REPOS and open issues")
    w.add_argument("--lookback-hours", type=int, default=24)
    w.add_argument("--max-runs", type=int, default=3)
    w.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return asyncio.run(cmd_diagnose(args) if args.cmd == "diagnose" else cmd_watch(args))


if __name__ == "__main__":
    sys.exit(main())
