# SPDX-License-Identifier: Apache-2.0
"""Shared failure-mode enumeration used by both chaos backends.

The two backends (toxiproxy + httpx-mock) accept the same shape, so
scenarios stay backend-agnostic.

The canonical scenario:
    Mid-test, toxiproxy injects: 502 for 30s -> 60s slow for 30s ->
    drop-all for 30s -> recover.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    """One of the supported chaos modes."""

    NONE = "none"
    LATENCY = "latency"  # parameterised by latency_ms
    BANDWIDTH = "bandwidth"  # parameterised by bandwidth_kbps
    DOWN = "down"  # drop all connections (502 / connection refused)
    SERVER_ERROR = "server_error"  # synthetic 502 with body
    MID_STREAM_DISCONNECT = "mid_stream_disconnect"  # close socket after N bytes


@dataclass(frozen=True)
class FailureSpec:
    """One concrete failure configuration.

    Args:
        kind:           Which failure mode to apply.
        latency_ms:     For LATENCY: artificial delay per packet.
        bandwidth_kbps: For BANDWIDTH: throttle to N KB/s.
        body_bytes_before_drop: For MID_STREAM_DISCONNECT: how many
                                 bytes pass before closing.
        duration_sec:   How long the failure remains active before
                        ``orchestrate_chaos`` advances to the next slot.
    """

    kind: FailureKind = FailureKind.NONE
    latency_ms: int = 0
    bandwidth_kbps: int = 0
    body_bytes_before_drop: int = 0
    duration_sec: float = 30.0


# The canonical S5 sequence — same on either backend.
S5_TIMELINE: tuple[FailureSpec, ...] = (
    FailureSpec(kind=FailureKind.SERVER_ERROR, duration_sec=30.0),
    FailureSpec(kind=FailureKind.LATENCY, latency_ms=60_000, duration_sec=30.0),
    FailureSpec(kind=FailureKind.DOWN, duration_sec=30.0),
    FailureSpec(kind=FailureKind.NONE, duration_sec=10.0),  # recover
)
