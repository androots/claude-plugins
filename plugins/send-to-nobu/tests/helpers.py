# -*- coding: utf-8 -*-
"""テスト用の合成フィクスチャと、契約どおりに振る舞う偽サーバー。

本物の会話ログは使わない。行の形（キー名・type・origin.kind など）だけを実データに合わせてある。
テスト用の差し替え（会話ログの場所・いまの時刻・送り先）は CLI ではなくモジュールの変数で行う。
"""

import datetime
import gzip
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid as uuidlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import agentlog  # noqa: E402

HOME = os.path.expanduser("~")
CWD = os.path.join(HOME, "work", "billing")
SEND_CMD = "<command-message>send-to-nobu</command-message>\n<command-name>/send-to-nobu</command-name>"
SEND_CMD_NS = ("<command-message>send-to-nobu:send-to-nobu</command-message>\n"
               "<command-name>/send-to-nobu:send-to-nobu</command-name>")


def send_cmd(args=""):
    """/send-to-nobu の行（引数があれば <command-args> 付き）。"""
    return SEND_CMD + ("\n<command-args>%s</command-args>" % args if args else "")


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_uuid():
    return str(uuidlib.uuid4())


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class Lines(object):
    """会話 1 本ぶんの行を組み立てる。時刻は base から 1 分ずつ進む（明示もできる）。"""

    def __init__(self, sid=None, base=None, cwd=CWD, entrypoint="cli"):
        self.sid = sid or new_uuid()
        self.base = base if base is not None else time.time() - 3600
        self.cwd = cwd
        self.entrypoint = entrypoint
        self.rows = []
        self.parent = None
        self.tick = 0

    def _ts(self, at):
        if at is None:
            self.tick += 1
            at = self.base + self.tick * 60
        return iso(at)

    def _msg(self, typ, content, at=None, uid=None, **extra):
        uid = uid or new_uuid()
        row = {"parentUuid": self.parent, "isSidechain": False, "type": typ,
               "message": {"role": typ, "content": content}, "uuid": uid, "timestamp": self._ts(at),
               "userType": "external", "entrypoint": self.entrypoint, "cwd": self.cwd, "sessionId": self.sid,
               "version": "2.1.283", "gitBranch": "main"}
        row.update(extra)
        self.rows.append(row)
        self.parent = uid
        return self

    def user(self, text, **kw):
        return self._msg("user", text, **kw)

    def user_blocks(self, blocks, **kw):
        return self._msg("user", blocks, **kw)

    def assistant(self, text="了解", **kw):
        return self._msg("assistant", [{"type": "text", "text": text}], **kw)

    def bash(self, command, **kw):
        """assistant が Bash ツールを呼んだ行。"""
        return self._msg("assistant", [{"type": "tool_use", "id": "toolu_" + new_uuid()[:8], "name": "Bash",
                                        "input": {"command": command}}], **kw)

    def tool_result(self, text="ok", **kw):
        return self._msg("user", [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}],
                         toolUseResult={"stdout": text}, **kw)

    def meta(self, typ, **fields):
        row = {"type": typ, "sessionId": self.sid}
        row.update(fields)
        self.rows.append(row)
        return self

    def system(self, subtype, **fields):
        row = {"parentUuid": self.parent, "isSidechain": False, "type": "system", "subtype": subtype,
               "uuid": new_uuid(), "timestamp": self._ts(None), "cwd": self.cwd, "sessionId": self.sid}
        row.update(fields)
        self.rows.append(row)
        return self

    def raw(self, data):
        self.rows.append(data)  # bytes をそのまま 1 行として書く
        return self

    def copy_from(self, other, count=None):
        """分岐コピー: 相手の行を uuid・timestamp ごとそのまま先頭に写す。"""
        for row in other.rows[:count]:
            if isinstance(row, dict):
                self.rows.append(dict(row, sessionId=self.sid))
        return self

    def encode(self):
        out = []
        for row in self.rows:
            if isinstance(row, bytes):
                out.append(row if row.endswith(b"\n") else row + b"\n")
            else:
                out.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        return b"".join(out)


class World(object):
    """一時ディレクトリに projects/ とデータディレクトリを作り、CLI を中で呼ぶ。

    いまの会話（self.current）は「最初の指示が /send-to-nobu」の会話として projects/ に置く。
    """

    SEND_PROJECT = "-Users-alice"

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="stn-test-")
        self.projects = os.path.join(self.tmp, "projects")
        self.data = os.path.join(self.tmp, "plugins", "data", "send-to-nobu-data")   # 設定ディレクトリの形に合わせる
        os.makedirs(self.projects)
        self.saved_projects = (agentlog.CONFIG_DIR, agentlog.PROJECTS_DIR)
        agentlog.CONFIG_DIR = self.tmp
        agentlog.PROJECTS_DIR = self.projects
        self.current = self.start_send_session()

    def close(self):
        agentlog.CONFIG_DIR, agentlog.PROJECTS_DIR = self.saved_projects
        shutil.rmtree(self.tmp, ignore_errors=True)

    def project_dir(self, name="-Users-alice-work-billing"):
        d = os.path.join(self.projects, name)
        os.makedirs(d, exist_ok=True)
        return d

    def write(self, lines, project="-Users-alice-work-billing", mtime=None):
        path = os.path.join(self.project_dir(project), lines.sid + ".jsonl")
        with open(path, "wb") as f:
            f.write(lines.encode())
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def append(self, path, lines_or_bytes, mtime=None):
        data = lines_or_bytes if isinstance(lines_or_bytes, bytes) else lines_or_bytes.encode()
        with open(path, "ab") as f:
            f.write(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def subagent(self, main_path, rel, lines_or_bytes):
        p = os.path.join(main_path[:-len(".jsonl")], "subagents", *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = lines_or_bytes if isinstance(lines_or_bytes, bytes) else lines_or_bytes.encode()
        with open(p, "wb") as f:
            f.write(data)
        return p

    def start_send_session(self):
        """新しい会話で /send-to-nobu を打った状態を作る。会話 ID を返す。"""
        L = Lines(base=time.time() - 120).user(send_cmd()).user("スキル本文", isMeta=True).assistant("確認する")
        self.write(L, project=self.SEND_PROJECT)
        return L.sid

    def session_file(self, sid=None):
        return os.path.join(self.projects, self.SEND_PROJECT, (sid or self.current) + ".jsonl")

    def reply(self, text="なし", sid=None, plain=False):
        """本人の返事を、いまの会話に足す（既定は /send-to-nobu <返事> の形）。"""
        path = self.session_file(sid)
        if os.path.exists(path):
            self.append(path, Lines(sid=sid or self.current).user(text if plain else send_cmd(text)))

    def answer(self, questions, answers, sid=None, annotations=None, answers_in_input=False, error=False, afk=None):
        """本人が選択画面（AskUserQuestion）に答えた 2 行（AI の tool_use と、答えの tool_result）を、いまの会話に足す。

        形は Claude Code 2.1.284 の対話で実際に残った行に合わせる（toolUseResult に questions・answers・annotations）。
        """
        sid = sid or self.current
        L = Lines(sid=sid)
        tid = "toolu_" + new_uuid().replace("-", "")[:20]
        inp = {"questions": questions}
        if answers_in_input:
            inp["answers"] = answers
        L._msg("assistant", [{"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": inp,
                              "caller": {"type": "direct"}}])
        src = L.parent
        result = {"type": "tool_result", "tool_use_id": tid,
                  "content": "The user answered: %s" % ", ".join('"%s"="%s"' % kv for kv in answers.items())}
        extra = {"sourceToolAssistantUUID": src}
        if error:
            result["is_error"] = True
        else:
            extra["toolUseResult"] = {"questions": questions, "answers": answers, "annotations": annotations or {}}
            if afk is not None:
                extra["toolUseResult"]["afkTimeoutMs"] = afk   # 離席で自動的に閉じた（実データにある形）
        L._msg("user", [result], **extra)
        self.append(self.session_file(sid), L)

    def ask(self):
        return (self.pending().get("ask") or {}).get("questions")

    def run(self, *argv, stdin="", session=None, now=None, reply=True):
        """CLI をこのプロセスの中で呼ぶ。会話 ID は env だけ、時刻は agentlog._now の差し替え。"""
        argv = list(argv) + ["--data-dir", self.data]
        sid = self.current if session is None else session
        if argv[0] == "send" and reply and sid:
            self.reply("返事", sid=sid)
        saved_env = os.environ.get("CLAUDE_CODE_SESSION_ID")
        saved_now = agentlog._now
        if sid:
            os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        if now is not None:
            agentlog._now = lambda: now
        out, err = io.StringIO(), io.StringIO()
        try:
            code = agentlog.main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err)
        finally:
            agentlog._now = saved_now
            if saved_env is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved_env
        return code, out.getvalue(), err.getvalue()

    def list(self, **kw):
        code, out, err = self.run("list", **kw)
        if code != 0:
            raise AssertionError("list failed: %s" % err)
        return json.loads(out)

    def review_files(self, n):
        """いまの一覧の n 番の確認用ファイル（パート順）。"""
        d = self.pending()["output"]["review_dir"]
        names = [x for x in os.listdir(d) if re.match(r"^%02d-\d+\.txt$" % n, x)]
        return [os.path.join(d, x) for x in sorted(names, key=lambda x: int(x.split("-")[1].split(".")[0]))]

    def review_blocks(self, n):
        """いまの一覧の n 番の確認用ファイルを読み、[(話し手, 本文)] にする。"""
        lines = []
        for path in self.review_files(n):
            with open(path, encoding="utf-8") as f:
                lines += [l for l in f.read().split("\n")
                          if not l.startswith("# ") and not re.match(r"^（\d+/\d+ ここまで）$", l)]
        blocks = []
        for line in lines:
            m = re.match(r"^【(本人|AI|サブエージェントへの指示|サブエージェント)】$", line)
            if m:
                blocks.append([m.group(1), []])
            elif blocks:
                blocks[-1][1].append(line)
        return [(who, "\n".join(body).strip()) for who, body in blocks]

    def human(self, n):
        """n 番の会話で、確認係に「本人の指示」として渡る本文。"""
        return [t for who, t in self.review_blocks(n) if who == "本人"]

    def checker_results(self, ok="all", caution=None, unknown=None):
        """番号の指定から、確認係ごとの答えの並びを作る（書かなかった会話の確認係は未着のまま）。"""
        p = self.pending()
        ids = {it["n"]: it.get("checkers") or [] for it in p["items"]}
        ticket = {c["id"]: c["ticket"] for c in p["checker_list"]}

        def nums(spec):
            if spec == "all":
                return sorted(ids)
            if not spec or spec == "none":
                return []
            return sorted(agentlog.parse_numbers(spec, ids, "test"))
        out = []
        for verdict, spec in (("ok", ok), ("caution", caution), ("unknown", unknown)):
            for n in nums(spec):
                for k, c in enumerate(ids[n]):
                    v = verdict if (verdict != "caution" or k == 0) else "ok"
                    out.append({"ticket": ticket[c], "verdict": v, "reasons": ["テストの理由"] if v == "caution" else []})
        return out

    def check(self, ok="all", caution=None, unknown=None, session=None, results=None):
        """確認係の結果を控えに渡す（既定は全部 ok）。checked の出力を返す。"""
        if results is None:
            results = self.checker_results(ok, caution, unknown)
        code, out, err = self.run("checked", stdin=json.dumps(results, ensure_ascii=False), session=session)
        if code != 0:
            raise AssertionError("checked failed: %s" % err)
        return json.loads(out)

    def tickets(self):
        """いまの一覧の確認係の番号 → 札。"""
        return {c["id"]: c["ticket"] for c in self.pending()["checker_list"]}

    def checkers(self):
        """いまの一覧の確認係すべて（控え）。"""
        return self.pending()["checker_list"]

    def state(self):
        return agentlog.read_json(os.path.join(self.data, "state.json"), None)

    def pending(self):
        return agentlog.read_json(os.path.join(self.data, "pending.json"), None)


# ---------------------------------------------------------------- 偽サーバー


class FakeInbox(object):
    """契約 v2 の /v1/uploads・PUT・/v1/finish を模す（GCS の代わりにメモリに持つ）。"""

    CODE = "abcdefghijklmnopqrstuvwxyz"

    def __init__(self):
        self.objects = {}          # object -> {"body", "sha256"}
        self.issued = {}           # token -> {"object", "headers"}
        self.upload_batches = []   # 1 回の /v1/uploads で来た件数
        self.upload_calls = 0
        self.uploads_status = None  # 例: (400, "invalid_argument")
        self.uploads_redirect = False
        self.put_url_base = None   # 署名 URL の先頭を差し替える（許可されていない先を返す試験）
        self.put_attempts = {}     # object -> 回数
        self.put_delay = 0.0
        self.fail_put = {}         # object に含まれる文字列 -> 先頭何回 503 にするか
        self.forbid_put = set()    # ここに含まれる文字列を持つ object は 403
        self.finish_bodies = []
        self.finish_lose_first = False
        self.finished = False
        self.redirect_hits = 0
        self.lock = threading.Lock()
        inbox = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _err(self, status, code, details=None):
                body = {"error": {"code": code, "message": "テスト用のエラー"}}
                if details:
                    body["error"]["details"] = details
                self._json(status, body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n)

            def do_POST(self):
                body = self._body()
                if self.path.startswith("/elsewhere"):
                    inbox.redirect_hits += 1
                    return self._json(200, {"uploads": []})
                if self.headers.get("Authorization") != "Bearer " + inbox.CODE:
                    return self._err(401, "unauthorized")
                req = json.loads(body.decode())
                if self.path == "/v1/uploads":
                    inbox.upload_calls += 1
                    if inbox.uploads_redirect:
                        self.send_response(307)
                        self.send_header("Location", "http://127.0.0.1:%d/elsewhere" % inbox.port)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if inbox.uploads_status:
                        return self._err(*inbox.uploads_status)
                    return self._uploads(req)
                if self.path == "/v1/finish":
                    return self._finish(req)
                return self._err(404, "not_found")

            def _uploads(self, req):
                files = req.get("files") or []
                if len(files) > 200 or any(set(f) != {"session_id", "rel", "bytes", "sha256"} for f in files):
                    return self._err(400, "invalid_argument")
                inbox.upload_batches.append(len(files))
                out = []
                for f in files:
                    obj = "raw/androots/alice/claude-code/%s" % f["session_id"]
                    obj += (".jsonl.gz" if f["rel"] is None else "/subagents/%s.gz" % f["rel"])
                    token = hashlib.sha256(obj.encode()).hexdigest()[:16]
                    headers = {"Content-Type": "application/gzip", "x-goog-meta-sha256": f["sha256"],
                               "x-goog-content-length-range": "%d,%d" % (f["bytes"], f["bytes"])}
                    with inbox.lock:
                        inbox.issued[token] = {"object": obj, "headers": headers}
                    base = inbox.put_url_base or "http://127.0.0.1:%d" % inbox.port
                    out.append({"session_id": f["session_id"], "rel": f["rel"], "object": obj, "method": "PUT",
                                "url": "%s/put/%s?X-Goog-Signature=x" % (base, token), "headers": headers})
                self._json(200, {"uploads": out})

            def do_PUT(self):
                token = self.path.split("/put/", 1)[-1].split("?", 1)[0]
                body = self._body()
                if inbox.put_delay:
                    time.sleep(inbox.put_delay)
                with inbox.lock:
                    issued = inbox.issued.get(token)
                if not issued:
                    return self._err(404, "not_found")
                obj = issued["object"]
                with inbox.lock:
                    inbox.put_attempts[obj] = inbox.put_attempts.get(obj, 0) + 1
                    attempt = inbox.put_attempts[obj]
                if any(k in obj for k in inbox.forbid_put):
                    return self._err(403, "forbidden")
                for k, n in inbox.fail_put.items():
                    if k in obj and attempt <= n:
                        return self._err(503, "unavailable")
                # 署名対象ヘッダーがそのまま付いているか
                for k, v in issued["headers"].items():
                    if self.headers.get(k) != v:
                        return self._err(403, "signature_mismatch")
                if hashlib.sha256(body).hexdigest() != issued["headers"]["x-goog-meta-sha256"]:
                    return self._err(400, "sha_mismatch")
                with inbox.lock:
                    inbox.objects[obj] = {"body": body, "sha256": issued["headers"]["x-goog-meta-sha256"]}
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _finish(self, req):
                if inbox.finished:
                    return self._err(409, "already_finished")
                if not {"sent", "excluded_count", "note", "plugin_version"} <= set(req) \
                        or set(req) - {"sent", "excluded_count", "note", "plugin_version", "assistant_note"}:
                    return self._err(400, "invalid_argument")
                sent = req.get("sent") or []
                if not sent and not req.get("note"):
                    return self._err(400, "invalid_argument")
                bad = []
                for s in sent:
                    base = "raw/androots/alice/claude-code/%s" % s["session_id"]
                    checks = [(base + ".jsonl.gz", s["bytes"], s["sha256"])]
                    checks += [(base + "/subagents/%s.gz" % a["rel"], a["bytes"], a["sha256"]) for a in s["subagents"]]
                    for obj, size, sha in checks:
                        got = inbox.objects.get(obj)
                        if not got or len(got["body"]) != size or got["sha256"] != sha:
                            bad.append(obj)
                if bad:
                    return self._err(422, "size_mismatch", bad)
                inbox.finish_bodies.append(req)
                inbox.finished = True
                if inbox.finish_lose_first and len(inbox.finish_bodies) == 1:
                    return self._err(502, "bad_gateway")  # 置けたが返事が届かなかった
                self._json(200, {"submission_id": "20260928T090312Z-1a2b3c4d",
                                 "object": "submissions/androots/alice/20260928T090312Z-1a2b3c4d.json",
                                 "sent_count": len(sent),
                                 "subagent_count": sum(len(s["subagents"]) for s in sent)})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def object_lines(self, obj):
        return gzip.decompress(self.objects[obj]["body"])


class Patched(object):
    """偽サーバー向けに agentlog の送り先・待ち時間・通信を差し替える（テストコードからだけ）。"""

    def __init__(self, inbox):
        self.saved = (agentlog.ALLOWED_API_BASES, agentlog.ALLOWED_PUT_PREFIXES, agentlog.BACKOFF_BASE,
                      agentlog._OPENER)
        agentlog.ALLOWED_API_BASES = (inbox.base,)
        agentlog.ALLOWED_PUT_PREFIXES = (inbox.base + "/put/",)
        agentlog.BACKOFF_BASE = 0.001
        agentlog._OPENER = agentlog.build_opener(use_proxy=False)  # OS のプロキシ設定を使わない

    def restore(self):
        (agentlog.ALLOWED_API_BASES, agentlog.ALLOWED_PUT_PREFIXES, agentlog.BACKOFF_BASE,
         agentlog._OPENER) = self.saved
