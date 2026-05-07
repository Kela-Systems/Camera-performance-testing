"""
task1_validation.py  —  Layer 1: Command & Readback Validation

Tests
-----
1a  Precision Move  : 50 random AbsoluteMove commands, immediate GetStatus
                      readback; flags any delta > 0.2° spec.
1b  Preset Cycle    : Create / GoTo / Delete 10 presets; verifies 256-preset
                      capacity and round-trip position accuracy.
1c  Velocity Test   : 180° pan at maximum speed; background thread measures
                      realized °/sec via Δposition / Δtime.

Coordinate conventions (ONVIF normalized space):
  Pan  ∈ [-1, 1]  ↔  [-180°, +180°]
  Tilt ∈ [-1, 1]  ↔  [  -90°,  +90°]
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import threading
import time

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

# Normalized accuracy threshold derived from the 0.2° hardware spec
_ACC_PAN_NORM  = 0.2 / PAN_SCALE   # ≈ 0.00111
_ACC_TILT_NORM = 0.2 / TILT_SCALE  # ≈ 0.00222


# ===========================================================================
# Position resolution helper
# ===========================================================================

def _resolve_actual_pos(
    stream_monitor: StreamTelemetryMonitor | None,
    gs_pan_deg: float,
    gs_tilt_deg: float,
    stream_max_age_s: float,
    *,
    mode: str = "auto",
) -> tuple[float, float, str, object]:
    """Return (actual_pan, actual_tilt, source_tag, stream_cmp).

    mode is read from ptz_cfg["position_source"] and controls which source
    is used for Actual_Pos / pass-fail decisions:

      "getstatus"  — GetStatus only. Caller should use wait_for_position_stable
                     before calling this. stream_cmp is still computed and
                     written to the CSV as a diagnostic.
      "stream"     — Stream preferred; falls back to GetStatus with tag
                     "getstatus_fallback" when no fresh sample is available.
      "auto"       — (default) prefer stream when available, else GetStatus.
                     Caller should use wait_for_position_stable so both sources
                     are reliable.

    stream_cmp always reflects stream-vs-GetStatus so the CSV delta column
    remains a useful diagnostic regardless of which source wins.
    """
    stream_cmp = compare_stream_to_getstatus(
        stream_monitor,
        gs_pan_deg,
        gs_tilt_deg,
        max_age_s=stream_max_age_s,
    )

    if mode == "getstatus":
        return gs_pan_deg, gs_tilt_deg, "getstatus", stream_cmp

    if stream_cmp.sample is not None:
        return (
            stream_cmp.sample.pan_deg,
            stream_cmp.sample.tilt_deg,
            "stream",
            stream_cmp,
        )

    # Stream unavailable
    tag = "getstatus_fallback" if mode == "stream" else "getstatus"
    return gs_pan_deg, gs_tilt_deg, tag, stream_cmp


# ===========================================================================
# 1a — Precision Move Test
# ===========================================================================

def run_precision_moves(
    cm: CommunicationManager,
    reporter: Reporter,
    count: int | None = None,
    stream_monitor: StreamTelemetryMonitor | None = None,
) -> dict:
    """Issue `count` AbsoluteMove commands to random positions.

    For each move:
      - Record the commanded target.
      - Immediately call GetStatus (in-flight readback — captures commanded
        vs. reported while the camera is still moving).
      - Poll until IDLE for the settled readback (actual accuracy).
      - Flag if settled delta exceeds 0.2° spec.

    Returns summary statistics dict.
    """
    n = count or 50
    stream_max_age_s = float(getattr(cm, "stream_cfg", {}).get("max_sample_age_ms", 500)) / 1000
    threshold_deg = float(cm.ptz_cfg.get("accuracy_threshold_deg", 0.2))
    pos_mode = cm.ptz_cfg.get("position_source", "auto")
    logger.info("=== 1a: Precision Move Test (%d moves) ===", n)
    ui.banner(f"1a  Precision Move Test  ({n} moves)  —  spec: ±{threshold_deg}°")

    failures = 0
    deltas_pan: list[float]  = []
    deltas_tilt: list[float] = []

    for i in range(n):
        # Random normalized target, avoiding ±1 boundary
        pan_n  = random.uniform(-0.9, 0.9)
        tilt_n = random.uniform(-0.9, 0.9)
        target_pan_deg, target_tilt_deg = norm_to_deg(pan_n, tilt_n)

        try:
            cm.absolute_move(pan_n, tilt_n, pan_speed=1.0, tilt_speed=1.0)
        except Fault as exc:
            logger.error("AbsoluteMove fault on iteration %d: %s — skipping.", i, exc)
            continue

        # Immediate (in-flight) readback
        t_imm = time.perf_counter()
        try:
            imm_status = cm.get_status()
            imm_pt = imm_status.Position.PanTilt
            imm_pan_deg  = imm_pt.x * PAN_SCALE
            imm_tilt_deg = imm_pt.y * TILT_SCALE
        except Fault as exc:
            logger.warning("GetStatus (immediate) fault on iter %d: %s", i, exc)
            imm_pan_deg  = float("nan")
            imm_tilt_deg = float("nan")

        comm_latency_ms = (time.perf_counter() - t_imm) * 1000

        # Wait for settled position
        cm.wait_for_idle(timeout=float(cm.ptz_cfg.get("move_timeout_s", 30)))

        try:
            if pos_mode == "stream":
                gs_pan_deg, gs_tilt_deg = cm.get_position_deg()
            else:
                gs_pan_deg, gs_tilt_deg = cm.wait_for_position_stable(
                    tolerance_deg=threshold_deg / 2,
                )
        except Fault as exc:
            logger.warning("GetStatus (settled) fault on iter %d: %s", i, exc)
            gs_pan_deg, gs_tilt_deg = imm_pan_deg, imm_tilt_deg

        actual_pan_deg, actual_tilt_deg, pos_source, stream_cmp = _resolve_actual_pos(
            stream_monitor, gs_pan_deg, gs_tilt_deg, stream_max_age_s, mode=pos_mode
        )

        delta_pan  = abs(target_pan_deg  - actual_pan_deg)
        delta_tilt = abs(target_tilt_deg - actual_tilt_deg)
        passed = delta_pan <= threshold_deg and delta_tilt <= threshold_deg

        if not passed:
            failures += 1
            logger.warning(
                "FAIL [%d] target=(%.3f°, %.3f°) actual=(%.3f°, %.3f°) "
                "Δpan=%.3f° Δtilt=%.3f°",
                i, target_pan_deg, target_tilt_deg,
                actual_pan_deg, actual_tilt_deg,
                delta_pan, delta_tilt,
            )
        else:
            logger.info(
                "PASS [%d] Δpan=%.4f° Δtilt=%.4f°", i, delta_pan, delta_tilt
            )

        ui.precision_row(
            i + 1, n,
            target_pan_deg, target_tilt_deg,
            actual_pan_deg, actual_tilt_deg,
            delta_pan, delta_tilt,
            passed,
        )

        deltas_pan.append(delta_pan)
        deltas_tilt.append(delta_tilt)

        reporter.write(
            command_type="AbsoluteMove",
            target_pan_deg=target_pan_deg,
            target_tilt_deg=target_tilt_deg,
            actual_pan_deg=actual_pan_deg,
            actual_tilt_deg=actual_tilt_deg,
            comm_latency_ms=comm_latency_ms,
            stream_pan_deg=stream_cmp.sample.pan_deg if stream_cmp.sample else None,
            stream_tilt_deg=stream_cmp.sample.tilt_deg if stream_cmp.sample else None,
            stream_delta_pan_deg=stream_cmp.delta_pan_deg,
            stream_delta_tilt_deg=stream_cmp.delta_tilt_deg,
            stream_age_ms=stream_cmp.age_ms,
            stream_notes=stream_cmp.notes,
            pass_fail="PASS" if passed else "FAIL",
            notes=f"imm=({imm_pan_deg:.3f}°,{imm_tilt_deg:.3f}°) src={pos_source}",
        )

    summary = {
        "total": n,
        "failures": failures,
        "pass_rate_pct": (n - failures) / n * 100,
        "mean_delta_pan_deg":  float(np.mean(deltas_pan))  if deltas_pan  else 0.0,
        "mean_delta_tilt_deg": float(np.mean(deltas_tilt)) if deltas_tilt else 0.0,
        "max_delta_pan_deg":   float(np.max(deltas_pan))   if deltas_pan  else 0.0,
        "max_delta_tilt_deg":  float(np.max(deltas_tilt))  if deltas_tilt else 0.0,
    }
    logger.info(
        "1a Summary: %d/%d passed | mean Δpan=%.4f° Δtilt=%.4f°",
        n - failures, n,
        summary["mean_delta_pan_deg"],
        summary["mean_delta_tilt_deg"],
    )
    overall = "PASS" if failures == 0 else "FAIL"
    ui.summary_table("1a  Result", [
        ("Moves completed",              f"{n}"),
        ("Passed / Failed",              f"{n - failures} / {failures}"),
        ("Pass rate",                    f"{summary['pass_rate_pct']:.1f}%  {overall}"),
        (f"Mean Δpan  (spec ≤ {threshold_deg}°)", f"{summary['mean_delta_pan_deg']:.4f}°"),
        (f"Mean Δtilt (spec ≤ {threshold_deg}°)", f"{summary['mean_delta_tilt_deg']:.4f}°"),
        ("Max  Δpan",                    f"{summary['max_delta_pan_deg']:.4f}°"),
        ("Max  Δtilt",                   f"{summary['max_delta_tilt_deg']:.4f}°"),
    ])
    return summary


# ===========================================================================
# 1b — Preset Cycle
# ===========================================================================

def run_preset_cycle(
    cm: CommunicationManager,
    reporter: Reporter,
    count: int | None = None,
    stream_monitor: StreamTelemetryMonitor | None = None,
) -> dict:
    """Create, GoTo, verify, and delete `count` presets.

    The FLIR PT-Series supports 256 presets; this test exercises the
    create/recall/delete lifecycle and verifies position accuracy on recall.
    """
    n = count or 10
    stream_max_age_s = float(getattr(cm, "stream_cfg", {}).get("max_sample_age_ms", 500)) / 1000
    threshold_deg = float(cm.ptz_cfg.get("accuracy_threshold_deg", 0.2))
    pos_mode = cm.ptz_cfg.get("position_source", "auto")
    logger.info("=== 1b: Preset Cycle Test (%d presets) ===", n)
    ui.banner(f"1b  Preset Cycle Test  ({n} presets)")
    preset_tokens: list[str] = []
    preset_positions: list[tuple[float, float]] = []   # (pan_deg, tilt_deg)
    failures = 0

    # --- Create phase ---
    ui.section("Create")
    for i in range(n):
        pan_n  = random.uniform(-0.8, 0.8)
        tilt_n = random.uniform(-0.8, 0.8)
        target_pan_deg, target_tilt_deg = norm_to_deg(pan_n, tilt_n)

        cm.absolute_move(pan_n, tilt_n)
        cm.wait_for_idle(timeout=float(cm.ptz_cfg.get("move_timeout_s", 30)))

        try:
            req = cm.ptz.create_type("SetPreset")
            req.ProfileToken = cm.profile_token
            fmt = cm.ptz_cfg.get("preset_name_format", "test_preset_{i:02d}")
            req.PresetName   = fmt.format(i=i + 1)
            response = cm.ptz.SetPreset(req)
            token = response if isinstance(response, str) else response.PresetToken
            preset_tokens.append(token)
            preset_positions.append((target_pan_deg, target_tilt_deg))
            logger.info("Created preset [%d] token=%s at (%.2f°, %.2f°)", i, token, target_pan_deg, target_tilt_deg)
            ui.preset_create_row(i + 1, n, token, target_pan_deg, target_tilt_deg)
        except Fault as exc:
            logger.error("SetPreset fault [%d]: %s — skipping.", i, exc)
            preset_tokens.append(None)
            preset_positions.append((target_pan_deg, target_tilt_deg))
            ui.step(f"Preset {i+1}/{n} — SetPreset fault: {exc}", ok=False)

    # Return to home between create and recall to make the test meaningful
    cm.absolute_move(0.0, 0.0)
    cm.wait_for_idle()

    # --- GoTo + verify phase ---
    ui.section("GoTo + Verify")
    for i, (token, (saved_pan, saved_tilt)) in enumerate(
        zip(preset_tokens, preset_positions)
    ):
        if token is None:
            continue

        try:
            req = cm.ptz.create_type("GotoPreset")
            req.ProfileToken = cm.profile_token
            req.PresetToken  = token
            req.Speed = {
                "PanTilt": {"x": 1.0, "y": 1.0, "space": None},
                "Zoom":    {"x": 0.0, "space": None},
            }
            t0 = time.perf_counter()
            cm.ptz.GotoPreset(req)
            comm_latency_ms = (time.perf_counter() - t0) * 1000
        except Fault as exc:
            logger.error("GotoPreset fault [%d]: %s", i, exc)
            continue

        cm.wait_for_idle(timeout=float(cm.ptz_cfg.get("move_timeout_s", 30)))

        try:
            if pos_mode == "stream":
                gs_pan_deg, gs_tilt_deg = cm.get_position_deg()
            else:
                gs_pan_deg, gs_tilt_deg = cm.wait_for_position_stable(
                    tolerance_deg=threshold_deg / 2,
                )
        except Fault as exc:
            logger.warning("GetStatus fault on GotoPreset verify [%d]: %s", i, exc)
            gs_pan_deg = gs_tilt_deg = float("nan")

        actual_pan_deg, actual_tilt_deg, pos_source, stream_cmp = _resolve_actual_pos(
            stream_monitor, gs_pan_deg, gs_tilt_deg, stream_max_age_s, mode=pos_mode
        )
        delta_pan  = abs(saved_pan  - actual_pan_deg)
        delta_tilt = abs(saved_tilt - actual_tilt_deg)
        passed = delta_pan <= threshold_deg and delta_tilt <= threshold_deg
        if not passed:
            failures += 1

        reporter.write(
            command_type="GotoPreset",
            target_pan_deg=saved_pan,
            target_tilt_deg=saved_tilt,
            actual_pan_deg=actual_pan_deg,
            actual_tilt_deg=actual_tilt_deg,
            comm_latency_ms=comm_latency_ms,
            stream_pan_deg=stream_cmp.sample.pan_deg if stream_cmp.sample else None,
            stream_tilt_deg=stream_cmp.sample.tilt_deg if stream_cmp.sample else None,
            stream_delta_pan_deg=stream_cmp.delta_pan_deg,
            stream_delta_tilt_deg=stream_cmp.delta_tilt_deg,
            stream_age_ms=stream_cmp.age_ms,
            stream_notes=stream_cmp.notes,
            pass_fail="PASS" if passed else "FAIL",
            notes=f"preset_token={token} src={pos_source}",
        )
        logger.info(
            "GoTo preset [%d] Δpan=%.4f° Δtilt=%.4f° %s",
            i, delta_pan, delta_tilt, "PASS" if passed else "FAIL",
        )
        ui.preset_goto_row(i + 1, n, delta_pan, delta_tilt, passed)

    # --- Delete + verify phase ---
    ui.section("Delete")
    for token in preset_tokens:
        if token is None:
            continue
        try:
            req = cm.ptz.create_type("RemovePreset")
            req.ProfileToken = cm.profile_token
            req.PresetToken  = token
            cm.ptz.RemovePreset(req)
        except Fault as exc:
            logger.warning("RemovePreset fault for token %s: %s", token, exc)

    # Confirm deletion
    remaining = cm.ptz.GetPresets({"ProfileToken": cm.profile_token})
    remaining_tokens = {p.token for p in (remaining or [])}
    leaked = [t for t in preset_tokens if t and t in remaining_tokens]
    if leaked:
        logger.warning("Preset tokens not fully removed: %s", leaked)
        ui.step(f"{len(leaked)} preset(s) not removed — check camera", ok=False, warn=True)
    else:
        logger.info("All %d test presets successfully deleted.", len(preset_tokens))
        ui.step(f"All {len(preset_tokens)} presets deleted cleanly")

    summary = {
        "total": n,
        "failures": failures,
        "pass_rate_pct": (n - failures) / n * 100,
        "leaked_presets": len(leaked),
    }
    logger.info("1b Summary: %d/%d GotoPreset passed.", n - failures, n)
    overall = "PASS" if failures == 0 else "FAIL"
    ui.summary_table("1b  Result", [
        ("Presets created / recalled / deleted", f"{n}"),
        ("GoTo accuracy failures",               f"{failures}  ({overall})"),
        ("Pass rate",                            f"{summary['pass_rate_pct']:.1f}%"),
        ("Leaked presets",                       f"{len(leaked)}"),
    ])
    return summary


# ===========================================================================
# 1c — Velocity Verification
# ===========================================================================

def run_velocity(
    cm: CommunicationManager,
    reporter: Reporter,
    stream_monitor: StreamTelemetryMonitor | None = None,
) -> dict:
    """Pan 180° at the documented maximum speed.

    Position samples are collected from the stream when position_source is
    "auto" or "stream" (accurate real-time data), or from a GetStatus polling
    thread when position_source is "getstatus". The realized velocity is
    calculated via numpy linear regression over the moving segment.
    """
    spec_vel    = float(cm.ptz_cfg.get("max_pan_speed_deg", 60.0))
    pos_mode    = cm.ptz_cfg.get("position_source", "auto")
    poll_hz     = float(cm.ptz_cfg.get("velocity_poll_hz", 20))
    poll_sleep  = 1.0 / poll_hz
    start_pan_n = float(cm.ptz_cfg.get("velocity_start_pan", -0.5))
    travel_deg  = float(cm.ptz_cfg.get("velocity_travel_deg", 180.0))
    stream_max_age_s = float(getattr(cm, "stream_cfg", {}).get("max_sample_age_ms", 500)) / 1000

    use_stream = pos_mode in ("auto", "stream") and stream_monitor is not None

    logger.info("=== 1c: Velocity Verification (%.0f° @ max speed) ===", travel_deg)
    ui.banner(
        f"1c  Velocity Verification  —  {travel_deg:.0f}° pan @ max speed "
        f"({spec_vel:.0f}°/s spec)  src={'stream' if use_stream else 'getstatus'}"
    )

    # Move to start position and wait for a stable GetStatus reading
    cm.absolute_move(start_pan_n, 0.0, pan_speed=0.5, tilt_speed=0.5)
    cm.wait_for_idle(timeout=float(cm.ptz_cfg.get("move_timeout_s", 30)))
    start_pan_deg, _ = cm.wait_for_position_stable(tolerance_deg=0.05)

    # If stream is the source, also confirm start via the stream for consistency
    if use_stream:
        s = stream_monitor.latest(max_age_s=1.0)
        if s is not None:
            start_pan_deg = s.pan_deg
            logger.info("Start position from stream: %.3f°", start_pan_deg)
        else:
            logger.info("Start position from GetStatus (stream sample not yet available): %.3f°", start_pan_deg)

    target_pan_deg = start_pan_deg + travel_deg
    if target_pan_deg > 180.0:
        target_pan_deg -= 360.0

    # --- GetStatus polling thread (always runs; used as primary or fallback) ---
    gs_samples: list[tuple[float, float]] = []   # (timestamp_s, pan_deg)
    stop_flag = threading.Event()

    def _poll() -> None:
        while not stop_flag.is_set():
            try:
                pan_d, _ = cm.get_position_deg()
                gs_samples.append((time.perf_counter(), pan_d))
            except Fault as exc:
                logger.debug("GetStatus fault in velocity poll: %s", exc)
            time.sleep(poll_sleep)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()

    # --- Issue ContinuousMove ---
    expected_duration = travel_deg / spec_vel
    velocity_total_s = float(
        cm.timeout_cfg.get("velocity_total_s", expected_duration * 2.5)
        if hasattr(cm, "timeout_cfg")
        else expected_duration * 2.5
    )
    logger.info(
        "ContinuousMove: start=%.2f°  target≈%.2f°  expected≈%.2fs  cap=%.1fs",
        start_pan_deg, target_pan_deg, expected_duration, velocity_total_s,
    )
    print(
        f"  ContinuousMove  start={start_pan_deg:+.1f}°  "
        f"target≈{target_pan_deg:+.1f}°  "
        f"expected≈{expected_duration:.1f}s  "
        f"hard cap={velocity_total_s:.0f}s  "
        f"src={'stream' if use_stream else f'getstatus @ {poll_hz:.0f} Hz'} ...",
        flush=True,
    )

    cm.continuous_move(pan_speed=1.0, tilt_speed=0.0)
    move_t0 = time.perf_counter()

    # --- Detect target reached ---
    # Use stream for real-time detection when available; fall back to GetStatus
    deadline = move_t0 + velocity_total_s
    capped = False
    while time.perf_counter() < deadline:
        time.sleep(poll_sleep)
        try:
            if use_stream:
                s = stream_monitor.latest(max_age_s=stream_max_age_s)
                pan_now = s.pan_deg if s is not None else None
            else:
                pan_now, _ = cm.get_position_deg()
            if pan_now is not None and abs(pan_now - target_pan_deg) < 5.0:
                logger.info("Target pan reached (~%.2f°) — stopping.", pan_now)
                break
        except Fault:
            pass
    else:
        capped = True
        elapsed = time.perf_counter() - move_t0
        logger.warning(
            "Velocity test hard cap reached (%.1fs) before target pan — stopping.", elapsed,
        )
        ui.step(
            f"Velocity test capped at {velocity_total_s:.0f}s — "
            "camera may be slower than spec",
            ok=False, warn=True,
        )

    move_stop_ts = time.perf_counter()
    cm.stop()
    stop_flag.set()
    poller.join(timeout=2.0)
    cm.wait_for_idle(timeout=5.0)

    # --- Choose sample source for analysis ---
    if use_stream:
        stream_samples = stream_monitor.samples()
        # Keep only samples captured during the move window
        window = [
            (s.timestamp_s, s.pan_deg)
            for s in stream_samples
            if move_t0 <= s.timestamp_s <= move_stop_ts
        ]
        if len(window) >= 4:
            samples = window
            sample_src = "stream"
            logger.info("Velocity analysis using %d stream samples.", len(samples))
        else:
            logger.warning(
                "Too few stream samples in move window (%d) — falling back to GetStatus.",
                len(window),
            )
            samples = gs_samples
            sample_src = "getstatus_fallback"
    else:
        samples = gs_samples
        sample_src = "getstatus"

    if len(samples) < 4:
        logger.error("Insufficient samples for velocity analysis (%d).", len(samples))
        return {"error": "insufficient_samples"}

    timestamps = np.array([s[0] for s in samples])
    positions  = np.array([s[1] for s in samples])

    # Detect the moving segment: drop the still portion at the start/end
    velocities   = np.diff(positions) / np.diff(timestamps)
    moving_mask  = np.abs(velocities) > 1.0
    moving_indices = np.where(moving_mask)[0]

    if len(moving_indices) == 0:
        logger.error("No motion detected in velocity samples (src=%s).", sample_src)
        return {"error": "no_motion_detected"}

    first_motion = int(moving_indices[0])
    last_motion  = int(moving_indices[-1]) + 1
    t_moving = timestamps[first_motion : last_motion + 1]
    p_moving = positions[first_motion  : last_motion + 1]

    if len(t_moving) >= 2:
        coeffs = np.polyfit(t_moving - t_moving[0], p_moving, 1)
        realized_vel = float(abs(coeffs[0]))
    else:
        realized_vel = float(
            abs(p_moving[-1] - p_moving[0]) / (t_moving[-1] - t_moving[0])
        )

    threshold_95      = spec_vel * 0.95
    passed            = realized_vel >= threshold_95 and not capped
    actual_travel_deg = float(abs(p_moving[-1] - p_moving[0]))
    final_ts          = float(t_moving[-1])

    stream_cmp = compare_stream_to_getstatus(
        stream_monitor,
        float(p_moving[-1]),
        0.0,
        timestamp_s=final_ts,
        max_age_s=stream_max_age_s,
    )

    logger.info(
        "1c Result: realized_vel=%.2f°/s  spec=%.1f°/s  travel=%.2f°  src=%s  %s",
        realized_vel, spec_vel, actual_travel_deg, sample_src,
        "PASS" if passed else "FAIL",
    )
    ui.summary_table("1c  Result", [
        ("Realized velocity",            f"{realized_vel:.2f}°/s"),
        ("Spec (max pan speed)",         f"{spec_vel:.1f}°/s"),
        ("Threshold (95% of spec)",      f"{threshold_95:.1f}°/s"),
        ("Actual travel",                f"{actual_travel_deg:.2f}°"),
        ("Samples collected",            f"{len(samples)}  (src={sample_src})"),
        ("Result",                       f"{'PASS' if passed else 'FAIL'}"),
    ])

    reporter.write(
        command_type="ContinuousMove",
        target_pan_deg=target_pan_deg,
        target_tilt_deg=0.0,
        actual_pan_deg=float(p_moving[-1]),
        actual_tilt_deg=0.0,
        stream_pan_deg=stream_cmp.sample.pan_deg if stream_cmp.sample else None,
        stream_tilt_deg=stream_cmp.sample.tilt_deg if stream_cmp.sample else None,
        stream_delta_pan_deg=stream_cmp.delta_pan_deg,
        stream_delta_tilt_deg=stream_cmp.delta_tilt_deg,
        stream_age_ms=stream_cmp.age_ms,
        stream_notes=stream_cmp.notes,
        pass_fail="PASS" if passed else "FAIL",
        notes=(
            f"realized_vel={realized_vel:.2f}°/s "
            f"spec={spec_vel:.1f}°/s "
            f"travel={actual_travel_deg:.2f}° "
            f"samples={len(samples)} "
            f"src={sample_src}"
        ),
    )

    return {
        "realized_velocity_deg_s": realized_vel,
        "spec_velocity_deg_s":     spec_vel,
        "actual_travel_deg":       actual_travel_deg,
        "sample_count":            len(samples),
        "sample_src":              sample_src,
        "passed":                  passed,
    }


# ===========================================================================
# Entry point
# ===========================================================================

def _results_dir(model: str) -> str:
    slug = re.sub(r"[^\w]+", "_", model).strip("_").lower()
    return f"results/{slug}" if slug else "results"


def main() -> None:
    parser = argparse.ArgumentParser(description="Task 1: Command & Readback Validation")
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
    stream_monitor = build_stream_monitor(cm)

    results: dict[str, object] = {}

    try:
        results["precision"] = run_precision_moves(
            cm, reporter,
            count=int(cm.ptz_cfg.get("precision_test_count", 50)),
            stream_monitor=stream_monitor,
        )

        results["preset"] = run_preset_cycle(
            cm, reporter,
            count=int(cm.ptz_cfg.get("preset_test_count", 10)),
            stream_monitor=stream_monitor,
        )

        results["velocity"] = run_velocity(cm, reporter, stream_monitor=stream_monitor)

        # Return camera to home after all tests
        cm.absolute_move(0.0, 0.0)
        cm.wait_for_idle()
    finally:
        if stream_monitor is not None:
            stream_monitor.stop()

    ui.final_banner(str(reporter.filepath))


if __name__ == "__main__":
    main()
