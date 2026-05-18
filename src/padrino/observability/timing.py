"""Wall-clock helpers for the observability layer (US-059).

Factored out so consumers under ``padrino.runner.game_runner`` — which must
not import ``time`` / ``datetime`` directly per the purity firewall — can
still record phase durations through a single seam.

Impure module: imports ``time``. Pure-core code never touches it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from padrino.observability.metrics import record_phase_duration


@contextmanager
def time_phase(ruleset: str, phase_kind: str) -> Iterator[None]:
    """Time the wrapped block and observe its duration on the phase histogram."""
    start = time.monotonic()
    try:
        yield
    finally:
        record_phase_duration(
            ruleset=ruleset,
            phase_kind=phase_kind,
            duration_s=time.monotonic() - start,
        )


__all__ = ["time_phase"]
