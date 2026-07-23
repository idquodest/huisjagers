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
from matcher import matches  # noqa: E402
from run import load_config  # noqa: E402

app = FastAPI(title="Huisjagers")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
SIGNUP_INVITE_CODE = os.environ.get("SIGNUP_INVITE_CODE", "")

_LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
_FILTER_FIELDS = [
    "price_min", "price_max", "beds_min", "baths_min", "sqft_min", "sqft_max",
    "pet_friendly_required", "required_amenities", "exclude_keywords", "include_keywords",
]


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


def _num(s: str) -> float | None:
    s = (s or "").strip()
    return float(s) if s else None


def _lines(s: str) -> list[str]:
    return [line.strip() for line in (s or "").replace(",", "\n").splitlines() if line.strip()]


def _prefs_from_getter(get) -> dict:
    """Builds the same preferences-shaped dict matcher.matches() expects,
    from anything with a .get(key, default) - either Starlette's
    request.query_params (live filter bar) or a plain dict of submitted
    form fields (saving to user_city_preferences)."""
    return {
        "price_min": _num(get("price_min", "")), "price_max": _num(get("price_max", "")),
        "beds_min": _num(get("beds_min", "")), "baths_min": _num(get("baths_min", "")),
        "sqft_min": _num(get("sqft_min", "")), "sqft_max": _num(get("sqft_max", "")),
        "pet_friendly_required": get("pet_friendly_required", "") == "true",
        "required_amenities": _lines(get("required_amenities", "")),
        "exclude_keywords": _lines(get("exclude_keywords", "")),
        "include_keywords": _lines(get("include_keywords", "")),
    }


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

    response = RedirectResponse("/", status_code=303)
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


# --- account (notifications only - city filters live on the matches page) ------

@app.get("/account", response_class=HTMLResponse)
def account_form(request: Request):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)

    return templates.TemplateResponse(request, "account.html", {"session": session})


@app.post("/account/ntfy-topic")
def update_ntfy_topic(request: Request, csrf_token: str = Form(...), ntfy_topic_url: str = Form("")):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.check_csrf(request, session, csrf_token):
            return RedirectResponse("/account", status_code=303)

        db.update_user_ntfy_topic(conn, session["user_id"], ntfy_topic_url.strip() or None)
        conn.commit()

    return RedirectResponse("/account", status_code=303)


# --- save notification filters (used by the 20-min notifier, not by browsing) --

@app.post("/notification-filters/{city_key}")
def save_notification_filters(
    request: Request, city_key: str,
    csrf_token: str = Form(...),
    price_min: str = Form(""), price_max: str = Form(""),
    beds_min: str = Form(""), baths_min: str = Form(""),
    sqft_min: str = Form(""), sqft_max: str = Form(""),
    pet_friendly_required: bool = Form(False),
    required_amenities: str = Form(""), exclude_keywords: str = Form(""), include_keywords: str = Form(""),
):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.check_csrf(request, session, csrf_token):
            return RedirectResponse(f"/?city={city_key}", status_code=303)

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

    # Redirect without filter query params so the page reloads showing the
    # just-saved values as the default (saved == live, nothing to compare).
    return RedirectResponse(f"/?city={city_key}", status_code=303)


# --- my matches / live browse ---------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, city: str = Query(default="")):
    config = load_config(CONFIG_PATH)
    qp = request.query_params
    form_submitted = "price_min" in qp  # the filter bar submits every field, even blank

    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)

        matched_rows = []
        filters = None
        filters_are_saved = True
        saved_summary = None

        if city:
            saved_pref_rows = db.get_user_preferences(conn, session["user_id"], city_key=city)
            saved_prefs = db.preferences_row_to_dict(saved_pref_rows[0]) if saved_pref_rows else None

            if form_submitted:
                filters = _prefs_from_getter(qp.get)
            elif saved_prefs is not None:
                filters = saved_prefs
            else:
                filters = _prefs_from_getter(lambda k, d="": d)

            # Only meaningfully "saved" when a saved row actually exists and
            # the currently-shown filters match it exactly - covers both the
            # form-submitted-something-different case and the never-saved-
            # anything-for-this-city case (dict == None is always False, so
            # this also handles saved_prefs being None correctly).
            filters_are_saved = filters == saved_prefs
            saved_summary = saved_prefs

            for row in db.get_listings(conn, city_key=city):
                if matches(db.row_to_listing(row), filters):
                    matched_rows.append(row)
        else:
            # No single city selected: aggregate each city's live matches
            # against that city's own saved filters (no ad-hoc query
            # filtering across cities - there's no single filter bar to
            # apply, since each city can have different saved criteria).
            for city_key in config["cities"]:
                pref_rows = db.get_user_preferences(conn, session["user_id"], city_key=city_key)
                if not pref_rows:
                    continue
                prefs = db.preferences_row_to_dict(pref_rows[0])
                for row in db.get_listings(conn, city_key=city_key):
                    if matches(db.row_to_listing(row), prefs):
                        matched_rows.append(row)

    listings = []
    for row in matched_rows:
        listings.append({
            **dict(row),
            "amenities": json.loads(row["amenities"] or "[]"),
            "first_seen_sort": row["first_seen"],
            "first_seen": _to_local(row["first_seen"]),
        })
    listings.sort(key=lambda l: l["first_seen_sort"], reverse=True)

    cities = {key: cfg.get("name", key) for key, cfg in config["cities"].items()}
    return templates.TemplateResponse(
        request, "index.html",
        {
            "session": session, "listings": listings, "cities": cities, "selected_city": city,
            "filters": filters if city else None,
            "filters_are_saved": filters_are_saved,
            "saved_summary": saved_summary,
        },
    )
