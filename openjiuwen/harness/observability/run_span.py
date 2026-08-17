# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Root span around one single-agent run.

``OtelCallbackHandler`` skips LLM/tool span creation when no parent span exists
(see ``callback_handler._get_parent_context_for_llm_tool``). A single-agent run
sets neither a team span nor a current agent span, so without a root span zero
spans are produced even after a clean ``init_observability``. These helpers open
a root span and register it as the run's root — the same mechanism Team mode
uses internally via ``get_or_create_team_span`` — so LLM/tool spans nest under
it and are exported.

Usage (must be paired, in the same coroutine so the ContextVar propagates into
the runner's LLM calls)::

    handle = open_agent_run_span(session_id=sid, mode=mode)
    try:
        ...  # Runner.run_agent_streaming / Runner.run_agent
    finally:
        close_agent_run_span(handle, session_id=sid, output=answer)
"""

from __future__ import annotations

from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.harness.observability.span_context import (
    register_run_root_span,
    unregister_run_root_span,
)

# Tracer name the single-agent root spans are emitted under.
_RUN_TRACER_NAME = "openjiuwen.harness.observability"

# Span attribute carrying the host's request mode, so traces stay filterable
# without parsing the span name.
_RUN_MODE_ATTRIBUTE = "openjiuwen.agent.mode"


def build_run_span_name(*, mode: str, session_id: str) -> str:
    """Build a hierarchical OTel span name: ``agent.<mode>.<session_id>``.

    *mode* is the host's request mode, typically shaped ``<category>.<submode>``
    (e.g. ``agent.plan`` / ``agent.fast`` / ``code.normal``), so it yields the
    hierarchy directly::

        agent.plan  -> agent.agent.plan.<session_id>
        code.normal -> agent.code.normal.<session_id>

    Falls back gracefully when either component is empty.

    Args:
        mode: Request mode of the run; empty is allowed.
        session_id: Session the run belongs to; empty is allowed.

    Returns:
        The span name.
    """
    normalized_mode = (mode or "").strip()
    normalized_session = (session_id or "").strip()
    if not normalized_mode:
        return f"agent.run.{normalized_session}" if normalized_session else "agent.run"
    if not normalized_session:
        return f"agent.{normalized_mode}.run"
    return f"agent.{normalized_mode}.{normalized_session}"


def open_agent_run_span(*, session_id: str = "", mode: str = "") -> Any:
    """Open the root span of a single-agent run.

    Args:
        session_id: Session the run belongs to; also keys the fallback registry.
        mode: Request mode of the run, stamped on the span and used in its name.

    Returns:
        An opaque handle to pass to :func:`close_agent_run_span`, or ``None``
        when single-agent tracing is off (in which case closing is a no-op).
    """
    try:
        from opentelemetry.trace import SpanKind

        from openjiuwen.extensions.observability.semconv import LANGFUSE_SESSION_ID
        from openjiuwen.extensions.observability.setup import get_tracer, is_initialized
        from openjiuwen.extensions.observability.span_context import (
            set_current_session_id,
            set_root_span,
        )
        from openjiuwen.harness.observability.setup import is_tracing_enabled

        if not is_initialized():
            return None
        if not is_tracing_enabled():
            return None

        tracer = get_tracer(_RUN_TRACER_NAME)
        name = build_run_span_name(mode=mode, session_id=session_id)
        span = tracer.start_span(name=name, kind=SpanKind.SERVER)
        span.set_attribute(LANGFUSE_SESSION_ID, session_id or "")
        span.set_attribute(_RUN_MODE_ATTRIBUTE, mode or "")
        # Register as the run root so parent lookup finds it for LLM/tool span
        # creation. The session id goes into the shared registry as well as the
        # local fallback table — supervisor tasks may not inherit ContextVars.
        sid = session_id or ""
        set_root_span(span, session_id=sid)
        set_current_session_id(sid)
        register_run_root_span(span, session_id=sid)
        logger.info("[AgentObservability] root span opened: name=%s", name)
        return span
    except Exception as exc:
        logger.warning("[AgentObservability] open root span failed: %s", exc)
        return None


def stamp_run_output(handle: Any, output: str) -> None:
    """Write the run's final answer onto the root span as the trace output.

    Team mode fills the equivalent attribute on its ``team.<name>`` span from
    the leader's iteration result (``ObservabilityRail.after_task_iteration``),
    which keys off ``TeamRole.LEADER`` and therefore never fires for a single
    agent — leaving the trace with an empty top-level output. The single-agent
    counterpart is the run's final answer, stamped here.

    Redaction follows the active ``ObservabilityConfig`` so ``redact_completions``
    covers this attribute exactly as it covers llm/agent span outputs.

    Args:
        handle: The still-recording root span.
        output: Final answer text; empty means nothing to stamp.
    """
    if not output:
        return
    from openjiuwen.extensions.observability.redaction import redact_completion
    from openjiuwen.extensions.observability.semconv import LANGFUSE_OBSERVATION_OUTPUT
    from openjiuwen.extensions.observability.setup import get_config

    config = get_config()
    text = redact_completion(output, config) if config else output
    handle.set_attribute(LANGFUSE_OBSERVATION_OUTPUT, text)


def close_agent_run_span(handle: Any, *, session_id: str = "", output: str = "") -> None:
    """End the root span opened by :func:`open_agent_run_span` and clear it.

    Args:
        handle: Opaque handle from :func:`open_agent_run_span`; None is a no-op.
        session_id: Session the run belonged to; its registry entry is dropped.
        output: The run's final answer, stamped as the trace-level output.
            Empty (aborted / errored run) leaves the attribute unset.
    """
    # Drop this run's fallback entry — and only this run's. Sessions overlap,
    # so clearing whatever happens to be registered would blind a run that is
    # still going (its sub-agents would lose their spans mid-run).
    unregister_run_root_span(handle, session_id=session_id)
    if handle is None:
        return
    try:
        from openjiuwen.extensions.observability.span_context import (
            cascade_close_children,
            clear_root_span,
            flush_child_spans,
        )

        try:
            stamp_run_output(handle, output)
        except Exception as exc:
            logger.debug("[AgentObservability] stamp run output failed: %s", exc)

        # End any still-open child LLM/tool spans (e.g. run aborted mid-call).
        # Two nets are needed for the single-agent path:
        #   1. cascade_close_children — closes spans whose state was pushed on
        #      the _llm_span_stack / _tool_span_map ContextVars in THIS context.
        #   2. flush_child_spans — the SpanProcessor-backed safety net Team mode
        #      relies on (finalize_trace -> flush_child_spans via
        #      ActiveSpanTracker). The single-agent runner opens LLM spans inside
        #      its own child context, so their ContextVar state is not visible
        #      here; the tracker closes them by trace_id regardless of context.
        # Both must run BEFORE the root binding is cleared, and the flush is
        # scoped to our trace only (flush_spans_for_trace), so concurrent runs
        # are not affected.
        #
        # Ordering note — the root span is ended BETWEEN the two nets, not after
        # them: ``flush_spans_for_trace`` spares only spans whose name starts
        # with ``team.`` (Team mode's root), so our ``agent.<mode>.<sid>`` root
        # would otherwise be swept up as a leaked child — reported as an ORPHAN
        # warning, force-ended by the tracker, and then re-ended here ("Calling
        # end() on an ended span"). Ending it first makes it non-recording, which
        # the tracker skips, so the root keeps its own end time and status while
        # the net still catches genuinely leaked children. That is also why the
        # trace id is read up front and passed in explicitly: an ended root is no
        # longer resolvable from the ContextVar, so letting flush_child_spans
        # discover it would skip the flush entirely.
        trace_id = getattr(getattr(handle, "context", None), "trace_id", None)
        try:
            cascade_close_children()
        except Exception as exc:
            logger.debug("[AgentObservability] cascade_close_children failed: %s", exc)
        try:
            handle.end()
        except Exception as exc:
            logger.debug("[AgentObservability] end root span failed: %s", exc)
        try:
            flush_child_spans(trace_id=trace_id)
        except Exception as exc:
            logger.debug("[AgentObservability] flush_child_spans failed: %s", exc)
        try:
            clear_root_span(session_id=session_id or "", expected_span=handle)
        except Exception as exc:
            logger.debug("[AgentObservability] clear_root_span failed: %s", exc)
        clear_root_span()
    except Exception as exc:
        logger.warning("[AgentObservability] close root span failed: %s", exc)


__all__ = [
    "build_run_span_name",
    "close_agent_run_span",
    "open_agent_run_span",
    "stamp_run_output",
]
