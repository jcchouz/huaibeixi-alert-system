"""验证运行中 API 快照工具的核心安全逻辑。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.api_validation import count_csv_rows, snapshot_table


class SnapshotCursor:
    """模拟快照所需的数据库游标。"""

    def __init__(self) -> None:
        self.copy_sql = ""

    def __enter__(self) -> "SnapshotCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.copy_sql = sql

    def fetchone(self) -> tuple[int]:
        return (1,)

    def copy_expert(self, sql: str, target: Any) -> None:
        self.copy_sql = sql
        target.write("id,sensor_no\n1,sensor-1\n")


class SnapshotConnection:
    """模拟提供单一快照游标的数据库连接。"""

    def __init__(self) -> None:
        self.snapshot_cursor = SnapshotCursor()

    def cursor(self) -> SnapshotCursor:
        return self.snapshot_cursor


def test_count_csv_rows_handles_quoted_multiline_fields() -> None:
    payload = (
        'id,rule_snapshot\n'
        'alert-1,"{""note"": ""line 1\nline 2""}"\n'
        'alert-2,"{""note"": ""single line""}"\n'
    )

    assert count_csv_rows(payload) == 2


def test_count_csv_rows_returns_zero_for_header_only_snapshot() -> None:
    assert count_csv_rows("id,sensor_no\n") == 0


def test_snapshot_uses_query_copy_for_timescaledb_hypertable(tmp_path: Path) -> None:
    connection = SnapshotConnection()

    snapshot = snapshot_table(
        connection,
        "t_alert_record",
        tmp_path / "alert.csv",
    )

    assert snapshot.row_count == 1
    assert "COPY (SELECT * FROM public.t_alert_record)" in (
        connection.snapshot_cursor.copy_sql
    )
    assert "FORMAT csv" in connection.snapshot_cursor.copy_sql
    assert "BINARY" not in connection.snapshot_cursor.copy_sql
