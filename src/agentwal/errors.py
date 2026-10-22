"""Exceptions raised by AgentWAL."""


class AgentWALError(Exception):
    """Base class for all AgentWAL errors."""


class CorruptJournalError(AgentWALError):
    """The journal is unreadable in a way that torn-tail truncation cannot fix."""


class NonDeterminismError(AgentWALError):
    """A replayed call does not match what the journal recorded at that sequence number.

    Raised when the agent code changed between runs, or when it used a value that was
    not journaled (wall clock, random, uuid) to decide what to do next.
    """

    def __init__(self, seq: int, expected: str, got: str, reason: str = "request mismatch"):
        self.seq = seq
        self.expected = expected
        self.got = got
        self.reason = reason
        super().__init__(
            f"non-determinism at seq={seq} ({reason}):\n"
            f"  journal recorded: {expected}\n"
            f"  code now issued : {got}"
        )


class RunMismatchError(AgentWALError):
    """The journal belongs to a different run (run_id, input or code version differ)."""


class ToolError(AgentWALError):
    """A journaled tool call failed. Replayed failures raise this with the recorded message."""
