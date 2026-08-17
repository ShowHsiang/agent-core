# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Root span opened around a single-agent run."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from openjiuwen.extensions.observability import setup as shared_setup
from openjiuwen.extensions.observability import span_context as shared_span_context
from openjiuwen.extensions.observability.semconv import (
    LANGFUSE_OBSERVATION_OUTPUT,
    LANGFUSE_SESSION_ID,
)
from openjiuwen.harness.observability import span_context as agent_span_context
from openjiuwen.harness.observability import setup as agent_setup
from openjiuwen.harness.observability.run_span import (
    build_run_span_name,
    close_agent_run_span,
    open_agent_run_span,
    stamp_run_output,
)


@pytest.fixture
def exporter(monkeypatch) -> InMemorySpanExporter:
    """Serve a real tracer over an in-memory exporter with tracing enabled."""
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(shared_setup, "is_initialized", lambda: True)
    monkeypatch.setattr(shared_setup, "get_tracer", provider.get_tracer)
    monkeypatch.setattr(shared_setup, "get_config", lambda: None)
    monkeypatch.setattr(agent_setup, "is_tracing_enabled", lambda: True)
    shared_span_context.reset_state()
    agent_span_context.reset_run_root_spans()
    yield memory
    shared_span_context.reset_state()
    agent_span_context.reset_run_root_spans()


def test_span_name_carries_the_mode_hierarchy_and_degrades_gracefully() -> None:
    assert build_run_span_name(mode="code.normal", session_id="s1") == "agent.code.normal.s1"
    assert build_run_span_name(mode="agent.plan", session_id="") == "agent.agent.plan.run"
    assert build_run_span_name(mode="", session_id="s1") == "agent.run.s1"
    assert build_run_span_name(mode="", session_id="") == "agent.run"


def test_open_registers_the_root_so_child_spans_find_a_parent(exporter) -> None:
    """Without a registered root, the callback handler creates no llm/tool span."""
    handle = open_agent_run_span(session_id="sess-A", mode="agent.fast")
    try:
        assert handle is not None
        assert shared_span_context.get_root_span(session_id="sess-A") is handle
        assert agent_span_context.resolve_run_root_span() is handle
    finally:
        close_agent_run_span(handle, session_id="sess-A")


def test_close_ends_the_span_stamps_the_output_and_clears_the_root(exporter) -> None:
    handle = open_agent_run_span(session_id="sess-A", mode="agent.fast")

    close_agent_run_span(handle, session_id="sess-A", output="final answer")

    finished = exporter.get_finished_spans()
    assert [span.name for span in finished] == ["agent.agent.fast.sess-A"]
    assert finished[0].attributes[LANGFUSE_SESSION_ID] == "sess-A"
    assert finished[0].attributes[LANGFUSE_OBSERVATION_OUTPUT] == "final answer"
    assert shared_span_context.get_root_span(session_id="sess-A") is None
    assert agent_span_context.resolve_run_root_span() is None


def test_no_span_is_opened_while_single_agent_tracing_is_off(exporter, monkeypatch) -> None:
    """The Team subsystem may hold the provider up while agent tracing is off."""
    monkeypatch.setattr(agent_setup, "is_tracing_enabled", lambda: False)

    handle = open_agent_run_span(session_id="sess-A", mode="agent.fast")

    assert handle is None
    close_agent_run_span(handle, session_id="sess-A")  # no-op, must not raise
    assert exporter.get_finished_spans() == ()


def test_an_aborted_run_leaves_the_output_attribute_unset() -> None:
    """Empty output means nothing to stamp — not an empty answer."""

    def _fail(key, value):
        raise AssertionError(f"must not stamp {key}={value}")

    stamp_run_output(SimpleNamespace(set_attribute=_fail), "")


def test_leaked_child_spans_are_flushed_against_the_run_trace(exporter, monkeypatch) -> None:
    """The safety net must still know which trace to sweep after the root ends.

    ``flush_child_spans`` resolves the trace from the root ContextVar when no
    trace id is given, and an ended root is no longer resolvable — so the flush
    would silently skip and a leaked llm/tool span would never be closed.
    """
    flushed: list[int | None] = []
    monkeypatch.setattr(
        shared_span_context,
        "flush_child_spans",
        lambda *, trace_id=None: flushed.append(trace_id),
    )
    handle = open_agent_run_span(session_id="sess-A", mode="agent.fast")
    expected_trace_id = handle.context.trace_id

    close_agent_run_span(handle, session_id="sess-A")

    assert flushed == [expected_trace_id]
