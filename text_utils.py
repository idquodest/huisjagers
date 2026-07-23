import re


def find_keyword_matches(text: str, keywords: list[str]) -> list[str]:
    # Word-boundary, not substring: a plain "in" check would match "Park"
    # inside "Parking" (or "Bus" inside "Business"), tagging things with a
    # spurious amenity that was never actually mentioned on its own. Same
    # rule applies whether this is scraper-side amenity detection or
    # matcher-side keyword filtering - one implementation, used by both, so
    # they can't drift apart.
    return [kw for kw in keywords if re.search(rf"\b{re.escape(kw.lower())}\b", text)]


def any_keyword_matches(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(re.search(rf"\b{re.escape(kw.lower())}\b", text_lower) for kw in keywords)
