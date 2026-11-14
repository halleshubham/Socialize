"""Fire-and-forget execution for work too slow to run inside an HTTP
request. Two separate pools, not one: Reel Editor's Veo calls can take up
to ~6 minutes per scene (Google's own documented worst case), and started
out as the only thing here - but every other backgrounded action in
routes_board.py (email/RSS fetch, drafts, poster/carousel generation,
WhatsApp/Postiz sends, auto-schedule) has since been routed through the
same run_in_background, and ended up sharing its pool. With only 4
workers, a handful of concurrent reel generations (observed live: emails
sat "processing" on the board for many minutes with no code-level error,
root-caused to this) can starve every one of those fast, seconds-scale
tasks for as long as the slow ones hold every thread. run_in_background_slow
gives reel generation its own small pool instead, so it can never block
anything else. A full job queue (Celery+Redis) is the more conventional
path once this app needs retries/distributed workers; two plain thread
pools are enough at personal-tool, single-process scale.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bg-task")
_slow_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bg-task-slow")


def _submit(executor: ThreadPoolExecutor, fn, args, kwargs) -> None:
    def _wrapped():
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("Background task %s failed", getattr(fn, "__name__", fn))

    executor.submit(_wrapped)


def run_in_background(fn, *args, **kwargs) -> None:
    _submit(_executor, fn, args, kwargs)


def run_in_background_slow(fn, *args, **kwargs) -> None:
    """For reel generation specifically (generate_reel's Veo calls) - see
    module docstring. Never use this for anything that isn't genuinely
    minutes-long, or it reintroduces the same starvation on this smaller
    pool."""
    _submit(_slow_executor, fn, args, kwargs)
