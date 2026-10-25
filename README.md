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
- `auth.py` / `db.py` - accounts, sessions, saved notification preferences,
  notifier bookkeeping.
- `dashboard/` - FastAPI app: signup/login/account pages, and My Matches -
  a live filter/browse page over the shared listing pool, with a "Save as
  my notification filters" action that's the only thing feeding the 30-min
  notifier. PWA manifest + service worker.

## Known limitations

- **Being gentle with the sites.** Huisjagers scrapes every 30 minutes,
  pauses between sources and between pages, downloads each page once per
  run, and checks ikwilhuren's nationwide catalogue at most every 2 hours.
  ikwilhuren, bpdwoningfonds and pararius have temporarily blocked the home
  connection's IP when request volume was higher - keep it this low.
- **No descriptions from pararius/huurwoningen.** Cloudflare answers their
  individual listing pages with a bot check, so those listings only have the
  search-result summary, and the listing page says so. Anything written only
  on the listing page (e.g. "students only") can't be filtered on - use the
  Hide button on such listings.
- **Old listings are removed** once no scrape has seen them for
  `listing_retention_days` (14) days.
- **Later, if blocks continue:** if these sites keep blocking even at this
  reduced volume, the plan is to route outbound scraper traffic through a
  proxy.
