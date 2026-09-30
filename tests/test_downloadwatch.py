"""DownloadWatch tests. Standard library only: python -m unittest discover -s tests -v"""
import base64
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import downloadwatch as dw  # noqa: E402

MOVIE = {"MediaContainer": {"Metadata": [{"type": "movie", "title": "Charade", "year": 1963, "librarySectionTitle": "Movies",
                                          "Media": [{"videoResolution": "1080", "container": "mp4",
                                                     "Part": [{"id": 20, "size": 2_300_000_000, "duration": 6_780_000}]}]}]}}
EPISODE = {"MediaContainer": {"Metadata": [{"type": "episode", "title": "Fog Signal", "grandparentTitle": "Harbor Lights",
                                            "parentIndex": 1, "index": 3, "librarySectionTitle": "TV Shows",
                                            "Media": [{"Part": [{"id": 30, "size": 900_000_000}]}]}]}}
ACCOUNTS = {"MediaContainer": {"Account": [{"id": 1, "name": "owner"}, {"id": 7, "name": "Alex"}]}}


def fake_plex(path):
    if path == "/accounts":
        return ACCOUNTS
    if path.endswith("/10"):
        return MOVIE
    if path.endswith("/11"):
        return EPISODE
    raise RuntimeError("unexpected " + path)


def act(uid, user, mid, part, progress):
    return {"uuid": uid, "type": "media.download", "userID": user, "title": "", "subtitle": "x", "progress": progress,
            "Context": {"deviceID": "", "metadataID": str(mid), "partID": str(part)}}


class Base(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"DATA_DIR": self.tmp.name, "PLEX_URL": "http://plex.test:32400", "TIMEZONE": "America/Denver"}
        env.update(self.env)
        dw.C.clear()
        dw.C.update(dw.load_config(env))
        dw.reset_state()
        dw.TOKEN = "secret-token-abcdef123456"
        dw.accounts_at[0] = 0.0
        dw.failures.clear()
        dw.init_db()
        p = mock.patch.object(dw, "plex_json", side_effect=fake_plex)
        p.start()
        self.addCleanup(p.stop)
        dw.log.disabled = True

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        with dw.db() as c:
            return {r["activity_uuid"]: dict(r) for r in c.execute("SELECT * FROM downloads")}


class StatusRules(Base):
    def test_lifecycle_and_dedupe(self):
        dw.seen(act("a", 1, 10, 20, -1), "started")
        for p in (5, 40, 40, 80):
            dw.seen(act("a", 1, 10, 20, p))
        r = self.rows()
        self.assertEqual(len(r), 1)
        self.assertEqual(r["a"]["status"], "Active")
        self.assertEqual(r["a"]["last_progress"], 80)
        self.assertEqual(r["a"]["plex_username"], "owner")
        self.assertEqual(r["a"]["title"], "Charade")
        self.assertEqual(r["a"]["bytes_estimated"], 2_300_000_000)
        dw.seen(act("a", 1, 10, 20, 100), "ended")
        r = self.rows()["a"]
        self.assertEqual(r["status"], "Completed")
        self.assertIsNotNone(r["completed_at"])

    def test_ended_below_99_is_stopped(self):
        dw.seen(act("b", 7, 11, 30, 30), "started")
        dw.seen(act("b", 7, 11, 30, 45), "ended")
        r = self.rows()["b"]
        self.assertEqual(r["status"], "Stopped")
        self.assertIsNone(r["completed_at"])
        self.assertEqual(r["plex_username"], "Alex")

    def test_vanished(self):
        dw.seen(act("c", 7, 11, 30, 99.5))
        dw.seen(act("d", 7, 11, 30, 20))
        with dw.db() as c:
            c.execute("UPDATE downloads SET last_seen_at='2020-01-01T00:00:00Z'")
        dw.reconcile(set(), "vanished")
        r = self.rows()
        self.assertEqual(r["c"]["status"], "Completed")
        self.assertEqual(r["d"]["status"], "Stopped")

    def test_recent_active_not_closed_by_poll_gap(self):
        dw.seen(act("e", 7, 11, 30, 20))
        dw.reconcile(set(), "vanished")
        self.assertEqual(self.rows()["e"]["status"], "Active")

    def test_restart_marks_unknown(self):
        dw.seen(act("f", 1, 11, 30, 50))
        dw.reconcile(set(), "startup")
        self.assertEqual(self.rows()["f"]["status"], "Unknown")

    def test_late_update_does_not_reopen(self):
        dw.seen(act("g", 1, 10, 20, 100), "ended")
        dw.seen(act("g", 1, 10, 20, 100))
        self.assertEqual(self.rows()["g"]["status"], "Completed")

    def test_episode_title_and_row_out(self):
        dw.seen(act("h", 7, 11, 30, 10))
        with dw.db() as c:
            out = dw.row_out(c.execute("SELECT * FROM downloads").fetchone())
        self.assertEqual(out["display_title"], "Harbor Lights — S01E03 — Fog Signal")
        self.assertEqual(out["season"], 1)
        self.assertEqual(out["episode"], 3)

    def test_name_fallback_from_title(self):
        a = act("i", 99, 10, 20, 10)
        a["title"] = "Media download by guest"
        dw.seen(a)
        self.assertEqual(self.rows()["i"]["plex_username"], "guest")

    def test_token_never_stored(self):
        a = act("j", 1, 10, 20, 10)
        a["extra"] = f"url?X-Plex-Token={dw.TOKEN}"
        dw.seen(a)
        self.assertNotIn(dw.TOKEN, self.rows()["j"]["raw_activity"])


class Helpers(Base):
    def test_csv_cell(self):
        cases = [("=HYPERLINK(1)", "'=HYPERLINK(1)"), ("+1", "'+1"), ("-cmd", "'-cmd"), ("@SUM(A1)", "'@SUM(A1)"),
                 ("\tx", "'\tx"), ("\rx", "'\rx"), ("Charade (1963)", "Charade (1963)"), (-5, -5), (2.5, 2.5),
                 (None, ""), ("", "")]
        for value, expected in cases:
            self.assertEqual(dw.csv_cell(value), expected, value)

    def test_where_is_parameterised(self):
        w, args = dw.where({"q": ["x' OR 1=1 --"], "user": ["Alex"], "from": ["2026-01-02"], "to": ["2026-01-03"]})
        self.assertNotIn("OR 1=1", w)
        self.assertIn("x' OR 1=1 --", args[1])
        self.assertEqual(args[-2], "2026-01-02T07:00:00Z")   # Denver midnight in UTC
        self.assertEqual(args[-1], "2026-01-04T07:00:00Z")   # inclusive end date
        self.assertEqual(dw.where({"from": ["not-a-date"]}), ("", []))

    def test_nav_links_only_safe_urls(self):
        c = dw.load_config({"NAV_LINKS": "Home=/,Grafana=http://g:3000,Bad=javascript:alert(1),Empty="})
        self.assertEqual([n["label"] for n in c["nav"]], ["Home", "Grafana"])

    def test_config_file(self):
        path = os.path.join(self.tmp.name, "downloadwatch.env")
        with open(path, "w", encoding="utf-8-sig") as f:   # with BOM, as Windows editors often write
            f.write('# comment\nPLEX_URL=http://10.0.0.5:32400\nAUTH_PASSWORD="p w=1"\nTIMEZONE=Europe/London\nbroken line\n')
        c = dw.load_config({"TIMEZONE": "Asia/Tokyo"}, config_file=path)
        self.assertEqual(c["plex_url"], "http://10.0.0.5:32400")
        self.assertEqual(c["auth_password"], "p w=1")
        self.assertEqual(c["tzname"], "Asia/Tokyo")          # environment beats the file

    def test_bad_timezone_falls_back_to_utc(self):
        self.assertEqual(dw.load_config({"TIMEZONE": "Mars/Base"})["tzname"], "UTC")

    def test_login(self):
        dw.C.update(auth_user="admin", auth_password="pw-123456789")
        ok = "Basic " + base64.b64encode(b"admin:pw-123456789").decode()
        bad = "Basic " + base64.b64encode(b"admin:nope").decode()
        self.assertTrue(dw.login_ok(ok))
        self.assertFalse(dw.login_ok(bad))
        self.assertFalse(dw.login_ok("Basic !!!"))
        self.assertFalse(dw.login_ok(None))

    def test_can_setup_matrix(self):
        dw.TOKEN = ""
        self.assertTrue(dw.can_setup())                     # first run
        dw.TOKEN = "t"
        self.assertFalse(dw.can_setup())                    # configured, dashboard open to anyone: no re-link
        dw.C["auth_password"] = "pw"
        self.assertTrue(dw.can_setup())                     # configured and protected
        dw.C["token_env"] = "from-env"
        self.assertFalse(dw.can_setup())                    # token managed by env
        dw.C.update(token_env="", plex_url="")
        self.assertFalse(dw.can_setup())                    # nowhere to connect

    def test_demo_seed(self):
        dw.C["demo"] = True
        dw.demo_seed()
        s = dw.summary()
        self.assertGreater(s["all"], 100)
        self.assertEqual(len(s["users"]), 6)
        dw.demo_seed()                                      # idempotent
        self.assertEqual(dw.summary()["all"], s["all"])


class SignIn(Base):
    def setUp(self):
        super().setUp()
        dw.TOKEN = ""

    def fake_http(self, pin_token=None, owned=True, machine="m1"):
        def http_json(url, token=None, method="GET", timeout=10):
            if url.endswith("/api/v2/pins?strong=false"):
                return {"id": 42, "code": "AB12", "expiresIn": 900}
            if url.endswith("/api/v2/pins/42"):
                return {"id": 42, "authToken": pin_token}
            if "/api/v2/resources" in url:
                return [{"clientIdentifier": machine, "accessToken": "server-token-xyz", "name": "Home", "owned": owned}]
            raise AssertionError(url)
        return http_json

    def fake_urlopen(self, fail=None):
        class R:
            def __init__(self, body=b"{}"):
                self.body = body

            def read(self):
                return self.body

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def urlopen(req, timeout=10):
            url = req.full_url
            if url.endswith("/identity"):
                return R(json.dumps({"MediaContainer": {"machineIdentifier": "m1"}}).encode())
            if url.endswith("/activities"):
                if fail:
                    raise dw.urllib.error.HTTPError(url, fail, "no", {}, io.BytesIO(b""))
                self.used_token = req.headers.get("X-plex-token")
                return R()
            raise AssertionError(url)
        return urlopen

    def run_flow(self, **kw):
        with mock.patch.object(dw, "http_json", self.fake_http(**{k: v for k, v in kw.items() if k != "fail"})), \
             mock.patch.object(dw.urllib.request, "urlopen", self.fake_urlopen(kw.get("fail"))):
            self.assertEqual(dw.setup_start()["code"], "AB12")
            dw.setup["last_check"] = 0
            return dw.setup_check()

    def test_waiting(self):
        self.assertEqual(self.run_flow(pin_token=None)["state"], "waiting")
        self.assertEqual(dw.TOKEN, "")

    def test_linked_uses_server_token_and_saves_it(self):
        r = self.run_flow(pin_token="account-token")
        self.assertEqual(r["state"], "linked")
        self.assertEqual(dw.TOKEN, "server-token-xyz")
        self.assertEqual(self.used_token, "server-token-xyz")
        self.assertEqual(dw.read_text(dw.C["token_file"]), "server-token-xyz")
        if os.name != "nt":  # Windows uses folder ACLs (set by the installer) instead of mode bits
            self.assertEqual(os.stat(dw.C["token_file"]).st_mode & 0o777, 0o600)

    def test_not_owner_refused(self):
        r = self.run_flow(pin_token="account-token", owned=False)
        self.assertEqual(r["state"], "error")
        self.assertIn("does not own", r["error"])
        self.assertEqual(dw.TOKEN, "")

    def test_server_refuses(self):
        r = self.run_flow(pin_token="account-token", fail=401)
        self.assertEqual(r["state"], "error")
        self.assertFalse(os.path.exists(dw.C["token_file"]))

    def test_expired(self):
        with mock.patch.object(dw, "http_json", self.fake_http()):
            dw.setup_start()
        dw.setup["expires"] = time.time() - 1
        self.assertEqual(dw.setup_check()["state"], "expired")


class Web(Base):
    env = {"AUTH_PASSWORD": "correct-horse-battery"}

    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), dw.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]
        dw.seen(act("w", 7, 10, 20, 100), "ended")

    def req(self, method, path, auth=True, headers=None, body=None):
        h = dict(headers or {})
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(b"admin:correct-horse-battery").decode()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        c.close()
        return out

    def test_login_required(self):
        status, headers, _ = self.req("GET", "/api/summary", auth=False)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.req("GET", "/", auth=False)[0], 401)

    def test_health_is_public_and_minimal(self):
        status, _, body = self.req("GET", "/health", auth=False)
        doc = json.loads(body)
        self.assertIn(status, (200, 503))
        self.assertNotIn("error", doc)

    def test_dashboard_and_api(self):
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"DownloadWatch", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        doc = json.loads(self.req("GET", "/api/downloads?user=Alex")[2])
        self.assertEqual(doc["total"], 1)
        self.assertNotIn("raw_activity", doc["rows"][0])
        self.assertEqual(json.loads(self.req("GET", "/api/config")[2])["auth"], "password")

    def test_csv_export(self):
        status, headers, body = self.req("GET", "/api/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b"Charade (1963)", body)

    def test_path_traversal_blocked(self):
        self.assertEqual(self.req("GET", "/../downloadwatch.py")[0], 404)
        self.assertEqual(self.req("GET", "/%2e%2e/downloadwatch.py")[0], 404)

    def test_post_needs_same_origin_json(self):
        base = f"127.0.0.1:{self.port}"
        self.assertEqual(self.req("POST", "/api/setup/start", headers={"Origin": "http://evil.example",
                                                                       "Content-Type": "application/json"}, body="{}")[0], 403)
        self.assertEqual(self.req("POST", "/api/setup/start", headers={"Content-Type": "application/json"}, body="{}")[0], 403)
        self.assertEqual(self.req("POST", "/api/setup/start", headers={"Origin": f"http://{base}",
                                                                       "Content-Type": "text/plain"}, body="{}")[0], 403)
        with mock.patch.object(dw, "http_json", return_value={"id": 1, "code": "ZZ99", "expiresIn": 900}):
            status, _, body = self.req("POST", "/api/setup/start", headers={"Origin": f"http://{base}",
                                                                            "Content-Type": "application/json"}, body="{}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["code"], "ZZ99")


if __name__ == "__main__":
    unittest.main()
