"""The parent stops the inference subprocess by putting SHUTDOWN on a multiprocessing queue, which
pickles every item. A bare object() arrived as a copy, so `item is SHUTDOWN` failed and every stop
logged "unexpected queue item: <class 'object'>" as an unhandled error (staging, 2026-11-23)."""

from __future__ import annotations

import multiprocessing as mp


def test_shutdown_is_still_shutdown_after_a_multiprocessing_queue():
    from services.worker.app import inference_proc

    q = mp.get_context("spawn").Queue()
    try:
        q.put(inference_proc.SHUTDOWN)
        assert q.get(timeout=10) is inference_proc.SHUTDOWN
    finally:
        q.close()
        q.join_thread()
