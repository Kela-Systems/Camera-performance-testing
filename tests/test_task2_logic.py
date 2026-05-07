"""
test_task2_logic.py — tests for task2_latency.py

Covers:
  - MechLatencyMonitor: detects first motion, stays quiet when still
  - LatencySummary: stat calculations (mean, std, percentiles)
  - Single-cycle latency measurement (comm + mech)
  - Jitter (std-dev) over multiple cycles
  - scapy unavailability degrades gracefully
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from task2_latency import (
    MechLatencyMonitor,
    LatencySummary,
    _run_single_cycle,
    _start_sniffer,
)
from tests.conftest import make_status


# ---------------------------------------------------------------------------
# MechLatencyMonitor
# ---------------------------------------------------------------------------

class TestMechLatencyMonitor:

    def test_detects_motion_above_threshold(self, mock_cm) -> None:
        """
        Camera starts at (0°, 0°); after a short delay the mock reports
        a 1° pan change.  The monitor must capture a non-None timestamp.
        """
        call_count = {"n": 0}
        t_motion = time.perf_counter() + 0.06   # motion appears after ~60 ms

        def fake_get_pos():
            call_count["n"] += 1
            if time.perf_counter() >= t_motion:
                return 1.0, 0.0   # 1° pan offset — well above 0.05° threshold
            return 0.0, 0.0

        mock_cm.get_position_deg = fake_get_pos

        monitor = MechLatencyMonitor(
            mock_cm,
            baseline_pan_deg=0.0,
            baseline_tilt_deg=0.0,
            threshold_deg=0.05,
            poll_hz=50,
        )
        monitor.start()
        time.sleep(0.15)   # give the monitor time to observe the change
        monitor.stop()

        ts = monitor.first_motion_timestamp()
        assert ts is not None, "Monitor should have detected motion"
        assert ts >= t_motion - 0.1    # timestamp must be at or after motion start

    def test_no_motion_leaves_timestamp_none(self, mock_cm) -> None:
        """Camera stays at baseline — monitor must not fire."""
        mock_cm.get_position_deg = lambda: (0.0, 0.0)

        monitor = MechLatencyMonitor(
            mock_cm,
            baseline_pan_deg=0.0,
            baseline_tilt_deg=0.0,
            threshold_deg=0.05,
            poll_hz=50,
        )
        monitor.start()
        time.sleep(0.1)
        monitor.stop()

        assert monitor.first_motion_timestamp() is None

    def test_detects_tilt_motion(self, mock_cm) -> None:
        """Motion on tilt axis also triggers the monitor."""
        call_count = {"n": 0}

        def fake_get_pos():
            call_count["n"] += 1
            return 0.0, 1.0 if call_count["n"] > 2 else 0.0  # tilt jumps after 2 polls

        mock_cm.get_position_deg = fake_get_pos

        monitor = MechLatencyMonitor(
            mock_cm,
            baseline_pan_deg=0.0,
            baseline_tilt_deg=0.0,
            threshold_deg=0.05,
            poll_hz=100,
        )
        monitor.start()
        time.sleep(0.1)
        monitor.stop()

        assert monitor.first_motion_timestamp() is not None

    def test_motion_below_threshold_ignored(self, mock_cm) -> None:
        """Sub-threshold jitter (0.01°) must not trigger the monitor."""
        mock_cm.get_position_deg = lambda: (0.01, 0.0)   # 0.01° < 0.05° threshold

        monitor = MechLatencyMonitor(
            mock_cm,
            baseline_pan_deg=0.0,
            baseline_tilt_deg=0.0,
            threshold_deg=0.05,
            poll_hz=100,
        )
        monitor.start()
        time.sleep(0.1)
        monitor.stop()

        assert monitor.first_motion_timestamp() is None


# ---------------------------------------------------------------------------
# LatencySummary stats
# ---------------------------------------------------------------------------

class TestLatencySummary:

    def _summary_with_comm(self, values: list[float]) -> LatencySummary:
        s = LatencySummary()
        s.comm_latency_ms = values
        return s

    def test_mean(self) -> None:
        s = self._summary_with_comm([10.0, 20.0, 30.0])
        assert s.comm_stats()["mean_ms"] == pytest.approx(20.0)

    def test_std(self) -> None:
        s = self._summary_with_comm([10.0, 20.0, 30.0])
        assert s.comm_stats()["std_ms"] == pytest.approx(np.std([10, 20, 30]))

    def test_min_max(self) -> None:
        s = self._summary_with_comm([5.0, 15.0, 25.0])
        stats = s.comm_stats()
        assert stats["min_ms"] == pytest.approx(5.0)
        assert stats["max_ms"] == pytest.approx(25.0)

    def test_p95(self) -> None:
        values = list(range(1, 101))            # 1 … 100 ms
        s = self._summary_with_comm([float(v) for v in values])
        assert s.comm_stats()["p95_ms"] == pytest.approx(np.percentile(values, 95))

    def test_empty_returns_empty_dict(self) -> None:
        s = LatencySummary()
        assert s.comm_stats() == {}
        assert s.mech_stats()  == {}

    def test_wire_stats_with_data(self) -> None:
        s = LatencySummary()
        s.wire_rtt_ms          = [10.0, 20.0, 30.0]
        s.retransmission_count = 2
        ws = s.wire_stats()
        assert ws["mean_ms"]         == pytest.approx(20.0)
        assert ws["retransmissions"] == pytest.approx(2.0)

    def test_jitter_over_100_cycles_is_std(self) -> None:
        """std-dev of uniformly spaced values has a known formula."""
        n      = 100
        values = [float(i) for i in range(n)]
        s      = self._summary_with_comm(values)
        assert s.comm_stats()["std_ms"] == pytest.approx(np.std(values), rel=1e-6)


# ---------------------------------------------------------------------------
# Single-cycle measurement
# ---------------------------------------------------------------------------

class TestSingleCycle:

    def test_comm_latency_is_recorded(self, mock_cm, tmp_path) -> None:
        """Comm_Latency_ms must be a positive float after a successful move."""
        mock_cm.ptz.GetStatus.return_value = make_status(0.0, 0.0, "IDLE")
        mock_cm.get_position_deg = lambda: (0.0, 0.0)

        summary = LatencySummary()
        result = _run_single_cycle(
            mock_cm, summary,
            threshold_deg=0.2,
            mech_threshold_deg=0.05,
            mech_poll_hz=20,
        )

        assert result.comm_latency_ms is not None
        assert result.comm_latency_ms > 0
        assert len(summary.comm_latency_ms) == 1

    def test_passed_when_actual_matches_target(self, mock_cm) -> None:
        """All moves that end at (0,0) while target is also (0,0) → PASS."""
        mock_cm.ptz.GetStatus.return_value = make_status(0.0, 0.0, "IDLE")
        mock_cm.get_position_deg = lambda: (0.0, 0.0)

        summary = LatencySummary()
        with patch("task2_latency.random.uniform", return_value=0.0):
            result = _run_single_cycle(
                mock_cm, summary,
                threshold_deg=0.2,
                mech_threshold_deg=0.05,
                mech_poll_hz=20,
            )
        assert result.passed is True

    def test_failed_when_offset_exceeds_spec(self, mock_cm) -> None:
        """Camera reports (0,0) but target is (90°, 0°) → FAIL."""
        mock_cm.ptz.GetStatus.return_value = make_status(0.0, 0.0, "IDLE")
        mock_cm.get_position_deg = lambda: (0.0, 0.0)

        summary = LatencySummary()
        # Force target to 0.5 normalized = 90°
        with patch("task2_latency.random.uniform", return_value=0.5):
            result = _run_single_cycle(
                mock_cm, summary,
                threshold_deg=0.2,
                mech_threshold_deg=0.05,
                mech_poll_hz=20,
            )
        assert result.passed is False

    def test_fault_on_absolute_move_marks_failed(self, mock_cm) -> None:
        from zeep.exceptions import Fault
        mock_cm.ptz.AbsoluteMove.side_effect = Fault("MovingPTZ")
        # Stop succeeds (for retry path)
        mock_cm.ptz.Stop.return_value = None
        # Second AbsoluteMove also faults to force skip
        mock_cm.ptz.AbsoluteMove.side_effect = Fault("MovingPTZ")

        summary = LatencySummary()
        result = _run_single_cycle(
            mock_cm, summary,
            threshold_deg=0.2,
            mech_threshold_deg=0.05,
            mech_poll_hz=20,
        )
        assert result.passed is False


# ---------------------------------------------------------------------------
# scapy graceful degradation
# ---------------------------------------------------------------------------

class TestScapyDegradation:

    def test_permission_error_returns_none(self) -> None:
        # AsyncSniffer is imported inside _start_sniffer, so patch it at its
        # source location (scapy.all) rather than at the task2_latency module.
        with patch("scapy.all.AsyncSniffer", side_effect=PermissionError("no root")):
            sniffer = _start_sniffer("lo0", "127.0.0.1", 8080)
        assert sniffer is None

    def test_import_error_returns_none(self, monkeypatch) -> None:
        """Simulate scapy not installed."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "scapy" in name:
                raise ImportError("No module named 'scapy'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        sniffer = _start_sniffer("lo0", "127.0.0.1", 8080)
        assert sniffer is None
