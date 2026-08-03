import json
import secrets
import sqlite3
from contextlib import contextmanager

import bcrypt

from models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    city_key TEXT NOT NULL,
    address TEXT,
    description TEXT,
    price REAL,
    beds REAL,
    baths REAL,
    sqft REAL,
    pet_friendly INTEGER,
    amenities TEXT,
    url TEXT,
    posted_date TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    ntfy_topic_url TEXT,
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_city_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    city_key TEXT NOT NULL,
    price_min REAL,
    price_max REAL,
    beds_min REAL,
    baths_min REAL,
    sqft_min REAL,
    sqft_max REAL,
    pet_friendly_required INTEGER NOT NULL DEFAULT 0,
    required_amenities TEXT,
    exclude_keywords TEXT,
    include_keywords TEXT,
    excluded_sources TEXT,
    hide_house_swaps INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, city_key)
);

CREATE TABLE IF NOT EXISTS user_listing_status (
    user_id INTEGER NOT NULL REFERENCES users(id),
    listing_id TEXT NOT NULL REFERENCES listings(id),
    matched INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    matched_at TEXT,
    notified_at TEXT,
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS application_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Dashboard writes (login/preferences) now happen alongside the
    # scraper's writes - WAL lets readers/writers avoid blocking each
    # other, and busy_timeout makes concurrent writers retry instead of
    # failing immediately with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """Adds a column to an existing table if it's not already there - lets
    the schema evolve without a full migration framework. Safe to call on
    every startup; SQLite's ALTER TABLE ADD COLUMN with a constant DEFAULT
    also backfills existing rows, not just new ones."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "user_city_preferences", "hide_house_swaps", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "user_city_preferences", "excluded_sources", "TEXT")
        _ensure_column(conn, "users", "oauth_provider", "TEXT")
        _ensure_column(conn, "users", "oauth_id", "TEXT")
        _ensure_column(conn, "users", "automation_token", "TEXT")
        # Partial index (only rows that actually have an oauth link) so two
        # users can't end up linked to the same provider account, without
        # constraining the many password-only rows where both are NULL.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oauth "
            "ON users(oauth_provider, oauth_id) WHERE oauth_provider IS NOT NULL"
        )
        conn.commit()


# --- listings (shared pool) -------------------------------------------------

def upsert_listing(conn: sqlite3.Connection, listing: Listing, now_iso: str) -> bool:
    """Insert or refresh a listing. Returns True if this is a newly seen listing."""
    existing = conn.execute(
        "SELECT id FROM listings WHERE id = ?", (listing.id,)
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO listings (
                id, source, city_key, address, description, price, beds, baths, sqft,
                pet_friendly, amenities, url, posted_date, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.id, listing.source, listing.city_key, listing.address,
                listing.description, listing.price, listing.beds, listing.baths, listing.sqft,
                int(listing.pet_friendly), json.dumps(listing.amenities), listing.url,
                listing.posted_date, now_iso, now_iso,
            ),
        )
        return True

    conn.execute(
        """
        UPDATE listings SET description = ?, price = ?, beds = ?, baths = ?, sqft = ?,
            pet_friendly = ?, amenities = ?, last_seen = ?
        WHERE id = ?
        """,
        (
            listing.description, listing.price, listing.beds, listing.baths, listing.sqft,
            int(listing.pet_friendly), json.dumps(listing.amenities), now_iso, listing.id,
        ),
    )
    return False


def get_known_listing_data(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every listing already in the DB, mapped to its current amenities and
    description - lets a scraper skip re-fetching a listing's own detail
    page (and re-risking a Cloudflare challenge) once it's already been
    visited, instead of paying for that page load on every single cycle."""
    rows = conn.execute("SELECT id, amenities, description FROM listings").fetchall()
    return {
        row["id"]: {"amenities": json.loads(row["amenities"] or "[]"), "description": row["description"]}
        for row in rows
    }


def get_listings(conn: sqlite3.Connection, city_key: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM listings"
    params: list = []
    if city_key:
        query += " WHERE city_key = ?"
        params.append(city_key)
    query += " ORDER BY first_seen DESC"
    return conn.execute(query, params).fetchall()


def get_listing_by_id(conn: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        source=row["source"],
        city_key=row["city_key"],
        address=row["address"],
        price=row["price"],
        beds=row["beds"],
        baths=row["baths"],
        url=row["url"],
        sqft=row["sqft"],
        pet_friendly=bool(row["pet_friendly"]),
        amenities=json.loads(row["amenities"] or "[]"),
        posted_date=row["posted_date"],
        description=row["description"],
    )


# --- users / sessions --------------------------------------------------------

def create_user(
    conn: sqlite3.Connection, email: str, password_hash: str, now_iso: str,
    display_name: str | None = None, ntfy_topic_url: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO users (email, password_hash, display_name, ntfy_topic_url, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (email, password_hash, display_name, ntfy_topic_url, now_iso),
    )
    return cur.lastrowid


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user_by_oauth(conn: sqlite3.Connection, provider: str, oauth_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE oauth_provider = ? AND oauth_id = ?", (provider, oauth_id)
    ).fetchone()


def create_oauth_user(
    conn: sqlite3.Connection, email: str, provider: str, oauth_id: str, now_iso: str,
    display_name: str | None = None,
) -> int:
    """Social-login signup: there's no password to check, but password_hash
    stays NOT NULL, so a real (unguessable, unusable) bcrypt hash is stored
    anyway rather than loosening the column constraint - it just never
    matches any password a user could type."""
    unusable_hash = bcrypt.hashpw(secrets.token_urlsafe(32).encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    cur = conn.execute(
        """
        INSERT INTO users (email, password_hash, display_name, oauth_provider, oauth_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (email, unusable_hash, display_name, provider, oauth_id, now_iso),
    )
    return cur.lastrowid


def link_oauth_to_user(conn: sqlite3.Connection, user_id: int, provider: str, oauth_id: str) -> None:
    conn.execute(
        "UPDATE users SET oauth_provider = ?, oauth_id = ? WHERE id = ?", (provider, oauth_id, user_id)
    )


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_active_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE is_active = 1").fetchall()


def update_user_ntfy_topic(conn: sqlite3.Connection, user_id: int, ntfy_topic_url: str | None) -> None:
    conn.execute("UPDATE users SET ntfy_topic_url = ? WHERE id = ?", (ntfy_topic_url, user_id))


def get_user_by_automation_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE automation_token = ?", (token,)).fetchone()


def ensure_automation_token(conn: sqlite3.Connection, user_id: int) -> str:
    """Non-browser clients (e.g. a phone automation app) can't send the
    session cookie a logged-in browser would - this token is a stand-in
    identity for that, embedded directly in the auto-apply URL rather than
    something the user has to configure by hand."""
    row = conn.execute("SELECT automation_token FROM users WHERE id = ?", (user_id,)).fetchone()
    if row and row["automation_token"]:
        return row["automation_token"]
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE users SET automation_token = ? WHERE id = ?", (token, user_id))
    return token


def create_session(
    conn: sqlite3.Connection, token: str, user_id: int, csrf_token: str,
    now_iso: str, expires_at_iso: str,
) -> None:
    conn.execute(
        "INSERT INTO sessions (token, user_id, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, user_id, csrf_token, now_iso, expires_at_iso),
    )


def get_session_with_user(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT sessions.*, users.email, users.display_name, users.ntfy_topic_url,
               users.is_active, users.created_at AS user_created_at
        FROM sessions JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?
        """,
        (token,),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --- per-user preferences -----------------------------------------------------

def upsert_user_preferences(conn: sqlite3.Connection, user_id: int, city_key: str, prefs: dict, now_iso: str) -> None:
    conn.execute(
        """
        INSERT INTO user_city_preferences (
            user_id, city_key, price_min, price_max, beds_min, baths_min, sqft_min, sqft_max,
            pet_friendly_required, required_amenities, exclude_keywords, include_keywords,
            excluded_sources, hide_house_swaps, enabled, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, city_key) DO UPDATE SET
            price_min=excluded.price_min, price_max=excluded.price_max,
            beds_min=excluded.beds_min, baths_min=excluded.baths_min,
            sqft_min=excluded.sqft_min, sqft_max=excluded.sqft_max,
            pet_friendly_required=excluded.pet_friendly_required,
            required_amenities=excluded.required_amenities,
            exclude_keywords=excluded.exclude_keywords,
            include_keywords=excluded.include_keywords,
            excluded_sources=excluded.excluded_sources,
            hide_house_swaps=excluded.hide_house_swaps,
            enabled=excluded.enabled, updated_at=excluded.updated_at
        """,
        (
            user_id, city_key, prefs.get("price_min"), prefs.get("price_max"),
            prefs.get("beds_min"), prefs.get("baths_min"), prefs.get("sqft_min"), prefs.get("sqft_max"),
            int(prefs.get("pet_friendly_required", False)),
            json.dumps(prefs.get("required_amenities", [])),
            json.dumps(prefs.get("exclude_keywords", [])),
            json.dumps(prefs.get("include_keywords", [])),
            json.dumps(prefs.get("excluded_sources", [])),
            int(prefs.get("hide_house_swaps", True)),
            int(prefs.get("enabled", True)), now_iso,
        ),
    )


def get_user_preferences(conn: sqlite3.Connection, user_id: int, city_key: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM user_city_preferences WHERE user_id = ? AND enabled = 1"
    params: list = [user_id]
    if city_key:
        query += " AND city_key = ?"
        params.append(city_key)
    return conn.execute(query, params).fetchall()


def get_all_user_city_settings(conn: sqlite3.Connection, user_id: int) -> dict[str, sqlite3.Row]:
    """Every row for this user keyed by city_key, regardless of enabled -
    unlike get_user_preferences(), this lets a caller distinguish three
    real states: never configured (no row at all), actively configured
    (row, enabled=1), and explicitly opted out (row, enabled=0). The first
    two look identical to the notifier (no notifications either way) but
    are very different to a user checking "did I set this up or forget
    it?"."""
    rows = conn.execute(
        "SELECT * FROM user_city_preferences WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {row["city_key"]: row for row in rows}


def set_city_enabled(conn: sqlite3.Connection, user_id: int, city_key: str, enabled: bool, now_iso: str) -> None:
    """Toggles notifications for a city on/off without touching whatever
    filter values are already saved there (if any) - opting out and back
    in restores your old criteria rather than losing them. If no row
    exists yet (opting out of a city you never configured), creates a
    bare marker row so it shows up as an explicit decision, not silence."""
    existing = conn.execute(
        "SELECT id FROM user_city_preferences WHERE user_id = ? AND city_key = ?", (user_id, city_key)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE user_city_preferences SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), now_iso, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO user_city_preferences (user_id, city_key, enabled, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, city_key, int(enabled), now_iso),
        )


def preferences_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "price_min": row["price_min"], "price_max": row["price_max"],
        "beds_min": row["beds_min"], "baths_min": row["baths_min"],
        "sqft_min": row["sqft_min"], "sqft_max": row["sqft_max"],
        "pet_friendly_required": bool(row["pet_friendly_required"]),
        "required_amenities": json.loads(row["required_amenities"] or "[]"),
        "exclude_keywords": json.loads(row["exclude_keywords"] or "[]"),
        "include_keywords": json.loads(row["include_keywords"] or "[]"),
        "excluded_sources": json.loads(row["excluded_sources"] or "[]"),
        "hide_house_swaps": bool(row["hide_house_swaps"]),
    }


# --- notifier bookkeeping (run.py only - the dashboard filters listings
# live against saved/query preferences instead of reading stored match
# state, so nothing here is read by dashboard/app.py) ------------------------

def get_unprocessed_listings_for_user(conn: sqlite3.Connection, user_id: int, city_key: str) -> list[sqlite3.Row]:
    # Anti-join, not a timestamp cursor: "listings this user has never been
    # evaluated against". Robust to restarts/crashes and means a new signup
    # is automatically evaluated against the existing backlog.
    return conn.execute(
        """
        SELECT listings.* FROM listings
        LEFT JOIN user_listing_status
            ON user_listing_status.listing_id = listings.id AND user_listing_status.user_id = ?
        WHERE listings.city_key = ? AND user_listing_status.listing_id IS NULL
        """,
        (user_id, city_key),
    ).fetchall()


def mark_user_match_status(
    conn: sqlite3.Connection, user_id: int, listing_id: str, matched: bool, notified: bool, now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO user_listing_status (user_id, listing_id, matched, notified, matched_at, notified_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, listing_id, int(matched), int(notified),
            now_iso if matched else None, now_iso if notified else None,
        ),
    )


# --- application templates -----------------------------------------------------

def create_application_template(conn: sqlite3.Connection, user_id: int, name: str, body: str, now_iso: str) -> int:
    cur = conn.execute(
        "INSERT INTO application_templates (user_id, name, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, body, now_iso, now_iso),
    )
    return cur.lastrowid


def get_application_templates(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM application_templates WHERE user_id = ? ORDER BY created_at", (user_id,)
    ).fetchall()


def get_application_template(conn: sqlite3.Connection, user_id: int, template_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM application_templates WHERE user_id = ? AND id = ?", (user_id, template_id)
    ).fetchone()


def update_application_template(
    conn: sqlite3.Connection, user_id: int, template_id: int, name: str, body: str, now_iso: str,
) -> None:
    conn.execute(
        "UPDATE application_templates SET name = ?, body = ?, updated_at = ? WHERE user_id = ? AND id = ?",
        (name, body, now_iso, user_id, template_id),
    )


def delete_application_template(conn: sqlite3.Connection, user_id: int, template_id: int) -> None:
    conn.execute(
        "DELETE FROM application_templates WHERE user_id = ? AND id = ?", (user_id, template_id)
    )
