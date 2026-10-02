# -*- coding: utf-8 -*-
"""send-to-nobu 0.5 の対話 e2e（本物の claude を疑似端末で動かし、キー入力で選択画面に答える）。

- 設定ディレクトリ・HOME は毎回の一時ディレクトリ。会話ログは合成。準備が 1 つでも失敗したら止まる
- モデルは台本どおりに動く偽の Anthropic API（ANTHROPIC_BASE_URL）。スキルの手順どおりに道具を呼ぶだけ
- MCP（whoami / start_submission）と受け口（/v1/uploads・PUT・/v1/finish）も偽物。本物のサーバーには送らない
- プラグインは一時ディレクトリへの複製（送り先の許可リストと .mcp.json の URL だけを偽物に向ける）
- AI の確認（判定用の claude -p）も偽物（tests/fake_claude.py）。ふだんは候補なし

  /usr/bin/python3 dev/e2e_tty.py [scenario ...]
    none     「なし（全部送る）」と入力欄の感想 → 全部届く・感想はそのまま
    numbers  入力欄に番号 → その会話は届かない
    suggest  AI の確認が「個人的な相談」を候補にする → 1 問目に目印 → 「提案どおり外す」でその会話だけ届かない
    reask    読めない番号 → 同じターンで聞き直す → AI のメモ（ヒアドキュメント）も許可の確認なしで届く
    afk      答えずに放置（askUserQuestionTimeout 60s）→ 何も届かない
    control  対照: allowed-tools から Bash を外すと許可ダイアログを検出する（検出の仕組みが効いている確認）
    many     60 件（長いタイトル）を 24 行 × 80 桁（macOS の Terminal の既定）で 1 回の選択画面に出す。最後の会話・
             質問の行・選択肢が見え、PageUp で 1 件目と見出しまで戻れる → そのまま答えて全部届く
"""
import fcntl
import http.server
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import threading
import time
import unicodedata
import uuid

REPO_PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "send-to-nobu")
sys.path.insert(0, os.path.join(REPO_PLUGIN, "tests"))
from helpers import FakeInbox, Lines  # noqa: E402

REAL_CLAUDE = os.path.realpath(os.path.expanduser("~/.claude"))
TMP_ROOT = os.path.realpath(tempfile.gettempdir())
KEY = "sk-ant-api03-" + "Q1w2E3r4T5y6U7i8O9p0" * 4


def die(msg):
    print("SETUP FAILED: " + msg)
    sys.exit(2)


# ---------------------------------------------------------------- 偽の MCP


class FakeMCP(object):
    def __init__(self, inbox):
        self.calls = []
        mcp = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_DELETE(self):
                self.do_GET()

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if "id" not in req:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                m, p = req.get("method"), req.get("params") or {}
                if m == "initialize":
                    res = {"protocolVersion": p.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                           "serverInfo": {"name": "agent-log-inbox-fake", "version": "0"}}
                elif m == "tools/list":
                    res = {"tools": [
                        {"name": "whoami", "description": "ログイン中の人", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "start_submission", "description": "引換券を発行する",
                         "inputSchema": {"type": "object", "properties": {
                             "tool": {"type": "string", "enum": ["claude-code"], "default": "claude-code"}}}}]}
                elif m == "tools/call":
                    mcp.calls.append(p.get("name"))
                    out = ({"email": "alice@example.com", "client": "androots", "person": "alice"}
                           if p.get("name") == "whoami" else
                           {"submission_id": "20260929T000000Z-e2e", "upload_code": FakeInbox.CODE,
                            "expires_at": "2099-01-01T00:00:00Z", "api_base": inbox.base})
                    res = {"content": [{"type": "text", "text": json.dumps(out)}], "structuredContent": out}
                else:
                    res = {}
                data = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


# ---------------------------------------------------------------- 偽のモデル（スキルの手順どおりに道具を呼ぶ）


def _blocks(msg):
    c = msg.get("content")
    return [{"type": "text", "text": c}] if isinstance(c, str) else (c or [])


def _result_text(b):
    c = b.get("content")
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c or ""


class FakeModel(object):
    def __init__(self):
        self.log = []       # (道具の名前, 入力の要約)
        self.paths = []
        model = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                model.paths.append("GET " + self.path)
                self._send(200, b'{"data": []}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                model.paths.append("POST " + self.path)
                if "count_tokens" in self.path:
                    return self._send(200, b'{"input_tokens": 100}')
                if not self.path.startswith("/v1/messages"):
                    return self._send(404, b'{"type":"error","error":{"type":"not_found_error","message":"x"}}')
                content, stop = model.respond(body)
                msg = {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant",
                       "model": body.get("model", "claude-e2e"), "content": content, "stop_reason": stop,
                       "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10,
                                                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
                if not body.get("stream"):
                    return self._send(200, json.dumps(msg).encode())
                ev = [("message_start", {"type": "message_start", "message": dict(msg, content=[], stop_reason=None)})]
                for i, blk in enumerate(content):
                    if blk["type"] == "text":
                        ev += [("content_block_start", {"type": "content_block_start", "index": i,
                                                        "content_block": {"type": "text", "text": ""}}),
                               ("content_block_delta", {"type": "content_block_delta", "index": i,
                                                        "delta": {"type": "text_delta", "text": blk["text"]}})]
                    else:
                        ev += [("content_block_start", {"type": "content_block_start", "index": i, "content_block":
                                                        {"type": "tool_use", "id": blk["id"], "name": blk["name"],
                                                         "input": {}}}),
                               ("content_block_delta", {"type": "content_block_delta", "index": i, "delta":
                                                        {"type": "input_json_delta",
                                                         "partial_json": json.dumps(blk["input"], ensure_ascii=False)}})]
                    ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
                ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                          "usage": {"output_tokens": 10}}),
                       ("message_stop", {"type": "message_stop"})]
                data = "".join("event: %s\ndata: %s\n\n" % (n, json.dumps(d, ensure_ascii=False)) for n, d in ev)
                self._send(200, data.encode("utf-8"), "text/event-stream")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tool(self, name, inp):
        self.log.append((name, inp.get("command", "")[:160] if name == "Bash" else json.dumps(inp, ensure_ascii=False)[:160]))
        return [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24], "name": name, "input": inp}], "tool_use"

    def text(self, t):
        self.log.append(("TEXT", t))
        return [{"type": "text", "text": t}], "end_turn"

    def respond(self, body):
        tools = {t.get("name") for t in body.get("tools") or []}
        msgs = body.get("messages") or []
        if len(tools) < 3 or not msgs:
            return [{"type": "text", "text": "ok"}], "end_turn"     # 題名づけなど、横の呼び出し
        all_text = "\n".join(b.get("text", "") for m in msgs if m.get("role") == "user"
                             for b in _blocks(m) if b.get("type") == "text")
        m = re.search(r"(/usr/bin/python3 \S+/scripts/agentlog\.py) list --data-dir (\S+)", all_text)
        if not m:
            return self.text("（スキルが読み込まれていない）")
        script, data = m.group(1), m.group(2).rstrip("`")
        start = [t for t in tools if t.endswith("agent-log-inbox__start_submission")]
        uses = {b["id"]: b for mm in msgs if mm.get("role") == "assistant" for b in _blocks(mm) if b.get("type") == "tool_use"}
        if len(self.log) > 30:
            return self.text("（道具を呼びすぎた）")
        results = []        # 最後の assistant より後ろの user メッセージにある結果
        for mm in reversed(msgs):
            if mm.get("role") == "assistant":
                break
            results = [b for b in _blocks(mm) if b.get("type") == "tool_result"] + results
        if not results:
            if start:
                return self.tool(start[0], {"tool": "claude-code"})
            if "ToolSearch" in tools:
                return self.tool("ToolSearch", {"query": "select:mcp__plugin_send-to-nobu_agent-log-inbox__start_submission",
                                                "max_results": 1})
            return self.text("（start_submission が無い）")
        r = results[-1]
        prev = uses.get(r.get("tool_use_id")) or {}
        name, text = prev.get("name", ""), _result_text(r)
        code = None
        for mm in msgs:     # いちばん新しい引換券
            for b in _blocks(mm):
                if b.get("type") == "tool_result" and '"upload_code"' in _result_text(b):
                    got = json.loads(re.search(r"\{.*\}", _result_text(b), re.S).group(0))
                    code = got if "upload_code" in got else code
        send_cmd = "%s send --data-dir %s --code %s --api-base %s" % (
            script, data, code and code["upload_code"], code and code["api_base"])
        if name == "ToolSearch":
            return self.tool("mcp__plugin_send-to-nobu_agent-log-inbox__start_submission", {"tool": "claude-code"})
        if name.endswith("start_submission"):
            if len([1 for mm in msgs for b in _blocks(mm) if b.get("type") == "tool_use"
                    and b.get("name", "").endswith("start_submission")]) > 1:
                return self.tool("Bash", {"command": send_cmd, "description": "送る"})
            return self.tool("Bash", {"command": "%s list --data-dir %s" % (script, data), "description": "一覧を出す"})
        if name == "AskUserQuestion":
            asks = [1 for mm in msgs for b in _blocks(mm) if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion"]
            if len(asks) > 1:
                send_cmd += " --assistant-note - <<'NOTE'\n選択画面の番号が読めず 1 回聞き直した\nNOTE"
            return self.tool("Bash", {"command": send_cmd, "description": "送る"})
        if name == "Bash":
            try:
                out = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
            except Exception:
                return self.text("（出力が読めない）" + text[:200])
            if "ask" in out:
                content, stop = self.tool("AskUserQuestion", {"questions": out["ask"]["questions"]})
                if out.get("say"):      # 本人に伝えてから、同じメッセージで聞き直す
                    self.log.append(("SAY", out["say"]))
                    content = [{"type": "text", "text": out["say"]}] + content
                return content, stop
            if "start_submission" in out.get("next", "") and start:
                return self.tool(start[0], {"tool": "claude-code"})
            return self.text(out.get("say") or ("（say が無い）" + text[:200]))
        return self.text("（想定外の道具 %s）" % name)


# ---------------------------------------------------------------- 疑似端末


def screen(buf):
    t = buf.decode("utf-8", "replace")
    t = re.sub(r"\x1b\[(\d*)C", lambda m: " " * int(m.group(1) or 1), t)
    t = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", t)
    t = re.sub(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]", "", t)
    t = re.sub(r"\x1b[()][0-9A-Za-z]|\x1b[=>78]", "", t)
    return t


class Screen(object):
    """最後の画面（行 × 桁）を再現する小さな端末もどき。全角は 2 桁。"""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.g = [[" "] * cols for _ in range(rows)]
        self.r = self.c = 0
        self.back = []          # 画面の上に押し出された行（端末のスクロールで戻って見られる）
        self.cleared_back = 0   # スクロールの履歴を消した回数（ESC [3J）

    def _scroll(self):
        self.back.append(self.g.pop(0))
        self.g.append([" "] * self.cols)
        self.r = self.rows - 1

    def feed(self, data):
        t = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", data.decode("utf-8", "replace"))
        i = 0
        while i < len(t):
            ch = t[i]
            if ch == "\x1b":
                m = re.match(r"\x1b\[([0-9;?<>=]*)([ -/]*)([@-~])", t[i:])
                if not m:
                    i += 2
                    continue
                i += m.end()
                args = [int(x) if x.isdigit() else 0 for x in m.group(1).lstrip("?<>=").split(";")] if m.group(1) else []
                n = (args[0] if args and args[0] else 1)
                f = m.group(3)
                if f == "A":
                    self.r = max(0, self.r - n)
                elif f == "B":
                    self.r = min(self.rows - 1, self.r + n)
                elif f == "C":
                    self.c = min(self.cols - 1, self.c + n)
                elif f == "D":
                    self.c = max(0, self.c - n)
                elif f == "G":
                    self.c = min(self.cols - 1, n - 1)
                elif f in "Hf":
                    self.r = min(self.rows - 1, (args[0] if args and args[0] else 1) - 1)
                    self.c = min(self.cols - 1, (args[1] if len(args) > 1 and args[1] else 1) - 1)
                elif f == "J":
                    mode = args[0] if args else 0
                    if mode == 0:
                        self.g[self.r][self.c:] = [" "] * (self.cols - self.c)
                        for rr in range(self.r + 1, self.rows):
                            self.g[rr] = [" "] * self.cols
                    elif mode == 2:
                        self.g = [[" "] * self.cols for _ in range(self.rows)]
                    elif mode == 3:
                        self.back, self.cleared_back = [], self.cleared_back + 1
                elif f == "K":
                    mode = args[0] if args else 0
                    if mode == 0:
                        self.g[self.r][self.c:] = [" "] * (self.cols - self.c)
                    elif mode == 2:
                        self.g[self.r] = [" "] * self.cols
                continue
            i += 1
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                self.r += 1
                if self.r >= self.rows:
                    self._scroll()
            elif ch == "\x08":
                self.c = max(0, self.c - 1)
            elif ch >= " ":
                w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
                if self.c + w > self.cols:
                    self.c = 0
                    self.r += 1
                    if self.r >= self.rows:
                        self._scroll()
                self.g[self.r][self.c] = ch
                if w == 2 and self.c + 1 < self.cols:
                    self.g[self.r][self.c + 1] = ""
                self.c += w

    def text(self, back=False):
        return "\n".join("".join(row).rstrip() for row in (self.back if back else []) + self.g)


def flat(s):
    return re.sub(r"\s+", "", s)


class Term(object):
    def __init__(self, argv, env, cwd, rows=60, cols=140):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)
        self.buf = b""
        self.rows, self.cols = rows, cols
        self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while self.alive:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                with self.lock:
                    self.buf += data

    def screen(self, back=False):
        """いま端末に見えている画面（back なら、スクロールで戻って見られる行も）。"""
        return self.emulate().text(back)

    def emulate(self):
        sc = Screen(self.rows, self.cols)
        with self.lock:
            sc.feed(self.buf)
        return sc

    def mark(self):
        with self.lock:
            return len(self.buf)

    def text(self, since=0):
        with self.lock:
            return screen(self.buf[since:])

    def wait(self, *needles, since=0, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = flat(self.text(since))
            for k in range(2):      # 流れてきた文字列と、いまの画面（狭い端末では文字が飛び飛びに描かれる）の両方で探す
                for n in needles:
                    if flat(n) in s:
                        return n
                s = flat(self.screen()) if k == 0 else s
            time.sleep(0.3)
        return None

    def send(self, data, delay=0.4):
        os.write(self.fd, data if isinstance(data, bytes) else data.encode("utf-8"))
        time.sleep(delay)

    def type(self, s):
        for ch in s:
            self.send(ch, 0.03)

    def close(self):
        self.alive = False
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except OSError:
                return
            for _ in range(20):
                if os.waitpid(self.pid, os.WNOHANG)[0]:
                    return
                time.sleep(0.2)


# ---------------------------------------------------------------- 準備


def make_world(root, inbox, mcp, settings_extra=None, control=False, many=0, judge_flags=False):
    home = os.path.join(root, "home")
    cfg = os.path.join(root, "cfg")
    work = os.path.join(home, "work")
    plugin = os.path.join(root, "plugin", "send-to-nobu")
    for d in (home, cfg, work):
        os.makedirs(d)
    # プラグインの複製: 送り先の許可リストと .mcp.json の URL だけを偽物に向ける
    shutil.copytree(REPO_PLUGIN, plugin, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    sp = os.path.join(plugin, "scripts", "agentlog.py")
    s = open(sp, encoding="utf-8").read()
    for old, new in (('ALLOWED_API_BASES = ("https://agent-log-inbox-mcp.androots.co.jp",)',
                      'ALLOWED_API_BASES = ("%s",)' % inbox.base),
                     ('ALLOWED_PUT_PREFIXES = ("https://storage.googleapis.com/",)',
                      'ALLOWED_PUT_PREFIXES = ("%s/put/",)' % inbox.base)):
        if s.count(old) != 1:
            die("送り先の定数が見つからない")
        s = s.replace(old, new)
    open(sp, "w", encoding="utf-8").write(s)
    if control:
        kp = os.path.join(plugin, "skills", "send-to-nobu", "SKILL.md")
        k = open(kp, encoding="utf-8").read()
        line = "  - Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py *)\n"
        if k.count(line) != 1:
            die("対照実験: allowed-tools の Bash 行が見つからない")
        open(kp, "w", encoding="utf-8").write(k.replace(line, ""))
    json.dump({"mcpServers": {"agent-log-inbox": {"type": "http", "url": mcp.url}}},
              open(os.path.join(plugin, ".mcp.json"), "w"))
    settings = {"apiKeyHelper": "echo e2e-dummy-key", "permissions": {"defaultMode": "default"},
                "cleanupPeriodDays": 30}
    settings.update(settings_extra or {})
    json.dump(settings, open(os.path.join(cfg, "settings.json"), "w"))
    json.dump({"hasCompletedOnboarding": True, "theme": "dark", "numStartups": 5,
               "projects": {os.path.realpath(work): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}},
              open(os.path.join(cfg, ".claude.json"), "w"))
    # 合成の会話ログ
    proj = os.path.join(cfg, "projects", "-Users-alice-work-billing")
    os.makedirs(proj)
    now = time.time()
    convs = []

    def put(L):
        with open(os.path.join(proj, L.sid + ".jsonl"), "wb") as f:
            f.write(L.encode())
        convs.append(L)
        return os.path.join(proj, L.sid + ".jsonl")

    a = Lines(base=now - 9000).user("請求書の CSV を集計して。キーは %s" % KEY).assistant("読みます")
    a.user([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo" * 300}},
            {"type": "text", "text": "このスクショも見て"}]).assistant("見ました")
    a.meta("ai-title", aiTitle="請求書の集計")
    pa = put(a)
    sub = os.path.join(pa[:-6], "subagents")
    os.makedirs(sub)
    with open(os.path.join(sub, "agent-abc.jsonl"), "wb") as f:
        f.write(Lines(base=now - 8900).user("サブの指示").assistant("完了").encode())
    put(Lines(base=now - 7000).user("議事録のテンプレートを作って").assistant().meta("ai-title", aiTitle="議事録テンプレート"))
    put(Lines(base=now - 5000).user("個人的な相談なんだけど").assistant().meta("ai-title", aiTitle="個人的な相談"))
    d = Lines(base=now - 3000).user("取引先リストを整理して")
    d.tool("Bash", {"command": "cat list.csv"}, result="山田商事,03-1234-5678").assistant()
    put(d.meta("ai-title", aiTitle="取引先リストの整理"))
    for i in range(many):
        put(Lines(base=now - 20000 + i * 60).user("指示 %d" % i).assistant().meta(
            "ai-title", aiTitle="とても長いタイトルの会話で、どれが何の話かを見分けるための説明 %02d" % i))
    # 出さない会話: 前の送信用の会話・自動実行
    old_send = Lines(base=now - 6000).user("<command-message>send-to-nobu</command-message>\n"
                                           "<command-name>/send-to-nobu</command-name>").assistant("一覧")
    put(old_send)
    put(Lines(base=now - 4000, entrypoint="sdk-cli").user("自動実行のプロンプト").assistant())
    # 安全確認: 読む projects は一時ディレクトリ
    real = os.path.realpath(os.path.join(cfg, "projects"))
    if not real.startswith(TMP_ROOT + os.sep) or real.startswith(REAL_CLAUDE) or REAL_CLAUDE in real:
        die("projects が一時ディレクトリではない: " + real)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC", "OTEL")) and k not in ("CLAUDECODE",)}
    env.update(HOME=home, CLAUDE_CONFIG_DIR=cfg, TERM="xterm-256color", LANG="ja_JP.UTF-8",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1",
               SEND_TO_NOBU_CLAUDE=os.path.join(REPO_PLUGIN, "tests", "fake_claude.py"),
               FAKE_JUDGE=json.dumps({"flag": {"個人的な相談": "私的な話"}} if judge_flags else {}, ensure_ascii=False))
    if env["CLAUDE_CONFIG_DIR"] != cfg or not os.path.realpath(env["HOME"]).startswith(TMP_ROOT):
        die("env が一時ディレクトリを指していない")
    return {"home": home, "cfg": cfg, "work": work, "plugin": plugin, "env": env,
            "ids": {"a": a.sid, "b": convs[1].sid, "c": convs[2].sid, "d": convs[3].sid}}


PERMISSION_WORDS = ("Do you want to proceed", "Do you want to allow", "don't ask again", "Allow this", "Allow once")


def run_scenario(name):
    root = os.path.realpath(tempfile.mkdtemp(prefix="stn-e2e-%s-" % name))
    inbox, model = FakeInbox(), FakeModel()
    mcp = FakeMCP(inbox)
    extra = {"askUserQuestionTimeout": "60s"} if name == "afk" else {}
    w = make_world(root, inbox, mcp, extra, control=(name == "control"), many=56 if name == "many" else 0,
                   judge_flags=(name == "suggest"))
    model_env = dict(w["env"], ANTHROPIC_BASE_URL=model.base)
    term = Term(["claude", "--plugin-dir", w["plugin"]], model_env, w["work"], rows=24 if name == "many" else 60, cols=80 if name == "many" else 140)
    ok, notes = True, []

    def check(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        notes.append(("PASS " if cond else "FAIL ") + msg)
        print("  [%s] %s" % (name, notes[-1]), flush=True)

    try:
        if not term.wait("❯", timeout=40):
            die("claude の入力欄が出ない:\n" + term.text()[-1500:])
        # 2.1.28x の初回の案内（auto mode を既定にするか）。手動のまま（許可ダイアログを見るため）。出るまで少しかかる
        if term.wait("Make auto mode your default", timeout=8):
            time.sleep(1.0)
            term.send(b"\x1b[B", 0.3)
            term.send(b"\r", 1.0)
        time.sleep(2.0)
        m0 = term.mark()
        term.type("/send-to-nobu")
        time.sleep(1.0)
        term.send(b"\r", 1.0)
        found = term.wait("送らない会話は", "送る会話はない", *PERMISSION_WORDS, since=m0, timeout=60)
        if name == "control":
            check(found in PERMISSION_WORDS, "対照: allowed-tools に無い Bash では許可ダイアログを検出できる（%s）" % found)
            raise StopIteration
        check(found == "送らない会話は", "選択画面が出た（%s）" % found)
        if found != "送らない会話は":
            raise RuntimeError("no screen")
        shown = term.text(m0)
        check("請求書の集計" in shown, "1 問目に一覧のタイトルが出る")
        if name == "suggest":
            check(flat("【候補: 私的な話】個人的な相談") in flat(shown) and "提案どおり外す（3）" in flat(shown),
                  "AI の候補に目印と理由が付き、「提案どおり外す（3）」が出る")
        if name == "many":
            term.wait("外さずに送る", since=m0, timeout=10)
            time.sleep(1.5)
            sc = term.screen()
            check("60." in sc and "送らない会話は" in sc and "外さずに送る" in sc,
                  "24 行の画面に 60 件目・質問の行・選択肢が見える")
            check("ほかに" not in sc, "60 件なら「ほかに N 件」は出ない")
            seen = sc
            for _ in range(12):                 # 上へスクロール（PageUp）して、1 件目と見出しまで戻れるか
                term.send(b"\x1b[5~", 0.8)
                seen += "\n" + term.screen()
                if "未送信の会話が 60 件" in seen:
                    break
            check("未送信の会話が 60 件" in seen and "\n│ 1. " in seen,
                  "PageUp で 1 件目と見出しまで戻って見られる")
            for _ in range(12):
                term.send(b"\x1b[6~", 0.5)     # 元の位置へ（PageDown）
        if name == "afk":
            done = term.wait("答えがなかった", *PERMISSION_WORDS, since=m0, timeout=150)
        else:
            if name in ("none", "many", "suggest"):
                term.send(b"\r", 1.0)                      # 1 つ目の選択肢（なし（全部送る）/ 提案どおり外す）
            else:
                term.send(b"\x1b[B", 0.3)
                term.send(b"\x1b[B", 0.3)                  # 入力欄
                term.type("3番以外" if name == "reask" else "3")
                term.send(b"\r", 1.0)
            m1 = term.mark()
            if not term.wait("わからなかったこと・質問も", since=m0, timeout=20):
                raise RuntimeError("no Q2")
            if name == "none":
                term.send(b"\x1b[B", 0.3)
                term.send(b"\x1b[B", 0.3)
                term.type("e2e の感想です。MCP のログインで迷った")
                term.send(b"\r", 1.0)
            else:
                term.send(b"\r", 1.0)                      # 特になし
            m2 = term.mark()
            if term.wait("Submit", since=m1, timeout=10):
                m2 = term.mark()
                term.send(b"\r", 1.0)
            if name == "reask":
                again = term.wait("番号として読めなかった", *PERMISSION_WORDS, since=m2, timeout=60)
                check(again == "番号として読めなかった", "読めない番号は同じターンで聞き直す（%s）" % again)
                time.sleep(2.0)     # 伝える文と同じメッセージで選択画面が開く
                term.send(b"\r", 1.0)
                term.wait("わからなかったこと・質問も", since=m2, timeout=20)
                term.send(b"\r", 1.0)
                if term.wait("Submit", since=m2, timeout=10):
                    term.send(b"\r", 1.0)
            done = term.wait("件送った", *PERMISSION_WORDS, since=m1, timeout=90)
        time.sleep(1.5)
        full = term.text(m0)
        check(not any(flat(p) in flat(full) for p in PERMISSION_WORDS), "許可ダイアログ 0")
        ids = w["ids"]
        if name == "afk":
            check(done == "答えがなかった", "離席で閉じたら「答えがなかった」で止まる（%s）" % done)
            check(inbox.objects == {} and inbox.finish_bodies == [], "何も送られていない")
        else:
            check(done == "件送った", "送り終わった（%s）" % done)
            fin = inbox.finish_bodies[-1] if inbox.finish_bodies else {}
            sent = sorted(s["session_id"] for s in fin.get("sent", []))
            want = (sorted(ids.values()) if name in ("none", "reask") else sorted([ids["a"], ids["b"], ids["d"]])
                    if name in ("numbers", "suggest") else None)
            if name == "many":
                want = sent if len(sent) == 60 and set(ids.values()) <= set(sent) else ["60 件ではない"]
            check(sent == want, "送った会話が一覧どおり（%d 件）" % len(sent))
            check(fin.get("excluded_count") == (1 if name in ("numbers", "suggest") else 0), "外した件数 %s" % fin.get("excluded_count"))
            check(fin.get("note") == ("e2e の感想です。MCP のログインで迷った" if name == "none" else ""),
                  "感想は本人の言葉そのまま（%r）" % fin.get("note"))
            if name == "reask":
                check(fin.get("assistant_note") == "選択画面の番号が読めず 1 回聞き直した",
                      "AI のメモ（ヒアドキュメント）は別の欄に届く（%r）" % fin.get("assistant_note"))
            body = inbox.body(ids["a"]) or b""
            check(KEY.encode() not in body and b"[REDACTED:anthropic_key]" in body and b"[OMITTED:image/png" in body,
                  "キーは伏せ、画像は抜いてある")
            check(inbox.body(ids["a"], "agent-abc.jsonl") is not None, "サブエージェントも届く")
            if name in ("numbers", "suggest"):
                check(inbox.body(ids["c"]) is None, "外した 3 番は届いていない")
        # 本物の transcript（一時ディレクトリの中）に残った答えの形
        tdir = os.path.join(w["cfg"], "projects")
        shapes = []
        for dp, _, fns in os.walk(tdir):
            for fn in fns:
                if fn.endswith(".jsonl") and os.path.basename(dp) != "-Users-alice-work-billing":
                    for line in open(os.path.join(dp, fn), encoding="utf-8", errors="replace"):
                        d = json.loads(line) if line.strip().startswith("{") else {}
                        if d.get("type") == "permission-mode":
                            shapes.append("mode=%s" % d.get("permissionMode"))
                        r = d.get("toolUseResult")
                        if isinstance(r, dict) and "questions" in r:
                            shapes.append((sorted(r), [v[:20] for v in (r.get("answers") or {}).values()]))
        notes.append("INFO transcript の選択画面の答え: %s" % shapes)
        notes.append("INFO 道具の順: %s" % [n for n, _ in model.log])
        notes.append("INFO MCP: %s / API: %s" % (mcp.calls, sorted(set(model.paths))))
    except StopIteration:
        pass
    except Exception as e:
        ok = False
        notes.append("FAIL 例外 %r" % e)
        notes.append("---- 画面の最後\n" + term.text()[-1500:])
        notes.append("---- いまの画面\n" + term.screen())
        notes.append("INFO 道具の順: %s" % model.log)
    finally:
        term.close()
        inbox.close()
        shutil.rmtree(root, ignore_errors=True)
    print("== %s: %s" % (name, "OK" if ok else "NG"))
    for n in notes:
        print("  " + n)
    return ok


if __name__ == "__main__":
    names = sys.argv[1:] or ["none", "numbers", "suggest", "reask", "afk", "control", "many"]
    results = [run_scenario(n) for n in names]
    print("ALL OK" if all(results) else "SOME FAILED")
    sys.exit(0 if all(results) else 1)
