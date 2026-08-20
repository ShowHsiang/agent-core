# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Value objects shared by the third-party agent harness protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, TypeAlias

if TYPE_CHECKING:
    from openjiuwen.agent_teams.external.protocol.checkpoints import HarnessCheckpoint, HarnessCheckpointSink
    from openjiuwen.agent_teams.external.protocol.hooks import HarnessHookDispatcher
    from openjiuwen.agent_teams.external.protocol.interactions import HarnessInteractionHandler
    from openjiuwen.agent_teams.external.protocol.tools import ExternalToolGateway, McpServerConfig

PROTOCOL_VERSION = "3.0"

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = Mapping[str, JsonValue]


class HarnessCapability(str, Enum):
    """Optional behavior an external harness may advertise."""

    STEER = "steer"
    GRACEFUL_ABORT = "graceful_abort"
    FORCE_ABORT = "force_abort"
    PAUSE_RESUME = "pause_resume"
    PERSISTENT_SESSION = "persistent_session"
    CHECKPOINT = "checkpoint"
    NATIVE_TOOLS = "native_tools"
    MCP_TOOLS = "mcp_tools"
    HOOKS = "hooks"
    HOST_INTERACTIONS = "host_interactions"


class DeliveryMode(str, Enum):
    """How an input is delivered relative to the active turn."""

    AUTO = "auto"
    STEER = "steer"
    FOLLOW_UP = "follow_up"


class AbortMode(str, Enum):
    """How the active turn should be aborted."""

    GRACEFUL = "graceful"
    FORCE = "force"


class ResumePolicy(str, Enum):
    """How a harness should use the checkpoint supplied at start."""

    NEW = "new"
    RESUME_IF_AVAILABLE = "resume_if_available"
    REQUIRE_RESUME = "require_resume"


@dataclass(frozen=True, slots=True)
class ExternalHarnessCard:
    """Static identity and capability metadata for one harness implementation."""

    name: str
    implementation_version: str
    protocol_version: str = PROTOCOL_VERSION
    capabilities: frozenset[HarnessCapability] = field(default_factory=frozenset)

    def supports(self, capability: HarnessCapability) -> bool:
        """Return whether the implementation declares ``capability``."""
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class ExternalHarnessInput:
    """One input accepted by an external harness."""

    content: Any
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Acknowledgement that a harness accepted an input command."""

    message_id: str
    accepted_mode: DeliveryMode


@dataclass(frozen=True, slots=True)
class ExternalHarnessContext:
    """Per-member runtime context supplied when an external harness starts.

    ``env`` may contain secrets and must not be logged or copied into events.
    ``checkpoint`` is opaque to OpenJiuwen; only the owning implementation may
    interpret its contents. ``interactions`` is an awaited control plane and
    must not be replaced by observation events.
    """

    team_name: str
    member_name: str
    member_agent_id: str
    team_session_id: str
    system_prompt: str
    resume_policy: ResumePolicy = ResumePolicy.NEW
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    checkpoint: "HarnessCheckpoint | None" = None
    checkpoint_sink: "HarnessCheckpointSink | None" = None
    tools: "ExternalToolGateway | None" = None
    mcp_servers: tuple["McpServerConfig", ...] = ()
    hooks: "HarnessHookDispatcher | None" = None
    interactions: "HarnessInteractionHandler | None" = None
    telemetry: Any = None
    metadata: JsonObject = field(default_factory=dict)


__all__ = [
    "PROTOCOL_VERSION",
    "AbortMode",
    "DeliveryMode",
    "ExternalHarnessCard",
    "ExternalHarnessContext",
    "ExternalHarnessInput",
    "HarnessCapability",
    "JsonObject",
    "JsonValue",
    "ResumePolicy",
    "SendReceipt",
]
