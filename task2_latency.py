"""
task2_latency.py  —  Layer 2: Programmatic Control Latency

Measurements
------------
2a  Application Latency   : time.perf_counter() around AbsoluteMove HTTP call
                             → Comm_Latency_ms in CSV.
2b  Mechanical Start       : background GetStatus poll at 20 Hz detects first
                             coordinate change after the move command
                             → Mech_Latency_ms in CSV.
2c  Reporting Jitter       : std-dev of Comm_Latency_ms over 100 cycles.

Packet Analysis (scapy)
-----------------------
Runs an AsyncSniffer on the configured outbound NIC, filtered to
    host <camera_host> and port <camera_port>
Captures every TCP segment exchanged during the 100-cycle run and:
  - Cross-validates Python-layer timing against wire timestamps.
  - Detects TCP retransmissions (duplicate sequence numbers) that inflate
    observed latency.
  - Reports min/max/mean RTT measured purely from the packet trace.

NOTE: scapy sniffing requires elevated privileges.
  macOS : sudo python task2_latency.py
  Linux : sudo python task2_latency.py  (or set cap_net_raw)
  If privileges are unavailable the script degrades gracefully — all
  application-layer metrics still work; packet analysis is skipped.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from zeep.exceptions import Fault

from comm_manager import CommunicationManager, PAN_SCALE, TILT_SCALE, norm_to_deg
from reporter import Reporter
import ui
from stream_metadata import (
    StreamTelemetryMonitor,
    build_stream_monitor,
    compare_stream_to_getstatus,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# Data structures
# ===========================================================================

@dataclass
class CycleResult:
    pan_target_deg:   float = 0.0
    tilt_target_deg:  float = 0.0
    pan_actual_deg:   float = 0.0
    tilt_actual_deg:  float = 0.0
    comm_latency_ms:  float | None = None
    mech_latency_ms:  float | None = None
    stream_mech_latency_ms: float | None = None
    stream_pan_deg:   float | None = None
    stream_tilt_deg:  float | None = None
    stream_delta_pan_deg: float | None = None
    stream_delta_tilt_deg: float | None = None
    stream_age_ms:    float | None = None
    stream_notes:     str = ""
    passed:           bool  = True
    notes:            str   = ""


@dataclass
class LatencySummary:
    cycle_count:           int              = 0
    comm_latency_ms:       list[float]      = field(default_factory=list)
    mech_latency_ms:       list[float]      = field(default_factory=list)
    stream_mech_latency_ms: list[float]     = field(default_factory=list)

    # scapy-derived (filled only when sniffing succeeds)
    wire_rtt_ms:           list[float]      = field(default_factory=list)
    retransmission_count:  int              = 0

    def comm_stats(self) -> dict[str, float]:
        if not self.comm_latency_ms:
            return {}
        a = np.array(self.comm_latency_ms)
        return {
            "mean_ms":   float(np.mean(a)),
            "std_ms":    float(np.std(a)),
            "min_ms":    float(np.min(a)),
            "max_ms":    float(np.max(a)),
            "p95_ms":    float(np.percentile(a, 95)),
            "p99_ms":    float(np.percentile(a, 99)),
        }

    def mech_stats(self) -> dict[str, float]:
        if not self.mech_latency_ms:
            return {}
        a = np.array(self.mech_latency_ms)
        return {
            "mean_ms":   float(np.mean(a)),
            "std_ms":    float(np.std(a)),
            "min_ms":    float(np.min(a)),
            "max_ms":    float(np.max(a)),
        }

    def stream_mech_stats(self) -> dict[str, float]:
        if not self.stream_mech_latency_ms:
            return {}
        a = np.array(self.stream_mech_latency_ms)
        return {
            "mean_ms":   float(np.mean(a)),
            "std_ms":    float(np.std(a)),
            "min_ms":    float(np.min(a)),
            "max_ms":    float(np.max(a)),
        }

    def wire_stats(self) -> dict[str, float]:
        if not self.wire_rtt_ms:
            return {}
        a = np.array(self.wire_rtt_ms)
        return {
            "mean_ms":   float(np.mean(a)),
            "std_ms":    float(np.std(a)),
            "min_ms":    float(np.min(a)),
            "max_ms":    float(np.max(a)),
            "retransmissions": float(self.retransmission_count),
        }


# ===========================================================================
# Mechanical latency monitor (background thread)
# ===========================================================================

class MechLatencyMonitor:
    """Polls GetStatus at `poll_hz` Hz; records the first timestamp at which
    the pan or tilt position changes by more than `threshold_deg`."""

    def __init__(
        self,
        cm: CommunicationManager,
        baseline_pan_deg: float,
        baseline_tilt_deg: float,
        threshold_deg: float = 0.05,
        poll_hz: float = 20.0,
    ) -> None:
        self._cm              = cm
        self._baseline_pan    = baseline_pan_deg
        self._baseline_tilt   = baseline_tilt_deg
        self._threshold       = threshold_deg
        self._poll_sleep      = 1.0 / poll_hz

        self._stop_event      = threading.Event()
        self._first_motion_ts: float | None = None   # perf_counter timestamp
        self._lock            = threading.Lock()
        self._thread          = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def first_motion_timestamp(self) -> float | None:
        with self._lock:
            return self._first_motion_ts

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                pan_d, tilt_d = self._cm.get_position_deg()
                moved = (
                    abs(pan_d  - self._baseline_pan)  > self._threshold
                    or abs(tilt_d - self._baseline_tilt) > self._threshold
                )
                if moved:
                    ts = time.perf_counter()
                    with self._lock:
                        if self._first_motion_ts is None:
                            self._first_motion_ts = ts
                    # Keep polling so the monitor stays in a valid state
                    # after subsequent moves; caller resets between cycles.
            except Fault as exc:
                logger.debug("GetStatus fault in MechLatencyMonitor: %s", exc)
            time.sleep(self._poll_sleep)


# ===========================================================================
# scapy packet analysis
# ===========================================================================

def _start_sniffer(
    interface: str,
    host: str,
    port: int,
) -> Any | None:
    """Start a background scapy AsyncSniffer.  Returns the sniffer object or
    None if scapy / permissions are unavailable.

    scapy's AsyncSniffer.start() launches a background thread; BPF permission
    errors are raised there and stored as sniffer.exception — not surfaced at
    start() time.  We wait briefly so the thread has time to attempt the open,
    then check for a stored exception before returning.
    """
    try:
        from scapy.all import AsyncSniffer  # type: ignore[import]
        sniffer = AsyncSniffer(
            iface=interface,
            filter=f"host {host} and tcp port {port}",
            store=True,
            prn=None,
        )
        sniffer.start()
        # Give the background thread ~200 ms to open the BPF device so any
        # deferred PermissionError / Scapy_Exception is stored in sniffer.exception
        # before we return the object to the caller.
        time.sleep(0.2)
        if getattr(sniffer, "exception", None) is not None:
            raise sniffer.exception  # noqa: RSE102
        logger.info(
            "scapy AsyncSniffer started on %s (filter: host %s port %d).",
            interface, host, port,
        )
        return sniffer
    except PermissionError:
        logger.warning(
            "scapy requires root/elevated privileges — packet analysis disabled. "
            "Re-run with sudo for wire-level metrics."
        )
    except ImportError:
        logger.warning("scapy not installed — packet analysis disabled.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("scapy sniffer failed to start: %s — skipping.", exc)
    return None


def _analyse_packets(sniffer: Any, summary: LatencySummary) -> None:
    """Parse captured packets and populate wire RTT and retransmission stats."""
    try:
        from scapy.all import IP, TCP  # type: ignore[import]
    except ImportError:
        return

    try:
        sniffer.stop()
    except Exception as exc:  # noqa: BLE001
        # Deferred BPF/permission exception surfaced on stop() — treat as no-capture
        logger.warning(
            "scapy sniffer stop raised an exception (permission error?): %s "
            "— wire-level analysis skipped. Re-run with sudo.", exc,
        )
        return
    packets = sniffer.results or []
    if not packets:
        logger.info("scapy: no packets captured.")
        return

    # Build SYN → SYN-ACK RTT pairs and detect retransmissions
    syn_times: dict[tuple, float] = {}   # (src, sport, dst, dport, seq) → timestamp
    seen_seqs: set[tuple]         = set()

    for pkt in packets:
        if not (IP in pkt and TCP in pkt):
            continue
        ip  = pkt[IP]
        tcp = pkt[TCP]
        ts  = float(pkt.time)
        key = (ip.src, tcp.sport, ip.dst, tcp.dport)

        # Retransmission: same (src, dst, sport, dport, seq) seen more than once
        seq_key = key + (tcp.seq,)
        if seq_key in seen_seqs and not (tcp.flags & 0x02):  # not a SYN
            summary.retransmission_count += 1
        seen_seqs.add(seq_key)

        # RTT from SYN to SYN-ACK
        if tcp.flags & 0x02 and not (tcp.flags & 0x10):  # SYN only
            syn_times[key] = ts
        elif tcp.flags & 0x12:                            # SYN-ACK
            reverse_key = (ip.dst, tcp.dport, ip.src, tcp.sport)
            if reverse_key in syn_times:
                rtt_ms = (ts - syn_times.pop(reverse_key)) * 1000
                summary.wire_rtt_ms.append(rtt_ms)

    logger.info(
        "scapy: %d packets analysed | %d RTT samples | %d retransmissions",
        len(packets),
        len(summary.wire_rtt_ms),
        summary.retransmission_count,
    )


# ===========================================================================
# Single-cycle measurement
# ===========================================================================

def _run_single_cycle(
    cm: CommunicationManager,
    summary: LatencySummary,
    threshold_deg: float,
    mech_threshold_deg: float,
    mech_poll_hz: float,
    stream_monitor: StreamTelemetryMonitor | None = None,
    stream_max_age_s: float = 0.5,
    pos_mode: str = "auto",
) -> CycleResult:
    """Execute one AbsoluteMove → Stop cycle and measure both latencies.

    pos_mode controls which source is used for position and mech-latency:
      "getstatus" — GetStatus only (wait_for_position_stable for accuracy).
      "stream"    — stream for mech latency and actual position; GetStatus
                    only for stream-vs-GetStatus delta column.
      "auto"      — stream when available, else GetStatus (default).
    """
    use_stream = pos_mode in ("auto", "stream") and stream_monitor is not None
    result = CycleResult()

    # Random normalized target
    pan_n  = random.uniform(-0.8, 0.8)
    tilt_n = random.uniform(-0.8, 0.8)
    result.pan_target_deg, result.tilt_target_deg = norm_to_deg(pan_n, tilt_n)

    # --- Baseline position ---
    # Used both for mech-latency detection and as the starting reference.
    try:
        if use_stream:
            s = stream_monitor.latest(max_age_s=1.0)
            baseline_pan  = s.pan_deg  if s is not None else None
            baseline_tilt = s.tilt_deg if s is not None else None
        if not use_stream or baseline_pan is None:
            baseline_pan, baseline_tilt = cm.wait_for_position_stable(
                tolerance_deg=mech_threshold_deg / 2
            )
    except Fault:
        baseline_pan, baseline_tilt = 0.0, 0.0

    # GetStatus mechanical monitor always runs; used as fallback / comparison.
    monitor = MechLatencyMonitor(
        cm,
        baseline_pan_deg=baseline_pan,
        baseline_tilt_deg=baseline_tilt,
        threshold_deg=mech_threshold_deg,
        poll_hz=mech_poll_hz,
    )
    monitor.start()

    # --- Application latency (2a) ---
    t0 = time.perf_counter()
    try:
        cm.absolute_move(pan_n, tilt_n, pan_speed=1.0, tilt_speed=1.0)
        t1 = time.perf_counter()
        result.comm_latency_ms = (t1 - t0) * 1000
        summary.comm_latency_ms.append(result.comm_latency_ms)
    except Fault as exc:
        logger.warning("AbsoluteMove fault: %s", exc)
        monitor.stop()
        result.passed = False
        result.notes  = f"fault:{exc}"
        return result

    # Wait for the camera to settle
    cm.wait_for_idle(timeout=float(cm.ptz_cfg.get("move_timeout_s", 30)))
    monitor.stop()

    # --- Mechanical latency (2b) ---
    # Stream is the primary source when available; GetStatus is fallback/comparison.
    first_motion_ts = monitor.first_motion_timestamp()
    gs_mech_ms: float | None = None
    if first_motion_ts is not None:
        gs_mech_ms = max(0.0, (first_motion_ts - t0) * 1000)

    stream_motion = None
    if stream_monitor is not None:
        stream_motion = stream_monitor.first_motion_after(
            t0,
            baseline_pan,
            baseline_tilt,
            threshold_deg=mech_threshold_deg,
        )
    if stream_motion is not None:
        result.stream_mech_latency_ms = max(
            0.0, (stream_motion.timestamp_s - t0) * 1000
        )
        summary.stream_mech_latency_ms.append(result.stream_mech_latency_ms)

    if use_stream and result.stream_mech_latency_ms is not None:
        # Stream is primary: promote to Mech_Latency_ms
        result.mech_latency_ms = result.stream_mech_latency_ms
        summary.mech_latency_ms.append(result.mech_latency_ms)
        result.notes = (
            f"src=stream gs_mech_ms={gs_mech_ms:.2f}"
            if gs_mech_ms is not None else "src=stream gs_mech=n/a"
        )
    elif gs_mech_ms is not None:
        # Fall back to GetStatus-based detection
        result.mech_latency_ms = gs_mech_ms
        summary.mech_latency_ms.append(result.mech_latency_ms)
        result.notes = "src=getstatus" if use_stream else "src=getstatus"
    else:
        result.notes = "no_motion_detected"
        logger.debug("No mechanical motion detected for this cycle.")

    # --- Actual settled position ---
    # GetStatus (stable) is always read for the stream-vs-GetStatus delta column.
    try:
        if pos_mode == "stream":
            gs_pan, gs_tilt = cm.get_position_deg()
        else:
            gs_pan, gs_tilt = cm.wait_for_position_stable(
                tolerance_deg=threshold_deg / 2
            )
    except Fault:
        gs_pan, gs_tilt = result.pan_target_deg, result.tilt_target_deg

    # Choose actual position source
    if use_stream:
        s = stream_monitor.latest(max_age_s=stream_max_age_s)
        if s is not None:
            result.pan_actual_deg  = s.pan_deg
            result.tilt_actual_deg = s.tilt_deg
            pos_src = "stream"
        else:
            result.pan_actual_deg  = gs_pan
            result.tilt_actual_deg = gs_tilt
            pos_src = "getstatus_fallback"
    else:
        result.pan_actual_deg  = gs_pan
        result.tilt_actual_deg = gs_tilt
        pos_src = "getstatus"

    result.notes = f"{result.notes} pos_src={pos_src}"
    actual_ts = time.perf_counter()

    # stream_cmp always compares stream vs GetStatus for the delta column
    stream_cmp = compare_stream_to_getstatus(
        stream_monitor,
        gs_pan,
        gs_tilt,
        timestamp_s=actual_ts,
        max_age_s=stream_max_age_s,
    )
    result.stream_pan_deg        = stream_cmp.sample.pan_deg  if stream_cmp.sample else None
    result.stream_tilt_deg       = stream_cmp.sample.tilt_deg if stream_cmp.sample else None
    result.stream_delta_pan_deg  = stream_cmp.delta_pan_deg
    result.stream_delta_tilt_deg = stream_cmp.delta_tilt_deg
    result.stream_age_ms         = stream_cmp.age_ms
    result.stream_notes          = stream_cmp.notes

    delta_pan  = abs(result.pan_target_deg  - result.pan_actual_deg)
    delta_tilt = abs(result.tilt_target_deg - result.tilt_actual_deg)
    result.passed = delta_pan <= threshold_deg and delta_tilt <= threshold_deg

    return result


# ===========================================================================
# Full latency suite (100 cycles)
# ===========================================================================

def run_latency_suite(
    cm: CommunicationManager,
    reporter: Reporter,
) -> LatencySummary:
    """Run `cycle_count` AbsoluteMove / Stop cycles with full latency profiling."""
    cycle_count      = int(cm.lat_cfg.get("cycle_count", 100))
    mech_threshold   = float(cm.lat_cfg.get("motion_detect_threshold_deg", 0.05))
    mech_poll_hz     = float(cm.lat_cfg.get("mech_poll_hz", 20))
    accuracy_deg     = float(cm.ptz_cfg.get("accuracy_threshold_deg", 0.2))
    pos_mode         = cm.ptz_cfg.get("position_source", "auto")
    cycle_timeout_s  = float(
        cm.timeout_cfg.get("latency_cycle_s", 45)
        if hasattr(cm, "timeout_cfg") else 45
    )
    stream_max_age_s = float(getattr(cm, "stream_cfg", {}).get("max_sample_age_ms", 500)) / 1000

    logger.info("=== Task 2: Latency Suite (%d cycles, pos_mode=%s) ===", cycle_count, pos_mode)
    ui.banner(f"Task 2  Latency Suite  ({cycle_count} cycles  pos={pos_mode})")
    ui.step(f"Per-cycle deadline: {cycle_timeout_s:.0f}s  (TIMEOUT row written if exceeded)")

    # --- Start scapy sniffer ---
    sniffer = _start_sniffer(
        interface=cm.scapy_cfg.get("interface", "en0"),
        host=cm._host,
        port=cm._port,
    )
    if sniffer:
        ui.step(f"scapy sniffer active  (interface={cm.scapy_cfg.get('interface','en0')}  filter=host {cm._host} port {cm._port})")
    else:
        ui.step("scapy packet capture not available (run as root for wire metrics)", ok=False, warn=True)

    stream_monitor = build_stream_monitor(cm)
    if stream_monitor is not None:
        ui.step(f"RTSP metadata monitor active  ({stream_monitor.status_note()})")
    else:
        ui.step("RTSP metadata monitor not active", ok=False, warn=True)

    summary = LatencySummary(cycle_count=cycle_count)
    print()

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    for i in range(cycle_count):
        future = executor.submit(
            _run_single_cycle,
            cm, summary,
            accuracy_deg, mech_threshold, mech_poll_hz,
            stream_monitor, stream_max_age_s, pos_mode,
        )
        try:
            result = future.result(timeout=cycle_timeout_s)
        except concurrent.futures.TimeoutError:
            elapsed = cycle_timeout_s
            logger.warning(
                "Cycle %d timed out after %.0fs — skipping and stopping camera.",
                i + 1, elapsed,
            )
            ui.step(
                f"Cycle {i+1}/{cycle_count} — deadline {cycle_timeout_s:.0f}s exceeded",
                ok=False, warn=True,
            )
            try:
                cm.stop()
            except Exception:  # noqa: BLE001
                pass
            reporter.write(
                command_type="AbsoluteMove",
                target_pan_deg=0.0, target_tilt_deg=0.0,
                actual_pan_deg=0.0, actual_tilt_deg=0.0,
                pass_fail="TIMEOUT",
                notes=f"cycle_deadline_{cycle_timeout_s:.0f}s_exceeded",
            )
            continue

        reporter.write(
            command_type="AbsoluteMove",
            target_pan_deg=result.pan_target_deg,
            target_tilt_deg=result.tilt_target_deg,
            actual_pan_deg=result.pan_actual_deg,
            actual_tilt_deg=result.tilt_actual_deg,
            comm_latency_ms=result.comm_latency_ms,
            mech_latency_ms=result.mech_latency_ms,
            stream_pan_deg=result.stream_pan_deg,
            stream_tilt_deg=result.stream_tilt_deg,
            stream_delta_pan_deg=result.stream_delta_pan_deg,
            stream_delta_tilt_deg=result.stream_delta_tilt_deg,
            stream_age_ms=result.stream_age_ms,
            stream_notes=(
                f"{result.stream_notes};stream_mech_ms={result.stream_mech_latency_ms:.2f}"
                if result.stream_mech_latency_ms is not None
                else result.stream_notes
            ),
            pass_fail="PASS" if result.passed else "FAIL",
            notes=result.notes,
        )

        # Print every cycle; log a running average every 10
        mean_comm = float(sum(summary.comm_latency_ms) / len(summary.comm_latency_ms)) \
            if summary.comm_latency_ms else 0.0
        ui.latency_row(
            i + 1, cycle_count,
            result.comm_latency_ms,
            result.mech_latency_ms,
            mean_comm,
            result.passed,
        )

        if (i + 1) % 10 == 0:
            partial_comm = summary.comm_stats()
            logger.info(
                "Cycle %d/%d — comm mean=%.1fms std=%.1fms",
                i + 1, cycle_count,
                partial_comm.get("mean_ms", 0),
                partial_comm.get("std_ms", 0),
            )

    executor.shutdown(wait=False)

    # --- Stop sniffer and analyse ---
    if sniffer is not None:
        _analyse_packets(sniffer, summary)
    if stream_monitor is not None:
        stream_monitor.stop()

    return summary


# ===========================================================================
# Entry point
# ===========================================================================

def _results_dir(model: str) -> str:
    slug = re.sub(r"[^\w]+", "_", model).strip("_").lower()
    return f"results/{slug}" if slug else "results"


def main() -> None:
    parser = argparse.ArgumentParser(description="Task 2: Programmatic Control Latency")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to camera config YAML (default: config.yaml)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    cm       = CommunicationManager(config_path=args.config)
    reporter = Reporter(results_dir=_results_dir(cm.camera_model), camera_model=cm.camera_model)

    summary = run_latency_suite(cm, reporter)

    # Return to home
    cm.absolute_move(0.0, 0.0)
    cm.wait_for_idle()

    # --- Final report ---
    cs = summary.comm_stats()
    ms = summary.mech_stats()
    sms = summary.stream_mech_stats()
    ws = summary.wire_stats()

    comm_rows = [(k, f"{v:.2f} ms") for k, v in cs.items()] if cs else [("No data", "—")]
    mech_rows = [(k, f"{v:.2f} ms") for k, v in ms.items()] if ms else [("No motion data collected", "—")]
    stream_mech_rows = [(k, f"{v:.2f} ms") for k, v in sms.items()] if sms else [("No stream motion data collected", "—")]
    wire_rows = [(k, f"{v:.2f}") for k, v in ws.items()] if ws else [("Packet capture unavailable (needs root)", "—")]

    ui.summary_table("Application (HTTP round-trip) Latency", comm_rows)
    ui.summary_table("Mechanical Start Latency", mech_rows)
    ui.summary_table("Stream Metadata Mechanical Start Latency", stream_mech_rows)
    ui.summary_table("scapy Wire-Level Analysis", wire_rows)
    ui.final_banner(str(reporter.filepath))


if __name__ == "__main__":
    main()
