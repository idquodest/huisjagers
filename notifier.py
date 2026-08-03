import logging
import os
from urllib.parse import urlencode, urlsplit

import requests

from apply_injectors import SOURCE_APPLY_INJECTORS
from models import Listing

logger = logging.getLogger(__name__)

# Same default/override pattern as dashboard/app.py's PUBLIC_BASE_URL -
# needed here too since the ntfy "Auto-apply" action button links back to
# our own /apply/{id}/auto endpoint, not just the source site.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://huisjagers.solvire.nl")

# A personal MacroDroid webhook URL (trigger.macrodroid.com/<user>/<id>),
# not something every Huisjagers user has - unset by default, so the
# "Auto-apply" action is only added when this one deployment's owner has
# configured their own mobile automation. Not a generic per-user feature
# yet; deliberately kept out of the repo since it's account-specific.
MACRODROID_WEBHOOK_URL = os.environ.get("MACRODROID_WEBHOOK_URL", "")


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
    # "http" (not "broadcast") deliberately - ntfy's "broadcast" action
    # (an Android intent with the URL as a structured extra, caught via a
    # MacroDroid "Intent Received" trigger) never once reached MacroDroid
    # in testing, with both a custom and ntfy's documented default intent
    # name - likely not actually implemented by the installed ntfy app, or
    # blocked by Android's inter-app broadcast restrictions. "http" fires a
    # plain network request straight from the phone instead, no Android
    # intent system involved, hitting MacroDroid's own cloud Webhook
    # trigger (which relays it down to the device to fire the macro) -
    # sidesteps whatever was blocking the broadcast entirely.
    if auto_apply_url and MACRODROID_WEBHOOK_URL:
        webhook_url = f"{MACRODROID_WEBHOOK_URL}?{urlencode({'auto_apply_url': auto_apply_url})}"
        payload["actions"] = [
            {
                "action": "http",
                "label": "Auto-apply",
                "url": webhook_url,
                "method": "GET",
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
