# -*- coding: utf-8 -*-
"""守ること 1〜6。すべて一時の設定ディレクトリと合成の会話ログ。"""

import base64
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import SEND_CMD, SEND_CMD_NS, FakeInbox, Lines, Patched, World, agentlog  # noqa: E402

KEY = "sk-ant-api03-" + "Q1w2E3r4T5y6U7i8O9p0" * 4
PNG = base64.b64encode(b"\x89PNG" + b"\x00" * 3000).decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.inbox = FakeInbox()
        self.patch = Patched(self.inbox)

    def tearDown(self):
        self.patch.restore()
        self.inbox.close()
        self.w.close()

    def conv(self, *prompts, **kw):
        """人の指示 prompts の会話を 1 本置く。(Lines, パス)。"""
        L = Lines(**kw)
        for p in prompts:
            L.user(p).assistant()
        return L, self.w.write(L)

    def send(self, *extra, **kw):
        return self.w.run("send", "--code", FakeInbox.CODE, "--api-base", self.inbox.base, *extra, **kw)

    def finish(self):
        self.assertEqual(len(self.inbox.finish_bodies), 1)
        return self.inbox.finish_bodies[0]


class OnlyWhatWasShown(Base):
    """1. 本人が外した会話は送らない。送るのはページで見せた会話だけ、ページを作った時点までの中身だけ。"""

    def test_excluded_conversation_is_not_sent(self):
        a, _ = self.conv("一つ目", base=time.time() - 3000)
        b, _ = self.conv("二つ目", base=time.time() - 2000)
        c, _ = self.conv("三つ目", base=time.time() - 1000)
        self.w.page()
        self.assertEqual(self.w.listed(), [a.sid, b.sid, c.sid])
        self.w.decide(exclude=[2])
        code, res = self.send()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["say"], "2 件送った・1 件外した")
        self.assertEqual(self.inbox.sent_ids(), sorted([a.sid, c.sid]))
        self.assertEqual(self.finish()["excluded_count"], 1)
        self.assertNotIn(b.sid, json.dumps(self.finish()))
        self.assertEqual(self.w.state()["sessions"][b.sid]["d"], "excluded")

    def test_nothing_written_after_the_page_is_sent(self):
        L, path = self.conv("最初の指示")
        sub = self.w.subagent(path, "agent-a1.jsonl", Lines().user("サブの指示").assistant("サブの答え"))
        with open(path, "rb") as f:
            at_page = f.read()
        with open(sub, "rb") as f:
            sub_at_page = f.read()
        self.w.page()
        # ページのあとで: 続きを書く・サブエージェントが増える・新しい会話ができる
        self.w.append(path, Lines(sid=L.sid).user("ページのあとの指示").assistant())
        self.w.append(sub, Lines().user("ページのあとのサブ").assistant())
        self.w.subagent(path, "agent-new.jsonl", Lines().user("新しいサブ").assistant())
        late, _ = self.conv("ページのあとに始めた会話")
        self.w.decide()
        code, res = self.send()
        self.assertEqual(code, 0, res)
        self.assertEqual(self.inbox.body(L.sid), at_page)
        self.assertEqual(self.inbox.body(L.sid, "agent-a1.jsonl"), sub_at_page)
        self.assertIsNone(self.inbox.body(L.sid, "agent-new.jsonl"))
        self.assertEqual(self.inbox.sent_ids(), [L.sid])
        # 続きは次のページに出る（ページのあとに始めた会話も）
        self.w.next_day()
        self.w.page()
        self.assertEqual(sorted(self.w.listed()), sorted([L.sid, late.sid]))

    def test_too_many_shows_the_oldest_and_the_rest_comes_next(self):
        saved = agentlog.LIST_MAX
        agentlog.LIST_MAX = 3
        try:
            convs = [self.conv("会話 %d" % i, base=time.time() - 5000 + i * 100)[0] for i in range(5)]
            self.w.page()
            self.assertEqual(self.w.listed(), [c.sid for c in convs[:3]])
            self.assertEqual(self.w.page_data()["more"], 2)
            self.w.decide()
            code, res = self.send()
            self.assertEqual(res["sent"], 3)
            self.assertIn("ほかに 2 件ある", res["say"])
            self.assertEqual(self.inbox.sent_ids(), sorted(c.sid for c in convs[:3]))
            self.w.page()   # 同じ送信用の会話でもう一度
            self.assertEqual(self.w.listed(), [c.sid for c in convs[3:]])
        finally:
            agentlog.LIST_MAX = saved


class DecisionRequired(Base):
    """2. 本人がページで押した答えなしに送らない。答えは今のページの回・今の会話のものだけ。"""

    def setUp(self):
        super().setUp()
        self.a, _ = self.conv("会話 A", base=time.time() - 2000)
        self.b, _ = self.conv("会話 B", base=time.time() - 1000)
        self.w.page()

    def assertNothingSent(self, code, res):
        self.assertNotEqual(code, 0)
        self.assertEqual(self.inbox.objects, {})
        self.assertEqual(self.inbox.finish_bodies, [])
        self.assertEqual(self.w.state()["sessions"], {})
        self.assertIsNotNone(self.w.pending())      # 控えは残す（押し直して送れる）

    def test_without_a_decision(self):
        code, res = self.send()
        self.assertNothingSent(code, res)
        self.assertIn("まだ押されていない", res["say"])
        self.assertIn(os.path.join(self.w.data, "page"), res["next"])   # 保存先を教えて get し直させる

    def test_a_decision_from_an_earlier_page_is_not_read(self):
        self.w.decide()
        self.w.page()       # 新しいページを作ると、前の答えは消える
        self.assertNothingSent(*self.send())
        self.w.decide(round="r-20261014-0700-abcdef")     # 前の回のページで押した
        code, res = self.send()
        self.assertNothingSent(code, res)
        self.assertIn("前のページ", res["say"])
        self.w.decide()
        code, res = self.send()
        self.assertEqual((code, res["sent"]), (0, 2))

    def test_numbers_not_on_the_page_stop_everything(self):
        for exclude in ([3], [0], [1, 3], ["1"], [True], [1.0]):
            self.w.decide(exclude=exclude)
            code, res = self.send()
            self.assertNothingSent(code, res)
            self.assertIn("読めなかった", res["say"], exclude)
        for raw in ({"round": self.w.pending()["round"], "exclude": "1", "note": ""},
                    {"round": self.w.pending()["round"], "exclude": [], "note": 3},
                    {"round": self.w.pending()["round"], "note": ""}, ["not", "a", "dict"]):
            self.w.decide(raw=raw)
            self.assertNothingSent(*self.send())

    def test_another_conversation_cannot_send(self):
        self.w.decide()
        other = Lines().user(SEND_CMD).user("スキル本文", isMeta=True)
        self.w.write(other, project=World.SEND_PROJECT)
        code, res = self.send(session=other.sid)
        self.assertNothingSent(code, res)
        self.assertIn("一覧が見つからない", res["say"])

    def test_a_decision_is_used_once(self):
        self.w.decide(exclude=[1])
        self.assertEqual(self.send()[0], 0)
        self.assertFalse(os.path.exists(agentlog.decision_path(self.w.data)))
        code, res = self.send()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.inbox.finish_bodies), 1)

    def test_the_ai_cannot_pass_numbers_or_the_note(self):
        self.w.decide()
        for extra in (["--exclude", "1"], ["--note", "AI が書いた感想"]):
            code, res = self.send(*extra)
            self.assertEqual(code, 2)
            self.assertIn("コマンドの形が違う", res["next"])
        self.assertEqual(self.inbox.objects, {})


class Masked(Base):
    """3. 送る前にキー類を伏せ、画像・PDF の base64 は抜く。"""

    def test_keys_and_images_do_not_leave(self):
        L = Lines().user("キーは %s です" % KEY).assistant("了解")
        L.tool("Read", {"file_path": "/tmp/shot.png"},
               result=[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}],
               tur={"type": "image", "file": {"base64": PNG, "type": "image/png", "originalSize": 3004}})
        L.user("ふつうの指示").assistant()
        path = self.w.write(L)
        self.w.subagent(path, "agent-a1.jsonl", Lines().user("サブ %s" % KEY).assistant())
        self.w.page()
        self.w.decide()
        code, res = self.send()
        self.assertEqual(code, 0, res)
        main, sub = self.inbox.body(L.sid), self.inbox.body(L.sid, "agent-a1.jsonl")
        for body in (main, sub):
            self.assertNotIn(KEY.encode(), body)
            self.assertIn(b"[REDACTED:anthropic_key]", body)
            for line in body.splitlines():
                json.loads(line)
        self.assertNotIn(PNG[:200].encode(), main)
        self.assertEqual(main.count(b"[OMITTED:image/png 3004 bytes]"), 2)
        self.assertIn(L.encode().splitlines()[-1], main)     # 触らなかった行はバイト単位でそのまま
        self.assertEqual(self.finish()["sent"][0]["redactions"], 2)

    def test_keys_in_the_title_and_the_note_are_masked(self):
        L = Lines().user("キーは %s で" % KEY).assistant()
        self.w.write(L)
        self.w.page()
        self.assertNotIn(KEY, json.dumps(self.w.pending()))
        self.assertNotIn(KEY, self.w.page_html())
        self.w.decide(note="これ使って %s" % KEY)
        self.send()
        self.assertEqual(self.finish()["note"], "これ使って [REDACTED:anthropic_key]")
        self.assertEqual(self.finish()["sent"][0]["title"], "キーは [REDACTED:anthropic_key] で")


class UsersWords(Base):
    """4. 感想は本人がページに書いた言葉だけ。AI のメモは別の欄に。"""

    def setUp(self):
        super().setUp()
        self.conv("請求書の集計")
        self.w.page()

    def test_typed_note_arrives_as_is(self):
        note = "MCP のログインで迷った。\n「記号」$HOME `x` </script> も そのまま"
        self.w.decide(note=note)
        self.send()
        self.assertEqual(self.finish()["note"], note)
        self.assertNotIn("assistant_note", self.finish())

    def test_an_empty_note(self):
        self.w.decide(note="  ")
        code, res = self.send()
        self.assertEqual(self.finish()["note"], "")
        self.assertFalse(res["result"]["note"])

    def test_assistant_note_goes_to_its_own_field(self):
        self.w.decide(note="使えてる")
        self.send("--assistant-note", "-", stdin="start_submission が 1 回失敗した\n")
        self.assertEqual(self.finish()["note"], "使えてる")
        self.assertEqual(self.finish()["assistant_note"], "start_submission が 1 回失敗した")


class SendSession(Base):
    """5. 送信用の会話と、一覧の出力を含む会話は一覧に出さない。ページは送信用の会話の中でしか作らない。"""

    def test_send_conversations_are_not_listed(self):
        for cmd in (SEND_CMD, SEND_CMD_NS):
            L = Lines().user(cmd).user("スキル本文", isMeta=True).assistant("一覧").user("ついでに質問").assistant()
            self.w.write(L)
        ok, _ = self.conv("ふつうの会話")
        self.w.page()
        self.assertEqual(self.w.listed(), [ok.sid])

    def test_conversations_holding_the_list_are_not_listed(self):
        self.conv("ふつうの会話")
        self.w.page()
        for leaked in (json.dumps(self.w.pending(), ensure_ascii=False), self.w.page_html()):
            peek = Lines().user("調べて").assistant()
            peek.tool("Bash", {"command": "cat ~/.claude/plugins/data/send-to-nobu-androots/page/x.html"},
                      result=leaked)
            self.w.write(peek)
        sub_peek, path = self.conv("サブエージェントに調べさせた")
        self.w.subagent(path, "agent-x.jsonl",
                        Lines().user("読んで").tool("Read", {"file_path": "p"}, result=self.w.page_html()))
        self.w.next_day()
        self.w.page()
        self.assertEqual(len(self.w.listed()), 1)
        self.assertNotIn(sub_peek.sid, self.w.listed())

    def test_the_page_is_refused_in_an_ordinary_conversation(self):
        L = Lines().user("作業する").assistant().user(SEND_CMD).user("スキル本文", isMeta=True)
        self.w.write(L, project=World.SEND_PROJECT)
        self.conv("ふつうの会話")
        code, res = self.w.run("page", session=L.sid)
        self.assertEqual(code, 1)
        self.assertIn("新しい会話", res["say"])
        self.assertNotIn(agentlog.LIST_MARKER, res)
        self.assertIsNone(self.w.pending())
        self.assertFalse(os.path.exists(os.path.join(self.w.data, "page")))

    def test_the_send_to_nobu_routine_counts_as_a_send_conversation(self):
        routine = ('<scheduled-task name="send-to-nobu" file="/Users/alice/.claude/scheduled-tasks/send-to-nobu/SKILL.md">\n'
                   "This is an automated run of a scheduled task.\n\nsend-to-nobu スキルで今日のページを作る\n</scheduled-task>")
        L = Lines(base=time.time() - 60).user(routine).assistant("ページを作る")
        self.w.write(L, project=World.SEND_PROJECT)
        other, _ = self.conv("ふつうの会話")
        code, res = self.w.run("page", session=L.sid)
        self.assertEqual(code, 0, res)
        self.assertIn(other.sid, self.w.listed())
        self.assertNotIn(L.sid, self.w.listed())

    def test_another_routine_is_refused(self):
        routine = '<scheduled-task name="daily-check" file="/x/SKILL.md">\nsend-to-nobu のページも作って\n</scheduled-task>'
        L = Lines(base=time.time() - 60).user(routine).assistant()
        self.w.write(L, project=World.SEND_PROJECT)
        self.conv("ふつうの会話")
        code, res = self.w.run("page", session=L.sid)
        self.assertEqual(code, 1)
        self.assertIsNone(self.w.pending())

    def test_the_current_conversation_is_not_listed(self):
        self.w.append(self.w.session_file(), Lines(sid=self.w.current).user("いまの会話で話す").assistant())
        res = self.w.page()
        self.assertEqual((res["count"], res["next"]), (0, agentlog.END))


class ExcludedStays(Base):
    """6. 前に外した会話は、続きが書かれても外したまま（聞かない）。"""

    def test_excluded_stays_excluded_and_sent_comes_back(self):
        ex, ex_path = self.conv("外す会話", base=time.time() - 2000)
        sent, sent_path = self.conv("送る会話", base=time.time() - 1000)
        self.w.page()
        self.w.decide(exclude=[1])
        self.send()
        for L, path in ((ex, ex_path), (sent, sent_path)):
            self.w.append(path, Lines(sid=L.sid).user("続き").assistant())
        self.w.subagent(ex_path, "agent-late.jsonl", Lines().user("あとから").assistant())
        self.w.next_day()
        code, out = self.w.run("nudge", now=time.time() + 86400)
        self.assertIn("1 件", out["systemMessage"])
        self.w.page()
        self.assertEqual(self.w.listed(), [sent.sid])

    def test_exclusions_made_by_0_4_are_kept(self):
        L, path = self.conv("0.4 で外した会話")
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "state.json"), "w") as f:
            json.dump({"v": 1, "baseline": time.time() - 7 * 86400, "too_long": {}, "nudged_day": "2026-09-28",
                       "sessions": {L.sid: {"d": "excluded", "at": "2026-09-28T00:00:00Z", "offset": 10, "size": 10,
                                            "mtime": 0, "sub_n": 0, "sub_bytes": 0}}}, f)
        os.makedirs(os.path.join(self.w.data, "review", "1-abcdefgh"))
        self.assertEqual(self.w.page()["count"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.w.data, "review")))   # 0.4 の確認用の本文は消す


if __name__ == "__main__":
    unittest.main()
