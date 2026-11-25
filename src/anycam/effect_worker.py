"""Run picture processors in their own process.

The AI effects load large models and, for effect packs, third-party code.  A
crash, a hang or a memory blow-up there must not take the canvas, the recording
or the stream down with it, so a processor can run in a worker process:

* frames travel through one ``multiprocessing.shared_memory`` block (no pickling
  of pixels), a ``Pipe`` carries the small control messages;
* until the worker is ready, and whenever it is gone or too slow, the frame
  passes through unchanged, so the picture never stalls;
* a dead or hung worker is replaced after 1, 2, 4 ... 10 seconds.

``RemoteProcessor`` has the same interface as :class:`anycam.processors.Processor`.
"""
from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import threading
import time
import weakref
from multiprocessing import shared_memory
from pathlib import Path

from PIL import Image

# Processors that load models or third-party code run isolated; the cheap ones
# (touch-ups, zoom) stay in-process where a copy of every frame would cost more
# than the work.
ISOLATED_KINDS = frozenset({"background", "spotlight", "sticker", "mask", "privacy",
                            "touchup", "autoframe", "pack", "selftest"})

MAX_CONTROL_BYTES = 64 * 1024

_enabled = False
_live_lock = threading.Lock()
_live_workers: weakref.WeakSet[RemoteProcessor] = weakref.WeakSet()


def set_isolation(enabled: bool) -> None:
    """Turn process isolation on (the window does, from the project setting)."""
    global _enabled
    _enabled = bool(enabled)


def isolation_enabled() -> bool:
    env = os.environ.get("ANYCAM_AI_WORKER", "").strip().lower()
    if env in ("0", "off", "false", "no"):
        return False
    if env in ("1", "on", "true", "yes"):
        return True
    return _enabled


_local = threading.local()


class in_process:
    """Context: processors built in this thread stay in-process (one-off renders).

    A thumbnail is drawn once and thrown away; waiting for a worker to start
    would only return the untouched picture.  Packs are the exception: their
    code is never run in the main process, so a pack tile is drawn from the
    original picture.
    """

    def __enter__(self):
        _local.depth = getattr(_local, "depth", 0) + 1
        return self

    def __exit__(self, *_exc) -> None:
        _local.depth -= 1


def wants_isolation(spec_name: str) -> bool:
    kind = spec_name.partition(":")[0]
    if kind == "pack":
        return True              # third-party code never runs in the main process
    if spec_name in ("touchup:glow", "touchup:night"):
        return False             # pure pixel effects, no AI model
    if getattr(_local, "depth", 0) > 0:
        return False
    return isolation_enabled() and kind in ISOLATED_KINDS


def notify_runtime_settings(device: str | None = None, model: str | None = None) -> None:
    """Push updated inference device / segmentation model settings to live workers."""
    with _live_lock:
        workers = list(_live_workers)
    for worker in workers:
        try:
            worker.set_runtime_settings(device=device, model=model)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ safe IPC
def _send_msg(conn, payload: list) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_CONTROL_BYTES:
        raise ValueError("control message exceeds limit")
    conn.send_bytes(raw)


def _recv_msg(conn) -> list:
    raw = conn.recv_bytes(MAX_CONTROL_BYTES)
    if len(raw) > MAX_CONTROL_BYTES:
        raise ValueError("control message exceeds limit")
    message = json.loads(raw.decode("utf-8"))
    if not isinstance(message, list) or not message or not isinstance(message[0], str):
        raise ValueError("malformed worker control message")
    return message


def _attach_shm(name: str) -> shared_memory.SharedMemory:
    """Attach to parent-owned shared memory without registering in child tracker."""
    try:
        return shared_memory.SharedMemory(name=name, track=False)
    except TypeError:
        return shared_memory.SharedMemory(name=name)


# ------------------------------------------------------------------ the worker
def _serve(conn, spec_name: str, amount: float, device: str = "cpu",
           model: str = "mediapipe", packs_root: str | None = None) -> None:  # pragma: no cover - child
    """Child process: build the processor, then answer frame requests."""
    from . import inference, packs, processors, segment

    if spec_name.startswith("pack:"):
        try:
            import resource

            limit = 2 * 1024 * 1024 * 1024  # 2 GiB cap for third-party effect packs
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            if hard == resource.RLIM_INFINITY or hard > limit:
                resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
            elif soft == resource.RLIM_INFINITY or soft > hard:
                resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
        except (ImportError, OSError, ValueError):
            pass

    try:
        inference.set_preference(device)
        segment.set_model(model)
        if packs_root:
            packs.set_custom_root(Path(packs_root))
        processor = processors.make_processor(spec_name, amount)
    except Exception as exc:  # noqa: BLE001
        try:
            _send_msg(conn, ["error", f"{type(exc).__name__}: {exc}"])
        except (OSError, ValueError):
            pass
        return
    try:
        _send_msg(conn, ["ready", os.getpid()])
    except (OSError, ValueError):
        processor.close()
        return
    block = None
    block_name = ""
    while True:
        try:
            message = _recv_msg(conn)
        except (EOFError, OSError, BrokenPipeError, ValueError):
            break
        kind = message[0]
        if kind == "stop":
            break
        if kind == "amount":
            if len(message) >= 2:
                try:
                    processor.set_amount(float(message[1]))
                except (TypeError, ValueError):
                    pass
            continue
        if kind == "config":
            if len(message) >= 2 and message[1] is not None:
                inference.set_preference(str(message[1]))
            if len(message) >= 3 and message[2] is not None:
                if not segment.set_model(str(message[2])):
                    segment.reset()
            continue
        if kind != "frame" or len(message) < 5:
            continue
        _kind, shm_name, width, height, now = message[:5]
        try:
            width, height, now = int(width), int(height), float(now)
            if width <= 0 or height <= 0:
                raise ValueError("invalid frame dimensions")
            if shm_name != block_name:
                if block is not None:
                    block.close()
                    block = None
                block = _attach_shm(str(shm_name))
                block_name = str(shm_name)
            size = width * height * 4
            with memoryview(block.buf)[:size] as view:
                image = Image.frombytes("RGBA", (width, height), view)
            result = processor.process(image, now)
            if result.size != (width, height):
                result = result.resize((width, height), Image.BILINEAR)
            if result.mode != "RGBA":
                result = result.convert("RGBA")
            with memoryview(block.buf)[:size] as out_view:
                out_view[:] = result.tobytes()
            _send_msg(conn, ["done"])
        except Exception as exc:  # noqa: BLE001 - report, keep serving
            try:
                _send_msg(conn, ["fail", f"{type(exc).__name__}: {exc}"])
            except (OSError, ValueError):
                break
    try:
        processor.close()
    finally:
        if block is not None:
            try:
                block.close()
            except (OSError, BufferError):
                pass


class RemoteProcessor:
    """A processor that runs in a worker process (see module docstring)."""

    FRAME_TIMEOUT_S = 1.5
    RETRY_FIRST_S = 1.0
    RETRY_MAX_S = 10.0

    def __init__(self, spec_name: str, amount: float = 0.5) -> None:
        self.spec_name = spec_name
        self.amount = float(amount)
        self.processor = self
        self.restarts = 0
        self.failures = 0
        self.last_error = ""
        self.ready = False
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._process = None
        self._conn = None
        self._shm: shared_memory.SharedMemory | None = None
        self._shm_size = 0
        self._starting = False
        self._retry_at = 0.0
        self._retry_timer: threading.Timer | None = None
        self._ready_at = 0.0
        self._last_good = None
        self._consecutive_fails = 0
        self._closed = False
        self._shm_finalizer = None
        with _live_lock:
            _live_workers.add(self)
        self._spawn()

    # ------------------------------------------------------------ lifecycle
    def _spawn(self) -> None:
        with self._lock:
            if self._closed or self._starting:
                return
            self._starting = True
        threading.Thread(target=self._start_worker, daemon=True,
                         name="anycam-ai-spawn").start()

    def _start_worker(self) -> None:
        from . import inference, packs, segment

        with self._lock:
            spawn_amount = self.amount
        spawn_device = inference.preference()
        spawn_model = segment.chosen_model()
        custom_root = packs.custom_root()
        packs_root_str = str(custom_root) if custom_root is not None else None

        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_serve,
            args=(child, self.spec_name, spawn_amount, spawn_device, spawn_model, packs_root_str),
            daemon=True, name="anycam-ai-worker")
        try:
            try:
                process.start()
            finally:
                child.close()
            deadline = time.monotonic() + 60.0            # models can take a while to load
            while time.monotonic() < deadline and not self._closed:
                if parent.poll(0.2):
                    message = _recv_msg(parent)
                    if message[0] == "ready":
                        with self._io_lock:
                            with self._lock:
                                if self._closed:
                                    break
                                cur_amount = self.amount
                                cur_device = inference.preference()
                                cur_model = segment.chosen_model()
                                if cur_amount != spawn_amount:
                                    _send_msg(parent, ["amount", cur_amount])
                                if cur_device != spawn_device or cur_model != spawn_model:
                                    _send_msg(parent, ["config", cur_device, cur_model])
                                self._process, self._conn = process, parent
                                self.ready = True
                                self._ready_at = time.monotonic()
                                self._starting = False
                        return
                    with self._lock:
                        self.last_error = str(message[1]) if len(message) > 1 else "worker failed"
                    break
                if not process.is_alive():
                    with self._lock:
                        self.last_error = "the worker exited while starting"
                    break
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
        self._discard(process, parent)
        with self._lock:
            self._starting = False
        self._schedule_retry()

    def _schedule_retry(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.failures += 1
            delay = min(self.RETRY_MAX_S, self.RETRY_FIRST_S * (2 ** min(self.failures - 1, 6)))
            self._retry_at = time.monotonic() + delay
            old_timer = getattr(self, "_retry_timer", None)
            if old_timer is not None:
                old_timer.cancel()
            timer = threading.Timer(delay, self._spawn)
            timer.daemon = True
            self._retry_timer = timer
            timer.start()

    @staticmethod
    def _discard(process, conn) -> None:
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if process is not None and process.is_alive():
                process.kill()
            if process is not None:
                process.join(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass

    def _lose_worker(self, reason: str) -> None:
        with self._lock:
            process, conn = self._process, self._conn
            self._process = self._conn = None
            self.ready = False
            self.last_error = reason
            self.restarts += 1
            self._consecutive_fails = 0
        self._discard(process, conn)
        self._schedule_retry()

    def worker_pid(self) -> int:
        with self._lock:
            process = self._process
        return int(process.pid) if process is not None and process.pid else 0

    # ----------------------------------------------------------- Processor API
    def _acquire_io(self):
        lock = getattr(self, "_io_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    def set_amount(self, amount: float) -> None:
        clamped = max(0.0, min(1.0, float(amount)))
        with self._acquire_io():
            with self._lock:
                self.amount = clamped
                conn = self._conn
            if conn is not None:
                try:
                    _send_msg(conn, ["amount", clamped])
                except (OSError, ValueError):
                    pass

    def set_runtime_settings(self, device: str | None = None, model: str | None = None) -> None:
        with self._acquire_io():
            with self._lock:
                conn = self._conn
            if conn is not None:
                try:
                    _send_msg(conn, ["config", device, model])
                except (OSError, ValueError):
                    pass

    def _held(self, image: Image.Image) -> Image.Image:
        """The last processed frame, so effects never leak the raw picture.

        During startup (~0.2 s) and restart backoff (1-10 s) the unprocessed
        frame used to pass through, flashing face/background effects off
        (audit item 8).  Holding the last good frame keeps the effect on;
        only the very first frames (no good frame yet) pass through.
        """
        if self._last_good is not None and self._last_good.size == image.size:
            return self._last_good.copy()
        return image

    def process(self, image: Image.Image, now: float) -> Image.Image:
        with self._acquire_io():
            with self._lock:
                conn = self._conn
                starting = self._starting
                retry_at = self._retry_at
                closed = self._closed
            if conn is None:
                if not starting and time.monotonic() >= retry_at and not closed:
                    self._spawn()
                return self._held(image)
            width, height = image.size
            size = width * height * 4
            try:
                if self._shm is None or self._shm_size < size:
                    self._release_block()
                    self._shm = shared_memory.SharedMemory(create=True, size=size)
                    self._shm_size = size
                    self._shm_finalizer = weakref.finalize(self, _cleanup_shm, self._shm)
                rgba = image if image.mode == "RGBA" else image.convert("RGBA")
                with memoryview(self._shm.buf)[:size] as in_view:
                    in_view[:] = rgba.tobytes()
                _send_msg(conn, ["frame", self._shm.name, width, height, float(now)])
                if not conn.poll(self.FRAME_TIMEOUT_S):
                    self._lose_worker("the effect worker stopped answering")
                    return self._held(image)
                reply = _recv_msg(conn)
            except (EOFError, OSError, BrokenPipeError, ValueError):
                self._lose_worker("the effect worker exited")
                return self._held(image)
            if reply[0] != "done":
                err = str(reply[1]) if len(reply) > 1 else "frame failed"
                with self._lock:
                    self.last_error = err
                    self._consecutive_fails += 1
                    too_many = self._consecutive_fails >= 5
                if too_many:
                    self._lose_worker(err)
                return self._held(image)
            with self._lock:
                self._consecutive_fails = 0
                if self.failures and time.monotonic() - self._ready_at > 10.0:
                    self.failures = 0                  # it has been healthy: start the backoff over
            try:
                with memoryview(self._shm.buf)[:size] as out_view:
                    result = Image.frombytes("RGBA", (width, height), out_view)
            except (OSError, BufferError, ValueError) as exc:
                with self._lock:
                    self.last_error = f"shared memory read failed: {exc}"
                return self._held(image)
            self._last_good = result
            return result

    def _release_block(self) -> None:
        finalizer = getattr(self, "_shm_finalizer", None)
        if finalizer is not None:
            finalizer.detach()
            self._shm_finalizer = None
        shm = getattr(self, "_shm", None)
        if shm is not None:
            self._shm = None
            self._shm_size = 0
            _cleanup_shm(shm)

    def close(self) -> None:
        with _live_lock:
            _live_workers.discard(self)
        with self._lock:
            self._closed = True
            timer = getattr(self, "_retry_timer", None)
            self._retry_timer = None
        if timer is not None:
            timer.cancel()
        with self._acquire_io():
            with self._lock:
                process, conn = self._process, self._conn
                self._process = self._conn = None
                self.ready = False
            try:
                if conn is not None:
                    _send_msg(conn, ["stop"])
            except (OSError, ValueError):
                pass
            self._discard(process, conn)
            self._release_block()

    def status(self) -> dict:
        with self._lock:
            return {"ready": self.ready, "restarts": self.restarts, "error": self.last_error,
                    "pid": self.worker_pid()}


def _cleanup_shm(shm: shared_memory.SharedMemory) -> None:
    try:
        shm.close()
    except (FileNotFoundError, OSError, BufferError):
        pass
    try:
        shm.unlink()
    except (FileNotFoundError, OSError, BufferError):
        pass


# ---------------------------------------------------- lightweight inference worker
def _serve_inference(conn, device: str = "cpu", model: str = "mediapipe") -> None:  # pragma: no cover - child
    """Child process dedicated solely to downscaled ONNX inference (faces & mattes).

    Sending a 384x216 RGB frame (248 KB) and returning JSON face boxes (~300 B)
    or a 256x144 L8 matte (36 KB) avoids copying 8.3 MB 1080p RGBA frames across
    shared memory 6 times per frame (cutting worker IPC from ~25 ms to ~1-2 ms).
    """
    import numpy as np

    from . import face as face_mod
    from . import inference, segment

    inference.set_preference(device)
    segment.set_model(model)
    try:
        _send_msg(conn, ["ready", os.getpid()])
    except (OSError, ValueError):
        return
    block = None
    block_name = ""
    seg_instance = None
    seg_gen = -1
    while True:
        try:
            message = _recv_msg(conn)
        except (EOFError, OSError, BrokenPipeError, ValueError):
            break
        kind = message[0]
        if kind == "stop":
            break
        if kind == "config":
            if len(message) >= 2 and message[1] is not None:
                inference.set_preference(str(message[1]))
            if len(message) >= 3 and message[2] is not None:
                if not segment.set_model(str(message[2])):
                    segment.reset()
            seg_instance = None
            continue
        if kind == "faces" and len(message) >= 6:
            _k, shm_name, w, h, thresh, top_k = message[:6]
            try:
                w, h = int(w), int(h)
                if shm_name != block_name:
                    if block is not None:
                        block.close()
                    block = _attach_shm(str(shm_name))
                    block_name = str(shm_name)
                size = w * h * 3
                with memoryview(block.buf)[:size] as view:
                    rgb = np.frombuffer(view, dtype=np.uint8).reshape(h, w, 3).copy()
                detector = face_mod.shared_detector()
                if detector is None:
                    _send_msg(conn, ["faces", None])
                else:
                    found = detector.detect(rgb, score_threshold=float(thresh), top_k=int(top_k))
                    payload = [
                        [f.x, f.y, f.w, f.h, f.score,
                         [list(pt) for pt in f.landmarks] if f.landmarks else None]
                        for f in found
                    ]
                    _send_msg(conn, ["faces", payload])
            except Exception as exc:  # noqa: BLE001
                try:
                    _send_msg(conn, ["fail", f"{type(exc).__name__}: {exc}"])
                except (OSError, ValueError):
                    break
        elif kind == "segment" and len(message) >= 4:
            _k, shm_name, w, h = message[:4]
            try:
                w, h = int(w), int(h)
                if shm_name != block_name:
                    if block is not None:
                        block.close()
                    block = _attach_shm(str(shm_name))
                    block_name = str(shm_name)
                size = w * h * 3
                with memoryview(block.buf)[:size] as view:
                    rgb = Image.frombytes("RGB", (w, h), view)
                cur_gen = segment.generation()
                if seg_instance is None or seg_gen != cur_gen:
                    seg_instance = segment.segmenter()
                    seg_gen = cur_gen
                alpha = seg_instance.alpha(rgb)
                if alpha is None:
                    _send_msg(conn, ["matte", 0, 0, False])
                else:
                    mh, mw = alpha.shape[:2]
                    l8 = (np.clip(alpha, 0.0, 1.0) * 255.0).astype(np.uint8)
                    out_bytes = l8.tobytes()
                    with memoryview(block.buf)[:len(out_bytes)] as out_view:
                        out_view[:] = out_bytes
                    _send_msg(conn, ["matte", mw, mh, bool(seg_instance.sharpen)])
            except Exception as exc:  # noqa: BLE001
                try:
                    _send_msg(conn, ["fail", f"{type(exc).__name__}: {exc}"])
                except (OSError, ValueError):
                    break
    if block is not None:
        try:
            block.close()
        except (OSError, BufferError):
            pass


_INFERENCE_WORKER: "InferenceWorker | None" = None
_INFERENCE_WORKER_LOCK = threading.Lock()


def shared_inference_worker() -> "InferenceWorker":
    """Process-wide slim inference worker (1-2 ms downscaled IPC vs 21-27 ms full-frame)."""
    global _INFERENCE_WORKER
    with _INFERENCE_WORKER_LOCK:
        if _INFERENCE_WORKER is None or _INFERENCE_WORKER._closed:
            _INFERENCE_WORKER = InferenceWorker()
        return _INFERENCE_WORKER


def close_shared_inference_worker() -> None:
    global _INFERENCE_WORKER
    with _INFERENCE_WORKER_LOCK:
        worker, _INFERENCE_WORKER = _INFERENCE_WORKER, None
    if worker is not None:
        worker.close()


def slim_isolation_active() -> bool:
    """True when built-in AI inference should route through the slim InferenceWorker."""
    return isolation_enabled() and getattr(_local, "depth", 0) == 0


class InferenceWorker:
    """Out-of-process ONNX inference client for downscaled face & person masks."""

    TIMEOUT_S = 2.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process = None
        self._conn = None
        self._shm: shared_memory.SharedMemory | None = None
        self._shm_size = 0
        self._shm_finalizer = None
        self._closed = False
        self.last_error = ""

    def _ensure_started(self) -> bool:
        from . import inference, segment

        if self._closed:
            return False
        if self._conn is not None and self._process is not None and self._process.is_alive():
            return True
        self._drop()
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        proc = ctx.Process(
            target=_serve_inference,
            args=(child, inference.preference(), segment.chosen_model()),
            daemon=True, name="anycam-infer-worker")
        try:
            try:
                proc.start()
            finally:
                child.close()
            if not parent.poll(30.0):
                self.last_error = "inference worker start timed out"
                RemoteProcessor._discard(proc, parent)
                return False
            msg = _recv_msg(parent)
            if msg[0] != "ready":
                self.last_error = str(msg[1]) if len(msg) > 1 else "inference worker failed"
                RemoteProcessor._discard(proc, parent)
                return False
            self._process, self._conn = proc, parent
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            RemoteProcessor._discard(proc, parent)
            return False

    def _ensure_shm(self, size: int) -> shared_memory.SharedMemory:
        if self._shm is None or self._shm_size < size:
            self._release_shm()
            self._shm = shared_memory.SharedMemory(create=True, size=size)
            self._shm_size = size
            self._shm_finalizer = weakref.finalize(self, _cleanup_shm, self._shm)
        return self._shm

    def _release_shm(self) -> None:
        fin = self._shm_finalizer
        if fin is not None:
            fin.detach()
            self._shm_finalizer = None
        if self._shm is not None:
            shm, self._shm, self._shm_size = self._shm, None, 0
            _cleanup_shm(shm)

    def _drop(self) -> None:
        proc, conn = self._process, self._conn
        self._process = self._conn = None
        RemoteProcessor._discard(proc, conn)

    def detect_faces(self, rgb_image: Image.Image, score_threshold: float = 0.6,
                     top_k: int = 4):
        from .face import Face

        with self._lock:
            if not self._ensure_started():
                return None
            rgb = rgb_image if rgb_image.mode == "RGB" else rgb_image.convert("RGB")
            w, h = rgb.size
            raw = rgb.tobytes()
            shm = self._ensure_shm(max(len(raw), 640 * 360 * 3))
            try:
                with memoryview(shm.buf)[:len(raw)] as view:
                    view[:] = raw
                _send_msg(self._conn, ["faces", shm.name, w, h, float(score_threshold), int(top_k)])
                if not self._conn.poll(self.TIMEOUT_S):
                    self.last_error = "inference worker timed out"
                    self._drop()
                    return None
                reply = _recv_msg(self._conn)
            except (EOFError, OSError, BrokenPipeError, ValueError) as exc:
                self.last_error = str(exc)
                self._drop()
                return None
            if reply[0] != "faces":
                self.last_error = str(reply[1]) if len(reply) > 1 else "face inference failed"
                return None
            if reply[1] is None:
                return None
            return [
                Face(x=float(f[0]), y=float(f[1]), w=float(f[2]), h=float(f[3]),
                     score=float(f[4]),
                     landmarks=tuple((float(p[0]), float(p[1])) for p in f[5]) if f[5] else None)
                for f in reply[1]
            ]

    def segment_person(self, rgb_image: Image.Image):
        """Return ``(alpha_float32, sharpen)`` or ``(None, True)`` on failure."""
        import numpy as np

        with self._lock:
            if not self._ensure_started():
                return None, True
            rgb = rgb_image if rgb_image.mode == "RGB" else rgb_image.convert("RGB")
            w, h = rgb.size
            raw = rgb.tobytes()
            shm = self._ensure_shm(max(len(raw), 640 * 360 * 3))
            try:
                with memoryview(shm.buf)[:len(raw)] as view:
                    view[:] = raw
                _send_msg(self._conn, ["segment", shm.name, w, h])
                if not self._conn.poll(self.TIMEOUT_S):
                    self.last_error = "inference worker timed out"
                    self._drop()
                    return None, True
                reply = _recv_msg(self._conn)
            except (EOFError, OSError, BrokenPipeError, ValueError) as exc:
                self.last_error = str(exc)
                self._drop()
                return None, True
            if reply[0] != "matte" or int(reply[1]) <= 0 or int(reply[2]) <= 0:
                return None, True
            mw, mh, sharpen = int(reply[1]), int(reply[2]), bool(reply[3])
            with memoryview(shm.buf)[:mw * mh] as out_view:
                alpha = np.frombuffer(out_view, dtype=np.uint8).reshape(mh, mw).astype(np.float32) / 255.0
            return alpha, sharpen

    def set_runtime_settings(self, device: str | None = None, model: str | None = None) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    _send_msg(self._conn, ["config", device, model])
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._conn is not None:
                try:
                    _send_msg(self._conn, ["stop"])
                except (OSError, ValueError):
                    pass
            self._drop()
            self._release_shm()
