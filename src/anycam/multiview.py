"""Multi-view preview: one live thumbnail per capture source."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QGridLayout, QLabel, QVBoxLayout

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402


class SourcePreview:
    """A small GStreamer pipeline that renders one capture channel to a QLabel."""

    def __init__(self, channel: str, label: QLabel) -> None:
        self.channel = channel
        self.label = label
        self.pipeline = Gst.Pipeline.new(f"preview-{channel}")
        src = Gst.ElementFactory.make("intervideosrc", f"mv-src-{channel}")
        src.set_property("channel", channel)
        conv = Gst.ElementFactory.make("videoconvert", f"mv-conv-{channel}")
        sink = Gst.ElementFactory.make("appsink", f"mv-sink-{channel}")
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 2)
        sink.set_property("drop", True)
        sink.set_property("sync", False)
        caps = Gst.ElementFactory.make("capsfilter", f"mv-caps-{channel}")
        caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGBA"))
        elements = (src, conv, caps, sink)
        for element in elements:
            self.pipeline.add(element)
        for first, second in zip(elements, elements[1:]):
            if not first.link(second):
                raise RuntimeError("could not link multi-view preview")
        self._last = None
        sink.connect("new-sample", self._on_sample)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        video = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        w, h = video.width, video.height
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            # Own the pixels before unmapping; QPixmap belongs on the GUI thread.
            img = QImage(info.data, w, h, video.stride[0], QImage.Format.Format_RGBA8888)
            self._last = img.copy()
        finally:
            buf.unmap(info)
        return Gst.FlowReturn.OK

    def pump(self) -> None:
        image = self._last
        self._last = None
        if image is not None:
            self.label.setPixmap(QPixmap.fromImage(image).scaled(
                self.label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def start(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
        self._last = None


class MultiViewDialog(QDialog):
    def __init__(self, pool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Multi-view")
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        grid = QGridLayout()
        layout.addLayout(grid)
        self.previews: list[SourcePreview] = []
        entries = pool.entries()
        cols = 2 if len(entries) <= 4 else 3
        for i, entry in enumerate(entries):
            label = QLabel(f"{entry.spec.kind}: {entry.spec.source or 'source'}")
            label.setMinimumSize(320, 180)
            label.setStyleSheet("background:#0e1216; border:1px solid #1e2830;")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(label, i // cols, i % cols)
            sp = SourcePreview(entry.channel, label)
            sp.start()
            self.previews.append(sp)

    def pump(self) -> None:
        for sp in self.previews:
            sp.pump()

    def closeEvent(self, event) -> None:
        for sp in self.previews:
            sp.stop()
        super().closeEvent(event)