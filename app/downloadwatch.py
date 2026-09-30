#!/usr/bin/env python3
"""DownloadWatch for Plex: a permanent history of who downloaded what from your Plex server.

Plex shows a client download ("Download" in the Plex apps, for offline viewing) only while it runs, and keeps no
timestamped record afterwards. DownloadWatch listens to your Plex Media Server and writes every download into its
own SQLite database the moment it starts, then serves a small dashboard, a JSON API and CSV export.

Sources (Plex Media Server HTTP API):
  * /:/eventsource/notifications  live "activity" events: started / updated / ended. Primary source.
  * /activities                   polled every POLL_INTERVAL_SECONDS as a backstop (reconnects, missed events).
  * /accounts                     user id -> name.
  * /library/metadata/<id>        title, year, show/season/episode, library, source file size (cached in the DB).
Plex does not include the device in download activities, so the device is not recorded.

Status rules (never claim success without evidence):
  Active     the download is running
  Completed  Plex ended it at >= 99 %
  Stopped    it ended below 99 % (cancelled, failed or interrupted; Plex does not say which)
  Unknown    it disappeared while DownloadWatch or Plex was unreachable and the last progress was below 99 %

Standard library only (Python 3.10+). Configuration is by environment variables; see README.md.
"""
import base64
import contextlib
import csv
import datetime as dt
import hmac
import io
import json
import logging
import logging.handlers
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__version__ = "0.2.0"
PRODUCT = "DownloadWatch for Plex"
PLEX_TV = "https://plex.tv"
DONE_AT = 99.0
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
EXPORT_COLS = [("started_local", "Started"), ("completed_local", "Completed"), ("plex_username", "User"),
               ("display_title", "Title"), ("media_type", "Type"), ("year", "Year"), ("library", "Library"),
               ("status", "Status"), ("last_progress", "Progress %"), ("size_gb", "File size (GB)"),
               ("duration_min", "Duration (min)"), ("resolution", "Resolution"), ("metadata_id", "Plex metadata ID"),
               ("activity_uuid", "Activity UUID")]

log = logging.getLogger("downloadwatch")
C = {}          # configuration (see load_config)
TOKEN = ""      # Plex token in use; never logged or served
state = {}
db_lock = threading.Lock()
setup_lock = threading.Lock()
setup = {"pin_id": None, "code": None, "expires": 0.0, "last_check": 0.0, "error": None}
failures = []   # timestamps of failed logins (brute-force brake)


# ---------------------------------------------------------------- configuration
def read_text(path):
    with open(path) as f:
        return f.read().strip()


def env_bool(env, key, default=False):
    return str(env.get(key, str(default))).strip().lower() in ("1", "true", "yes", "on")


def read_env_file(path):
    """KEY=VALUE lines (# comments, optional quotes). Used by the Windows install and --config."""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key.strip()] = value
    return out


def default_data_dir():
    if os.name == "nt":
        return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "DownloadWatch", "data")
    return "/data"


def load_config(env=None, config_file=None):
    env = dict(os.environ if env is None else env)
    config_file = config_file or env.get("DOWNLOADWATCH_CONFIG")
    if config_file:
        env = {**read_env_file(config_file), **env}   # real environment variables win over the file
    data = env.get("DATA_DIR") or default_data_dir()
    tzname = env.get("TIMEZONE") or env.get("TZ") or "UTC"
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        tz, tzname = ZoneInfo("UTC"), "UTC"
    nav = []
    for item in (env.get("NAV_LINKS") or "").split(","):
        label, _, url = item.partition("=")
        label, url = label.strip(), url.strip()
        if label and (url.startswith("/") or url.startswith("http://") or url.startswith("https://")):
            nav.append({"label": label, "url": url})
    password = env.get("AUTH_PASSWORD", "")
    if not password and env.get("AUTH_PASSWORD_FILE"):
        try:
            password = read_text(env["AUTH_PASSWORD_FILE"])
        except OSError:
            password = ""
    return {
        "plex_url": (env.get("PLEX_URL") or "").rstrip("/"),
        "token_env": (env.get("PLEX_TOKEN") or "").strip(),
        "token_file": env.get("PLEX_TOKEN_FILE") or os.path.join(data, "plex_token"),
        "data_dir": data,
        "db_path": env.get("DATABASE_PATH") or os.path.join(data, "downloads.db"),
        "poll_s": max(1.0, float(env.get("POLL_INTERVAL_SECONDS", "5"))),
        "host": env.get("HOST", "0.0.0.0"),
        "port": int(env.get("PORT", env.get("WEB_PORT", "8090"))),
        "tz": tz, "tzname": tzname,
        "auth_user": env.get("AUTH_USERNAME", "admin"),
        "auth_password": password,
        "auth_disabled": env_bool(env, "AUTH_DISABLED"),   # "a reverse proxy in front already asks for a login"
        "demo": env_bool(env, "DEMO"),
        "debug": env_bool(env, "DEBUG_PLEX_ACTIVITY"),
        "log_file": env.get("LOG_FILE", ""),
        "snapshot": env_bool(env, "SNAPSHOT", True),
        "title": env.get("SITE_TITLE", "DownloadWatch"),
        "nav": nav,
    }


def reset_state():
    state.clear()
    state.update(plex_connected=False, auth_error=False, last_poll=None, last_event=None, feed_connected=False,
                 last_error=None, started=None, server_name=None)


reset_state()


# ---------------------------------------------------------------- helpers
def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.isoformat().replace("+00:00", "Z") if t else None


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def local(s):
    return parse_iso(s).astimezone(C["tz"]).strftime("%Y-%m-%d %H:%M:%S") if s else ""


def scrub(text):
    if TOKEN:
        text = text.replace(TOKEN, "<token>")
    return re.sub(r'(?i)(token"?\s*[:=]\s*"?)[A-Za-z0-9_\-]{12,}', r"\1<token>", text)


def client_id():
    """Stable per-install identifier that Plex and plex.tv use to recognise this app."""
    path = os.path.join(C["data_dir"], "client_id")
    try:
        return read_text(path)
    except OSError:
        cid = "downloadwatch-" + uuidlib.uuid4().hex
        try:
            os.makedirs(C["data_dir"], exist_ok=True)
            with open(path, "w") as f:
                f.write(cid)
        except OSError:
            pass
        return cid


def plex_headers(token=None):
    h = {"Accept": "application/json", "X-Plex-Client-Identifier": client_id(), "X-Plex-Product": PRODUCT,
         "X-Plex-Version": __version__, "X-Plex-Device-Name": "DownloadWatch"}
    if token:
        h["X-Plex-Token"] = token
    return h


def http_json(url, token=None, method="GET", timeout=10):
    req = urllib.request.Request(url, headers=plex_headers(token), method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else {}


def plex_request(path, timeout=10):
    req = urllib.request.Request(C["plex_url"] + path, headers=plex_headers(TOKEN))
    return urllib.request.urlopen(req, timeout=timeout)


def plex_json(path):
    with plex_request(path) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def note_error(where, e):
    if isinstance(e, urllib.error.HTTPError):
        e.close()
    if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
        if not state["auth_error"]:
            log.error("Plex rejected the token (%s, HTTP %s). Sign in again from the dashboard or replace PLEX_TOKEN.",
                      where, e.code)
        state["auth_error"] = True
        msg = f"Plex rejected the token (HTTP {e.code})"
    else:
        msg = f"{where}: {type(e).__name__}: {scrub(str(e))[:200]}"
        if state["plex_connected"] or state["last_error"] != msg:
            log.warning("Plex unreachable: %s", msg)
    state["plex_connected"] = False
    state["last_error"] = msg


# ---------------------------------------------------------------- token
def load_token():
    if C["token_env"]:
        return C["token_env"]
    try:
        return read_text(C["token_file"])
    except OSError:
        return ""


def save_token(token):
    path = C["token_file"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def configured():
    return bool(TOKEN) or C["demo"]


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_uuid TEXT UNIQUE,
    plex_user_id TEXT,
    plex_username TEXT,
    client_identifier TEXT,
    device_name TEXT,
    platform TEXT,
    media_type TEXT,
    metadata_id INTEGER,
    media_part_id INTEGER,
    title TEXT,
    parent_title TEXT,
    grandparent_title TEXT,
    year INTEGER,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    completed_at TEXT,
    ended_at TEXT,
    first_progress REAL,
    last_progress REAL,
    status TEXT,
    bytes_estimated INTEGER,
    bytes_transferred INTEGER,
    raw_activity TEXT
);
CREATE INDEX IF NOT EXISTS downloads_started ON downloads(started_at);
CREATE INDEX IF NOT EXISTS downloads_user ON downloads(plex_username);
CREATE TABLE IF NOT EXISTS metadata_cache (
    metadata_id INTEGER PRIMARY KEY,
    fetched_at TEXT,
    doc TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT,
    updated_at TEXT
);
"""
SCHEMA_VERSION = 1


@contextlib.contextmanager
def db():
    """Connection that commits on success, rolls back on error, and is always closed."""
    conn = sqlite3.connect(C["db_path"], timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(C["db_path"]) or ".", exist_ok=True)
    with db_lock, db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        if c.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# ---------------------------------------------------------------- enrichment
accounts_at = [0.0]


def refresh_accounts(force=False):
    if C["demo"]:
        return
    if not force and time.time() - accounts_at[0] < 3600:
        return
    try:
        rows = plex_json("/accounts").get("MediaContainer", {}).get("Account", [])
    except Exception as e:
        note_error("accounts", e)
        return
    accounts_at[0] = time.time()
    now = iso(utcnow())
    with db_lock, db() as c:
        for a in rows:
            if a.get("name"):
                c.execute("INSERT INTO accounts(id,name,updated_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                          "name=excluded.name, updated_at=excluded.updated_at", (str(a.get("id")), a["name"], now))


def username(user_id, title):
    with db() as c:
        row = c.execute("SELECT name FROM accounts WHERE id=?", (str(user_id),)).fetchone()
    if not row:
        refresh_accounts(force=True)
        with db() as c:
            row = c.execute("SELECT name FROM accounts WHERE id=?", (str(user_id),)).fetchone()
    if row:
        return row["name"]
    m = re.match(r"Media download by (.+)$", title or "")
    return m.group(1) if m else None


def metadata(mid, part_id):
    """Enrichment for a metadata id (cached; library items rarely change)."""
    if not mid:
        return {}
    with db() as c:
        row = c.execute("SELECT doc FROM metadata_cache WHERE metadata_id=?", (mid,)).fetchone()
    if row:
        doc = json.loads(row["doc"])
    else:
        try:
            m = plex_json(f"/library/metadata/{int(mid)}")["MediaContainer"]["Metadata"][0]
        except Exception as e:
            log.warning("Metadata lookup failed for %s: %s", mid, type(e).__name__)
            return {}
        parts = []
        for media in m.get("Media", []):
            for p in media.get("Part", []):
                parts.append({"id": p.get("id"), "size": p.get("size"), "container": p.get("container") or media.get("container"),
                              "resolution": media.get("videoResolution"), "bitrate": media.get("bitrate"),
                              "duration": p.get("duration") or media.get("duration")})
        doc = {k: m.get(k) for k in ("type", "title", "parentTitle", "grandparentTitle", "index", "parentIndex",
                                     "year", "librarySectionTitle", "duration")}
        doc["parts"] = parts
        with db_lock, db() as c:
            c.execute("INSERT OR REPLACE INTO metadata_cache(metadata_id,fetched_at,doc) VALUES(?,?,?)",
                      (mid, iso(utcnow()), json.dumps(doc)))
    part = next((p for p in doc.get("parts", []) if str(p.get("id")) == str(part_id)), None) \
        or (doc.get("parts") or [None])[0] or {}
    return {"type": doc.get("type"), "title": doc.get("title"), "parent_title": doc.get("parentTitle"),
            "grandparent_title": doc.get("grandparentTitle"), "year": doc.get("year"),
            "size": part.get("size"), "_doc": doc}


def display_title_of(md, fallback):
    if md.get("type") == "episode":
        doc = md.get("_doc") or {}
        return f'{md.get("grandparent_title")} — S{doc.get("parentIndex") or 0:02d}E{doc.get("index") or 0:02d} — {md.get("title")}'
    if md.get("title"):
        return f'{md["title"]} ({md["year"]})' if md.get("year") else md["title"]
    return fallback or "?"


# ---------------------------------------------------------------- recording
def is_download(a):
    return isinstance(a, dict) and a.get("type") == "media.download"


def seen(a, event="updated"):
    """Record a media.download activity (from the feed or a poll)."""
    uid = a.get("uuid")
    if not uid:
        return
    now = iso(utcnow())
    progress = a.get("progress")
    progress = float(progress) if isinstance(progress, (int, float)) and progress >= 0 else None
    ctx = a.get("Context") or {}
    raw = scrub(json.dumps(a))
    if C["debug"]:
        log.info("activity %s %s", event, raw)
    with db() as c:
        row = c.execute("SELECT id FROM downloads WHERE activity_uuid=?", (uid,)).fetchone()
    if row is None:
        mid = int(ctx["metadataID"]) if str(ctx.get("metadataID", "")).isdigit() else None
        pid = int(ctx["partID"]) if str(ctx.get("partID", "")).isdigit() else None
        md = metadata(mid, pid)
        user = username(a.get("userID"), a.get("title"))
        with db_lock, db() as c:
            cur = c.execute("""INSERT OR IGNORE INTO downloads(activity_uuid,plex_user_id,plex_username,client_identifier,media_type,
                         metadata_id,media_part_id,title,parent_title,grandparent_title,year,started_at,last_seen_at,
                         first_progress,last_progress,status,bytes_estimated,raw_activity)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Active',?,?)""",
                            (uid, str(a.get("userID")), user, ctx.get("deviceID") or None, md.get("type"), mid, pid,
                             md.get("title") or a.get("subtitle"), md.get("parent_title"), md.get("grandparent_title"),
                             md.get("year"), now, now, progress, progress, md.get("size"), raw))
        if cur.rowcount:
            log.info("New download: %s -> %s", user, display_title_of(md, a.get("subtitle")))
    if event == "ended":
        end(uid, progress, how="ended")
        return
    with db_lock, db() as c:
        c.execute("""UPDATE downloads SET last_seen_at=?, last_progress=COALESCE(?, last_progress),
                     first_progress=COALESCE(first_progress, ?), raw_activity=?
                     WHERE activity_uuid=? AND status='Active'""", (now, progress, progress, raw, uid))


def end(uid, progress=None, how="ended"):
    now = iso(utcnow())
    with db_lock, db() as c:
        row = c.execute("SELECT id,status,last_progress,plex_username,title FROM downloads WHERE activity_uuid=?", (uid,)).fetchone()
        if not row or row["status"] != "Active":
            return
        p = progress if progress is not None else row["last_progress"]
        if p is not None and p >= DONE_AT:
            status = "Completed"
        else:
            status = "Stopped" if how in ("ended", "vanished") else "Unknown"
        c.execute("""UPDATE downloads SET status=?, last_progress=COALESCE(?, last_progress), ended_at=?, completed_at=?,
                     last_seen_at=CASE WHEN ?='ended' THEN ? ELSE last_seen_at END WHERE id=?""",
                  (status, p, now, now if status == "Completed" else None, how, now, row["id"]))
    log.info("Download %s: %s -> %s (%s%%)", status.lower(), row["plex_username"], row["title"], "?" if p is None else int(p))


def reconcile(active_uuids, how):
    """Close any Active row that Plex no longer reports."""
    with db() as c:
        rows = c.execute("SELECT activity_uuid,last_seen_at FROM downloads WHERE status='Active'").fetchall()
    cutoff = utcnow() - dt.timedelta(seconds=max(15, C["poll_s"] * 3))
    for r in rows:
        if r["activity_uuid"] in active_uuids:
            continue
        if how == "startup" or parse_iso(r["last_seen_at"]) < cutoff:
            end(r["activity_uuid"], how=how)


# ---------------------------------------------------------------- background loops
def feed_loop():
    backoff = 2
    while True:
        if not TOKEN or not C["plex_url"]:
            time.sleep(2)
            continue
        try:
            with plex_request("/:/eventsource/notifications?filters=activity", timeout=90) as r:
                state["feed_connected"] = True
                log.info("Connected to the Plex notification feed")
                backoff = 2
                event = None
                for line in r:
                    line = line.decode("utf-8", "replace").strip()
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:") and event == "activity":
                        try:
                            n = json.loads(line[5:]).get("ActivityNotification", {})
                        except ValueError:
                            log.warning("Unexpected feed payload: %s", scrub(line)[:300])
                            continue
                        a = n.get("Activity") or {}
                        if is_download(a):
                            state["last_event"] = iso(utcnow())
                            seen(a, n.get("event") or "updated")
        except Exception as e:
            if not isinstance(e, TimeoutError):  # an idle feed times out after 90 s; just reconnect
                note_error("feed", e)
        state["feed_connected"] = False
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def poll_once(first=False):
    acts = plex_json("/activities").get("MediaContainer", {}).get("Activity", []) or []
    if not state["plex_connected"]:
        log.info("Plex connection OK (%s)", C["plex_url"])
    state.update(plex_connected=True, auth_error=False, last_error=None, last_poll=iso(utcnow()))
    downloads = [a for a in acts if is_download(a)]
    for a in downloads:
        seen(a)
    reconcile({a["uuid"] for a in downloads}, "startup" if first else "vanished")
    refresh_accounts()


def poll_loop():
    first = True
    while True:
        if TOKEN and C["plex_url"]:
            try:
                poll_once(first)
                first = False
            except Exception as e:
                note_error("poll", e)
        time.sleep(C["poll_s"])


def snapshot_loop():
    """Consistent daily copy of the database (SQLite backup API) at 01:30 local time, for file-level backups:
    a live WAL database copied mid-write can be inconsistent. Written to <data>/snapshot/."""
    target = os.path.join(os.path.dirname(C["db_path"]), "snapshot", os.path.basename(C["db_path"]))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    last = None
    while True:
        now = dt.datetime.now(C["tz"])
        if (last is None and not os.path.exists(target)) or (now.strftime("%H:%M") >= "01:30" and last != now.date() and now.hour < 2):
            try:
                with db() as src:
                    dst = sqlite3.connect(target + ".tmp")
                    src.backup(dst)
                    dst.close()
                os.replace(target + ".tmp", target)
                log.info("Database snapshot written: %s", target)
            except Exception as e:
                log.error("Database snapshot failed: %s", e)
            last = now.date()
        time.sleep(60)


# ---------------------------------------------------------------- plex.tv sign-in (PIN / plex.tv/link)
def setup_start():
    """Ask plex.tv for a short link code. The user enters it at https://plex.tv/link."""
    with setup_lock:
        pin = http_json(f"{PLEX_TV}/api/v2/pins?strong=false", method="POST")
        setup.update(pin_id=pin["id"], code=pin["code"], error=None, last_check=0.0,
                     expires=time.time() + int(pin.get("expiresIn") or 900))
        log.info("plex.tv link code created; waiting for the user to enter it at plex.tv/link")
        return {"code": pin["code"], "expires_in": int(setup["expires"] - time.time())}


def pick_server_token(account_token):
    """Prefer the server's own access token for PLEX_URL over the account token (narrower scope)."""
    machine = None
    try:
        with urllib.request.urlopen(urllib.request.Request(C["plex_url"] + "/identity", headers={"Accept": "application/json"}),
                                    timeout=10) as r:
            machine = json.loads(r.read())["MediaContainer"].get("machineIdentifier")
    except Exception as e:
        raise RuntimeError(f"Can't reach Plex at {C['plex_url']} ({type(e).__name__}). Check PLEX_URL.")
    try:
        resources = http_json(f"{PLEX_TV}/api/v2/resources?includeHttps=1", token=account_token)
    except Exception:
        resources = []
    for res in resources or []:
        if res.get("clientIdentifier") == machine and res.get("accessToken"):
            return res["accessToken"], res.get("name"), bool(res.get("owned"))
    return account_token, None, None


def setup_check():
    """Poll plex.tv (at most every 2 s) until the code is linked; then pick, verify and save the token."""
    global TOKEN
    with setup_lock:
        if not setup["pin_id"]:
            return {"state": "idle"}
        if time.time() > setup["expires"]:
            setup.update(pin_id=None, code=None)
            return {"state": "expired"}
        if time.time() - setup["last_check"] < 2:
            return {"state": "waiting", "code": setup["code"]}
        setup["last_check"] = time.time()
        pin = http_json(f"{PLEX_TV}/api/v2/pins/{int(setup['pin_id'])}")
        account_token = pin.get("authToken")
        if not account_token:
            return {"state": "waiting", "code": setup["code"]}
        setup.update(pin_id=None, code=None)
        try:
            token, name, owned = pick_server_token(account_token)
            if owned is False:
                setup["error"] = ("Signed in, but this Plex account does not own the server. Sign in with the "
                                  "server owner's account: only the owner can see everyone's downloads.")
                return {"state": "error", "error": setup["error"]}
            # /activities is admin-only, so this proves the token can see download activity
            req = urllib.request.Request(C["plex_url"] + "/activities", headers=plex_headers(token))
            urllib.request.urlopen(req, timeout=10).close()
        except urllib.error.HTTPError as e:
            e.close()
            setup["error"] = (f"Signed in, but the Plex server at {C['plex_url']} refused this account (HTTP {e.code}). "
                              "Sign in with the account that owns the server.")
            return {"state": "error", "error": setup["error"]}
        except Exception as e:
            setup["error"] = str(e) if isinstance(e, RuntimeError) else f"Could not verify with Plex ({type(e).__name__})."
            return {"state": "error", "error": setup["error"]}
        save_token(token)
        TOKEN = token
        state.update(auth_error=False, server_name=name)
        log.info("Signed in with plex.tv%s; token saved to %s", f" (server {name})" if name else "", C["token_file"])
        return {"state": "linked", "server": name}


# ---------------------------------------------------------------- queries
def csv_cell(v):
    """CSV value that spreadsheets show as text: text starting with = + - @ tab or CR gets a leading apostrophe
    (formula/CSV injection; user names and titles come from Plex). Numbers are left alone."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def row_out(r):
    d = dict(r)
    md = {"type": d["media_type"], "title": d["title"], "grandparent_title": d["grandparent_title"], "year": d["year"]}
    doc = {}
    if d.get("metadata_id"):
        with db() as c:
            m = c.execute("SELECT doc FROM metadata_cache WHERE metadata_id=?", (d["metadata_id"],)).fetchone()
        doc = json.loads(m["doc"]) if m else {}
    md["_doc"] = doc
    d["display_title"] = display_title_of(md, d["title"])
    part = next((p for p in doc.get("parts", []) if str(p.get("id")) == str(d.get("media_part_id"))), {}) or {}
    d["library"] = doc.get("librarySectionTitle")
    d["resolution"] = part.get("resolution")
    d["container"] = part.get("container")
    d["duration_min"] = round(part["duration"] / 60000) if part.get("duration") else None
    d["size_gb"] = round(d["bytes_estimated"] / 1e9, 2) if d.get("bytes_estimated") else None
    d["season"] = doc.get("parentIndex")
    d["episode"] = doc.get("index")
    d["started_local"] = local(d["started_at"])
    d["completed_local"] = local(d["completed_at"])
    return d


def where(q):
    """Parameterised WHERE clause from query-string filters."""
    clauses, args = [], []

    def one(k):
        return (q.get(k) or [""])[0].strip()
    if one("user"):
        clauses.append("plex_username = ?"); args.append(one("user"))
    if one("type"):
        clauses.append("media_type = ?"); args.append(one("type"))
    if one("status"):
        clauses.append("status = ?"); args.append(one("status"))
    if one("q"):
        like = "%" + one("q").replace("%", "").replace("_", "") + "%"
        clauses.append("(title LIKE ? OR grandparent_title LIKE ? OR parent_title LIKE ? OR plex_username LIKE ?)")
        args += [like] * 4
    for key, op in (("from", ">="), ("to", "<")):
        v = one(key)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            day = dt.datetime.fromisoformat(v).replace(tzinfo=C["tz"])
            if key == "to":
                day += dt.timedelta(days=1)  # inclusive end date
            clauses.append(f"started_at {op} ?"); args.append(iso(day.astimezone(dt.timezone.utc)))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def day_start(days_ago=0):
    t = dt.datetime.now(C["tz"]).replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=days_ago)
    return iso(t.astimezone(dt.timezone.utc))


def summary():
    with db() as c:
        def count(since):
            return c.execute("SELECT COUNT(*) FROM downloads WHERE started_at >= ?", (since,)).fetchone()[0]
        out = {"today": count(day_start(0)), "d7": count(day_start(6)), "d30": count(day_start(29)),
               "all": c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0],
               "first": c.execute("SELECT MIN(started_at) FROM downloads").fetchone()[0]}
        out["gb30"] = round((c.execute("SELECT COALESCE(SUM(bytes_estimated),0) FROM downloads WHERE started_at >= ? "
                                       "AND status != 'Unknown'", (day_start(29),)).fetchone()[0]) / 1e9, 1)
        top = c.execute("SELECT plex_username, COUNT(*) n FROM downloads WHERE started_at >= ? GROUP BY plex_username "
                        "ORDER BY n DESC LIMIT 1", (day_start(29),)).fetchone()
        out["top30"] = {"user": top[0], "n": top[1]} if top else None
        out["active"] = [row_out(r) for r in c.execute("SELECT * FROM downloads WHERE status='Active' ORDER BY started_at DESC")]
        out["users"] = [dict(r) for r in c.execute(
            """SELECT plex_username user, COUNT(*) n,
                      SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END) completed,
                      ROUND(COALESCE(SUM(bytes_estimated),0)/1e9,1) gb, MAX(started_at) last
               FROM downloads GROUP BY plex_username ORDER BY n DESC""")]
        out["filters"] = {
            "users": [r[0] for r in c.execute("SELECT DISTINCT plex_username FROM downloads WHERE plex_username IS NOT NULL ORDER BY 1 COLLATE NOCASE")],
            "types": [r[0] for r in c.execute("SELECT DISTINCT media_type FROM downloads WHERE media_type IS NOT NULL ORDER BY 1")],
        }
    for a in out["active"]:
        a.pop("raw_activity", None)
    out["health"] = health()
    out["timezone"] = C["tzname"]
    return out


def health():
    with db() as c:
        active = c.execute("SELECT COUNT(*) FROM downloads WHERE status='Active'").fetchone()[0]
    if C["demo"]:
        ok = True
    else:
        ok = bool(TOKEN) and state["plex_connected"] and not state["auth_error"]
    return {"status": "ok" if ok else ("setup" if not configured() else "degraded"),
            "summary": "downloadwatch healthy" if ok else "downloadwatch not healthy",
            "version": __version__, "demo": C["demo"], "configured": configured(),
            "plex_connected": state["plex_connected"] or C["demo"], "auth_error": state["auth_error"],
            "feed_connected": state["feed_connected"] or C["demo"], "last_poll": state["last_poll"],
            "last_download_event": state["last_event"], "poll_interval": C["poll_s"], "active_downloads": active,
            "error": state["last_error"], "started": state["started"]}


def public_config():
    if C["auth_disabled"]:
        auth = "proxy"
    elif C["auth_password"]:
        auth = "password"
    else:
        auth = "none"
    return {"product": PRODUCT, "title": C["title"], "version": __version__, "demo": C["demo"],
            "configured": configured(), "plex_url_set": bool(C["plex_url"]), "plex_url": C["plex_url"],
            "token_from_env": bool(C["token_env"]), "auth": auth, "can_relink": can_setup(), "nav": C["nav"],
            "timezone": C["tzname"], "server_name": state.get("server_name")}


def can_setup():
    """Linking is always allowed before a token exists. Re-linking an already configured install is allowed only
    when the dashboard is protected (built-in password or a declared proxy login) and the token is not from env."""
    if C["demo"] or C["token_env"] or not C["plex_url"]:
        return False
    if not TOKEN:
        return True
    return bool(C["auth_password"]) or C["auth_disabled"]


# ---------------------------------------------------------------- demo mode
DEMO_USERS = ["Alex", "Sam", "Jordan", "Priya", "Marcus", "Lena"]
DEMO_MOVIES = [("Night of the Living Dead", 1968, 1.4), ("His Girl Friday", 1940, 1.9), ("Charade", 1963, 2.3),
               ("The General", 1926, 1.2), ("Nosferatu", 1922, 1.1), ("Detour", 1945, 1.0),
               ("The Little Shop of Horrors", 1960, 1.3), ("Plan 9 from Outer Space", 1957, 1.5),
               ("Carnival of Souls", 1962, 1.6), ("D.O.A.", 1949, 1.7), ("The Kid", 1921, 0.9),
               ("Sherlock Jr.", 1924, 0.8), ("Metropolis", 1927, 3.1), ("The Last Man on Earth", 1964, 1.8)]
DEMO_SHOWS = [("Harbor Lights", ["Low Tide", "The Keeper", "Fog Signal", "Salt", "Undertow", "Lanterns"]),
              ("Orbit Street", ["Pilot", "Second Moon", "Retrograde", "Apogee", "Burn", "Splashdown"]),
              ("The Bakers of Elm Row", ["Proof", "Sourdough", "Crumb", "Rise", "Scald", "Knead"])]


def demo_seed(now=None):
    """Fill an empty database with ~45 days of made-up downloads (public-domain films and fictional shows)."""
    rng = random.Random(42)
    now = now or utcnow()
    with db_lock, db() as c:
        if c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]:
            return
        mid = 1000
        for i in range(140):
            mid += 1
            started = now - dt.timedelta(days=rng.uniform(0.05, 45), minutes=rng.randint(0, 59))
            user = rng.choices(DEMO_USERS, weights=[9, 6, 4, 3, 2, 1])[0]
            if rng.random() < 0.45:
                title, year, gb = rng.choice(DEMO_MOVIES)
                doc = {"type": "movie", "title": title, "year": year, "librarySectionTitle": "Movies",
                       "parts": [{"id": mid, "size": int(gb * 1e9), "container": "mp4", "resolution": "1080", "duration": 5_400_000}]}
            else:
                show, eps = rng.choice(DEMO_SHOWS)
                season, ep = rng.randint(1, 3), rng.randint(1, len(eps))
                gb = rng.uniform(0.4, 1.4)
                doc = {"type": "episode", "title": eps[ep - 1], "grandparentTitle": show, "parentTitle": f"Season {season}",
                       "parentIndex": season, "index": ep, "librarySectionTitle": "TV Shows",
                       "parts": [{"id": mid, "size": int(gb * 1e9), "container": "mkv", "resolution": "720", "duration": 2_700_000}]}
            roll = rng.random()
            status = "Completed" if roll < 0.86 else ("Stopped" if roll < 0.97 else "Unknown")
            progress = 100.0 if status == "Completed" else round(rng.uniform(5, 90), 1)
            ended = started + dt.timedelta(minutes=rng.randint(2, 25))
            c.execute("INSERT OR REPLACE INTO metadata_cache(metadata_id,fetched_at,doc) VALUES(?,?,?)", (mid, iso(now), json.dumps(doc)))
            c.execute("""INSERT INTO downloads(activity_uuid,plex_user_id,plex_username,media_type,metadata_id,media_part_id,title,
                         parent_title,grandparent_title,year,started_at,last_seen_at,completed_at,ended_at,first_progress,
                         last_progress,status,bytes_estimated,raw_activity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (f"demo-{i}", str(DEMO_USERS.index(user) + 1), user, doc["type"], mid, mid, doc["title"],
                       doc.get("parentTitle"), doc.get("grandparentTitle"), doc.get("year"), iso(started), iso(ended),
                       iso(ended) if status == "Completed" else None, iso(ended), 0.0, progress, status,
                       doc["parts"][0]["size"], json.dumps({"demo": True})))
    log.info("Demo mode: seeded sample history")


def demo_loop():
    """Simulate live downloads so the dashboard moves: one or two at a time, progressing to completion."""
    rng = random.Random()
    n = 0
    while True:
        with db() as c:
            active = c.execute("SELECT activity_uuid,last_progress FROM downloads WHERE status='Active'").fetchall()
        if len(active) < 2 and rng.random() < 0.5:
            n += 1
            with db() as c:
                mid = c.execute("SELECT metadata_id FROM metadata_cache ORDER BY RANDOM() LIMIT 1").fetchone()[0]
            md = metadata(mid, mid)
            # userID 0 is never in the accounts table, so the name comes from the "Media download by" title
            seen({"uuid": f"demo-live-{int(time.time())}-{n}", "type": "media.download", "userID": 0,
                  "title": f"Media download by {rng.choice(DEMO_USERS)}", "subtitle": md.get("title"), "progress": 0,
                  "Context": {"deviceID": "", "metadataID": str(mid), "partID": str(mid)}}, "started")
        for a in active:
            p = min(100.0, (a["last_progress"] or 0) + rng.uniform(3, 12))
            seen({"uuid": a["activity_uuid"], "type": "media.download", "progress": p}, "ended" if p >= 100 else "updated")
        state.update(last_poll=iso(utcnow()))
        time.sleep(3)


# ---------------------------------------------------------------- web
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def login_ok(header):
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user.encode(), C["auth_user"].encode()) & hmac.compare_digest(pw.encode(), C["auth_password"].encode())


class Handler(BaseHTTPRequestHandler):
    server_version = "DownloadWatch"
    sys_version = ""

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def authorised(self):
        if not C["auth_password"] or C["auth_disabled"]:
            return True
        now = time.time()
        failures[:] = [t for t in failures if now - t < 60]
        if login_ok(self.headers.get("Authorization")):
            return True
        if self.headers.get("Authorization"):
            failures.append(now)
            if len(failures) > 10:
                time.sleep(2)  # slow down password guessing
        self.send(401, {"error": "login required"}, extra={"WWW-Authenticate": 'Basic realm="DownloadWatch", charset="UTF-8"'})
        return False

    def same_origin(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        return self.headers.get("Content-Type", "").startswith("application/json") and origin in (f"http://{host}", f"https://{host}")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if path == "/health":  # no login: for container health checks and uptime monitors; no personal data
            h = health()
            return self.send(200 if h["status"] == "ok" else 503,
                             {k: h[k] for k in ("status", "summary", "version", "configured", "plex_connected",
                                                "feed_connected", "last_poll", "active_downloads")})
        if not self.authorised():
            return
        try:
            if path == "/api/config":
                return self.send(200, public_config())
            if path == "/api/summary":
                return self.send(200, summary())
            if path == "/api/downloads":
                w, args = where(q)
                limit = min(int((q.get("limit") or ["100"])[0]), 1000)
                offset = max(int((q.get("offset") or ["0"])[0]), 0)
                with db() as c:
                    total = c.execute("SELECT COUNT(*) FROM downloads" + w, args).fetchone()[0]
                    rows = c.execute("SELECT * FROM downloads" + w + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                                     args + [limit, offset]).fetchall()
                out = [row_out(r) for r in rows]
                for r in out:
                    r.pop("raw_activity", None)
                return self.send(200, {"total": total, "rows": out})
            m = re.fullmatch(r"/api/downloads/(\d+)", path)
            if m:
                with db() as c:
                    r = c.execute("SELECT * FROM downloads WHERE id=?", (int(m.group(1)),)).fetchone()
                return self.send(200, row_out(r)) if r else self.send(404, {"error": "not found"})
            if path == "/api/export.csv":
                w, args = where(q)
                with db() as c:
                    rows = [row_out(r) for r in c.execute("SELECT * FROM downloads" + w + " ORDER BY started_at DESC", args)]
                buf = io.StringIO()
                wr = csv.writer(buf)
                wr.writerow([h for _, h in EXPORT_COLS])
                for r in rows:
                    wr.writerow([csv_cell(r.get(k)) for k, _ in EXPORT_COLS])
                name = "downloadwatch-" + dt.datetime.now(C["tz"]).strftime("%Y%m%d-%H%M") + ".csv"
                return self.send(200, buf.getvalue(), "text/csv; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/api/setup/status":
                return self.send(200, setup_check())
            fname = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.realpath(os.path.join(STATIC, fname))
            if full.startswith(STATIC + os.sep) and os.path.isfile(full):
                with open(full, "rb") as f:
                    return self.send(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
            return self.send(404, {"error": "not found"})
        except (ValueError, KeyError) as e:
            return self.send(400, {"error": f"bad request: {type(e).__name__}"})
        except Exception:
            log.exception("Request failed: %s", path)
            return self.send(500, {"error": "internal error"})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.authorised():
            return
        if not self.same_origin():
            return self.send(403, {"error": "Request refused (must come from the DownloadWatch page)."})
        try:
            if path == "/api/setup/start":
                if not can_setup():
                    return self.send(403, {"error": "Sign-in is not available (already set up without a dashboard "
                                                    "password, token set by PLEX_TOKEN, PLEX_URL missing, or demo mode)."})
                return self.send(200, setup_start())
            return self.send(404, {"error": "not found"})
        except urllib.error.URLError as e:
            return self.send(502, {"error": f"Could not reach plex.tv ({type(e).__name__})."})
        except Exception:
            log.exception("Request failed: %s", path)
            return self.send(500, {"error": "internal error"})


# ---------------------------------------------------------------- main
def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(sys.stdout)] if sys.stdout else []  # no console under pythonw/Task Scheduler
    if C["log_file"]:
        os.makedirs(os.path.dirname(C["log_file"]) or ".", exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(C["log_file"], maxBytes=5_000_000, backupCount=5))
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def main():
    global TOKEN
    config_file = None
    args = sys.argv[1:]
    if args[:1] == ["--version"]:
        print(f"{PRODUCT} {__version__}")
        return
    if args[:1] == ["--config"] and len(args) > 1:
        config_file = args[1]
    elif args:
        sys.exit("usage: downloadwatch.py [--config PATH] | [--version]")
    C.update(load_config(config_file=config_file))
    setup_logging()
    state["started"] = iso(utcnow())
    if C["demo"]:
        C["db_path"] = os.path.join(C["data_dir"], "demo.db")
    init_db()
    log.info("%s %s starting on %s:%d (timezone %s)", PRODUCT, __version__, C["host"], C["port"], C["tzname"])
    if C["demo"]:
        log.info("DEMO MODE: sample data only, no Plex connection")
        demo_seed()
        threading.Thread(target=demo_loop, daemon=True, name="demo").start()
    else:
        TOKEN = load_token()
        if not C["plex_url"]:
            log.error("PLEX_URL is not set (e.g. http://192.168.1.10:32400). The dashboard will say so.")
        elif not TOKEN:
            log.warning("No Plex token yet: open the dashboard and click 'Sign in with Plex'.")
        if not C["auth_password"] and not C["auth_disabled"]:
            log.warning("No AUTH_PASSWORD set: anyone who can reach this port can see the download history.")
        if TOKEN and C["plex_url"]:
            refresh_accounts(force=True)
        threading.Thread(target=poll_loop, daemon=True, name="poll").start()
        threading.Thread(target=feed_loop, daemon=True, name="feed").start()
    if C["snapshot"]:
        threading.Thread(target=snapshot_loop, daemon=True, name="snapshot").start()
    ThreadingHTTPServer((C["host"], C["port"]), Handler).serve_forever()


if __name__ == "__main__":
    main()
