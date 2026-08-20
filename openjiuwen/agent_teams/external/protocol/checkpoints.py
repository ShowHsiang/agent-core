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
    data: JsonObject = field(default_factory=dict)
    session_id: str | None = None
    revision: str | None = None

    def __post_init__(self) -> None:
        required = {
            "provider": self.provider,
            "schema_version": self.schema_version,
            "member_agent_id": self.member_agent_id,
        }
        for field_name, value in required.items():
            if not value:
                raise ValueError(f"checkpoint {field_name} must not be empty")


@runtime_checkable
class HarnessCheckpointSink(Protocol):
    """Durable host-side destination for proactively published checkpoints."""

    async def save(self, checkpoint: HarnessCheckpoint, *, reason: CheckpointReason) -> None:
        """Persist ``checkpoint`` before returning to the harness."""
        ...


__all__ = ["CheckpointReason", "HarnessCheckpoint", "HarnessCheckpointSink"]
