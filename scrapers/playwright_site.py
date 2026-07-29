import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from models import Listing
from text_utils import find_keyword_matches
from .base import Scraper

_NUMBER_RE = re.compile(r"[\d,.]+")

# Telltale strings from Cloudflare's JS-challenge interstitial ("Just a
# moment...") - if a detail-page fetch lands on this instead of the real
# listing, its own text must never be mistaken for listing content.
_CLOUDFLARE_CHALLENGE_MARKERS = (
    "just a moment",
    "performing security verification",
    "enable javascript and cookies to continue",
    "ray id:",
)


def _looks_like_cloudflare_challenge(page_text: str) -> bool:
    lowered = page_text.lower()
    return any(marker in lowered for marker in _CLOUDFLARE_CHALLENGE_MARKERS)


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
        # point - the opposite of generic_css's US-style parsing.
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


class PlaywrightSiteScraper(Scraper):
    """For JS-rendered sites (Nuxt/React/Vue) where a plain HTTP GET returns
    an empty shell. Renders the page in headless Chromium first, then parses
    the resulting HTML the same way generic_css does. Slower and heavier
    than generic_css - only use it when a source's listings genuinely don't
    appear in the raw HTML."""

    def fetch(
        self, city_key: str, source_cfg: dict, known_amenities: dict[str, list[str]] | None = None
    ) -> list[Listing]:
        known_amenities = known_amenities or {}
        url = source_cfg["url"]
        selectors = source_cfg["selectors"]
        source_name = source_cfg["name"]
        decimal_comma = source_cfg.get("decimal_comma", False)
        wait_selector = source_cfg.get("wait_selector") or selectors["listing"]
        # Fields only present on each listing's own page, not the summary
        # card - e.g. rebogroep only shows parking facilities there. Each
        # entry costs one extra page load per listing.
        detail_amenities = source_cfg.get("detail_amenities", [])
        amenity_keywords = selectors.get("amenity_keywords", [])
        # The listing's own written blurb, e.g. Pararius-platform sites'
        # ".listing-detail-description__content" - only exists on the detail
        # page, never the summary card. When present, this replaces the
        # card's raw text as the listing's description (see _fetch_detail_data).
        description_selector = selectors.get("description")
        # Safety net: even with a UI filter applied (below), keep dropping
        # any card that isn't actually this city.
        city_filter = {c.lower() for c in source_cfg.get("city_filter", [])}
        # Click-based filters to apply before scraping - e.g. rebogroep has
        # a "Plaats" (location) dropdown with per-city counts that isn't
        # reflected in the URL, so it has to be driven via the page instead
        # of just constructing a query string.
        ui_filters = source_cfg.get("ui_filters", [])
        # Cookiebot's cookie-consent overlay intercepts clicks on everything
        # behind it until dismissed - just remove it outright.
        cookie_dialog_selector = source_cfg.get("cookie_dialog_selector", "#CybotCookiebotDialog")
        next_page_selector = source_cfg.get("next_page_selector", ".pagination .next")
        # Sites with real page-N URLs (e.g. pararius: /apartments/utrecht/page-2)
        # can be paged by direct navigation instead of clicking a "next"
        # control - simpler and more reliable when a site supports it.
        page_url_pattern = source_cfg.get("page_url_pattern")
        max_pages = source_cfg.get("max_pages", 20)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (apartment-finder personal use)"
                )
                page.goto(url, wait_until="load", timeout=45000)
                page.wait_for_selector(wait_selector, timeout=15000)
                page.evaluate(
                    "(sel) => { const el = document.querySelector(sel); if (el) el.remove(); }",
                    cookie_dialog_selector,
                )

                for f in ui_filters:
                    self._apply_ui_filter(page, f["trigger_text"], f["option_text"], wait_selector)

                listings = []
                if page_url_pattern:
                    for page_num in range(1, max_pages + 1):
                        if page_num > 1:
                            page.goto(page_url_pattern.format(url=url, page=page_num), wait_until="load", timeout=45000)
                            try:
                                page.wait_for_selector(wait_selector, timeout=10000)
                            except PlaywrightTimeoutError:
                                break
                        listings.extend(
                            self._parse_cards(
                                page.content(), selectors, source_name, city_key, url, decimal_comma, city_filter
                            )
                        )
                else:
                    for _ in range(max_pages):
                        listings.extend(
                            self._parse_cards(
                                page.content(), selectors, source_name, city_key, url, decimal_comma, city_filter
                            )
                        )
                        if not self._go_to_next_page(page, next_page_selector, wait_selector):
                            break

                if detail_amenities or amenity_keywords or description_selector:
                    for listing in listings:
                        # Already-seen listings keep whatever detail-page
                        # amenities were found the first time, instead of
                        # paying for another page load - their amenities
                        # (e.g. parking) don't change run to run.
                        if listing.id in known_amenities:
                            _add_amenities(listing.amenities, known_amenities[listing.id])
                        else:
                            found_amenities, detail_text = self._fetch_detail_data(
                                page, listing.url, detail_amenities, amenity_keywords, description_selector
                            )
                            _add_amenities(listing.amenities, found_amenities)
                            if description_selector:
                                # A targeted selector gives the listing's own
                                # written blurb - use it on its own rather
                                # than tacking it onto the noisy card-text
                                # summary (address/price/etc. already live in
                                # their own columns).
                                if detail_text:
                                    listing.description = detail_text
                            else:
                                # No dedicated blurb selector for this source
                                # - fold the whole detail page's text in as
                                # before, e.g. rebogroep's parking info only
                                # exists there, and a keyword filter for
                                # "parking" needs it findable in description.
                                listing.description = f"{listing.description or ''} {detail_text}".strip()
            finally:
                browser.close()

        return listings

    def _apply_ui_filter(self, page, trigger_text: str, option_text: str, wait_selector: str) -> None:
        trigger = page.get_by_text(trigger_text, exact=True).first
        trigger.locator('xpath=ancestor::div[contains(@class,"select")][1]').click(timeout=10000)
        page.wait_for_timeout(500)
        page.locator("button", has_text=option_text).first.click(timeout=10000)
        page.wait_for_timeout(1000)
        page.wait_for_selector(wait_selector, timeout=15000)

    def _go_to_next_page(self, page, next_page_selector: str, wait_selector: str) -> bool:
        next_btn = page.locator(next_page_selector).first
        if next_btn.count() == 0 or next_btn.get_attribute("disabled") is not None:
            return False
        next_btn.click(timeout=10000)
        page.wait_for_timeout(1000)
        try:
            page.wait_for_selector(wait_selector, timeout=10000)
        except PlaywrightTimeoutError:
            return False
        return True

    def _parse_cards(self, html, selectors, source_name, city_key, url, decimal_comma, city_filter):
        soup = BeautifulSoup(html, "html.parser")

        listings = []
        for card in soup.select(selectors["listing"]):
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

            # The card itself is the <a> on this site; fall back to a child
            # link selector for sites where the card wraps a separate <a>.
            link_selector = selectors.get("link")
            link_el = card.select_one(link_selector) if link_selector else None
            href = link_el.get("href") if link_el else card.get("href")
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

    def _fetch_detail_data(
        self, page, detail_url: str, detail_amenities: list[dict], amenity_keywords: list[str],
        description_selector: str | None = None,
    ) -> tuple[list[str], str]:
        page.goto(detail_url, wait_until="load", timeout=45000)
        soup = BeautifulSoup(page.content(), "html.parser")
        page_text = soup.get_text(" ", strip=True)

        if _looks_like_cloudflare_challenge(page_text):
            # The JS challenge usually clears itself within a few seconds in
            # a real browser engine - give it one retry before giving up,
            # rather than storing the interstitial's own text as if it were
            # the listing's description/amenities.
            page.wait_for_timeout(6000)
            soup = BeautifulSoup(page.content(), "html.parser")
            page_text = soup.get_text(" ", strip=True)
            if _looks_like_cloudflare_challenge(page_text):
                return [], ""

        found = []
        for entry in detail_amenities:
            el = soup.select_one(entry["selector"])
            if el and el.get_text(strip=True):
                found.append(entry["label"])

        if amenity_keywords:
            _add_amenities(found, find_keyword_matches(page_text.lower(), amenity_keywords))

        if description_selector:
            return found, self._extract_first_paragraph(soup, description_selector)
        return found, page_text

    def _extract_first_paragraph(self, soup, description_selector: str) -> str:
        el = soup.select_one(description_selector)
        if not el:
            return ""
        text = el.get_text(" ", strip=True)
        # Sites often duplicate their own section heading ("Description")
        # into the leading text of the content block itself.
        if text.lower().startswith("description"):
            text = text[len("description"):].lstrip(" :–-")
        # Agencies write the blurb as one text node with real embedded
        # newlines for paragraph breaks (not separate <p> tags) - split on
        # those rather than truncating by character count, so a paragraph
        # never gets cut off mid-sentence.
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        return paragraphs[0] if paragraphs else ""
