"""预警记录和总览计数的数据库访问。"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from app.domain.alert_types import (
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    alert_category_for_type,
    normalize_alert_type,
)
from app.infrastructure.database import get_db_cursor


class AlertRepository:
    """封装预警 SQL 查询和写入操作。"""

    def get_overview_snapshot(self) -> Dict[str, Any]:
        """加载 KPI、类型统计、统计记录数和最近预警快照。"""
        with get_db_cursor() as cur:
            # 预警状态和级别计数。
            cur.execute(
                """
                SELECT
                    COALESCE(MAX(seq), 0) AS max_seq,
                    COUNT(*) AS total_alerts,
                    COUNT(*) FILTER (WHERE end_time IS NULL) AS active_alerts,
                    COUNT(*) FILTER (WHERE alert_status = 0) AS unacked_alerts,
                    COUNT(*) FILTER (WHERE alert_status = 1) AS acked_alerts,
                    COUNT(*) FILTER (WHERE end_time IS NOT NULL) AS resolved_alerts,
                    0 AS suppressed_alerts,
                    COUNT(*) FILTER (WHERE alert_level = 1 AND end_time IS NULL) AS active_l1,
                    COUNT(*) FILTER (WHERE alert_level = 2 AND end_time IS NULL) AS active_l2,
                    COUNT(*) FILTER (WHERE alert_level = 3 AND end_time IS NULL) AS active_l3,
                    COUNT(*) FILTER (WHERE alert_level = 1) AS total_l1,
                    COUNT(*) FILTER (WHERE alert_level = 2) AS total_l2,
                    COUNT(*) FILTER (WHERE alert_level = 3) AS total_l3,
                    COUNT(DISTINCT sensor_no) FILTER (
                        WHERE alert_type IN (%s, %s)
                          AND end_time IS NULL
                    ) AS offline_alert_count
                FROM public.t_alert_record
                """,
                (ALERT_TYPE_OFFLINE, ALERT_TYPE_DAQ_OFFLINE),
            )
            kpi = dict(cur.fetchone())

            # 按预警类型汇总数量。
            cur.execute(
                """
                SELECT alert_type,
                       COUNT(*) AS total_count,
                       COUNT(*) FILTER (WHERE end_time IS NULL) AS active_count
                FROM public.t_alert_record
                GROUP BY alert_type
                ORDER BY total_count DESC
                """
            )
            by_type_rows = [dict(r) for r in cur.fetchall()]

            # 仪表盘使用的辅助计数。
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
        """读取轮询序列之后的增量预警。"""
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
            # 多取一条记录，用于判断是否还有下一页。
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
        alert_category: Optional[int],
        sensor_no: Optional[str],
        keyword: Optional[str],
        page: int,
        page_size: int,
    ) -> Tuple[List[Dict[str, Any]], int, Dict[str, int]]:
        """返回筛选结果、总数和不受状态筛选影响的状态计数。"""
        # 状态汇总和筛选结果共用这些基础筛选条件。
        base_where_clauses = ["1 = 1"]
        base_params: List[Any] = []
        if alert_level is not None:
            base_where_clauses.append("alert_level = %s")
            base_params.append(alert_level)
        if alert_type:
            base_where_clauses.append("alert_type = %s")
            base_params.append(normalize_alert_type(alert_type) or alert_type.strip())
        if alert_category is not None:
            base_where_clauses.append("alert_category = %s")
            base_params.append(alert_category)
        if sensor_no:
            base_where_clauses.append("LOWER(sensor_no) LIKE %s")
            base_params.append(f"%{sensor_no.strip().lower()}%")
        if keyword:
            keyword_like = f"%{keyword.strip()}%"
            base_where_clauses.append(
                "("
                "LOWER(sensor_no) LIKE LOWER(%s) "
                "OR LOWER(COALESCE(channel, '')) LIKE LOWER(%s)"
                ")"
            )
            base_params.extend([keyword_like, keyword_like])

        where_clauses = list(base_where_clauses)
        params = list(base_params)
        if alert_status is not None:
            where_clauses.append("alert_status = %s")
            params.append(alert_status)

        base_where_sql = " AND ".join(base_where_clauses)
        where_sql = " AND ".join(where_clauses)
        offset = (page - 1) * page_size
        with get_db_cursor() as cur:
            # 状态计数不应用 alert_status，确保各状态页签始终可见。
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE alert_status = 0) AS unhandled,
                    COUNT(*) FILTER (WHERE alert_status = 1) AS handled
                FROM public.t_alert_record
                WHERE {base_where_sql}
                """,
                tuple(base_params),
            )
            status_row = cur.fetchone()
            status_counts = {
                "total": int(status_row["total"] or 0),
                "unhandled": int(status_row["unhandled"] or 0),
                "handled": int(status_row["handled"] or 0),
            }

            # 总数和分页记录使用完整筛选条件。
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
                    (end_time IS NULL) DESC,
                    alert_level ASC,
                    seq DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [page_size, offset]),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows, total, status_counts

    def acknowledge(
        self,
        alert_id: str,
        *,
        target_status: int,
        handler: str,
        handle_phone: Optional[str],
        handle_opinion: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """处理单条预警，并更新轮询序列。"""
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                "SELECT * FROM public.t_alert_record WHERE id = %s",
                (alert_id,),
            )
            existing = cur.fetchone()
            if not existing:
                return None
            # 重复确认已处理预警保持幂等。
            if int(existing["alert_status"] or 0) == 1:
                return dict(existing)

            cur.execute(
                """
                UPDATE public.t_alert_record
                SET alert_status = 1,
                    handle_by = %s,
                    handle_phone = %s,
                    handle_time = NOW(),
                    handle_opinion = %s,
                    seq = nextval('public.seq_alert_record')
                WHERE id = %s
                RETURNING *
                """,
                (handler, handle_phone, handle_opinion, alert_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def acknowledge_all(
        self,
        *,
        handler: str,
        handle_phone: Optional[str],
        alert_level: Optional[int],
        alert_category: Optional[int],
    ) -> List[str]:
        """批量处理符合级别或分类条件的未处理预警。"""
        with get_db_cursor(commit=True) as cur:
            where_clauses = ["alert_status = 0"]
            params: List[Any] = [handler, handle_phone]
            if alert_level is not None:
                where_clauses.append("alert_level = %s")
                params.append(alert_level)
            if alert_category is not None:
                where_clauses.append("alert_category = %s")
                params.append(alert_category)

            cur.execute(
                f"""
                UPDATE public.t_alert_record
                SET alert_status = 1,
                    handle_by = %s,
                    handle_phone = %s,
                    handle_time = NOW(),
                    seq = nextval('public.seq_alert_record')
                WHERE {' AND '.join(where_clauses)}
                RETURNING id
                """,
                tuple(params),
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
    ) -> Dict[str, Any]:
        """插入人工创建的预警记录。"""
        alert_category = alert_category_for_type(alert_type)
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO public.t_alert_record (
                    id, sensor_no, threshold_id, trigger_val, create_time,
                    alert_status, channel, seq, alert_type, alert_category,
                    alert_level, end_time
                ) VALUES (
                    %s, %s, %s, %s, NOW(),
                    0, %s, nextval('public.seq_alert_record'), %s, %s,
                    %s, NULL
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
                    alert_category,
                    alert_level,
                ),
            )
            return dict(cur.fetchone())

    def delete(self, alert_id: str) -> bool:
        """删除单条预警，并返回是否实际删除记录。"""
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM public.t_alert_record WHERE id = %s RETURNING id",
                (alert_id,),
            )
            return cur.fetchone() is not None

    @staticmethod
    def _max_seq(cur: Any) -> int:
        """返回最新预警轮询序列。"""
        cur.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM public.t_alert_record")
        return int(cur.fetchone()["m"] or 0)

    @staticmethod
    def total_pages(total: int, page_size: int) -> int:
        """计算分页响应的总页数。"""
        return math.ceil(total / page_size) if page_size > 0 else 1
