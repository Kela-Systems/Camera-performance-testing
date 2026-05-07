"""
stream_metadata.py

Optional ONVIF RTSP metadata reader used to compare stream-reported PTZ
position against PTZ.GetStatus readback.
"""

from __future__ import annotations

import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque

logger = logging.getLogger(__name__)

PAN_SCALE = 180.0
TILT_SCALE = 90.0

_PANTILT_RE = re.compile(r"<[\w:.-]*PanTilt\b(?P<attrs>[^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""\b(?P<name>x|y)=["'](?P<value>[-+]?\d+(?:\.\d+)?)["']""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StreamTelemetrySample:
    """One PTZ sample parsed from the ONVIF metadata stream."""

    timestamp_s: float
    pan_deg: float
    tilt_deg: float
    pan_norm: float
    tilt_norm: float


@dataclass(frozen=True)
class StreamComparison:
    """Comparison between stream telemetry and a GetStatus position."""

    sample: StreamTelemetrySample | None
    delta_pan_deg: float | None
    delta_tilt_deg: float | None
    age_ms: float | None
    notes: str = ""


def parse_telemetry_samples(
    text: str,
    timestamp_s: float | None = None,
) -> list[StreamTelemetrySample]:
    """Parse ONVIF PTZ PanTilt samples from XML text or XML fragments."""
    samples, _ = _extract_samples(text, timestamp_s or time.perf_counter())
    return samples


def compare_stream_to_getstatus(
    monitor: "StreamTelemetryMonitor | None",
    pan_deg: float,
    tilt_deg: float,
    *,
    timestamp_s: float | None = None,
    max_age_s: float = 0.5,
) -> StreamComparison:
    """Compare the closest stream sample to a GetStatus position."""
    if monitor is None:
        return StreamComparison(None, None, None, None, "stream_disabled")

    ts = timestamp_s or time.perf_counter()
    sample = monitor.closest_to(ts, max_age_s=max_age_s)
    if sample is None:
        return StreamComparison(None, None, None, None, monitor.status_note())

    return StreamComparison(
        sample=sample,
        delta_pan_deg=abs(sample.pan_deg - pan_deg),
        delta_tilt_deg=abs(sample.tilt_deg - tilt_deg),
        age_ms=abs(ts - sample.timestamp_s) * 1000,
        notes="ok",
    )


def build_stream_monitor(cm: Any) -> "StreamTelemetryMonitor | None":
    """Create and start a StreamTelemetryMonitor from CommunicationManager config."""
    cfg = getattr(cm, "stream_cfg", {}) or {}
    if not cfg.get("enabled", False):
        return None

    try:
        rtsp_uri = cm.get_stream_uri(include_credentials=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stream metadata unavailable: could not get RTSP URI: %s", exc)
        return None

    monitor = StreamTelemetryMonitor(
        rtsp_uri,
        enabled=True,
        ffmpeg_bin=str(cfg.get("ffmpeg_bin", "ffmpeg")),
        transport=str(cfg.get("transport", "tcp")),
        stream_map=str(cfg.get("ffmpeg_map", "0:d:0?")),
        startup_timeout_s=float(cfg.get("startup_timeout_s", 5)),
    )
    monitor.start()
    return monitor


class StreamTelemetryMonitor:
    """Background reader for ONVIF metadata carried in an RTSP stream.

    The monitor uses ffmpeg to demux the metadata track to stdout. If ffmpeg,
    RTSP auth, or the metadata track is unavailable, callers can keep running
    and the monitor exposes the failure through status_note().
    """

    def __init__(
        self,
        rtsp_uri: str,
        *,
        enabled: bool = True,
        ffmpeg_bin: str = "ffmpeg",
        transport: str = "tcp",
        stream_map: str = "0:d:0?",
        sample_limit: int = 500,
        startup_timeout_s: float = 5.0,
    ) -> None:
        self._rtsp_uri = rtsp_uri
        self._enabled = enabled
        self._ffmpeg_bin = ffmpeg_bin
        self._transport = transport
        self._stream_map = stream_map
        self._startup_timeout_s = startup_timeout_s
        self._samples: Deque[StreamTelemetrySample] = deque(maxlen=sample_limit)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_tail: Deque[str] = deque(maxlen=20)
        self._stdout_tail: Deque[str] = deque(maxlen=20)
        self._stdout_bytes = 0
        self._note = "not_started"

    def start(self) -> bool:
        if not self._enabled:
            self._note = "stream_disabled"
            return False
        if shutil.which(self._ffmpeg_bin) is None:
            self._note = f"{self._ffmpeg_bin}_not_found"
            logger.warning("Stream metadata disabled: %s not found.", self._ffmpeg_bin)
            return False

        cmd = self._command()
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            self._note = f"start_failed:{exc}"
            logger.warning("Stream metadata ffmpeg start failed: %s", exc)
            return False

        self._note = "starting"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._thread.start()
        self._stderr_thread.start()

        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            if self.latest() is not None:
                self._note = "ok"
                return True
            if self._proc.poll() is not None:
                self._note = f"ffmpeg_exited:{self._proc.returncode}"
                return False
            time.sleep(0.05)

        self._note = "no_samples_yet"
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2.0)

    def latest(self, *, max_age_s: float | None = None) -> StreamTelemetrySample | None:
        with self._lock:
            sample = self._samples[-1] if self._samples else None
        if sample is None:
            return None
        if max_age_s is not None and time.perf_counter() - sample.timestamp_s > max_age_s:
            return None
        return sample

    def samples(self) -> list[StreamTelemetrySample]:
        """Return a point-in-time copy of buffered samples."""
        with self._lock:
            return list(self._samples)

    def diagnostics(self) -> dict[str, object]:
        """Return process status and recent ffmpeg stderr lines."""
        return {
            "status": self._note,
            "returncode": self._proc.returncode if self._proc else None,
            "command": self._masked_command(),
            "stdout_bytes": self._stdout_bytes,
            "stdout_tail": list(self._stdout_tail),
            "stderr": list(self._stderr_tail),
        }

    def _masked_command(self) -> list[str]:
        return [
            self._mask_uri(part) if part.startswith("rtsp://") else part
            for part in self._command()
        ]

    def _command(self) -> list[str]:
        return [
            self._ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            self._transport,
            "-i",
            self._rtsp_uri,
            "-copy_unknown",
            "-map",
            self._stream_map,
            "-c",
            "copy",
            "-f",
            "data",
            "-",
        ]

    @staticmethod
    def _mask_uri(uri: str) -> str:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(uri)
        if not parsed.username:
            return uri
        netloc = f"{parsed.username}:***@{parsed.hostname}"
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    def closest_to(
        self,
        timestamp_s: float,
        *,
        max_age_s: float = 0.5,
    ) -> StreamTelemetrySample | None:
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return None
        sample = min(samples, key=lambda item: abs(item.timestamp_s - timestamp_s))
        if abs(sample.timestamp_s - timestamp_s) > max_age_s:
            return None
        return sample

    def first_motion_after(
        self,
        start_ts: float,
        baseline_pan_deg: float,
        baseline_tilt_deg: float,
        *,
        threshold_deg: float,
    ) -> StreamTelemetrySample | None:
        with self._lock:
            samples = list(self._samples)
        for sample in samples:
            if sample.timestamp_s < start_ts:
                continue
            if (
                abs(sample.pan_deg - baseline_pan_deg) > threshold_deg
                or abs(sample.tilt_deg - baseline_tilt_deg) > threshold_deg
            ):
                return sample
        return None

    def status_note(self) -> str:
        return self._note

    def _run(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None

        buffer = ""
        stdout_fd = self._proc.stdout.fileno()
        os.set_blocking(stdout_fd, False)
        while not self._stop_event.is_set():
            ready, _, _ = select.select([stdout_fd], [], [], 0.1)
            if not ready:
                if self._proc.poll() is not None:
                    break
                continue

            try:
                chunk = os.read(stdout_fd, 4096)
            except BlockingIOError:
                continue

            if not chunk:
                if self._proc.poll() is not None:
                    break
                continue

            self._stdout_bytes += len(chunk)
            buffer += chunk.decode("utf-8", errors="ignore")
            printable = chunk[:160].decode("utf-8", errors="replace")
            if printable.strip():
                self._stdout_tail.append(printable.replace("\n", "\\n"))
            if len(buffer) > 200_000:
                buffer = buffer[-100_000:]
            samples, last_end = _extract_samples(buffer, time.perf_counter())
            if samples:
                with self._lock:
                    self._samples.extend(samples)
                self._note = "ok"
            if last_end:
                buffer = buffer[last_end:]

        if self._note in {"starting", "no_samples_yet"}:
            returncode = self._proc.returncode if self._proc else None
            self._note = f"reader_stopped:{returncode}"

    def _read_stderr(self) -> None:
        assert self._proc is not None
        assert self._proc.stderr is not None

        for raw_line in self._proc.stderr:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if line:
                self._stderr_tail.append(line)


def _extract_samples(
    text: str,
    timestamp_s: float,
) -> tuple[list[StreamTelemetrySample], int]:
    samples: list[StreamTelemetrySample] = []
    last_end = 0

    for match in _PANTILT_RE.finditer(text):
        attrs = {
            attr.group("name").lower(): float(attr.group("value"))
            for attr in _ATTR_RE.finditer(match.group("attrs"))
        }
        if "x" not in attrs or "y" not in attrs:
            continue
        pan_norm = attrs["x"]
        tilt_norm = attrs["y"]
        samples.append(
            StreamTelemetrySample(
                timestamp_s=timestamp_s,
                pan_deg=pan_norm * PAN_SCALE,
                tilt_deg=tilt_norm * TILT_SCALE,
                pan_norm=pan_norm,
                tilt_norm=tilt_norm,
            )
        )
        last_end = match.end()

    return samples, last_end
