# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional progress feedback at complete ReAct boundaries."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.runtime_monitor.evaluator import JevRuntimeEvaluator
from openjiuwen.core.single_agent.runtime_monitor.memory import RuntimeMemory, fingerprint
from openjiuwen.core.single_agent.runtime_monitor.schema import (
    AcceptanceCriterion, Advice, EvidenceAdapter, InterventionReview, Observation, ReviewInput,
    RuntimeEvaluator, RuntimeGoal, RuntimeMonitorConfig, StepAssessment, StepRecord, ToolEvidence,
)

RUNTIME_MONITOR_CONTEXT_KEY = "_runtime_monitor_run"


class RuntimeMonitor:
    """Configure JEV by injecting a dedicated client, or replace it with an evaluator.

    State lives in the supplied Session, not in a process-global registry. The
    host owns evaluator clients and their shutdown. No live requests are made
    until a completed tool step reaches the next model decision boundary.
    """

    def __init__(self, *, jev_client=None, evaluator: RuntimeEvaluator | None = None,
                 config: RuntimeMonitorConfig | None = None, evidence_adapter: EvidenceAdapter | None = None,
                 review_evaluator: RuntimeEvaluator | None = None, fallback_evaluator: RuntimeEvaluator | None = None,
                 scope_id: str | None = None,
                 on_update: Callable[[Any, RuntimeMemory], Awaitable[None]] | None = None):
        self.config = config or RuntimeMonitorConfig()
        if evaluator is None:
            if jev_client is None:
                raise ValueError("Supply a JEV client (the default backend) or a RuntimeEvaluator")
            evaluator = JevRuntimeEvaluator(jev_client, timeout=self.config.assessment_timeout)
        self.evaluator = evaluator
        self.review_evaluator = review_evaluator or evaluator
        self.fallback_evaluator = fallback_evaluator
        self.evidence_adapter = evidence_adapter
        self.on_update = on_update
        self.state_key = "runtime_monitor_" + (fingerprint(scope_id)[:32] if scope_id else uuid.uuid4().hex)

    def get_memory(self, session) -> RuntimeMemory | None:
        """Return a detached snapshot suitable for a host's diagnostics UI."""
        value = session.get_state(self.state_key)
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Unsupported runtime memory envelope")
        return RuntimeMemory.model_validate_json(value["payload"])

    async def publish(self, session) -> None:
        """Deliver a detached snapshot to the host, with bounded notification time."""
        if self.on_update is None:
            return
        try:
            memory = self.get_memory(session)
            if memory is not None:
                async with asyncio.timeout(self.config.notification_timeout):
                    await self.on_update(session, memory)
        except Exception as exc:
            logger.warning("Runtime monitor notification failed: %s", type(exc).__name__)

    def _write(self, session, memory: RuntimeMemory) -> None:
        # Session.update_state recursively merges dicts and expands dotted keys.
        # A JSON payload preserves literal evidence IDs and actually removes old keys.
        session.update_state({self.state_key: {
            "schema_version": 1, "generation": memory.generation, "revision": memory.revision,
            "payload": memory.model_dump_json(),
        }})

    def start_task(self, session, goal: str, *, task_id: str | None = None, goal_version: int = 1,
                   criteria: dict[str, AcceptanceCriterion] | None = None) -> RuntimeMemory:
        """Explicit tasks persist across invokes until the host starts another task."""
        memory = RuntimeMemory(
            goal=RuntimeGoal(task_id=task_id or uuid.uuid4().hex, text=goal, version=goal_version,
                             criteria=criteria or {}, explicit=True),
            rubric_version=self.config.rubric_version,
        )
        self._write(session, memory)
        return memory.model_copy(deep=True)

    def bind(self, session, goal: str, *, resume: bool = False, task_id: str | None = None):
        memory = self.get_memory(session)
        if memory is None or (not resume and not memory.goal.explicit):
            memory = RuntimeMemory(
                goal=RuntimeGoal(task_id=task_id or uuid.uuid4().hex, text=goal), rubric_version=self.config.rubric_version,
            )
        elif memory.rubric_version != self.config.rubric_version:
            memory = RuntimeMemory(goal=memory.goal, rubric_version=self.config.rubric_version)
        self._write(session, memory)
        return _RuntimeRun(self, session, memory)


class _RuntimeRun:
    """Invocation-local writer. Check session identity/revision after every await."""

    def __init__(self, monitor: RuntimeMonitor, session, memory: RuntimeMemory):
        self.monitor = monitor
        self.session = session
        self.memory = memory
        self.config = monitor.config
        self.request_advice_id: str | None = None
        self.request_id: str | None = None

    def _current(self) -> bool:
        stored = self.session.get_state(self.monitor.state_key)
        return isinstance(stored, dict) and stored.get("generation") == self.memory.generation and \
            stored.get("revision") == self.memory.revision

    def _save(self) -> bool:
        if not self._current():
            return False
        self.memory.prune(self.config)
        self.memory.revision += 1
        self.monitor._write(self.session, self.memory)
        return True

    def steer(self, messages: list[str]) -> None:
        if not messages:
            return
        if not self._current():
            return
        # User constraints invalidate old advice and scores, retaining recorded facts.
        for advice in self.memory.advice:
            if advice.state in {"created", "delivered", "observing"}:
                advice.state = "expired"
        for step in self.memory.steps:
            step.eligible = False
        self.memory.low_gain_streak = self.memory.unknown_streak = 0
        self.memory.evaluated_steps = self.memory.episode_interventions = 0
        self.memory.conflicts.clear()
        self.memory.stalled = False
        self.memory.goal.version += 1
        self.memory.goal.text = (self.memory.goal.text + "\nUser constraints:\n" + "\n".join(messages))[-8000:]
        # The goal changes locally, so write once against the previously checked revision.
        self.memory.revision += 1
        self.monitor._write(self.session, self.memory)

    def begin_step(self, message) -> None:
        if not self._current():
            return
        seq = self.memory.next_seq
        self.memory.next_seq += 1
        actions = {}
        for call in message.tool_calls or []:
            arguments = call.arguments
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    pass
            summary = arguments
            if isinstance(arguments, dict):
                summary = {key: "[redacted]" if any(secret in key.lower() for secret in
                           ("password", "secret", "token", "api_key", "authorization")) else value
                           for key, value in arguments.items()}
            actions[call.id] = {"name": str(call.name or ""), "arguments_hash": fingerprint(arguments),
                                "arguments": json.dumps(summary, ensure_ascii=False, default=str)[:512]}
        usage = message.usage_metadata
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump(mode="json")
        step = StepRecord(
            step_id=f"s{seq}", seq=seq, actions=actions,
            status="partial" if actions else "terminal", advice_id=self.request_advice_id,
            request_id=self.request_id, usage=usage if isinstance(usage, dict) else None,
        )
        self.memory.steps.append(step)
        self._save()

    def partial_result(self, tool_call, message) -> None:
        step = self.memory.pending_step()
        if step is None or tool_call.id not in step.actions:
            return
        if message is None:
            step.partial_results.pop(tool_call.id, None)
        else:
            step.partial_results[tool_call.id] = str(message.content)[:self.config.excerpt_chars]
        self._save()

    def tool_results(self, tool_calls, results, interrupted_ids: set[str]) -> None:
        step = self.memory.pending_step()
        if step is None or not self._current():
            return
        calls = {call.id: call for call in tool_calls}
        for index, (result, message) in enumerate(results):
            call_id = getattr(message, "tool_call_id", None)
            if call_id is None and index < len(tool_calls):
                call_id = tool_calls[index].id
            if call_id not in step.actions or call_id in interrupted_ids or call_id in step.completed_calls:
                continue
            if message is None:
                continue
            call = calls.get(call_id)
            try:
                if self.monitor.evidence_adapter:
                    evidence = self.monitor.evidence_adapter.extract(call, result, message)
                    evidence = ToolEvidence.model_validate(evidence)
                else:
                    evidence = ToolEvidence(observations=[Observation(text=str(message.content))])
            except Exception as exc:
                logger.warning("Runtime evidence adapter failed: %s", type(exc).__name__)
                evidence = ToolEvidence(observations=[Observation(text=str(message.content), complete=False)])
            for offset, supplied in enumerate(evidence.observations):
                if len(step.observations) >= self.config.max_step_observations:
                    step.input_complete = False
                    break
                observation = supplied.model_copy(deep=True)
                observation.obs_id = f"{step.step_id}:{call_id}:{offset}"
                observation.source = observation.source or f"tool:{call_id}"
                observation.observed_at = observation.observed_at or datetime.now(timezone.utc).isoformat()
                observation.fingerprint = fingerprint([
                    observation.text, observation.state_before, observation.state_after,
                    observation.subject, observation.property, observation.scope, observation.version, observation.value,
                ])
                if len(observation.text) > self.config.excerpt_chars:
                    observation.text = observation.text[:self.config.excerpt_chars]
                    observation.complete = False
                step.observations.append(observation)
            step.verifications.extend(evidence.verifications)
            if evidence.usage is not None:
                step.tool_usage[call_id] = evidence.usage
            step.completed_calls.append(call_id)
            step.partial_results.pop(call_id, None)
            if evidence.waiting:
                step.waiting_calls.append(call_id)
        if set(step.completed_calls) == set(step.actions):
            step.status = "closed"
            step.eligible = set(step.waiting_calls) != set(step.actions)
            self.memory.apply_verifications(step)
            step.repeat_key = fingerprint([
                sorted((a["name"], a["arguments_hash"]) for a in step.actions.values()),
                sorted(o.fingerprint for o in step.observations),
            ]) if step.input_complete and all(o.complete for o in step.observations) else ""
        self._save()

    async def _evaluate(self, evaluator, method: str, data, timeout: float):
        if len(data.model_dump_json()) > self.config.input_chars:
            raise ValueError("runtime_evaluation_input_too_large")
        async with asyncio.timeout(timeout):
            return await getattr(evaluator, method)(data)

    async def evaluate_pending(self) -> None:
        for step in self.memory.steps:
            if not step.eligible or step.assessment_started or step.status != "closed":
                continue
            step.assessment_started = True
            if not self._save():
                return
            data = self.memory.evaluation_input(step, self.config)
            fallback_used = False
            try:
                assessment = StepAssessment.model_validate(await self._evaluate(
                    self.monitor.evaluator, "assess_step", data, self.config.assessment_timeout))
            except Exception as exc:
                assessment = StepAssessment(error=type(exc).__name__, backend="rules")
                if self.monitor.fallback_evaluator:
                    fallback_used = True
                    try:
                        assessment = StepAssessment.model_validate(await self._evaluate(
                            self.monitor.fallback_evaluator, "assess_step", data, self.config.fallback_timeout))
                    except Exception as fallback_exc:
                        assessment.error = type(fallback_exc).__name__
            if not self._current():
                return
            self.memory.accept_assessment(data, assessment, self.config)
            signals = self.memory.signals(step, self.config)
            self._save()
            if not self.memory.may_review(signals, self.config):
                self._save()
                continue
            review_data = ReviewInput(
                goal=self.memory.goal, stats=self.memory.window(self.config), signals=signals,
                observations=[o for s in self.memory.steps[-self.config.window_size:] for o in s.observations],
                options=self.memory.options(), revision=self.memory.revision, rubric_version=self.config.rubric_version,
            ).model_copy(deep=True)
            while len(review_data.model_dump_json()) > self.config.input_chars and review_data.observations:
                review_data.observations.pop(0)
            try:
                review = InterventionReview.model_validate(await self._evaluate(
                    self.monitor.review_evaluator, "review_intervention", review_data, self.config.review_timeout))
            except Exception as exc:
                review = InterventionReview(error=type(exc).__name__, backend="rules")
            if (review.needed is None or review.option_id is None) and self.monitor.fallback_evaluator and not fallback_used:
                try:
                    review = InterventionReview.model_validate(await self._evaluate(
                        self.monitor.fallback_evaluator, "review_intervention", review_data, self.config.fallback_timeout))
                except Exception as exc:
                    review.error = type(exc).__name__
            if not self._current():
                return
            self.memory.last_review_at = self.memory.evaluated_steps
            step.review = review.model_dump(mode="json")
            options = {option.option_id: option for option in review_data.options}
            must_verify = "PREMISE_REFUTED" in signals
            if review.needed is False and not must_verify:
                self._save()
                continue
            selected = options.get(review.option_id) if review.needed is True else None
            degraded = selected is None
            if selected is None and signals == ["OFF_TRACK"] and not must_verify:
                self._save()
                continue
            selected = selected or options["verify"]
            previous = self.memory.active_advice()
            if previous:
                previous.state = "superseded"
            self.memory.advice.append(Advice(
                advice_id=uuid.uuid4().hex, option=selected, goal_version=self.memory.goal.version,
                created_seq=step.seq, memory_revision=self.memory.revision, trigger_codes=signals,
                degraded=degraded, state="observed_only" if self.config.mode == "observe" else "created",
            ))
            self.memory.episode_interventions += 1
            self._save()

    def model_hint(self) -> str | None:
        return self.memory.render_advice(self.config) if self._current() else None

    def request_dispatched(self, messages, request_id: str) -> None:
        self.request_advice_id = None
        self.request_id = request_id
        advice = self.memory.active_advice()
        if advice is None or not self._current() or self.config.mode != "active":
            return
        # Count actual provider dispatches, not context previews or rebuilds.
        if any(advice.advice_id in str(message.content) and "[runtime_intervention]" in str(message.content)
               for message in messages):
            self.request_advice_id = advice.advice_id
            if request_id not in advice.delivered_requests:
                advice.delivered_requests.append(request_id)
                advice.delivered_requests = advice.delivered_requests[-16:]
            advice.state = "delivered"
        else:
            advice.missed_requests += 1
            if advice.missed_requests >= 2:
                advice.state = "expired"
        self._save()

    def finish(self, *, interrupted: bool = False, aborted: bool = False, continuing: bool = False) -> None:
        if interrupted or not self._current():
            return
        pending = self.memory.pending_step()
        if pending:
            pending.status = "aborted"
            pending.eligible = False
        if aborted:
            for step in self.memory.steps:
                if step.eligible and step.assessment_started and step.assessment is None:
                    step.assessment = StepAssessment(backend="rules", error="evaluation_cancelled")
                    self.memory.low_gain_streak = 0
                    self.memory.unknown_streak += 1
        advice = self.memory.active_advice()
        if advice and (aborted or (not continuing and not self.memory.goal.explicit)):
            advice.state = "inconclusive"
        self._save()
