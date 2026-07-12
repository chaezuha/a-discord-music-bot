"""Exceptions shared across command and UI modules."""

from __future__ import annotations


class UserError(Exception):
    """A problem the user can fix; shown as-is in chat."""
