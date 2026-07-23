import json
import sqlite3
from contextlib import contextmanager

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


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
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


def get_listings(conn: sqlite3.Connection, city_key: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM listings"
    params: list = []
    if city_key:
        query += " WHERE city_key = ?"
        params.append(city_key)
    query += " ORDER BY first_seen DESC"
    return conn.execute(query, params).fetchall()


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


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_active_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE is_active = 1").fetchall()


def update_user_ntfy_topic(conn: sqlite3.Connection, user_id: int, ntfy_topic_url: str | None) -> None:
    conn.execute("UPDATE users SET ntfy_topic_url = ? WHERE id = ?", (ntfy_topic_url, user_id))


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
            enabled, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, city_key) DO UPDATE SET
            price_min=excluded.price_min, price_max=excluded.price_max,
            beds_min=excluded.beds_min, baths_min=excluded.baths_min,
            sqft_min=excluded.sqft_min, sqft_max=excluded.sqft_max,
            pet_friendly_required=excluded.pet_friendly_required,
            required_amenities=excluded.required_amenities,
            exclude_keywords=excluded.exclude_keywords,
            include_keywords=excluded.include_keywords,
            enabled=excluded.enabled, updated_at=excluded.updated_at
        """,
        (
            user_id, city_key, prefs.get("price_min"), prefs.get("price_max"),
            prefs.get("beds_min"), prefs.get("baths_min"), prefs.get("sqft_min"), prefs.get("sqft_max"),
            int(prefs.get("pet_friendly_required", False)),
            json.dumps(prefs.get("required_amenities", [])),
            json.dumps(prefs.get("exclude_keywords", [])),
            json.dumps(prefs.get("include_keywords", [])),
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


def preferences_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "price_min": row["price_min"], "price_max": row["price_max"],
        "beds_min": row["beds_min"], "baths_min": row["baths_min"],
        "sqft_min": row["sqft_min"], "sqft_max": row["sqft_max"],
        "pet_friendly_required": bool(row["pet_friendly_required"]),
        "required_amenities": json.loads(row["required_amenities"] or "[]"),
        "exclude_keywords": json.loads(row["exclude_keywords"] or "[]"),
        "include_keywords": json.loads(row["include_keywords"] or "[]"),
    }


# --- per-user match/notification status ---------------------------------------

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


def get_user_matched_listings(conn: sqlite3.Connection, user_id: int, city_key: str | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT listings.*, user_listing_status.matched, user_listing_status.notified,
               user_listing_status.matched_at
        FROM user_listing_status
        JOIN listings ON listings.id = user_listing_status.listing_id
        WHERE user_listing_status.user_id = ? AND user_listing_status.matched = 1
    """
    params: list = [user_id]
    if city_key:
        query += " AND listings.city_key = ?"
        params.append(city_key)
    query += " ORDER BY listings.first_seen DESC"
    return conn.execute(query, params).fetchall()
