"""Detection rule-version persistence helpers."""

from __future__ import annotations

from typing import Any, Optional


def load_rule_version(cur: Any) -> Optional[int]:
    """Return the singleton threshold-rule version."""

    cur.execute(
        """
        SELECT version
        FROM public.t_detection_rule_version
        WHERE id = 1
        """
    )
    row = cur.fetchone()
    return int(row["version"]) if row else None
