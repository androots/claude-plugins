# -*- coding: utf-8 -*-
"""send: 偽サーバー（標準ライブラリの HTTP サーバー）で /v1/uploads・PUT・/v1/finish を往復する。"""

import glob
import gzip
import json
import os
import sys
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import FakeInbox, Lines, World, agentlog  # noqa: E402

ANTHROPIC = "sk-ant-api03-" + "Z9x8C7v6B5n4M3a2S1d0" * 4
IMG = __import__("base64").b64encode(b"\x89PNG" + b"\x01" * 5996).decode()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


GITHUB = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8"


class SendTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.srv = FakeInbox()
        self.saved_backoff = agentlog.BACKOFF_BASE
        agentlog.BACKOFF_BASE = 0.001
        # テストでは OS のプロキシ設定を使わない
        self.saved_opener = agentlog._OPENER
        agentlog._OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # 送り先の許可リストはテストコードからだけ差し替える
        self.saved_allowed = agentlog.ALLOWED_API_BASES
        agentlog.ALLOWED_API_BASES = (self.srv.base,)

    def tearDown(self):
        agentlog._OPENER = self.saved_opener
        agentlog.ALLOWED_API_BASES = self.saved_allowed
        agentlog.BACKOFF_BASE = self.saved_backoff
        self.srv.close()
        self.w.close()

    def send(self, exclude="none", note=None, code=None, extra=()):
        args = ["send", "--exclude", exclude, "--code", code or FakeInbox.CODE, "--api-base", self.srv.base]
        if note is not None:
            args += ["--note-file", "-"]
        args += list(extra)
        return self.w.run(*args, stdin=note or "")

    def two_sessions(self):
        a = Lines(base=time.time() - 7200)
        a.user("請求書の集計をしたい。キーは %s" % ANTHROPIC).assistant("了解").tool_result("ok").assistant()
        a.user_blocks([{"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": IMG}}]}],
            toolUseResult={"type": "image", "file": {"base64": IMG, "type": "image/png", "originalSize": 6000}})
        a.assistant("スクショを見た")
        a.meta("ai-title", aiTitle="請求書の集計")
        pa = self.w.write(a)
        self.w.subagent(pa, "agent-a1b2.jsonl", Lines().user("サブ %s" % GITHUB).assistant())
        self.w.subagent(pa, "agent-a1b2.meta.json", b'{"agentType":"Explore","description":"x"}')
        self.w.subagent(pa, "workflows/wf_abc-123/agent-c3.jsonl", Lines().user("入れ子").assistant())
        b = Lines(base=time.time() - 3600).user("別の会話").assistant()
        pb = self.w.write(b)
        return a, pa, b, pb

    def obj(self, sid, rel=None):
        base = "raw/androots/alice/claude-code/%s" % sid
        return base + ".jsonl.gz" if rel is None else base + "/subagents/%s.gz" % rel

    def test_roundtrip(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        note = "昨日の感想：MCP のログインで迷った。\n質問: $HOME って何？ `echo` も \"引用\" も そのまま\n"
        code, out, err = self.send(note=note)
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["submission_id"], "20260928T090312Z-1a2b3c4d")
        self.assertEqual((res["sent_count"], res["excluded_count"], res["subagent_count"]), (2, 0, 3))
        self.assertEqual(res["redactions"], 2)
        self.assertEqual(res["omitted"], 2)  # 画像ブロックと Read の結果の 2 か所

        # 本体: 変換した行（マスク 1 行・画像 1 行）以外はバイト一致
        orig = read_bytes(pa).splitlines(True)
        got = self.srv.object_lines(self.obj(a.sid)).splitlines(True)
        self.assertEqual(len(orig), len(got))
        diff = [i for i, (x, y) in enumerate(zip(orig, got)) if x != y]
        self.assertEqual(len(diff), 2)
        self.assertNotIn(ANTHROPIC.encode(), got[diff[0]])
        self.assertNotIn(IMG.encode(), b"".join(got))
        self.assertEqual(got[diff[1]].count(b"[OMITTED:image/png 6000 bytes]"), 2)
        for ln in got:
            json.loads(ln)
        self.assertEqual(self.srv.object_lines(self.obj(b.sid)), read_bytes(pb))
        # サブエージェント（入れ子・.meta.json）も届く
        self.assertNotIn(GITHUB.encode(), self.srv.object_lines(self.obj(a.sid, "agent-a1b2.jsonl")))
        self.assertEqual(json.loads(self.srv.object_lines(self.obj(a.sid, "agent-a1b2.meta.json")))["agentType"],
                         "Explore")
        self.assertIn(self.obj(a.sid, "workflows/wf_abc-123/agent-c3.jsonl"), self.srv.objects)

        # 送信票に渡したもの
        fin = self.srv.finish_bodies[0]
        self.assertEqual(fin["note"], note.strip())  # 本人の言葉のまま
        self.assertEqual(fin["excluded_count"], 0)
        self.assertEqual(fin["plugin_version"], agentlog.plugin_version())
        sa = [s for s in fin["sent"] if s["session_id"] == a.sid][0]
        self.assertEqual(sa["title"], "請求書の集計")
        self.assertEqual(sa["project"], "~/work/billing")
        self.assertEqual(sa["redactions"], 2)  # 外した画像は数えない（契約は変えない）
        self.assertEqual(sorted(sa), ["bytes", "last_activity", "project", "redactions", "session_id", "sha256",
                                      "subagents", "title"])
        self.assertTrue(sa["last_activity"].endswith("Z"))
        self.assertEqual(sorted(x["rel"] for x in sa["subagents"]),
                         ["agent-a1b2.jsonl", "agent-a1b2.meta.json", "workflows/wf_abc-123/agent-c3.jsonl"])

        # 状態の更新・控えと一時ディレクトリの削除
        st = self.w.state()["sessions"]
        self.assertEqual(st[a.sid]["d"], "sent")
        self.assertEqual(st[a.sid]["offset"], os.path.getsize(pa))
        self.assertIsNone(self.w.pending())
        self.assertEqual(glob.glob(os.path.join(self.w.data, "pack-*")), [])
        self.assertEqual(self.w.list()["count"], 0)

        # 閉じただけ（メタ行）は出ない・続きを書いたら出る
        self.w.append(pa, Lines(sid=a.sid).meta("last-prompt", lastPrompt="x"), mtime=time.time() + 5)
        self.assertEqual(self.w.list()["count"], 0)
        self.w.append(pb, Lines(sid=b.sid).user("続き").assistant())
        items = self.w.list()["items"]
        self.assertEqual([it["session_id"] for it in items], [b.sid])
        self.assertNotIn("previously_excluded", items[0])

    def test_gzip_is_stable(self):
        a, pa, b, pb = self.two_sessions()
        tmp = os.path.join(self.w.tmp, "g")
        os.makedirs(tmp)
        s1 = agentlog.pack_jsonl(pa, os.path.join(tmp, "1.gz"))
        s2 = agentlog.pack_jsonl(pa, os.path.join(tmp, "2.gz"))
        self.assertEqual((s1["bytes"], s1["sha256"]), (s2["bytes"], s2["sha256"]))
        with gzip.open(os.path.join(tmp, "1.gz")) as g:
            self.assertEqual(len(g.read().splitlines()), len(read_bytes(pa).splitlines()))

    def test_exclude_and_counts(self):
        a, pa, b, pb = self.two_sessions()
        items = self.w.list()["items"]
        n_a = [it["n"] for it in items if it["session_id"] == a.sid][0]
        code, out, err = self.send(exclude=str(n_a), note="なし" and "")
        self.assertEqual(code, 0, err)
        fin = self.srv.finish_bodies[0]
        self.assertEqual([s["session_id"] for s in fin["sent"]], [b.sid])
        self.assertEqual(fin["excluded_count"], 1)
        self.assertNotIn(a.sid, json.dumps(fin))           # 外した会話は ID もタイトルも送らない
        self.assertNotIn("請求書", json.dumps(fin, ensure_ascii=False))
        self.assertFalse(any(a.sid in o for o in self.srv.objects))
        st = self.w.state()["sessions"]
        self.assertEqual((st[a.sid]["d"], st[b.sid]["d"]), ("excluded", "sent"))

    def test_headers_are_passed_as_is(self):
        self.two_sessions()
        self.w.list()
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        # 偽サーバーは署名対象ヘッダーが 1 つでも違うと 403 を返す。全部届いていれば一致している
        self.assertEqual(len(self.srv.objects), 5)

    def test_batches_of_100(self):
        L = Lines().user("大量のサブエージェント").assistant()
        path = self.w.write(L)
        for i in range(230):
            self.w.subagent(path, "agent-%04d.jsonl" % i, Lines().user("sub %d" % i).assistant())
        self.w.list()
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.srv.upload_batches, [100, 100, 31])
        self.assertEqual(json.loads(out)["subagent_count"], 230)
        self.assertEqual(len(self.srv.objects), 231)

    def test_put_retry(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        self.srv.fail_put = {b.sid: 2}
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.srv.put_attempts[self.obj(b.sid)], 3)

    def test_failure_keeps_state(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        before = self.w.state()
        self.srv.forbid_put = {"agent-c3"}
        code, out, err = self.send(note="感想")
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(out, "")
        self.assertIn("アップロードに失敗", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(self.w.state(), before)            # 状態は更新しない（翌朝また出る）
        self.assertIsNotNone(self.w.pending())               # 引換券を取り直せばやり直せる
        self.assertEqual(self.srv.finish_bodies, [])
        self.assertEqual(glob.glob(os.path.join(self.w.data, "pack-*")), [])
        self.assertEqual(self.w.list()["count"], 2)

    def test_permanent_5xx_gives_up(self):
        self.two_sessions()
        self.w.list()
        self.srv.fail_put = {"agent-a1b2.jsonl": 99}
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(max(self.srv.put_attempts.values()), agentlog.RETRIES + 1)

    def test_note_only_when_nothing_to_send(self):
        self.w.list()
        code, out, err = self.send(note="今日は使わなかった。質問だけ: スキルって何？")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.srv.upload_batches, [])
        fin = self.srv.finish_bodies[0]
        self.assertEqual((fin["sent"], fin["excluded_count"]), ([], 0))
        self.assertEqual(json.loads(out)["sent_count"], 0)

    def test_all_excluded_with_note(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        code, out, err = self.send(exclude="1,2", note="全部外したけど感想はある")
        self.assertEqual(code, 0, err)
        fin = self.srv.finish_bodies[0]
        self.assertEqual((fin["sent"], fin["excluded_count"]), ([], 2))

    def test_shares_history_stops_until_confirmed(self):
        a = Lines(base=time.time() - 7200).user("元の会話").assistant()
        b = Lines(base=time.time() - 3600).copy_from(a).user("分岐").assistant()
        self.w.write(a)
        self.w.write(b)
        items = self.w.list()["items"]
        self.assertEqual(items[0]["shares_history_with"], [2])
        code, out, err = self.send(exclude="1")
        self.assertEqual(code, agentlog.EXIT_CONFIRM_SHARED)
        self.assertIn("2 番と 1 番", err)
        self.assertEqual(self.srv.upload_batches, [])
        self.assertEqual(self.srv.finish_bodies, [])
        code, out, err = self.send(exclude="1", extra=["--confirm-shared"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sent_count"], 1)

    def test_pending_guard(self):
        self.two_sessions()
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertIn("/send-to-nobu", err)
        self.w.list()
        code, out, err = self.w.run("send", "--exclude", "none", "--code", "x", "--api-base", self.srv.base,
                                    session="another-session")
        self.assertEqual(code, agentlog.EXIT_USAGE)
        code, out, err = self.w.run("send", "--exclude", "none", "--code", "x", "--api-base", self.srv.base,
                                    now=time.time() + 7 * 3600)
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertIn("古い", err)
        self.assertEqual(self.srv.upload_batches, [])

    def test_exclude_parsing(self):
        self.two_sessions()
        self.w.list()
        for bad in ("3", "1,x", "0-5"):
            code, out, err = self.send(exclude=bad)
            self.assertEqual(code, agentlog.EXIT_USAGE, bad)
        self.assertEqual(agentlog.parse_exclude("１、2", {1: 0, 2: 0}), {1, 2})
        self.assertEqual(agentlog.parse_exclude("なし", {1: 0}), set())
        self.assertEqual(agentlog.parse_exclude("1-2", {1: 0, 2: 0, 3: 0}), {1, 2})

    def test_unauthorized(self):
        self.two_sessions()
        self.w.list()
        code, out, err = self.send(code="wrong-code")
        self.assertEqual(code, agentlog.EXIT_UNAUTHORIZED)
        self.assertIn("start_submission", err)
        self.assertNotIn("wrong-code", err)
        self.assertIsNotNone(self.w.pending())

    def test_finish_response_lost_then_already_finished(self):
        self.two_sessions()
        self.w.list()
        self.srv.finish_lose_first = True
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sent_count"], 2)
        self.assertEqual(self.w.state()["sessions"] != {}, True)

    def test_already_finished_is_success(self):
        self.w.list()
        self.srv.finished = True  # 送信票はもう置かれている
        code, out, err = self.send(note="感想")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.w.pending())

    def test_4xx_is_not_retried(self):
        self.two_sessions()
        self.w.list()
        self.srv.uploads_status = (400, "invalid_argument")
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(self.srv.upload_calls, 1)
        self.srv.uploads_status = (500, "internal")
        code, out, err = self.send()
        self.assertEqual(self.srv.upload_calls, 1 + agentlog.RETRIES + 1)

    def test_ssl_falls_back_to_system_bundle(self):
        import ssl
        saved = ssl.create_default_context
        try:
            ssl.create_default_context = lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx = agentlog._ssl_context()
        finally:
            ssl.create_default_context = saved
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 0)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_api_base_allowlist(self):
        self.two_sessions()
        self.w.list()
        for bad in ("https://evil.example.com", "http://agent-log-inbox-mcp.androots.co.jp",
                    "https://agent-log-inbox-mcp.androots.co.jp.evil.com", self.srv.base + "/x"):
            code, out, err = self.w.run("send", "--exclude", "none", "--code", FakeInbox.CODE, "--api-base", bad)
            self.assertEqual(code, agentlog.EXIT_USAGE, bad)
        self.assertEqual(self.srv.upload_calls, 0)
        self.assertIsNotNone(self.w.pending())
        # 本番の既定値
        self.assertEqual(self.saved_allowed, ("https://agent-log-inbox-mcp.androots.co.jp",))
        self.assertEqual(agentlog.check_api_base(self.srv.base + "/"), self.srv.base)

    def test_note_too_long(self):
        self.w.list()
        code, out, err = self.send(note="あ" * (agentlog.NOTE_MAX + 1))
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertEqual(self.srv.finish_bodies, [])


if __name__ == "__main__":
    unittest.main()
