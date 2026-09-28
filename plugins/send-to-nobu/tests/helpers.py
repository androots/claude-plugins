# -*- coding: utf-8 -*-
"""テスト用の合成フィクスチャと、契約どおりに振る舞う偽サーバー。

本物の会話ログは使わない。行の形（キー名・type・origin.kind など）だけを実データに合わせてある。
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

HOME = os.path.expanduser("~")
CWD = os.path.join(HOME, "work", "billing")


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_uuid():
    return str(uuidlib.uuid4())


class Lines(object):
    """会話 1 本ぶんの行を組み立てる。時刻は base から 1 分ずつ進む（明示もできる）。"""

    def __init__(self, sid=None, base=None, cwd=CWD):
        self.sid = sid or new_uuid()
        self.base = base if base is not None else time.time() - 3600
        self.cwd = cwd
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
               "userType": "external", "entrypoint": "cli", "cwd": self.cwd, "sessionId": self.sid,
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
    """一時ディレクトリに projects/ とデータディレクトリを作り、CLI を中で呼ぶ。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="stn-test-")
        self.projects = os.path.join(self.tmp, "projects")
        self.data = os.path.join(self.tmp, "send-to-nobu-data")
        self.current = new_uuid()
        os.makedirs(self.projects)

    def close(self):
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

    def run(self, *argv, stdin="", session=None, now=None):
        argv = list(argv) + ["--data-dir", self.data, "--projects-dir", self.projects]
        if argv[0] in ("list", "send"):
            argv += ["--session", session or self.current]
        if now is not None:
            argv += ["--now", str(now)]
        out, err = io.StringIO(), io.StringIO()
        code = agentlog.main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def list(self, preview=True, **kw):
        args = ["list"] + (["--preview"] if preview else [])
        code, out, err = self.run(*args, **kw)
        if code != 0:
            raise AssertionError("list failed: %s" % err)
        return json.loads(out)

    def state(self):
        return agentlog.read_json(os.path.join(self.data, "state.json"), None)

    def pending(self):
        return agentlog.read_json(os.path.join(self.data, "pending.json"), None)


# ---------------------------------------------------------------- 偽サーバー


class FakeInbox(object):
    """契約 v2 の /v1/uploads・PUT・/v1/finish を模す（GCS の代わりにメモリに持つ）。"""

    CODE = "abcdefghijklmnopqrstuvwxyz"

    def __init__(self):
        self.objects = {}          # object -> {"body", "sha256", "content_type"}
        self.issued = {}           # token -> {"object", "headers"}
        self.upload_batches = []   # 1 回の /v1/uploads で来た件数
        self.put_attempts = {}     # object -> 回数
        self.fail_put = {}         # object の rel/sid を含む文字列 -> 先頭何回 503 にするか
        self.forbid_put = set()    # ここに含まれる文字列を持つ object は 403
        self.finish_bodies = []
        self.finish_lose_first = False
        self.uploads_status = None   # 例: (400, "invalid_argument")
        self.upload_calls = 0
        self.finished = False
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
                self._json(status, {"error": {"code": code, "message": "テスト用のエラー", "details": details or []}})

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n)

            def do_POST(self):
                body = self._body()
                if self.headers.get("Authorization") != "Bearer " + inbox.CODE:
                    return self._err(401, "unauthorized")
                req = json.loads(body.decode())
                if self.path == "/v1/uploads":
                    inbox.upload_calls += 1
                    if inbox.uploads_status:
                        return self._err(*inbox.uploads_status)
                    return self._uploads(req)
                if self.path == "/v1/finish":
                    return self._finish(req)
                return self._err(404, "not_found")

            def _uploads(self, req):
                files = req.get("files") or []
                if len(files) > 200:
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
                    out.append({"session_id": f["session_id"], "rel": f["rel"], "object": obj, "method": "PUT",
                                "url": "http://127.0.0.1:%d/put/%s?X-Goog-Signature=x" % (inbox.port, token),
                                "headers": headers})
                self._json(200, {"uploads": out})

            def do_PUT(self):
                token = self.path.split("/put/", 1)[-1].split("?", 1)[0]
                body = self._body()
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
