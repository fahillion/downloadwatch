# Security

Please report security problems privately via GitHub's **Report a vulnerability** (Security tab), not as a public issue.

What DownloadWatch protects, and how:

- **Plex token:** stored in `data/plex_token` (mode 0600) or taken from `PLEX_TOKEN`; sent only to your Plex server and plex.tv, only in the `X-Plex-Token` header; removed from logs and stored activity.
- **Dashboard:** optional HTTP Basic login (`AUTH_PASSWORD`, constant-time comparison, slowed after repeated failures). Put it behind HTTPS (a reverse proxy) if you reach it from outside your LAN; Basic login over plain HTTP is only suitable on a trusted network.
- **Browser:** strict Content-Security-Policy (no inline script), no framing, changes (sign-in) only from the page itself (JSON + same Origin), all Plex-supplied text rendered as text, CSV cells that could run as spreadsheet formulas are neutralised.
- **Database:** parameterised SQL only.
- **Container:** runs as UID 1000, works with a read-only root filesystem and no capabilities (see `docker-compose.yml`).
