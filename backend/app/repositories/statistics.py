from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


class StatisticsRepository:
    @staticmethod
    def list_sensor_rows(cur: Any) -> List[Dict[str, Any]]:
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
                val_std,
                max_time,
                min_time,
                sample_count
            FROM public.t_stat_record
            WHERE COALESCE(channel, '') <> 'wind_rose'
            ORDER BY LOWER(sensor_no), COALESCE(channel, ''),
                     CASE WHEN period_type = '1m' THEN 1 WHEN period_type = '1h' THEN 2 ELSE 3 END ASC,
                     stat_time DESC
            """
        )
        return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def resolve_channel(cur: Any, sensor_no: str) -> str:
        cur.execute(
            """
            SELECT COALESCE(channel, '') AS ch
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s AND COALESCE(channel, '') <> 'wind_rose'
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
            SELECT id, stat_time, val_max, val_min, val_avg, val_std,
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

        if not rows and time_filter:
            cur.execute(
                """
                SELECT id, stat_time, val_max, val_min, val_avg, val_std,
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
    def threshold_series_rows(
        cur: Any,
        *,
        sensor_no: str,
        channel: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 600,
    ) -> List[Dict[str, Any]]:
        cur.execute(
            """
            SELECT id, stat_time, val_max, val_min, val_avg, val_std, max_time, min_time, sample_count
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

        cur.execute(
            """
            SELECT id, stat_time, val_max, val_min, val_avg, val_std, max_time, min_time, sample_count
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s AND COALESCE(channel, '') = %s
            ORDER BY stat_time DESC
            LIMIT 300
            """,
            (sensor_no, channel),
        )
        return list(reversed([dict(row) for row in cur.fetchall()]))

    @staticmethod
    def wind_rose_rows(
        cur: Any,
        *,
        sensor_no: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        where_clauses = ["period_type = '10m'", "channel = 'wind_rose'"]
        params: List[Any] = []
        if sensor_no and sensor_no not in ("all", ""):
            where_clauses.append("LOWER(sensor_no) = %s")
            params.append(sensor_no)
        if start_time is not None:
            where_clauses.append("stat_time >= %s")
            params.append(start_time)
        if end_time is not None:
            where_clauses.append("stat_time <= %s")
            params.append(end_time)

        cur.execute(
            f"""
            SELECT id, sensor_no, stat_time, val_max, val_min, val_avg, val_std,
                   max_time, min_time, sample_count, ext_json
            FROM public.t_stat_record
            WHERE {' AND '.join(where_clauses)}
            ORDER BY stat_time DESC
            """,
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]
