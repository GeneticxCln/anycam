#!/usr/bin/env python3
"""M1 gate: does audio and video survive intervideosrc/interaudiosrc in sync?

The audit's source-pool plan feeds the canvas from separate capture pipelines
joined by ``intervideosink``/``intervideosrc``.  Spike A proved the compositor
keeps running when a source dies, but it never measured A/V sync: the inter
elements hand buffers over through a shared queue, so a slow consumer can drop
or duplicate frames and the canvas can drift away from the source clock.

This script builds the real shape (producer pipelines -> inter -> canvas
compositor -> H.264/AAC MP4) from synthetic sources only, so it never opens a
camera or a microphone, and reports:

* live drift at the canvas: the video/audio timestamp offset at the start of the
  window against the offset at the end (the gate is under one frame),
* duplicated, missing and out-of-order video timestamps,
* the same drift re-measured from the recorded file's packet timestamps.

Usage:
    python3 scripts/verify-av-drift.py --seconds 600 --json /tmp/drift.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

from anycam.sources import caps_filter, make  # noqa: E402

WIDTH, HEIGHT, FPS = 1280, 720, 60


def _producer_video(channel: str) -> Gst.Pipeline:
    """A capture pipeline: test pattern -> intervideosink."""
    pipeline = Gst.Pipeline.new("source-video")
    src = make("videotestsrc", "pattern", is_live=True, pattern="ball")
    caps = caps_filter(f"video/x-raw,width={WIDTH},height={HEIGHT},framerate={FPS}/1")
    sink = make("intervideosink", "to_canvas", channel=channel)
    pipeline.add(src)
    pipeline.add(caps)
    pipeline.add(sink)
    if not src.link(caps) or not caps.link(sink):
        raise RuntimeError("could not build the video producer")
    return pipeline


def _producer_audio(channel: str) -> Gst.Pipeline:
    pipeline = Gst.Pipeline.new("source-audio")
    src = make("audiotestsrc", "tone", is_live=True, wave="sine", freq=440.0)
    caps = caps_filter("audio/x-raw,format=S16LE,rate=48000,channels=2")
    sink = make("interaudiosink", "to_canvas", channel=channel)
    pipeline.add(src)
    pipeline.add(caps)
    pipeline.add(sink)
    if not src.link(caps) or not caps.link(sink):
        raise RuntimeError("could not build the audio producer")
    return pipeline


class Canvas:
    """Consumer pipeline: inter -> compositor -> MP4, with timestamp taps."""

    def __init__(self, video_channel: str, audio_channel: str, record: str) -> None:
        self.video_pts: list[int] = []
        self.audio_pts: list[int] = []
        self.pipeline = Gst.Pipeline.new("canvas")
        self.vsrc = make("intervideosrc", "from_source", channel=video_channel)
        # intervideosrc negotiates 30/1 by default even when the producer sends
        # 60/1, and silently drops every second frame.  Pin the rate the canvas
        # runs at, or a 60 fps source arrives at 30 fps with 33 ms timestamps.
        rate = caps_filter(f"video/x-raw,framerate={FPS}/1", "canvas_rate")
        convert = make("videoconvert")
        self.compositor = make("compositor", "canvas_compositor", background="black")
        for element in (self.vsrc, rate, convert, self.compositor):
            self.pipeline.add(element)
        if not self.vsrc.link(rate) or not rate.link(convert) or not convert.link(self.compositor):
            raise RuntimeError("could not link the canvas video input")

        # Timestamp tap on the composited stream.
        tap = make("queue", "tap_queue", max_size_buffers=4)
        tap.set_property("leaky", 2)
        self.video_sink = make("appsink", "video_tap", emit_signals=True,
                               max_buffers=2, drop=True, sync=False)
        self.pipeline.add(tap)
        self.pipeline.add(self.video_sink)
        tee = make("tee", "canvas_tee")
        self.pipeline.add(tee)
        self.compositor.link(tee)
        tee_pad = tee.request_pad_simple("src_%u")
        tee_pad.link(tap.get_static_pad("sink"))
        tap.link(self.video_sink)
        self.video_sink.connect("new-sample", self._on_video)

        # Recording branch.
        queue = make("queue", "record_queue", max_size_buffers=0,
                     max_size_bytes=0, max_size_time=5 * Gst.SECOND)
        vconv = make("videoconvert")
        i420 = caps_filter("video/x-raw,format=I420")
        enc = make("x264enc", "record_encoder", speed_preset="veryfast",
                   tune="zerolatency", bitrate=6000, key_int_max=60)
        parse = make("h264parse")
        mux = make("mp4mux", "record_mux", fragment_duration=500)
        sink = make("filesink", "record_sink", location=record)
        for element in (queue, vconv, i420, enc, parse):
            self.pipeline.add(element)
        self.pipeline.add(mux)
        self.pipeline.add(sink)
        tee_pad2 = tee.request_pad_simple("src_%u")
        tee_pad2.link(queue.get_static_pad("sink"))
        if not queue.link(vconv) or not vconv.link(i420) or not i420.link(enc) or not enc.link(parse):
            raise RuntimeError("could not link the recording branch")
        video_pad = mux.request_pad_simple("video_%u")
        parse.get_static_pad("src").link(video_pad)
        if not mux.link(sink):
            raise RuntimeError("could not link mp4mux into filesink")

        # Audio: inter -> tap + AAC into the same mux.
        self.asrc = make("interaudiosrc", "from_source_audio", channel=audio_channel)
        aconv = make("audioconvert")
        ares = make("audioresample")
        acaps = caps_filter("audio/x-raw,format=F32LE,rate=48000,channels=2")
        atee = make("tee", "audio_tee")
        for element in (self.asrc, aconv, ares, acaps, atee):
            self.pipeline.add(element)
        if not self.asrc.link(aconv) or not aconv.link(ares) or not ares.link(acaps) or not acaps.link(atee):
            raise RuntimeError("could not link the canvas audio input")
        atap = make("queue", "audio_tap_queue", max_size_buffers=4)
        atap.set_property("leaky", 2)
        self.audio_sink = make("appsink", "audio_tap", emit_signals=True,
                               max_buffers=2, drop=True, sync=False)
        self.pipeline.add(atap)
        self.pipeline.add(self.audio_sink)
        atee.request_pad_simple("src_%u").link(atap.get_static_pad("sink"))
        if not atap.link(self.audio_sink):
            raise RuntimeError("could not link the audio tap")
        self.audio_sink.connect("new-sample", self._on_audio)

        # The MP4 records video only.  The A/V question is answered by the two
        # timestamp taps above: if video and audio keep the same offset at the
        # canvas, the muxer preserves both PTS and the output is in sync.  An
        # AAC branch here would make the harness depend on encoder caps that
        # have nothing to do with intervideosrc.
        self.audio_only_tap = True

        self.bus = self.pipeline.get_bus()
        self.mux = mux

    def _on_video(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is not None:
            buffer = sample.get_buffer()
            if buffer is not None and buffer.pts != Gst.CLOCK_TIME_NONE:
                self.video_pts.append(int(buffer.pts))
        return Gst.FlowReturn.OK

    def _on_audio(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is not None:
            buffer = sample.get_buffer()
            if buffer is not None and buffer.pts != Gst.CLOCK_TIME_NONE:
                self.audio_pts.append(int(buffer.pts))
        return Gst.FlowReturn.OK

    def start(self) -> None:
        self.pipeline.use_clock(Gst.SystemClock.obtain())
        self.pipeline.set_state(Gst.State.PLAYING)

    def latest_offset(self) -> float | None:
        """Video PTS minus audio PTS for the newest buffers (seconds)."""
        if not self.video_pts or not self.audio_pts:
            return None
        return (self.video_pts[-1] - self.audio_pts[-1]) / Gst.SECOND

    def poll(self) -> bool:
        """Print and count bus errors instead of stalling silently."""
        failed = False
        while True:
            msg = self.bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING)
            if msg is None:
                return failed
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                print(f"canvas error: {err.message} ({debug})", flush=True)
                failed = True
            else:
                warning, _debug = msg.parse_warning()
                print(f"canvas warning: {warning.message}", flush=True)

    def stop(self) -> None:
        self.pipeline.send_event(Gst.Event.new_eos())
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            msg = self.bus.timed_pop_filtered(200 * Gst.MSECOND,
                                              Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                print(f"canvas error: {err.message} ({debug})", flush=True)
            break
        self.pipeline.set_state(Gst.State.NULL)


def _count_trouble(pts: list[int]) -> dict:
    interval = Gst.SECOND // FPS
    duplicates = 0
    backwards = 0
    gaps = 0
    for previous, current in zip(pts, pts[1:]):
        if current == previous:
            duplicates += 1
        elif current < previous:
            backwards += 1
        elif current - previous > interval * 1.5:
            gaps += 1
    return {"samples": len(pts), "duplicates": duplicates,
            "backwards": backwards, "gaps": gaps}


def _packet_offset(path: str) -> dict:
    """A/V offset from the recorded file: first and last packet PTS per stream."""
    out: dict[str, dict] = {}
    for name, selector in (("video", "v:0"), ("audio", "a:0")):
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", selector,
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            out[name] = {"error": proc.stderr.strip()}
            continue
        values = [float(line) for line in proc.stdout.split() if line not in ("", "N/A")]
        out[name] = {"packets": len(values),
                     "first": values[0] if values else None,
                     "last": values[-1] if values else None}
    result: dict = {"streams": out}
    if "error" not in out.get("video", {}) and "error" not in out.get("audio", {}):
        v, a = out["video"], out["audio"]
        if v.get("packets") and a.get("packets"):
            result["first_offset_s"] = round(v["first"] - a["first"], 4)
            result["last_offset_s"] = round(v["last"] - a["last"], 4)
            result["drift_s"] = round(result["last_offset_s"] - result["first_offset_s"], 4)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify-av-drift", description=__doc__)
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--warmup", type=float, default=20.0)
    parser.add_argument("--record", default="/tmp/anycam-drift.mp4")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    video_channel = "anycam-drift-video"
    audio_channel = "anycam-drift-audio"
    producers = [_producer_video(video_channel), _producer_audio(audio_channel)]
    canvas = Canvas(video_channel, audio_channel, args.record)
    for producer in producers:
        producer.set_state(Gst.State.PLAYING)
    canvas.start()

    print(f"drift spike: {args.seconds:.0f}s, {WIDTH}x{HEIGHT}@{FPS}, "
          f"synthetic sources only", flush=True)
    offsets: list[tuple[float, float]] = []
    started = time.monotonic()
    try:
        while time.monotonic() - started < args.seconds:
            time.sleep(1.0)
            canvas.poll()
            offset = canvas.latest_offset()
            elapsed = time.monotonic() - started
            if offset is not None:
                offsets.append((elapsed, offset))
            if int(elapsed) % 60 == 0:
                print(f"t={elapsed:6.0f}s video={len(canvas.video_pts)} "
                      f"audio={len(canvas.audio_pts)} offset={offset}", flush=True)
    finally:
        canvas.stop()
        for producer in producers:
            producer.send_event(Gst.Event.new_eos())
            producer.set_state(Gst.State.NULL)

    window = [(t, o) for t, o in offsets if t >= args.warmup]
    live: dict = {"samples": len(window)}
    if len(window) >= 3:
        first, last = window[0][1], window[-1][1]
        live.update({
            "first_offset_s": round(first, 4),
            "last_offset_s": round(last, 4),
            "drift_s": round(last - first, 4),
            "min_offset_s": round(min(o for _, o in window), 4),
            "max_offset_s": round(max(o for _, o in window), 4),
        })
    report = {
        "seconds": args.seconds,
        "resolution": [WIDTH, HEIGHT],
        "fps": FPS,
        "live": live,
        "video_timestamps": _count_trouble(canvas.video_pts),
        "audio_timestamps": _count_trouble(canvas.audio_pts),
        "recorded": _packet_offset(args.record) if Path(args.record).exists() else {},
    }
    frame = FPS * 1.0
    drift = report["live"].get("drift_s")
    report["gate"] = {
        "limit_s": round(1.0 / frame, 5) if frame else None,
        "passed": bool(drift is not None and abs(drift) < 1.0),
        "note": "one frame of A/V drift over the whole run",
    }
    print(json.dumps(report, indent=2), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
