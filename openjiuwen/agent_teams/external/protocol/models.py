# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Value objects shared by the third-party agent harness protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, TypeAlias

from openjiuwen.agent_teams.external.protocol.errors import ExternalHarnessProtocolError

if TYPE_CHECKING:
    from openjiuwen.agent_teams.external.protocol.checkpoints import HarnessCheckpoint, HarnessCheckpointSink
    from openjiuwen.agent_teams.external.protocol.hooks import HarnessHookDispatcher
    from openjiuwen.agent_teams.external.protocol.interactions import HarnessInteractionHandler
    from openjiuwen.agent_teams.external.protocol.tools import ExternalToolGateway, McpServerConfig

PROTOCOL_VERSION = "4.0"

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


class HostCapability(str, Enum):
    """Fine-grained host service available to a harness implementation."""

    CHECKPOINT_SINK = "checkpoint_sink"
    NATIVE_TOOL_GATEWAY = "native_tool_gateway"
    MCP_SERVERS = "mcp_servers"
    HOOKS = "hooks"
    TOOL_APPROVAL = "tool_approval"
    USER_INPUT = "user_input"
    MCP_ELICITATION = "mcp_elicitation"
    DYNAMIC_TOOL_CALL = "dynamic_tool_call"
    PROVIDER_INTERACTION = "provider_interaction"
    TELEMETRY = "telemetry"


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
    compatible_protocol_versions: frozenset[str] = field(default_factory=lambda: frozenset({PROTOCOL_VERSION}))
    capabilities: frozenset[HarnessCapability] = field(default_factory=frozenset)
    required_host_capabilities: frozenset[HostCapability] = field(default_factory=frozenset)
    optional_host_capabilities: frozenset[HostCapability] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.name or not self.implementation_version or not self.protocol_version:
            raise ValueError("harness card identity and versions must not be empty")
        if self.protocol_version not in self.compatible_protocol_versions:
            raise ValueError("card protocol_version must be included in compatible_protocol_versions")
        overlap = self.required_host_capabilities & self.optional_host_capabilities
        if overlap:
            names = ", ".join(sorted(capability.value for capability in overlap))
            raise ValueError(f"host capabilities cannot be both required and optional: {names}")

    def supports(self, capability: HarnessCapability) -> bool:
        """Return whether the implementation declares ``capability``."""
        return capability in self.capabilities

    def validate_host(
        self,
        *,
        protocol_version: str,
        capabilities: frozenset[HostCapability],
    ) -> None:
        """Fail fast when a host cannot satisfy this provider's requirements."""

        if protocol_version not in self.compatible_protocol_versions:
            raise ExternalHarnessProtocolError(f"host protocol {protocol_version!r} is not supported by {self.name!r}")
        missing = self.required_host_capabilities - capabilities
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise ExternalHarnessProtocolError(f"host is missing required capabilities: {names}")


@dataclass(frozen=True, slots=True)
class ExternalHarnessInput:
    """One input accepted by an external harness."""

    content: Any
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Acknowledgement that a harness accepted an input command."""

    message_id: str
    turn_id: str
    accepted_mode: DeliveryMode

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("send receipt message_id must not be empty")
        if not self.turn_id:
            raise ValueError("send receipt turn_id must not be empty")


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
    protocol_version: str = PROTOCOL_VERSION
    host_capabilities: frozenset[HostCapability] = field(default_factory=frozenset)
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
    "HostCapability",
    "JsonObject",
    "JsonValue",
    "ResumePolicy",
    "SendReceipt",
]
