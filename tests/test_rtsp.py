"""Local RTSP integration test for the network source layer.

The test starts a tiny RTSP control server and an ffmpeg RTP producer, then
feeds ``rtsp://127.0.0.1:<port>/test`` into a real AnyCam engine.  It is kept
out of the default CI path when ffmpeg is absent, but otherwise exercises the
actual ``uridecodebin``/``rtspsrc`` path rather than mocking it.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("gi")
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_COMPOSITOR = Gst.ElementFactory.find("compositor") is not None
    HAS_RTSP = Gst.ElementFactory.find("rtspsrc") is not None
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_COMPOSITOR = False
    HAS_RTSP = False

from anycam.model import Layer, Project, Scene
from anycam.pipeline import AnyCamEngine

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or not HAS_COMPOSITOR or not HAS_RTSP,
    reason="ffmpeg, GStreamer compositor and rtspsrc are required",
)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _RTSPTestServer:
    """Minimal RTSP server that forwards an ffmpeg RTP stream to one client."""

    def __init__(self, sdp_path: Path, rtp_port: int):
        self.sdp_path = sdp_path
        self.rtp_port = rtp_port
        self._stop = threading.Event()
        self._session = "12345678"
        self._client_rtp = 0
        self._client_addr: tuple[str, int] | None = None
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind(("127.0.0.1", rtp_port))
        self._udp.settimeout(0.5)
        self._control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._control.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._control.bind(("127.0.0.1", 0))
        self._control.listen(1)
        self.port = int(self._control.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._forward = threading.Thread(target=self._forward_rtp, daemon=True)

    def start(self) -> "_RTSPTestServer":
        self._thread.start()
        self._forward.start()
        return self

    def close(self) -> None:
        self._stop.set()
        for sock in (self._control, self._udp):
            try:
                sock.close()
            except OSError:
                pass
        for thread in (self._thread, self._forward):
            thread.join(timeout=2)

    def _read_request(self, conn: socket.socket) -> tuple[str, dict[str, str]] | None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.split("\r\n")
        method = lines[0].split(" ", 1)[0].upper()
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        return method, headers

    def _reply(self, conn: socket.socket, cseq: str, headers: list[str],
               body: str = "") -> None:
        payload = body.encode()
        lines = ["RTSP/1.0 200 OK", f"CSeq: {cseq}",
                 f"Session: {self._session};timeout=60"]
        lines.extend(headers)
        if body:
            lines.append("Content-Type: application/sdp")
            lines.append(f"Content-Length: {len(payload)}")
        else:
            lines.append("Content-Length: 0")
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)

    def _serve(self) -> None:
        conn = None
        try:
            self._control.settimeout(5)
            conn, addr = self._control.accept()
            self._client_addr = addr
            conn.settimeout(2)
            while not self._stop.is_set():
                request = self._read_request(conn)
                if request is None:
                    break
                method, headers = request
                cseq = headers.get("cseq", "1")
                if method == "OPTIONS":
                    self._reply(conn, cseq,
                                ["Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"])
                elif method == "DESCRIBE":
                    sdp = self.sdp_path.read_text()
                    self._reply(conn, cseq, [], sdp)
                elif method == "SETUP":
                    transport = headers.get("transport", "")
                    for item in transport.split(";"):
                        if item.strip().startswith("client_port="):
                            port = item.split("=", 1)[1].split("-", 1)[0]
                            self._client_rtp = int(port)
                    self._reply(
                        conn, cseq,
                        [f"Transport: RTP/AVP;unicast;client_port={self._client_rtp}-"
                         f"{self._client_rtp + 1};server_port={self.rtp_port}-"
                         f"{self.rtp_port + 1};ssrc=42"],
                    )
                elif method in ("PLAY", "PAUSE"):
                    self._reply(conn, cseq, [])
                elif method == "TEARDOWN":
                    self._reply(conn, cseq, [])
                    break
        except (OSError, ValueError, KeyError):
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def _forward_rtp(self) -> None:
        while not self._stop.is_set():
            try:
                packet, _source = self._udp.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            if not self._client_rtp or self._client_addr is None:
                continue
            try:
                self._udp.sendto(packet, (self._client_addr[0], self._client_rtp))
            except OSError:
                return


def _make_clip(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-t", "2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(path)],
        check=True, timeout=20,
    )


def _start_rtp_producer(clip: Path, sdp: Path, rtp_port: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-stream_loop", "-1", "-i", str(clip), "-an", "-c:v", "copy",
         "-f", "rtp", "-sdp_file", str(sdp), "-payload_type", "96",
         f"rtp://127.0.0.1:{rtp_port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def test_network_layer_decodes_a_local_rtsp_stream(tmp_path):
    clip = tmp_path / "clip.mp4"
    sdp = tmp_path / "stream.sdp"
    _make_clip(clip)
    rtp_port = _free_udp_port()
    producer = _start_rtp_producer(clip, sdp, rtp_port)
    server = None
    try:
        deadline = time.monotonic() + 5
        while not sdp.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sdp.exists(), "ffmpeg did not write the RTP SDP"
        server = _RTSPTestServer(sdp, rtp_port).start()

        project = Project(name="rtsp")
        project.output.canvas_w = 320
        project.output.canvas_h = 180
        project.output.fps = 30
        scene = Scene("scene_rtsp", "RTSP")
        scene.add_layer(Layer(
            "layer_network", "network", "Local RTSP",
            source=f"rtsp://127.0.0.1:{server.port}/test",
            box=(0.0, 0.0, 1.0, 1.0), z=1,
        ))
        project.scenes = [scene]
        project.active_scene = scene.id
        frames = []
        engine = AnyCamEngine(project, preview=True,
                              on_frame=frames.append).build()
        try:
            report = engine.run(4.0)
        finally:
            engine.pipeline.set_state(Gst.State.NULL)
        assert report["error"] == "", report["error"]
        assert frames, "network layer produced no preview samples"
        assert report["frames"] > 10
    finally:
        if server is not None:
            server.close()
        producer.terminate()
        try:
            producer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            producer.kill()
