"""Fault injection driven by an environment variable.

AGENTWAL_KILL_AT="<point>:<n>" makes the process SIGKILL itself the n-th time it reaches
<point> (see agentwal.KILL_POINTS). "any:<n>" counts hits of every point. The kill is a
real `kill -9`: no cleanup, no finally blocks, no flushing of user-space buffers.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable

ENV = "AGENTWAL_KILL_AT"


def parse(spec: str | None) -> tuple[str, int] | None:
    if not spec:
        return None
    point, _, n = spec.rpartition(":")
    return point, int(n)


def make_hook(spec: str | None) -> Callable[[str], None] | None:
    target = parse(spec)
    if target is None:
        return None
    want, nth = target
    hits = {"n": 0}

    def hook(point: str) -> None:
        if want in (point, "any"):
            hits["n"] += 1
            if hits["n"] == nth:
                os.kill(os.getpid(), signal.SIGKILL)

    return hook


def from_env() -> Callable[[str], None] | None:
    return make_hook(os.environ.get(ENV))
