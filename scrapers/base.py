from abc import ABC, abstractmethod

from models import Listing


class Scraper(ABC):
    """Interface every site adapter implements. A scraper failing (network
    error, changed HTML, etc.) should raise - run.py catches per-source so
    one broken adapter doesn't block the others."""

    @abstractmethod
    def fetch(
        self, city_key: str, source_cfg: dict, known_amenities: dict[str, list[str]] | None = None
    ) -> list[Listing]:
        # known_amenities maps listing id -> amenities already recorded for
        # it, letting a scraper skip expensive per-listing work (e.g. a
        # detail-page visit) for listings it's already seen.
        ...
