"""
test_comm_utils.py — unit tests for CommunicationManager utility methods.

Tests pure functions and methods that require no real network connection:
  - _rewrite_url()    : NAT traversal URL rewriting
  - norm_to_deg()     : coordinate conversion
  - deg_to_norm()     : coordinate conversion (inverse)
  - wait_for_idle()   : polling logic (IDLE / timeout / fault recovery)
  - get_position_deg(): position extraction from GetStatus response
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from zeep.exceptions import Fault

import pytest

from comm_manager import CommunicationManager, norm_to_deg, deg_to_norm, PAN_SCALE, TILT_SCALE
from tests.conftest import make_status


# ---------------------------------------------------------------------------
# _rewrite_url
# ---------------------------------------------------------------------------

class TestRewriteUrl:
    """_rewrite_url replaces host:port while preserving path."""

    def _cm(self, host: str = "1.2.3.4", port: int = 9000) -> CommunicationManager:
        cm = object.__new__(CommunicationManager)
        cm._host = host
        cm._port = port
        return cm

    def test_replaces_internal_ip(self) -> None:
        cm = self._cm("100.67.177.125", 8085)
        result = cm._rewrite_url("http://192.168.1.152:80/onvif/ptz")
        assert result == "http://100.67.177.125:8085/onvif/ptz"

    def test_preserves_path(self) -> None:
        cm = self._cm("ext.host", 9000)
        result = cm._rewrite_url("http://10.0.0.5:80/onvif/imaging/wsdl")
        assert result == "http://ext.host:9000/onvif/imaging/wsdl"

    def test_noop_when_already_external(self) -> None:
        cm = self._cm("100.67.177.125", 8085)
        url = "http://100.67.177.125:8085/onvif/device_service"
        assert cm._rewrite_url(url) == url

    def test_empty_string_returns_empty(self) -> None:
        cm = self._cm("1.2.3.4", 80)
        assert cm._rewrite_url("") == ""

    def test_handles_https_scheme(self) -> None:
        cm = self._cm("ext.host", 443)
        result = cm._rewrite_url("https://192.168.1.100:443/onvif/ptz")
        assert result == "https://ext.host:443/onvif/ptz"

    def test_replaces_non_standard_internal_port(self) -> None:
        cm = self._cm("ext.host", 8085)
        result = cm._rewrite_url("http://192.168.0.1:8000/onvif/ptz")
        assert result == "http://ext.host:8085/onvif/ptz"


# ---------------------------------------------------------------------------
# Coordinate conversions
# ---------------------------------------------------------------------------

class TestCoordinateConversions:

    @pytest.mark.parametrize("pan_n,tilt_n,exp_pan,exp_tilt", [
        ( 0.0,  0.0,    0.0,   0.0),
        ( 1.0,  1.0,  180.0,  90.0),
        (-1.0, -1.0, -180.0, -90.0),
        ( 0.5, -0.5,   90.0, -45.0),
    ])
    def test_norm_to_deg(self, pan_n, tilt_n, exp_pan, exp_tilt) -> None:
        pan_d, tilt_d = norm_to_deg(pan_n, tilt_n)
        assert pan_d  == pytest.approx(exp_pan,  abs=1e-9)
        assert tilt_d == pytest.approx(exp_tilt, abs=1e-9)

    @pytest.mark.parametrize("pan_d,tilt_d,exp_pan,exp_tilt", [
        (  0.0,   0.0,  0.0,  0.0),
        (180.0,  90.0,  1.0,  1.0),
        (-90.0, -45.0, -0.5, -0.5),
    ])
    def test_deg_to_norm(self, pan_d, tilt_d, exp_pan, exp_tilt) -> None:
        pan_n, tilt_n = deg_to_norm(pan_d, tilt_d)
        assert pan_n  == pytest.approx(exp_pan,  abs=1e-9)
        assert tilt_n == pytest.approx(exp_tilt, abs=1e-9)

    def test_round_trip(self) -> None:
        for pan_d, tilt_d in [(45.0, 30.0), (-120.0, -60.0), (0.1, -0.1)]:
            pan_n, tilt_n = deg_to_norm(pan_d, tilt_d)
            back_pan, back_tilt = norm_to_deg(pan_n, tilt_n)
            assert back_pan  == pytest.approx(pan_d,  abs=1e-9)
            assert back_tilt == pytest.approx(tilt_d, abs=1e-9)

    def test_pan_scale_constant(self) -> None:
        assert PAN_SCALE  == 180.0

    def test_tilt_scale_constant(self) -> None:
        assert TILT_SCALE == 90.0


# ---------------------------------------------------------------------------
# wait_for_idle
# ---------------------------------------------------------------------------

class TestWaitForIdle:

    def test_returns_true_when_already_idle(self, mock_cm) -> None:
        mock_cm.ptz.GetStatus.return_value = make_status(0.0, 0.0, "IDLE")
        assert mock_cm.wait_for_idle(timeout=1.0) is True

    def test_returns_true_after_several_polls(self, mock_cm) -> None:
        # First two calls return MOVING, third returns IDLE
        mock_cm.ptz.GetStatus.side_effect = [
            make_status(0.1, 0.0, "MOVING"),
            make_status(0.05, 0.0, "MOVING"),
            make_status(0.0, 0.0, "IDLE"),
        ]
        assert mock_cm.wait_for_idle(timeout=2.0, poll_interval=0.01) is True

    def test_returns_false_on_timeout(self, mock_cm) -> None:
        mock_cm.ptz.GetStatus.return_value = make_status(0.5, 0.0, "MOVING")
        result = mock_cm.wait_for_idle(timeout=0.05, poll_interval=0.01)
        assert result is False

    def test_tolerates_transient_fault(self, mock_cm) -> None:
        """A single Fault during polling must not crash wait_for_idle."""
        mock_cm.ptz.GetStatus.side_effect = [
            Fault("temporary network error"),
            make_status(0.0, 0.0, "IDLE"),
        ]
        assert mock_cm.wait_for_idle(timeout=1.0, poll_interval=0.01) is True

    def test_case_insensitive_idle_check(self, mock_cm) -> None:
        mock_cm.ptz.GetStatus.return_value = make_status(0.0, 0.0, "idle")
        assert mock_cm.wait_for_idle(timeout=1.0) is True


# ---------------------------------------------------------------------------
# get_position_deg
# ---------------------------------------------------------------------------

class TestGetPositionDeg:

    def test_returns_correct_degrees(self, mock_cm) -> None:
        mock_cm.ptz.GetStatus.return_value = make_status(0.5, -0.5, "IDLE")
        pan_d, tilt_d = mock_cm.get_position_deg()
        assert pan_d  == pytest.approx(90.0,  abs=1e-6)
        assert tilt_d == pytest.approx(-45.0, abs=1e-6)

    def test_zero_position(self, mock_cm) -> None:
        mock_cm.ptz.GetStatus.return_value = make_status(0.0, 0.0, "IDLE")
        pan_d, tilt_d = mock_cm.get_position_deg()
        assert pan_d  == pytest.approx(0.0, abs=1e-6)
        assert tilt_d == pytest.approx(0.0, abs=1e-6)

    def test_full_range(self, mock_cm) -> None:
        mock_cm.ptz.GetStatus.return_value = make_status(1.0, 1.0, "IDLE")
        pan_d, tilt_d = mock_cm.get_position_deg()
        assert pan_d  == pytest.approx(180.0, abs=1e-6)
        assert tilt_d == pytest.approx(90.0,  abs=1e-6)
