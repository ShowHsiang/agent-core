# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The agent-tier span every DeepAgent gets, and what other layers add to it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    TaskIterationInputs,
)
from openjiuwen.extensions.observability import span_context as shared_span_context
from openjiuwen.extensions.observability.semconv import (
    DA_AGENT_NAME,
    DA_TASK_ITERATION,
    LANGFUSE_OBSERVATION_INPUT,
    LANGFUSE_OBSERVATION_OUTPUT,
    LANGFUSE_OBSERVATION_TYPE,
)
from openjiuwen.harness.observability.rail import (
    AgentObservabilityRail,
    AgentSpanDecoration,
)


@pytest.fixture
def tracing():
    """Serve a real tracer over an in-memory exporter, with a run root open."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("agent-rail-test")

    shared_span_context.reset_state()
    root = tracer.start_span("run.root")
    shared_span_context.set_root_span(root)
    yield SimpleNamespace(exporter=exporter, tracer=tracer, root=root)
    if root.is_recording():
        root.end()
    shared_span_context.reset_state()


def _agent(name: str = "solo", *, enable_task_loop: bool = True):
    """Build the smallest agent stub the rail reads."""
    return SimpleNamespace(
        member_name=name,
        deep_config=SimpleNamespace(enable_task_loop=enable_task_loop),
    )


def _iteration_ctx(agent, *, iteration: int = 1, query: str = "do it"):
    """Build the callback context a task-loop round would pass to the rails."""
    inputs = TaskIterationInputs(iteration=iteration, query=query, loop_event=None)
    return AgentCallbackContext(agent=agent, inputs=inputs)


def _finished(exporter: InMemorySpanExporter, name: str):
    return [span for span in exporter.get_finished_spans() if span.name == name]


@pytest.mark.asyncio
async def test_iteration_span_opens_under_the_run_root_and_carries_generic_attributes(tracing):
    """The agent tier is team-agnostic: no ``agentteam.*`` unless a layer adds it."""
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    ctx = _iteration_ctx(_agent())

    await rail.before_task_iteration(ctx)
    ctx.inputs.result = "the answer"
    await rail.after_task_iteration(ctx)

    spans = _finished(tracing.exporter, "agent.solo.task_iteration.1")
    assert len(spans) == 1
    span = spans[0]
    assert span.parent.span_id == tracing.root.context.span_id
    assert span.attributes[LANGFUSE_OBSERVATION_TYPE] == "agent"
    assert span.attributes[DA_AGENT_NAME] == "solo"
    assert span.attributes[DA_TASK_ITERATION] == 1
    assert span.attributes[LANGFUSE_OBSERVATION_INPUT] == "do it"
    assert span.attributes[LANGFUSE_OBSERVATION_OUTPUT] == "the answer"
    assert not [key for key in span.attributes if key.startswith("agentteam.")]


@pytest.mark.asyncio
async def test_no_agent_span_without_a_run_root(tracing):
    """An orphan agent span would start a trace of its own — skip instead."""
    shared_span_context.reset_state()
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    ctx = _iteration_ctx(_agent())

    await rail.before_task_iteration(ctx)
    await rail.after_task_iteration(ctx)

    assert _finished(tracing.exporter, "agent.solo.task_iteration.1") == []


@pytest.mark.asyncio
async def test_a_contributed_decoration_is_applied_on_open_and_at_close(tracing):
    """This is how a layer extends the span without subclassing or re-opening it."""
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    ctx = _iteration_ctx(_agent())
    AgentSpanDecoration(
        attributes={"contrib.team.id": "research"},
        input_attribute_keys=("contrib.agent.input",),
        output_attribute_keys=("contrib.agent.output",),
    ).park(ctx)

    await rail.before_task_iteration(ctx)
    ctx.inputs.result = "the answer"
    await rail.after_task_iteration(ctx)

    span = _finished(tracing.exporter, "agent.solo.task_iteration.1")[0]
    assert span.attributes["contrib.team.id"] == "research"
    assert span.attributes["contrib.agent.input"] == "do it"
    assert span.attributes["contrib.agent.output"] == "the answer"


@pytest.mark.asyncio
async def test_a_decoration_never_leaks_into_another_agents_span(tracing):
    """Contributions are parked per callback context, not on a ContextVar."""
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    decorated = _iteration_ctx(_agent("decorated"))
    AgentSpanDecoration(attributes={"contrib.team.id": "research"}).park(decorated)
    await rail.before_task_iteration(decorated)
    await rail.after_task_iteration(decorated)

    plain = _iteration_ctx(_agent("plain"))
    await rail.before_task_iteration(plain)
    await rail.after_task_iteration(plain)

    span = _finished(tracing.exporter, "agent.plain.task_iteration.1")[0]
    assert "contrib.team.id" not in span.attributes


@pytest.mark.asyncio
async def test_single_round_agent_gets_an_invoke_span(tracing):
    """Sub-agents never fire iteration events; the invoke hook is their agent tier."""
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    agent = _agent("explore_agent", enable_task_loop=False)
    ctx = AgentCallbackContext(agent=agent, inputs=SimpleNamespace(query="look", result=None))

    await rail.before_invoke(ctx)
    ctx.inputs.result = "found it"
    await rail.after_invoke(ctx)

    spans = _finished(tracing.exporter, "agent.explore_agent.invoke")
    assert len(spans) == 1
    assert spans[0].attributes[LANGFUSE_OBSERVATION_OUTPUT] == "found it"


@pytest.mark.asyncio
async def test_multi_round_agent_gets_no_invoke_span(tracing):
    """One agent tier per round: the iteration hook owns the multi-round path."""
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    ctx = AgentCallbackContext(
        agent=_agent(enable_task_loop=True), inputs=SimpleNamespace(query="do it", result=None)
    )

    await rail.before_invoke(ctx)
    await rail.after_invoke(ctx)

    assert _finished(tracing.exporter, "agent.solo.invoke") == []


@pytest.mark.asyncio
async def test_subagent_invoke_nests_under_the_dispatching_agent_span(tracing):
    """Otherwise the sub-agent's whole run reads as if the parent had made the calls."""
    parent_rail = AgentObservabilityRail(tracer=tracing.tracer)
    parent_ctx = _iteration_ctx(_agent("leader"))
    await parent_rail.before_task_iteration(parent_ctx)
    parent_span = parent_ctx.extra["_otel_agent_scope"].span

    subagent_rail = AgentObservabilityRail(tracer=tracing.tracer)
    subagent_ctx = AgentCallbackContext(
        agent=_agent("explore_agent", enable_task_loop=False),
        inputs=SimpleNamespace(query="look", result=None),
    )
    await subagent_rail.before_invoke(subagent_ctx)
    await subagent_rail.after_invoke(subagent_ctx)
    await parent_rail.after_task_iteration(parent_ctx)

    subagent_span = _finished(tracing.exporter, "agent.explore_agent.invoke")[0]
    assert subagent_span.parent.span_id == parent_span.context.span_id


@pytest.mark.asyncio
async def test_an_orphan_span_from_the_same_agent_is_drained_not_left_open(tracing):
    """A round that never closed would otherwise swallow the next round's children."""
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    first = _iteration_ctx(_agent(), iteration=1)
    await rail.before_task_iteration(first)
    orphan = first.extra["_otel_agent_scope"].span

    second = _iteration_ctx(_agent(), iteration=2)
    await rail.before_task_iteration(second)
    await rail.after_task_iteration(second)

    assert not orphan.is_recording()
    assert _finished(tracing.exporter, "agent.solo.task_iteration.2")[0].parent.span_id == (
        tracing.root.context.span_id
    )


@pytest.mark.asyncio
async def test_another_agents_inherited_span_is_left_alone(tracing):
    """A ContextVar snapshot from another agent's task must not be ended here."""
    other_rail = AgentObservabilityRail(tracer=tracing.tracer)
    other_ctx = _iteration_ctx(_agent("teammate"))
    await other_rail.before_task_iteration(other_ctx)
    other_span = other_ctx.extra["_otel_agent_scope"].span

    rail = AgentObservabilityRail(tracer=tracing.tracer)
    ctx = _iteration_ctx(_agent("leader"))
    await rail.before_task_iteration(ctx)
    await rail.after_task_iteration(ctx)

    assert other_span.is_recording()


@pytest.mark.asyncio
async def test_the_run_root_is_ambient_again_after_a_round_closes(tracing):
    """Work that follows a round must not hang off the span that just ended."""
    from opentelemetry import trace as otel_trace

    rail = AgentObservabilityRail(tracer=tracing.tracer)
    ctx = _iteration_ctx(_agent())

    await rail.before_task_iteration(ctx)
    await rail.after_task_iteration(ctx)

    assert otel_trace.get_current_span() is tracing.root


@pytest.mark.asyncio
async def test_a_concurrent_session_of_the_same_agent_is_never_drained(tracing):
    """Overlapping runs share an agent name — only the trace tells them apart.

    A process serves several chats at once, so the same agent name is live in
    all of them, and an inherited ContextVar snapshot can put another session's
    span in front of this round. Ending it would leave that run's remaining
    llm/tool spans parentless and break its trace mid-run.
    """
    other_root = tracing.tracer.start_span("run.root.other")
    shared_span_context.set_root_span(other_root)
    other_rail = AgentObservabilityRail(tracer=tracing.tracer)
    other_ctx = _iteration_ctx(_agent("coder"))
    await other_rail.before_task_iteration(other_ctx)
    other_span = other_ctx.extra["_otel_agent_scope"].span

    # This session's round starts while the other session's span is the one
    # left in the inherited context.
    shared_span_context.set_root_span(tracing.root)
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    ctx = _iteration_ctx(_agent("coder"))
    await rail.before_task_iteration(ctx)
    mine = ctx.extra["_otel_agent_scope"].span
    await rail.after_task_iteration(ctx)

    assert other_span.is_recording(), "a concurrent session's span was ended"
    assert mine.context.trace_id == tracing.root.context.trace_id
    assert mine.parent.span_id == tracing.root.context.span_id
    other_span.end()
    other_root.end()


@pytest.mark.asyncio
async def test_an_own_orphan_in_the_same_run_is_still_drained(tracing):
    """The orphan sweep must keep working inside one run — that is its job."""
    rail = AgentObservabilityRail(tracer=tracing.tracer)
    first = _iteration_ctx(_agent("coder"), iteration=1)
    await rail.before_task_iteration(first)
    orphan = first.extra["_otel_agent_scope"].span

    second = _iteration_ctx(_agent("coder"), iteration=2)
    await rail.before_task_iteration(second)
    await rail.after_task_iteration(second)

    assert not orphan.is_recording()


@pytest.mark.asyncio
async def test_subagent_invoke_nests_under_the_tool_span_that_dispatched_it(tracing):
    """A task-tool sub-agent belongs *inside* the tool call that launched it.

    Parenting it to the dispatching agent instead leaves the ``task_tool`` span
    empty and the sub-agent's work sitting beside it, which is what made a
    dispatched run read as flat rather than layered.
    """
    from opentelemetry.trace import set_span_in_context

    parent_rail = AgentObservabilityRail(tracer=tracing.tracer)
    parent_ctx = _iteration_ctx(_agent("leader"))
    await parent_rail.before_task_iteration(parent_ctx)
    parent_span = parent_ctx.extra["_otel_agent_scope"].span

    tool_span = tracing.tracer.start_span(
        "tool.task_tool",
        context=set_span_in_context(parent_span),
    )
    shared_span_context.push_tool_span("task_tool", tool_span)

    subagent_rail = AgentObservabilityRail(tracer=tracing.tracer)
    subagent_ctx = AgentCallbackContext(
        agent=_agent("explore_agent", enable_task_loop=False),
        inputs=SimpleNamespace(query="look", result=None),
    )
    await subagent_rail.before_invoke(subagent_ctx)
    await subagent_rail.after_invoke(subagent_ctx)
    tool_span.end()
    await parent_rail.after_task_iteration(parent_ctx)

    subagent_span = _finished(tracing.exporter, "agent.explore_agent.invoke")[0]
    assert subagent_span.parent.span_id == tool_span.context.span_id
