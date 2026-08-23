"""Dependency-free Agent Loop control-flow exceptions."""

from __future__ import annotations


class AgentLoopControlError(Exception):
    """A control-flow marker that must never trigger Provider fallback."""


class ChatRunCancelled(AgentLoopControlError):
    """Raised when a chat run loses cancellation or delivery eligibility."""
