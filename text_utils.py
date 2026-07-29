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


# Structural/administrative section markers that show up as their own
# short paragraph within a listing's description block, marking the
# boundary between real descriptive prose and a spec dump / boilerplate
# CTA that follows it (bullet feature list, application terms, etc).
# Deliberately conservative - only added once actually observed on a real
# listing, not guessed in advance. Case-insensitive, trailing ":" ignored.
_DESCRIPTION_STOP_MARKERS = {
    "kenmerken", "layout", "indeling", "features", "bijzonderheden",
    "interesse", "interesse?", "details",
}


def extract_description(el) -> str:
    """Pulls a listing's own written blurb out of a detail-page element,
    stopping before it runs into a spec-list/CTA section (see
    _DESCRIPTION_STOP_MARKERS) - sites mix real prose and a bulleted
    feature dump in the same content block, and only the prose part
    belongs in "the description". Paragraph breaks come from either
    separate <p> tags or <br><br> within one <p> depending on the site -
    both get normalized to the same scheme before splitting."""
    for br in el.find_all("br"):
        br.replace_with("\n")
    raw = el.get_text("\n\n", strip=True)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]

    # Sites often duplicate their own section heading ("Description") into
    # the leading text of the content block itself.
    if paragraphs and paragraphs[0].lower() == "description":
        paragraphs = paragraphs[1:]

    kept = []
    for p in paragraphs:
        if p.rstrip(":").lower() in _DESCRIPTION_STOP_MARKERS:
            break
        kept.append(p)
    return "\n\n".join(kept).strip()
