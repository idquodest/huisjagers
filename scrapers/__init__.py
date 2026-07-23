from .mock import MockScraper
from .generic_css import GenericCssScraper
from .playwright_site import PlaywrightSiteScraper

# Registry mapping config `type:` values to scraper classes.
SCRAPER_TYPES = {
    "mock": MockScraper,
    "generic_css": GenericCssScraper,
    "playwright_site": PlaywrightSiteScraper,
}


def get_scraper(source_type: str):
    try:
        return SCRAPER_TYPES[source_type]()
    except KeyError:
        raise ValueError(
            f"Unknown scraper type '{source_type}'. Known types: {list(SCRAPER_TYPES)}"
        )
