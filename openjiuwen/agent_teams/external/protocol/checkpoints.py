# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Versioned checkpoint contract for third-party agent harnesses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from openjiuwen.agent_teams.external.protocol.models import JsonObject


class CheckpointReason(str, Enum):
    """Reason an implementation publishes a checkpoint."""

    SESSION_ACTIVATED = "session_activated"
    TURN_COMPLETED = "turn_completed"
    STATE_CHANGED = "state_changed"
    PERIODIC = "periodic"
    PROVIDER_REQUESTED = "provider_requested"


@dataclass(frozen=True, slots=True)
class HarnessCheckpoint:
    """Opaque provider state scoped to one team member.

    Only the provider named by ``provider`` may interpret ``data`` and
    ``schema_version``. The host persists the complete envelope unchanged.
    """

    provider: str
    schema_version: str
    member_agent_id: str
    team_session_id: str
    checkpoint_id: str
    sequence: int
    data: JsonObject = field(default_factory=dict)
    session_id: str | None = None
    revision: str | None = None

    def __post_init__(self) -> None:
        required = {
            "provider": self.provider,
            "schema_version": self.schema_version,
            "member_agent_id": self.member_agent_id,
            "team_session_id": self.team_session_id,
            "checkpoint_id": self.checkpoint_id,
        }
        for field_name, value in required.items():
            if not value:
                raise ValueError(f"checkpoint {field_name} must not be empty")
        if self.sequence < 0:
            raise ValueError("checkpoint sequence must be non-negative")


@dataclass(frozen=True, slots=True)
class CheckpointSaveReceipt:
    """Durable acknowledgement of one idempotent checkpoint write."""

    checkpoint_id: str
    sequence: int
    storage_revision: str

    def __post_init__(self) -> None:
        if not self.checkpoint_id or not self.storage_revision:
            raise ValueError("checkpoint save receipt ids must not be empty")
        if self.sequence < 0:
            raise ValueError("checkpoint save receipt sequence must be non-negative")


@runtime_checkable
class HarnessCheckpointSink(Protocol):
    """Durable host-side destination for proactively published checkpoints.

    Sequence numbers are monotonic within one provider/member/session scope.
    Retrying the same ``checkpoint_id`` is idempotent and returns the original
    receipt. A different write at an existing or lower sequence, or a failed
    ``expected_storage_revision`` comparison, raises
    ``CheckpointConflictError`` instead of overwriting newer state.
    """

    async def save(
        self,
        checkpoint: HarnessCheckpoint,
        *,
        reason: CheckpointReason,
        expected_storage_revision: str | None = None,
    ) -> CheckpointSaveReceipt:
        """Persist ``checkpoint`` durably and return its storage revision."""
        ...


__all__ = ["CheckpointReason", "CheckpointSaveReceipt", "HarnessCheckpoint", "HarnessCheckpointSink"]
