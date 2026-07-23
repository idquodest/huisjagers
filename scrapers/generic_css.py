import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from models import Listing
from text_utils import find_keyword_matches
from .base import Scraper

_NUMBER_RE = re.compile(r"[\d,.]+")


def _select_one_opt(card, selector: str | None):
    return card.select_one(selector) if selector else None


def _add_amenities(amenities: list[str], new_items: list[str]) -> None:
    # Case-insensitive dedup: a site's own amenities selector might already
    # have "Furnished" and a keyword match for "furnished" shouldn't add a
    # second, differently-cased copy of the same thing.
    existing_lower = {a.lower() for a in amenities}
    for item in new_items:
        if item.lower() not in existing_lower:
            amenities.append(item)
            existing_lower.add(item.lower())


def _parse_number(text: str | None, decimal_comma: bool = False) -> float | None:
    if not text:
        return None
    if decimal_comma:
        # European format: "." is a thousands separator, "," is the decimal
        # point (or a bare ",-" meaning "no cents") - the opposite of the
        # US-style parsing below.
        cleaned = text.replace(".", "").replace(",", ".")
    else:
        cleaned = text.replace(",", "")
    match = _NUMBER_RE.search(cleaned)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


class GenericCssScraper(Scraper):
    """Configurable scraper for simple (non-JS-heavy) sites: property
    management pages, small listing boards, etc. Selectors are defined per
    source in config.yaml. Not suited to JS-rendered aggregators like Zillow
    (those need a headless-browser scraper - a future addition)."""

    def fetch(
        self, city_key: str, source_cfg: dict, known_amenities: dict[str, list[str]] | None = None
    ) -> list[Listing]:
        url = source_cfg["url"]
        selectors = source_cfg["selectors"]
        source_name = source_cfg["name"]
        # Safety net / primary filter: some sites (e.g. ikwilhuren.nu) list
        # every city nationwide on one page with no server-side city filter.
        city_filter = {c.lower() for c in source_cfg.get("city_filter", [])}
        # Sites with real page-N URLs can be walked page by page - e.g.
        # ikwilhuren.nu: "{url}?page={page}".
        page_url_pattern = source_cfg.get("page_url_pattern")
        max_pages = source_cfg.get("max_pages", 1)
        decimal_comma = source_cfg.get("decimal_comma", False)

        listings = []
        for page_num in range(1, max_pages + 1):
            page_url = url if page_num == 1 else page_url_pattern.format(url=url, page=page_num)
            resp = requests.get(
                page_url,
                headers={"User-Agent": "Mozilla/5.0 (apartment-finder personal use)"},
                timeout=15,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            cards = soup.select(selectors["listing"])
            if not cards:
                break

            listings.extend(
                self._parse_cards(cards, selectors, source_name, city_key, url, city_filter, decimal_comma)
            )

            if not page_url_pattern:
                break

        return listings

    def _parse_cards(self, cards, selectors, source_name, city_key, url, city_filter, decimal_comma):
        listings = []
        for card in cards:
            if city_filter:
                city_el = _select_one_opt(card, selectors.get("city"))
                card_city = city_el.get_text(strip=True).lower() if city_el else ""
                if not any(target in card_city for target in city_filter):
                    continue

            address_el = _select_one_opt(card, selectors.get("address"))
            price_el = _select_one_opt(card, selectors.get("price"))
            beds_el = _select_one_opt(card, selectors.get("beds"))
            baths_el = _select_one_opt(card, selectors.get("baths"))
            sqft_el = _select_one_opt(card, selectors.get("sqft"))
            link_el = _select_one_opt(card, selectors.get("link"))

            amenity_els = card.select(selectors.get("amenities", "")) if selectors.get("amenities") else []
            amenities = [el.get_text(strip=True) for el in amenity_els]

            # Full card text, original case - stored on the listing so
            # per-user keyword filters (matcher.py) can search more than
            # just the address. card_text is the lowercased version used
            # for the scrape-time checks below.
            full_text = card.get_text(" ", strip=True)
            card_text = full_text.lower()

            exclude_keywords = selectors.get("exclude_keywords", [])
            if any(kw.lower() in card_text for kw in exclude_keywords):
                continue

            pet_keywords = selectors.get("pet_friendly_keywords", [])
            pet_friendly = any(kw.lower() in card_text for kw in pet_keywords)

            amenity_keywords = selectors.get("amenity_keywords", [])
            _add_amenities(amenities, find_keyword_matches(card_text, amenity_keywords))

            href = link_el.get("href") if link_el else None
            listing_url = urljoin(url, href) if href else url

            listings.append(
                Listing(
                    source=source_name,
                    city_key=city_key,
                    address=address_el.get_text(strip=True) if address_el else "Unknown address",
                    price=_parse_number(price_el.get_text() if price_el else None, decimal_comma),
                    beds=_parse_number(beds_el.get_text() if beds_el else None, decimal_comma),
                    baths=_parse_number(baths_el.get_text() if baths_el else None, decimal_comma),
                    sqft=_parse_number(sqft_el.get_text() if sqft_el else None, decimal_comma),
                    pet_friendly=pet_friendly,
                    amenities=amenities,
                    url=listing_url,
                    description=full_text,
                )
            )
        return listings
