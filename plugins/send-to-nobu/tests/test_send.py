# -*- coding: utf-8 -*-
"""send: 偽サーバー（標準ライブラリの HTTP サーバー）で /v1/uploads・PUT・/v1/finish を往復する。"""

import base64
import glob
import gzip
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import FakeInbox, Lines, Patched, World, agentlog, read_bytes  # noqa: E402

ANTHROPIC = "sk-ant-api03-" + "Z9x8C7v6B5n4M3a2S1d0" * 4
GITHUB = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8"
IMG = base64.b64encode(b"\x89PNG" + b"\x01" * 5996).decode()   # 6000 bytes
NOTE = ("昨日の感想：MCP のログインで迷った。\n"
        "質問: $HOME って何？ `echo $(date)` も \"引用\" も 'single' も && ; | > そのまま\n"
        "```python\nprint(\"hello\")\n```\n"
        "SEND_TO_NOBU_NOTE じゃない行\n")


class SendBase(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.srv = FakeInbox()
        self.patch = Patched(self.srv)

    def tearDown(self):
        self.patch.restore()
        self.srv.close()
        self.w.close()

    def send(self, exclude="none", note=None, code=None, extra=(), check=True, **kw):
        """送る。既定では先に確認係の結果（全部 ok）を控えに書く（確認できなかった会話は既定で外れるため）。"""
        p = self.w.pending()
        if check and p and p.get("session") == (kw.get("session") or self.w.current) and p["items"] \
                and not any(it.get("checked") for it in p["items"]):
            self.w.check(session=kw.get("session"))
        args = ["send", "--exclude", exclude, "--code", code or FakeInbox.CODE, "--api-base", self.srv.base]
        if note is not None:
            args += ["--note-file", "-"]
        args += list(extra)
        return self.w.run(*args, stdin=note or "", **kw)

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

    def next_day(self):
        self.w.current = self.w.start_send_session()


class RoundTripTest(SendBase):
    def test_roundtrip(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        code, out, err = self.send(note=NOTE)
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["submission_id"], "20260928T090312Z-1a2b3c4d")
        self.assertEqual((res["sent_count"], res["excluded_count"], res["subagent_count"]), (2, 0, 3))
        self.assertEqual((res["redactions"], res["omitted"]), (2, 2))

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
        self.assertNotIn(GITHUB.encode(), self.srv.object_lines(self.obj(a.sid, "agent-a1b2.jsonl")))
        self.assertEqual(json.loads(self.srv.object_lines(self.obj(a.sid, "agent-a1b2.meta.json")))["agentType"],
                         "Explore")
        self.assertIn(self.obj(a.sid, "workflows/wf_abc-123/agent-c3.jsonl"), self.srv.objects)

        # 送信票に渡したもの（契約のキーだけ）
        fin = self.srv.finish_bodies[0]
        self.assertEqual(fin["note"], NOTE.strip())  # 本人の言葉のまま（複数行・記号も）
        self.assertEqual(fin["excluded_count"], 0)
        self.assertEqual(fin["plugin_version"], agentlog.plugin_version())
        sa = [s for s in fin["sent"] if s["session_id"] == a.sid][0]
        self.assertEqual(sorted(sa), ["bytes", "last_activity", "project", "redactions", "session_id", "sha256",
                                      "subagents", "title"])
        self.assertEqual((sa["title"], sa["project"], sa["redactions"]), ("請求書の集計", "~/work/billing", 2))
        self.assertTrue(sa["last_activity"].endswith("Z"))

        st = self.w.state()["sessions"]
        self.assertEqual(st[a.sid]["d"], "sent")
        self.assertEqual(st[a.sid]["offset"], os.path.getsize(pa))
        self.assertIsNone(self.w.pending())
        self.assertEqual(glob.glob(os.path.join(self.w.data, "pack-*")), [])

        self.next_day()
        self.assertEqual(self.w.list()["count"], 0)
        self.w.append(pa, Lines(sid=a.sid).meta("last-prompt", lastPrompt="x"), mtime=time.time() + 5)
        self.next_day()
        self.assertEqual(self.w.list()["count"], 0)
        self.w.append(pb, Lines(sid=b.sid).user("続き").assistant())
        self.next_day()
        items = self.w.list()["items"]
        self.assertEqual([it["session_id"] for it in items], [b.sid])
        self.assertNotIn("default_excluded", items[0])

    def test_growth_after_the_list_is_not_sent(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        main_before = read_bytes(pa)
        sub_before = read_bytes(pa[:-6] + "/subagents/agent-a1b2.jsonl")
        # 一覧を見たあとで、本体とサブエージェントが伸び、新しいサブエージェントもできた
        self.w.append(pa, Lines(sid=a.sid).user("一覧のあとに書いた秘密の相談").assistant())
        self.w.append(pa[:-6] + "/subagents/agent-a1b2.jsonl", Lines().user("あとから増えたサブ").assistant())
        self.w.subagent(pa, "agent-new9.jsonl", Lines().user("新しいサブ").assistant())
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        main_sent = self.srv.object_lines(self.obj(a.sid))
        self.assertNotIn("一覧のあとに書いた".encode(), main_sent)
        self.assertEqual(len(main_sent.splitlines()), len(main_before.splitlines()))
        self.assertEqual(len(self.srv.object_lines(self.obj(a.sid, "agent-a1b2.jsonl")).splitlines()),
                         len(sub_before.splitlines()))
        self.assertNotIn(self.obj(a.sid, "agent-new9.jsonl"), self.srv.objects)
        self.assertEqual(self.w.state()["sessions"][a.sid]["offset"], len(main_before))
        # 続きは翌日の一覧に出る
        self.next_day()
        self.assertIn(a.sid, [it["session_id"] for it in self.w.list()["items"]])

    def test_gzip_is_stable(self):
        a, pa, b, pb = self.two_sessions()
        tmp = os.path.join(self.w.tmp, "g")
        os.makedirs(tmp)
        s1 = agentlog.pack_jsonl(pa, os.path.join(tmp, "1.gz"), 10 ** 9)
        s2 = agentlog.pack_jsonl(pa, os.path.join(tmp, "2.gz"), 10 ** 9)
        self.assertEqual((s1["bytes"], s1["sha256"]), (s2["bytes"], s2["sha256"]))
        with gzip.open(os.path.join(tmp, "1.gz")) as g:
            self.assertEqual(len(g.read().splitlines()), len(read_bytes(pa).splitlines()))

    def test_exclude_and_counts(self):
        a, pa, b, pb = self.two_sessions()
        items = self.w.list()["items"]
        n_a = [it["n"] for it in items if it["session_id"] == a.sid][0]
        code, out, err = self.send(exclude=str(n_a))
        self.assertEqual(code, 0, err)
        fin = self.srv.finish_bodies[0]
        self.assertEqual([s["session_id"] for s in fin["sent"]], [b.sid])
        self.assertEqual(fin["excluded_count"], 1)
        self.assertNotIn(a.sid, json.dumps(fin))           # 外した会話は ID もタイトルも送らない
        self.assertNotIn("請求書", json.dumps(fin, ensure_ascii=False))
        self.assertFalse(any(a.sid in o for o in self.srv.objects))
        st = self.w.state()["sessions"]
        self.assertEqual((st[a.sid]["d"], st[b.sid]["d"]), ("excluded", "sent"))

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

    def test_note_only_when_nothing_to_send(self):
        self.w.list()
        code, out, err = self.send(note="今日は使わなかった。質問だけ: スキルって何？")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.srv.upload_batches, [])
        fin = self.srv.finish_bodies[0]
        self.assertEqual((fin["sent"], fin["excluded_count"]), ([], 0))

    def test_all_excluded_with_note(self):
        self.two_sessions()
        self.w.list()
        code, out, err = self.send(exclude="1,2", note="全部外したけど感想はある")
        self.assertEqual(code, 0, err)
        fin = self.srv.finish_bodies[0]
        self.assertEqual((fin["sent"], fin["excluded_count"]), ([], 2))


class RoundSendTest(SendBase):
    def test_sent_and_excluded_do_not_come_back_in_later_rounds(self):
        now = time.time()
        sids = []
        for i in range(20):
            L = Lines(base=now - 90000 + i * 2000).user("会話 %02d" % i).assistant()
            self.w.write(L)
            sids.append(L.sid)
        r1 = self.w.list()
        self.assertEqual((r1["count"], r1["remaining"], r1["round"]), (15, 5, 1))
        self.assertNotIn("note_already_sent", r1)
        code, out, err = self.send(exclude="1", note="1 ラウンド目の感想")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["sent_count"], res["excluded_count"], res["remaining"]), (14, 1, 5))
        self.srv.finished = False                        # 次のラウンドは新しい引換券
        # 送信のあと、同じ会話でもう一度 /send-to-nobu → 次のラウンド
        code, out, err = self.w.run("status")
        self.assertEqual(json.loads(out), {"pending": False})
        r2 = self.w.list()
        self.assertEqual((r2["count"], r2["remaining"], r2["round"]), (5, 0, 2))
        self.assertTrue(r2["note_already_sent"])        # 2 ラウンド目は感想を聞かない
        # 別の会話（翌日）では 1 ラウンド目から・感想も聞く
        other = self.w.start_send_session()
        code, out, err = self.w.run("list", session=other)
        r_other = json.loads(out)
        self.assertEqual(r_other["round"], 1)
        self.assertNotIn("note_already_sent", r_other)
        code, out, err = self.w.run("list")   # もとの会話に戻る（控えは別の会話に移った）
        r2 = json.loads(out)
        self.assertEqual((r2["count"], r2["round"]), (5, 2))
        self.assertEqual([it["session_id"] for it in r2["items"]], sids[:5])
        self.assertEqual([it["n"] for it in r2["items"]], [1, 2, 3, 4, 5])
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["remaining"], 0)
        self.assertEqual(self.w.list()["count"], 0)
        sent = {o.split("/")[-1].split(".")[0] for o in self.srv.objects}
        self.assertEqual(sent, set(sids) - {sids[5]})


class UnconfirmedTest(SendBase):
    def three(self):
        now = time.time()
        for i in range(3):
            self.w.write(Lines(base=now - 9000 + i * 1000).user("会話 %d" % i).assistant())
        return self.w.list()

    def test_nothing_is_sent_without_the_checkers_result(self):
        r1 = self.three()
        code, out, err = self.send(check=False)                           # 確認係の結果を書いていない
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["sent_count"], res["excluded_count"], res["deferred_unconfirmed"], res["remaining"]),
                         (0, 0, 3, 3))
        self.assertEqual(self.srv.upload_calls, 0)
        self.assertEqual({k for k in self.w.state()["sessions"] if k != self.w.current}, set())   # 何も記録しない
        # 同じ会話の次のラウンドで、同じ 3 件がもう一度出て確認係にかかる
        r2 = self.w.list()
        self.assertEqual([it["session_id"] for it in r2["items"]], [it["session_id"] for it in r1["items"]])
        self.assertTrue(all(it["review"] for it in r2["items"]))
        self.assertNotEqual(r2["review_dir"], r1["review_dir"])

    def test_unconfirmed_is_deferred_not_excluded(self):
        r1 = self.three()
        unconfirmed_sid = r1["items"][2]["session_id"]
        self.assertEqual(self.w.check(ok="1", caution="2"), {"unconfirmed": [3]})
        code, out, err = self.send(check=False)
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["sent_count"], res["excluded_count"], res["deferred_unconfirmed"], res["remaining"]),
                         (2, 0, 1, 1))
        self.assertEqual(self.srv.finish_bodies[0]["excluded_count"], 0)   # 外したとは送らない
        self.assertNotIn(unconfirmed_sid, self.w.state()["sessions"])     # 未決定のまま
        self.srv.finished = False
        # 同じ会話でもう一度 /send-to-nobu → 未確認だった会話が出て、確認係にかかる（前に外した印は付かない）
        r2 = self.w.list()
        self.assertEqual([it["session_id"] for it in r2["items"]], [unconfirmed_sid])
        self.assertEqual(r2["items"][0]["review"], [["01-1.txt"]])
        self.assertNotIn("default_excluded", r2["items"][0])
        self.w.check()                                                     # 今度は確認できた
        code, out, err = self.send(check=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sent_count"], 1)
        self.assertEqual(self.w.state()["sessions"][unconfirmed_sid]["d"], "sent")

    def test_explicitly_excluded_unconfirmed_is_recorded_as_excluded(self):
        r1 = self.three()
        self.w.check(ok="1,2")
        code, out, err = self.send(exclude="3", check=False)             # 本人が外すと言った
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["sent_count"], res["excluded_count"], res["deferred_unconfirmed"], res["remaining"]),
                         (2, 1, 0, 0))
        self.assertEqual(self.w.state()["sessions"][r1["items"][2]["session_id"]]["d"], "excluded")
        self.assertEqual(self.w.list()["count"], 0)

    def test_include_sends_an_unconfirmed_conversation(self):
        self.three()
        self.w.check(ok="1,2")
        code, out, err = self.send(check=False, extra=["--include", "3"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sent_count"], 3)

    def test_checked_rejects_bad_input(self):
        self.three()
        for args in (["--ok", "1", "--caution", "1"], ["--ok", "4"], ["--ok", ""], ["--caution", "2-1"]):
            code, out, err = self.w.run("checked", *args)
            self.assertEqual(code, agentlog.EXIT_USAGE, args)
        other = self.w.start_send_session()
        code, out, err = self.w.run("checked", "--ok", "1", session=other)
        self.assertEqual(code, agentlog.EXIT_USAGE)                          # 別の会話の控えには書けない

    def test_queued_text_does_not_open_the_gate(self):
        # 一覧のターンの最中に打った文（queued）は、一覧を見る前かもしれないので返事にしない
        self.three()
        self.w.check()
        self.w.append(self.w.session_file(), Lines(sid=self.w.current).meta(
            "attachment", attachment={"type": "queued_command", "commandMode": "prompt", "prompt": "なし"}))
        code, out, err = self.send(check=False, reply=False)
        self.assertEqual(code, agentlog.EXIT_NOT_ANSWERED)
        self.assertEqual(self.srv.upload_calls, 0)
        self.w.reply("なし")                                            # ターンが終わってから打った返事
        code, out, err = self.send(check=False, reply=False)
        self.assertEqual(code, 0, err)

    def test_shared_history_with_an_unconfirmed_conversation_has_its_own_wording(self):
        a = Lines(base=time.time() - 7200).user("元の会話").assistant()
        b = Lines(base=time.time() - 3600).copy_from(a).user("分岐").assistant()
        self.w.write(a)
        self.w.write(b)
        self.w.list()
        self.w.check(ok="1")                                            # 2 は確認できなかった
        code, out, err = self.send(check=False)
        self.assertEqual(code, agentlog.EXIT_CONFIRM_SHARED)
        self.assertIn("確認できなかった方（2 番）は今回送らない", err)
        self.assertNotIn("外した方の中身", err)
        code, out, err = self.send(check=False, extra=["--confirm-shared"])
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["sent_count"], res["excluded_count"], res["deferred_unconfirmed"]), (1, 0, 1))


class GateTest(SendBase):
    def test_send_without_a_reply_is_refused(self):
        self.two_sessions()
        self.w.list()
        code, out, err = self.send(reply=False)
        self.assertEqual(code, agentlog.EXIT_NOT_ANSWERED)
        self.assertEqual(self.srv.upload_calls, 0)
        # /send-to-nobu を引数なしで打ち直しただけでは返事にならない
        self.w.reply("", plain=False)
        code, out, err = self.send(reply=False)
        self.assertEqual(code, agentlog.EXIT_NOT_ANSWERED)
        # 普通の返事なら通る
        self.w.reply("なし", plain=True)
        code, out, err = self.send(reply=False)
        self.assertEqual(code, 0, err)

    def test_relisting_resets_the_gate(self):
        self.two_sessions()
        self.w.list()
        self.w.reply("なし")
        self.w.list()  # 同じ一覧をもう一度見せた → 返事はそこから数え直し
        code, out, err = self.send(reply=False)
        self.assertEqual(code, agentlog.EXIT_NOT_ANSWERED)


class DefaultExcludedTest(SendBase):
    def setup_previously_excluded(self):
        a = Lines(base=time.time() - 7200).user("外したい相談").assistant()
        pa = self.w.write(a)
        self.w.list()
        code, out, err = self.send(exclude="1")
        self.assertEqual(code, 0, err)
        self.srv.finished = False
        self.w.append(pa, Lines(sid=a.sid).user("続き").assistant())
        b = Lines(base=time.time() - 60).user("ふつうの会話").assistant()
        self.w.write(b)
        self.next_day()
        items = {it["session_id"]: it for it in self.w.list()["items"]}
        self.assertTrue(items[a.sid]["default_excluded"])
        return a, b, items[a.sid]["n"], items[b.sid]["n"]

    def test_previously_excluded_is_excluded_by_default(self):
        a, b, na, nb = self.setup_previously_excluded()
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["sent_count"], res["excluded_count"]), (1, 1))
        self.assertFalse(any(a.sid in o for o in self.srv.objects))

    def test_include_sends_it(self):
        a, b, na, nb = self.setup_previously_excluded()
        code, out, err = self.send(extra=["--include", str(na)])
        self.assertEqual(code, 0, err)
        self.assertIn(self.obj(a.sid), self.srv.objects)
        self.assertEqual(json.loads(out)["sent_count"], 2)

    def test_include_and_exclude_conflict(self):
        a, b, na, nb = self.setup_previously_excluded()
        code, out, err = self.send(exclude=str(na), extra=["--include", str(na)])
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertEqual(self.srv.upload_calls, 0)


class ArgumentTest(SendBase):
    def test_exclude_parsing_fails_closed(self):
        self.two_sessions()
        self.w.list()
        for bad in ("3", "1,x", "0-5", "2-1", "", "  ", "無し", "0", "ない"):
            code, out, err = self.send(exclude=bad)
            self.assertEqual(code, agentlog.EXIT_USAGE, repr(bad))
        self.assertEqual(self.srv.upload_calls, 0)
        self.assertEqual(agentlog.parse_numbers("１、2", {1: 0, 2: 0}, "exclude"), {1, 2})
        self.assertEqual(agentlog.parse_numbers("なし", {1: 0}, "exclude"), set())
        self.assertEqual(agentlog.parse_numbers("NONE", {1: 0}, "exclude"), set())
        self.assertEqual(agentlog.parse_numbers("1-2", {1: 0, 2: 0, 3: 0}, "exclude"), {1, 2})

    def test_note_file_must_be_stdin(self):
        self.w.list()
        code, out, err = self.w.run("send", "--exclude", "none", "--note-file", "/etc/hosts", "--code", "x",
                                    "--api-base", self.srv.base)
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertIn("--note-file", err)

    def test_note_limit_is_checked_after_masking(self):
        self.w.list()
        note = "あ" * (agentlog.NOTE_MAX - 30) + ANTHROPIC  # 伏せる前は超える・伏せたあとは収まる
        self.assertGreater(len(note), agentlog.NOTE_MAX)
        code, out, err = self.send(note=note)
        self.assertEqual(code, 0, err)
        self.assertIn("[REDACTED:anthropic_key]", self.srv.finish_bodies[0]["note"])

    def test_note_too_long(self):
        self.w.list()
        code, out, err = self.send(note="あ" * (agentlog.NOTE_MAX + 1))
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertEqual(self.srv.finish_bodies, [])

    def test_api_base_allowlist(self):
        self.two_sessions()
        self.w.list()
        for bad in ("https://evil.example.com", "http://agent-log-inbox-mcp.androots.co.jp",
                    "https://agent-log-inbox-mcp.androots.co.jp.evil.com", self.srv.base + "/x", ""):
            code, out, err = self.w.run("send", "--exclude", "none", "--code", FakeInbox.CODE, "--api-base", bad)
            self.assertEqual(code, agentlog.EXIT_USAGE, bad)
        self.assertEqual(self.srv.upload_calls, 0)
        self.assertEqual(self.patch.saved[0], ("https://agent-log-inbox-mcp.androots.co.jp",))
        self.assertEqual(self.patch.saved[1], ("https://storage.googleapis.com/",))


class TransportTest(SendBase):
    def test_headers_are_passed_as_is(self):
        self.two_sessions()
        self.w.list()
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        # 偽サーバーは署名対象ヘッダーが 1 つでも違うと 403 を返す。全部届いていれば一致している
        self.assertEqual(len(self.srv.objects), 5)

    def test_redirect_is_not_followed(self):
        self.two_sessions()
        self.w.list()
        self.srv.uploads_redirect = True
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(self.srv.redirect_hits, 0)
        self.assertIsNotNone(self.w.pending())

    def test_put_url_must_be_allowed(self):
        self.two_sessions()
        self.w.list()
        self.srv.put_url_base = "http://localhost:%d" % self.srv.port  # 許可リストに無い先
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(self.srv.put_attempts, {})

    def test_put_retry(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        self.srv.fail_put = {b.sid: 2}
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.srv.put_attempts[self.obj(b.sid)], 3)

    def test_failure_keeps_state(self):
        self.two_sessions()
        self.w.list()
        before = self.w.state()
        self.srv.forbid_put = {"agent-c3"}
        code, out, err = self.send(note="感想")
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(out, "")
        self.assertIn("アップロードに失敗", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(self.w.state(), before)
        self.assertIsNotNone(self.w.pending())
        self.assertEqual(self.srv.finish_bodies, [])
        self.assertEqual(glob.glob(os.path.join(self.w.data, "pack-*")), [])

    def test_permanent_5xx_gives_up(self):
        self.two_sessions()
        self.w.list()
        self.srv.fail_put = {"agent-a1b2.jsonl": 99}
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(max(self.srv.put_attempts.values()), agentlog.RETRIES + 1)

    def test_4xx_is_not_retried(self):
        self.two_sessions()
        self.w.list()
        self.srv.uploads_status = (400, "invalid_argument")
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(self.srv.upload_calls, 1)
        self.srv.uploads_status = (500, "internal")
        self.send()
        self.assertEqual(self.srv.upload_calls, 1 + agentlog.RETRIES + 1)

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

    def test_already_finished_on_the_first_try_is_a_used_ticket(self):
        # 古い引換券の使い回し: 感想だけの送信でも黙って成功にしない（スキルは start_submission からやり直す）
        self.w.list()
        self.srv.finished = True
        code, out, err = self.send(note="感想")
        self.assertEqual(code, agentlog.EXIT_UNAUTHORIZED)
        self.assertIn("使用済み", err)
        self.assertIsNotNone(self.w.pending())
        self.assertEqual(self.w.state()["sessions"], {})

    @unittest.skipUnless(os.path.exists(agentlog.SYSTEM_CA_BUNDLE), "macOS のシステムバンドルが無い環境")
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


class SafetyTest(SendBase):
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
        self.assertEqual(self.srv.upload_calls, 0)
        code, out, err = self.send(exclude="1", extra=["--confirm-shared"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sent_count"], 1)

    def test_pending_guard(self):
        self.two_sessions()
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertIn("/send-to-nobu", err)
        self.w.list()
        other = self.w.start_send_session()
        code, out, err = self.send(session=other)
        self.assertEqual(code, agentlog.EXIT_USAGE)
        code, out, err = self.send(now=time.time() + 7 * 3600)
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertIn("古い", err)
        self.assertEqual(self.srv.upload_calls, 0)

    def test_tampered_pending_path_is_refused(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        p = self.w.pending()
        link = os.path.join(self.w.tmp, "outside.jsonl")
        os.symlink(pa, link)
        p["items"][0]["path"] = link
        agentlog.write_json_atomic(os.path.join(self.w.data, "pending.json"), p)
        code, out, err = self.send()
        self.assertEqual(code, agentlog.EXIT_ERROR)
        self.assertEqual(self.srv.put_attempts, {})

    def test_old_pack_dirs_are_swept(self):
        self.w.list()
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        os.makedirs(os.path.join(self.w.data, "pack-%d-dead" % gone.pid))   # もういないプロセス
        os.makedirs(os.path.join(self.w.data, "pack-oldformat"))
        alive = os.path.join(self.w.data, "pack-%d-alive" % os.getppid())  # 生きているプロセスは残す
        os.makedirs(alive)
        code, out, err = self.send(note="感想")
        self.assertEqual(code, 0, err)
        self.assertEqual(glob.glob(os.path.join(self.w.data, "pack-*")), [alive])

    def test_sigterm_removes_the_temp_dir(self):
        a, pa, b, pb = self.two_sessions()
        self.w.list()
        self.w.check()
        self.w.reply("なし")
        self.srv.put_delay = 5.0
        runner = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            import agentlog
            agentlog.CONFIG_DIR = %r
            agentlog.PROJECTS_DIR = %r
            agentlog.ALLOWED_API_BASES = (%r,)
            agentlog.ALLOWED_PUT_PREFIXES = (%r,)
            agentlog._OPENER = agentlog.build_opener(use_proxy=False)
            sys.exit(agentlog.main(["send", "--exclude", "none", "--code", %r, "--api-base", %r,
                                    "--data-dir", %r]))
        """) % (os.path.dirname(agentlog.__file__), self.w.tmp, self.w.projects, self.srv.base, self.srv.base + "/put/",
                FakeInbox.CODE, self.srv.base, self.w.data)
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID=self.w.current)
        proc = subprocess.Popen([sys.executable, "-c", runner], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline and not self.srv.put_attempts:
            time.sleep(0.05)
        self.assertTrue(glob.glob(os.path.join(self.w.data, "pack-*")))
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
        self.assertEqual(glob.glob(os.path.join(self.w.data, "pack-*")), [])
        self.assertEqual(self.srv.finish_bodies, [])


if __name__ == "__main__":
    unittest.main()
