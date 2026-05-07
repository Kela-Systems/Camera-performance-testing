"""
comm_manager.py  —  Task 0: Bootstrap & Configuration Layer

Responsibilities:
  1. Connect to the camera (direct IP, no WS-Discovery for WAN cameras).
  2. Rewrite all ONVIF service URLs returned by GetCapabilities from the
     camera's internal IP to the configured external host:port (NAT traversal).
  3. Validate and configure the PTZ node (spaces, timeout).
  4. Move the camera to the home position (0, 0) before any test begins.

ONVIF normalized PTZ space conventions used throughout this suite:
  Pan  : [-1, 1]  ↔  [-180°, +180°]   (scale = 180°)
  Tilt : [-1, 1]  ↔  [  -90°,  +90°]  (scale =  90°)
  Speed: [-1, 1]  ↔  full range in the respective direction
"""

from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from urllib.parse import quote, urlparse, urlunparse

import yaml
from onvif import ONVIFCamera
from zeep.exceptions import Fault
from zeep.helpers import serialize_object

import ui

logger = logging.getLogger(__name__)

# ONVIF service namespace URIs used as keys in ONVIFCamera.xaddrs
_NS_PTZ       = "http://www.onvif.org/ver20/ptz/wsdl"
_NS_MEDIA     = "http://www.onvif.org/ver10/media/wsdl"
_NS_IMAGING   = "http://www.onvif.org/ver20/imaging/wsdl"
_NS_ANALYTICS = "http://www.onvif.org/ver20/analytics/wsdl"

# ONVIF generic space URIs
_SPACE_ABS_PANTILT = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
_SPACE_SPEED       = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"

# Normalized-to-degree conversion constants
PAN_SCALE  = 180.0
TILT_SCALE = 90.0


def norm_to_deg(pan: float, tilt: float) -> tuple[float, float]:
    """Convert ONVIF normalized position to degrees."""
    return pan * PAN_SCALE, tilt * TILT_SCALE


def deg_to_norm(pan_deg: float, tilt_deg: float) -> tuple[float, float]:
    """Convert degrees to ONVIF normalized position."""
    return pan_deg / PAN_SCALE, tilt_deg / TILT_SCALE


class CommunicationManager:
    """Manages the ONVIF connection and exposes PTZ control primitives."""

    def __init__(self, config_path: str = "config.yaml") -> None:
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh)

        cam = cfg["camera"]
        self._host      = cam["host"]
        self._port      = int(cam["port"])
        self._user      = cam["user"]
        self._password  = cam["password"]

        self.ptz_cfg    = cfg["ptz"]
        self.scapy_cfg  = cfg.get("scapy", {})
        self.lat_cfg    = cfg.get("latency", {})
        self.stream_cfg = cfg.get("stream_metadata", {})

        _to             = cfg.get("timeouts", {})
        self._connect_timeout_s = float(_to.get("connect_s", 8))
        self._soap_timeout_s    = float(_to.get("soap_s", 10))
        self.timeout_cfg        = _to

        socket.setdefaulttimeout(self._soap_timeout_s)

        # Service handles — populated by _map_services()
        self._camera: ONVIFCamera | None = None
        self.devicemgmt  = None
        self.ptz         = None
        self.media       = None
        self.imaging     = None
        self.analytics   = None

        # Set after profile enumeration
        self.profile_token: str | None = None
        self.ptz_config_token: str | None = None
        self.ptz_node_token: str | None = None

        # Set after _connect()
        self.camera_model: str = ""

        ui.banner(f"Bootstrap  —  {self._host}:{self._port}")
        ui.step(
            f"Timeouts  connect={self._connect_timeout_s:.0f}s  "
            f"soap={self._soap_timeout_s:.0f}s",
            ok=True,
        )
        self._connect()
        self._map_services()
        self._configure_ptz()
        self._go_home()
        ui.divider()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _timed(self, timeout_s: float):
        """Temporarily tighten the global socket timeout, then restore it."""
        previous = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout_s)
        try:
            yield
        finally:
            socket.setdefaulttimeout(previous)

    # ------------------------------------------------------------------
    # Step 1: Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        logger.info("Connecting to camera at %s:%d", self._host, self._port)
        print(f"  Connecting to {self._host}:{self._port} ...", end=" ", flush=True)
        try:
            with self._timed(self._connect_timeout_s):
                self._camera = ONVIFCamera(
                    self._host,
                    self._port,
                    self._user,
                    self._password,
                )
                self.devicemgmt = self._camera.create_devicemgmt_service()
                info = self.devicemgmt.GetDeviceInformation()
        except socket.timeout:
            raise ConnectionError(
                f"Timed out connecting to {self._host}:{self._port} "
                f"after {self._connect_timeout_s:.0f}s — "
                "check the camera address and port forwarding rule."
            )
        except OSError as exc:
            raise ConnectionError(
                f"Cannot reach {self._host}:{self._port} — {exc}"
            ) from exc
        self.camera_model = f"{info.Manufacturer} {info.Model}"
        logger.info(
            "Device: %s %s  FW: %s",
            info.Manufacturer,
            info.Model,
            info.FirmwareVersion,
        )
        ui.step(
            f"Connected  —  {info.Manufacturer} {info.Model}  FW {info.FirmwareVersion}"
        )

    # ------------------------------------------------------------------
    # Step 2: Service URL rewriting + service creation
    # ------------------------------------------------------------------

    def _rewrite_url(self, url: str) -> str:
        """Replace whatever host:port the camera reports with the configured
        external address.  This is a no-op when running on the same LAN."""
        if not url:
            return url
        parsed = urlparse(url)
        rewritten = parsed._replace(netloc=f"{self._host}:{self._port}")
        result = urlunparse(rewritten)
        if result != url:
            logger.debug("URL rewrite:  %s  →  %s", url, result)
        return result

    def _rewrite_stream_url(self, url: str) -> str:
        """Rewrite RTSP stream URLs using the stream_metadata host/port.

        ONVIF SOAP may use an HTTP port-forward such as 8085, while RTSP
        metadata typically uses a separate forwarded stream port such as 554.
        """
        if not url:
            return url
        parsed = urlparse(url)
        host = self.stream_cfg.get("host") or self._host
        port = int(self.stream_cfg.get("port", parsed.port or 554))
        netloc = f"{host}:{port}"
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo = f"{userinfo}:{parsed.password}"
            netloc = f"{userinfo}@{netloc}"
        return urlunparse(parsed._replace(netloc=netloc))

    def _with_rtsp_credentials(self, url: str) -> str:
        """Add camera credentials to an RTSP URI if the camera omitted them."""
        parsed = urlparse(url)
        if parsed.username:
            return url
        user = quote(self._user, safe="")
        password = quote(self._password, safe="")
        netloc = f"{user}:{password}@{parsed.hostname or self._host}"
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    def get_stream_uri(self, include_credentials: bool = True) -> str:
        """Return the RTSP URI for the active profile, rewritten for WAN access."""
        if self.media is None or self.profile_token is None:
            raise RuntimeError("Media service/profile is not initialized.")

        req = self.media.create_type("GetStreamUri")
        req.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        req.ProfileToken = self.profile_token
        response = self.media.GetStreamUri(req)
        uri = getattr(response, "Uri", response)
        if not isinstance(uri, str) or not uri:
            raise RuntimeError("Media.GetStreamUri did not return an RTSP URI.")

        uri = self._rewrite_stream_url(uri)
        if include_credentials:
            uri = self._with_rtsp_credentials(uri)
        return uri

    def _map_services(self) -> None:
        caps = self.devicemgmt.GetCapabilities({"Category": "All"})

        # Inject rewritten URLs into the ONVIFCamera xaddrs dict so that
        # subsequent create_*_service() calls use the external address.
        self._camera.xaddrs[_NS_PTZ]     = self._rewrite_url(caps.PTZ.XAddr)
        self._camera.xaddrs[_NS_MEDIA]   = self._rewrite_url(caps.Media.XAddr)
        self._camera.xaddrs[_NS_IMAGING] = self._rewrite_url(caps.Imaging.XAddr)

        try:
            self._camera.xaddrs[_NS_ANALYTICS] = self._rewrite_url(
                caps.Analytics.XAddr
            )
        except AttributeError:
            logger.warning("Analytics capability not advertised — skipping.")

        self.ptz     = self._camera.create_ptz_service()
        self.media   = self._camera.create_media_service()
        self.imaging = self._camera.create_imaging_service()

        if _NS_ANALYTICS in self._camera.xaddrs:
            try:
                self.analytics = self._camera.create_analytics_service()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not create Analytics service: %s", exc)

        profiles = self.media.GetProfiles()
        if not profiles:
            raise RuntimeError("No media profiles found on the camera.")
        self.profile_token = profiles[0].token
        logger.info("Active media profile: %s", self.profile_token)
        analytics_status = "Analytics" if _NS_ANALYTICS in self._camera.xaddrs else "no Analytics"
        ui.step(f"Services mapped  (PTZ / Media / Imaging / {analytics_status})")

    # ------------------------------------------------------------------
    # Step 3: PTZ node validation and configuration
    # ------------------------------------------------------------------

    def _configure_ptz(self) -> None:
        nodes = self.ptz.GetNodes()
        if not nodes:
            raise RuntimeError("No PTZ nodes found.")
        node = nodes[0]
        self.ptz_node_token = node.token
        logger.info("PTZ node token: %s  HomeSupported: %s", node.token, node.HomeSupported)

        spaces = node.SupportedPTZSpaces
        abs_spaces   = [s.URI for s in (getattr(spaces, "AbsolutePanTiltPositionSpace", None) or [])]
        speed_spaces = [s.URI for s in (getattr(spaces, "PanTiltSpeedSpace", None) or [])]

        if not abs_spaces:
            raise RuntimeError("Camera does not advertise AbsolutePanTiltPositionSpace.")
        if not speed_spaces:
            raise RuntimeError("Camera does not advertise PanTiltSpeedSpace.")

        logger.info("AbsolutePanTilt spaces : %s", abs_spaces)
        logger.info("PanTiltSpeed spaces    : %s", speed_spaces)

        abs_space = _SPACE_ABS_PANTILT if _SPACE_ABS_PANTILT in abs_spaces else abs_spaces[0]

        configs = self.ptz.GetConfigurations()
        if not configs:
            raise RuntimeError("No PTZ configurations found.")
        config = configs[0]
        self.ptz_config_token = config.token

        ptz_timeout = self.ptz_cfg.get("ptz_timeout", "PT5S")

        # serialize_object converts the zeep type to a plain dict, preserving
        # all fields (including Name) so the SetConfiguration round-trip is valid.
        config_dict = serialize_object(config)
        config_dict["DefaultPTZTimeout"]                    = ptz_timeout
        config_dict["DefaultAbsolutePantTiltPositionSpace"] = abs_space
        config_dict.pop("DefaultPanTiltSpeedSpace", None)

        try:
            req = self.ptz.create_type("SetConfiguration")
            req.PTZConfiguration = config_dict
            req.ForcePersistence = True
            self.ptz.SetConfiguration(req)
            logger.info(
                "PTZ SetConfiguration OK — timeout: %s  absSpace: %s",
                ptz_timeout, abs_space,
            )
            ui.step(f"PTZ configured  (timeout={ptz_timeout}  node={node.token})")
        except Exception as exc:  # noqa: BLE001
            logger.warning("SetConfiguration failed (%s) — using camera defaults.", exc)
            ui.step(
                f"PTZ SetConfiguration skipped — using camera defaults  ({exc})",
                ok=True, warn=True,
            )

    # ------------------------------------------------------------------
    # Step 4: Home position reset
    # ------------------------------------------------------------------

    def _go_home(self) -> None:
        logger.info("Resetting camera to home position (0, 0).")
        print("  Moving to home position (0°, 0°) ...", end=" ", flush=True)
        pan_spd  = float(self.ptz_cfg.get("default_pan_speed",  0.5))
        tilt_spd = float(self.ptz_cfg.get("default_tilt_speed", 0.5))
        self.absolute_move(0.0, 0.0, pan_speed=pan_spd, tilt_speed=tilt_spd)
        settled = self.wait_for_idle(timeout=float(self.ptz_cfg.get("home_timeout_s", 15)))
        if settled:
            logger.info("Camera at home position.")
            ui.step("Home position reached")
        else:
            logger.warning("Camera did not reach IDLE within home_timeout_s — proceeding anyway.")
            ui.step("Home timeout — proceeding anyway", ok=False, warn=True)

    # ------------------------------------------------------------------
    # PTZ control helpers
    # ------------------------------------------------------------------

    def absolute_move(
        self,
        pan: float,
        tilt: float,
        pan_speed: float = 1.0,
        tilt_speed: float = 1.0,
    ) -> None:
        """Issue an AbsoluteMove command using ONVIF normalized coordinates."""
        req = self.ptz.create_type("AbsoluteMove")
        req.ProfileToken = self.profile_token
        req.Position = {
            "PanTilt": {"x": pan, "y": tilt, "space": None},
            "Zoom":    {"x": 0.0, "space": None},
        }
        req.Speed = {
            "PanTilt": {"x": pan_speed, "y": tilt_speed, "space": None},
            "Zoom":    {"x": 0.0, "space": None},
        }
        try:
            self.ptz.AbsoluteMove(req)
        except Fault as exc:
            code = str(exc)
            if any(k in code for k in ("MovingPTZ", "NoPTZToken", "InvalidArgVal")):
                logger.warning("ONVIF Fault on AbsoluteMove (%s) — stopping and retrying.", code)
                self.stop()
                time.sleep(1.0)
                self.ptz.AbsoluteMove(req)
            else:
                raise

    def continuous_move(
        self,
        pan_speed: float,
        tilt_speed: float = 0.0,
        timeout: str | None = None,
    ) -> None:
        """Issue a ContinuousMove command."""
        req = self.ptz.create_type("ContinuousMove")
        req.ProfileToken = self.profile_token
        req.Velocity = {
            "PanTilt": {"x": pan_speed, "y": tilt_speed, "space": None},
            "Zoom":    {"x": 0.0, "space": None},
        }
        if timeout:
            req.Timeout = timeout
        self.ptz.ContinuousMove(req)

    def stop(self) -> None:
        """Stop all PTZ motion."""
        req = self.ptz.create_type("Stop")
        req.ProfileToken = self.profile_token
        req.PanTilt = True
        req.Zoom    = False
        try:
            self.ptz.Stop(req)
        except Fault as exc:
            logger.debug("Stop fault (non-fatal): %s", exc)

    def get_status(self) -> object:
        """Return the raw ONVIF GetStatus response object."""
        return self.ptz.GetStatus({"ProfileToken": self.profile_token})

    def get_position_deg(self) -> tuple[float, float]:
        """Return the current (pan_deg, tilt_deg) from GetStatus."""
        status = self.get_status()
        pt = status.Position.PanTilt
        return pt.x * PAN_SCALE, pt.y * TILT_SCALE

    def wait_for_idle(
        self,
        timeout: float = 30.0,
        poll_interval: float = 0.1,
    ) -> bool:
        """Block until MoveStatus is IDLE or timeout expires. Returns True on success."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.get_status()
                ms = status.MoveStatus
                pan_tilt_status = (
                    getattr(ms, "PanTilt", None)
                    or getattr(ms, "panTilt", None)
                    or str(ms)
                )
                if str(pan_tilt_status).upper() == "IDLE":
                    return True
            except Fault as exc:
                logger.warning("GetStatus fault in wait_for_idle: %s", exc)
            time.sleep(poll_interval)
        logger.warning("wait_for_idle timed out after %.1fs.", timeout)
        return False

    def wait_for_position_stable(
        self,
        *,
        tolerance_deg: float = 0.05,
        stable_count: int = 3,
        poll_interval: float = 0.05,
        timeout: float = 5.0,
    ) -> tuple[float, float]:
        """Poll GetStatus until successive position readings converge.

        Some cameras (e.g. Hanwha) report MoveStatus=IDLE before their
        position register has finished updating. Calling GetStatus immediately
        after wait_for_idle therefore returns a stale value. This method keeps
        polling until `stable_count` consecutive readings agree within
        `tolerance_deg`, guaranteeing the register has settled.

        Returns the stable (pan_deg, tilt_deg). On timeout, returns the
        most recent reading rather than raising.
        """
        deadline = time.monotonic() + timeout
        history: list[tuple[float, float]] = []

        while time.monotonic() < deadline:
            try:
                pan_deg, tilt_deg = self.get_position_deg()
            except Fault as exc:
                logger.warning("GetStatus fault in wait_for_position_stable: %s", exc)
                time.sleep(poll_interval)
                continue

            history.append((pan_deg, tilt_deg))

            if len(history) >= stable_count:
                recent = history[-stable_count:]
                pan_spread  = max(p for p, _ in recent) - min(p for p, _ in recent)
                tilt_spread = max(t for _, t in recent) - min(t for _, t in recent)
                if pan_spread <= tolerance_deg and tilt_spread <= tolerance_deg:
                    return pan_deg, tilt_deg

            time.sleep(poll_interval)

        logger.warning(
            "wait_for_position_stable timed out after %.1fs — returning last reading.", timeout
        )
        return history[-1] if history else self.get_position_deg()
