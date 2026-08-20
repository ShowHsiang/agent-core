# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Errors raised by third-party agent harness protocol implementations."""

from __future__ import annotations


class ExternalHarnessError(RuntimeError):
    """Base error for the external harness protocol boundary."""


class ExternalHarnessStateError(ExternalHarnessError):
    """Raised when a command is invalid for the harness's current state."""


class UnsupportedHarnessCapabilityError(ExternalHarnessError):
    """Raised when the caller requests a capability the harness did not declare."""


class ExternalHarnessProtocolError(ExternalHarnessError):
    """Raised when an implementation violates a protocol invariant."""


__all__ = [
    "ExternalHarnessError",
    "ExternalHarnessProtocolError",
    "ExternalHarnessStateError",
    "UnsupportedHarnessCapabilityError",
]
