from dataclasses import dataclass, field
import hashlib


@dataclass
class Listing:
    source: str
    city_key: str
    address: str
    price: float | None
    beds: float | None
    baths: float | None
    url: str
    sqft: float | None = None
    pet_friendly: bool = False
    amenities: list[str] = field(default_factory=list)
    posted_date: str | None = None
    description: str | None = None

    @property
    def id(self) -> str:
        # Stable id per (source, url) so re-scraping the same listing
        # updates it instead of creating a duplicate row.
        raw = f"{self.source}:{self.url}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
