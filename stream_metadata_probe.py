"""
stream_metadata_probe.py

Standalone diagnostic for checking ONVIF RTSP metadata before running PTZ
mechanics tests. This script discovers the stream URI and listens for metadata
samples only; it does not send any PTZ move, stop, preset, or status commands.

Usage:
    .venv/bin/python stream_metadata_probe.py --duration 15
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import shutil
import socket
import subprocess
import time
from urllib.parse import quote, unquote, urlparse, urlunparse

import yaml
from onvif import ONVIFCamera

from stream_metadata import StreamTelemetryMonitor, parse_telemetry_samples

_NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
_AUTH_FIELD_RE = re.compile(r'(\w+)=(?:"([^"]+)"|([^,\s]+))')


def _rewrite_url(url: str, host: str, port: int) -> str:
    if not url:
        return url
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


def _default_ffprobe_bin(ffmpeg_bin: str) -> str:
    return ffmpeg_bin[:-6] + "ffprobe" if ffmpeg_bin.endswith("ffmpeg") else "ffprobe"


def inspect_rtsp_uri(rtsp_uri: str, stream_cfg: dict, timeout_s: float = 10.0) -> None:
    ffprobe_bin = str(stream_cfg.get("ffprobe_bin", _default_ffprobe_bin(str(stream_cfg.get("ffmpeg_bin", "ffmpeg")))))
    if shutil.which(ffprobe_bin) is None:
        print(f"ffprobe not found ({ffprobe_bin}); skipping stream inspection.")
        return

    print("Inspecting RTSP streams with ffprobe ...")
    cmd = [
        ffprobe_bin,
        "-hide_banner",
        "-v",
        "error",
        "-rtsp_transport",
        str(stream_cfg.get("transport", "tcp")),
        "-show_entries",
        "stream=index,codec_type,codec_name",
        "-of",
        "compact=p=0:nk=0",
        rtsp_uri,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"ffprobe timed out after {timeout_s:.0f}s.")
        return

    if completed.stdout.strip():
        print("ffprobe streams:")
        for line in completed.stdout.strip().splitlines():
            print(f"  {line}")
    else:
        print("ffprobe returned no stream list.")

    if completed.stderr.strip():
        print("ffprobe stderr:")
        for line in completed.stderr.strip().splitlines()[-8:]:
            print(f"  {line}")
    print(f"ffprobe exit code: {completed.returncode}")


def _parse_auth(header: str) -> dict[str, str]:
    values = {}
    for key, quoted, bare in _AUTH_FIELD_RE.findall(header):
        values[key] = quoted or bare
    return values


def _digest_header(
    method: str,
    uri: str,
    username: str,
    password: str,
    challenge: str,
) -> str:
    params = _parse_auth(challenge)
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (
        'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
        'uri="{uri}", response="{response}"'
    ).format(
        username=username,
        realm=realm,
        nonce=nonce,
        uri=uri,
        response=response,
    )


class RtspProbeClient:
    def __init__(self, rtsp_uri: str, timeout_s: float = 10.0) -> None:
        self._original_uri = rtsp_uri
        self._parsed = urlparse(rtsp_uri)
        self._username = unquote(self._parsed.username or "")
        self._password = unquote(self._parsed.password or "")
        host = self._parsed.hostname
        if not host:
            raise RuntimeError("RTSP URI has no host.")
        self._host = host
        self._port = int(self._parsed.port or 554)
        self._timeout_s = timeout_s
        self._cseq = 1
        self._session: str | None = None
        self._auth_challenge: str | None = None
        self._sync_discarded_bytes = 0
        self._bad_frame_headers = 0
        netloc = self._host if self._parsed.port is None else f"{self._host}:{self._port}"
        self._request_uri = urlunparse(self._parsed._replace(netloc=netloc))
        self._sock = socket.create_connection((self._host, self._port), timeout=timeout_s)
        self._sock.settimeout(timeout_s)
        self._buffer = b""

    def close(self) -> None:
        try:
            if self._session:
                self.request("TEARDOWN", self._request_uri)
        except Exception:
            pass
        self._sock.close()

    def describe(self) -> str:
        status, headers, body = self.request(
            "DESCRIBE",
            self._request_uri,
            {"Accept": "application/sdp"},
        )
        if status != 200:
            raise RuntimeError(f"DESCRIBE failed with RTSP status {status}")
        return body.decode("utf-8", errors="ignore")

    def setup(self, control_uri: str, interleaved: str) -> dict[str, str]:
        status, headers, _ = self.request(
            "SETUP",
            control_uri,
            {"Transport": f"RTP/AVP/TCP;unicast;interleaved={interleaved}"},
        )
        if status != 200:
            raise RuntimeError(f"SETUP failed with RTSP status {status}")
        session = (headers.get("session") or "").split(";")[0]
        if session:
            self._session = session
        return headers

    def play(self) -> None:
        status, _, _ = self.request("PLAY", self._request_uri)
        if status != 200:
            raise RuntimeError(f"PLAY failed with RTSP status {status}")

    def request(
        self,
        method: str,
        uri: str,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        status, headers, body = self._send_request(method, uri, extra_headers)
        if status == 401 and "www-authenticate" in headers:
            self._auth_challenge = headers["www-authenticate"]
            status, headers, body = self._send_request(method, uri, extra_headers)
        return status, headers, body

    def read_interleaved_payload(self, timeout_s: float) -> tuple[int | None, bytes]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len(self._buffer) >= 4 and self._buffer[0] == 0x24:
                channel = self._buffer[1]
                length = int.from_bytes(self._buffer[2:4], "big")
                if length < 12 or length > 65535:
                    self._bad_frame_headers += 1
                    self._buffer = self._buffer[1:]
                    continue
                if len(self._buffer) >= 4 + length:
                    packet = self._buffer[4 : 4 + length]
                    self._buffer = self._buffer[4 + length :]
                    return channel, _rtp_payload(packet)
            elif self._buffer:
                next_frame = self._buffer.find(b"$", 1)
                if next_frame == -1:
                    self._sync_discarded_bytes += len(self._buffer)
                    self._buffer = b""
                else:
                    self._sync_discarded_bytes += next_frame
                    self._buffer = self._buffer[next_frame:]

            remaining = max(0.1, deadline - time.monotonic())
            self._sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return None, b""
            self._buffer += chunk
        return None, b""

    def sync_stats(self) -> dict[str, int]:
        return {
            "discarded_bytes": self._sync_discarded_bytes,
            "bad_frame_headers": self._bad_frame_headers,
        }

    def _send_request(
        self,
        method: str,
        uri: str,
        extra_headers: dict[str, str] | None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            "CSeq": str(self._cseq),
            "User-Agent": "flir-metadata-probe/1.0",
        }
        self._cseq += 1
        if self._session:
            headers["Session"] = self._session
        if self._auth_challenge and self._username:
            headers["Authorization"] = _digest_header(
                method,
                uri,
                self._username,
                self._password,
                self._auth_challenge,
            )
        if extra_headers:
            headers.update(extra_headers)

        request = f"{method} {uri} RTSP/1.0\r\n"
        request += "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        request += "\r\n"
        self._sock.sendall(request.encode("utf-8"))
        return self._read_response()

    def _read_response(self) -> tuple[int, dict[str, str], bytes]:
        while b"\r\n\r\n" not in self._buffer:
            self._buffer += self._sock.recv(4096)
        header_blob, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        lines = header_blob.decode("utf-8", errors="ignore").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        while len(self._buffer) < content_length:
            self._buffer += self._sock.recv(4096)
        body = self._buffer[:content_length]
        self._buffer = self._buffer[content_length:]
        return status, headers, body


def _rtp_payload(packet: bytes) -> bytes:
    if len(packet) < 12:
        return b""
    cc = packet[0] & 0x0F
    extension = bool(packet[0] & 0x10)
    offset = 12 + cc * 4
    if extension and len(packet) >= offset + 4:
        extension_words = int.from_bytes(packet[offset + 2 : offset + 4], "big")
        offset += 4 + extension_words * 4
    return packet[offset:]


def _control_tracks(rtsp_uri: str, sdp: str) -> list[tuple[str, str]]:
    tracks: list[tuple[str, str]] = []
    current_media = ""
    for raw_line in sdp.splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            current_media = line[2:].split()[0].lower()
        elif line.startswith("a=control:"):
            control = line.split(":", 1)[1]
            if control == "*":
                continue
            tracks.append((current_media, _resolve_control_uri(rtsp_uri, control)))
    return tracks


def _resolve_control_uri(base_uri: str, control: str) -> str:
    if control.startswith("rtsp://"):
        return control
    parsed = urlparse(base_uri)
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/{control.lstrip('/')}"
    return urlunparse(parsed._replace(path=path))


def direct_rtsp_metadata_probe(rtsp_uri: str, duration_s: float) -> int:
    print("Trying direct RTSP-over-TCP metadata read ...")
    client = RtspProbeClient(rtsp_uri)
    payload_bytes = 0
    parsed_samples = 0
    printable_tail = ""
    try:
        sdp = client.describe()
        print("RTSP SDP media/control lines:")
        for line in sdp.splitlines():
            if line.startswith(("m=", "a=control:", "a=rtpmap:")):
                print(f"  {line}")
        tracks = _control_tracks(rtsp_uri, sdp)
        if not tracks:
            raise RuntimeError("SDP did not contain track control URIs.")

        metadata_channels: set[int] = set()
        print("RTSP SETUP tracks:")
        for idx, (media, control_uri) in enumerate(tracks):
            interleaved = f"{idx * 2}-{idx * 2 + 1}"
            print(f"  {media or 'unknown'} {interleaved} {_mask_uri(control_uri)}")
            setup_headers = client.setup(control_uri, interleaved)
            transport = setup_headers.get("transport", "")
            if transport:
                print(f"    transport: {transport}")
            if media in {"application", "data"}:
                metadata_channels.add(idx * 2)
        client.play()

        deadline = time.monotonic() + duration_s
        channel_bytes: dict[int, int] = {}
        channel_printable_tail: dict[int, str] = {}
        while time.monotonic() < deadline:
            channel, payload = client.read_interleaved_payload(timeout_s=1.0)
            if not payload:
                continue
            if channel is not None:
                channel_bytes[channel] = channel_bytes.get(channel, 0) + len(payload)
            text = payload.decode("utf-8", errors="ignore")
            if text.strip():
                channel_printable_tail[channel or -1] = text[-240:].replace("\n", "\\n")
            samples = parse_telemetry_samples(text)
            if metadata_channels and channel not in metadata_channels and not samples:
                continue
            payload_bytes += len(payload)
            if text.strip():
                printable_tail = text[-240:].replace("\n", "\\n")
            for sample in samples:
                parsed_samples += 1
                print(
                    "direct sample "
                    f"pan={sample.pan_deg:+.4f}deg "
                    f"tilt={sample.tilt_deg:+.4f}deg "
                    f"norm=({sample.pan_norm:+.5f},{sample.tilt_norm:+.5f})"
                )
    finally:
        client.close()

    if "channel_bytes" in locals() and channel_bytes:
        print("Direct RTSP payload bytes by channel:")
        for channel, count in sorted(channel_bytes.items()):
            print(f"  channel {channel}: {count}")
    if "channel_printable_tail" in locals() and channel_printable_tail:
        print("Direct RTSP printable payload tail by channel:")
        for channel, tail in sorted(channel_printable_tail.items()):
            print(f"  channel {channel}: {tail}")
    if "client" in locals():
        sync_stats = client.sync_stats()
        print(
            "Direct RTSP parser sync: "
            f"discarded_bytes={sync_stats['discarded_bytes']} "
            f"bad_frame_headers={sync_stats['bad_frame_headers']}"
        )
    print(f"Direct RTSP payload bytes: {payload_bytes}")
    if printable_tail:
        print("Direct RTSP printable payload tail:")
        print(f"  {printable_tail}")
    print(f"Direct RTSP parsed samples: {parsed_samples}")
    return parsed_samples


def discover_rtsp_uri(config_path: str) -> tuple[str, dict]:
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    cam = cfg["camera"]
    stream_cfg = cfg.get("stream_metadata", {})
    timeouts = cfg.get("timeouts", {})

    camera_host = cam["host"]
    camera_port = int(cam["port"])
    stream_host = stream_cfg.get("host") or camera_host
    stream_port = int(stream_cfg.get("port", 554))

    socket.setdefaulttimeout(float(timeouts.get("soap_s", 10)))

    print(f"Connecting to ONVIF media service at {camera_host}:{camera_port} ...")
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
    print(f"Media service: {media_xaddr}")

    media = camera.create_media_service()
    profiles = media.GetProfiles()
    if not profiles:
        raise RuntimeError("Camera returned no media profiles.")

    print("Media profiles:")
    for idx, candidate in enumerate(profiles):
        name = getattr(candidate, "Name", "")
        token = candidate.token
        metadata_cfg = getattr(candidate, "MetadataConfiguration", None)
        video_encoder_cfg = getattr(candidate, "VideoEncoderConfiguration", None)
        metadata_token = getattr(metadata_cfg, "token", None) if metadata_cfg is not None else None
        encoder_token = getattr(video_encoder_cfg, "token", None) if video_encoder_cfg is not None else None
        print(
            f"  [{idx}] token={token} "
            f"name={name or '-'} "
            f"metadata={'yes' if metadata_cfg is not None else 'no'}"
            f"{f'({metadata_token})' if metadata_token else ''} "
            f"encoder={encoder_token or '-'}"
        )

    requested_profile = stream_cfg.get("profile_token")
    if requested_profile:
        matches = [profile for profile in profiles if profile.token == requested_profile]
        if not matches:
            raise RuntimeError(f"Configured stream_metadata.profile_token={requested_profile!r} was not found.")
        profile = matches[0]
    else:
        profile = profiles[0]

    profile_token = profile.token
    profile_name = getattr(profile, "Name", "")
    print(f"Using profile: {profile_token} {f'({profile_name})' if profile_name else ''}")

    req = media.create_type("GetStreamUri")
    req.StreamSetup = {
        "Stream": "RTP-Unicast",
        "Transport": {"Protocol": "RTSP"},
    }
    req.ProfileToken = profile_token
    response = media.GetStreamUri(req)
    raw_uri = getattr(response, "Uri", response)
    if not isinstance(raw_uri, str) or not raw_uri:
        raise RuntimeError("Media.GetStreamUri did not return an RTSP URI.")

    rtsp_uri = _rewrite_rtsp_url(raw_uri, stream_host, stream_port)
    rtsp_uri = _with_credentials(rtsp_uri, cam["user"], cam["password"])
    return rtsp_uri, stream_cfg


def run_probe(config_path: str, duration_s: float) -> int:
    rtsp_uri, stream_cfg = discover_rtsp_uri(config_path)
    print(f"RTSP URI: {_mask_uri(rtsp_uri)}")
    inspect_rtsp_uri(rtsp_uri, stream_cfg)
    print("Starting metadata reader. No PTZ movement commands will be sent.")

    monitor = StreamTelemetryMonitor(
        rtsp_uri,
        enabled=bool(stream_cfg.get("enabled", True)),
        ffmpeg_bin=str(stream_cfg.get("ffmpeg_bin", "ffmpeg")),
        transport=str(stream_cfg.get("transport", "tcp")),
        stream_map=str(stream_cfg.get("ffmpeg_map", "0:d:0?")),
        startup_timeout_s=float(stream_cfg.get("startup_timeout_s", 5)),
    )

    monitor.start()
    start = time.monotonic()
    last_seen_ts: float | None = None
    printed = 0

    try:
        while time.monotonic() - start < duration_s:
            sample = monitor.latest()
            if sample and sample.timestamp_s != last_seen_ts:
                age_ms = (time.perf_counter() - sample.timestamp_s) * 1000
                print(
                    "sample "
                    f"pan={sample.pan_deg:+.4f}deg "
                    f"tilt={sample.tilt_deg:+.4f}deg "
                    f"norm=({sample.pan_norm:+.5f},{sample.tilt_norm:+.5f}) "
                    f"age={age_ms:.1f}ms"
                )
                last_seen_ts = sample.timestamp_s
                printed += 1
            time.sleep(0.1)
    finally:
        monitor.stop()

    total = len(monitor.samples())
    diagnostics = monitor.diagnostics()
    print(f"Done. Samples buffered={total}, printed={printed}, status={monitor.status_note()}")
    command = diagnostics.get("command")
    if command:
        print("ffmpeg command:")
        print("  " + " ".join(str(part) for part in command))
    print(f"ffmpeg stdout bytes: {diagnostics.get('stdout_bytes')}")
    stdout_lines = diagnostics.get("stdout_tail") or []
    if stdout_lines:
        print("ffmpeg stdout tail:")
        for line in stdout_lines[-4:]:
            print(f"  {line}")
    stderr_lines = diagnostics.get("stderr") or []
    if stderr_lines:
        print("ffmpeg stderr tail:")
        for line in stderr_lines[-8:]:
            print(f"  {line}")
    print(f"ffmpeg exit code: {diagnostics.get('returncode')}")
    if total == 0:
        print(
            "No metadata PTZ samples were parsed. Check that RTSP port forwarding "
            "is correct, ffmpeg is installed, and the selected profile exposes an "
            "ONVIF metadata track."
        )
        try:
            direct_samples = direct_rtsp_metadata_probe(rtsp_uri, duration_s)
        except Exception as exc:  # noqa: BLE001
            print(f"Direct RTSP metadata probe failed: {exc}")
            return 1
        return 0 if direct_samples else 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe ONVIF RTSP PTZ metadata only.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--duration", type=float, default=15.0, help="Probe duration in seconds")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    raise SystemExit(run_probe(args.config, args.duration))


if __name__ == "__main__":
    main()
