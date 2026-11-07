"""Phone cameras over the local network: QR pairing, HTTPS page, WebRTC in.

The desktop shows a QR code.  The phone scans it, opens AnyCam's own HTTPS page
(served from this computer, nothing leaves the LAN), pairs with a one-time
token and sends its camera over WebRTC.  The received video is published on a
fixed ``intervideosink`` channel per phone (:func:`phone_channel`), which the
capture pool reads like any other source.  See ``docs/PHONE_CAMERA.md``.
"""

from __future__ import annotations

import re

PHONE_PORT = 8443
# Every phone is normalised to this frame (letterboxed when held upright), so
# a layer's crop and fit never change when the phone rotates or reconnects.
PHONE_SIZE = (1920, 1080)


def phone_channel(device_id: str) -> str:
    """The intervideo channel a paired phone's video is published on."""
    return "anycam-phone-" + re.sub(r"[^A-Za-z0-9_-]", "_", device_id or "none")
