"""Observe the actual ReAct provider request, not a callback preview."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.context_engine import ContextWindow
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage
from openjiuwen.core.foundation.tool import ToolInfo
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent import AgentCard, ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.core.single_agent.runtime_monitor import (
    InterventionReview, RuntimeMonitor, RuntimeMonitorConfig, StepAssessment,
)
from tests.unit_tests.fixtures.mock_llm import MockLLMModel


class ProgressEvaluator:
    def __init__(self):
        self.inputs = []
        self.reviews = []

    async def assess_step(self, data):
        self.inputs.append(data)
        return StepAssessment(
            gains={o.obs_id: "NEW_EVIDENCE" if "verified source" in o.text else "NO_GAIN"
                   for o in data.step.observations}, backend="stub",
            followed_advice="YES" if data.advice else "UNKNOWN", addressed_target=True if data.advice else None,
        )

    async def review_intervention(self, data):
        self.reviews.append(data)
        return InterventionReview(needed=True, option_id="verify", backend="stub")


def response(call_id, name="search"):
    return AssistantMessage(content="", tool_calls=[
        ToolCall(id=call_id, type="function", name=name, arguments='{"query":"cause"}')
    ])


def wire(agent, model):
    messages = []
    context = MagicMock()
    context.get_messages.side_effect = lambda **kwargs: list(messages)
    context.add_messages = AsyncMock(side_effect=lambda msg: messages.append(msg))
    context.session_id.return_value = "monitor-context"

    async def window(**kwargs):
        return ContextWindow(system_messages=kwargs["system_messages"],
                             context_messages=list(messages), tools=kwargs.get("tools") or [])

    context.get_context_window = AsyncMock(side_effect=window)
    engine = MagicMock()
    engine.create_context = AsyncMock(return_value=context)
    engine.get_context.return_value = context
    engine.save_contexts = AsyncMock()
    agent.context_engine = engine
    agent._llm = model
    agent.ability_manager.list_tool_info = AsyncMock(return_value=[
        ToolInfo(name="search", description="search", parameters={}),
        ToolInfo(name="verify", description="verify", parameters={}),
    ])
    effects = []

    async def execute(**kwargs):
        results = []
        for call in kwargs["tool_call"]:
            text = "verified source for the cause" if call.name == "verify" else "nothing new"
            effects.append(call.name)
            message = ToolMessage(tool_call_id=call.id, content=text)
            if kwargs.get("on_tool_result"):
                kwargs["on_tool_result"](call, None)
                kwargs["on_tool_result"](call, message)
            results.append((text, message))
        return results

    agent.ability_manager.execute = execute
    return context, effects


def rig(*, enabled=True, max_iterations=8):
    agent = ReActAgent(AgentCard(id=uuid.uuid4().hex, name="runtime-integration"))
    agent.configure(ReActAgentConfig(model_name="mock", max_iterations=max_iterations))
    model = MockLLMModel()
    evaluator = ProgressEvaluator()
    monitor = RuntimeMonitor(evaluator=evaluator,
                             config=RuntimeMonitorConfig(warmup_steps=0, low_gain_steps=1))
    if enabled:
        agent.set_runtime_monitor(monitor)
    context, effects = wire(agent, model)
    session = Session(session_id=uuid.uuid4().hex, card=agent.card)
    return agent, model, monitor, evaluator, context, effects, session


def hints(model):
    return [message.content for request in model.call_history for message in request
            if "[runtime_intervention]" in str(message.content)]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["invoke", "model_stream", "stream"])
async def test_feedback_is_in_actual_model_request_and_progress_closes_it(entrypoint):
    agent, model, monitor, evaluator, _, effects, session = rig()
    model.set_responses([response("a"), response("b", "verify"), AssistantMessage(content="done")])
    if entrypoint == "stream":
        async with asyncio.timeout(5):
            chunks = [chunk async for chunk in agent.stream({"query": "Find the cause"}, session)]
        result = next(chunk.payload for chunk in chunks if chunk.type == "answer")
    else:
        result = await agent.invoke({"query": "Find the cause"}, session, _streaming=entrypoint == "model_stream")
    assert result["output"] == "done"
    assert effects == ["search", "verify"]
    assert len(evaluator.inputs) == 2
    assert len(evaluator.reviews) == 1
    memory = monitor.get_memory(session)
    advice = memory.advice[0]
    assert advice.state == "improved"
    assert advice.delivered_requests
    assert advice.advice_id in "\n".join(hints(model))
    assert not any("[runtime_intervention]" in str(m.content) for m in model.call_history[0])
    assert any(advice.advice_id in str(m.content) for m in model.call_history[1])
    assert "[runtime_intervention]" not in agent.prompt_builder.build(), "no mutation of the shared builder"


@pytest.mark.asyncio
async def test_disabled_monitor_preserves_requests_and_executor_signature():
    agent, model, monitor, _, _, _, session = rig(enabled=False)
    seen = []

    async def execute(ctx, tool_call, session, parallel_tool_calls):
        seen.extend(tool_call)
        return [("ok", ToolMessage(content="ok", tool_call_id=tool_call[0].id))]

    agent.ability_manager.execute = execute
    model.set_responses([response("a"), AssistantMessage(content="done")])
    assert (await agent.invoke({"query": "task"}, session))["output"] == "done"
    assert len(seen) == 1
    assert not hints(model)
    assert monitor.get_memory(session) is None


@pytest.mark.asyncio
async def test_no_tool_answer_and_iteration_limit_do_not_schedule_extra_evaluation():
    agent, model, monitor, evaluator, _, _, session = rig(max_iterations=1)
    model.set_responses([response("a")])
    result = await agent.invoke({"query": "task"}, session)
    assert result["termination_reason"] == "max_iterations"
    assert not evaluator.inputs and not evaluator.reviews
    assert monitor.get_memory(session).steps[0].gain is None
    agent, model, monitor, evaluator, _, _, session = rig()
    model.set_responses([AssistantMessage(content="done")])
    await agent.invoke({"query": "task"}, session)
    assert not evaluator.inputs
    assert monitor.get_memory(session).steps[0].status == "terminal"


@pytest.mark.asyncio
async def test_workflow_pause_resume_uses_the_same_step():
    from openjiuwen.core.workflow import WorkflowOutput, WorkflowExecutionState

    agent, model, monitor, evaluator, _, _, session = rig()
    model.set_responses([response("paused"), AssistantMessage(content="done")])
    count = 0

    async def execute(**kwargs):
        nonlocal count
        count += 1
        result = WorkflowOutput(state=WorkflowExecutionState.INPUT_REQUIRED, result=None) if count == 1 else "ok"
        return [(result, ToolMessage(content="waiting" if count == 1 else "nothing new", tool_call_id="paused"))]

    agent.ability_manager.execute = execute
    result = await agent.invoke({"query": "task"}, session)
    assert result["result_type"] == "interrupt"
    before = monitor.get_memory(session)
    assert before.steps[0].status == "partial"
    assert not evaluator.inputs
    result = await agent.invoke({"query": "continue"}, session)
    after = monitor.get_memory(session)
    assert result["result_type"] == "answer"
    assert after.goal.task_id == before.goal.task_id
    assert len(evaluator.inputs) == 1
    assert after.steps[0].completed_calls == ["paused"]
    assert len(after.steps[0].observations) == 1


@pytest.mark.asyncio
async def test_confirmation_rail_preserves_batch_and_executes_tool_once():
    from openjiuwen.core.runner import Runner
    from tests.unit_tests.agent.react_agent.interrupt.test_base import (
        assert_interrupt_result, confirm_interrupt, create_simple_agent,
    )

    await Runner.start()
    try:
        agent, session, read_tool, _, _ = await create_simple_agent(
            session_id_prefix="monitored_hitl_" + uuid.uuid4().hex, rail_tool_names=["read"],
        )
        evaluator = ProgressEvaluator()
        monitor = RuntimeMonitor(evaluator=evaluator, config=RuntimeMonitorConfig(warmup_steps=0, low_gain_steps=1))
        agent.set_runtime_monitor(monitor)
        model = MockLLMModel()
        model.set_responses([
            AssistantMessage(content="", tool_calls=[ToolCall(
                id="read-call", type="function", name="read", arguments='{"filepath":"report.txt"}',
            )]), AssistantMessage(content="done"),
        ])
        agent._llm = model
        first = await agent.invoke({"query": "Read the report"}, session)
        interrupt_ids, _ = assert_interrupt_result(first)
        before = monitor.get_memory(session)
        assert before.steps[0].status == "partial"
        assert read_tool.invoke_count == 0 and not evaluator.inputs

        second = await agent.invoke({"query": confirm_interrupt(interrupt_ids[0])}, session)
        after = monitor.get_memory(session)
        assert second["result_type"] == "answer"
        assert read_tool.invoke_count == 1
        assert len(evaluator.inputs) == 1
        assert after.goal.task_id == before.goal.task_id
        assert after.steps[0].completed_calls == ["read-call"]
        assert len(after.steps[0].observations) == 1
        assert hints(model)
    finally:
        await Runner.stop()


@pytest.mark.asyncio
async def test_force_finish_boundary_does_not_spend_on_supervision():
    agent, model, _, evaluator, _, _, session = rig()
    model.set_responses([response("a")])

    async def finish(ctx):
        ctx.request_force_finish({"result_type": "answer", "output": "stop"})

    await agent.register_callback(AgentCallbackEvent.AFTER_REACT_ITERATION, finish)
    result = await agent.invoke({"query": "task"}, session)
    assert result["output"] == "stop"
    assert not evaluator.inputs


@pytest.mark.asyncio
async def test_cancellation_retains_partial_facts_without_scoring_them():
    agent, model, monitor, evaluator, _, _, session = rig()
    model.set_responses([AssistantMessage(content="", tool_calls=response("a").tool_calls + response("b").tool_calls)])

    async def execute(**kwargs):
        first = kwargs["tool_call"][0]
        kwargs["on_tool_result"](first, ToolMessage(content="a really executed", tool_call_id="a"))
        raise asyncio.CancelledError()

    agent.ability_manager.execute = execute
    with pytest.raises(asyncio.CancelledError):
        await agent.invoke({"query": "task"}, session)
    memory = monitor.get_memory(session)
    assert memory.steps[0].status == "aborted"
    assert memory.steps[0].partial_results == {"a": "a really executed"}
    assert not evaluator.inputs


@pytest.mark.asyncio
async def test_factory_deep_agent_forwards_monitor_and_real_provider_hint(monkeypatch, tmp_path):
    from openjiuwen.harness import create_deep_agent

    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *a, **k: None)
    script = MockLLMModel()
    script.set_responses([response("a"), response("b", "verify"), AssistantMessage(content="done")])
    model = SimpleNamespace(model_client_config=None, model_config=None, invoke=script.invoke, stream=script.stream)
    deep = create_deep_agent(model=model, card=AgentCard(id=uuid.uuid4().hex, name="monitored-deep"),
                             rails=[], enable_task_loop=False, enable_sys_operation=False,
                             add_general_purpose_agent=False, workspace=str(tmp_path))
    evaluator = ProgressEvaluator()
    monitor = RuntimeMonitor(evaluator=evaluator, config=RuntimeMonitorConfig(warmup_steps=0, low_gain_steps=1))
    deep.set_runtime_monitor(monitor)
    wire(deep.react_agent, model)
    session = Session(session_id=uuid.uuid4().hex, card=deep.card)
    result = await deep.invoke({"query": "Find the cause"}, session)
    assert result["output"] == "done"
    assert hints(script)
    assert monitor.get_memory(session).advice[0].state == "improved"
    assert deep._create_react_agent()._runtime_monitor is monitor
    deep.set_runtime_monitor(None)
    assert deep.react_agent._runtime_monitor is None


@pytest.mark.asyncio
async def test_steering_arriving_during_assessment_expires_old_advice():
    agent, model, monitor, evaluator, _, _, session = rig()
    queue = asyncio.Queue()
    assess = evaluator.assess_step

    async def steering_assessment(data):
        await queue.put("New constraint: use only source B")
        return await assess(data)

    evaluator.assess_step = steering_assessment
    model.set_responses([response("a"), AssistantMessage(content="done")])
    await agent.invoke({"query": "task", "_steering_queue": queue}, session)
    final_messages = "\n".join(str(m.content) for m in model.call_history[-1])
    assert "New constraint: use only source B" in final_messages
    assert "[runtime_intervention]" not in final_messages
    assert monitor.get_memory(session).goal.version == 2


@pytest.mark.asyncio
async def test_host_receives_detached_snapshots_and_final_outcome():
    agent, model, monitor, _, _, _, session = rig()
    snapshots = []

    async def notify(observed_session, memory):
        assert observed_session is session
        snapshots.append(memory.model_copy(deep=True))
        memory.goal.text = "host mutation must not affect the agent"

    monitor.on_update = notify
    model.set_responses([response("a"), AssistantMessage(content="done")])
    await agent.invoke({"query": "task"}, session)
    assert any(s.active_advice() for s in snapshots)
    assert snapshots[-1].advice[0].state == "inconclusive"
    assert snapshots[-1].advice[0].delivered_requests
    assert monitor.get_memory(session).goal.text == "task"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_host_notification_failure_does_not_stop_main_loop(failure):
    agent, model, monitor, _, _, _, session = rig()
    monitor.config = monitor.config.model_copy(update={"notification_timeout": 0.005})

    async def notify(*_args):
        if failure == "error":
            raise OSError("observer unavailable")
        await asyncio.sleep(1)

    monitor.on_update = notify
    model.set_responses([response("a"), response("b", "verify"), AssistantMessage(content="done")])
    result = await agent.invoke({"query": "task"}, session)
    assert result["output"] == "done"
    assert monitor.get_memory(session).advice[0].state == "improved"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["invoke", "stream", "interaction_round"])
async def test_final_notification_cancellation_releases_deep_agent_lifecycle(entry, monkeypatch, tmp_path):
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.schema.interaction import RoundWorkItem

    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *a, **k: None)
    script = MockLLMModel()
    script.set_responses([response("a"), AssistantMessage(content="done")])
    model = SimpleNamespace(model_client_config=None, model_config=None, invoke=script.invoke, stream=script.stream)
    deep = create_deep_agent(model=model, card=AgentCard(id=uuid.uuid4().hex, name="cancel-observer"),
                             rails=[], enable_task_loop=False, enable_sys_operation=False,
                             add_general_purpose_agent=False, workspace=str(tmp_path))

    async def cancelled_final_notification(_session, memory):
        if memory.advice and memory.advice[-1].state == "inconclusive":
            raise asyncio.CancelledError()

    monitor = RuntimeMonitor(evaluator=ProgressEvaluator(), on_update=cancelled_final_notification,
                             config=RuntimeMonitorConfig(warmup_steps=0, low_gain_steps=1))
    deep.set_runtime_monitor(monitor)
    wire(deep.react_agent, model)
    session = Session(session_id=uuid.uuid4().hex, card=deep.card)
    try:
        with pytest.raises(asyncio.CancelledError):
            inputs = {"query": "Find the cause"}
            if entry == "invoke":
                await deep.invoke(inputs, session)
            elif entry == "stream":
                _ = [chunk async for chunk in deep.stream(inputs, session)]
            else:
                work = RoundWorkItem.user(request_id="request-1", inputs=inputs)
                await asyncio.wait_for(deep.run_one_round(work, "task-1", session), timeout=10)
        assert not deep.is_invoke_active
        assert monitor.get_memory(session).advice[-1].state == "inconclusive"
    finally:
        await deep._force_cleanup_controller()
        await deep._agent_callback_manager.clear()
        await deep.react_agent.agent_callback_manager.clear()


@pytest.mark.asyncio
async def test_final_notification_cancellation_still_closes_owned_react_session(monkeypatch):
    agent, model, monitor, _, _, _, session = rig()
    monkeypatch.setattr("openjiuwen.core.single_agent.agents.react_agent.create_agent_session",
                        lambda **kwargs: session)
    session.pre_run = AsyncMock()
    session.close_stream = AsyncMock()
    session.commit = AsyncMock()

    async def cancelled_final_notification(_session, memory):
        if memory.advice and memory.advice[-1].state == "inconclusive":
            raise asyncio.CancelledError()

    monitor.on_update = cancelled_final_notification
    model.set_responses([response("a"), AssistantMessage(content="done")])
    with pytest.raises(asyncio.CancelledError):
        await agent.invoke({"query": "task"})
    session.close_stream.assert_awaited_once()
    session.commit.assert_awaited_once()
    agent.context_engine.save_contexts.assert_awaited()
