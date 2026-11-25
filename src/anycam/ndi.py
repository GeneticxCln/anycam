"""Feature-detected NDI input/output support via GStreamer ``gst-plugin-ndi``."""
from __future__ import annotations

import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


def src_available() -> bool:
    """True when ``ndisrc`` (gst-plugin-ndi) is installed."""
    return Gst.ElementFactory.find("ndisrc") is not None


def sink_available() -> bool:
    """True when ``ndisink`` (gst-plugin-ndi) is installed."""
    return Gst.ElementFactory.find("ndisink") is not None


def discover_sources(timeout_s: float = 1.5) -> list[str]:
    """List NDI stream names visible on the LAN (empty when plugin is absent)."""
    if not src_available():
        return []
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Source/Network", None)
    if not monitor.start():
        return []
    try:
        time.sleep(max(0.1, min(3.0, float(timeout_s))))
        names: list[str] = []
        for dev in monitor.get_devices() or ():
            props = dev.get_properties()
            ndi_name = props.get_string("ndi-name") if props is not None else None
            label = ndi_name or dev.get_display_name()
            if label and label not in names:
                names.append(label)
        return names
    finally:
        monitor.stop()


def build_ndi_chain(ndi_name: str, width: int, height: int, fps: int, layer=None):
    """Build a GStreamer SourceChain receiving an NDI network video feed."""
    from .sources import (
        SourceChain,
        _add_effects,
        _finish_chain,
        _fit_letterbox,
        make,
    )

    if not src_available():
        raise RuntimeError(
            "NDI input requires gst-plugin-ndi (ndisrc) and the NDI runtime library")
    cleaned = str(ndi_name or "").strip()
    if not cleaned:
        raise ValueError("NDI source name cannot be empty")
    chain = SourceChain()
    src = make("ndisrc", ndi_name=cleaned)
    demux = Gst.ElementFactory.make("ndisrcdemux", None)
    if demux is not None:
        conv = make("videoconvert")
        chain.add(src, demux, conv)
        chain.dynamic_from = demux
    else:
        chain.add(src, make("videoconvert"))
    if layer is not None:
        _add_effects(chain, layer)
    fitted = _fit_letterbox(chain, width, height, width, height, live=True, fps=fps, layer=layer)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer, pin_rate=True)
    chain.source_size = (width, height)
    return chain
