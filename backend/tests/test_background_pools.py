"""run_in_background/run_in_background_slow must be genuinely separate pools -
found live: reel generation (minutes, via generate_reel) and every other
backgrounded action (email/RSS fetch, drafts, sends) shared one 4-worker
pool, so a few concurrent reels could starve everything else, including
email processing - which is exactly what "stuck at processing emails" on
the board turned out to be."""

import threading
import time

from backend.app.worker import background


def test_pools_are_separate_executors():
    assert background._executor is not background._slow_executor


def test_slow_task_does_not_block_fast_pool():
    slow_started = threading.Event()
    release_slow = threading.Event()

    def slow_work():
        slow_started.set()
        release_slow.wait(timeout=5)

    fast_done = threading.Event()

    def fast_work():
        fast_done.set()

    # Saturate the slow pool (2 workers) with long-running tasks.
    for _ in range(2):
        background.run_in_background_slow(slow_work)
    assert slow_started.wait(timeout=2)

    # A fast task on the OTHER pool must still complete promptly, even
    # while the slow pool is fully occupied.
    background.run_in_background(fast_work)
    assert fast_done.wait(timeout=2)

    release_slow.set()


def test_exception_in_background_task_is_swallowed_not_raised():
    def boom():
        raise ValueError("expected in this test")

    # Must not raise here - run_in_background is fire-and-forget.
    background.run_in_background(boom)
    background.run_in_background_slow(boom)
    time.sleep(0.1)
