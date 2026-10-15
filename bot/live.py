"""Live-mode helper for Source A: poll for new messages after catching up."""

from .utils import StopRequested

LIVE_POLL_INTERVAL = 30  # seconds between polls once caught up


async def wait_for_new(get_latest, last_seen, sleep, stop_event):
    """Poll get_latest() every LIVE_POLL_INTERVAL until a newer id appears.

    Returns the new latest id. Raises StopRequested when stop_event fires.
    """
    while True:
        try:
            await sleep(LIVE_POLL_INTERVAL)
        except StopRequested:
            raise
        latest = await get_latest()
        if latest and latest > last_seen:
            return latest
        if stop_event.is_set():
            raise StopRequested()
