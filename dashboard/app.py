import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth  # noqa: E402
import db  # noqa: E402
from run import load_config  # noqa: E402

app = FastAPI(title="Huisjagers")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
SIGNUP_INVITE_CODE = os.environ.get("SIGNUP_INVITE_CODE", "")

_LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def _db_path() -> str:
    return os.path.join(BASE_DIR, load_config(CONFIG_PATH)["database"]["path"])


def _to_local(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    return datetime.fromisoformat(iso_str).astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, secure=COOKIE_SECURE, samesite="lax",
        max_age=auth.SESSION_LIFETIME_DAYS * 24 * 3600,
    )


# --- auth routes -------------------------------------------------------------

@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": None})


@app.post("/signup")
def signup_submit(
    request: Request,
    email: str = Form(...), password: str = Form(...), invite_code: str = Form(...),
    ntfy_topic_url: str = Form(""),
):
    if SIGNUP_INVITE_CODE and invite_code != SIGNUP_INVITE_CODE:
        return templates.TemplateResponse(request, "signup.html", {"error": "Invalid invite code"}, status_code=400)

    with db.connect(_db_path()) as conn:
        if db.get_user_by_email(conn, email):
            return templates.TemplateResponse(request, "signup.html", {"error": "That email is already registered"}, status_code=400)

        now_iso = datetime.now(timezone.utc).isoformat()
        user_id = db.create_user(
            conn, email, auth.hash_password(password), now_iso,
            ntfy_topic_url=ntfy_topic_url or None,
        )
        token, _ = auth.start_session(conn, user_id)
        conn.commit()

    response = RedirectResponse("/preferences", status_code=303)
    _set_session_cookie(response, token)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    with db.connect(_db_path()) as conn:
        user = db.get_user_by_email(conn, email)
        if user is None or not auth.verify_password(password, user["password_hash"]):
            return templates.TemplateResponse(request, "login.html", {"error": "Incorrect email or password"}, status_code=400)

        token, _ = auth.start_session(conn, user["id"])
        conn.commit()

    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, token)
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session and auth.check_csrf(request, session, csrf_token):
            db.delete_session(conn, request.cookies.get(auth.SESSION_COOKIE))
            conn.commit()

    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# --- preferences ---------------------------------------------------------------

@app.get("/preferences", response_class=HTMLResponse)
def preferences_form(request: Request):
    config = load_config(CONFIG_PATH)
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)

        existing = {row["city_key"]: db.preferences_row_to_dict(row) for row in db.get_user_preferences(conn, session["user_id"])}

    cities = [{"key": key, "name": cfg.get("name", key)} for key, cfg in config["cities"].items()]
    return templates.TemplateResponse(
        request, "preferences.html",
        {"session": session, "cities": cities, "existing": existing, "error": None},
    )


@app.post("/preferences/{city_key}")
def preferences_submit(
    request: Request, city_key: str,
    csrf_token: str = Form(...),
    price_min: str = Form(""), price_max: str = Form(""),
    beds_min: str = Form(""), baths_min: str = Form(""),
    sqft_min: str = Form(""), sqft_max: str = Form(""),
    pet_friendly_required: bool = Form(False),
    required_amenities: str = Form(""), exclude_keywords: str = Form(""), include_keywords: str = Form(""),
):
    def _num(s: str) -> float | None:
        s = s.strip()
        return float(s) if s else None

    def _lines(s: str) -> list[str]:
        return [line.strip() for line in s.replace(",", "\n").splitlines() if line.strip()]

    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.check_csrf(request, session, csrf_token):
            return RedirectResponse("/preferences", status_code=303)

        prefs = {
            "price_min": _num(price_min), "price_max": _num(price_max),
            "beds_min": _num(beds_min), "baths_min": _num(baths_min),
            "sqft_min": _num(sqft_min), "sqft_max": _num(sqft_max),
            "pet_friendly_required": pet_friendly_required,
            "required_amenities": _lines(required_amenities),
            "exclude_keywords": _lines(exclude_keywords),
            "include_keywords": _lines(include_keywords),
            "enabled": True,
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        db.upsert_user_preferences(conn, session["user_id"], city_key, prefs, now_iso)
        conn.commit()

    return RedirectResponse("/preferences", status_code=303)


# --- my matches ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, city: str = Query(default="")):
    config = load_config(CONFIG_PATH)
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)

        rows = db.get_user_matched_listings(conn, session["user_id"], city_key=city or None)

    listings = []
    for row in rows:
        listings.append({
            **dict(row),
            "amenities": json.loads(row["amenities"] or "[]"),
            "first_seen": _to_local(row["first_seen"]),
        })

    cities = {key: cfg.get("name", key) for key, cfg in config["cities"].items()}
    return templates.TemplateResponse(
        request, "index.html",
        {"session": session, "listings": listings, "cities": cities, "selected_city": city},
    )
