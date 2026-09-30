# Changelog

## 0.1.0 — first public release

- Records every Plex client download (live notification feed plus a 5-second backstop poll) into SQLite.
- Dashboard: downloads in progress, history with search, filters and date ranges, per-user totals, detail view, CSV export.
- Sign in with a plex.tv/link code (or set `PLEX_TOKEN`); refuses accounts that don't own the server.
- Built-in password login, same-origin checks on changes, strict Content-Security-Policy, CSV formula protection.
- Demo mode with made-up data; daily consistent database snapshot; `/health` endpoint.
