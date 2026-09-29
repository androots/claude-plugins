# -*- coding: utf-8 -*-
"""守ること 1〜6。すべて一時の設定ディレクトリと合成の会話ログ。"""

import base64
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import SEND_CMD, SEND_CMD_NS, FakeInbox, Lines, Patched, World, agentlog  # noqa: E402

KEY = "sk-ant-api03-" + "Q1w2E3r4T5y6U7i8O9p0" * 4
PNG = base64.b64encode(b"\x89PNG" + b"\x00" * 3000).decode()
NONE, ALL = agentlog.NONE_LABEL, agentlog.ALL_LABEL


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
    """1. 本人が外した会話は送らない。送るのは一覧で見せた会話だけ、一覧を出した時点までの中身だけ。"""

    def test_excluded_conversation_is_not_sent(self):
        a, _ = self.conv("一つ目", base=time.time() - 3000)
        b, _ = self.conv("二つ目", base=time.time() - 2000)
        c, _ = self.conv("三つ目", base=time.time() - 1000)
        self.w.list()
        self.assertEqual(self.w.listed(), [a.sid, b.sid, c.sid])
        self.w.answer_simple(exclude="2")
        code, res = self.send()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["say"], "2 件送った・1 件外した")
        self.assertEqual(self.inbox.sent_ids(), sorted([a.sid, c.sid]))
        self.assertEqual(self.finish()["excluded_count"], 1)
        self.assertNotIn(b.sid, json.dumps(self.finish()))
        self.assertEqual(self.w.state()["sessions"][b.sid]["d"], "excluded")

    def test_nothing_written_after_the_list_is_sent(self):
        L, path = self.conv("最初の指示")
        sub = self.w.subagent(path, "agent-a1.jsonl", Lines().user("サブの指示").assistant("サブの答え"))
        with open(path, "rb") as f:
            at_list = f.read()
        with open(sub, "rb") as f:
            sub_at_list = f.read()
        self.w.list()
        # 一覧のあとで: 続きを書く・サブエージェントが増える・新しい会話ができる
        self.w.append(path, Lines(sid=L.sid).user("一覧のあとの指示").assistant())
        self.w.append(sub, Lines().user("一覧のあとのサブ").assistant())
        self.w.subagent(path, "agent-new.jsonl", Lines().user("新しいサブ").assistant())
        late, _ = self.conv("一覧のあとに始めた会話")
        self.w.answer_simple()
        code, res = self.send()
        self.assertEqual(code, 0, res)
        self.assertEqual(self.inbox.body(L.sid), at_list)
        self.assertEqual(self.inbox.body(L.sid, "agent-a1.jsonl"), sub_at_list)
        self.assertIsNone(self.inbox.body(L.sid, "agent-new.jsonl"))
        self.assertEqual(self.inbox.sent_ids(), [L.sid])
        # 続きは次の一覧に出る（一覧のあとに始めた会話も）
        self.w.next_day()
        self.w.list()
        self.assertEqual(sorted(self.w.listed()), sorted([L.sid, late.sid]))

    def test_too_many_shows_the_oldest_and_the_rest_comes_next(self):
        saved = agentlog.LIST_MAX
        agentlog.LIST_MAX = 3
        try:
            convs = [self.conv("会話 %d" % i, base=time.time() - 5000 + i * 100)[0] for i in range(5)]
            self.w.list()
            self.assertEqual(self.w.listed(), [c.sid for c in convs[:3]])
            self.assertIn("ほかに 2 件", self.w.pending()["questions"][0]["question"])
            self.w.answer_simple()
            code, res = self.send()
            self.assertEqual(res["sent"], 3)
            self.assertIn("ほかに 2 件ある", res["say"])
            self.assertEqual(self.inbox.sent_ids(), sorted(c.sid for c in convs[:3]))
            self.w.list()   # 同じ送信用の会話でもう一度
            self.assertEqual(self.w.listed(), [c.sid for c in convs[3:]])
        finally:
            agentlog.LIST_MAX = saved


class AnswerRequired(Base):
    """2. 本人の答えなしに送らない。答えはスクリプトが会話ログから直接読む。"""

    def setUp(self):
        super().setUp()
        self.a, _ = self.conv("会話 A", base=time.time() - 2000)
        self.b, _ = self.conv("会話 B", base=time.time() - 1000)
        self.w.list()

    def assertNothingSent(self, code, res):
        self.assertNotEqual(code, 0)
        self.assertEqual(self.inbox.objects, {})
        self.assertEqual(self.inbox.finish_bodies, [])
        self.assertEqual(self.w.state()["sessions"], {})

    def test_without_an_answer(self):
        code, res = self.send()
        self.assertNothingSent(code, res)
        self.assertIn("答えがなかった", res["say"])
        self.assertIsNotNone(self.w.pending())

    def test_closed_while_away(self):
        self.w.answer_simple(afk=60000)     # 離席で自動的に閉じた（選んでいた選択肢が入っていても数えない）
        self.assertNothingSent(*self.send())

    def test_empty_answers_and_errors(self):
        self.w.answer({})
        self.w.answer_simple(exclude="", note="")
        self.w.answer_simple(error=True)
        self.assertNothingSent(*self.send())

    def test_answers_put_in_by_the_ai(self):
        self.w.answer_simple(answers_in_input=True)
        self.assertNothingSent(*self.send())

    def test_rewritten_questions(self):
        qs = json.loads(json.dumps(self.w.pending()["questions"]))
        qs[0]["question"] = qs[0]["question"].replace("会話 B", "別の話")
        self.w.answer({qs[0]["question"]: NONE}, questions=qs)
        qs = json.loads(json.dumps(self.w.pending()["questions"]))
        qs[0]["options"] = qs[0]["options"][:1] + [{"label": "送る", "description": ""}]
        self.w.answer({qs[0]["question"]: NONE}, questions=qs)
        self.assertNothingSent(*self.send())

    def test_answers_before_the_list(self):
        self.w.answer_simple()
        self.w.list()       # 同じ会話でもう一度一覧を出した。前の答えは数えない
        self.assertNothingSent(*self.send())
        self.w.answer_simple()
        code, res = self.send()
        self.assertEqual(code, 0, res)

    def test_exclude_question_needs_an_explicit_answer(self):
        self.w.answer_simple(exclude=None, note="順調に使えてる")
        code, res = self.send()
        self.assertEqual(code, 3)
        self.assertEqual(res["ask"]["questions"], self.w.pending()["questions"])   # 同じ質問で聞き直す
        self.assertIn("答えがなかった", res["say"])
        self.assertEqual(self.inbox.objects, {})
        self.w.answer_simple(exclude=NONE)
        code, res = self.send()
        self.assertEqual((code, res["sent"]), (0, 2))

    def test_unreadable_numbers_are_asked_again(self):
        for text in ("1以外", "3", "0", "1-3", "特になし"):
            self.w.answer_simple(exclude=text)
            code, res = self.send()
            self.assertEqual(code, 3, text)
            self.assertIn("番号として読めなかった", res["say"])
        self.assertEqual(self.inbox.objects, {})

    def test_an_answer_that_was_asked_again_is_not_read_again(self):
        # 聞き直したあと、新しい答えがまだ会話ログに書かれていなくても、前の答えで送らない
        self.w.answer_simple(exclude="1以外")
        self.assertEqual(self.send()[0], 3)
        self.w.answer_simple(exclude="1以外", afk=60000)
        code, res = self.send()
        self.assertEqual(code, 1)
        self.assertIn("答えがなかった", res["say"])
        saved = agentlog.ANSWER_WAIT
        agentlog.ANSWER_WAIT = 5.0
        try:
            t = threading.Timer(0.3, self.w.answer_simple, kwargs={"exclude": "1"})
            t.start()
            code, res = self.send()
            t.join()
        finally:
            agentlog.ANSWER_WAIT = saved
        self.assertEqual((code, res["sent"], res["excluded"]), (0, 1, 1))

    def test_number_forms(self):
        count = 2
        for text, want in (("1", {1}), ("1, 2", {1, 2}), ("１、２", {1, 2}), ("1-2", {1, 2}), ("1〜2", {1, 2}),
                           ("2番", {2}), ("1と2", {1, 2}), ("なし", set()), ("ｎｏｎｅ", set())):
            self.assertEqual(agentlog.parse_numbers(text, count), want, text)
        self.assertIsNone(agentlog.parse_numbers("2-1", count))

    def test_send_everything_or_nothing(self):
        self.w.answer_simple(exclude=ALL, note="今日は送らないでおく")
        code, res = self.send()
        self.assertEqual(code, 0, res)
        self.assertEqual(self.inbox.objects, {})
        self.assertEqual(self.finish()["sent"], [])
        self.assertEqual(self.finish()["note"], "今日は送らないでおく")
        self.assertEqual(self.finish()["excluded_count"], 2)
        self.assertEqual(res["say"], "感想を送った（会話は 2 件外した）")

    def test_the_answer_line_can_arrive_a_moment_later(self):
        saved = agentlog.ANSWER_WAIT
        agentlog.ANSWER_WAIT = 5.0
        try:
            t = threading.Timer(0.3, self.w.answer_simple)
            t.start()
            code, res = self.send()
            t.join()
            self.assertEqual((code, res["sent"]), (0, 2))
        finally:
            agentlog.ANSWER_WAIT = saved

    def test_the_ai_cannot_pass_numbers_or_the_note(self):
        self.w.answer_simple(exclude=NONE)
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
        self.w.list()
        self.w.answer_simple()
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
        self.w.list()
        self.assertNotIn(KEY, json.dumps(self.w.pending()))
        self.w.answer_simple(note="これ使って %s" % KEY)
        self.send()
        self.assertEqual(self.finish()["note"], "これ使って [REDACTED:anthropic_key]")
        self.assertEqual(self.finish()["sent"][0]["title"], "キーは [REDACTED:anthropic_key] で")


class UsersWords(Base):
    """4. 感想は本人の言葉だけ。AI のメモは別の欄に。"""

    def setUp(self):
        super().setUp()
        self.conv("請求書の集計")
        self.w.list()

    def test_typed_note_arrives_as_is(self):
        note = "MCP のログインで迷った。\n「記号」$HOME `x` も そのまま"
        self.w.answer_simple(note=note)
        self.send()
        self.assertEqual(self.finish()["note"], note)
        self.assertNotIn("assistant_note", self.finish())

    def test_options_and_notes_field(self):
        q2 = self.w.pending()["note_question"]
        self.w.answer_simple(note="順調に使えてる", annotations={q2: {"notes": "補足も書いた"}})
        self.send()
        self.assertEqual(self.finish()["note"], "順調に使えてる\n補足も書いた")

    def test_nothing_to_say_is_an_empty_note(self):
        self.w.answer_simple(note="特になし")
        self.send()
        self.assertEqual(self.finish()["note"], "")

    def test_assistant_note_goes_to_its_own_field(self):
        self.w.answer_simple(note="使えてる")
        self.send("--assistant-note", "-", stdin="start_submission が 1 回失敗した\n")
        self.assertEqual(self.finish()["note"], "使えてる")
        self.assertEqual(self.finish()["assistant_note"], "start_submission が 1 回失敗した")

    def test_assistant_note_with_a_title_is_not_sent(self):
        self.w.answer_simple()
        self.send("--assistant-note", "-", stdin="「請求書の集計」を送るときに詰まった")
        self.assertNotIn("assistant_note", self.finish())


class SendSession(Base):
    """5. 送信用の会話と、一覧の出力を含む会話は一覧に出さない。一覧は送信用の会話の中でしか出さない。"""

    def test_send_conversations_are_not_listed(self):
        for cmd in (SEND_CMD, SEND_CMD_NS):
            L = Lines().user(cmd).user("スキル本文", isMeta=True).assistant("一覧").user("ついでに質問").assistant()
            self.w.write(L)
        ok, _ = self.conv("ふつうの会話")
        self.w.list()
        self.assertEqual(self.w.listed(), [ok.sid])

    def test_conversations_holding_the_list_are_not_listed(self):
        self.conv("ふつうの会話")
        self.w.list()
        leaked = json.dumps({agentlog.LIST_MARKER: 1, "ask": self.w.pending()["questions"]}, ensure_ascii=False)
        peek = Lines().user("調べて").assistant()
        peek.tool("Bash", {"command": "cat ~/.claude/plugins/data/send-to-nobu-androots/pending.json"}, result=leaked)
        self.w.write(peek)
        sub_peek, path = self.conv("サブエージェントに調べさせた")
        self.w.subagent(path, "agent-x.jsonl", Lines().user("読んで").tool("Read", {"file_path": "p"}, result=leaked))
        self.w.next_day()
        self.w.list()
        self.assertEqual(len(self.w.listed()), 1)
        self.assertNotIn(peek.sid, self.w.listed())
        self.assertNotIn(sub_peek.sid, self.w.listed())

    def test_the_list_is_refused_in_an_ordinary_conversation(self):
        L = Lines().user("作業する").assistant().user(SEND_CMD).user("スキル本文", isMeta=True)
        self.w.write(L, project=World.SEND_PROJECT)
        self.conv("ふつうの会話")
        code, res = self.w.run("list", session=L.sid)
        self.assertEqual(code, 1)
        self.assertIn("新しい会話", res["say"])
        self.assertNotIn(agentlog.LIST_MARKER, res)
        self.assertIsNone(self.w.pending())

    def test_the_current_conversation_is_not_listed(self):
        self.w.append(self.w.session_file(), Lines(sid=self.w.current).user("いまの会話で話す").assistant())
        self.w.list()
        self.assertEqual(self.w.listed(), [])


class ExcludedStays(Base):
    """6. 前に外した会話は、続きが書かれても外したまま（聞かない）。"""

    def test_excluded_stays_excluded_and_sent_comes_back(self):
        ex, ex_path = self.conv("外す会話", base=time.time() - 2000)
        sent, sent_path = self.conv("送る会話", base=time.time() - 1000)
        self.w.list()
        self.w.answer_simple(exclude="1")
        self.send()
        for L, path in ((ex, ex_path), (sent, sent_path)):
            self.w.append(path, Lines(sid=L.sid).user("続き").assistant())
        self.w.subagent(ex_path, "agent-late.jsonl", Lines().user("あとから").assistant())
        self.w.next_day()
        code, out = self.w.run("nudge", now=time.time() + 86400)
        self.assertIn("1 件", out["systemMessage"])
        self.w.list()
        self.assertEqual(self.w.listed(), [sent.sid])

    def test_exclusions_made_by_0_4_are_kept(self):
        L, path = self.conv("0.4 で外した会話")
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "state.json"), "w") as f:
            json.dump({"v": 1, "baseline": time.time() - 7 * 86400, "too_long": {}, "nudged_day": "2026-09-28",
                       "sessions": {L.sid: {"d": "excluded", "at": "2026-09-28T00:00:00Z", "offset": 10, "size": 10,
                                            "mtime": 0, "sub_n": 0, "sub_bytes": 0}}}, f)
        os.makedirs(os.path.join(self.w.data, "review", "1-abcdefgh"))
        self.w.list()
        self.assertEqual(self.w.listed(), [])
        self.assertFalse(os.path.exists(os.path.join(self.w.data, "review")))   # 0.4 の確認用の本文は消す


if __name__ == "__main__":
    unittest.main()
