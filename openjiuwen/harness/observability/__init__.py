# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public API for the single-agent observability subsystem.

The non-team counterpart of :mod:`openjiuwen.agent_teams.observability`: both
sit on the shared runtime in :mod:`openjiuwen.extensions.observability` and add
what their own runtime needs. Team mode adds the team monitor and the
``team.<name>`` root; a single agent adds a run root span, the session-keyed
fallback that keeps it reachable from supervisor tasks, and the agent-tier rail
wiring for the agent and every sub-agent it dispatches.

Quickstart::

    from openjiuwen.harness.observability import (
        acquire_observability,
        close_agent_run_span,
        install_subagent_observability_hook,
        mark_single_agent_team,
        open_agent_run_span,
    )

    install_subagent_observability_hook()          # once per process
    acquire_observability(ObservabilityConfig(endpoint="http://localhost:4317"))
    mark_single_agent_team(agent)
    handle = open_agent_run_span(session_id=session_id, mode="agent.fast")
    try:
        ...  # Runner.run_agent_streaming / Runner.run_agent
    finally:
        close_agent_run_span(handle, session_id=session_id, output=answer)

Provider caveat: OpenTelemetry allows exactly ONE global ``TracerProvider`` per
process. In a process where both Team and single-agent observability are
enabled, whichever initializes first wins and the other reuses it (its
exporter/endpoint/service_name are ignored). Demands are coordinated in
:mod:`openjiuwen.extensions.observability.demand`, so releasing one runtime
never tears down a provider the other still needs.
"""

from openjiuwen.harness.observability.constants import SINGLE_AGENT_TEAM_NAME
from openjiuwen.harness.observability.rail import (
    apply_single_agent_team_attr_suppression,
    attach_subagent_observability,
    install_subagent_observability_hook,
    mark_single_agent_team,
)
from openjiuwen.harness.observability.run_span import (
    build_run_span_name,
    close_agent_run_span,
    open_agent_run_span,
)
from openjiuwen.harness.observability.setup import (
    acquire_observability,
    get_config,
    is_initialized,
    is_tracing_enabled,
    release_observability,
)
from openjiuwen.harness.observability.span_context import (
    install_root_span_fallback,
    resolve_run_root_span,
)

__all__ = [
    "SINGLE_AGENT_TEAM_NAME",
    "acquire_observability",
    "apply_single_agent_team_attr_suppression",
    "attach_subagent_observability",
    "build_run_span_name",
    "close_agent_run_span",
    "get_config",
    "install_root_span_fallback",
    "install_subagent_observability_hook",
    "is_initialized",
    "is_tracing_enabled",
    "mark_single_agent_team",
    "open_agent_run_span",
    "release_observability",
    "resolve_run_root_span",
]
