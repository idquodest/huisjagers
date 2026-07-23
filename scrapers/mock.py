from models import Listing
from .base import Scraper

# Fixed sample data so repeated runs behave like a real site: the same
# listings come back each poll, and only genuinely new ids trigger alerts.
# Swap this scraper out for a real one once you've picked actual sites.
_SAMPLE_LISTINGS = [
    dict(address="123 Maple St, Unit 2", price=1450, beds=1, baths=1,
         sqft=650, pet_friendly=True, amenities=["parking"]),
    dict(address="456 Oak Ave, Unit 5B", price=1950, beds=2, baths=2,
         sqft=900, pet_friendly=False, amenities=["in_unit_laundry", "parking"]),
    dict(address="789 Pine Rd, Unit 1", price=2600, beds=2, baths=1,
         sqft=800, pet_friendly=True, amenities=[]),  # over budget - should be filtered out
    dict(address="321 Birch Ln, Studio", price=1100, beds=0, baths=1,
         sqft=450, pet_friendly=True, amenities=["parking"]),  # under price_min - filtered out
]


class MockScraper(Scraper):
    def fetch(
        self, city_key: str, source_cfg: dict, known_amenities: dict[str, list[str]] | None = None
    ) -> list[Listing]:
        source_name = source_cfg["name"]
        listings = []
        for i, data in enumerate(_SAMPLE_LISTINGS):
            listings.append(
                Listing(
                    source=source_name,
                    city_key=city_key,
                    address=data["address"],
                    price=data["price"],
                    beds=data["beds"],
                    baths=data["baths"],
                    sqft=data["sqft"],
                    pet_friendly=data["pet_friendly"],
                    amenities=data["amenities"],
                    url=f"https://example.com/mock-listing/{city_key}/{i}",
                )
            )
        return listings
