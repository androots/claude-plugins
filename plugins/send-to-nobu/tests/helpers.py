# -*- coding: utf-8 -*-
"""テスト用の合成の会話ログと、契約どおりに振る舞う偽の受け口。

本物の会話ログは読まない。設定ディレクトリは毎回の一時ディレクトリで、そうなっていなければ止まる。
行の形（キー名・type・origin.kind・toolUseResult など）だけを実データに合わせてある。
"""

import datetime
import gzip
import hashlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid as uuidlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import agentlog  # noqa: E402

REAL_CONFIG = os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude"))
TMP_ROOT = os.path.realpath(tempfile.gettempdir())
CWD = os.path.join(os.path.expanduser("~"), "work", "billing")
SEND_CMD = "<command-message>send-to-nobu</command-message>\n<command-name>/send-to-nobu</command-name>"
SEND_CMD_NS = ("<command-message>send-to-nobu:send-to-nobu</command-message>\n"
               "<command-name>/send-to-nobu:send-to-nobu</command-name>")

agentlog.ANSWER_WAIT = 0.0      # 答えの行を待たない（待つ動きは個別に試す）


def assert_sandboxed():
    """読む会話ログが一時ディレクトリであること。違えば全体を止める。"""
    p = os.path.realpath(agentlog.projects_dir())
    if not p.startswith(TMP_ROOT + os.sep) or p.startswith(REAL_CONFIG):
        raise SystemExit("テストの projects が一時ディレクトリではない: %s" % p)


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_uuid():
    return str(uuidlib.uuid4())


class Lines(object):
    """会話 1 本ぶんの行を組み立てる。時刻は base から 1 分ずつ進む（明示もできる）。"""

    def __init__(self, sid=None, base=None, cwd=CWD, entrypoint="cli"):
        self.sid = sid or new_uuid()
        self.base = base if base is not None else time.time() - 3600
        self.cwd, self.entrypoint = cwd, entrypoint
        self.rows, self.parent, self.tick = [], None, 0

    def _ts(self, at):
        if at is None:
            self.tick += 1
            at = self.base + self.tick * 60
        return iso(at)

    def msg(self, typ, content, at=None, **extra):
        uid = new_uuid()
        row = {"parentUuid": self.parent, "isSidechain": False, "type": typ, "message": {"role": typ, "content": content},
               "uuid": uid, "timestamp": self._ts(at), "userType": "external", "entrypoint": self.entrypoint,
               "cwd": self.cwd, "sessionId": self.sid, "version": "2.1.284", "gitBranch": "main"}
        row.update(extra)
        self.rows.append(row)
        self.parent = uid
        return self

    def user(self, text, **kw):
        return self.msg("user", text, **kw)

    def assistant(self, text="了解", **kw):
        return self.msg("assistant", [{"type": "text", "text": text}], **kw)

    def tool(self, name, inp, result="ok", tur=None):
        """assistant の tool_use と、その結果の user 行。"""
        tid = "toolu_" + new_uuid().replace("-", "")[:20]
        self.msg("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": inp}])
        return self.msg("user", [{"type": "tool_result", "tool_use_id": tid, "content": result}],
                        toolUseResult=tur if tur is not None else {"stdout": result})

    def meta(self, typ, **fields):
        self.rows.append(dict({"type": typ, "sessionId": self.sid}, **fields))
        return self

    def raw(self, data):
        self.rows.append(data)
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
    """一時の設定ディレクトリ（projects/ とプラグインのデータ）を作り、CLI をこのプロセスの中で呼ぶ。

    いまの会話（self.current）は「最初の指示が /send-to-nobu」の会話として projects/ に置く。
    """

    SEND_PROJECT = "-Users-alice"

    def __init__(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="stn-test-"))
        self.projects = os.path.join(self.tmp, "projects")
        self.data = os.path.join(self.tmp, "plugins", "data", "send-to-nobu-androots")
        os.makedirs(self.projects)
        self.saved = (agentlog.CONFIG_DIR, os.environ.get("CLAUDE_CONFIG_DIR"))
        agentlog.CONFIG_DIR = self.tmp
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp
        assert_sandboxed()
        self.current = self.start_send_session()

    def close(self):
        agentlog.CONFIG_DIR = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.saved[1]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, lines, project="-Users-alice-work-billing", mtime=None):
        d = os.path.join(self.projects, project)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, lines.sid + ".jsonl")
        with open(path, "wb") as f:
            f.write(lines.encode())
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def append(self, path, lines_or_bytes):
        with open(path, "ab") as f:
            f.write(lines_or_bytes if isinstance(lines_or_bytes, bytes) else lines_or_bytes.encode())

    def subagent(self, main_path, rel, lines_or_bytes):
        p = os.path.join(main_path[:-len(".jsonl")], "subagents", *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(lines_or_bytes if isinstance(lines_or_bytes, bytes) else lines_or_bytes.encode())
        return p

    def start_send_session(self):
        """新しい会話で /send-to-nobu を打った状態を作る。会話 ID を返す。"""
        L = Lines(base=time.time() - 120).user(SEND_CMD).user("スキル本文", isMeta=True).assistant("送る準備")
        self.write(L, project=self.SEND_PROJECT)
        return L.sid

    def session_file(self):
        return os.path.join(self.projects, self.SEND_PROJECT, self.current + ".jsonl")

    def answer(self, answers, questions=None, annotations=None, afk=None, answers_in_input=False, error=False):
        """本人が選択画面（AskUserQuestion）に答えた 2 行を、いまの会話に足す。
        形は Claude Code 2.1.284 の対話で残った行に合わせる（toolUseResult に questions・answers・annotations）。"""
        questions = questions if questions is not None else self.pending()["questions"]
        L = Lines(sid=self.current)
        tid = "toolu_" + new_uuid().replace("-", "")[:20]
        inp = dict({"questions": questions}, **({"answers": answers} if answers_in_input else {}))
        L.msg("assistant", [{"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": inp}])
        result = {"type": "tool_result", "tool_use_id": tid,
                  "content": "User has answered your questions: " + ", ".join('"%s"="%s"' % kv for kv in answers.items())}
        extra = {"sourceToolAssistantUUID": L.parent}
        if error:
            result["is_error"] = True
        else:
            extra["toolUseResult"] = {"questions": questions, "answers": answers, "annotations": annotations or {}}
            if afk is not None:
                extra["toolUseResult"]["afkTimeoutMs"] = afk
        L.msg("user", [result], **extra)
        self.append(self.session_file(), L)

    def answer_simple(self, exclude=agentlog.NONE_LABEL, note=agentlog.NOTE_OPTIONS[0], **kw):
        """1 問目（外す）と 2 問目（感想）に答える。None の質問は答えない。"""
        p = self.pending()
        answers = {}
        if p["exclude_question"] and exclude is not None:
            answers[p["exclude_question"]] = exclude
        if note is not None:
            answers[p["note_question"]] = note
        self.answer(answers, **kw)

    def run(self, *argv, stdin="", session=None, now=None):
        """CLI を呼ぶ。いまの会話 ID は env だけ、時刻は agentlog._now の差し替え。(終了コード, 出力の JSON)。"""
        assert_sandboxed()
        argv = list(argv) + ["--data-dir", self.data]
        saved_env, saved_now = os.environ.get("CLAUDE_CODE_SESSION_ID"), agentlog._now
        os.environ["CLAUDE_CODE_SESSION_ID"] = session or self.current
        if now is not None:
            agentlog._now = lambda: now
        out = io.StringIO()
        try:
            code = agentlog.main(argv, stdin=io.StringIO(stdin), stdout=out)
        finally:
            agentlog._now = saved_now
            if saved_env is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved_env
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else {})

    def list(self, **kw):
        code, res = self.run("list", **kw)
        if code != 0:
            raise AssertionError("list failed: %s" % res)
        return res

    def titles(self):
        """いまの一覧の番号 → タイトル。"""
        return {it["n"]: it["title"] for it in self.pending()["items"]}

    def listed(self):
        """いまの一覧に出た会話 ID（番号順）。"""
        return [it["session_id"] for it in self.pending()["items"]]

    def state(self):
        return agentlog.read_json(os.path.join(self.data, "state.json"), None)

    def pending(self):
        return agentlog.read_json(os.path.join(self.data, "pending.json"), None)

    def next_day(self):
        """翌朝、新しい会話で /send-to-nobu を打つ。"""
        self.current = self.start_send_session()


# ---------------------------------------------------------------- 偽の受け口


class FakeInbox(object):
    """契約 v2 の /v1/uploads・PUT・/v1/finish を模す（GCS の代わりにメモリに持つ）。"""

    CODE = "abcdefghijklmnopqrstuvwxyz"

    def __init__(self):
        self.objects, self.issued, self.finish_bodies = {}, {}, []
        self.upload_batches = []
        self.finished = False
        self.finish_lose_first = False     # 送信票は置けたが返事が届かなかった（502）
        self.put_url_base = None
        self.redirect_uploads = False
        self.redirect_hits = 0
        self.lock = threading.Lock()
        inbox = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, obj, headers=()):
                data = json.dumps(obj).encode()
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _err(self, status, code, headers=()):
                self._json(status, {"error": {"code": code, "message": "テスト用のエラー"}}, headers)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self):
                body = self._body()
                if self.path == "/elsewhere":
                    inbox.redirect_hits += 1
                    return self._json(200, {"uploads": []})
                if self.headers.get("Authorization") != "Bearer " + inbox.CODE:
                    return self._err(401, "unauthorized")
                req = json.loads(body.decode())
                if self.path == "/v1/uploads":
                    if inbox.redirect_uploads:
                        return self._err(307, "moved", [("Location", "http://127.0.0.1:%d/elsewhere" % inbox.port)])
                    if inbox.finished:
                        return self._err(401, "unauthorized")
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
                    obj += ".jsonl.gz" if f["rel"] is None else "/subagents/%s.gz" % f["rel"]
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
                with inbox.lock:
                    issued = inbox.issued.get(token)
                if not issued:
                    return self._err(404, "not_found")
                for k, v in issued["headers"].items():   # 署名に含めたヘッダーがそのまま付いているか
                    if self.headers.get(k) != v:
                        return self._err(403, "signature_mismatch")
                if hashlib.sha256(body).hexdigest() != issued["headers"]["x-goog-meta-sha256"]:
                    return self._err(400, "sha_mismatch")
                with inbox.lock:
                    inbox.objects[issued["object"]] = body
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _finish(self, req):
                if inbox.finished:
                    return self._err(409, "already_finished")
                keys = {"sent", "excluded_count", "note", "plugin_version"}
                if not keys <= set(req) or set(req) - keys - {"assistant_note"}:
                    return self._err(400, "invalid_argument")    # 契約に無いキーは 400
                if not req["sent"] and not req["note"]:
                    return self._err(400, "invalid_argument")
                for s in req["sent"]:
                    base = "raw/androots/alice/claude-code/%s" % s["session_id"]
                    checks = [(base + ".jsonl.gz", s["bytes"], s["sha256"])]
                    checks += [(base + "/subagents/%s.gz" % a["rel"], a["bytes"], a["sha256"]) for a in s["subagents"]]
                    for obj, size, sha in checks:
                        got = inbox.objects.get(obj)
                        if got is None or len(got) != size or hashlib.sha256(got).hexdigest() != sha:
                            return self._err(422, "size_mismatch")
                inbox.finish_bodies.append(req)
                inbox.finished = True
                if inbox.finish_lose_first and len(inbox.finish_bodies) == 1:
                    return self._err(502, "bad_gateway")
                self._json(200, {"submission_id": "20260929T090312Z-1a2b3c4d", "object": "submissions/x.json",
                                 "sent_count": len(req["sent"]), "subagent_count": 0})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def body(self, sid, rel=None):
        """受け口に届いた会話の中身（gzip を解いたバイト列）。無ければ None。"""
        obj = "raw/androots/alice/claude-code/%s" % sid + (".jsonl.gz" if rel is None else "/subagents/%s.gz" % rel)
        got = self.objects.get(obj)
        return gzip.decompress(got) if got is not None else None

    def sent_ids(self):
        return sorted({o.split("/")[4].split(".")[0] for o in self.objects})


class Patched(object):
    """偽の受け口に向けて送り先・待ち時間・通信を差し替える（テストコードからだけ）。"""

    def __init__(self, inbox):
        self.saved = (agentlog.ALLOWED_API_BASES, agentlog.ALLOWED_PUT_PREFIXES, agentlog.BACKOFF_BASE, agentlog._OPENER)
        agentlog.ALLOWED_API_BASES = (inbox.base,)
        agentlog.ALLOWED_PUT_PREFIXES = (inbox.base + "/put/",)
        agentlog.BACKOFF_BASE = 0.001
        agentlog._OPENER = agentlog.build_opener(use_proxy=False)

    def restore(self):
        agentlog.ALLOWED_API_BASES, agentlog.ALLOWED_PUT_PREFIXES, agentlog.BACKOFF_BASE, agentlog._OPENER = self.saved
