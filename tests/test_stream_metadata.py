"""
test_stream_metadata.py — tests for ONVIF RTSP metadata parsing helpers.
"""

from __future__ import annotations

import pytest

from stream_metadata import (
    StreamTelemetryMonitor,
    compare_stream_to_getstatus,
    parse_telemetry_samples,
)


def test_parse_namespaced_pantilt_sample() -> None:
    xml = """
    <tt:MetadataStream>
      <tt:VideoAnalytics>
        <tt:Frame UtcTime="2026-05-07T10:00:00Z">
          <tt:PTZStatus>
            <tt:Position>
              <tt:PanTilt x="0.5000" y="-0.2500"/>
            </tt:Position>
          </tt:PTZStatus>
        </tt:Frame>
      </tt:VideoAnalytics>
    </tt:MetadataStream>
    """

    samples = parse_telemetry_samples(xml, timestamp_s=123.0)

    assert len(samples) == 1
    assert samples[0].pan_deg == pytest.approx(90.0)
    assert samples[0].tilt_deg == pytest.approx(-22.5)
    assert samples[0].timestamp_s == pytest.approx(123.0)


def test_parse_multiple_fragments() -> None:
    xml = '<tt:PanTilt x="0.0000" y="0.0000"/><tt:PanTilt x="1.0000" y="1.0000"/>'

    samples = parse_telemetry_samples(xml, timestamp_s=10.0)

    assert len(samples) == 2
    assert samples[1].pan_deg == pytest.approx(180.0)
    assert samples[1].tilt_deg == pytest.approx(90.0)


def test_compare_stream_to_getstatus_returns_delta() -> None:
    monitor = StreamTelemetryMonitor("rtsp://example/stream", enabled=False)
    sample = parse_telemetry_samples('<tt:PanTilt x="0.5000" y="0.0000"/>', timestamp_s=20.0)[0]
    monitor._samples.append(sample)

    comparison = compare_stream_to_getstatus(
        monitor,
        pan_deg=89.8,
        tilt_deg=0.1,
        timestamp_s=20.1,
        max_age_s=0.5,
    )

    assert comparison.sample is sample
    assert comparison.delta_pan_deg == pytest.approx(0.2)
    assert comparison.delta_tilt_deg == pytest.approx(0.1)
    assert comparison.age_ms == pytest.approx(100.0)


def test_compare_stream_to_getstatus_handles_stale_samples() -> None:
    monitor = StreamTelemetryMonitor("rtsp://example/stream", enabled=False)
    sample = parse_telemetry_samples('<tt:PanTilt x="0.0000" y="0.0000"/>', timestamp_s=1.0)[0]
    monitor._samples.append(sample)

    comparison = compare_stream_to_getstatus(
        monitor,
        pan_deg=0.0,
        tilt_deg=0.0,
        timestamp_s=3.0,
        max_age_s=0.5,
    )

    assert comparison.sample is None
    assert comparison.notes == "not_started"
