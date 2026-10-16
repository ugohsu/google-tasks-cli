"""gtasks.py のテスト。Google の代わりにローカルの偽サーバを立て、gtasks.py を別プロセスで動かす。

    python3 -m unittest discover -s tests
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "gtasks.py"


class FakeGoogle(BaseHTTPRequestHandler):
    """トークン発行と Tasks API の、必要な分だけをまねる。"""

    def log_message(self, *args):
        pass

    def _send(self, code, data=None):
        raw = b"" if data is None else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        srv = self.server
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        req = {"method": self.command, "path": url.path,
               "query": urllib.parse.parse_qs(url.query),
               "auth": self.headers.get("Authorization")}
        if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            req["form"] = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        elif raw:
            req["body"] = json.loads(raw)
        srv.requests.append(req)

        if url.path == "/token":
            if srv.token_error:
                return self._send(400, {"error": srv.token_error})
            srv.issued += 1
            res = {"access_token": f"access-{srv.issued}", "expires_in": 3600}
            if req["form"]["grant_type"] == "authorization_code":
                res["refresh_token"] = "refresh-1"
            return self._send(200, res)
        if url.path == "/api/users/@me/lists":
            return self._send(200, {"items": [{"id": "L1", "title": "仕事"},
                                              {"id": "L2", "title": "家"}]})
        if url.path.startswith("/api/lists/"):
            parts = url.path.split("/")  # ['', 'api', 'lists', ID, 'tasks', (TASK)]
            list_id = parts[3]
            if len(parts) == 5 and self.command == "GET":
                pages = srv.tasks.get(list_id, [[]])
                page = int(req["query"].get("pageToken", ["0"])[0])
                res = {"items": pages[page]}
                if page + 1 < len(pages):
                    res["nextPageToken"] = str(page + 1)
                return self._send(200, res)
            if len(parts) == 5 and self.command == "POST":
                return self._send(200, dict(req["body"], id="NEW", status="needsAction",
                                            etag="x", selfLink="y"))
            if len(parts) == 6 and self.command == "PATCH":
                return self._send(200, dict(req["body"], id=parts[5], title="t"))
            if len(parts) == 6 and self.command == "DELETE":
                return self._send(204)
        self._send(404, {"error": "not found"})

    do_GET = do_POST = do_PATCH = do_DELETE = _handle


class GtasksTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), FakeGoogle)
        self.server.requests = []
        self.server.tasks = {}
        self.server.token_error = None
        self.server.issued = 0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "conf"
        self.conf.mkdir()
        (self.conf / "client_secret.json").write_text(json.dumps(
            {"installed": {"client_id": "cid", "client_secret": "csecret"}}))
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.env = dict(os.environ, GTASKS_CONFIG_DIR=str(self.conf),
                        GTASKS_AUTH_URL=f"{base}/auth", GTASKS_TOKEN_URL=f"{base}/token",
                        GTASKS_API_BASE=f"{base}/api")

    def run_cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                           capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def authorize(self):
        url = self.run_cli("auth-url").stdout.strip()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.run_cli("auth-finish", f"http://127.0.0.1:8765/?state={state}&code=abc&scope=x")
        self.server.requests.clear()

    def test_auth_flow(self):
        url = self.run_cli("auth-url").stdout.strip()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["scope"], ["https://www.googleapis.com/auth/tasks"])

        bad = self.run_cli("auth-finish", "http://127.0.0.1:8765/?state=wrong&code=abc", ok=False)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("state", bad.stderr)

        state = query["state"][0]
        self.run_cli("auth-finish", f"http://127.0.0.1:8765/?state={state}&code=abc")
        form = self.server.requests[-1]["form"]
        self.assertEqual(form["code"], "abc")
        self.assertEqual(form["client_secret"], "csecret")
        self.assertTrue(form["code_verifier"])

        token = self.conf / "token.json"
        self.assertEqual(json.loads(token.read_text())["refresh_token"], "refresh-1")
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertFalse((self.conf / "pending_auth.json").exists())

    def test_not_authorized(self):
        p = self.run_cli("lists", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("auth-url", p.stderr)

    def test_refresh_when_expired(self):
        self.authorize()
        token = self.conf / "token.json"
        saved = json.loads(token.read_text())
        saved["expires_at"] = time.time() - 10
        token.write_text(json.dumps(saved))
        self.run_cli("lists")
        refresh = self.server.requests[0]
        self.assertEqual(refresh["form"]["grant_type"], "refresh_token")
        self.assertEqual(refresh["form"]["refresh_token"], "refresh-1")
        # 新しいアクセストークンで API を呼び、リフレッシュトークンは残す
        self.assertNotEqual(self.server.requests[1]["auth"], f"Bearer {saved['access_token']}")
        self.assertEqual(json.loads(token.read_text())["refresh_token"], "refresh-1")

    def test_invalid_grant(self):
        self.authorize()
        token = self.conf / "token.json"
        saved = json.loads(token.read_text())
        saved["expires_at"] = 0
        token.write_text(json.dumps(saved))
        self.server.token_error = "invalid_grant"
        p = self.run_cli("lists", ok=False)
        self.assertIn("7 日", p.stderr)

    def test_list_pages_order_and_filters(self):
        self.authorize()
        self.server.tasks["L1"] = [
            [{"id": "c", "title": "子", "parent": "a", "position": "1", "status": "needsAction",
              "etag": "e"},
             {"id": "b", "title": "二番", "position": "2", "status": "needsAction",
              "due": "2026-10-20T00:00:00.000Z"}],
            [{"id": "a", "title": "一番", "position": "1", "status": "needsAction",
              "due": "2026-10-16T00:00:00.000Z"}],
        ]
        out = json.loads(self.run_cli("list", "-l", "仕事").stdout)
        self.assertEqual([t["id"] for t in out], ["a", "c", "b"])
        self.assertEqual(out[0]["due"], "2026-10-16")
        self.assertNotIn("etag", out[1])
        get = [r for r in self.server.requests if r["path"] == "/api/lists/L1/tasks"][0]
        self.assertEqual(get["query"]["showCompleted"], ["false"])

        out = json.loads(self.run_cli("list", "-l", "仕事", "--due-by", "2026-10-17").stdout)
        self.assertEqual([t["id"] for t in out], ["a"])

        self.server.requests.clear()
        self.run_cli("list", "-l", "L1", "--completed-since", "2026-10-16")
        get = self.server.requests[-1]
        self.assertEqual(get["query"]["showHidden"], ["true"])
        self.assertEqual(get["query"]["completedMin"], ["2026-10-16T00:00:00.000Z"])

        text = self.run_cli("list", "-l", "仕事", "--text").stdout
        self.assertIn("[ ] 一番  (期限 2026-10-16)  a", text)
        self.assertIn("    [ ] 子  c", text)

    def test_list_all_lists(self):
        self.authorize()
        self.server.tasks["L1"] = [[{"id": "a", "title": "x", "status": "needsAction"}]]
        self.server.tasks["L2"] = [[{"id": "b", "title": "y", "status": "needsAction"}]]
        out = json.loads(self.run_cli("list", "--all-lists").stdout)
        self.assertEqual([(t["id"], t["list"]) for t in out], [("a", "仕事"), ("b", "家")])

    def test_unknown_list(self):
        self.authorize()
        p = self.run_cli("list", "-l", "ない", ok=False)
        self.assertIn("仕事, 家", p.stderr)

    def test_add_update_done_delete(self):
        self.authorize()
        out = json.loads(self.run_cli("add", "論文を読む", "-n", "3 章まで", "-d", "2026-10-20",
                                      "-p", "P1").stdout)
        post = self.server.requests[-1]
        self.assertEqual(post["path"], "/api/lists/@default/tasks")
        self.assertEqual(post["query"]["parent"], ["P1"])
        self.assertEqual(post["body"], {"title": "論文を読む", "notes": "3 章まで",
                                        "due": "2026-10-20T00:00:00.000Z"})
        self.assertEqual(out["due"], "2026-10-20")
        self.assertNotIn("etag", out)

        self.run_cli("update", "T1", "-l", "家", "--no-due", "--title", "新")
        patch = self.server.requests[-1]
        self.assertEqual(patch["path"], "/api/lists/L2/tasks/T1")
        self.assertEqual(patch["body"], {"title": "新", "due": None})

        self.assertEqual(self.run_cli("update", "T1", ok=False).returncode, 1)
        self.assertEqual(self.run_cli("add", "x", "-d", "10/20", ok=False).returncode, 1)

        self.run_cli("done", "T1")
        self.assertEqual(self.server.requests[-1]["body"], {"status": "completed"})
        self.run_cli("undone", "T1")
        self.assertEqual(self.server.requests[-1]["body"],
                         {"status": "needsAction", "completed": None})

        out = json.loads(self.run_cli("delete", "T1").stdout)
        self.assertEqual(self.server.requests[-1]["method"], "DELETE")
        self.assertEqual(out, {"ok": True, "deleted": "T1"})


if __name__ == "__main__":
    unittest.main()
