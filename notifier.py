import logging
import os
from urllib.parse import urlsplit

import requests

from apply_injectors import SOURCE_APPLY_INJECTORS
from models import Listing

logger = logging.getLogger(__name__)

# Same default/override pattern as dashboard/app.py's PUBLIC_BASE_URL -
# needed here too since the ntfy "Auto-apply" action button links back to
# our own /apply/{id}/auto endpoint, not just the source site.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://huisjagers.solvire.nl")


def send_notification(ntfy_topic_url: str, listing: Listing, city_name: str) -> bool:
    """POST a match to ntfy.sh. Returns True on success (2xx response)."""
    price_str = f"€{listing.price:,.0f}" if listing.price is not None else "price n/a"
    beds_str = f"{listing.beds:g} Rooms" if listing.beds is not None else "Rooms n/a"
    baths_str = f"{listing.baths:g} Bath" if listing.baths is not None else "Baths n/a"
    sqft_str = f"{listing.sqft:g}m²" if listing.sqft is not None else "size n/a"

    amenities_str = ", ".join(listing.amenities) if listing.amenities else "none listed"

    title = f"New match in {city_name}: {price_str}"
    body = (
        f"{listing.address}\n{beds_str} / {baths_str} / {sqft_str}\n"
        f"Amenities: {amenities_str}\nSource: {listing.source}"
    )

    auto_apply_url = None
    if listing.source in SOURCE_APPLY_INJECTORS:
        auto_apply_url = f"{PUBLIC_BASE_URL}/apply/{listing.id}/auto"
        # Keep the URL visible in the plain message text too, as a fallback
        # for manual copy-paste when no automation is set up.
        body += f"\nAuto-apply: {auto_apply_url}"

    # HTTP headers are latin-1 only, so non-ASCII characters (€, m², etc.)
    # in the title crash a header-based publish. ntfy's JSON publish API
    # (POST to the server root, not the topic URL) sends everything in the
    # body instead, which is plain UTF-8 and has no such restriction.
    split = urlsplit(ntfy_topic_url)
    server_url = f"{split.scheme}://{split.netloc}"
    topic = split.path.strip("/")

    payload = {
        "topic": topic,
        "title": title,
        "message": body,
        "click": listing.url,
        "priority": 3,
    }

    # Only sources with a configured form injector can actually be
    # auto-filled - see apply_injectors.py. Leaves "click" (default tap
    # behavior, opens the listing itself) untouched for every listing.
    #
    # "broadcast" (not "view") deliberately - it fires an Android intent
    # with the URL as a structured extra instead of opening a browser, so
    # an automation app (e.g. MacroDroid) can catch it via an "Intent
    # Received" trigger and read the extra directly - no notification-text
    # parsing/regex required on the automation side.
    #
    # Deliberately NOT overriding "intent" with a custom action name here -
    # ntfy's own documented default ("io.heckel.ntfy.USER_ACTION") is what
    # its Android app is known to actually send; a custom override wasn't
    # reliably honored in testing.
    if auto_apply_url:
        payload["actions"] = [
            {
                "action": "broadcast",
                "label": "Auto-apply",
                "extras": {"auto_apply_url": auto_apply_url},
                "clear": False,
            }
        ]

    try:
        resp = requests.post(
            server_url,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Failed to send notification for %s: %s", listing.id, exc)
        return False
