# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Provider-neutral terminal results emitted by external harness turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from openjiuwen.agent_teams.external.protocol.models import JsonObject, JsonValue


class TurnStatus(str, Enum):
    """Terminal status of one external-input-driven turn."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """Normalized token usage with room for provider-specific counters."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnError:
    """Normalized failure information for a turn."""

    message: str
    code: str | None = None
    category: str | None = None
    retryable: bool | None = None
    provider_data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Complete provider-neutral terminal result for one turn."""

    status: TurnStatus
    final_output: JsonValue = None
    structured_output: JsonValue = None
    stop_reason: str | None = None
    error: TurnError | None = None
    usage: TurnUsage | None = None
    cost_usd: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    duration_ms: int | None = None
    provider_data: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is TurnStatus.FAILED and self.error is None:
            raise ValueError("failed turn result requires error")
        if self.status is not TurnStatus.FAILED and self.error is not None:
            raise ValueError("only failed turn result may contain error")


__all__ = ["TurnError", "TurnResult", "TurnStatus", "TurnUsage"]
