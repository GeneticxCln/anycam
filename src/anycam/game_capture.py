"""Window and game capture helpers (PipeWire vkcapture/glcapture, Portal Window, X11)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


def list_pipewire_game_nodes() -> list[dict]:
    """Return active PipeWire Video/Source nodes (obs-vkcapture, gamescope, window streams)."""
    pw_dump = shutil.which("pw-dump")
    if pw_dump is None:
        return []
    try:
        res = subprocess.run([pw_dump], capture_output=True, text=True, timeout=3.0)
        if res.returncode != 0 or not res.stdout.strip():
            return []
        objs = json.loads(res.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    found: list[dict] = []
    for obj in objs if isinstance(objs, list) else []:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        media_class = str(props.get("media.class") or "")
        if "Video/Source" not in media_class and "Stream/Output/Video" not in media_class:
            continue
        name = str(props.get("node.name") or "")
        desc = str(props.get("node.description") or props.get("application.name") or name)
        # Skip AnyCam's own virtual camera nodes and regular V4L2 webcams.
        if name.startswith("anycam") or name.startswith("v4l2_input"):
            continue
        node_id = obj.get("id")
        if isinstance(node_id, int):
            is_game = any(
                tok in (name + " " + desc).lower()
                for tok in ("vkcapture", "glcapture", "gamescope", "obs", "vulkan", "steam")
            )
            found.append({
                "node_id": node_id,
                "name": desc or f"PipeWire node {node_id}",
                "node_name": name,
                "is_game": is_game,
            })
    found.sort(key=lambda item: (not item["is_game"], item["name"].lower()))
    return found


def x11_capture_available() -> bool:
    """True when GStreamer ``ximagesrc`` is installed and an X11 display is present."""
    return bool(os.environ.get("DISPLAY")) and Gst.ElementFactory.find("ximagesrc") is not None


def build_game_chain(source: str, width: int, height: int, fps: int, layer=None):
    """Build a GStreamer SourceChain for ``pw:<node_id>`` or ``x11:<xid>`` game/window capture."""
    from .sources import (
        SourceChain,
        _add_effects,
        _finish_chain,
        _fit_letterbox,
        make,
    )

    spec = str(source or "").strip()
    chain = SourceChain()
    if spec.startswith("pw:"):
        node_id = spec[3:].strip()
        if not node_id.isdigit():
            raise ValueError(f"invalid PipeWire node id: {source!r}")
        if Gst.ElementFactory.find("pipewiresrc") is None:
            raise RuntimeError("PipeWire game capture requires GStreamer pipewiresrc")
        src = make("pipewiresrc", path=node_id, do_timestamp=True, always_copy=True)
        try:
            src.set_property("provide-clock", False)
        except (TypeError, ValueError):
            pass
    elif spec.startswith("x11:"):
        raw_xid = spec[4:].strip()
        try:
            xid = int(raw_xid, 0)
        except ValueError as exc:
            raise ValueError(f"invalid X11 window id: {source!r}") from exc
        if Gst.ElementFactory.find("ximagesrc") is None:
            raise RuntimeError("X11 window capture requires GStreamer ximagesrc")
        src = make("ximagesrc", xid=xid, use_damage=False, show_pointer=True)
    else:
        raise ValueError(f"unsupported game/window capture source: {source!r}")

    chain.add(src, make("videoconvert"))
    if layer is not None:
        _add_effects(chain, layer)
    fitted = _fit_letterbox(chain, width, height, width, height, live=True, fps=fps, layer=layer)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer, pin_rate=True)
    chain.source_size = (width, height)
    return chain
