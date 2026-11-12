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

import multiprocessing
import os
import threading
import time
from multiprocessing import shared_memory

from PIL import Image

# Processors that load models or third-party code run isolated; the cheap ones
# (touch-ups, zoom) stay in-process where a copy of every frame would cost more
# than the work.
ISOLATED_KINDS = frozenset({"background", "spotlight", "sticker", "mask", "privacy",
                            "touchup", "autoframe", "pack", "selftest"})

_enabled = False


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


def wants_isolation(spec_name: str) -> bool:
    kind = spec_name.partition(":")[0]
    if kind == "pack":
        return True              # third-party code never runs in the main process
    return isolation_enabled() and kind in ISOLATED_KINDS


# ------------------------------------------------------------------ the worker
def _serve(conn, spec_name: str, amount: float) -> None:  # pragma: no cover - child
    """Child process: build the processor, then answer frame requests."""
    from . import processors

    try:
        processor = processors.make_processor(spec_name, amount)
    except Exception as exc:  # noqa: BLE001
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
        return
    conn.send(("ready", os.getpid()))
    block = None
    block_name = ""
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            break
        kind = message[0]
        if kind == "stop":
            break
        if kind == "amount":
            processor.set_amount(message[1])
            continue
        if kind != "frame":
            continue
        _kind, shm_name, width, height, now = message
        try:
            if shm_name != block_name:
                if block is not None:
                    block.close()
                block = shared_memory.SharedMemory(name=shm_name)
                block_name = shm_name
            size = width * height * 4
            image = Image.frombuffer("RGBA", (width, height), bytes(block.buf[:size]), "raw",
                                     "RGBA", 0, 1)
            result = processor.process(image, now)
            if result.size != (width, height):
                result = result.resize((width, height), Image.BILINEAR)
            block.buf[:size] = result.convert("RGBA").tobytes()
            conn.send(("done",))
        except Exception as exc:  # noqa: BLE001 - report, keep serving
            conn.send(("fail", f"{type(exc).__name__}: {exc}"))
    try:
        processor.close()
    finally:
        if block is not None:
            block.close()


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
        self._process = None
        self._conn = None
        self._shm: shared_memory.SharedMemory | None = None
        self._shm_size = 0
        self._starting = False
        self._retry_at = 0.0
        self._ready_at = 0.0
        self._closed = False
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
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_serve, args=(child, self.spec_name, self.amount),
                                  daemon=True, name="anycam-ai-worker")
        try:
            process.start()
            child.close()
            deadline = time.monotonic() + 60.0            # models can take a while to load
            while time.monotonic() < deadline and not self._closed:
                if parent.poll(0.2):
                    message = parent.recv()
                    if message[0] == "ready":
                        with self._lock:
                            if self._closed:
                                break
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
        self.failures += 1
        delay = min(self.RETRY_MAX_S, self.RETRY_FIRST_S * (2 ** min(self.failures - 1, 6)))
        self._retry_at = time.monotonic() + delay
        if not self._closed:
            timer = threading.Timer(delay, self._spawn)
            timer.daemon = True
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
        process = self._process
        return int(process.pid) if process is not None and process.pid else 0

    # ----------------------------------------------------------- Processor API
    def set_amount(self, amount: float) -> None:
        self.amount = max(0.0, min(1.0, float(amount)))
        with self._lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.send(("amount", self.amount))
            except (OSError, ValueError):
                pass

    def process(self, image: Image.Image, now: float) -> Image.Image:
        with self._lock:
            conn = self._conn
        if conn is None:
            if not self._starting and time.monotonic() >= self._retry_at and not self._closed:
                self._spawn()
            return image                       # not up (yet): the picture goes through
        width, height = image.size
        size = width * height * 4
        try:
            if self._shm is None or self._shm_size < size:
                self._release_block()
                self._shm = shared_memory.SharedMemory(create=True, size=size)
                self._shm_size = size
            self._shm.buf[:size] = image.convert("RGBA").tobytes()
            conn.send(("frame", self._shm.name, width, height, now))
            if not conn.poll(self.FRAME_TIMEOUT_S):
                self._lose_worker("the effect worker stopped answering")
                return image
            reply = conn.recv()
        except (EOFError, OSError, BrokenPipeError):
            self._lose_worker("the effect worker exited")
            return image
        if reply[0] != "done":
            self.last_error = str(reply[1]) if len(reply) > 1 else "frame failed"
            return image
        if self.failures and time.monotonic() - self._ready_at > 10.0:
            self.failures = 0                  # it has been healthy: start the backoff over
        return Image.frombytes("RGBA", (width, height), bytes(self._shm.buf[:size]))

    def _release_block(self) -> None:
        if self._shm is not None:
            try:
                self._shm.close()
                self._shm.unlink()
            except (FileNotFoundError, OSError):
                pass
            self._shm = None
            self._shm_size = 0

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process, conn = self._process, self._conn
            self._process = self._conn = None
            self.ready = False
        try:
            if conn is not None:
                conn.send(("stop",))
        except (OSError, ValueError):
            pass
        self._discard(process, conn)
        self._release_block()

    def status(self) -> dict:
        return {"ready": self.ready, "restarts": self.restarts, "error": self.last_error,
                "pid": self.worker_pid()}
