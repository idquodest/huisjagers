import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
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
# Maps the sortable column name (used in the ?sort= query param and the
# template) to the key in each listing dict actually being compared.
_SORT_COLUMNS = {
    "address": "address", "price": "price", "beds": "beds", "baths": "baths",
    "sqft": "sqft", "source": "source", "first_seen": "first_seen_sort",
}


def _db_path() -> str:
    return os.path.join(BASE_DIR, load_config(CONFIG_PATH)["database"]["path"])


@app.on_event("startup")
def _ensure_schema() -> None:
    # run.py's own init_db() call would eventually pick up new columns
    # too, but only on its next 20-min cycle - the dashboard needs the
    # current schema immediately, not whenever the scraper next happens
    # to run, especially right after a deploy that added a column.
    db.init_db(_db_path())


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


# --- application templates: {placeholder} substitution --------------------------

_TEMPLATE_VAR_NAMES = ["address", "city", "price", "sqft", "rooms", "bathrooms", "source", "url", "amenities"]


def _listing_template_vars(row: dict, city_name: str) -> dict:
    amenities = json.loads(row["amenities"] or "[]")
    return {
        "address": row["address"] or "",
        "city": city_name,
        "price": f"€{row['price']:.0f}" if row["price"] is not None else "the listed price",
        "sqft": f"{row['sqft']:.0f} m²" if row["sqft"] is not None else "an unspecified size",
        "rooms": str(int(row["beds"])) if row["beds"] is not None else "an unspecified number of",
        "bathrooms": str(int(row["baths"])) if row["baths"] is not None else "an unspecified number of",
        "source": row["source"] or "",
        "url": row["url"] or "",
        "amenities": ", ".join(amenities) if amenities else "no listed amenities",
    }


def _render_template(body: str, variables: dict) -> str:
    # {word} substitution, not str.format() - a template with a stray
    # brace or a typo'd variable name (e.g. "{adress}") should render with
    # that placeholder left visibly as-is, not crash the whole page.
    return re.sub(r"\{(\w+)\}", lambda m: str(variables.get(m.group(1), m.group(0))), body)


def _num(s: str) -> float | None:
    s = (s or "").strip()
    return float(s) if s else None


def _lines(s: str) -> list[str]:
    return [line.strip() for line in (s or "").replace(",", "\n").splitlines() if line.strip()]


def _prefs_from_getter(get, excluded_sources: list[str] | None = None, hide_house_swaps_default: bool = False) -> dict:
    """Builds the same preferences-shaped dict matcher.matches() expects,
    from anything with a .get(key, default) - either Starlette's
    request.query_params (live filter bar) or a plain dict of submitted
    form fields (saving to user_city_preferences).

    excluded_sources is multi-value (checkboxes), so it doesn't fit the
    single-value get() pattern - callers extract it themselves (qp.getlist
    or a Form-bound list) and pass it straight through; blank state = [].

    hide_house_swaps_default only matters for the "nothing submitted,
    nothing saved" blank state: a real form submission always means
    "checkbox absent = explicitly unchecked" (False), same as
    pet_friendly_required, but the blank/fresh-landing state should
    default to hiding house swaps rather than showing them."""
    return {
        "price_min": _num(get("price_min", "")), "price_max": _num(get("price_max", "")),
        "beds_min": _num(get("beds_min", "")), "baths_min": _num(get("baths_min", "")),
        "sqft_min": _num(get("sqft_min", "")), "sqft_max": _num(get("sqft_max", "")),
        "pet_friendly_required": get("pet_friendly_required", "") == "true",
        "required_amenities": _lines(get("required_amenities", "")),
        "exclude_keywords": _lines(get("exclude_keywords", "")),
        "include_keywords": _lines(get("include_keywords", "")),
        "excluded_sources": excluded_sources or [],
        "hide_house_swaps": get("hide_house_swaps", "true" if hide_house_swaps_default else "") == "true",
    }


def _sort_listings(listings: list[dict], column: str, desc: bool) -> list[dict]:
    """Sorts by the given column, case-insensitively for text. Listings
    with no value for that column always sort to the end regardless of
    direction - reversing the whole list for a descending sort would
    otherwise put them first, which reads as broken ("cheapest first"
    showing unpriced listings at the very top)."""
    key = _SORT_COLUMNS.get(column, "first_seen_sort")
    with_value = [l for l in listings if l[key] is not None]
    without_value = [l for l in listings if l[key] is None]
    with_value.sort(key=lambda l: l[key].lower() if isinstance(l[key], str) else l[key])
    if desc:
        with_value.reverse()
    return with_value + without_value


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
    config = load_config(CONFIG_PATH)
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        settings_by_city = db.get_all_user_city_settings(conn, session["user_id"])

    city_statuses = []
    for key, cfg in config["cities"].items():
        row = settings_by_city.get(key)
        if row is None:
            status = "unconfigured"
        elif row["enabled"]:
            status = "on"
        else:
            status = "opted_out"
        city_statuses.append({
            "key": key, "name": cfg.get("name", key), "status": status,
            "summary": db.preferences_row_to_dict(row) if row else None,
        })

    return templates.TemplateResponse(request, "account.html", {"session": session, "city_statuses": city_statuses})


@app.post("/account/city-status/{city_key}")
def set_city_status(request: Request, city_key: str, csrf_token: str = Form(...), enabled: str = Form(...)):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.check_csrf(request, session, csrf_token):
            return RedirectResponse("/account", status_code=303)
        if city_key not in load_config(CONFIG_PATH)["cities"]:
            return RedirectResponse("/account", status_code=303)

        now_iso = datetime.now(timezone.utc).isoformat()
        db.set_city_enabled(conn, session["user_id"], city_key, enabled == "true", now_iso)
        conn.commit()

    return RedirectResponse("/account", status_code=303)


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


# --- application templates -------------------------------------------------------

@app.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        user_templates = db.get_application_templates(conn, session["user_id"])

    return templates.TemplateResponse(
        request, "templates.html",
        {"session": session, "user_templates": user_templates, "var_names": _TEMPLATE_VAR_NAMES},
    )


@app.post("/templates")
def templates_create(request: Request, csrf_token: str = Form(...), name: str = Form(...), body: str = Form(...)):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.check_csrf(request, session, csrf_token):
            return RedirectResponse("/templates", status_code=303)

        now_iso = datetime.now(timezone.utc).isoformat()
        db.create_application_template(conn, session["user_id"], name.strip() or "Untitled", body, now_iso)
        conn.commit()

    return RedirectResponse("/templates", status_code=303)


@app.post("/templates/{template_id}")
def templates_update(
    request: Request, template_id: int,
    csrf_token: str = Form(...), name: str = Form(...), body: str = Form(...),
):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.check_csrf(request, session, csrf_token):
            return RedirectResponse("/templates", status_code=303)

        now_iso = datetime.now(timezone.utc).isoformat()
        db.update_application_template(conn, session["user_id"], template_id, name.strip() or "Untitled", body, now_iso)
        conn.commit()

    return RedirectResponse("/templates", status_code=303)


@app.post("/templates/{template_id}/delete")
def templates_delete(request: Request, template_id: int, csrf_token: str = Form(...)):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.check_csrf(request, session, csrf_token):
            return RedirectResponse("/templates", status_code=303)

        db.delete_application_template(conn, session["user_id"], template_id)
        conn.commit()

    return RedirectResponse("/templates", status_code=303)


@app.get("/apply/{listing_id}", response_class=HTMLResponse)
def apply_view(request: Request, listing_id: str, template_id: int | None = None):
    config = load_config(CONFIG_PATH)
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)

        listing = db.get_listing_by_id(conn, listing_id)
        user_templates = db.get_application_templates(conn, session["user_id"])

        rendered = None
        selected_template = None
        if listing is not None and user_templates:
            selected_template = next((t for t in user_templates if t["id"] == template_id), user_templates[0])
            city_name = config["cities"].get(listing["city_key"], {}).get("name", listing["city_key"])
            rendered = _render_template(selected_template["body"], _listing_template_vars(listing, city_name))

    return templates.TemplateResponse(
        request, "apply.html",
        {
            "session": session, "listing": listing, "user_templates": user_templates,
            "selected_template": selected_template, "rendered": rendered,
        },
    )


# --- save notification filters (used by the 20-min notifier, not by browsing) --

@app.post("/notification-filters")
def save_notification_filters(
    request: Request,
    csrf_token: str = Form(...),
    cities: list[str] = Form([]), excluded_sources: list[str] = Form([]),
    price_min: str = Form(""), price_max: str = Form(""),
    beds_min: str = Form(""), baths_min: str = Form(""),
    sqft_min: str = Form(""), sqft_max: str = Form(""),
    pet_friendly_required: bool = Form(False), hide_house_swaps: bool = Form(False),
    required_amenities: str = Form(""), exclude_keywords: str = Form(""), include_keywords: str = Form(""),
):
    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.check_csrf(request, session, csrf_token):
            return RedirectResponse("/", status_code=303)

        prefs = {
            "price_min": _num(price_min), "price_max": _num(price_max),
            "beds_min": _num(beds_min), "baths_min": _num(baths_min),
            "sqft_min": _num(sqft_min), "sqft_max": _num(sqft_max),
            "pet_friendly_required": pet_friendly_required,
            "required_amenities": _lines(required_amenities),
            "exclude_keywords": _lines(exclude_keywords),
            "include_keywords": _lines(include_keywords),
            "excluded_sources": excluded_sources,
            "hide_house_swaps": hide_house_swaps,
            "enabled": True,
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        # Same values applied to every checked city - lets someone with
        # identical preferences everywhere set it up once instead of
        # repeating the form per city.
        valid_cities = load_config(CONFIG_PATH)["cities"]
        saved_cities = [c for c in cities if c in valid_cities]
        for city_key in saved_cities:
            db.upsert_user_preferences(conn, session["user_id"], city_key, prefs, now_iso)
        conn.commit()

    # Redirect back showing exactly what was just saved, so the view
    # immediately reflects it instead of resetting to the unfiltered default.
    params = [("cities", c) for c in saved_cities] + [("excluded_sources", s) for s in excluded_sources] + [
        ("price_min", price_min), ("price_max", price_max),
        ("beds_min", beds_min), ("baths_min", baths_min),
        ("sqft_min", sqft_min), ("sqft_max", sqft_max),
        ("pet_friendly_required", "true" if pet_friendly_required else ""),
        ("hide_house_swaps", "true" if hide_house_swaps else ""),
        ("required_amenities", required_amenities),
        ("exclude_keywords", exclude_keywords), ("include_keywords", include_keywords),
    ]
    return RedirectResponse(f"/?{urlencode(params)}", status_code=303)


# --- my matches / live browse ---------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    config = load_config(CONFIG_PATH)
    qp = request.query_params
    all_city_keys = list(config["cities"].keys())
    form_submitted = "price_min" in qp  # the filter card submits every field, even blank
    cities_param_present = "cities" in qp

    with db.connect(_db_path()) as conn:
        session = auth.get_current_user(request, conn)
        if session is None:
            return RedirectResponse("/login", status_code=303)

        requested_cities = [c for c in qp.getlist("cities") if c in config["cities"]] if cities_param_present else []

        if form_submitted:
            # The filter card was actually submitted ("Show results" or
            # arriving fresh from "Save") - use exactly what was checked
            # and typed, live, no persisted state involved.
            selected_cities = requested_cities
            filters = _prefs_from_getter(qp.get, excluded_sources=qp.getlist("excluded_sources"))
        elif cities_param_present and len(requested_cities) == 1:
            # "Reset to saved filters" link: just cities=X, no filter
            # fields - fall back to that one city's saved preferences.
            selected_cities = requested_cities
            pref_rows = db.get_user_preferences(conn, session["user_id"], city_key=requested_cities[0])
            filters = db.preferences_row_to_dict(pref_rows[0]) if pref_rows else _prefs_from_getter(lambda k, d="": d, hide_house_swaps_default=True)
        else:
            # Truly fresh landing on the page: show everything, unfiltered,
            # every city checked - no filtering happens until you ask for it.
            # House swaps are the one deliberate exception to "unfiltered":
            # hidden by default everywhere, not just once you've configured
            # a city, since most people using this don't have a place to swap.
            selected_cities = all_city_keys
            filters = _prefs_from_getter(lambda k, d="": d, hide_house_swaps_default=True)

        matched_rows = []
        for city_key in selected_cities:
            for row in db.get_listings(conn, city_key=city_key):
                if matches(db.row_to_listing(row), filters):
                    matched_rows.append(row)

        # A "saved vs. live" comparison only makes sense with exactly one
        # city in scope - with several (or zero) checked there's no single
        # "the" saved filter to compare the current view against.
        saved_summary = None
        filters_are_saved = False
        if len(selected_cities) == 1:
            pref_rows = db.get_user_preferences(conn, session["user_id"], city_key=selected_cities[0])
            saved_summary = db.preferences_row_to_dict(pref_rows[0]) if pref_rows else None
            filters_are_saved = filters == saved_summary

        # Cities with no saved-preferences row at all - not "opted out"
        # (that's a deliberate row with enabled=0, see db.set_city_enabled),
        # genuinely never touched. Worth a gentle nag since it's otherwise
        # indistinguishable from "I don't care about this city" and easy to
        # forget you never finished setting up.
        settings_by_city = db.get_all_user_city_settings(conn, session["user_id"])
        unconfigured_city_names = [
            cfg.get("name", key) for key, cfg in config["cities"].items() if key not in settings_by_city
        ]

    listings = []
    for row in matched_rows:
        listings.append({
            **dict(row),
            "amenities": json.loads(row["amenities"] or "[]"),
            "first_seen_sort": row["first_seen"],
            "first_seen": _to_local(row["first_seen"]),
        })

    sort_param = qp.get("sort", "-first_seen")
    sort_column = sort_param.lstrip("-")
    sort_desc = sort_param.startswith("-")
    if sort_column not in _SORT_COLUMNS:
        sort_column, sort_desc = "first_seen", True
    listings = _sort_listings(listings, sort_column, sort_desc)

    # Every header link needs to preserve the current filters/cities, just
    # swapping the sort - clicking the already-active column flips its
    # direction, any other column starts ascending.
    base_params = [(k, v) for k, v in qp.multi_items() if k not in ("sort", "csrf_token")]
    sort_links = {}
    for col in _SORT_COLUMNS:
        next_desc = not sort_desc if col == sort_column else False
        qs = urlencode(base_params + [("sort", ("-" if next_desc else "") + col)])
        sort_links[col] = f"/?{qs}"

    cities = {key: cfg.get("name", key) for key, cfg in config["cities"].items()}
    selected_city_names = [cities[c] for c in selected_cities]
    all_sources = sorted({s["name"] for city_cfg in config["cities"].values() for s in city_cfg.get("sources", [])})
    return templates.TemplateResponse(
        request, "index.html",
        {
            "session": session, "listings": listings, "cities": cities,
            "selected_cities": selected_cities, "selected_city_names": selected_city_names,
            "all_sources": all_sources,
            "sort_column": sort_column, "sort_desc": sort_desc, "sort_links": sort_links,
            "filters": filters,
            "filters_are_saved": filters_are_saved,
            "saved_summary": saved_summary,
            "unconfigured_city_names": unconfigured_city_names,
        },
    )
