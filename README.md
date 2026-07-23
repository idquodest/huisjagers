# Huisjagers

Multi-user Dutch apartment listing finder. Forked from the personal
[apartment-finder](https://github.com/idquodest/apartment-finder) project -
same scraping engine, but rebuilt for multiple independent users: each
person gets their own accounts, filters (price/size/keywords/excludes),
and notifications, matched against one shared scraped listing pool.

## Stack

Python (FastAPI + Jinja2 server-rendered pages, no separate frontend
framework), SQLite (WAL mode), Playwright for JS-rendered sources, ntfy for
push notifications, bcrypt + server-side sessions for auth.

## Local setup

```
python -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install --with-deps chromium
venv/bin/python run.py                 # one scrape+match cycle
venv/bin/uvicorn dashboard.app:app --reload
```

Set `SIGNUP_INVITE_CODE` (env var) to gate signups. `COOKIE_SECURE=false`
disables the `secure` cookie flag for local HTTP testing (defaults to
`true`, required in production behind HTTPS).

## Structure

- `config.yaml` - shared/admin-configured cities + scrape sources (no
  per-user data here - see `db.py`'s `user_city_preferences` table for that).
- `scrapers/` - site adapters (`generic_css` for static HTML,
  `playwright_site` for JS-rendered/Cloudflare-protected sites).
- `run.py` - scrape phase (shared) + per-user match phase.
- `matcher.py` - per-user preference matching, including keyword filters.
- `auth.py` / `db.py` - accounts, sessions, preferences, match tracking.
- `dashboard/` - FastAPI app: signup/login/preferences/my-matches pages,
  PWA manifest + service worker.
