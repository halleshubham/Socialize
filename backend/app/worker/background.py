"""Fire-and-forget execution for work too slow to run inside an HTTP
request - specifically Reel Editor's Veo calls, which can take up to ~6
minutes per scene (Google's own documented worst case). A full job queue
(Celery+Redis) is the more conventional path once this app needs retries/
distributed workers; a plain thread pool is enough at personal-tool, single-
process scale and avoids adding a new required service for one slow agent.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bg-task")


def run_in_background(fn, *args, **kwargs) -> None:
    def _wrapped():
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("Background task %s failed", getattr(fn, "__name__", fn))

    _executor.submit(_wrapped)
