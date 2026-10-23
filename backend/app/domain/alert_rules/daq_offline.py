"""Instrument-level offline aggregation rule."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Set


@dataclass(frozen=True)
class DaqOfflineEvaluation:
    """Instrument offline decision consumed by the scan orchestrator."""

    active: bool
    previous_active: bool
    should_create: bool
    should_close: bool


class DaqOfflineRule:
    """Create one instrument alert only when every configured channel is offline."""

    def evaluate(
        self,
        *,
        total_channels: int,
        offline_channels: Set[str],
        previous_active: bool,
        active_alert_exists: bool,
        state_known: bool,
    ) -> DaqOfflineEvaluation:
        active = total_channels > 0 and len(offline_channels) >= total_channels
        should_create = (
            active
            and not previous_active
            and not (active_alert_exists and not state_known)
        )
        return DaqOfflineEvaluation(
            active=active,
            previous_active=previous_active,
            should_create=should_create,
            should_close=previous_active and not active,
        )
