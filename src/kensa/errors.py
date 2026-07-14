"""Shared Kensa exception types."""

from __future__ import annotations


class KensaCaseError(Exception):
    """Raised when a Kensa case contract is violated."""


class KensaTimeoutError(TimeoutError):
    """Raised when a bounded Kensa operation times out."""
