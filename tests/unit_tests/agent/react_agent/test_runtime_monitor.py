"""Progress windows, evaluator boundaries, pause/resume and bounded advisory feedback."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage
from openjiuwen.core.foundation.llm.system_one import ChoiceAnswer, NoulAnswer, SystemOneResponse, SystemOneUsage
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.runtime_monitor import (
    AcceptanceCriterion, InterventionReview, JevRuntimeEvaluator, LlmRuntimeEvaluator, Observation,
    RuntimeMonitor, RuntimeMonitorConfig, StepAssessment, ToolEvidence, Verification,
)


class Evaluator:
    def __init__(self, labels=None):
        self.labels = list(labels or [])
        self.assessments = []
        self.reviews = []

    async def assess_step(self, data):
        self.assessments.append(data.model_copy(deep=True))
        label = self.labels.pop(0) if self.labels else "NO_GAIN"
        return StepAssessment(
            gains={o.obs_id: label for o in data.step.observations}, backend="stub",
            followed_advice="YES" if data.advice else "UNKNOWN", addressed_target=True if data.advice else None,
        )

    async def review_intervention(self, data):
        self.reviews.append(data.model_copy(deep=True))
        return InterventionReview(needed=True, option_id="verify", backend="stub")


def make_run(evaluator=None, **config):
    session = Session(session_id="monitor-unit-session")
    evaluator = evaluator or Evaluator()
    monitor = RuntimeMonitor(evaluator=evaluator, config=RuntimeMonitorConfig(**config))
    return monitor, monitor.bind(session, "Find and verify a cause"), evaluator, session


def call(call_id="a", name="search", arguments='{"q": "cause"}'):
    return ToolCall(id=call_id, type="function", name=name, arguments=arguments)


def close_step(run, text="no information", calls=None):
    calls = calls or [call()]
    run.begin_step(AssistantMessage(content="", tool_calls=calls))
    results = [(text, ToolMessage(tool_call_id=c.id, content=text)) for c in calls]
    run.tool_results(calls, results, set())
    return run.memory.steps[-1]


@pytest.mark.asyncio
async def test_default_window_triggers_only_after_warmup_and_three_low_steps():
    monitor, run, evaluator, session = make_run()
    for index in range(5):
        close_step(run, f"Unhelpful result {index}", [call(arguments=f'{{"q": "attempt {index}"}}')])
        await run.evaluate_pending()
        if index < 4:
            assert not evaluator.reviews
    assert len(evaluator.assessments) == 5
    assert len(evaluator.reviews) == 1
    assert evaluator.reviews[0].stats.low_gain_streak == 3
    assert run.memory.window(monitor.config).gain_mean == 0
    assert monitor.get_memory(session).active_advice().option.recommendation == "VERIFY"
    await run.evaluate_pending()
    assert len(evaluator.assessments) == 5, "replaying a closed batch must not call JEV twice"


@pytest.mark.asyncio
async def test_unknown_breaks_streak_and_does_not_become_zero():
    monitor, run, _, _ = make_run(Evaluator(["NO_GAIN", "UNKNOWN", "NO_GAIN"]), warmup_steps=0)
    for i in range(3):
        close_step(run, f"distinct result {i}")
        await run.evaluate_pending()
    stats = run.memory.window(monitor.config)
    assert stats.low_gain_streak == 1
    assert stats.valid_ratio == pytest.approx(2 / 3)
    assert run.memory.steps[1].gain is None
    assert stats.unknown_streak == 0


@pytest.mark.asyncio
async def test_parallel_batch_and_partial_resume_count_once():
    monitor, run, evaluator, session = make_run()
    calls = [call("a"), call("b")]
    run.begin_step(AssistantMessage(content="", tool_calls=calls))
    done = [("result a", ToolMessage(tool_call_id="a", content="result a"))]
    run.partial_result(calls[0], done[0][1])
    run.partial_result(calls[0], None)
    run.tool_results(calls, done, {"b"})
    await run.evaluate_pending()
    assert evaluator.assessments == []
    assert run.memory.steps[0].status == "partial"
    run.finish(interrupted=True)
    resumed = monitor.bind(session, "yes", resume=True)
    resumed.tool_results([calls[1]], [("b", ToolMessage(tool_call_id="b", content="result b"))], set())
    await resumed.evaluate_pending()
    resumed.tool_results(calls, done, set())
    await resumed.evaluate_pending()
    assert len(evaluator.assessments) == 1
    assert len(resumed.memory.steps) == 1
    assert resumed.memory.steps[0].completed_calls == ["a", "b"]


@pytest.mark.asyncio
async def test_gain_is_not_summed_over_parallel_tools_and_duplicates_get_no_credit():
    _, run, _, _ = make_run(Evaluator(["NEW_EVIDENCE", "NEW_EVIDENCE"]))
    first = close_step(run, "one useful fact", [call("a"), call("b")])
    await run.evaluate_pending()
    assert first.gain == 0.5
    second = close_step(run, "one useful fact")
    await run.evaluate_pending()
    assert second.gain is None, "an identical receipt cannot prove unchanged external state"
    assert not second.verified_progress


@pytest.mark.asyncio
async def test_incomplete_observation_cannot_establish_no_gain():
    _, run, _, _ = make_run(excerpt_chars=64)
    step = close_step(run, "x" * 1000)
    await run.evaluate_pending()
    assert step.gain is None
    assert not step.observations[0].complete


@pytest.mark.asyncio
async def test_trusted_repeat_is_zero_even_when_semantic_evaluation_fails():
    evaluator = Evaluator()
    evaluator.assess_step = AsyncMock(side_effect=TimeoutError())
    _, run, _, _ = make_run(evaluator)
    run.monitor.evidence_adapter = SimpleNamespace(extract=lambda *args: ToolEvidence(
        observations=[Observation(text="same versioned source", repeatable=True)],
    ))
    first = close_step(run)
    await run.evaluate_pending()
    assert first.gain is None
    second = close_step(run)
    await run.evaluate_pending()
    assert second.gain == 0
    assert second.assessment.error == "TimeoutError"


@pytest.mark.asyncio
async def test_gain_lower_bound_cannot_prove_low_gain_at_a_higher_threshold():
    _, run, evaluator, _ = make_run(Evaluator(["NEW_EVIDENCE"]), warmup_steps=0,
                                   low_gain_steps=1, low_gain_threshold=0.75, excerpt_chars=64)
    step = close_step(run, "source text " * 100)
    await run.evaluate_pending()
    assert step.gain == 0.5 and step.gain_is_lower_bound
    assert run.memory.low_gain_streak == 0
    assert not evaluator.reviews


@pytest.mark.asyncio
async def test_advice_reaches_request_and_result_is_observed():
    _, run, evaluator, _ = make_run(Evaluator(["NO_GAIN", "NEW_EVIDENCE"]), warmup_steps=0, low_gain_steps=1)
    close_step(run)
    await run.evaluate_pending()
    advice = run.memory.active_advice()
    hint = run.model_hint()
    assert advice.advice_id in hint
    run.request_dispatched([SimpleNamespace(content=hint)], "request-2")
    close_step(run, "new evidence for the cause")
    await run.evaluate_pending()
    assert advice.state == "improved"
    assert advice.followed == "YES"
    assert evaluator.assessments[-1].advice.advice_id == advice.advice_id


@pytest.mark.asyncio
async def test_observe_mode_keeps_suggestions_out_of_requests():
    _, run, _, _ = make_run(mode="observe", warmup_steps=0, low_gain_steps=1)
    close_step(run)
    await run.evaluate_pending()
    assert run.memory.advice[-1].state == "observed_only"
    assert run.model_hint() is None


@pytest.mark.asyncio
async def test_undelivered_advice_expires_without_claiming_adoption():
    _, run, _, _ = make_run(warmup_steps=0, low_gain_steps=1)
    close_step(run)
    await run.evaluate_pending()
    advice = run.memory.active_advice()
    run.request_dispatched([SimpleNamespace(content="ordinary prompt")], "r1")
    run.request_dispatched([SimpleNamespace(content="ordinary prompt")], "r2")
    assert advice.state == "expired"
    assert not advice.delivered_requests
    assert advice.followed == "UNKNOWN"


@pytest.mark.asyncio
async def test_stale_evaluation_does_not_overwrite_new_goal():
    entered, release = asyncio.Event(), asyncio.Event()
    evaluator = Evaluator()

    async def assess(data):
        entered.set()
        await release.wait()
        return StepAssessment(gains={o.obs_id: "NEW_EVIDENCE" for o in data.step.observations})

    evaluator.assess_step = assess
    monitor, run, _, session = make_run(evaluator)
    close_step(run)
    work = asyncio.create_task(run.evaluate_pending())
    await entered.wait()
    monitor.start_task(session, "Different goal", task_id="new-goal")
    release.set()
    await work
    assert monitor.get_memory(session).goal.task_id == "new-goal"
    assert monitor.get_memory(session).steps == []


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_cancellation_propagates():
    evaluator = Evaluator()

    async def wait_forever(_):
        await asyncio.Event().wait()

    evaluator.assess_step = wait_forever
    _, run, _, _ = make_run(evaluator, assessment_timeout=0.01)
    step = close_step(run)
    await run.evaluate_pending()
    assert step.gain is None
    assert step.assessment.error == "TimeoutError"
    _, run, _, _ = make_run(evaluator)
    close_step(run)
    pending = asyncio.create_task(run.evaluate_pending())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    run.finish(aborted=True)
    assert run.memory.steps[-1].assessment.error == "evaluation_cancelled"


@pytest.mark.asyncio
async def test_rules_degradation_and_fallback_are_bounded():
    evaluator = Evaluator()
    evaluator.assess_step = AsyncMock(side_effect=RuntimeError("offline"))
    evaluator.review_intervention = AsyncMock(side_effect=RuntimeError("offline"))
    fallback = Evaluator()
    fallback.assess_step = AsyncMock(side_effect=ValueError("invalid"))
    fallback.review_intervention = AsyncMock(side_effect=ValueError("invalid"))
    monitor, run, _, _ = make_run(evaluator, unknown_steps=1)
    monitor.fallback_evaluator = fallback
    close_step(run)
    await run.evaluate_pending()
    assert fallback.assess_step.await_count == 1
    assert fallback.review_intervention.await_count == 0
    assert run.memory.active_advice().degraded
    assert run.memory.active_advice().option.recommendation == "VERIFY"


class Verifier:
    def extract(self, tool_call, result, message):
        return ToolEvidence(observations=[Observation(text=message.content)], verifications=[
            Verification(criterion_id="c1", proof_ref="test-report", version="v2", passed=result != "failed")
        ])


@pytest.mark.asyncio
async def test_only_trusted_versioned_verification_counts_and_regression_triggers():
    monitor, _, evaluator, session = make_run()
    monitor.evidence_adapter = Verifier()
    monitor.start_task(session, "Pass tests", criteria={"c1": AcceptanceCriterion(description="suite", version="v2")})
    run = monitor.bind(session, "same task")
    first = close_step(run, "passed")
    await run.evaluate_pending()
    again = close_step(run, "passed")
    await run.evaluate_pending()
    assert first.gain == 1 and first.verified_progress == ["c1"]
    assert again.verified_progress == []
    failed = close_step(run, "failed")
    await run.evaluate_pending()
    assert failed.refuted == ["c1"]
    assert "PREMISE_REFUTED" in evaluator.reviews[-1].signals
    assert not run.memory.verified


@pytest.mark.asyncio
async def test_waiting_is_explicit_not_inferred_from_tool_name():
    monitor, run, evaluator, _ = make_run()
    monitor.evidence_adapter = SimpleNamespace(extract=lambda *args: ToolEvidence(waiting=True))
    step = close_step(run)
    await run.evaluate_pending()
    assert not step.eligible
    assert not evaluator.assessments
    monitor.evidence_adapter = None
    step = close_step(run, calls=[call(name="wait")])
    await run.evaluate_pending()
    assert step.eligible


@pytest.mark.asyncio
async def test_sessions_and_goal_versions_do_not_share_windows():
    monitor, run, _, session = make_run()
    close_step(run)
    await run.evaluate_pending()
    other_session = Session(session_id="another-session")
    other = monitor.bind(other_session, "Different")
    assert not other.memory.steps
    run.steer(["Use the new constraint"])
    assert run.memory.goal.version == 2
    assert run.memory.window(monitor.config).step_ids == []
    restored = monitor.bind(session, "resume", resume=True)
    assert restored.memory.goal.version == 2


def choice(options, winner):
    return ChoiceAnswer(type="choice", choice=winner, confidence=0.95,
                        probabilities={key: 1.0 if key == winner else 0.0 for key in options})


@pytest.mark.asyncio
async def test_default_jev_batches_questions_and_reviews_updated_window():
    requests = []

    async def system_one(state, questions, **kwargs):
        requests.append((state, questions))
        answers = {}
        for name, question in questions.items():
            if question.type == "choice":
                winner = "NO_GAIN" if name.startswith("gain:") else "verify"
                answers[name] = choice(question.criteria, winner)
            else:
                answers[name] = NoulAnswer(type="noul", noul=1.0 if name == "needs_intervention" else 0.0)
        return SystemOneResponse(model="jev-test", answers=answers,
                                 usage=SystemOneUsage(input_tokens=10, output_tokens=1))

    client = SimpleNamespace(max_retries=0, system_one=system_one)
    monitor = RuntimeMonitor(jev_client=client, config=RuntimeMonitorConfig(warmup_steps=0, low_gain_steps=1))
    assert isinstance(monitor.evaluator, JevRuntimeEvaluator)
    run = monitor.bind(Session(), "Find evidence")
    close_step(run)
    await run.evaluate_pending()
    assert len(requests) == 2
    assert set(requests[0][1]) == {"gain:s1:a:0", "off_track"}
    assert requests[1][0]["stats"]["low_gain_streak"] == 1
    assert run.memory.steps[0].assessment.usage["input_tokens"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [float("nan"), 1.5, True, "0.9"])
async def test_malformed_jev_probabilities_are_unknown(bad):
    answer = SimpleNamespace(type="choice", choice="NEW_EVIDENCE", confidence=0.9,
                             probabilities={"NEW_EVIDENCE": bad, "NO_GAIN": 0.1, "UNKNOWN": 0, "STATE_PROGRESS": 0},
                             model_dump=lambda: {})
    client = SimpleNamespace(max_retries=0, system_one=AsyncMock(return_value=SimpleNamespace(
        model="jev", answers={"gain:s1:a:0": answer}, usage=SystemOneUsage(input_tokens=1, output_tokens=1))))
    monitor = RuntimeMonitor(jev_client=client)
    run = monitor.bind(Session(), "goal")
    step = close_step(run)
    await run.evaluate_pending()
    assert step.gain is None


@pytest.mark.asyncio
async def test_llm_replacement_uses_same_contract_without_tools():
    model = SimpleNamespace(invoke=AsyncMock(return_value=AssistantMessage(
        content='{"gains":{"s1:a:0":"NEW_EVIDENCE"},"off_track":false}'
    )))
    monitor, run, _, _ = make_run(LlmRuntimeEvaluator(model))
    step = close_step(run, "evidence")
    await run.evaluate_pending()
    assert step.gain == 0.5
    assert step.assessment.backend == "llm"
    assert model.invoke.await_args.kwargs["tools"] is None
    assert step.assessment.diagnostics == {}


def test_jev_retrying_client_is_rejected_without_mutating_it():
    client = SimpleNamespace(max_retries=2)
    with pytest.raises(ValueError, match="max_retries=0"):
        RuntimeMonitor(jev_client=client)
    assert client.max_retries == 2


@pytest.mark.asyncio
async def test_no_improvement_stops_repeated_interventions():
    monitor, run, evaluator, _ = make_run(warmup_steps=0, low_gain_steps=1)
    for index in range(8):
        hint = run.model_hint()
        run.request_dispatched([SimpleNamespace(content=hint or "normal")], f"r{index}")
        close_step(run, f"unhelpful output {index}")
        await run.evaluate_pending()
    assert len(evaluator.reviews) == 2
    assert run.memory.stalled
    assert all(a.state == "no_observed_improvement" for a in run.memory.advice)
    assert run.memory.low_gain_streak == 8, "sending advice does not reset the streak"


@pytest.mark.asyncio
async def test_conflicts_deduplicate_and_different_versions_are_not_contradictions():
    evaluator = Evaluator()

    async def assess(data):
        return StepAssessment(gains={o.obs_id: "NO_GAIN" for o in data.step.observations},
                              relations={key: "CONTRADICTION" for key in data.pairs}, backend="stub")

    evaluator.assess_step = assess
    monitor, run, _, _ = make_run(evaluator, low_gain_steps=100, window_size=10, max_observations=80)
    version = "v1"
    monitor.evidence_adapter = SimpleNamespace(extract=lambda c, r, m: ToolEvidence(observations=[
        Observation(text=m.content, subject="service", property="status", scope="prod", version=version,
                    value=m.content, repeatable=True)
    ]))
    for text in ("up", "down", "up", "down"):
        close_step(run, text)
        await run.evaluate_pending()
    assert len(run.memory.conflicts) == 1
    assert run.memory.window(monitor.config).new_conflict_count == 1
    version = "v2"
    close_step(run, "maintenance")
    await run.evaluate_pending()
    assert len(run.memory.conflicts) == 1


@pytest.mark.asyncio
async def test_criterion_ids_are_literal_and_removed_state_does_not_survive_reload():
    monitor, run, _, session = make_run()
    monitor.start_task(session, "goal", criteria={"suite.v2": AcceptanceCriterion(description="current")})
    assert list(monitor.get_memory(session).goal.criteria) == ["suite.v2"]
    monitor.start_task(session, "new goal", criteria={})
    assert monitor.get_memory(session).goal.criteria == {}


@pytest.mark.asyncio
async def test_memory_retention_and_input_bounds_do_not_invent_scores():
    monitor, run, _, session = make_run(mode="observe", max_steps=5, input_chars=2000,
                                       low_gain_steps=100, repeat_threshold=100)
    for index in range(12):
        close_step(run, f"result {index} " + "x" * 600)
        await run.evaluate_pending()
    restored = monitor.get_memory(session)
    assert len(restored.steps) == 5
    assert restored.next_seq == 13
    assert len([o for step in restored.steps for o in step.observations]) <= monitor.config.max_observations


@pytest.mark.asyncio
async def test_stale_verification_and_unchanged_state_receive_no_verified_credit():
    monitor, _, _, session = make_run(Evaluator(["STATE_PROGRESS"]))
    monitor.start_task(session, "new release", criteria={"c1": AcceptanceCriterion(description="suite", version="v3")})
    monitor.evidence_adapter = SimpleNamespace(extract=lambda *args: ToolEvidence(
        observations=[Observation(text="same state", state_before="x", state_after="x")],
        verifications=[Verification(criterion_id="c1", proof_ref="old report", version="v2", passed=True)],
        usage={"duration_ms": 3},
    ))
    run = monitor.bind(session, "same task")
    step = close_step(run)
    await run.evaluate_pending()
    assert not step.verified_progress
    assert step.gain == 0
    assert step.tool_usage == {"a": {"duration_ms": 3}}
