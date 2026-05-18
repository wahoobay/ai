"""Shared-memory frame pool — replaces the multiprocessing.Queue pickle
hop between the FrameTap thread (parent process) and the inference
subprocess.

Why this exists
---------------
Original design pickled a numpy frame_bgr (1280x720x3 = 2.7 MB at 720p,
6.2 MB at 1080p) into a multiprocessing.Queue. The parent paid ~10 ms
serialising, the subprocess paid ~10 ms deserialising — roughly 30%
of the per-frame budget at 15 fps. Headline symptom: inference
throughput capped at ~13 fps on a camera that was producing 15.

This module hands the frame across via shared memory instead. Parent
copies the raw BGR bytes into a pre-allocated /dev/shm slot, drops a
small (~80-byte) FrameMeta tuple into a queue, and the subprocess
reconstructs a numpy view directly over the shared bytes — zero copy
on the receive side.

Pool semantics
--------------
N pre-allocated slots, each sized for max_h × max_w × 3. Two queues:

  empty_q  — indices of free slots (initial: every slot)
  filled_q — (slot_idx, meta) pairs ready for inference

Parent:  empty_q.get → memcpy frame → filled_q.put
Child:   filled_q.get → numpy view → process → empty_q.put

Drop-old (latest frame wins under backpressure) is handled by the
parent: if empty_q is empty, pop the oldest entry off filled_q and
reuse its slot. That keeps the queue from accumulating stale frames
when inference falls behind the camera.

3 slots fit the typical case: one being processed, one waiting in
filled_q, one being written. More slots just adds end-to-end latency
without extra throughput.
"""
from __future__ import annotations

from dataclasses import dataclass
from multiprocessing.shared_memory import SharedMemory

import numpy as np


# Sized for 1080p RGB so a future flip back to 1920x1080 doesn't need
# a re-alloc. 1920 * 1080 * 3 = 6,220,800 bytes per slot. At 3 slots,
# total /dev/shm usage is ~18 MB per worker — trivial.
SHM_MAX_H = 1080
SHM_MAX_W = 1920
SHM_MAX_C = 3
SHM_SLOT_BYTES = SHM_MAX_H * SHM_MAX_W * SHM_MAX_C
SHM_NUM_SLOTS = 3


@dataclass
class FrameShmDescriptor:
    """Picklable handle the parent passes to the inference subprocess so
    it can attach to the same shared-memory segments. Contains only the
    OS-level shm names + sizing — no numpy / SharedMemory objects, which
    don't survive the spawn-context pickle reliably."""
    names: list[str]
    slot_bytes: int


class FrameShmPool:
    """Parent-side pool. Owns the SharedMemory segments — creates them on
    init, unlinks them on close. The subprocess gets a `FrameShmDescriptor`
    and reattaches via `FrameShmReader`."""

    def __init__(
        self,
        num_slots: int = SHM_NUM_SLOTS,
        slot_bytes: int = SHM_SLOT_BYTES,
    ) -> None:
        self.slot_bytes = slot_bytes
        self.shms: list[SharedMemory] = [
            SharedMemory(create=True, size=slot_bytes) for _ in range(num_slots)
        ]
        # Cache numpy views per slot so write_into doesn't re-construct one
        # every frame. Views are zero-copy; mutating them mutates the shm.
        self._views = [
            np.ndarray((slot_bytes,), dtype=np.uint8, buffer=shm.buf)
            for shm in self.shms
        ]

    @property
    def num_slots(self) -> int:
        return len(self.shms)

    def descriptor(self) -> FrameShmDescriptor:
        return FrameShmDescriptor(
            names=[shm.name for shm in self.shms],
            slot_bytes=self.slot_bytes,
        )

    def write_into(self, slot_idx: int, frame: np.ndarray) -> tuple[int, int, int]:
        """Copy a frame's bytes into the given slot. Returns the frame's
        (H, W, C). Raises ValueError if the frame is larger than slot_bytes."""
        if not frame.flags["C_CONTIGUOUS"]:
            # cv2 frames are normally C-contiguous; this is just a guard.
            frame = np.ascontiguousarray(frame)
        h, w, c = frame.shape
        nbytes = h * w * c
        if nbytes > self.slot_bytes:
            raise ValueError(
                f"frame {h}x{w}x{c}={nbytes}B exceeds slot capacity {self.slot_bytes}B"
            )
        # memcpy directly into the shm-backed buffer. Slicing keeps the view
        # over shm (no realloc); flatten() copies frame into it.
        self._views[slot_idx][:nbytes] = frame.reshape(-1)
        return h, w, c

    def close(self) -> None:
        for shm in self.shms:
            try:
                shm.close()
            except Exception:
                pass
            try:
                shm.unlink()
            except Exception:
                pass
        self.shms = []
        self._views = []


class FrameShmReader:
    """Subprocess-side. Attaches to the shared-memory segments created by
    the parent (by name) and hands out zero-copy numpy views into them.

    DO NOT call .unlink() on the subprocess side — the parent owns slot
    lifecycle. close() releases the local mapping only."""

    def __init__(self, descriptor: FrameShmDescriptor) -> None:
        self.slot_bytes = descriptor.slot_bytes
        self.shms: list[SharedMemory] = [
            SharedMemory(name=name) for name in descriptor.names
        ]

    def view(self, slot_idx: int, shape: tuple[int, int, int]) -> np.ndarray:
        """Return a numpy view of the slot at the given shape (H, W, C).
        No copy — the returned array is backed by /dev/shm. Caller must
        not mutate it after handing back the slot."""
        h, w, c = shape
        nbytes = h * w * c
        if nbytes > self.slot_bytes:
            raise ValueError(
                f"requested shape {shape}={nbytes}B exceeds slot capacity {self.slot_bytes}B"
            )
        # ndarray over the SharedMemory buffer; .buf is a memoryview so
        # numpy wraps without copying.
        return np.ndarray((h, w, c), dtype=np.uint8, buffer=self.shms[slot_idx].buf)

    def close(self) -> None:
        for shm in self.shms:
            try:
                shm.close()
            except Exception:
                pass
        self.shms = []
