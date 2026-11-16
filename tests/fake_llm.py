"""A scripted stand-in for Gemini so the agent + MCP plumbing can be tested offline."""
from typing import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    final_json: str = "{}"
    seen_tool_output: list = []

    async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        last = llm_request.contents[-1]
        responses = [p.function_response for p in last.parts if p.function_response]
        if not responses:  # first turn: ask for the log
            call = types.FunctionCall(name="get_failed_log", args={"repo": "o/r", "run_id": 1})
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=call)]))
            return
        self.seen_tool_output.append(str(responses[0].response))
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=self.final_json)]))
