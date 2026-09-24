# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Browser policy facade: choose or delegate; all actions use normal tool rails."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.message import (
    AssistantMessage,
    ToolMessage,
    UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.llm.utils.request_sanitizer import clean_unicode

from ..playwright_runtime.browser_logging import browser_agent_log_info
from .action_space import build_menu, build_request
from .config import BrowserDecisionConfig
from .guard import DecisionGuard, canonical_arguments, validate_binding, validate_guard
from .intent import normalize_goal
from .jev_client import DecisionUnavailable, JevClient, decision_trace, validate_choice

CONTEXT_KEY = "browser_policy_observation"
_PHASE_KEY = "__browser_phase_budget_state__"


@dataclass
class _TaskPolicy:
    decisions: int = 0
    fallback_reason: str = ""
    last_action: str = ""
    fallback_scope: str = ""
    blocked_state: str = ""
    evaluated_states: list[str] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    search_bindings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _Observation:
    task_key: str
    session_id: str
    deadline_at: float
    state: dict[str, Any]
    controls: list[dict[str, Any]]
    page: dict[str, Any]
    error: str = ""


class BrowserPolicyModel(Model):
    """Keep Model's public shape and delegate generative/KV operations to its client."""

    def __init__(
        self, fallback: Model, config: BrowserDecisionConfig, runtime: Any, *, client: JevClient | None = None
    ):
        # Do not construct a second generative provider or install duplicate callbacks.
        self.bind_fallback(fallback)
        self.decision_config = config
        self.runtime = runtime
        self.jev = client or JevClient(config)
        self._observations: OrderedDict[str, _Observation] = OrderedDict()
        self._tasks: OrderedDict[str, _TaskPolicy] = OrderedDict()
        self._guards: dict[str, DecisionGuard] = {}
        self._sessions: dict[str, Any] = {}
        self.current_intent = ""
        self._shadow_tasks: set[asyncio.Task] = set()
        browser_agent_log_info(
            "[BROWSER_POLICY_CONFIG] %s",
            json.dumps(
                {
                    "mode": config.mode,
                    "provider": config.provider,
                    "model": config.model,
                    "api_key_env": config.api_key_env,
                    "min_confidence": config.min_confidence,
                    "candidate_limit": config.candidate_limit,
                    "max_decisions": config.max_decisions,
                    "request_timeout_ms": config.request_timeout_ms,
                    "max_retries": config.max_retries,
                    "fallback_reserve_ms": config.fallback_reserve_ms,
                },
                ensure_ascii=False,
            ),
        )

    def bind_fallback(self, model: Model) -> None:
        if isinstance(model, BrowserPolicyModel):
            model = model.fallback
        self.fallback = model
        self.model_config = model.model_config
        self.model_client_config = model.model_client_config
        self._client = getattr(model, "_client", None)

    def _task(self, key: str) -> _TaskPolicy:
        if key not in self._tasks:
            self._tasks[key] = _TaskPolicy()
        self._tasks.move_to_end(key)
        while len(self._tasks) > 64:
            old_key, _ = self._tasks.popitem(last=False)
            self._sessions.pop(old_key, None)
        return self._tasks[key]

    @staticmethod
    def _task_key(session: Any, phase: dict[str, Any]) -> str:
        owner = phase.get("query_id") or session.get_session_id()
        return f"{owner}:{phase.get('task_id')}:{phase.get('deadline_started_at')}"

    def _bind_task(self, session: Any, phase: dict[str, Any]) -> _TaskPolicy:
        key = self._task_key(session, phase)
        if key not in self._tasks:
            saved = phase.get("decision_policy") or {}
            if saved.get("task_key") == key:
                self._tasks[key] = _TaskPolicy(
                    **{name: copy.deepcopy(saved[name]) for name in _TaskPolicy.__dataclass_fields__ if name in saved}
                )
        task = self._task(key)
        self._sessions[key] = session
        return task

    def _persist(self, key: str) -> None:
        session = self._sessions.get(key)
        phase = session.get_state(_PHASE_KEY) if session is not None else None
        if isinstance(phase, dict) and self._task_key(session, phase) == key:
            phase["decision_policy"] = {"task_key": key, **asdict(self._task(key))}
            if callable(getattr(session, "update_state", None)):
                session.update_state({_PHASE_KEY: phase})

    @staticmethod
    def _count(task: _TaskPolicy, name: str) -> None:
        task.counters[name] = task.counters.get(name, 0) + 1

    @staticmethod
    def _fingerprint(observation: _Observation) -> str:
        # IDs, timestamps and arbitrary page text are not evidence of executable progress.
        controls = []
        for control in observation.controls:
            details = control.get("decision_state") or {}
            controls.append(
                {
                    "name": control.get("name"),
                    "text": control.get("text"),
                    "role": control.get("role"),
                    "href": control.get("href"),
                    "enabled": control.get("enabled"),
                    "actionable": control.get("actionable"),
                    "selected": control.get("selected"),
                    "value": details.get("current_value"),
                    "checked": details.get("checked"),
                    "expanded": (details.get("node_guard") or {}).get("expanded"),
                    "options": details.get("options"),
                }
            )
        state = observation.state
        value = {
            "intent": state.get("current_intent"),
            "url": observation.page.get("url"),
            "observed": not bool(observation.error),
            "history_length": (observation.page.get("page_guard") or {}).get("history_length"),
            "controls": sorted(controls, key=canonical_arguments),
            "position": state.get("page_position"),
            "semantic": state.get("executable_state"),
        }
        return hashlib.sha256(canonical_arguments(value).encode("utf-8", "replace")).hexdigest()[:24]

    def should_observe(self, context: Any) -> bool:
        session = context.get_session_ref() if context is not None else None
        phase = session.get_state(_PHASE_KEY) if session is not None else None
        if not isinstance(phase, dict) or not phase.get("goal") or self.decision_config.mode == "llm":
            return False
        self.current_intent = normalize_goal(phase.get("task") or phase["goal"])
        return self._bind_task(session, phase).fallback_scope not in {"task", "finish"}

    async def publish_context(
        self, context: Any, captured: dict[str, Any], *, refresh: bool, observation_only: bool
    ) -> dict[str, str]:
        del observation_only  # Observation acquisition belongs to runtime, not to the policy.
        session = context.get_session_ref() if context is not None else None
        phase = session.get_state(_PHASE_KEY) if session is not None else None
        if not isinstance(phase, dict) or not phase.get("goal"):
            return {}
        session_id = session.get_session_id()
        key = self._task_key(session, phase)
        task = self._bind_task(session, phase)
        now = time.time()
        deadline = float(phase.get("deadline_at") or now)
        if phase.get("invocation_remaining_s") is not None:
            deadline = min(deadline, now + float(phase["invocation_remaining_s"]))
        error = "" if captured.get("ok") else "observation_unavailable"
        controls: list[dict[str, Any]] = []
        omitted = 0
        page = dict(captured.get("page_state") or {})
        snapshot = captured.get("decision_observation") or {}
        if task.fallback_scope not in {"task", "finish"} and not error:
            try:
                remaining = deadline - time.time() - self.decision_config.fallback_reserve_ms / 1000
                if remaining <= 0:
                    raise DecisionUnavailable("insufficient_decision_time")
                if not snapshot.get("capture_id") or snapshot.get("url") != captured.get("url"):
                    raise DecisionUnavailable("decision_observation_unavailable")
                current = self.runtime._ensure_page_state()
                page = dict(snapshot.get("page") or {})
                if (
                    page.get("page_id") != current.page_id
                    or page.get("generation_id") != current.generation_id
                    or page.get("url") != current.url
                ):
                    raise DecisionUnavailable("observation_generation_changed")
                controls = snapshot.get("controls") or []
                omitted = int(snapshot.get("omitted_count") or 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Observation failure belongs to the policy, never to the existing LLM task.
                error = "decision_observation_unavailable"
        page["page_guard"] = snapshot.get("page_guard") or {}
        page["page_position"] = captured.get("page_position") or {}
        progress = captured.get("semantic_progress") or {}
        goal = normalize_goal(phase.get("goal"))
        intent = normalize_goal(phase.get("task")) or goal
        self.current_intent = intent
        semantic = captured.get("semantic_state") or {}
        state = {
            "goal": goal[:8000],
            "task_text_truncated": len(goal) > 8000 or len(intent) > 4000,
            "current_intent": intent[:4000],
            "repair_instruction": intent[:4000],
            "page_position": page["page_position"],
            "executable_state": {
                key: semantic[key]
                for key in ("form_values", "selected_filters", "selected_dates", "first_card_title", "result_count")
                if key in semantic
            },
            "execution_receipts": copy.deepcopy(task.receipts[-6:]),
            "page": {k: page.get(k) for k in ("page_id", "generation_id", "url", "title")},
            "page_text": str(snapshot.get("page_text") or "")[:6000],
            "capture_id": snapshot.get("capture_id"),
            "observed_at_ms": snapshot.get("observed_at_ms"),
            "visibility": snapshot.get("visibility"),
            "recent_results": copy.deepcopy((phase.get("recent_actions") or [])[-6:]),
            "no_progress": int(progress.get("consecutive_no_progress") or 0),
            "omitted_count": omitted,
            "probe_excluded": snapshot.get("excluded") or {},
        }
        token = uuid.uuid4().hex
        self._observations[token] = _Observation(
            key, session_id, deadline, clean_unicode(state), clean_unicode(controls), page, error
        )
        if refresh and not error and task.receipts and task.pending.get("capture_id") != snapshot.get("capture_id"):
            receipt = task.receipts[-1]
            if receipt.get("postcondition") == "awaiting_observation":
                changed = self._fingerprint(self._observations[token]) != task.pending.get("state")
                receipt["postcondition"] = "observed_state_change" if changed else "no_observable_progress"
                self._count(task, receipt["postcondition"])
                browser_agent_log_info("[BROWSER_POLICY_POSTCONDITION] %s", json.dumps({**receipt, "task_id": key}))
        while len(self._observations) > 16:
            self._observations.popitem(last=False)
        self._persist(key)
        return {CONTEXT_KEY: token}

    def _take_observation(self, messages: Any) -> _Observation | None:
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            metadata = getattr(message, "metadata", {})
            token = metadata.get(CONTEXT_KEY) if isinstance(metadata, dict) else None
            if token:
                return self._observations.pop(token, None)
        return None

    @staticmethod
    def _llm_messages(messages: Any) -> Any:
        if not isinstance(messages, list):
            return messages
        return [
            message.model_copy(update={"metadata": {k: v for k, v in message.metadata.items() if k != CONTEXT_KEY}})
            if CONTEXT_KEY in getattr(message, "metadata", {})
            else message
            for message in messages
        ]

    @staticmethod
    def _last_tool_failed(messages: Any) -> bool:
        for message in reversed(messages if isinstance(messages, list) else []):
            if isinstance(message, ToolMessage):
                if message.metadata.get("success") is False or message.metadata.get("denied") is True:
                    return True
                try:
                    result = json.loads(message.content)
                except (ValueError, TypeError):
                    return False
                return isinstance(result, dict) and (result.get("ok") is False or result.get("executed") is False)
        return False

    @staticmethod
    def _has_batch_tool(tools: Any, name_required: str = "browser_batch_interact") -> bool:
        for tool in tools or []:
            if isinstance(tool, dict):
                name = tool.get("name") or (tool.get("function") or {}).get("name")
            else:
                name = getattr(tool, "name", None)
            if name == name_required:
                return True
        return False

    async def _choose(self, messages: Any, tools: Any) -> tuple[AssistantMessage | None, dict[str, Any], float | None]:
        observation = self._take_observation(messages)
        diagnostic: dict[str, Any] = {
            "mode": self.decision_config.mode,
            "provider": self.decision_config.provider,
            "route": "llm",
        }
        if observation is None or self.decision_config.mode == "llm":
            diagnostic.update(reason="no_policy_context", evaluated=False, model_source="llm")
            browser_agent_log_info("[BROWSER_POLICY] %s", json.dumps(diagnostic))
            return None, diagnostic, None
        if self.decision_config.mode == "shadow":
            self._count(self._task(observation.task_key), "windows")
            self._persist(observation.task_key)
            diagnostic["reason"] = "shadow_busy" if self._shadow_tasks else "shadow_scheduled"
            if not self._shadow_tasks:
                task = asyncio.create_task(
                    self._decide(
                        observation,
                        has_batch_tool=self._has_batch_tool(tools),
                        last_tool_failed=self._last_tool_failed(messages),
                        has_page_tool=self._has_batch_tool(tools, "browser_page_action"),
                    )
                )
                self._shadow_tasks.add(task)
                task.add_done_callback(self._shadow_done)
            diagnostic.update(task_id=observation.task_key, evaluated=False, model_source="llm")
            browser_agent_log_info("[BROWSER_POLICY] %s", json.dumps(diagnostic))
            return None, diagnostic, observation.deadline_at
        return await self._decide(
            observation,
            has_batch_tool=self._has_batch_tool(tools),
            last_tool_failed=self._last_tool_failed(messages),
            has_page_tool=self._has_batch_tool(tools, "browser_page_action"),
        )

    def _shadow_done(self, task: asyncio.Task) -> None:
        self._shadow_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even when the LLM finishes before the shadow request.

    async def _decide(
        self, observation: _Observation, *, has_batch_tool: bool, last_tool_failed: bool, has_page_tool: bool = False
    ) -> tuple[AssistantMessage | None, dict[str, Any], float | None]:
        decision_id = "jev_" + uuid.uuid4().hex
        diagnostic: dict[str, Any] = {
            "mode": self.decision_config.mode,
            "provider": self.decision_config.provider,
            "route": "llm",
            "model_source": "llm",
            "decision_id": decision_id,
            "task_id": observation.task_key,
            "evaluated": False,
            "cached_fallback": False,
        }
        task = self._task(observation.task_key)
        fingerprint = self._fingerprint(observation)
        diagnostic["state_fingerprint"] = fingerprint
        if self.decision_config.mode != "shadow":
            self._count(task, "windows")
        started = time.monotonic()
        try:
            if task.fallback_scope in {"task", "finish"}:
                diagnostic["cached_fallback"] = True
                raise DecisionUnavailable(task.fallback_reason)
            reconciled = (
                task.fallback_reason == "runtime_recovery_required"
                and not last_tool_failed
                and not observation.error
                and observation.state["no_progress"] < 2
            )
            local_gate = task.fallback_reason in {"batch_tool_unavailable", "no_supported_actions"}
            if task.blocked_state == fingerprint and not reconciled and not local_gate:
                diagnostic["cached_fallback"] = True
                raise DecisionUnavailable(task.fallback_reason or "unchanged_state")
            if fingerprint in task.evaluated_states:
                diagnostic["cached_fallback"] = True
                raise DecisionUnavailable("already_evaluated_state")
            if observation.error:
                raise DecisionUnavailable(observation.error)
            if not observation.state.get("current_intent"):
                raise DecisionUnavailable("missing_task_intent")
            if observation.state.get("task_text_truncated"):
                raise DecisionUnavailable("task_intent_truncated")
            if task.decisions >= self.decision_config.max_decisions:
                raise DecisionUnavailable("decision_budget_exhausted")
            if not has_batch_tool and not has_page_tool:
                raise DecisionUnavailable("batch_tool_unavailable")
            if last_tool_failed or observation.state["no_progress"] >= 2:
                raise DecisionUnavailable("runtime_recovery_required")
            if task.fallback_scope == "segment":
                self._count(task, "reentries")
            task.fallback_reason, task.fallback_scope, task.blocked_state = "", "", ""
            menu = build_menu(
                observation.controls if has_batch_tool else [],
                observation.state["current_intent"],
                limit=self.decision_config.candidate_limit,
                page=observation.page,
                allow_page_actions=has_page_tool,
                search_bindings=task.search_bindings,
            )
            diagnostic.update(
                candidate_count=len(menu.steps),
                excluded=menu.excluded,
                probe_excluded=observation.state["probe_excluded"],
                omitted_count=observation.state["omitted_count"] + menu.omitted,
            )
            if not menu.steps:
                raise DecisionUnavailable("no_supported_actions")
            state = {
                **{key: value for key, value in observation.state.items() if key != "executable_state"},
                "candidate_count": len(menu.steps),
                "omitted_count": diagnostic["omitted_count"],
            }
            payload = build_request(self.decision_config.model, state, menu)
            task.decisions += 1
            task.evaluated_states.append(fingerprint)
            diagnostic.update(evaluated=True, decisions=task.decisions)
            self._count(task, "evaluations")
            self._persist(observation.task_key)  # Count cancelled/failed requests too, including after resume.
            browser_agent_log_info("[BROWSER_POLICY_REQUEST] %s", json.dumps(diagnostic))
            request_started = time.monotonic()
            try:
                with decision_trace(decision_id, observation.task_key):
                    result = await self.jev.evaluate(
                        payload, deadline_at=(observation.deadline_at - self.decision_config.fallback_reserve_ms / 1000)
                    )
            finally:
                diagnostic["jev_ms"] = round((time.monotonic() - request_started) * 1000, 2)
            answer = (result.get("answers") or {}).get("action")
            answer = answer if isinstance(answer, dict) else {}
            choice = answer.get("choice")
            numbers = [
                value
                for value in (answer.get("probabilities") or {}).values()
                if type(value) in {int, float} and math.isfinite(value)
            ]
            distribution = sorted(numbers, reverse=True)
            confidence = answer.get("confidence")
            usage = result.get("usage")
            usage = (
                {
                    key: usage[key]
                    for key in ("input_tokens", "output_tokens")
                    if type(usage.get(key)) is int and usage[key] >= 0
                }
                if isinstance(usage, dict)
                else {}
            )
            diagnostic.update(
                resolved_model=result.get("model"),
                choice=choice if choice in menu.criteria else "unknown",
                confidence=confidence if type(confidence) in {int, float} and math.isfinite(confidence) else None,
                top1=distribution[0] if distribution else None,
                probability_margin=distribution[0] - distribution[1] if len(distribution) > 1 else None,
                probability_sum=sum(numbers),
                operation=menu.steps.get(choice, {}).get("op"),
                usage=usage,
            )
            # Rejected responses retain diagnostics; acceptance validation must run AFTER this event.
            browser_agent_log_info("[BROWSER_POLICY_RESPONSE] %s", json.dumps(diagnostic))
            choice = validate_choice(answer, menu.criteria, self.decision_config.min_confidence)
            if self.decision_config.mode == "shadow":
                diagnostic["reason"] = "shadow"
                return None, diagnostic, observation.deadline_at
            if choice in {"HANDOFF", "FINISH"}:
                raise DecisionUnavailable("finish_to_llm" if choice == "FINISH" else "handoff_to_llm")
            step = menu.steps[choice]
            if time.time() >= observation.deadline_at:
                raise DecisionUnavailable("decision_deadline")
            page_action = step["op"] in {"navigate", "navigate_back", "scroll"}
            tool_name = "browser_page_action" if page_action else "browser_batch_interact"
            arguments = (
                {"generation_id": observation.page["generation_id"], **step}
                if page_action
                else {"generation_id": observation.page["generation_id"], "steps": [step]}
            )
            target = next((c for c in observation.controls if c["target_id"] == step.get("target_id")), None)
            self._guards[decision_id] = DecisionGuard(
                observation.session_id,
                observation.page["page_id"],
                observation.page["generation_id"],
                observation.page["url"],
                step.get("target_id", ""),
                canonical_arguments(arguments),
                target["decision_state"]["node_guard"] if target else observation.page["page_guard"],
                observation.task_key,
                observation.deadline_at,
                tool_name=tool_name,
            )
            while len(self._guards) > 16:
                self._guards.pop(next(iter(self._guards)))
            task.last_action = canonical_arguments(step)
            task.pending = {
                "decision_id": decision_id,
                "operation": step["op"],
                "state": fingerprint,
                "status": "compiled",
                "capture_id": observation.state.get("capture_id"),
            }
            self._count(task, "adopted")
            diagnostic.update(route="jev", model_source="jev", reason="compiled_action", operation=step["op"])
            usage_metadata = None
            if all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens")):
                usage_metadata = UsageMetadata(
                    model_name=result["model"],
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    total_tokens=usage["input_tokens"] + usage["output_tokens"],
                )
            return (
                AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=decision_id,
                            type="function",
                            name=tool_name,
                            arguments=json.dumps(arguments, ensure_ascii=False),
                            index=0,
                        )
                    ],
                    finish_reason="tool_calls",
                    response_model=result["model"],
                    usage_metadata=usage_metadata,
                    metadata={"browser_policy": diagnostic},
                ),
                diagnostic,
                observation.deadline_at,
            )
        except asyncio.CancelledError:
            diagnostic["reason"] = "cancelled"
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, DecisionUnavailable) else "policy_error"
            if task.fallback_scope not in {"task", "finish"}:
                hard = (
                    reason.startswith("invalid_jev")
                    or reason.startswith("invalid_choice")
                    or reason
                    in {
                        "missing_jev_key",
                        "jev_http_401",
                        "jev_http_402",
                        "jev_http_403",
                        "jev_http_400",
                        "jev_http_404",
                        "jev_model_mismatch",
                        "decision_budget_exhausted",
                        "policy_error",
                        "decision_deadline",
                    }
                )
                task.fallback_scope = "finish" if reason == "finish_to_llm" else "task" if hard else "segment"
                task.fallback_reason = reason
                task.blocked_state = fingerprint
            diagnostic.update(reason=reason, fallback_scope=task.fallback_scope)
            self._count(task, "llm_fallbacks")
            if diagnostic["cached_fallback"]:
                self._count(task, "cached_fallbacks")
            return None, diagnostic, observation.deadline_at
        finally:
            diagnostic["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
            self._persist(observation.task_key)
            marker = "[BROWSER_POLICY_SHADOW] %s" if self.decision_config.mode == "shadow" else "[BROWSER_POLICY] %s"
            browser_agent_log_info(marker, json.dumps(diagnostic, ensure_ascii=True, default=str))

    def record_execution(self, inputs: Any, session: Any, outcome: dict[str, Any]) -> None:
        call_id = str(getattr(getattr(inputs, "tool_call", None), "id", "") or "")
        if session is None:
            return
        phase = session.get_state(_PHASE_KEY)
        if not isinstance(phase, dict):
            return
        task = self._bind_task(session, phase)
        if not call_id.startswith("jev_"):
            if outcome.get("success") and not outcome.get("denied"):
                self._bind_llm_search(inputs, task)
                self._persist(self._task_key(session, phase))
            return
        self._guards.pop(call_id, None)  # Denied/non-executed calls consume their capability too.
        if any(receipt["decision_id"] == call_id for receipt in task.receipts):
            return
        success = bool(outcome.get("success")) and not outcome.get("denied")
        receipt = {
            "decision_id": call_id,
            "success": success,
            "denied": bool(outcome.get("denied")),
            "executed": outcome.get("executed"),
            "operation": task.pending.get("operation"),
            "postcondition": "awaiting_observation" if success else "llm_reconciliation_required",
        }
        task.receipts.append(receipt)
        task.receipts = task.receipts[-40:]
        task.pending["status"] = "executed" if success else "failed"
        self._count(task, "executions_ok" if success else "executions_failed")
        if not success and task.fallback_scope not in {"task", "finish"}:
            task.fallback_scope, task.fallback_reason = "segment", "runtime_recovery_required"
            task.blocked_state = task.pending.get("state", "")
        self._persist(self._task_key(session, phase))
        browser_agent_log_info(
            "[BROWSER_POLICY_EXECUTION] %s",
            json.dumps(
                {
                    **receipt,
                    "task_id": self._task_key(session, phase),
                    "model_source": "jev",
                }
            ),
        )

    def _bind_llm_search(self, inputs: Any, task: _TaskPolicy) -> None:
        """A successful LLM fill may bind this exact observed search node for later submit."""
        args = getattr(inputs, "tool_args", {})
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            return
        if not isinstance(args, dict):
            return
        name = str(getattr(inputs, "tool_name", ""))
        steps = (
            args.get("steps", [])
            if name.endswith("browser_batch_interact")
            else ([{**args, "op": "fill", "value": args.get("text")}] if name.endswith("browser_type") else [])
        )
        page = self.runtime._ensure_page_state()
        for step in steps:
            if not isinstance(step, dict) or step.get("op") not in {"fill", "type"}:
                continue
            value = step.get("value")
            if not isinstance(value, str) or not 0 < len(value) <= 200:
                continue
            for control in page.export_decision_targets():
                target = page.get_target(control["target_id"])
                details = control.get("decision_state") or {}
                guard = details.get("node_guard") or {}
                locator = step.get("target") or step.get("selector") or step.get("ref")
                matched = step.get("target_id") == control["target_id"] or (
                    bool(locator) and locator in {target.selector, target.ref}
                )
                if not matched or not details.get("search_like") or details.get("sensitive") or not guard:
                    continue
                binding = {"document": guard.get("document"), "node": guard.get("node"), "value": value}
                if binding not in task.search_bindings:
                    task.search_bindings = [*task.search_bindings[-15:], binding]
                    self._count(task, "llm_search_bindings")

    async def invoke(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> AssistantMessage:
        decision, diagnostic, deadline = await self._choose(messages, tools)
        if decision is not None:
            return decision
        remaining = self._remaining(deadline)
        async with asyncio.timeout(remaining):
            result = await self.fallback.invoke(messages=self._llm_messages(messages), tools=tools, **kwargs)
        return result.model_copy(update={"metadata": {**result.metadata, "browser_policy": diagnostic}})

    async def stream(self, messages: Any, *, tools: Any = None, **kwargs: Any):
        decision, diagnostic, deadline = await self._choose(messages, tools)
        if decision is not None:
            yield AssistantMessageChunk(**decision.model_dump())
            return
        first = True
        self._remaining(deadline)
        stream = self.fallback.stream(messages=self._llm_messages(messages), tools=tools, **kwargs)
        try:
            while True:
                try:
                    # Exit the timeout before yielding, and keep the producer on
                    # this task so its ContextVar tokens retain their owner.
                    async with asyncio.timeout(self._remaining(deadline)):
                        chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                if first:
                    chunk = chunk.model_copy(update={"metadata": {**chunk.metadata, "browser_policy": diagnostic}})
                    first = False
                yield chunk
        finally:
            if callable(getattr(stream, "aclose", None)):
                await stream.aclose()

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("browser task deadline exhausted before LLM fallback")
        return remaining

    def check_tool_call_binding(self, inputs: Any, session: Any) -> None:
        call_id = str(getattr(getattr(inputs, "tool_call", None), "id", "") or "")
        if not call_id.startswith("jev_"):
            return
        guard = self._guards.get(call_id)
        if guard is None:
            raise ValueError("browser_policy_consumed_or_unknown_decision")
        validate_binding(self.runtime, guard, inputs, session)

    async def validate_tool_call(self, inputs: Any, session: Any, *, actual_arguments: Any = None) -> None:
        call_id = str(getattr(getattr(inputs, "tool_call", None), "id", "") or "")
        if not call_id.startswith("jev_"):
            return
        guard = self._guards.pop(call_id, None)
        if guard is None:
            raise ValueError("browser_policy_consumed_or_unknown_decision")
        try:
            remaining = self._remaining(guard.deadline_at)
            if actual_arguments is not None and canonical_arguments(actual_arguments) != guard.arguments:
                raise ValueError("browser_policy_execution_arguments_changed")
            browser_agent_log_info(
                "[BROWSER_POLICY_DISPATCH] %s",
                json.dumps(
                    {
                        "decision_id": call_id,
                        "task_id": guard.task_key,
                        "stage": "validating",
                    }
                ),
            )
            await asyncio.wait_for(
                validate_guard(self.runtime, guard, inputs, session),
                timeout=min(remaining, self.decision_config.request_timeout_ms / 1000),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task = self._task(guard.task_key)
            task.fallback_reason, task.fallback_scope = "runtime_recovery_required", "segment"
            task.blocked_state = task.pending.get("state", "")
            self._persist(guard.task_key)
            raise ValueError("browser_policy_target_changed; use a fresh observation and the original LLM") from exc

    async def release_task_resources(self) -> None:
        pending = list(self._shadow_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._shadow_tasks.clear()
        self._observations.clear()
        self._guards.clear()
        await self.jev.aclose()
