# -*- coding: utf-8 -*-
"""テスト用の合成の会話ログ・偽の受け口（契約 v3: /v1/kit・/v1/report）・偽の claude / Chrome / sips。

本物の会話ログは読まない。設定ディレクトリは毎回の一時ディレクトリで、そうなっていなければ止まる。
行の形（キー名・type・origin.kind・message.model / usage・compact_boundary など）だけを実データに合わせてある。
"""

import base64
import datetime
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
import unittest
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
KIT_DIR = os.path.join(HERE, "kit")
REPORT_RE = re.compile(r'<script type="application/json" id="report-data">(.*?)</script>', re.S)
FAKE_CLAUDE = os.path.join(HERE, "fake_claude.py")
FAKE_CHROME = os.path.join(HERE, "fake_chrome.py")
FAKE_SIPS = os.path.join(HERE, "fake_sips.py")
CODE = "abcdefghijklmnopqrstuvwxyz"
# 判定・レビュー役は偽の claude -p で（本物の claude は呼ばない。子プロセスで動かす agentlog.py にも env で効く）
os.environ[agentlog.JUDGE_BIN_ENV] = FAKE_CLAUDE
for _k in ("FAKE_JUDGE", "FAKE_REVIEW", "FAKE_CHROME"):
    os.environ.pop(_k, None)


def load_kit(version="v3"):
    files = {}
    for name in agentlog.KIT_FILES:
        with open(os.path.join(KIT_DIR, name), encoding="utf-8") as f:
            files[name] = json.load(f) if name.endswith(".json") else f.read()
    return {"version": version, "files": files}


def assert_sandboxed():
    """読む会話ログが一時ディレクトリであること。違えば全体を止める。"""
    p = os.path.realpath(agentlog.projects_dir())
    if not p.startswith(TMP_ROOT + os.sep) or p.startswith(REAL_CONFIG):
        raise SystemExit("テストの projects が一時ディレクトリではない: %s" % p)


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_uuid():
    return str(uuidlib.uuid4())


def usage(i=0, o=0, cr=0, c5=0, c1=0):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": c5 + c1,
            "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": c1}}


class Lines(object):
    """会話 1 本ぶんの行を組み立てる。時刻は base から 5 秒ずつ進む（明示もできる）。"""

    def __init__(self, sid=None, base=None, cwd=CWD, entrypoint="cli"):
        self.sid = sid or new_uuid()
        self.base = base if base is not None else time.time() - 3600
        self.cwd, self.entrypoint = cwd, entrypoint
        self.rows, self.parent, self.tick = [], None, 0

    def _ts(self, at):
        if at is None:
            self.tick += 1
            at = self.base + self.tick * 5
        return iso(at)

    def msg(self, typ, content, at=None, **extra):
        uid = new_uuid()
        row = {"parentUuid": self.parent, "isSidechain": False, "type": typ, "message": {"role": typ, "content": content},
               "uuid": uid, "timestamp": self._ts(at), "userType": "external", "entrypoint": self.entrypoint,
               "cwd": self.cwd, "sessionId": self.sid, "version": "2.1.294", "gitBranch": "main"}
        row.update(extra)
        self.rows.append(row)
        self.parent = uid
        return self

    def user(self, text, **kw):
        return self.msg("user", text, **kw)

    def ai(self, content, model="claude-opus-5-5", use=None, mid=None, at=None, **extra):
        """assistant の 1 行（message.model・id・usage 付き）。"""
        self.msg("assistant", content, at=at, effort="high", **extra)
        self.rows[-1]["message"].update({"model": model, "id": mid or "msg_" + new_uuid().replace("-", "")[:24],
                                         "usage": use or usage()})
        return self

    def assistant(self, text="了解", **kw):
        return self.ai([{"type": "text", "text": text}], **kw)

    def tool(self, name, inp, result="ok", tur=None, **kw):
        """assistant の tool_use と、その結果の user 行。"""
        tid = "toolu_" + new_uuid().replace("-", "")[:20]
        self.ai([{"type": "tool_use", "id": tid, "name": name, "input": inp}], **kw)
        return self.msg("user", [{"type": "tool_result", "tool_use_id": tid, "content": result}],
                        toolUseResult=tur if tur is not None else {"stdout": result})

    def artifact(self, path, **inp):
        return self.tool("Artifact", dict({"file_path": path, "title": "ページ", "icon": "doc"}, **inp),
                         result="published https://claude.ai/code/artifact/x")

    def compact(self, trigger="auto", args=""):
        if trigger == "manual":
            self.user("<command-name>/compact</command-name>\n<command-message>compact</command-message>\n"
                      "<command-args>%s</command-args>" % args)
        self.rows.append({"parentUuid": None, "logicalParentUuid": self.parent, "isSidechain": False, "type": "system",
                          "subtype": "compact_boundary", "content": "Conversation compacted", "isMeta": False,
                          "timestamp": self._ts(None), "uuid": new_uuid(), "level": "info", "sessionId": self.sid,
                          "compactMetadata": {"trigger": trigger, "preTokens": 180000}})
        return self.user("This session is being continued from a previous conversation. 要約の本文", isMeta=True,
                         isCompactSummary=True)

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
    """一時の設定ディレクトリ（projects/・プラグインのデータ・.claude.json）を作り、CLI をこのプロセスの中で呼ぶ。

    いまの会話（self.current）は「最初の指示が /send-to-nobu」の会話として projects/ に置く。
    """

    SEND_PROJECT = "-Users-alice"

    def __init__(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="stn-test-"))
        self.projects = os.path.join(self.tmp, "projects")
        self.data = os.path.join(self.tmp, "plugins", "data", "send-to-nobu-androots")
        self.answers = os.path.join(self.tmp, "scratchpad")     # AI が ArtifactData の get に渡す out_dir
        self.work = os.path.join(self.tmp, "work")              # アーティファクトの .html を置く場所
        os.makedirs(self.projects)
        os.makedirs(self.work)
        with open(os.path.join(self.tmp, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "alice@example.co.jp"}}, f)
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

    def html(self, name, body):
        """アーティファクトの .html を置く。パスを返す。"""
        p = os.path.join(self.work, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write('<!doctype html><html><head><meta charset="utf-8"><title>%s</title></head><body>%s</body></html>'
                    % (name, body))
        return p

    def start_send_session(self):
        """新しい会話で /send-to-nobu を打った状態を作る。会話 ID を返す。"""
        L = Lines(base=time.time() - 120).user(SEND_CMD).user("スキル本文", isMeta=True).assistant("作る")
        self.write(L, project=self.SEND_PROJECT)
        return L.sid

    def decide(self, exclude=(), hide_shots=(), round=None, raw=None):
        """本人がページで「送る」を押し、AI が db の send/decision をファイルに保存した状態を作る。"""
        doc = raw if raw is not None else {"round": round or self.pending()["round"], "exclude": list(exclude),
                                           "hide_shots": list(hide_shots), "at": "2026-10-16T07:30:00+09:00"}
        path = agentlog.decision_path(self.answers)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)

    def run(self, *argv, session=None, now=None):
        """CLI を呼ぶ。いまの会話 ID は env だけ、時刻は agentlog._now の差し替え。(終了コード, 出力の JSON)。"""
        assert_sandboxed()
        argv = list(argv) + ["--data-dir", self.data]
        if argv[0] == "send" and "--answer-dir" not in argv:
            argv += ["--answer-dir", self.answers]
        saved_env, saved_now = os.environ.get("CLAUDE_CODE_SESSION_ID"), agentlog._now
        os.environ["CLAUDE_CODE_SESSION_ID"] = session or self.current
        if now is not None:
            agentlog._now = lambda: now
        out = io.StringIO()
        try:
            code = agentlog.main(argv, stdout=out)
        finally:
            agentlog._now = saved_now
            if saved_env is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved_env
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else {})

    def page_html(self):
        d = os.path.join(self.data, "page")
        names = [n for n in os.listdir(d) if n.endswith(".html")]
        assert len(names) == 1, names
        with open(os.path.join(d, names[0]), encoding="utf-8") as f:
            return f.read()

    def page_data(self):
        """ページに入れたレポート（画像の data URI 込み）。"""
        return json.loads(REPORT_RE.search(self.page_html()).group(1))

    def state(self):
        return agentlog.read_json(os.path.join(self.data, "state.json"), None)

    def pending(self):
        return agentlog.read_json(os.path.join(self.data, "pending.json"), None)

    def last(self):
        return agentlog.read_json(os.path.join(self.data, "last-prepare.json"), None)

    def next_day(self):
        """翌朝、新しい会話で /send-to-nobu を打つ。"""
        self.current = self.start_send_session()


# ---------------------------------------------------------------- 偽の受け口


IMAGE_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}\.jpg$")


class FakeInbox(object):
    """契約 v3 の GET /v1/kit と POST /v1/report を模す。"""

    def __init__(self):
        self.kit = load_kit()
        self.kit_mode = None           # unavailable（503）・broken（200 だが形が違う）
        self.kit_requests = []         # 受け取った If-None-Match
        self.reports = []              # 受け取った本文
        self.finished = False
        self.lose_first = False        # 受領票は置けたが返事が届かなかった（502）
        inbox = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, obj, headers=()):
                data = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _err(self, status, code):
                self._json(status, {"error": {"code": code, "message": "テスト用のエラー"}})

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer " + CODE:
                    return self._err(401, "unauthorized")
                if self.path != "/v1/kit":
                    return self._err(404, "not_found")
                inbox.kit_requests.append(self.headers.get("If-None-Match"))
                if inbox.kit_mode == "unavailable":
                    return self._err(503, "kit_unavailable")
                etag = '"%s"' % inbox.kit["version"]
                if inbox.kit_mode == "broken":
                    return self._json(200, {"version": "v9", "files": {"page.html": "x"}}, [("ETag", '"v9"')])
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                self._json(200, inbox.kit, [("ETag", etag)])

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Authorization") != "Bearer " + CODE:
                    return self._err(401, "unauthorized")
                if self.path != "/v1/report":
                    return self._err(404, "not_found")
                if len(body) > 12 * 1024 * 1024:
                    return self._err(413, "too_large")
                req = json.loads(body.decode())
                keys = {"report", "images", "plugin_version", "kit_version", "removed_count", "range"}
                if set(req) != keys:
                    return self._err(400, "invalid_argument")       # 契約に無いキー・足りないキーは 400
                rep = req["report"]
                if not isinstance(rep, dict) or type(rep.get("v")) is not int or not isinstance(rep.get("convs"), list):
                    return self._err(400, "invalid_argument")
                if not isinstance(req["images"], list) or len(req["images"]) > 16:
                    return self._err(400, "invalid_argument")
                for im in req["images"]:
                    if set(im) != {"name", "data_base64", "sha256"} or not IMAGE_NAME_RE.match(im["name"]):
                        return self._err(400, "invalid_argument")
                    data = base64.b64decode(im["data_base64"])
                    if (not data.startswith(b"\xff\xd8\xff") or len(data) > 2 * 1024 * 1024
                            or hashlib.sha256(data).hexdigest() != im["sha256"]):
                        return self._err(400, "invalid_image")
                if inbox.finished:
                    return self._err(409, "already_finished")
                inbox.reports.append(req)
                inbox.finished = True
                if inbox.lose_first and len(inbox.reports) == 1:
                    return self._err(502, "bad_gateway")
                self._json(200, {"report_id": "20261016T073000Z-1a2b3c4d",
                                 "object": "reports/androots/alice/20261016T073000Z-1a2b3c4d.json",
                                 "image_count": len(req["images"])})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def body(self):
        assert len(self.reports) == 1, len(self.reports)
        return self.reports[0]


class Patched(object):
    """偽の受け口・偽の Chrome と sips に向けて差し替える（テストコードからだけ）。"""

    def __init__(self, inbox):
        self.saved = (agentlog.ALLOWED_API_BASES, agentlog.BACKOFF_BASE, agentlog._OPENER, agentlog.CHROME,
                      agentlog.SIPS)
        agentlog.ALLOWED_API_BASES = (inbox.base,)
        agentlog.BACKOFF_BASE = 0.001
        agentlog._OPENER = agentlog.build_opener(use_proxy=False)
        agentlog.CHROME, agentlog.SIPS = FAKE_CHROME, FAKE_SIPS

    def restore(self):
        (agentlog.ALLOWED_API_BASES, agentlog.BACKOFF_BASE, agentlog._OPENER, agentlog.CHROME,
         agentlog.SIPS) = self.saved


def image_bytes(data_uri):
    return base64.b64decode(data_uri.split(",", 1)[1])


class Base(unittest.TestCase):
    """一時の世界・偽の受け口・偽の claude を用意する。"""

    def setUp(self):
        self.w = World()
        self.inbox = FakeInbox()
        self.patch = Patched(self.inbox)
        self.record = os.path.join(self.w.tmp, "claude-calls.jsonl")
        self.fake_judge()
        self.fake_review()

    def tearDown(self):
        for k in ("FAKE_JUDGE", "FAKE_REVIEW", "FAKE_CHROME"):
            os.environ.pop(k, None)
        os.environ[agentlog.JUDGE_BIN_ENV] = FAKE_CLAUDE
        self.patch.restore()
        self.inbox.close()
        self.w.close()

    def fake_judge(self, **conf):
        os.environ["FAKE_JUDGE"] = json.dumps(dict(conf, record=self.record), ensure_ascii=False)

    def fake_review(self, **conf):
        conf.setdefault("counter", os.path.join(self.w.tmp, "review-count-%s" % new_uuid()))
        os.environ["FAKE_REVIEW"] = json.dumps(dict(conf, record=self.record), ensure_ascii=False)

    def calls(self, kind=None):
        """偽の claude が受け取った呼び出し [{kind, argv, stdin, judge_env, cwd}]。"""
        try:
            with open(self.record, encoding="utf-8") as f:
                rows = [json.loads(x) for x in f]
        except OSError:
            return []
        return [r for r in rows if kind is None or r["kind"] == kind]

    def review_text(self):
        """いちばん新しいレビュー役の入力。"""
        return self.calls("review")[-1]["stdin"]

    def conv(self, *prompts, **kw):
        """人の指示 prompts の会話を 1 本置く。(Lines, パス)。"""
        L = Lines(**kw)
        for p in prompts:
            L.user(p).assistant()
        return L, self.w.write(L)

    def prepare(self, **kw):
        return self.w.run("prepare", "--code", CODE, "--api-base", self.inbox.base, **kw)

    def prepared(self, **kw):
        code, res = self.prepare(**kw)
        if code != 0:
            raise AssertionError("prepare failed: %s" % res)
        return res

    def send(self, **kw):
        return self.w.run("send", "--code", CODE, "--api-base", self.inbox.base, **kw)

    def report(self):
        """控えのレポート（ページに入れたもの・画像なし）。"""
        return self.w.pending()["report"]
