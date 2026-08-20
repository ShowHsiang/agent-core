# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behavioral protocol for third-party agent harness integrations."""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from openjiuwen.agent_teams.harness.state import HarnessState

from openjiuwen.agent_teams.external.protocol.checkpoints import HarnessCheckpoint
from openjiuwen.agent_teams.external.protocol.events import HarnessEvent
from openjiuwen.agent_teams.external.protocol.models import (
    AbortMode,
    DeliveryMode,
    ExternalHarnessCard,
    ExternalHarnessContext,
    ExternalHarnessInput,
    JsonObject,
    SendReceipt,
)


@runtime_checkable
class ExternalHarnessProtocol(Protocol):
    """Concurrent-safe harness behavior required of a third-party agent.

    ``events`` and ``turn_events`` are alternative views over one logical
    single-consumer observation channel. Implementations must serialize state
    transitions internally; callers may issue commands from different tasks.
    """

    @property
    def card(self) -> ExternalHarnessCard:
        """Return static implementation identity and declared capabilities."""
        ...

    @property
    def state(self) -> HarnessState:
        """Return the current high-level lifecycle state."""
        ...

    @property
    def session_id(self) -> str | None:
        """Return the provider-native session id once one is available."""
        ...

    async def start(self, context: ExternalHarnessContext) -> None:
        """Start one runtime cycle and settle in ``HarnessState.IDLE``."""
        ...

    async def stop(self) -> None:
        """Stop the cycle, close events, and settle in ``TERMINATED``.

        The operation must be idempotent.
        """
        ...

    def events(self) -> AsyncIterator[HarnessEvent]:
        """Return the cycle-long ordered observation stream.

        The iterator remains open across turn boundaries and ends only when
        the current ``start``/``stop`` cycle closes. Implementations must raise
        ``ExternalHarnessStateError`` if another observation iterator is
        already active.
        """
        ...

    def turn_events(self) -> AsyncIterator[HarnessEvent]:
        """Return one finite turn from the observation stream.

        The iterator waits for the next ``TurnEventKind.STARTED``, yields that
        event and the following ordered events, and ends immediately after
        yielding the matching terminal turn event. It is a convenience view
        over the same logical channel as ``events``; the two methods must not
        be consumed concurrently. Implementations must reject a second active
        iterator with ``ExternalHarnessStateError`` rather than racing it.
        """
        ...

    async def send(
        self,
        content: ExternalHarnessInput,
        *,
        mode: DeliveryMode = DeliveryMode.AUTO,
    ) -> SendReceipt:
        """Accept input without waiting for all resulting turns to finish."""
        ...

    async def abort(self, *, mode: AbortMode = AbortMode.GRACEFUL) -> None:
        """Abort the active turn according to a declared capability."""
        ...

    async def pause(self) -> None:
        """Pause the active turn when ``PAUSE_RESUME`` is supported."""
        ...

    async def resume(self, *, query: ExternalHarnessInput | None = None) -> None:
        """Resume a warm or checkpoint-restored paused turn."""
        ...

    async def export_checkpoint(self) -> HarnessCheckpoint | None:
        """Return the latest versioned provider snapshot for this member.

        Implementations should also publish recoverable checkpoints through
        ``context.checkpoint_sink`` when provider state changes materially.
        """
        ...


@runtime_checkable
class ExternalHarnessProvider(Protocol):
    """Factory SPI used to discover and construct external harnesses."""

    @property
    def card(self) -> ExternalHarnessCard:
        """Return metadata for harnesses created by this provider."""
        ...

    def create(self, config: JsonObject) -> ExternalHarnessProtocol:
        """Validate provider configuration and create an unstarted harness."""
        ...


__all__ = ["ExternalHarnessProtocol", "ExternalHarnessProvider"]
