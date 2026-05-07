"""
conftest.py — shared pytest fixtures.

The central fixture is `mock_cm`: a CommunicationManager instance whose
__init__ is bypassed entirely.  All ONVIF service calls are replaced with
MagicMocks so tests run instantly without any network or camera hardware.

Helper
------
make_status(pan_norm, tilt_norm, move_status)
    Builds a MagicMock that looks exactly like the object returned by
    ONVIFCamera.ptz.GetStatus(), including the attribute paths that
    comm_manager, task1, and task2 depend on.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from comm_manager import CommunicationManager


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_status(
    pan_norm: float = 0.0,
    tilt_norm: float = 0.0,
    move_status: str = "IDLE",
) -> MagicMock:
    """Return a MagicMock that mirrors the ONVIF GetStatus response shape."""
    s = MagicMock()
    s.Position.PanTilt.x = pan_norm
    s.Position.PanTilt.y = tilt_norm
    s.MoveStatus.PanTilt = move_status   # comm_manager reads this as a string
    return s


# ---------------------------------------------------------------------------
# Base PTZ config dict (mirrors a real config.yaml ptz section)
# ---------------------------------------------------------------------------

_PTZ_CFG = {
    "max_pan_speed_deg":    60.0,
    "max_tilt_speed_deg":   30.0,
    "accuracy_threshold_deg": 0.2,
    "ptz_timeout":          "PT5S",
    "home_timeout_s":       5,
    "move_timeout_s":       10,
    "default_pan_speed":    0.5,
    "default_tilt_speed":   0.5,
    "precision_test_count": 5,
    "preset_test_count":    3,
    "velocity_start_pan":   -0.5,
    "velocity_travel_deg":  180.0,
    "velocity_poll_hz":     20,
}

_LAT_CFG = {
    "cycle_count":                  5,
    "motion_detect_threshold_deg":  0.05,
    "mech_poll_hz":                 20,
}


# ---------------------------------------------------------------------------
# mock_cm fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_cm() -> CommunicationManager:
    """
    A fully-configured CommunicationManager whose __init__ is skipped.

    Services (ptz, media, imaging, devicemgmt) are MagicMocks.
    ptz.GetStatus is pre-configured to return IDLE at position (0, 0).

    Individual tests can override return values to simulate camera behaviour:

        mock_cm.ptz.GetStatus.return_value = make_status(0.5, 0.2, "IDLE")
        mock_cm.ptz.GetStatus.side_effect  = [status1, status2, ...]
    """
    cm = object.__new__(CommunicationManager)

    cm._host     = "127.0.0.1"
    cm._port     = 8080
    cm._user     = "admin"
    cm._password = "test"

    cm.ptz_cfg   = dict(_PTZ_CFG)
    cm.scapy_cfg = {"interface": "lo0"}
    cm.lat_cfg   = dict(_LAT_CFG)

    cm.profile_token    = "profile_1"
    cm.ptz_config_token = "config_1"
    cm.ptz_node_token   = "node_1"

    # --- Mock services ---
    cm.ptz        = MagicMock(name="ptz_service")
    cm.media      = MagicMock(name="media_service")
    cm.imaging    = MagicMock(name="imaging_service")
    cm.analytics  = None
    cm.devicemgmt = MagicMock(name="devicemgmt_service")

    # Default responses
    cm.ptz.GetStatus.return_value = make_status(0.0, 0.0, "IDLE")
    cm.ptz.AbsoluteMove.return_value = None
    cm.ptz.ContinuousMove.return_value = None
    cm.ptz.Stop.return_value = None

    # Return a fresh MagicMock per create_type call so that successive
    # request objects (SetPreset, GotoPreset, …) don't share state.
    cm.ptz.create_type.side_effect = lambda name: MagicMock(name=f"req_{name}")

    return cm
