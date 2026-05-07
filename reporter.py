"""
reporter.py
Thread-safe CSV writer for FLIR PT-Series test results.

Required columns (per spec):
    Command_Type, Target_Pos, Actual_Pos, Delta_Pos,
    Comm_Latency_ms, Mech_Latency_ms

Additional diagnostic columns:
    Stream_Pos, Stream_vs_GetStatus_Delta, Stream_Age_ms, Stream_Notes,
    Pass_Fail, Notes
"""

from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path

FIELDNAMES = [
    "Camera_Model",
    "Command_Type",
    "Target_Pos",
    "Actual_Pos",
    "Delta_Pos",
    "Comm_Latency_ms",
    "Mech_Latency_ms",
    "Stream_Pos",
    "Stream_vs_GetStatus_Delta",
    "Stream_Age_ms",
    "Stream_Notes",
    "Pass_Fail",
    "Notes",
]


class Reporter:
    """Writes one row per PTZ operation to a timestamped CSV file.

    All public methods are thread-safe.
    """

    def __init__(self, results_dir: str = "results", camera_model: str = "") -> None:
        path = Path(results_dir)
        path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._filepath = path / f"results_{timestamp}.csv"
        self._camera_model = camera_model
        self._lock = threading.Lock()

        with open(self._filepath, "w", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FIELDNAMES).writeheader()

        print(f"[Reporter] Writing results to: {self._filepath}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(
        self,
        *,
        command_type: str,
        target_pan_deg: float,
        target_tilt_deg: float,
        actual_pan_deg: float,
        actual_tilt_deg: float,
        comm_latency_ms: float | None = None,
        mech_latency_ms: float | None = None,
        stream_pan_deg: float | None = None,
        stream_tilt_deg: float | None = None,
        stream_delta_pan_deg: float | None = None,
        stream_delta_tilt_deg: float | None = None,
        stream_age_ms: float | None = None,
        stream_notes: str = "",
        pass_fail: str = "",
        notes: str = "",
    ) -> None:
        """Append a single result row to the CSV."""
        delta_pan = abs(target_pan_deg - actual_pan_deg)
        delta_tilt = abs(target_tilt_deg - actual_tilt_deg)

        row = {
            "Camera_Model":    self._camera_model,
            "Command_Type":    command_type,
            "Target_Pos":      f"{target_pan_deg:.4f},{target_tilt_deg:.4f}",
            "Actual_Pos":      f"{actual_pan_deg:.4f},{actual_tilt_deg:.4f}",
            "Delta_Pos":       f"{delta_pan:.4f},{delta_tilt:.4f}",
            "Comm_Latency_ms": f"{comm_latency_ms:.2f}" if comm_latency_ms is not None else "",
            "Mech_Latency_ms": f"{mech_latency_ms:.2f}" if mech_latency_ms is not None else "",
            "Stream_Pos": (
                f"{stream_pan_deg:.4f},{stream_tilt_deg:.4f}"
                if stream_pan_deg is not None and stream_tilt_deg is not None
                else ""
            ),
            "Stream_vs_GetStatus_Delta": (
                f"{stream_delta_pan_deg:.4f},{stream_delta_tilt_deg:.4f}"
                if stream_delta_pan_deg is not None and stream_delta_tilt_deg is not None
                else ""
            ),
            "Stream_Age_ms": f"{stream_age_ms:.2f}" if stream_age_ms is not None else "",
            "Stream_Notes": stream_notes,
            "Pass_Fail":       pass_fail,
            "Notes":           notes,
        }

        with self._lock:
            with open(self._filepath, "a", newline="") as fh:
                csv.DictWriter(fh, fieldnames=FIELDNAMES).writerow(row)

    @property
    def filepath(self) -> Path:
        return self._filepath
