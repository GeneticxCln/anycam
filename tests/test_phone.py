"""Phone camera: QR pairing, the HTTPS link, the WebRTC receiver, the pool."""

from __future__ import annotations

import http.client
import json
import shutil
import ssl
import threading
import time

import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

from anycam.phone import PHONE_SIZE, phone_channel  # noqa: E402
from anycam.phone.auth import Pairing  # noqa: E402


# ------------------------------------------------------------------ pairing
def test_a_pairing_token_works_once(tmp_path):
    pairing = Pairing(tmp_path / "phones.json")
    token = pairing.new_token()
    device, secret = pairing.pair(token, "Pixel")
    assert pairing.check(f"{device}.{secret}") == device
    assert pairing.pair(token, "Pixel") is None, "a used QR code must not pair again"
    assert pairing.check(f"{device}.wrong") is None
    assert pairing.check("nobody.secret") is None
    assert pairing.name(device) == "Pixel"


def test_an_expired_token_does_not_pair(tmp_path, monkeypatch):
    pairing = Pairing(tmp_path / "phones.json")
    token = pairing.new_token()
    real = time.monotonic
    monkeypatch.setattr("anycam.phone.auth.time.monotonic", lambda: real() + 301)
    assert pairing.pair(token, "Pixel") is None


def test_secrets_are_stored_hashed_and_survive_a_restart(tmp_path):
    path = tmp_path / "phones.json"
    pairing = Pairing(path)
    device, secret = pairing.pair(pairing.new_token(), "Pixel\x00\n 8")
    text = path.read_text()
    assert secret not in text
    assert (path.stat().st_mode & 0o777) == 0o600
    again = Pairing(path)
    assert again.check(f"{device}.{secret}") == device
    assert again.name(device) == "Pixel 8"
    assert again.forget(device)
    assert Pairing(path).check(f"{device}.{secret}") is None


def test_the_offer_loses_the_video_orientation_extension():
    from anycam.phone.webrtc import prepare_offer

    offer = ("v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
             "a=extmap:13 urn:3gpp:video-orientation\r\na=rtpmap:96 VP8/90000\r\n")
    cleaned = prepare_offer(offer)
    assert "video-orientation" not in cleaned
    assert "a=rtpmap:96 VP8/90000\r\n" in cleaned


# --------------------------------------------------------------- the server
@pytest.fixture
def service(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    from anycam.phone.server import PhoneService

    events: list[tuple[str, str, str]] = []
    svc = PhoneService(state_dir=tmp_path / "state", cert_dir=tmp_path / "cert",
                       host="127.0.0.1", port=0,
                       on_event=lambda *event: events.append(event))
    svc.start()
    svc.events = events
    yield svc
    svc.stop()


def _request(svc, method, path, body=None, headers=None):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(svc.host, svc.port, timeout=10,
                                             context=context)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _pair(svc):
    token = svc.pairing_url().split("#pair=", 1)[1]
    status, _headers, body = _request(
        svc, "POST", "/api/pair", json.dumps({"token": token, "name": "Pixel"}),
        {"Content-Type": "application/json"})
    return token, status, json.loads(body)


def test_the_phone_page_is_served_over_https(service):
    status, headers, body = _request(service, "GET", "/")
    assert status == 200
    assert b"phone.js" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    status, headers, body = _request(service, "GET", "/phone.js")
    assert status == 200 and b"RTCPeerConnection" in body
    assert _request(service, "GET", "/../server.py")[0] == 404
    assert len(service.fingerprint.split(":")) == 32


def test_an_unpaired_phone_gets_nothing(service):
    assert _request(service, "GET", "/api/check")[0] == 401
    status, _h, _b = _request(service, "POST", "/api/whip", "v=0\r\nm=video",
                              {"Content-Type": "application/sdp"})
    assert status == 401
    status, _h, _b = _request(service, "POST", "/api/whip", "v=0\r\nm=video",
                              {"Authorization": "Bearer phone-x.guess"})
    assert status == 401
    assert _request(service, "DELETE", "/api/whip/abc")[0] == 401
    assert service.receivers == {}


def test_pairing_over_https_and_token_replay(service):
    token, status, data = _pair(service)
    assert status == 200
    bearer = {"Authorization": f"Bearer {data['device']}.{data['secret']}"}
    status, _h, body = _request(service, "GET", "/api/check", headers=bearer)
    assert status == 200 and json.loads(body)["name"] == "Pixel"
    replay = _request(service, "POST", "/api/pair",
                      json.dumps({"token": token, "name": "Thief"}))
    assert replay[0] == 403
    assert ("paired", data["device"], "Pixel") in service.events


def test_oversized_and_malformed_requests_are_refused(service):
    assert _request(service, "POST", "/api/pair", "x" * (70 * 1024))[0] == 413
    assert _request(service, "POST", "/api/pair", "not json")[0] == 400
    _token, _status, data = _pair(service)
    bearer = {"Authorization": f"Bearer {data['device']}.{data['secret']}"}
    status, _h, _b = _request(service, "POST", "/api/whip", "v=0\r\n", bearer)
    assert status == 400, "an offer without video is refused before webrtcbin"


def test_the_pairing_link_keeps_the_token_out_of_requests(service):
    url = service.pairing_url()
    assert url.startswith(f"https://127.0.0.1:{service.port}/#pair=")


def test_qr_code_png():
    from anycam.phone.tls import qr_png

    png = qr_png("https://192.168.1.2:8443/#pair=abc")
    try:
        import segno  # noqa: F401
        have_encoder = True
    except ImportError:
        have_encoder = shutil.which("qrencode") is not None
    if not have_encoder:
        assert png is None
    else:
        assert png and png.startswith(b"\x89PNG")


# ----------------------------------------------------------- pool + channel
def _pull_gray(channel_pipeline_desc: str, frames: int = 15) -> bytes:
    pipeline = Gst.parse_launch(channel_pipeline_desc)
    sink = pipeline.get_by_name("out")
    pipeline.set_state(Gst.State.PLAYING)
    sample = None
    try:
        for _ in range(frames):
            sample = sink.emit("try-pull-sample", Gst.SECOND) or sample
    finally:
        pipeline.set_state(Gst.State.NULL)
    assert sample is not None
    buffer = sample.get_buffer()
    ok, info = buffer.map(Gst.MapFlags.READ)
    assert ok
    try:
        return bytes(info.data)
    finally:
        buffer.unmap(info)


def test_a_phone_layer_is_a_pooled_capture_of_its_channel():
    from anycam.model import Layer
    from anycam.pool import SourcePool, capture_spec

    layer = Layer("layer_p", "phone", "Pixel", source="phone-test1")
    spec = capture_spec(layer, 1920, 1080, 30)
    assert (spec.kind, spec.source, spec.width, spec.height) == (
        "phone", "phone-test1", *PHONE_SIZE)
    pool = SourcePool()
    pool.start()
    sender = Gst.parse_launch(
        "videotestsrc is-live=true pattern=white ! "
        f"video/x-raw,format=I420,width={PHONE_SIZE[0]},height={PHONE_SIZE[1]},"
        "framerate=30/1,pixel-aspect-ratio=1/1 ! "
        f"intervideosink channel={phone_channel('phone-test1')} sync=false")
    try:
        entry = pool.acquire(spec)
        assert entry.source_size == PHONE_SIZE
        dark = _pull_gray(
            f"intervideosrc channel={entry.channel} ! videoconvert ! "
            "video/x-raw,format=GRAY8 ! appsink name=out sync=false")
        assert max(dark[::4099]) < 40, "no phone yet: the layer shows black"
        sender.set_state(Gst.State.PLAYING)
        time.sleep(0.5)
        bright = _pull_gray(
            f"intervideosrc channel={entry.channel} ! videoconvert ! "
            "video/x-raw,format=GRAY8 ! appsink name=out sync=false")
        assert min(bright[::4099]) > 200, "the phone's frames reach the pool channel"
        assert entry.error == ""
    finally:
        sender.set_state(Gst.State.NULL)
        pool.release(spec)
        pool.stop()


# ------------------------------------------------- WebRTC, GStreamer only
def _webrtc_available() -> bool:
    try:
        gi.require_version("GstWebRTC", "1.0")
        gi.require_version("GstSdp", "1.0")
    except ValueError:
        return False
    return all(Gst.ElementFactory.find(name) is not None
               for name in ("webrtcbin", "nicesrc", "vp8enc", "vp8dec", "rtpvp8pay"))


@pytest.mark.skipif(not _webrtc_available(), reason="webrtcbin/libnice/VP8 not installed")
def test_a_webrtc_sender_reaches_the_phone_channel():
    """A GStreamer webrtcbin plays the phone: offer -> answer -> frames."""
    from gi.repository import GstSdp, GstWebRTC

    from anycam.phone.webrtc import PhoneReceiver

    sender = Gst.parse_launch(
        "webrtcbin name=w bundle-policy=max-bundle "
        "videotestsrc is-live=true pattern=white ! "
        "video/x-raw,width=320,height=240,framerate=30/1 ! videoconvert ! "
        "vp8enc deadline=1 ! rtpvp8pay ! "
        "application/x-rtp,media=video,encoding-name=VP8,payload=96 ! w.")
    webrtc = sender.get_by_name("w")
    gathered = threading.Event()

    def on_gathering(element, _pspec):
        if element.get_property("ice-gathering-state") == \
                GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            gathered.set()

    webrtc.connect("notify::ice-gathering-state", on_gathering)
    events: list[str] = []
    receiver = PhoneReceiver("phone-loop", on_event=lambda event, _d: events.append(event))
    sender.set_state(Gst.State.PLAYING)
    try:
        offer = PhoneReceiver._call(webrtc, "create-offer", None, 10, "offer")
        PhoneReceiver._call(webrtc, "set-local-description", offer, 10)
        assert gathered.wait(10)
        sdp = webrtc.get_property("local-description").sdp.as_text()
        answer_sdp = receiver.answer(sdp)
        assert "a=candidate" in answer_sdp
        _res, message = GstSdp.SDPMessage.new_from_text(answer_sdp)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, message)
        PhoneReceiver._call(webrtc, "set-remote-description", answer, 10)
        deadline = time.monotonic() + 15
        while receiver.frames < 10 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert receiver.frames >= 10, f"no frames over WebRTC ({receiver.error})"
        assert "streaming" in events
        frame = _pull_gray(
            f"intervideosrc channel={phone_channel('phone-loop')} ! "
            f"video/x-raw,width={PHONE_SIZE[0]},height={PHONE_SIZE[1]} ! "
            "videoconvert ! video/x-raw,format=GRAY8 ! appsink name=out sync=false")
        width = PHONE_SIZE[0]
        middle = frame[(PHONE_SIZE[1] // 2) * width + width // 2]
        corner = frame[(PHONE_SIZE[1] // 2) * width + 4]
        assert middle > 200, "the white test picture arrives"
        assert corner < 40, "a 4:3 phone picture is letterboxed, not stretched"
    finally:
        receiver.stop()
        sender.set_state(Gst.State.NULL)
    assert receiver.closed


# ----------------------------------------------------------------- firewall
def _ufw(tmp_path, enabled="yes", policy="DROP", rules=""):
    ufw_dir = tmp_path / "ufw"
    ufw_dir.mkdir(exist_ok=True)
    (ufw_dir / "ufw.conf").write_text(f"ENABLED={enabled}\nLOGLEVEL=low\n")
    (ufw_dir / "user.rules").write_text("*filter\n" + rules + "COMMIT\n")
    defaults = tmp_path / "default-ufw"
    defaults.write_text(f'DEFAULT_INPUT_POLICY="{policy}"\n')
    return {"ufw_dir": ufw_dir, "defaults": defaults}


def test_ufw_blocking_is_detected_without_root(tmp_path):
    from anycam.phone.firewall import ufw_blocks

    assert ufw_blocks(8443, **_ufw(tmp_path))
    assert not ufw_blocks(8443, **_ufw(tmp_path, enabled="no"))
    assert not ufw_blocks(8443, **_ufw(tmp_path, policy="ACCEPT"))
    allowed = "### tuple ### allow tcp 8443 0.0.0.0/0 any 0.0.0.0/0 in comment=x\n"
    assert not ufw_blocks(8443, **_ufw(tmp_path, rules=allowed))
    ranged = "### tuple ### allow any 8000:9000 0.0.0.0/0 any 0.0.0.0/0 in\n"
    assert not ufw_blocks(8443, **_ufw(tmp_path, rules=ranged))
    other = "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
    assert ufw_blocks(8443, **_ufw(tmp_path, rules=other))
    udp_only = "### tuple ### allow udp 8443 0.0.0.0/0 any 0.0.0.0/0 in\n"
    assert ufw_blocks(8443, **_ufw(tmp_path, rules=udp_only))


def test_a_missing_password_agent_is_recognised():
    from anycam.phone.firewall import no_agent

    assert no_agent("Error executing command as another user: No authentication agent found.")
    assert no_agent("Error creating textual authentication agent: Error opening current "
                    "controlling terminal for the process (`/dev/tty'): No such device or address")
    assert not no_agent("Error executing command as another user: Not authorized")


# ------------------------------------------------------------ certificates
def test_the_certificate_is_made_in_process_when_cryptography_is_there(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from anycam.phone import tls

    monkeypatch.setattr(tls.shutil, "which", lambda name: None)   # no openssl at all
    cert, key = tls.ensure_certificate(tmp_path / "c", host="192.168.1.20")
    assert (key.stat().st_mode & 0o777) == 0o600
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)                            # a usable pair
    from cryptography import x509

    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    names = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "192.168.1.20" in [str(ip) for ip in names.get_values_for_type(x509.IPAddress)]
    assert len(tls.fingerprint(cert).split(":")) == 32


def test_without_cryptography_the_openssl_command_is_used(tmp_path, monkeypatch):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    from anycam.phone import tls

    monkeypatch.setattr(tls, "_create_with_cryptography", lambda *a, **k: False)
    cert, key = tls.ensure_certificate(tmp_path / "c")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    assert (key.stat().st_mode & 0o777) == 0o600


def test_with_neither_the_error_says_what_to_install(tmp_path, monkeypatch):
    from anycam.phone import tls

    monkeypatch.setattr(tls, "_create_with_cryptography", lambda *a, **k: False)
    monkeypatch.setattr(tls.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="cryptography"):
        tls.ensure_certificate(tmp_path / "c")


# --------------------------------------------------------------- firewalld
class _FakeFirewalld:
    """A system bus stand-in that answers like firewalld and records calls."""

    def __init__(self, open_ports=(), fail=None):
        self.open_ports = set(open_ports)
        self.calls = []
        self.fail = fail or {}

    def call_sync(self, name, path, interface, method, params, reply_type, flags,
                  timeout, cancellable):
        from gi.repository import GLib

        args = params.unpack() if params is not None else ()
        self.calls.append((path, interface, method, args, int(flags)))
        if method in self.fail:
            raise RuntimeError(self.fail[method])
        if method == "NameHasOwner":
            return GLib.Variant("(b)", (True,))
        if method == "getZoneOfInterface":
            return GLib.Variant("(s)", ("home",))
        if method == "getDefaultZone":
            return GLib.Variant("(s)", ("public",))
        if method == "queryPort":
            return GLib.Variant("(b)", (args[1] in self.open_ports,))
        if method == "addPort" and interface.endswith(".zone") and len(args) == 4:
            self.open_ports.add(args[1])
            return GLib.Variant("(s)", (args[0],))
        if method == "getZoneByName":
            return GLib.Variant("(o)", ("/org/fedoraproject/FirewallD1/config/zone/3",))
        return None


def test_firewalld_is_queried_and_opened_over_dbus(monkeypatch):
    from anycam.phone import firewall

    bus = _FakeFirewalld()
    monkeypatch.setattr(firewall, "_bus", lambda: bus)
    monkeypatch.setattr(firewall, "_interface_of", lambda host: "wlan0")
    assert firewall.backend() == "firewalld"
    assert firewall.blocks(8443, "192.168.1.20")
    assert firewall.firewalld_allow(8443, "192.168.1.20") == ""
    assert not firewall.blocks(8443, "192.168.1.20")
    methods = [(call[2], call[3]) for call in bus.calls]
    assert ("getZoneOfInterface", ("wlan0",)) in methods
    assert ("addPort", ("home", "8443", "tcp", 0)) in methods, "runtime rule"
    assert ("addPort", ("8443", "tcp")) in methods, "permanent rule on the zone config"
    interactive = [call for call in bus.calls if call[2] == "addPort"]
    assert all(call[4] != 0 for call in interactive), "polkit may prompt for these"


def test_firewalld_already_open_is_fine_and_refusal_is_reported(monkeypatch):
    from anycam.phone import firewall

    monkeypatch.setattr(firewall, "_interface_of", lambda host: "")
    bus = _FakeFirewalld(fail={"addPort": "ALREADY_ENABLED: 8443:tcp"})
    monkeypatch.setattr(firewall, "_bus", lambda: bus)
    assert firewall.firewalld_allow(8443) == ""
    bus = _FakeFirewalld(fail={"addPort": "org.fedoraproject.FirewallD1.NotAuthorized"})
    monkeypatch.setattr(firewall, "_bus", lambda: bus)
    reason = firewall.firewalld_allow(8443)
    assert "NotAuthorized" in reason and firewall.auth_missing(reason)


def test_without_firewalld_ufw_is_used(monkeypatch):
    from anycam.phone import firewall

    monkeypatch.setattr(firewall, "firewalld_running", lambda: False)
    monkeypatch.setattr(firewall.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert firewall.backend() == "ufw"
    monkeypatch.setattr(firewall.shutil, "which", lambda name: None)
    monkeypatch.setattr(firewall, "UFW_DIR", firewall.Path("/nonexistent"))
    assert firewall.backend() == ""


def test_the_real_system_bus_check_never_raises():
    from anycam.phone import firewall

    assert firewall.firewalld_running() in (True, False)
