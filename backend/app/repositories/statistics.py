"""持久化监测统计数据的数据库查询。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


class StatisticsRepository:
    """封装统计服务使用的 t_stat_record 查询。"""

    @staticmethod
    def list_sensor_rows(cur: Any) -> List[Dict[str, Any]]:
        """返回每个传感器和通道的最新统计记录。"""
        cur.execute(
            """
            SELECT DISTINCT ON (LOWER(sensor_no), COALESCE(channel, ''))
                id,
                LOWER(sensor_no) AS sensor_no,
                COALESCE(channel, '') AS channel,
                period_type,
                stat_time AS latest_stat_time,
                val_max,
                val_min,
                val_avg,
                max_time,
                min_time,
                sample_count
            FROM public.t_stat_record
            ORDER BY LOWER(sensor_no), COALESCE(channel, ''),
                     stat_time DESC
            """
        )
        return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def resolve_channel(cur: Any, sensor_no: str) -> str:
        """接口未指定通道时解析默认通道。"""
        cur.execute(
            """
            SELECT COALESCE(channel, '') AS ch
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s
            LIMIT 1
            """,
            (sensor_no,),
        )
        row = cur.fetchone()
        return str(row["ch"]) if row else ""

    @staticmethod
    def extremes_rows(
        cur: Any,
        *,
        sensor_no: str,
        channel: str,
        period_type: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """按时间升序返回有限数量的统计记录。"""
        params: List[Any] = [sensor_no, channel, period_type]
        time_filter = ""
        if start_time is not None:
            time_filter += " AND stat_time >= %s"
            params.append(start_time)
        if end_time is not None:
            time_filter += " AND stat_time <= %s"
            params.append(end_time)

        cur.execute(
            f"""
            SELECT id, stat_time, val_max, val_min, val_avg,
                   max_time, min_time, sample_count
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s
              AND COALESCE(channel, '') = %s
              AND period_type = %s
              {time_filter}
            ORDER BY stat_time DESC
            LIMIT %s
            """,
            tuple(params + [limit]),
        )
        rows = [dict(row) for row in cur.fetchall()]

        # 窄时间范围可能没有数据，此时回退到最近历史记录。
        if not rows and time_filter:
            cur.execute(
                """
                SELECT id, stat_time, val_max, val_min, val_avg,
                       max_time, min_time, sample_count
                FROM public.t_stat_record
                WHERE LOWER(sensor_no) = %s
                  AND COALESCE(channel, '') = %s
                  AND period_type = %s
                ORDER BY stat_time DESC
                LIMIT %s
                """,
                (sensor_no, channel, period_type, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return list(reversed(rows))

    @staticmethod
    def extremes_summary(
        cur: Any,
        *,
        sensor_no: str,
        channel: str,
        period_type: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> Dict[str, Any]:
        """汇总指定时间范围内的极值和加权平均值。"""
        params: List[Any] = [sensor_no, channel, period_type]
        time_filter = ""
        if start_time is not None:
            time_filter += " AND stat_time >= %s"
            params.append(start_time)
        if end_time is not None:
            time_filter += " AND stat_time <= %s"
            params.append(end_time)
        cur.execute(
            f"""
            SELECT
                MAX(val_max) AS window_max,
                (ARRAY_AGG(max_time ORDER BY val_max DESC NULLS LAST))[1]
                    AS window_max_time,
                MIN(val_min) AS window_min,
                (ARRAY_AGG(min_time ORDER BY val_min ASC NULLS LAST))[1]
                    AS window_min_time,
                SUM(val_avg * sample_count) / NULLIF(SUM(sample_count), 0)
                    AS window_avg,
                COALESCE(SUM(sample_count), 0) AS total_samples,
                COUNT(*) AS bucket_count
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s
              AND COALESCE(channel, '') = %s
              AND period_type = %s
              {time_filter}
            """,
            tuple(params),
        )
        return dict(cur.fetchone())

    @staticmethod
    def threshold_series_rows(
        cur: Any,
        *,
        sensor_no: str,
        channel: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 600,
    ) -> List[Dict[str, Any]]:
        """返回最近的一分钟统计数据，用于阈值模拟。"""
        cur.execute(
            """
            SELECT id, stat_time, val_max, val_min, val_avg, max_time, min_time, sample_count
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s AND COALESCE(channel, '') = %s
              AND period_type = '1m'
              AND stat_time >= %s AND stat_time <= %s
            ORDER BY stat_time ASC
            LIMIT %s
            """,
            (sensor_no, channel, start_time, end_time, limit),
        )
        rows = [dict(row) for row in cur.fetchall()]
        if rows:
            return rows

        # 指定日期没有数据时，仍用最近数据保证模拟可用。
        cur.execute(
            """
            SELECT id, stat_time, val_max, val_min, val_avg, max_time, min_time, sample_count
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s AND COALESCE(channel, '') = %s
            ORDER BY stat_time DESC
            LIMIT 300
            """,
            (sensor_no, channel),
        )
        return list(reversed([dict(row) for row in cur.fetchall()]))
