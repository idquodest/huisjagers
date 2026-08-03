import fcntl
import logging
import os
from datetime import datetime, timezone

import yaml

import db
from matcher import matches
from notifier import send_notification
from scrapers import get_scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run")

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".run.lock")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def scrape_phase(config: dict, conn, now_iso: str) -> None:
    """Fetch every configured source and upsert into the shared listings
    pool. No knowledge of users/preferences - matching happens separately
    in match_phase(), against whichever listings this leaves behind."""
    # Loaded once for the whole run rather than once per source - with 8
    # cities x up to 6 sources each, re-querying every listing's data before
    # every single source's pass would be a lot of redundant DB reads for
    # a snapshot that doesn't change mid-run (upserts commit per source,
    # but this snapshot only needs to reflect "seen before this run").
    known_listings = db.get_known_listing_data(conn)

    for city_key, city_cfg in config["cities"].items():
        city_name = city_cfg.get("name", city_key)

        for source_cfg in city_cfg.get("sources", []):
            source_name = source_cfg["name"]
            try:
                scraper = get_scraper(source_cfg["type"])
                listings = scraper.fetch(city_key, source_cfg, known_listings)
            except Exception:
                logger.exception(
                    "Scraper '%s' failed for city '%s' - skipping this source",
                    source_name, city_key,
                )
                continue

            logger.info("Fetched %d listings from %s (%s)", len(listings), source_name, city_name)
            for listing in listings:
                db.upsert_listing(conn, listing, now_iso)
            # Commit per source, not once at the end of the whole scrape -
            # a single multi-minute transaction would hold SQLite's one
            # writer slot (even in WAL mode) the entire time, blocking the
            # dashboard's own writes (signup, preferences) for that whole
            # window instead of just for a moment.
            conn.commit()


def match_phase(config: dict, conn, now_iso: str) -> tuple[int, int]:
    """For every active user and every city they've set preferences on,
    evaluate listings they haven't seen yet (anti-join, not a timestamp
    cursor - robust to restarts, and means a new signup gets evaluated
    against the existing backlog automatically). Backlog listings (from
    before the user existed) are recorded as matched but never notified,
    so a new signup isn't flooded with day-one notifications for old
    listings."""
    city_names = {key: cfg.get("name", key) for key, cfg in config["cities"].items()}
    matched_count = 0
    notified_count = 0

    for user in db.get_active_users(conn):
        user_created_at = datetime.fromisoformat(user["created_at"])

        for pref_row in db.get_user_preferences(conn, user["id"]):
            city_key = pref_row["city_key"]
            preferences = db.preferences_row_to_dict(pref_row)
            city_name = city_names.get(city_key, city_key)

            for row in db.get_unprocessed_listings_for_user(conn, user["id"], city_key):
                listing = db.row_to_listing(row)
                is_match = matches(listing, preferences)
                notified = False

                if is_match:
                    matched_count += 1
                    first_seen = datetime.fromisoformat(row["first_seen"])
                    if first_seen >= user_created_at and user["ntfy_topic_url"]:
                        automation_token = user["automation_token"] or db.ensure_automation_token(conn, user["id"])
                        notified = send_notification(user["ntfy_topic_url"], listing, city_name, automation_token)
                        if notified:
                            notified_count += 1

                db.mark_user_match_status(conn, user["id"], listing.id, is_match, notified, now_iso)

        # Commit per user, same reasoning as scrape_phase's per-source
        # commits - don't hold the write lock across every user's entire
        # backlog evaluation.
        conn.commit()

    return matched_count, notified_count


def main() -> None:
    config = load_config()
    db_path = config["database"]["path"]
    db.init_db(db_path)

    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another run is already in progress - skipping this run")
        return

    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        with db.connect(db_path) as conn:
            scrape_phase(config, conn, now_iso)
            conn.commit()

            matched_count, notified_count = match_phase(config, conn, now_iso)
            conn.commit()

        logger.info(
            "Run complete: %d matched preferences, %d notifications sent",
            matched_count, notified_count,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
