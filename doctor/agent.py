"""Google ADK agent that investigates a failed run through the MCP tools."""
from __future__ import annotations

import asyncio
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

CATEGORY_HELP = {
    "network": "DNS, IPv6/IPv4 routing, connection refused or unreachable",
    "auth_failure": "a credential exists but is wrong, expired or revoked (401/403, password authentication failed)",
    "secret_misconfigured": "a secret or setting is missing, empty, misspelled or pasted in the wrong format (e.g. an env var that is not set)",
    "workflow_config": "the workflow file itself is wrong: invalid YAML, bad cron, missing permissions, wrong trigger",
    "rate_limit_or_quota": "HTTP 429, free-tier, compute or data-transfer quota exceeded",
    "timeout": "a step, job or request timed out or hung",
    "dependency": "a package, tool or runtime version is missing, conflicting or unsupported (e.g. pip resolution errors)",
    "code_bug": "an exception caused by our own code (e.g. a SyntaxError or a failing unit test)",
    "data_issue": "the input data is the problem: missing, empty or in an unexpected shape",
    "upstream_outage": "a third-party service or platform failed temporarily (e.g. HTTP 5xx); a retry fixes it",
    "unknown": "the evidence is not enough to decide",
}
CATEGORIES = list(CATEGORY_HELP)

# Tried in order: if a model is busy (503), rate limited (429) or retired (404),
# the next one is used. Override with DOCTOR_MODEL="model-a,model-b".
DEFAULT_MODELS = "gemini-3.8-flash,gemini-flash-latest,gemini-flash-lite-latest"
CASE_TIMEOUT = float(os.getenv("DOCTOR_TIMEOUT", "150"))  # seconds per diagnosis attempt
SKIP_MODEL_ERRORS = ("429", "503", "404", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "NOT_FOUND", "TimeoutError")


def models() -> list[str]:
    return [m.strip() for m in (os.getenv("DOCTOR_MODEL") or DEFAULT_MODELS).split(",") if m.strip()]

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

Categories (pick the one that names the ROOT cause, not the symptom):
{chr(10).join(f"- {k}: {v}" for k, v in CATEGORY_HELP.items())}

Finish with ONLY a JSON object, no markdown fence:
{{"category": one of the categories above,
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
    model: str = ""

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
        model=model or models()[0],
        instruction=INSTRUCTION,
        tools=[toolset],
        generate_content_config=types.GenerateContentConfig(
            temperature=0,
            http_options=types.HttpOptions(
                timeout=60_000,  # ms per model call
                retry_options=types.HttpRetryOptions(
                    attempts=2, initial_delay=3, max_delay=10, http_status_codes=[429, 500, 503, 504]
                ),
            ),
        ),
    )


async def _diagnose_once(repo, run_id, context, model, extra_env, tool_filter) -> Diagnosis:
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
        model=model if isinstance(model, str) else getattr(model, "model", ""),
    )


async def diagnose(repo: str, run_id: str | int, context: str = "", model=None,
                   extra_env: dict | None = None, tool_filter: list[str] | None = None) -> Diagnosis:
    """Diagnose one run. Falls back to the next model when one is busy, rate limited or retired."""
    candidates = [model] if model is not None else models()
    last_error: Exception | None = None
    for m in candidates:
        try:
            return await asyncio.wait_for(
                _diagnose_once(repo, run_id, context, m, extra_env, tool_filter), timeout=CASE_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001
            last_error = e
            text = f"{type(e).__name__}: {e}"
            if not any(code in text for code in SKIP_MODEL_ERRORS):
                raise
            print(f"  model {m} unavailable ({text[:120]}), trying next", file=sys.stderr, flush=True)
    raise RuntimeError(f"All models failed. Last error: {last_error}")
