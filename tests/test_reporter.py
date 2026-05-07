"""
test_reporter.py — tests for reporter.py

Covers:
  - CSV file creation and header correctness
  - Single-row write with correct values
  - Delta calculation (absolute difference)
  - Optional fields (None → empty string)
  - Thread safety under concurrent writes
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

from reporter import Reporter, FIELDNAMES


@pytest.fixture
def rep(tmp_path: Path) -> Reporter:
    return Reporter(results_dir=str(tmp_path / "results"))


# ---------------------------------------------------------------------------

def test_csv_file_is_created(rep: Reporter) -> None:
    assert rep.filepath.exists()


def test_csv_has_correct_headers(rep: Reporter) -> None:
    with open(rep.filepath, newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == FIELDNAMES


def test_single_row_is_written(rep: Reporter) -> None:
    rep.write(
        command_type="AbsoluteMove",
        target_pan_deg=45.0,
        target_tilt_deg=20.0,
        actual_pan_deg=45.1,
        actual_tilt_deg=19.95,
        comm_latency_ms=123.4,
        mech_latency_ms=55.6,
        pass_fail="PASS",
        notes="test note",
    )

    rows = _read_rows(rep)
    assert len(rows) == 1
    row = rows[0]
    assert row["Command_Type"]    == "AbsoluteMove"
    assert row["Target_Pos"]      == "45.0000,20.0000"
    assert row["Actual_Pos"]      == "45.1000,19.9500"
    assert row["Pass_Fail"]       == "PASS"
    assert row["Notes"]           == "test note"
    assert float(row["Comm_Latency_ms"]) == pytest.approx(123.4, abs=0.01)
    assert float(row["Mech_Latency_ms"]) == pytest.approx(55.6,  abs=0.01)


def test_delta_is_absolute_difference(rep: Reporter) -> None:
    rep.write(
        command_type="AbsoluteMove",
        target_pan_deg=10.0,
        target_tilt_deg=-5.0,
        actual_pan_deg=10.3,
        actual_tilt_deg=-4.8,
    )
    row = _read_rows(rep)[0]
    delta_pan, delta_tilt = [float(x) for x in row["Delta_Pos"].split(",")]
    assert delta_pan  == pytest.approx(0.3, abs=1e-4)
    assert delta_tilt == pytest.approx(0.2, abs=1e-4)


def test_none_latency_fields_become_empty_string(rep: Reporter) -> None:
    rep.write(
        command_type="ContinuousMove",
        target_pan_deg=0.0,
        target_tilt_deg=0.0,
        actual_pan_deg=0.0,
        actual_tilt_deg=0.0,
        comm_latency_ms=None,
        mech_latency_ms=None,
    )
    row = _read_rows(rep)[0]
    assert row["Comm_Latency_ms"] == ""
    assert row["Mech_Latency_ms"] == ""
    assert row["Stream_Pos"] == ""
    assert row["Stream_vs_GetStatus_Delta"] == ""
    assert row["Stream_Age_ms"] == ""


def test_stream_fields_are_written(rep: Reporter) -> None:
    rep.write(
        command_type="AbsoluteMove",
        target_pan_deg=10.0,
        target_tilt_deg=5.0,
        actual_pan_deg=10.1,
        actual_tilt_deg=5.1,
        stream_pan_deg=10.2,
        stream_tilt_deg=5.2,
        stream_delta_pan_deg=0.1,
        stream_delta_tilt_deg=0.1,
        stream_age_ms=42.5,
        stream_notes="ok",
    )
    row = _read_rows(rep)[0]
    assert row["Stream_Pos"] == "10.2000,5.2000"
    assert row["Stream_vs_GetStatus_Delta"] == "0.1000,0.1000"
    assert row["Stream_Age_ms"] == "42.50"
    assert row["Stream_Notes"] == "ok"


def test_multiple_rows_accumulate(rep: Reporter) -> None:
    for i in range(5):
        rep.write(
            command_type="AbsoluteMove",
            target_pan_deg=float(i),
            target_tilt_deg=0.0,
            actual_pan_deg=float(i),
            actual_tilt_deg=0.0,
        )
    assert len(_read_rows(rep)) == 5


def test_thread_safety(rep: Reporter) -> None:
    """50 threads each write 10 rows — final count must be exactly 500."""
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(10):
                rep.write(
                    command_type="AbsoluteMove",
                    target_pan_deg=1.0,
                    target_tilt_deg=0.0,
                    actual_pan_deg=1.0,
                    actual_tilt_deg=0.0,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
    assert len(_read_rows(rep)) == 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_rows(rep: Reporter) -> list[dict]:
    with open(rep.filepath, newline="") as fh:
        return list(csv.DictReader(fh))
