from models import Listing
from text_utils import any_keyword_matches, expand_keywords

# Some sources (huurwoningen.nl) mix in "house swap" listings alongside
# normal rentals - you'd need your own place to offer in exchange, which
# most people using this to find a rental don't have. Detected via the
# card text itself (e.g. "Home swap" badge), not a per-source scrape-time
# exclude, since whether to hide them is a per-user choice (default: yes).
_HOUSE_SWAP_KEYWORDS = ["home swap", "house swap", "woningruil"]


def matches(listing: Listing, preferences: dict) -> bool:
    price = listing.price
    price_min = preferences.get("price_min")
    price_max = preferences.get("price_max")
    if price is None:
        return False
    if price_min is not None and price < price_min:
        return False
    if price_max is not None and price > price_max:
        return False

    # Permissive on missing data, same as sqft below: a lot of sources
    # don't expose bedroom/bathroom counts at all (e.g. rebogroep only
    # gives a total room count), so treating "unknown" as a rejection
    # would silently hide real listings rather than just showing them
    # without that detail.
    beds_min = preferences.get("beds_min")
    if beds_min is not None and listing.beds is not None and listing.beds < beds_min:
        return False

    baths_min = preferences.get("baths_min")
    if baths_min is not None and listing.baths is not None and listing.baths < baths_min:
        return False

    sqft_min = preferences.get("sqft_min")
    if sqft_min is not None and listing.sqft is not None and listing.sqft < sqft_min:
        return False
    sqft_max = preferences.get("sqft_max")
    if sqft_max is not None and listing.sqft is not None and listing.sqft > sqft_max:
        return False

    if preferences.get("pet_friendly_required") and not listing.pet_friendly:
        return False

    excluded_sources = preferences.get("excluded_sources") or []
    if listing.source in excluded_sources:
        return False

    # Synonym-expanded (EN<->NL) - a scraped amenity might have matched in
    # whichever language the source used, so a required "balcony" needs to
    # also accept a listing whose amenities list only has "balkon".
    required_amenities = preferences.get("required_amenities") or []
    if required_amenities:
        listing_amenities = {a.strip().lower() for a in listing.amenities}
        for amenity in required_amenities:
            variants = {v.lower() for v in expand_keywords([amenity])}
            if not (variants & listing_amenities):
                return False

    # Per-user, match-time keyword filters - distinct from any scrape-time
    # exclude_keywords in config.yaml, which exist to drop things that
    # aren't real rentals at all (garages, storage units), not personal
    # taste. These search the address plus the full scraped card/detail
    # text, not just structured fields.
    searchable_text = f"{listing.address or ''} {listing.description or ''}"

    if preferences.get("hide_house_swaps", True) and any_keyword_matches(searchable_text, _HOUSE_SWAP_KEYWORDS):
        return False

    # expand_keywords adds each term's EN<->NL synonyms (see text_utils) -
    # typing "balcony" also catches "balkon" without the user needing to
    # know Dutch rental vocabulary themselves.
    exclude_keywords = preferences.get("exclude_keywords") or []
    if exclude_keywords and any_keyword_matches(searchable_text, expand_keywords(exclude_keywords)):
        return False

    include_keywords = preferences.get("include_keywords") or []
    if include_keywords and not any_keyword_matches(searchable_text, expand_keywords(include_keywords)):
        return False

    return True
