# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bounded page operations through the normal tool/permission/runtime lifecycle."""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput


class BrowserPageActionTool(Tool):
    accepts_tool_callback_context = True

    def __init__(self, runtime: Any):
        super().__init__(
            ToolCard(
                name="browser_page_action",
                description=(
                    "One bounded page operation: navigate to an explicit HTTP(S) URL, go back once, "
                    "or scroll one viewport up/down. Requires the current generation_id. "
                    "Observe the result before another action; do not replay an uncertain action."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "generation_id": {"type": "string"},
                        "op": {"type": "string", "enum": ["navigate", "navigate_back", "scroll"]},
                        "url": {"type": "string"},
                        "direction": {"type": "string", "enum": ["up", "down"]},
                    },
                    "required": ["generation_id", "op"],
                    "additionalProperties": False,
                },
            )
        )
        self._runtime = runtime

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        dispatched = False
        try:
            op = inputs.get("op")
            native = {
                "navigate": "browser_navigate",
                "navigate_back": "browser_navigate_back",
                "scroll": "browser_evaluate",
            }.get(op)
            if native is None:
                raise ValueError("unsupported_page_operation")
            allowed = self._runtime.service.allowed_tool_names
            if allowed is not None and native not in allowed:
                raise ValueError("page_operation_capability_denied")
            expected = {"generation_id", "op"} | (
                {"url"} if op == "navigate" else {"direction"} if op == "scroll" else set()
            )
            if set(inputs) != expected:
                raise ValueError("invalid_page_operation_arguments")
            self._runtime._ensure_page_state().validate_generation(inputs["generation_id"])
            if op == "navigate":
                url = urlsplit(inputs["url"])
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                    raise ValueError("invalid_page_navigation_url")
            elif op == "scroll" and inputs["direction"] not in {"up", "down"}:
                raise ValueError("invalid_scroll_direction")
            context = kwargs.get("_tool_callback_context")
            callback_inputs = getattr(context, "inputs", None)
            call_id = str(getattr(getattr(callback_inputs, "tool_call", None), "id", "") or "")
            session = kwargs.get("session")
            phase = session.get_state("__browser_phase_budget_state__") if session is not None else {}
            remaining = float(phase.get("deadline_at") or time.time() + 15) - time.time()
            remaining = min(remaining, float(phase.get("invocation_remaining_s", remaining)))
            if remaining <= 0:
                raise TimeoutError("browser_task_deadline")
            async with asyncio.timeout(remaining):
                if call_id.startswith("jev_"):
                    policy = getattr(self._runtime, "decision_policy", None)
                    if policy is None:
                        raise ValueError("browser_policy_unavailable_at_execution")
                    await policy.validate_tool_call(callback_inputs, session, actual_arguments=inputs)
                dispatched = True
                if op == "scroll":
                    sign = 1 if inputs["direction"] == "down" else -1
                    # Runtime-owned code only. Neither Jev nor tool arguments can provide JavaScript.
                    result = await self._runtime._call_playwright_run_code_unsafe(
                        "async (page) => await page.evaluate(() => {"
                        f"window.scrollBy(0, {sign} * Math.max(1, Math.floor(window.innerHeight * 0.8)));"
                        "return {ok:true,scroll_x:window.scrollX,scroll_y:window.scrollY};})"
                    )
                else:
                    args = {"url": inputs["url"]} if op == "navigate" else {}
                    result = await self._runtime._call_playwright_tool(native, args)
                outcome = self._runtime.classify_tool_result(result)
                if not outcome["success"]:
                    raise RuntimeError("page_operation_failed")
                self._runtime.record_tool_reference_state(tool_name=native, tool_args=inputs, tool_result=result)
                return ToolOutput(
                    success=True,
                    data={"ok": True, "executed": True, "state_changed": True, "operation": op, "result": result},
                )
        except Exception as exc:
            return ToolOutput(
                success=False,
                error=type(exc).__name__,
                data={
                    "ok": False,
                    "executed": dispatched,
                    "state_changed": dispatched,
                    "error": "page_action_uncertain" if dispatched else "page_action_rejected",
                    "recovery_hint": "Observe the page and use the LLM to reconcile; do not replay automatically.",
                },
            )

    async def stream(self, inputs: dict[str, Any], **kwargs: Any) -> AsyncIterator[ToolOutput]:
        yield await self.invoke(inputs, **kwargs)
