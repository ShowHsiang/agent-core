# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent-tier span wiring for single-agent runs.

``ObservabilityRail`` is what produces the ``agent.<type>.invoke`` /
``agent.<member>.task_iteration.<n>`` tier of the span tree. Team mode mounts it
through the team blueprint; a single agent has no blueprint doing that, and a
sub-agent it dispatches is built inside the SDK, so this module supplies the
equivalent wiring:

* :func:`mark_single_agent_team` — stamp the synthetic team marker the rail
  keys its agent-tier spans off.
* :func:`apply_single_agent_team_attr_suppression` — keep the redundant
  ``agentteam.*`` block off single-agent spans.
* :func:`attach_subagent_observability` /
  :func:`install_subagent_observability_hook` — give every dispatched
  sub-agent a rail of its own, whichever tool dispatched it.

The rail class itself still lives in :mod:`openjiuwen.agent_teams.observability`
alongside the Team monitor. It is imported lazily here so importing this module
never pulls the Team stack into a single-agent process.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.harness.observability.constants import SINGLE_AGENT_TEAM_NAME

# Idempotency marker so the rail patch is applied at most once per process.
_RAIL_TEAM_ATTR_PATCH_ATTR = "openjiuwen_single_agent_attr_patch"

# Private rail method this module rebinds via getattr/setattr.
_RAIL_STAMP_METHOD = "_stamp_agent_attributes"

# Attribute namespace carried by real team members only.
_TEAM_ATTR_PREFIX = "agentteam."

# Marker stamped on the ``create_subagent`` wrapper so a second install
# recognizes its own work and leaves it alone.
_SUBAGENT_HOOK_MARKER_ATTR = "openjiuwen_observability_hooked"


def mark_single_agent_team(agent: Any) -> None:
    """Stamp the synthetic team marker the observability rail keys off.

    ``ObservabilityRail.before_invoke`` returns early for an agent with no
    ``team_name``, and a single-round agent (``enable_task_loop=False``) gets
    its span from that hook alone — ``before_task_iteration`` never fires. A
    single agent has no team, so without this marker it produces **no
    agent-tier span at all**: its llm/tool spans and any sub-agent's
    ``agent.<type>.invoke`` span both attach straight to the run's root span,
    which is what flattens a task-tool sub-agent into the agent layer instead
    of nesting it under the dispatching agent.

    ``team_name`` is a plain attribute on DeepAgent. An agent that already
    carries one is a real team member and is left alone. Best-effort: tracing
    setup must never break a run.

    Args:
        agent: The DeepAgent instance about to run (main agent or sub-agent).
    """
    if agent is None:
        return
    if getattr(agent, "team_name", ""):
        return
    try:
        agent.team_name = SINGLE_AGENT_TEAM_NAME
    except Exception as exc:
        logger.debug("[AgentObservability] set team_name on agent failed: %s", exc)


def apply_single_agent_team_attr_suppression() -> None:
    """Drop the ``agentteam.*`` block from single-agent spans.

    The marker stamped by :func:`mark_single_agent_team` is a synthetic team,
    not a real one, so the team identity attributes it drags onto every span
    are noise. This patches ``ObservabilityRail._stamp_agent_attributes`` to
    rebind a single-agent span's ``set_attribute`` so any ``agentteam.*`` key
    (including the inline input/output stamped later on the same span) is
    discarded; real team members keep the original stamping.

    Best-effort and idempotent — a second call recognizes its own patch.
    """
    try:
        from openjiuwen.agent_teams.observability import rail as team_rail
        from openjiuwen.extensions.observability.semconv import (
            LANGFUSE_OBSERVATION_TYPE,
            LANGFUSE_SESSION_ID,
        )
    except Exception as exc:  # pragma: no cover - observability deps unavailable
        logger.debug("[AgentObservability] rail patch import failed: %s", exc)
        return

    rail_cls = team_rail.ObservabilityRail
    if getattr(rail_cls, _RAIL_TEAM_ATTR_PATCH_ATTR, False):
        return

    original_stamp = getattr(rail_cls, _RAIL_STAMP_METHOD)

    @staticmethod
    def _stamp_agent_attributes(
        span: Any,
        *,
        agent: Any,
        member_name: str,
        team_name: str,
        session_id: str,
        is_leader: bool,
    ) -> None:
        """Stamp agent attributes, minus the team block for a single agent."""
        if team_name != SINGLE_AGENT_TEAM_NAME:
            original_stamp(
                span,
                agent=agent,
                member_name=member_name,
                team_name=team_name,
                session_id=session_id,
                is_leader=is_leader,
            )
            return

        # Rebind this span's set_attribute to drop agentteam.* keys. The rail's
        # later inline input/output stamps hit the same span, so they are
        # caught too.
        try:
            original_set_attribute = span.set_attribute

            def _filter_attribute(key: Any, value: Any) -> None:
                """Forward every attribute except the team-only namespace."""
                if isinstance(key, str) and key.startswith(_TEAM_ATTR_PREFIX):
                    return
                original_set_attribute(key, value)

            span.set_attribute = _filter_attribute  # type: ignore[method-assign]
        except Exception as exc:
            logger.debug("[AgentObservability] set_attribute rebind failed: %s", exc)
            original_stamp(
                span,
                agent=agent,
                member_name=member_name,
                team_name=team_name,
                session_id=session_id,
                is_leader=is_leader,
            )
            return

        # Keep the two non-agentteam attrs; everything else the original would
        # set is agentteam.* and gets dropped by the filter above.
        span.set_attribute(LANGFUSE_OBSERVATION_TYPE, "agent")
        if session_id:
            span.set_attribute(LANGFUSE_SESSION_ID, session_id)

    setattr(rail_cls, _RAIL_STAMP_METHOD, _stamp_agent_attributes)
    setattr(rail_cls, _RAIL_TEAM_ATTR_PATCH_ATTR, True)


def attach_subagent_observability(subagent: Any) -> None:
    """Give *subagent* its own agent-tier span for the run that dispatches it.

    Without a rail of its own a sub-agent produces no ``agent.<type>.invoke``
    span, so its llm/tool spans attach to the **dispatching** agent's span —
    the sub-agent's whole run then reads as if the parent had made those calls,
    with nothing under the ``task_tool`` span it actually ran inside.

    Attaching at build time is unreliable: the parent agent is constructed
    once, typically before observability is initialized, so
    ``maybe_observability_rail()`` would return None. By dispatch time
    observability is up, and ``add_rail`` still lands before the sub-agent's
    first ``_ensure_initialized()`` registers its hooks.

    Idempotent, and a no-op when observability is off or *subagent* lacks the
    DeepAgent rail API. Best-effort: tracing must never break a run.

    Args:
        subagent: The freshly created sub-agent DeepAgent.
    """
    if subagent is None:
        return
    try:
        from openjiuwen.agent_teams.observability.rail import (
            ObservabilityRail,
            maybe_observability_rail,
        )

        rail = maybe_observability_rail()
        if rail is None:
            return  # observability not initialized -> nothing to trace
        configured = subagent.configured_rails() if hasattr(subagent, "configured_rails") else []
        if any(isinstance(item, ObservabilityRail) for item in configured):
            return  # already attached — never add a second one
        if hasattr(subagent, "add_rail"):
            subagent.add_rail(rail)
    except Exception as exc:
        logger.debug("[AgentObservability] attach subagent rail failed: %s", exc)

    # A sub-agent carries no team of its own, so it needs the same synthetic
    # marker as the agent that dispatched it — both for the rail's agent-tier
    # attributes and for the single-agent attribute suppression to key off.
    mark_single_agent_team(subagent)


def install_subagent_observability_hook() -> None:
    """Trace every sub-agent, whichever tool dispatched it.

    ``DeepAgent.create_subagent`` is the one point all dispatch paths share —
    the SDK's builtin ``task_tool``, a platform's custom agent tool, and
    background sub-agents. Wrapping it there is what makes tracing independent
    of the dispatcher; hooking a single tool covers only that tool.

    Idempotent — a second call sees the wrapper already installed. Best-effort:
    never raises, and a failure only costs sub-agent spans.
    """
    try:
        from openjiuwen.harness.deep_agent import DeepAgent
    except Exception as exc:  # pragma: no cover - import cycle guard
        logger.debug("[AgentObservability] subagent hook install skipped: %s", exc)
        return

    original = getattr(DeepAgent, "create_subagent", None)
    if original is None or getattr(original, _SUBAGENT_HOOK_MARKER_ATTR, False):
        return

    def create_subagent_with_observability(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Create the sub-agent, then give it its own observability rail."""
        subagent = original(self, *args, **kwargs)
        attach_subagent_observability(subagent)
        return subagent

    setattr(create_subagent_with_observability, _SUBAGENT_HOOK_MARKER_ATTR, True)
    DeepAgent.create_subagent = create_subagent_with_observability


__all__ = [
    "apply_single_agent_team_attr_suppression",
    "attach_subagent_observability",
    "install_subagent_observability_hook",
    "mark_single_agent_team",
]
