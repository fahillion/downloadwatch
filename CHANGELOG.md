# Changelog

## 0.2.0

- **Windows version (no Docker):** a zip with the official embeddable Python, time zone data and an installer that runs DownloadWatch at startup as a Scheduled Task, restricts the settings folder to Administrators/SYSTEM, and opens the firewall on private networks only. `Try-Demo.cmd` to try it without installing. Tested automatically on Windows for every change (install, login, upgrade, uninstall).
- Settings file support: `--config PATH` (or `DOWNLOADWATCH_CONFIG`) reads `KEY=VALUE` lines; environment variables still win.
- `--version`; logging works without a console.

## 0.1.1

- Fix: on first load the downloads table could show times in the browser's time zone instead of `TIMEZONE` until the next refresh.

## 0.1.0 — first public release

- Records every Plex client download (live notification feed plus a 5-second backstop poll) into SQLite.
- Dashboard: downloads in progress, history with search, filters and date ranges, per-user totals, detail view, CSV export.
- Sign in with a plex.tv/link code (or set `PLEX_TOKEN`); refuses accounts that don't own the server.
- Built-in password login, same-origin checks on changes, strict Content-Security-Policy, CSV formula protection.
- Demo mode with made-up data; daily consistent database snapshot; `/health` endpoint.
