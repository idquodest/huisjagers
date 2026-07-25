import re

# Most sources are Dutch-language; a couple are English (see huisjagers'
# config.yaml comments). A user typing "balcony" into Include/Exclude
# should also catch "balkon" without needing to know Dutch - each group
# below is a set of interchangeable terms (EN/NL, sometimes both use the
# same word e.g. "garage"/"lift"). Best-effort/common rental vocabulary,
# not a real translator - deliberately small and hand-picked rather than
# a general dictionary, so it stays predictable.
_SYNONYM_GROUPS: list[set[str]] = [
    {"balcony", "balkon"},
    {"terrace", "terras"},
    {"roof terrace", "dakterras"},
    {"garden", "tuin"},
    {"elevator", "lift"},
    {"dishwasher", "vaatwasser"},
    {"dryer", "droger"},
    {"washing machine", "wasmachine"},
    {"storage", "berging", "opslag"},
    {"bike storage", "fietsenstalling", "fietsenberging"},
    {"solar panels", "zonnepanelen"},
    {"underfloor heating", "vloerverwarming"},
    {"air conditioning", "airconditioning"},
    {"furnished", "gemeubileerd"},
    {"unfurnished", "ongemeubileerd"},
    {"bare", "kaal", "casco"},
    {"upholstered", "gestoffeerd"},
    {"parking", "parkeren", "parkeerplaats"},
    {"ground floor", "begane grond"},
    {"shared", "gedeeld", "gemeenschappelijk"},
    {"student", "studenten"},
    {"temporary", "tijdelijk"},
    {"new construction", "nieuwbouw"},
    {"renovated", "gerenoveerd"},
    {"attic", "zolder"},
    {"basement", "kelder"},
]
_SYNONYM_LOOKUP: dict[str, set[str]] = {
    term: group for group in _SYNONYM_GROUPS for term in group
}


def expand_keywords(keywords: list[str]) -> list[str]:
    """Adds each keyword's known synonyms (EN<->NL) alongside it, so typing
    just one language still catches listings written in the other. A
    keyword with no known synonyms passes through unchanged."""
    expanded: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        for variant in _SYNONYM_LOOKUP.get(kw.strip().lower(), {kw}):
            if variant.lower() not in seen:
                expanded.append(variant)
                seen.add(variant.lower())
    return expanded


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
