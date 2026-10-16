#!/usr/bin/env python3
"""gtasks: Google Tasks を操作する小さな CLI。標準ライブラリだけで動く。

使い方は README.md を参照。結果は JSON で標準出力に出す (--text で人向けの表示)。
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/tasks"
# デスクトップアプリの OAuth クライアントは、ループバックならどのポートでも受け付ける。
# 何も待ち受けていなくてよい: ブラウザはエラー画面になるが、アドレスバーに code が残る。
REDIRECT_URI = "http://127.0.0.1:8765/"

# 出力に残すタスクの項目 (etag や selfLink などは落とす)
TASK_FIELDS = ("id", "title", "notes", "status", "due", "completed", "parent",
               "position", "updated", "hidden", "deleted")


class GtasksError(Exception):
    pass


# ---- 設定とファイル ----

def endpoint(name, default):
    # テストで差し替えられるよう、呼ぶたびに環境変数を読む
    return os.environ.get(name, default)


def config_dir():
    d = os.environ.get("GTASKS_CONFIG_DIR")
    if d:
        return Path(d)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "gtasks"


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def write_private(path, data):
    """自分だけが読める権限 (0600) で JSON を書く。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_client():
    path = config_dir() / "client_secret.json"
    data = read_json(path)
    if data is None:
        raise GtasksError(
            f"{path} がありません。Google Cloud で作った OAuth クライアントの JSON をここに置いてください。")
    info = data.get("installed")
    if not info:
        raise GtasksError(
            f"{path} はデスクトップアプリのクライアントではありません。種類「デスクトップアプリ」で作り直してください。")
    return info["client_id"], info["client_secret"]


# ---- HTTP ----

def http(method, url, *, params=None, body=None, form=None, token=None):
    if params:
        query = {k: v for k, v in params.items() if v is not None}
        if query:
            url += "?" + urllib.parse.urlencode(query)
    headers = {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise GtasksError(f"{method} {url} が HTTP {e.code} で失敗しました: {detail}") from None
    except urllib.error.URLError as e:
        raise GtasksError(f"{method} {url} に接続できません: {e.reason}") from None
    return json.loads(raw) if raw else None


# ---- 認可 ----

def cmd_auth_url(args):
    client_id, _ = load_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    write_private(config_dir() / "pending_auth.json", {"verifier": verifier, "state": state})
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # 毎回リフレッシュトークンを出させる
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if args.account:
        params["login_hint"] = args.account
    print(endpoint("GTASKS_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
          + "?" + urllib.parse.urlencode(params))
    print("この URL をブラウザで開いて許可したあと、飛ばされた先 (エラー画面でよい) の URL を\n"
          "  gtasks auth-finish '<その URL>'\nに渡してください。", file=sys.stderr)


def cmd_auth_finish(args):
    pending_path = config_dir() / "pending_auth.json"
    pending = read_json(pending_path)
    if pending is None:
        raise GtasksError("先に gtasks auth-url を実行してください。")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(args.url).query)
    if "error" in query:
        raise GtasksError(f"認可されませんでした: {query['error'][0]}")
    code = query.get("code", [None])[0]
    if not code:
        raise GtasksError("URL に code がありません。ブラウザのアドレスバーの URL をそのまま渡してください。")
    if query.get("state", [None])[0] != pending["state"]:
        raise GtasksError("state が一致しません。最後に出した auth-url の URL から認可し直してください。")
    client_id, client_secret = load_client()
    tok = http("POST", endpoint("GTASKS_TOKEN_URL", "https://oauth2.googleapis.com/token"), form={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": pending["verifier"],
    })
    if "refresh_token" not in tok:
        raise GtasksError("リフレッシュトークンが返りませんでした。auth-url からやり直してください。")
    save_token(tok)
    pending_path.unlink()
    print(json.dumps({"ok": True, "token": str(config_dir() / "token.json")}, ensure_ascii=False))


def save_token(tok, refresh_token=None):
    write_private(config_dir() / "token.json", {
        "refresh_token": tok.get("refresh_token") or refresh_token,
        "access_token": tok["access_token"],
        "expires_at": time.time() + tok.get("expires_in", 3600),
    })


def access_token():
    saved = read_json(config_dir() / "token.json")
    if saved is None:
        raise GtasksError("まだ認可されていません。gtasks auth-url から始めてください。")
    if saved.get("access_token") and saved.get("expires_at", 0) > time.time() + 60:
        return saved["access_token"]
    client_id, client_secret = load_client()
    try:
        tok = http("POST", endpoint("GTASKS_TOKEN_URL", "https://oauth2.googleapis.com/token"), form={
            "refresh_token": saved["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        })
    except GtasksError as e:
        if "invalid_grant" in str(e):
            raise GtasksError(
                "リフレッシュトークンが失効しています。gtasks auth-url から認可し直してください。"
                "(OAuth 同意画面が「テスト」のままだと 7 日で切れます)") from None
        raise
    save_token(tok, saved["refresh_token"])
    return tok["access_token"]


# ---- Tasks API ----

class Api:
    def __init__(self):
        self.token = access_token()
        self.base = endpoint("GTASKS_API_BASE", "https://tasks.googleapis.com/tasks/v1")

    def call(self, method, path, **kw):
        return http(method, self.base + path, token=self.token, **kw)

    def paged(self, path, params):
        params = dict(params)
        items = []
        while True:
            res = self.call("GET", path, params=params) or {}
            items += res.get("items", [])
            if not res.get("nextPageToken"):
                return items
            params["pageToken"] = res["nextPageToken"]

    def lists(self):
        return self.paged("/users/@me/lists", {"maxResults": 100})


def q(s):
    return urllib.parse.quote(s, safe="@")


def resolve_list(api, name):
    """リストの ID か名前を ID にする。省略時は既定のリスト。"""
    if name is None or name == "@default":
        return "@default"
    lists = api.lists()
    if any(l["id"] == name for l in lists):
        return name
    hits = [l for l in lists if l["title"] == name]
    if len(hits) == 1:
        return hits[0]["id"]
    titles = ", ".join(l["title"] for l in lists)
    if not hits:
        raise GtasksError(f"リスト「{name}」が見つかりません。あるのは: {titles}")
    raise GtasksError(f"「{name}」という名前のリストが複数あります。ID で指定してください。")


def due_value(s):
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        raise GtasksError(f"日付は YYYY-MM-DD で指定してください: {s}") from None
    # Tasks の期限は日付だけ。時刻部分は保存されない
    return f"{d.isoformat()}T00:00:00.000Z"


def slim(task, list_title=None):
    out = {k: task[k] for k in TASK_FIELDS if k in task}
    if out.get("due"):
        out["due"] = out["due"][:10]
    if list_title is not None:
        out["list"] = list_title
    return out


def order(tasks):
    """親のすぐ後に子が並ぶよう、position で並べ替える。"""
    by_parent = {}
    for t in tasks:
        by_parent.setdefault(t.get("parent"), []).append(t)
    for children in by_parent.values():
        children.sort(key=lambda t: t.get("position", ""))
    ids = {t["id"] for t in tasks}
    result = []

    def walk(parent):
        for t in by_parent.get(parent, []):
            result.append(t)
            walk(t["id"])

    walk(None)
    # 親が絞り込みで落ちた子も捨てずに末尾へ
    result += [t for p, ch in by_parent.items() if p is not None and p not in ids for t in ch]
    return result


# ---- 出力 ----

def emit(args, data):
    if not args.text:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif isinstance(data, list) and data and "status" in data[0]:
        for t in data:
            mark = "x" if t.get("status") == "completed" else " "
            indent = "    " if t.get("parent") else ""
            extra = f"  (期限 {t['due']})" if t.get("due") else ""
            where = f"  [{t['list']}]" if t.get("list") else ""
            print(f"{indent}[{mark}] {t.get('title', '')}{extra}{where}  {t['id']}")
    elif isinstance(data, list):
        for l in data:
            print(f"{l['title']}  {l['id']}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


# ---- サブコマンド ----

def cmd_lists(args):
    api = Api()
    emit(args, [{"id": l["id"], "title": l["title"], "updated": l.get("updated")}
                for l in api.lists()])


def cmd_list(args):
    api = Api()
    show_completed = args.completed or args.completed_since is not None
    params = {
        "maxResults": 100,
        "showCompleted": str(show_completed).lower(),
        # 公式アプリで完了にしたタスクは hidden になるので、完了を見るときは hidden も出す
        "showHidden": str(show_completed).lower(),
        "completedMin": due_value(args.completed_since) if args.completed_since else None,
    }
    if args.all_lists:
        targets = [(l["id"], l["title"]) for l in api.lists()]
    else:
        targets = [(resolve_list(api, args.list), None)]
    result = []
    for list_id, title in targets:
        tasks = api.paged(f"/lists/{q(list_id)}/tasks", params)
        if args.due_by:
            limit = due_value(args.due_by)[:10]
            tasks = [t for t in tasks if t.get("due") and t["due"][:10] <= limit]
        result += [slim(t, title) for t in order(tasks)]
    emit(args, result)


def cmd_add(args):
    api = Api()
    list_id = resolve_list(api, args.list)
    body = {"title": args.title}
    if args.notes is not None:
        body["notes"] = args.notes
    if args.due:
        body["due"] = due_value(args.due)
    task = api.call("POST", f"/lists/{q(list_id)}/tasks",
                    params={"parent": args.parent}, body=body)
    emit(args, slim(task))


def cmd_update(args):
    api = Api()
    list_id = resolve_list(api, args.list)
    body = {}
    if args.title is not None:
        body["title"] = args.title
    if args.notes is not None:
        body["notes"] = args.notes
    if args.due:
        body["due"] = due_value(args.due)
    if args.no_due:
        body["due"] = None
    if not body:
        raise GtasksError("変える項目を 1 つ以上指定してください (--title, --notes, --due, --no-due)。")
    emit(args, slim(api.call("PATCH", f"/lists/{q(list_id)}/tasks/{q(args.task)}", body=body)))


def set_status(args, status):
    api = Api()
    list_id = resolve_list(api, args.list)
    body = {"status": status}
    if status == "needsAction":
        body["completed"] = None
    emit(args, slim(api.call("PATCH", f"/lists/{q(list_id)}/tasks/{q(args.task)}", body=body)))


def cmd_done(args):
    set_status(args, "completed")


def cmd_undone(args):
    set_status(args, "needsAction")


def cmd_delete(args):
    api = Api()
    list_id = resolve_list(api, args.list)
    api.call("DELETE", f"/lists/{q(list_id)}/tasks/{q(args.task)}")
    emit(args, {"ok": True, "deleted": args.task})


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-t", "--text", action="store_true", help="JSON ではなく人向けに表示する")
    with_list = argparse.ArgumentParser(add_help=False, parents=[common])
    with_list.add_argument("-l", "--list", help="リストの名前か ID (省略時は既定のリスト)")

    p = argparse.ArgumentParser(prog="gtasks", description="Google Tasks を操作する")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("auth-url", parents=[common], help="認可用の URL を出す")
    s.add_argument("--account", help="使う Google アカウントのメールアドレス (ヒント)")
    s.set_defaults(func=cmd_auth_url)

    s = sub.add_parser("auth-finish", parents=[common], help="認可後に飛ばされた URL を渡してトークンを保存する")
    s.add_argument("url")
    s.set_defaults(func=cmd_auth_finish)

    s = sub.add_parser("lists", parents=[common], help="タスクリストの一覧")
    s.set_defaults(func=cmd_lists)

    s = sub.add_parser("list", parents=[with_list], help="タスクの一覧 (既定では未完了のみ)")
    s.add_argument("-a", "--all-lists", action="store_true", help="すべてのリストから集める")
    s.add_argument("--completed", action="store_true", help="完了したタスクも含める")
    s.add_argument("--completed-since", metavar="DATE", help="この日以降に完了したものを含める (YYYY-MM-DD)")
    s.add_argument("--due-by", metavar="DATE", help="期限がこの日以前のものだけ (YYYY-MM-DD)")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("add", parents=[with_list], help="タスクを追加する")
    s.add_argument("title")
    s.add_argument("-n", "--notes")
    s.add_argument("-d", "--due", metavar="DATE", help="期限 (YYYY-MM-DD)")
    s.add_argument("-p", "--parent", metavar="TASK_ID", help="このタスクのサブタスクにする")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("update", parents=[with_list], help="タスクを書き換える")
    s.add_argument("task", metavar="TASK_ID")
    s.add_argument("--title")
    s.add_argument("-n", "--notes")
    due = s.add_mutually_exclusive_group()
    due.add_argument("-d", "--due", metavar="DATE")
    due.add_argument("--no-due", action="store_true", help="期限を外す")
    s.set_defaults(func=cmd_update)

    for name, func, text in (("done", cmd_done, "完了にする"),
                             ("undone", cmd_undone, "未完了に戻す"),
                             ("delete", cmd_delete, "削除する")):
        s = sub.add_parser(name, parents=[with_list], help=text)
        s.add_argument("task", metavar="TASK_ID")
        s.set_defaults(func=func)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except GtasksError as e:
        print(f"gtasks: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
