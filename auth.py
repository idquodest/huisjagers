import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Request

SESSION_COOKIE = "session"
SESSION_LIFETIME_DAYS = 30


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed stored hash - never crash a login attempt over it.
        return False


def start_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    """Creates a new session row, returns (token, csrf_token) - token is
    what goes in the cookie, csrf_token is embedded in forms and checked
    against the session's stored value on every POST."""
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=SESSION_LIFETIME_DAYS)
    import db
    db.create_session(conn, token, user_id, csrf_token, now.isoformat(), expires_at.isoformat())
    return token, csrf_token


def get_current_user(request: Request, conn: sqlite3.Connection) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    import db
    session = db.get_session_with_user(conn, token)
    if session is None:
        return None
    expires_at = datetime.fromisoformat(session["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        db.delete_session(conn, token)
        return None
    if not session["is_active"]:
        return None
    return session


def check_csrf(request: Request, session: sqlite3.Row, submitted_token: str | None) -> bool:
    return bool(submitted_token) and secrets.compare_digest(submitted_token, session["csrf_token"])
