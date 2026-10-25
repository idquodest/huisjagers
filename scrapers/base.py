import time
from abc import ABC, abstractmethod

from models import Listing

# Pause before every page load after a source's first one (next list page,
# a listing's own detail page), so no site ever gets back-to-back requests.
# run.py sets this from config.yaml's polling.delay_between_pages_seconds.
PAGE_DELAY_SECONDS = 5.0


def pause_between_pages() -> None:
    if PAGE_DELAY_SECONDS:
        time.sleep(PAGE_DELAY_SECONDS)


class Scraper(ABC):
    """Interface every site adapter implements. A scraper failing (network
    error, changed HTML, etc.) should raise - run.py catches per-source so
    one broken adapter doesn't block the others."""

    @abstractmethod
    def fetch(
        self, city_key: str, source_cfg: dict, known_listings: dict[str, dict] | None = None
    ) -> list[Listing]:
        # known_listings maps listing id -> {"amenities": [...], "description": ...}
        # already recorded for it, letting a scraper skip expensive
        # per-listing work (e.g. a detail-page visit) for listings it's
        # already seen.
        ...
