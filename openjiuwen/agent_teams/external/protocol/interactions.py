# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Awaited host interactions requested by a third-party agent harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypeAlias, overload, runtime_checkable

from openjiuwen.agent_teams.external.protocol.errors import ExternalHarnessProtocolError
from openjiuwen.agent_teams.external.protocol.models import JsonObject, JsonValue


class InteractionResponseStatus(str, Enum):
    """Completion status for an interaction that is not an approval."""

    COMPLETED = "completed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ToolApprovalDecision(str, Enum):
    """Host decision for a provider-requested tool execution."""

    ALLOW = "allow"
    ALLOW_FOR_SESSION = "allow_for_session"
    DENY = "deny"
    ABORT = "abort"


class InteractionCancelReason(str, Enum):
    """Reason a pending provider-to-host interaction was cancelled."""

    PROVIDER_WITHDREW = "provider_withdrew"
    TURN_ABORTED = "turn_aborted"
    HARNESS_STOPPED = "harness_stopped"
    DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(frozen=True, slots=True)
class ToolApprovalRequest:
    """Request authorization before a provider executes a tool call."""

    request_id: str
    call_id: str
    tool_name: str
    arguments: JsonObject = field(default_factory=dict)
    session_id: str | None = None
    turn_id: str | None = None
    title: str | None = None
    description: str | None = None
    reason: str | None = None
    deadline_at: float | None = None
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserInputRequest:
    """Request additional input from the user while a turn is active."""

    request_id: str
    prompt: str
    session_id: str | None = None
    turn_id: str | None = None
    choices: tuple[str, ...] = ()
    deadline_at: float | None = None
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpElicitationRequest:
    """Request structured user input for an MCP elicitation."""

    request_id: str
    server_name: str
    prompt: str
    schema: JsonObject | None = None
    session_id: str | None = None
    turn_id: str | None = None
    deadline_at: float | None = None
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DynamicToolCallRequest:
    """Delegate a provider-originated dynamic tool call to the host."""

    request_id: str
    call_id: str
    tool_name: str
    arguments: JsonObject = field(default_factory=dict)
    namespace: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    deadline_at: float | None = None
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderInteractionRequest:
    """Provider extension request not covered by a shared interaction type."""

    request_id: str
    provider: str
    request_type: str
    schema_version: str
    payload: JsonObject = field(default_factory=dict)
    session_id: str | None = None
    turn_id: str | None = None
    deadline_at: float | None = None


@dataclass(frozen=True, slots=True)
class ToolApprovalResponse:
    """Decision returned for :class:`ToolApprovalRequest`."""

    request_id: str
    decision: ToolApprovalDecision
    updated_arguments: JsonObject | None = None
    reason: str | None = None
    interrupt: bool = False
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserInputResponse:
    """Response returned for :class:`UserInputRequest`."""

    request_id: str
    status: InteractionResponseStatus
    content: JsonValue = None
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpElicitationResponse:
    """Response returned for :class:`McpElicitationRequest`."""

    request_id: str
    status: InteractionResponseStatus
    values: JsonObject = field(default_factory=dict)
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DynamicToolCallResponse:
    """Response returned for :class:`DynamicToolCallRequest`."""

    request_id: str
    status: InteractionResponseStatus
    result: JsonValue = None
    is_error: bool = False
    error_message: str | None = None
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderInteractionResponse:
    """Response returned for :class:`ProviderInteractionRequest`."""

    request_id: str
    status: InteractionResponseStatus
    payload: JsonObject = field(default_factory=dict)


HarnessInteractionRequest: TypeAlias = (
    ToolApprovalRequest | UserInputRequest | McpElicitationRequest | DynamicToolCallRequest | ProviderInteractionRequest
)
HarnessInteractionResponse: TypeAlias = (
    ToolApprovalResponse
    | UserInputResponse
    | McpElicitationResponse
    | DynamicToolCallResponse
    | ProviderInteractionResponse
)


_EXPECTED_RESPONSE_TYPE = {
    ToolApprovalRequest: ToolApprovalResponse,
    UserInputRequest: UserInputResponse,
    McpElicitationRequest: McpElicitationResponse,
    DynamicToolCallRequest: DynamicToolCallResponse,
    ProviderInteractionRequest: ProviderInteractionResponse,
}


def validate_interaction_response(
    request: HarnessInteractionRequest,
    response: HarnessInteractionResponse,
) -> HarnessInteractionResponse:
    """Validate request identity and the response shape required by its type."""

    if response.request_id != request.request_id:
        raise ExternalHarnessProtocolError(
            f"interaction response id {response.request_id!r} does not match request {request.request_id!r}"
        )
    expected_type = _EXPECTED_RESPONSE_TYPE[type(request)]
    if not isinstance(response, expected_type):
        raise ExternalHarnessProtocolError(
            f"{type(request).__name__} requires {expected_type.__name__}, got {type(response).__name__}"
        )
    return response


@runtime_checkable
class HarnessInteractionHandler(Protocol):
    """Host service for request/response interactions during a live turn.

    Implementations must return the response type paired with the request and
    the same ``request_id`` before ``deadline_at`` when one is supplied.
    ``cancel`` is idempotent and releases any pending host UI or policy
    operation for the request. Harnesses must cancel every pending request
    before aborting its turn or completing ``stop``.
    """

    @overload
    async def handle(self, request: ToolApprovalRequest) -> ToolApprovalResponse: ...

    @overload
    async def handle(self, request: UserInputRequest) -> UserInputResponse: ...

    @overload
    async def handle(self, request: McpElicitationRequest) -> McpElicitationResponse: ...

    @overload
    async def handle(self, request: DynamicToolCallRequest) -> DynamicToolCallResponse: ...

    @overload
    async def handle(self, request: ProviderInteractionRequest) -> ProviderInteractionResponse: ...

    async def handle(self, request: HarnessInteractionRequest) -> HarnessInteractionResponse:
        """Wait for and return the host response to ``request``."""
        ...

    async def cancel(
        self,
        request_id: str,
        *,
        reason: InteractionCancelReason = InteractionCancelReason.PROVIDER_WITHDREW,
    ) -> None:
        """Cancel a pending interaction when the provider withdraws it."""
        ...


__all__ = [
    "DynamicToolCallRequest",
    "DynamicToolCallResponse",
    "HarnessInteractionHandler",
    "HarnessInteractionRequest",
    "HarnessInteractionResponse",
    "InteractionCancelReason",
    "InteractionResponseStatus",
    "McpElicitationRequest",
    "McpElicitationResponse",
    "ProviderInteractionRequest",
    "ProviderInteractionResponse",
    "ToolApprovalDecision",
    "ToolApprovalRequest",
    "ToolApprovalResponse",
    "UserInputRequest",
    "UserInputResponse",
    "validate_interaction_response",
]
