# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent-tier rail wiring for a single agent and its sub-agents."""

from __future__ import annotations

from types import SimpleNamespace

from openjiuwen.agent_teams.observability.rail import ObservabilityRail
from openjiuwen.extensions.observability.semconv import (
    LANGFUSE_OBSERVATION_TYPE,
    LANGFUSE_SESSION_ID,
)
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.observability.constants import SINGLE_AGENT_TEAM_NAME
from openjiuwen.harness.observability.rail import (
    apply_single_agent_team_attr_suppression,
    attach_subagent_observability,
    install_subagent_observability_hook,
    mark_single_agent_team,
)


class _RecordingSpan:
    """Span stub collecting the attributes the rail stamps on it."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        """Record one attribute the way a real span would apply it."""
        self.attributes[key] = value


class _Subagent:
    """DeepAgent stand-in exposing just the rail API the hook touches."""

    def __init__(self) -> None:
        self.rails: list[object] = []
        self.team_name = ""

    def configured_rails(self) -> list[object]:
        """Return the rails currently mounted on this agent."""
        return list(self.rails)

    def add_rail(self, rail: object) -> None:
        """Mount one rail."""
        self.rails.append(rail)


def test_single_agent_marker_gives_the_agent_its_own_span_tier() -> None:
    """Without the marker the rail returns early and the agent tier disappears."""
    agent = SimpleNamespace(team_name="")

    mark_single_agent_team(agent)

    assert agent.team_name == SINGLE_AGENT_TEAM_NAME


def test_single_agent_marker_leaves_a_real_team_member_alone() -> None:
    """A spawned teammate already has its team; never overwrite it."""
    agent = SimpleNamespace(team_name="research_team")

    mark_single_agent_team(agent)

    assert agent.team_name == "research_team"


def test_single_agent_spans_drop_the_redundant_team_attributes() -> None:
    """The synthetic team is not a real one, so its identity block is noise."""
    apply_single_agent_team_attr_suppression()
    span = _RecordingSpan()

    ObservabilityRail._stamp_agent_attributes(
        span,
        agent=SimpleNamespace(),
        member_name="solo",
        team_name=SINGLE_AGENT_TEAM_NAME,
        session_id="sess-A",
        is_leader=False,
    )

    assert not [key for key in span.attributes if key.startswith("agentteam.")]
    assert span.attributes[LANGFUSE_OBSERVATION_TYPE] == "agent"
    assert span.attributes[LANGFUSE_SESSION_ID] == "sess-A"


def test_a_real_team_member_keeps_the_original_stamping() -> None:
    apply_single_agent_team_attr_suppression()
    span = _RecordingSpan()

    ObservabilityRail._stamp_agent_attributes(
        span,
        agent=SimpleNamespace(),
        member_name="researcher",
        team_name="research_team",
        session_id="sess-A",
        is_leader=True,
    )

    assert [key for key in span.attributes if key.startswith("agentteam.")]


def test_subagent_hook_traces_every_dispatch_path(monkeypatch) -> None:
    """Any sub-agent created through create_subagent gets an observability rail.

    The builtin ``task_tool`` creates its sub-agent inside the SDK, so only a
    hook at creation reaches it — attaching from a single dispatching tool
    leaves every other path untraced.
    """
    created = _Subagent()
    monkeypatch.setattr(
        DeepAgent, "create_subagent", lambda self, *args, **kwargs: created, raising=False
    )
    monkeypatch.setattr(
        "openjiuwen.agent_teams.observability.rail.maybe_observability_rail",
        ObservabilityRail,
    )

    install_subagent_observability_hook()
    returned = DeepAgent.create_subagent(object(), "explore_agent", "sess-1")

    assert returned is created
    assert sum(isinstance(rail, ObservabilityRail) for rail in created.rails) == 1
    assert created.team_name == SINGLE_AGENT_TEAM_NAME

    # Idempotent: re-installing must not stack wrappers, and a second creation
    # must not add a second rail.
    install_subagent_observability_hook()
    DeepAgent.create_subagent(object(), "explore_agent", "sess-1")

    assert sum(isinstance(rail, ObservabilityRail) for rail in created.rails) == 1


def test_subagent_gets_no_rail_while_observability_is_off(monkeypatch) -> None:
    """``maybe_observability_rail`` returns None before the provider is up."""
    monkeypatch.setattr(
        "openjiuwen.agent_teams.observability.rail.maybe_observability_rail",
        lambda: None,
    )
    created = _Subagent()
    monkeypatch.setattr(
        DeepAgent, "create_subagent", lambda self, *args, **kwargs: created, raising=False
    )

    install_subagent_observability_hook()
    DeepAgent.create_subagent(object(), "explore_agent", "sess-1")

    assert created.rails == []


def test_subagent_that_already_has_a_rail_is_left_alone(monkeypatch) -> None:
    """A second rail would double every span the sub-agent emits."""
    monkeypatch.setattr(
        "openjiuwen.agent_teams.observability.rail.maybe_observability_rail",
        ObservabilityRail,
    )
    subagent = _Subagent()
    subagent.add_rail(ObservabilityRail())

    attach_subagent_observability(subagent)

    assert sum(isinstance(rail, ObservabilityRail) for rail in subagent.rails) == 1
