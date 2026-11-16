"""Google ADK agent that investigates a failed run through the MCP tools."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

CATEGORIES = [
    "network",               # DNS, IPv6/IPv4, connection refused/unreachable
    "auth_failure",          # wrong/expired password or token, 401/403
    "secret_misconfigured",  # secret missing, empty, or pasted in the wrong format
    "workflow_config",       # invalid workflow YAML, wrong runner, bad trigger
    "rate_limit_or_quota",   # HTTP 429, free-tier or compute quota exceeded
    "timeout",               # step/job/request timed out or hung
    "dependency",            # package missing, version conflict, runner image change
    "code_bug",              # exception caused by our own code change
    "data_issue",            # missing file, empty/invalid input, schema changed
    "upstream_outage",       # third-party service down (5xx), endpoint suspended
    "unknown",
]

DEFAULT_MODEL = os.getenv("DOCTOR_MODEL", "gemini-2.5-flash")

INSTRUCTION = f"""You are Pipeline Doctor, an on-call data engineer.
A data pipeline run failed. Find the ROOT CAUSE and the FIX.

How to work:
1. Call get_failed_log for the run first. Read the first real error, not the last noise.
2. Use more tools only when they add evidence:
   - get_workflow_file if the run has no jobs or the error points at the workflow;
   - recent_commits if the failure started after a change;
   - check_database for connection/auth problems with Postgres;
   - databricks_job_runs for Databricks-side problems.
3. Never guess beyond the evidence. If the evidence is not enough, use category "unknown".
4. Never print secrets, passwords or tokens.

Finish with ONLY a JSON object, no markdown fence:
{{"category": one of {CATEGORIES},
  "root_cause": "one or two plain sentences",
  "evidence": ["exact short log lines or tool facts that prove it"],
  "fix": ["concrete steps, with the file/secret/setting to change"],
  "confidence": "high" | "medium" | "low"}}"""


@dataclass
class Diagnosis:
    category: str = "unknown"
    root_cause: str = ""
    evidence: list[str] = field(default_factory=list)
    fix: list[str] = field(default_factory=list)
    confidence: str = "low"
    tools_called: list[str] = field(default_factory=list)
    seconds: float = 0.0
    raw: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def parse_answer(text: str) -> dict:
    """Pull the JSON object out of the model's last message."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if data.get("category") not in CATEGORIES:
        data["category"] = "unknown"
    if data.get("confidence") not in ("high", "medium", "low"):
        data["confidence"] = "low"
    for key in ("evidence", "fix"):
        v = data.get(key, [])
        data[key] = [str(x) for x in v] if isinstance(v, list) else [str(v)]
    return data


def build_agent(model=None, extra_env: dict | None = None, tool_filter: list[str] | None = None) -> LlmAgent:
    env = dict(os.environ)  # the MCP subprocess needs the tokens and DOCTOR_REPLAY
    env.update(extra_env or {})
    toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable, args=["-m", "doctor.mcp_server"], env=env
            ),
            timeout=60,
        ),
        tool_filter=tool_filter,
    )
    return LlmAgent(
        name="pipeline_doctor",
        model=model or DEFAULT_MODEL,
        instruction=INSTRUCTION,
        tools=[toolset],
        generate_content_config=types.GenerateContentConfig(temperature=0),
    )


async def diagnose(repo: str, run_id: str | int, context: str = "", model=None,
                   extra_env: dict | None = None, tool_filter: list[str] | None = None) -> Diagnosis:
    agent = build_agent(model, extra_env, tool_filter)
    runner = InMemoryRunner(agent=agent, app_name="pipeline_doctor")
    session = await runner.session_service.create_session(app_name="pipeline_doctor", user_id="ci")
    prompt = f"Repository: {repo}\nFailed run id: {run_id}\n{context}".strip()
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])

    started, final, tools = time.perf_counter(), "", []
    try:
        async for event in runner.run_async(user_id="ci", session_id=session.id, new_message=msg):
            for call in event.get_function_calls() or []:
                tools.append(call.name)
            if event.content and event.content.parts and event.is_final_response():
                final = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()

    data = parse_answer(final)
    return Diagnosis(
        category=data.get("category", "unknown"),
        root_cause=data.get("root_cause", ""),
        evidence=data.get("evidence", []),
        fix=data.get("fix", []),
        confidence=data.get("confidence", "low"),
        tools_called=tools,
        seconds=round(time.perf_counter() - started, 1),
        raw=final,
    )
