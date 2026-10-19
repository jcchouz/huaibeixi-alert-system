from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.domain.alert_types import (
    ALERT_DIRECTION_NONE,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    normalize_alert_type,
)
from app.infrastructure.database import get_db_cursor


class AlertRepository:
    def get_overview_snapshot(self) -> Dict[str, Any]:
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(MAX(seq), 0) AS max_seq,
                    COUNT(*) AS total_alerts,
                    COUNT(*) FILTER (WHERE alert_status = 0) AS active_alerts,
                    COUNT(*) FILTER (WHERE alert_status = 0) AS unacked_alerts,
                    COUNT(*) FILTER (WHERE alert_status = 1) AS acked_alerts,
                    0 AS resolved_alerts,
                    0 AS suppressed_alerts,
                    COUNT(*) FILTER (WHERE alert_level = 1 AND alert_status = 0) AS active_l1,
                    COUNT(*) FILTER (WHERE alert_level = 2 AND alert_status = 0) AS active_l2,
                    COUNT(*) FILTER (WHERE alert_level = 3 AND alert_status = 0) AS active_l3,
                    COUNT(*) FILTER (WHERE alert_level = 1) AS total_l1,
                    COUNT(*) FILTER (WHERE alert_level = 2) AS total_l2,
                    COUNT(*) FILTER (WHERE alert_level = 3) AS total_l3,
                    COUNT(DISTINCT sensor_no) FILTER (
                        WHERE alert_type IN (%s, %s)
                          AND alert_status = 0
                    ) AS offline_alert_count
                FROM public.t_alert_record
                """,
                (ALERT_TYPE_OFFLINE, ALERT_TYPE_DAQ_OFFLINE),
            )
            kpi = dict(cur.fetchone())

            cur.execute(
                """
                SELECT alert_type,
                       COUNT(*) AS total_count,
                       COUNT(*) FILTER (WHERE alert_status = 0) AS active_count
                FROM public.t_alert_record
                GROUP BY alert_type
                ORDER BY total_count DESC
                """
            )
            by_type_rows = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) AS cnt FROM public.t_stat_record")
            stat_count = int(cur.fetchone()["cnt"] or 0)

            cur.execute(
                """
                SELECT *
                FROM public.t_alert_record
                ORDER BY seq DESC
                LIMIT 8
                """
            )
            recent_rows = [dict(r) for r in cur.fetchall()]

        return {
            "kpi": kpi,
            "by_type_rows": by_type_rows,
            "stat_count": stat_count,
            "recent_rows": recent_rows,
        }

    def poll(self, after_seq: int, limit: int) -> Dict[str, Any]:
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.t_alert_record
                WHERE seq > %s
                ORDER BY seq ASC
                LIMIT %s
                """,
                (after_seq, limit + 1),
            )
            rows = [dict(r) for r in cur.fetchall()]
            has_more = len(rows) > limit
            rows = rows[:limit]

            if rows:
                max_seq = max(int(row["seq"]) for row in rows)
            elif after_seq > 0:
                max_seq = after_seq
            else:
                max_seq = self._max_seq(cur)

            latest_seq = self._max_seq(cur) if after_seq == 0 else max_seq

        return {
            "rows": rows,
            "max_seq": max_seq,
            "latest_seq": latest_seq,
            "has_more": has_more,
        }

    def list(
        self,
        *,
        alert_level: Optional[int],
        alert_status: Optional[int],
        alert_type: Optional[str],
        sensor_no: Optional[str],
        keyword: Optional[str],
        page: int,
        page_size: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        where_clauses = ["1 = 1"]
        params: List[Any] = []
        if alert_level is not None:
            where_clauses.append("alert_level = %s")
            params.append(alert_level)
        if alert_status is not None:
            where_clauses.append("alert_status = %s")
            params.append(alert_status)
        if alert_type:
            where_clauses.append("alert_type = %s")
            params.append(normalize_alert_type(alert_type) or alert_type.strip())
        if sensor_no:
            where_clauses.append("LOWER(sensor_no) LIKE %s")
            params.append(f"%{sensor_no.strip().lower()}%")
        if keyword:
            keyword_like = f"%{keyword.strip()}%"
            where_clauses.append(
                "("
                "LOWER(sensor_no) LIKE LOWER(%s) "
                "OR LOWER(COALESCE(channel, '')) LIKE LOWER(%s)"
                ")"
            )
            params.extend([keyword_like, keyword_like])

        where_sql = " AND ".join(where_clauses)
        offset = (page - 1) * page_size
        with get_db_cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS cnt FROM public.t_alert_record WHERE {where_sql}",
                tuple(params),
            )
            total = int(cur.fetchone()["cnt"] or 0)

            cur.execute(
                f"""
                SELECT *
                FROM public.t_alert_record
                WHERE {where_sql}
                ORDER BY
                    alert_status ASC,
                    alert_level ASC,
                    seq DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [page_size, offset]),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows, total

    def acknowledge(
        self,
        alert_id: str,
        *,
        target_status: int,
        actor: str,
    ) -> Optional[Dict[str, Any]]:
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                "SELECT * FROM public.t_alert_record WHERE id = %s",
                (alert_id,),
            )
            existing = cur.fetchone()
            if not existing:
                return None
            if int(existing["alert_status"] or 0) == 1:
                return dict(existing)

            cur.execute(
                """
                UPDATE public.t_alert_record
                SET alert_status = 1,
                    ack_by = %s,
                    ack_time = NOW(),
                    seq = nextval('public.seq_alert_record')
                WHERE id = %s
                RETURNING *
                """,
                (actor, alert_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def acknowledge_all(
        self,
        *,
        actor: str,
        alert_level: Optional[int],
    ) -> List[str]:
        with get_db_cursor(commit=True) as cur:
            if alert_level is not None:
                cur.execute(
                    """
                    UPDATE public.t_alert_record
                    SET alert_status = 1,
                        ack_by = %s,
                        ack_time = NOW(),
                        seq = nextval('public.seq_alert_record')
                    WHERE alert_status = 0 AND alert_level = %s
                    RETURNING id
                    """,
                    (actor, alert_level),
                )
            else:
                cur.execute(
                    """
                    UPDATE public.t_alert_record
                    SET alert_status = 1,
                        ack_by = %s,
                        ack_time = NOW(),
                        seq = nextval('public.seq_alert_record')
                    WHERE alert_status = 0
                    RETURNING id
                    """,
                    (actor,),
                )
            return [str(row["id"]) for row in cur.fetchall()]

    def create(
        self,
        *,
        alert_id: str,
        sensor_no: str,
        channel: str,
        threshold_id: Optional[str],
        alert_type: str,
        alert_level: int,
        trigger_val: float,
        direction: str = ALERT_DIRECTION_NONE,
    ) -> Dict[str, Any]:
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO public.t_alert_record (
                    id, sensor_no, threshold_id, trigger_val, create_time,
                    alert_status, remark, channel, seq, alert_type, alert_level,
                    direction, trigger_count
                ) VALUES (
                    %s, %s, %s, %s, NOW(),
                    0, '', %s, nextval('public.seq_alert_record'), %s, %s,
                    %s, 1
                )
                RETURNING *
                """,
                (
                    alert_id,
                    sensor_no,
                    threshold_id,
                    trigger_val,
                    channel,
                    alert_type,
                    alert_level,
                    direction,
                ),
            )
            return dict(cur.fetchone())

    def delete(self, alert_id: str) -> bool:
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM public.t_alert_record WHERE id = %s RETURNING id",
                (alert_id,),
            )
            return cur.fetchone() is not None

    @staticmethod
    def _max_seq(cur: Any) -> int:
        cur.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM public.t_alert_record")
        return int(cur.fetchone()["m"] or 0)

    @staticmethod
    def total_pages(total: int, page_size: int) -> int:
        return math.ceil(total / page_size) if page_size > 0 else 1

    @staticmethod
    def now_string() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
