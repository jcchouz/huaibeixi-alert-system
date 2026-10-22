from __future__ import annotations

from datetime import datetime

import pytest

from tools.benchmark_10k import (
    BenchmarkError,
    assess_capacity,
    build_channel_specs,
    build_rate_plan,
    generate_row_batch,
    normalize_sensor_prefix,
    synthetic_value,
    validate_benchmark_options,
)


def test_rate_plan_uses_exact_row_count_and_partial_batches() -> None:
    plan = build_rate_plan(
        rows_per_second=10_000,
        seconds=3,
        batch_size=4_000,
        total_channels=40,
    )

    assert plan.total_rows == 30_000
    assert plan.rows_per_batch == 4_000
    assert plan.batch_count == 8
    assert plan.rows_per_channel == 750.0


def test_channel_specs_are_deterministic_and_prefix_scoped() -> None:
    channels = build_channel_specs("bench10k-", sensors=2, channels_per_sensor=3)

    assert len(channels) == 6
    assert channels[0].sensor_no == "bench10k-sensor-0000"
    assert channels[3].sensor_no == "bench10k-sensor-0001"
    assert [channel.channel for channel in channels[:3]] == ["ch000", "ch001", "ch002"]


def test_generated_batch_cycles_channels_and_spaces_samples() -> None:
    channels = build_channel_specs("bench10k-", sensors=1, channels_per_sensor=2)
    start = datetime(2026, 10, 21, 12, 0, 0)

    rows = generate_row_batch(
        channels,
        start_time=start,
        start_index=0,
        count=4,
        rows_per_second=10_000,
    )

    assert [row[1] for row in rows] == ["ch000", "ch001", "ch000", "ch001"]
    assert rows[0][3] == start
    assert rows[1][3] == start.replace(microsecond=100)
    assert rows[2][3] == start.replace(microsecond=200)
    assert rows[0][2] == 12.0
    assert rows == generate_row_batch(
        channels,
        start_time=start,
        start_index=0,
        count=4,
        rows_per_second=10_000,
    )


def test_synthetic_value_repeats_alarm_and_normal_phases() -> None:
    assert synthetic_value(0, 10_000) == 12.0
    assert synthetic_value(4_999, 10_000) == 12.0
    assert abs(synthetic_value(10_000, 10_000)) < 5.0
    assert synthetic_value(60_000, 10_000) == 12.0


def test_option_validation_rejects_unsafe_or_invalid_values() -> None:
    valid = {
        "rows_per_second": 10_000,
        "seconds": 10,
        "sensors": 10,
        "channels": 4,
        "batch_size": 1_000,
        "sensor_prefix": "bench10k-",
        "cleanup": False,
        "keep_data": False,
    }
    validate_benchmark_options(**valid)

    with pytest.raises(BenchmarkError):
        validate_benchmark_options(**{**valid, "rows_per_second": 0})
    with pytest.raises(BenchmarkError):
        validate_benchmark_options(**{**valid, "sensor_prefix": "production-"})
    with pytest.raises(BenchmarkError):
        validate_benchmark_options(**{**valid, "cleanup": True, "keep_data": True})
    with pytest.raises(BenchmarkError):
        normalize_sensor_prefix("test prefix-")


def test_capacity_assessment_requires_write_and_scan_to_keep_up() -> None:
    passing = assess_capacity(
        target_rows_per_second=10_000,
        expected_rows=100_000,
        processed_rows=100_000,
        write_rows_per_second=9_600,
        scan_rows_per_second=12_000,
    )
    assert passing["keepsUp"] is True

    slow_scan = assess_capacity(
        target_rows_per_second=10_000,
        expected_rows=100_000,
        processed_rows=100_000,
        write_rows_per_second=10_000,
        scan_rows_per_second=9_000,
    )
    assert slow_scan["scanKeptUp"] is False
    assert slow_scan["keepsUp"] is False

    incomplete = assess_capacity(
        target_rows_per_second=10_000,
        expected_rows=100_000,
        processed_rows=99_999,
        write_rows_per_second=10_000,
        scan_rows_per_second=20_000,
    )
    assert incomplete["scanProcessedAllRows"] is False

    slow_latency = assess_capacity(
        target_rows_per_second=10_000,
        expected_rows=100_000,
        processed_rows=100_000,
        write_rows_per_second=10_000,
        scan_rows_per_second=12_000,
        max_scan_latency_seconds=2.5,
        scan_interval_seconds=1.0,
    )
    assert slow_latency["scanLatencyKeptUp"] is False
    assert slow_latency["keepsUp"] is False
