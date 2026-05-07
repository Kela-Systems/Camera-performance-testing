"""
enable_stream_metadata.py

Small standalone ONVIF Media script that asks the camera to enable PTZ status
metadata on the active media profile. It does not send PTZ movement commands.

Usage:
    .venv/bin/python enable_stream_metadata.py
    .venv/bin/python enable_stream_metadata.py --profile MP0
    .venv/bin/python enable_stream_metadata.py --dry-run
"""

from __future__ import annotations

import argparse
import socket
from urllib.parse import quote, urlparse, urlunparse

import yaml
from onvif import ONVIFCamera
from zeep.helpers import serialize_object

_NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"


def _rewrite_url(url: str, host: str, port: int) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(netloc=f"{host}:{port}"))


def _rewrite_rtsp_url(url: str, host: str, port: int) -> str:
    parsed = urlparse(url)
    netloc = f"{host}:{port}"
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _with_credentials(url: str, user: str, password: str) -> str:
    parsed = urlparse(url)
    if parsed.username:
        return url
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parsed.hostname}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _mask_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if not parsed.username:
        return uri
    netloc = f"{parsed.username}:***@{parsed.hostname}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _select_profile(profiles: list, requested_token: str | None):
    if requested_token:
        matches = [profile for profile in profiles if profile.token == requested_token]
        if not matches:
            raise RuntimeError(f"Profile token {requested_token!r} was not found.")
        return matches[0]

    for profile in profiles:
        if getattr(profile, "MetadataConfiguration", None) is not None:
            return profile
    return profiles[0]


def _metadata_summary(config: dict) -> str:
    ptz_status = config.get("PTZStatus") or {}
    return (
        f"token={config.get('token')} "
        f"name={config.get('Name')} "
        f"PTZStatus.Position={ptz_status.get('Position')} "
        f"PTZStatus.Status={ptz_status.get('Status')}"
    )


def _enable_ptz_status_fields(config: dict) -> dict:
    updated = dict(config)
    ptz_status = dict(updated.get("PTZStatus") or {})
    ptz_status["Position"] = True
    ptz_status["Status"] = True
    updated["PTZStatus"] = ptz_status
    return updated


def _set_ptz_status_on_object(config: object) -> object:
    """Mutate the zeep MetadataConfiguration object in-place when possible."""
    ptz_status = getattr(config, "PTZStatus", None)
    if ptz_status is None:
        return config
    try:
        ptz_status.Position = True
        ptz_status.Status = True
    except Exception:  # noqa: BLE001
        pass
    return config


def _is_ptz_status_enabled(config: dict) -> bool:
    ptz_status = config.get("PTZStatus") or {}
    return bool(ptz_status.get("Position")) and bool(ptz_status.get("Status"))


def _set_metadata_configuration(media, config: object, *, force_persistence: bool = True) -> None:
    """Call SetMetadataConfiguration using the request shape used by onvif-zeep."""
    req = media.create_type("SetMetadataConfiguration")
    req.Configuration = config
    req.ForcePersistence = force_persistence
    media.SetMetadataConfiguration(req)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enable ONVIF PTZ status metadata on a camera media profile."
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--profile", default=None, help="ONVIF media profile token, e.g. MP0")
    parser.add_argument("--dry-run", action="store_true", help="Inspect only; do not call SetMetadataConfiguration")
    args = parser.parse_args()

    with open(args.config) as fh:
        cfg = yaml.safe_load(fh)

    cam = cfg["camera"]
    stream_cfg = cfg.get("stream_metadata", {})
    timeouts = cfg.get("timeouts", {})

    camera_host = cam["host"]
    camera_port = int(cam["port"])
    stream_host = stream_cfg.get("host") or camera_host
    stream_port = int(stream_cfg.get("port", 554))
    requested_profile = args.profile or stream_cfg.get("profile_token") or None

    socket.setdefaulttimeout(float(timeouts.get("soap_s", 10)))

    print(f"Connecting to ONVIF Media at {camera_host}:{camera_port} ...")
    camera = ONVIFCamera(
        camera_host,
        camera_port,
        cam["user"],
        cam["password"],
    )
    devicemgmt = camera.create_devicemgmt_service()
    caps = devicemgmt.GetCapabilities({"Category": "All"})
    media_xaddr = _rewrite_url(caps.Media.XAddr, camera_host, camera_port)
    camera.xaddrs[_NS_MEDIA] = media_xaddr
    media = camera.create_media_service()
    print(f"Media service: {media_xaddr}")

    profiles = media.GetProfiles()
    if not profiles:
        raise RuntimeError("Camera returned no media profiles.")

    print("Profiles:")
    for profile in profiles:
        metadata_cfg = getattr(profile, "MetadataConfiguration", None)
        metadata_token = getattr(metadata_cfg, "token", None) if metadata_cfg is not None else None
        print(
            f"  token={profile.token} "
            f"name={getattr(profile, 'Name', '-') or '-'} "
            f"metadata={'yes' if metadata_cfg is not None else 'no'}"
            f"{f'({metadata_token})' if metadata_token else ''}"
        )

    profile = _select_profile(profiles, requested_profile)
    print(f"Selected profile: {profile.token} ({getattr(profile, 'Name', '')})")

    metadata_cfg = getattr(profile, "MetadataConfiguration", None)
    if metadata_cfg is None:
        raise RuntimeError(
            "Selected profile has no MetadataConfiguration. Create/attach one in the camera UI first."
        )

    metadata_token = metadata_cfg.token
    try:
        full_cfg = media.GetMetadataConfiguration({"ConfigurationToken": metadata_token})
    except Exception:
        full_cfg = metadata_cfg

    config_dict = serialize_object(full_cfg)
    print(f"Current metadata config: {_metadata_summary(config_dict)}")

    object_config = _set_ptz_status_on_object(full_cfg)
    updated_config = _enable_ptz_status_fields(config_dict)
    print(f"Requested metadata config: {_metadata_summary(updated_config)}")

    if args.dry_run:
        print("Dry run only; not sending SetMetadataConfiguration.")
    else:
        print("Trying SetMetadataConfiguration with zeep object ...")
        _set_metadata_configuration(media, object_config)
        verified = serialize_object(
            media.GetMetadataConfiguration({"ConfigurationToken": metadata_token})
        )
        print(f"Verified after object update: {_metadata_summary(verified)}")

        if not _is_ptz_status_enabled(verified):
            print("Trying SetMetadataConfiguration with serialized dict ...")
            _set_metadata_configuration(media, updated_config)
            verified = serialize_object(
                media.GetMetadataConfiguration({"ConfigurationToken": metadata_token})
            )
            print(f"Verified after dict update: {_metadata_summary(verified)}")

        if _is_ptz_status_enabled(verified):
            print("PTZ status metadata is enabled.")
        else:
            print(
                "Camera accepted SetMetadataConfiguration but did not persist "
                "PTZStatus.Position/Status. This usually means the setting is "
                "read-only through ONVIF on this firmware or must be enabled in "
                "the camera web UI / FLIR Nexus configuration."
            )

    req = media.create_type("GetStreamUri")
    req.StreamSetup = {
        "Stream": "RTP-Unicast",
        "Transport": {"Protocol": "RTSP"},
    }
    req.ProfileToken = profile.token
    response = media.GetStreamUri(req)
    raw_uri = getattr(response, "Uri", response)
    rtsp_uri = _rewrite_rtsp_url(raw_uri, stream_host, stream_port)
    rtsp_uri = _with_credentials(rtsp_uri, cam["user"], cam["password"])
    print(f"Retest metadata with: .venv/bin/python stream_metadata_probe.py --duration 10")
    print(f"RTSP URI for selected profile: {_mask_uri(rtsp_uri)}")


if __name__ == "__main__":
    main()
