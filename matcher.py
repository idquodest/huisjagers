from models import Listing
from text_utils import any_keyword_matches


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

    beds_min = preferences.get("beds_min")
    if beds_min is not None:
        if listing.beds is None or listing.beds < beds_min:
            return False

    baths_min = preferences.get("baths_min")
    if baths_min is not None:
        if listing.baths is None or listing.baths < baths_min:
            return False

    # Deliberately asymmetric with beds/baths above: size data is spotty
    # across sources, so a listing with unknown sqft is still allowed
    # through rather than silently dropped.
    sqft_min = preferences.get("sqft_min")
    if sqft_min is not None and listing.sqft is not None and listing.sqft < sqft_min:
        return False
    sqft_max = preferences.get("sqft_max")
    if sqft_max is not None and listing.sqft is not None and listing.sqft > sqft_max:
        return False

    if preferences.get("pet_friendly_required") and not listing.pet_friendly:
        return False

    required_amenities = preferences.get("required_amenities") or []
    if required_amenities:
        listing_amenities = {a.strip().lower() for a in listing.amenities}
        if not all(a.strip().lower() in listing_amenities for a in required_amenities):
            return False

    # Per-user, match-time keyword filters - distinct from any scrape-time
    # exclude_keywords in config.yaml, which exist to drop things that
    # aren't real rentals at all (garages, storage units), not personal
    # taste. These search the address plus the full scraped card/detail
    # text, not just structured fields.
    searchable_text = f"{listing.address or ''} {listing.description or ''}"

    exclude_keywords = preferences.get("exclude_keywords") or []
    if exclude_keywords and any_keyword_matches(searchable_text, exclude_keywords):
        return False

    include_keywords = preferences.get("include_keywords") or []
    if include_keywords and not any_keyword_matches(searchable_text, include_keywords):
        return False

    return True
