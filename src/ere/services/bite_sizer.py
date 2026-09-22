"""Adaptive bite size: how many waiting requests the ERE takes next (DEC-11)."""

from ere.services.resolver_config import BatchSettings

SMOOTHING = 0.3
MIN_BITE = 1


class BiteSizer:
    """Keeps a moving average of mentions processed per second and sizes the next bite to the target duration."""

    def __init__(self, settings: BatchSettings):
        self._settings = settings
        self._rate: float | None = None

    def limit(self) -> int:
        """Requests the next bite may take; the first bite uses the configured maximum."""
        if self._rate is None:
            return self._settings.max_mentions
        wanted = round(self._rate * self._settings.target_seconds)
        return max(MIN_BITE, min(wanted, self._settings.max_mentions))

    def observe(self, mentions: int, seconds: float) -> None:
        """Record how long a bite took; ignores bites that took no measurable time."""
        if seconds <= 0:
            return
        rate = mentions / seconds
        self._rate = (
            rate
            if self._rate is None
            else SMOOTHING * rate + (1 - SMOOTHING) * self._rate
        )
