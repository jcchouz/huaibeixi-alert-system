"""阈值定义的数据库访问。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


class ThresholdRepository:
    """封装阈值规则查询和更新。"""

    @staticmethod
    def fetch_rule(cur: Any, threshold_id: str) -> Optional[Dict[str, Any]]:
        """按编号加载单条阈值规则。"""
        cur.execute(
            "SELECT * FROM public.t_alert_threshold WHERE id = %s",
            (threshold_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def fetch_rules(
        cur: Any,
        *,
        threshold_ids: Optional[Sequence[str]] = None,
        physical_quantity: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按指定编号或物理量加载阈值规则。"""
        if threshold_ids:
            cur.execute(
                "SELECT * FROM public.t_alert_threshold WHERE id = ANY(%s)",
                (list(threshold_ids),),
            )
        else:
            cur.execute(
                "SELECT * FROM public.t_alert_threshold WHERE physical_quantity = %s",
                (physical_quantity,),
            )
        return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def update_rule(cur: Any, threshold_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """持久化已校验的阈值和物理量程。"""
        cur.execute(
            """
            UPDATE public.t_alert_threshold
            SET level1_upper = %s,
                level1_lower = %s,
                level2_upper = %s,
                level2_lower = %s,
                level3_upper = %s,
                level3_lower = %s,
                expected_interval_ms = %s,
                range_min = %s,
                range_max = %s,
                update_by = %s,
                update_time = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (
                values["level1_upper"],
                values["level1_lower"],
                values["level2_upper"],
                values["level2_lower"],
                values["level3_upper"],
                values["level3_lower"],
                values["expected_interval_ms"],
                values["range_min"],
                values["range_max"],
                values["update_by"],
                threshold_id,
            ),
        )
        return dict(cur.fetchone())
