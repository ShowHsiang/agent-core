# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native-tool and MCP service descriptions for external harnesses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from openjiuwen.agent_teams.external.protocol.models import JsonObject


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Provider-neutral definition of a tool exposed by OpenJiuwen."""

    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """One native tool invocation requested by an external harness."""

    call_id: str
    name: str
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Provider-neutral result of a native tool invocation."""

    content: Any
    is_error: bool = False


@runtime_checkable
class ExternalToolGateway(Protocol):
    """Native SDK tool surface supplied by the OpenJiuwen host."""

    async def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return the tools visible to this member."""
        ...

    async def invoke(self, invocation: ToolInvocation) -> ToolExecutionResult:
        """Execute one visible tool under the host's permission policy."""
        ...


class McpTransport(str, Enum):
    """Transport used to expose an MCP server to an external harness."""

    STDIO = "stdio"
    HTTP = "http"
    IN_PROCESS = "in_process"


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """Provider-neutral MCP server configuration.

    Exactly one transport-specific target is required: ``command`` for stdio,
    ``url`` for HTTP, or ``instance`` for an in-process SDK server.
    """

    name: str
    transport: McpTransport
    command: tuple[str, ...] = ()
    url: str | None = None
    instance: Any = None
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject incomplete transport configurations early."""
        if self.transport is McpTransport.STDIO and not self.command:
            raise ValueError("stdio MCP server requires a non-empty command")
        if self.transport is McpTransport.HTTP and not self.url:
            raise ValueError("HTTP MCP server requires url")
        if self.transport is McpTransport.IN_PROCESS and self.instance is None:
            raise ValueError("in-process MCP server requires instance")


__all__ = [
    "ExternalToolGateway",
    "McpServerConfig",
    "McpTransport",
    "ToolDefinition",
    "ToolExecutionResult",
    "ToolInvocation",
]
