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
                segment.set_model(str(message[2]))
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
        self._closed = False
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
                    self.last_error = str(message[1]) if len(message) > 1 else "worker failed"
                    break
                if not process.is_alive():
                    self.last_error = "the worker exited while starting"
                    break
        except Exception as exc:  # noqa: BLE001
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
        self._discard(process, conn)
        self.restarts += 1
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
                self.last_error = str(reply[1]) if len(reply) > 1 else "frame failed"
                return self._held(image)
            if self.failures and time.monotonic() - self._ready_at > 10.0:
                self.failures = 0                  # it has been healthy: start the backoff over
            try:
                with memoryview(self._shm.buf)[:size] as out_view:
                    result = Image.frombytes("RGBA", (width, height), out_view)
            except (OSError, BufferError, ValueError):
                return self._held(image)
            self._last_good = result
            return result

    def _release_block(self) -> None:
        shm = getattr(self, "_shm", None)
        if shm is not None:
            self._shm = None
            self._shm_size = 0
            try:
                shm.close()
            except (FileNotFoundError, OSError, BufferError):
                pass
            try:
                shm.unlink()
            except (FileNotFoundError, OSError, BufferError):
                pass

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
        return {"ready": self.ready, "restarts": self.restarts, "error": self.last_error,
                "pid": self.worker_pid()}
