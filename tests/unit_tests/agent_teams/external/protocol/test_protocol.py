# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract-model tests for the third-party agent harness protocol."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import AsyncIterator

import pytest

from openjiuwen.agent_teams.external.protocol import (
    PROTOCOL_VERSION,
    AbortMode,
    BeforeToolContext,
    CheckpointConflictError,
    CheckpointReason,
    CheckpointSaveReceipt,
    DeliveryMode,
    ExternalHarnessCard,
    ExternalHarnessContext,
    ExternalHarnessInput,
    ExternalHarnessProtocol,
    ExternalHarnessProtocolError,
    ExternalHarnessProvider,
    HarnessCapability,
    HarnessCheckpoint,
    HarnessCheckpointSink,
    HarnessEvent,
    HarnessInteractionHandler,
    HarnessInteractionRequest,
    HarnessInteractionResponse,
    InteractionCancelReason,
    InteractionResponseStatus,
    McpServerConfig,
    McpTransport,
    OutputEvent,
    OutputKind,
    ProviderEvent,
    ResumePolicy,
    SendReceipt,
    StateChangedEvent,
    ToolApprovalDecision,
    ToolApprovalRequest,
    ToolApprovalResponse,
    TurnError,
    TurnEventKind,
    TurnLifecycleEvent,
    TurnResult,
    TurnStatus,
    UserInputRequest,
    UserInputResponse,
    validate_interaction_response,
)
from openjiuwen.agent_teams.harness import HarnessState


class _Harness:
    card = ExternalHarnessCard(
        name="test-harness",
        implementation_version="1.0.0",
        capabilities=frozenset(
            {
                HarnessCapability.STEER,
                HarnessCapability.CHECKPOINT,
                HarnessCapability.HOST_INTERACTIONS,
            }
        ),
    )

    def __init__(self) -> None:
        self._state = HarnessState.IDLE
        turn_id = "turn-1"
        self._stream = (
            HarnessEvent(
                sequence=1,
                timestamp=0.0,
                event=StateChangedEvent(old=HarnessState.IDLE, new=HarnessState.RUNNING),
            ),
            HarnessEvent(
                sequence=2,
                timestamp=0.1,
                event=TurnLifecycleEvent(kind=TurnEventKind.STARTED),
                turn_id=turn_id,
            ),
            HarnessEvent(
                sequence=3,
                timestamp=0.2,
                event=TurnLifecycleEvent(kind=TurnEventKind.PAUSED),
                turn_id=turn_id,
            ),
            HarnessEvent(
                sequence=4,
                timestamp=0.3,
                event=TurnLifecycleEvent(kind=TurnEventKind.RESUMED),
                turn_id=turn_id,
            ),
            HarnessEvent(
                sequence=5,
                timestamp=0.4,
                event=OutputEvent(kind=OutputKind.TEXT, content="done"),
                turn_id=turn_id,
            ),
            HarnessEvent(
                sequence=6,
                timestamp=0.5,
                event=TurnLifecycleEvent(
                    kind=TurnEventKind.FINISHED,
                    result=TurnResult(status=TurnStatus.COMPLETED, final_output="done"),
                ),
                turn_id=turn_id,
            ),
        )
        self._stream_cursor = 0

    @property
    def state(self) -> HarnessState:
        return self._state

    @property
    def session_id(self) -> str | None:
        return "provider-session"

    async def start(self, context: ExternalHarnessContext) -> None:
        _ = context
        self._state = HarnessState.IDLE

    async def stop(self) -> None:
        self._state = HarnessState.TERMINATED

    async def events(self) -> AsyncIterator[HarnessEvent]:
        while self._stream_cursor < len(self._stream):
            event = self._stream[self._stream_cursor]
            self._stream_cursor += 1
            yield event

    async def turn_events(self, turn_id: str | None = None) -> AsyncIterator[HarnessEvent]:
        selected_turn_id = None
        async for event in self.events():
            payload = event.event
            if selected_turn_id is None:
                if not (
                    isinstance(payload, TurnLifecycleEvent)
                    and payload.kind is TurnEventKind.STARTED
                    and (turn_id is None or event.turn_id == turn_id)
                ):
                    continue
                selected_turn_id = event.turn_id

            yield event
            if (
                event.turn_id == selected_turn_id
                and isinstance(payload, TurnLifecycleEvent)
                and payload.kind in {TurnEventKind.FINISHED, TurnEventKind.ABORTED, TurnEventKind.FAILED}
            ):
                return

    async def send(
        self,
        content: ExternalHarnessInput,
        *,
        mode: DeliveryMode = DeliveryMode.AUTO,
    ) -> SendReceipt:
        _ = content
        return SendReceipt(message_id="message-1", turn_id="turn-1", accepted_mode=mode)

    async def abort(self, *, mode: AbortMode = AbortMode.GRACEFUL) -> None:
        _ = mode

    async def pause(self) -> None:
        pass

    async def resume(self, *, query: ExternalHarnessInput | None = None) -> None:
        _ = query

    async def export_checkpoint(self) -> HarnessCheckpoint:
        return HarnessCheckpoint(
            provider="test",
            schema_version="1",
            member_agent_id="team-a_member-a",
            team_session_id="team-session-1",
            checkpoint_id="checkpoint-1",
            sequence=1,
            session_id="provider-session",
        )


class _Provider:
    card = _Harness.card

    def create(self, config):
        _ = config
        return _Harness()


class _CheckpointSink:
    def __init__(self) -> None:
        self.saved: tuple[HarnessCheckpoint, CheckpointReason] | None = None
        self._receipts: dict[str, CheckpointSaveReceipt] = {}
        self._latest_sequence = -1
        self._storage_revision: str | None = None

    async def save(
        self,
        checkpoint: HarnessCheckpoint,
        *,
        reason: CheckpointReason,
        expected_storage_revision: str | None = None,
    ) -> CheckpointSaveReceipt:
        if checkpoint.checkpoint_id in self._receipts:
            return self._receipts[checkpoint.checkpoint_id]
        if checkpoint.sequence <= self._latest_sequence:
            raise CheckpointConflictError("stale checkpoint sequence")
        if expected_storage_revision is not None and expected_storage_revision != self._storage_revision:
            raise CheckpointConflictError("checkpoint compare-and-set failed")
        self.saved = (checkpoint, reason)
        self._latest_sequence = checkpoint.sequence
        self._storage_revision = f"storage-{checkpoint.sequence}"
        receipt = CheckpointSaveReceipt(
            checkpoint_id=checkpoint.checkpoint_id,
            sequence=checkpoint.sequence,
            storage_revision=self._storage_revision,
        )
        self._receipts[checkpoint.checkpoint_id] = receipt
        return receipt


class _InteractionHandler:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, InteractionCancelReason]] = []

    async def handle(self, request: HarnessInteractionRequest) -> HarnessInteractionResponse:
        if isinstance(request, ToolApprovalRequest):
            return ToolApprovalResponse(request_id=request.request_id, decision=ToolApprovalDecision.ALLOW)
        if isinstance(request, UserInputRequest):
            return UserInputResponse(
                request_id=request.request_id,
                status=InteractionResponseStatus.COMPLETED,
                content="host answer",
            )
        raise AssertionError(f"unexpected interaction: {type(request).__name__}")

    async def cancel(
        self,
        request_id: str,
        *,
        reason: InteractionCancelReason = InteractionCancelReason.PROVIDER_WITHDREW,
    ) -> None:
        self.cancelled.append((request_id, reason))


def test_structural_protocols_accept_complete_implementations() -> None:
    assert isinstance(_Harness(), ExternalHarnessProtocol)
    assert isinstance(_Provider(), ExternalHarnessProvider)
    assert isinstance(_CheckpointSink(), HarnessCheckpointSink)
    assert isinstance(_InteractionHandler(), HarnessInteractionHandler)


def test_card_is_immutable_and_reports_capabilities() -> None:
    card = _Harness.card

    assert card.protocol_version == PROTOCOL_VERSION == "4.0"
    assert card.supports(HarnessCapability.STEER)
    assert not card.supports(HarnessCapability.PAUSE_RESUME)

    with pytest.raises(FrozenInstanceError):
        card.name = "changed"  # type: ignore[misc]


def test_context_keeps_checkpoint_and_host_services() -> None:
    mcp = McpServerConfig(
        name="team",
        transport=McpTransport.STDIO,
        command=("openjiuwen-team-mcp",),
        env={"MEMBER_SCOPE": "member-a"},
    )
    checkpoint = HarnessCheckpoint(
        provider="claude-code",
        schema_version="1",
        member_agent_id="team-a_member-a",
        team_session_id="session-a",
        checkpoint_id="checkpoint-1",
        sequence=1,
        data={"conversation_id": "conversation-a"},
    )
    checkpoint_sink = _CheckpointSink()
    interactions = _InteractionHandler()
    context = ExternalHarnessContext(
        team_name="team-a",
        member_name="member-a",
        member_agent_id="team-a_member-a",
        team_session_id="session-a",
        system_prompt="You are a teammate.",
        resume_policy=ResumePolicy.REQUIRE_RESUME,
        checkpoint=checkpoint,
        checkpoint_sink=checkpoint_sink,
        mcp_servers=(mcp,),
        interactions=interactions,
    )

    assert context.resume_policy is ResumePolicy.REQUIRE_RESUME
    assert context.checkpoint is checkpoint
    assert context.checkpoint_sink is checkpoint_sink
    assert context.interactions is interactions
    assert context.mcp_servers == (mcp,)


@pytest.mark.asyncio
async def test_checkpoint_sink_accepts_proactive_updates() -> None:
    sink = _CheckpointSink()
    checkpoint = await _Harness().export_checkpoint()

    receipt = await sink.save(checkpoint, reason=CheckpointReason.TURN_COMPLETED)
    retried = await sink.save(checkpoint, reason=CheckpointReason.TURN_COMPLETED)

    assert sink.saved == (checkpoint, CheckpointReason.TURN_COMPLETED)
    assert receipt == retried == CheckpointSaveReceipt("checkpoint-1", 1, "storage-1")

    stale = HarnessCheckpoint(
        provider="test",
        schema_version="1",
        member_agent_id="team-a_member-a",
        team_session_id="team-session-1",
        checkpoint_id="checkpoint-stale",
        sequence=0,
    )
    with pytest.raises(CheckpointConflictError, match="stale"):
        await sink.save(stale, reason=CheckpointReason.PERIODIC)


def test_checkpoint_requires_provider_version_and_member_scope() -> None:
    with pytest.raises(ValueError, match="provider must not be empty"):
        HarnessCheckpoint(
            provider="",
            schema_version="1",
            member_agent_id="member-a",
            team_session_id="session-a",
            checkpoint_id="checkpoint-1",
            sequence=1,
        )
    with pytest.raises(ValueError, match="schema_version must not be empty"):
        HarnessCheckpoint(
            provider="acme",
            schema_version="",
            member_agent_id="member-a",
            team_session_id="session-a",
            checkpoint_id="checkpoint-1",
            sequence=1,
        )
    with pytest.raises(ValueError, match="member_agent_id must not be empty"):
        HarnessCheckpoint(
            provider="acme",
            schema_version="1",
            member_agent_id="",
            team_session_id="session-a",
            checkpoint_id="checkpoint-1",
            sequence=1,
        )
    with pytest.raises(ValueError, match="team_session_id must not be empty"):
        HarnessCheckpoint(
            provider="acme",
            schema_version="1",
            member_agent_id="member-a",
            team_session_id="",
            checkpoint_id="checkpoint-1",
            sequence=1,
        )
    with pytest.raises(ValueError, match="checkpoint_id must not be empty"):
        HarnessCheckpoint(
            provider="acme",
            schema_version="1",
            member_agent_id="member-a",
            team_session_id="session-a",
            checkpoint_id="",
            sequence=1,
        )
    with pytest.raises(ValueError, match="sequence must be non-negative"):
        HarnessCheckpoint(
            provider="acme",
            schema_version="1",
            member_agent_id="member-a",
            team_session_id="session-a",
            checkpoint_id="checkpoint-1",
            sequence=-1,
        )


@pytest.mark.asyncio
async def test_interaction_handler_returns_correlated_response_and_cancels() -> None:
    handler = _InteractionHandler()
    request = ToolApprovalRequest(
        request_id="approval-1",
        call_id="call-1",
        tool_name="shell",
        arguments={"command": "pwd"},
        turn_id="turn-1",
        deadline_at=2_000_000_000.0,
    )

    response = validate_interaction_response(request, await handler.handle(request))
    await handler.cancel(request.request_id, reason=InteractionCancelReason.TURN_ABORTED)

    assert response == ToolApprovalResponse(request_id="approval-1", decision=ToolApprovalDecision.ALLOW)
    assert handler.cancelled == [("approval-1", InteractionCancelReason.TURN_ABORTED)]
    assert request.turn_id == "turn-1"
    assert request.deadline_at == 2_000_000_000.0

    with pytest.raises(ExternalHarnessProtocolError, match="requires ToolApprovalResponse"):
        validate_interaction_response(
            request,
            UserInputResponse(request_id=request.request_id, status=InteractionResponseStatus.COMPLETED),
        )
    with pytest.raises(ExternalHarnessProtocolError, match="does not match"):
        validate_interaction_response(
            request,
            ToolApprovalResponse(request_id="wrong-id", decision=ToolApprovalDecision.DENY),
        )


def test_hook_context_uses_turn_identity() -> None:
    context = BeforeToolContext(
        member_name="member-a",
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="shell",
        arguments={"command": "pwd"},
    )

    assert context.turn_id == "turn-1"


@pytest.mark.asyncio
async def test_turn_events_is_finite_and_includes_terminal_event() -> None:
    harness = _Harness()
    receipt = await harness.send(ExternalHarnessInput(content="hello"))
    response = [event async for event in harness.turn_events(receipt.turn_id)]
    remaining = [event async for event in harness.events()]

    assert receipt == SendReceipt(message_id="message-1", turn_id="turn-1", accepted_mode=DeliveryMode.AUTO)
    assert len(response) == 5
    assert [event.sequence for event in response] == [2, 3, 4, 5, 6]
    assert isinstance(response[0].event, TurnLifecycleEvent)
    assert response[0].event.kind is TurnEventKind.STARTED
    assert isinstance(response[1].event, TurnLifecycleEvent)
    assert response[1].event.kind is TurnEventKind.PAUSED
    assert isinstance(response[2].event, TurnLifecycleEvent)
    assert response[2].event.kind is TurnEventKind.RESUMED
    assert isinstance(response[-1].event, TurnLifecycleEvent)
    assert response[-1].event.kind is TurnEventKind.FINISHED
    assert response[-1].event.result == TurnResult(status=TurnStatus.COMPLETED, final_output="done")
    assert remaining == []


@pytest.mark.parametrize(
    ("transport", "kwargs", "message"),
    [
        (McpTransport.STDIO, {}, "non-empty command"),
        (McpTransport.HTTP, {}, "requires url"),
        (McpTransport.IN_PROCESS, {}, "requires instance"),
    ],
)
def test_mcp_config_rejects_missing_transport_target(transport, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        McpServerConfig(name="team", transport=transport, **kwargs)


def test_event_envelope_carries_neutral_output_and_correlation() -> None:
    payload = OutputEvent(kind=OutputKind.TEXT, content="done", is_delta=True)

    event = HarnessEvent(
        sequence=1,
        timestamp=1.5,
        event=payload,
        session_id="session-1",
        turn_id="turn-1",
        correlation_id="message-1",
    )

    assert event.event is payload
    assert event.turn_id == "turn-1"
    assert event.correlation_id == "message-1"


def test_turn_terminal_event_requires_matching_structured_result() -> None:
    result = TurnResult(status=TurnStatus.COMPLETED, final_output="done")
    payload = TurnLifecycleEvent(kind=TurnEventKind.FINISHED, result=result)

    event = HarnessEvent(sequence=2, timestamp=2.0, event=payload, turn_id="turn-1")

    assert payload.result is result
    assert event.event is payload

    with pytest.raises(ValueError, match="requires interrupted result"):
        TurnLifecycleEvent(kind=TurnEventKind.ABORTED, result=result)
    with pytest.raises(ValueError, match="non-terminal paused"):
        TurnLifecycleEvent(kind=TurnEventKind.PAUSED, result=result)
    with pytest.raises(ValueError, match="requires a result"):
        TurnLifecycleEvent(kind=TurnEventKind.FAILED)
    with pytest.raises(ValueError, match="requires turn_id"):
        HarnessEvent(sequence=3, timestamp=3.0, event=payload)


def test_failed_result_requires_structured_error() -> None:
    with pytest.raises(ValueError, match="requires error"):
        TurnResult(status=TurnStatus.FAILED)

    error = TurnError(message="provider failed", code="provider_error", retryable=True)
    result = TurnResult(status=TurnStatus.FAILED, error=error)

    assert result.error is error


def test_provider_event_preserves_namespaced_extension_payload() -> None:
    payload = ProviderEvent(
        provider="codex",
        event_type="thread.compacted",
        schema_version="1",
        payload={"thread_id": "thread-1"},
    )

    event = HarnessEvent(sequence=4, timestamp=4.0, event=payload, session_id="thread-1")

    assert event.event is payload
