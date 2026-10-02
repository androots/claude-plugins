# -*- coding: utf-8 -*-
"""AI の確認（ツールを持たない claude -p が外す候補を提案する）。claude は偽物（tests/fake_claude.py）。"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Lines, agentlog  # noqa: E402
from test_guarantees import Base  # noqa: E402


class Judge(Base):
    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.w.tmp, "judge.jsonl")
        self.a, _ = self.conv("請求書の集計をして", base=time.time() - 3000)
        self.b, _ = self.conv("部長の愚痴を聞いてほしい", base=time.time() - 2000)
        self.c, _ = self.conv("議事録のテンプレート", base=time.time() - 1000)

    def tearDown(self):
        os.environ.pop("FAKE_JUDGE", None)
        super().tearDown()

    def fake(self, **conf):
        os.environ["FAKE_JUDGE"] = json.dumps(dict(conf, record=self.record), ensure_ascii=False)

    def judged(self):
        """偽の claude が受け取った呼び出し [{argv, stdin, judge_env, cwd}]。"""
        with open(self.record, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def text_of(self, L, call):
        n = {it["session_id"]: it["n"] for it in self.w.pending()["items"]}[L.sid]
        return {c["n"]: c["text"] for c in json.loads(call["stdin"])}[n]

    def screen(self):
        q = self.w.pending()["questions"][0]
        return q["question"].splitlines(), [o["label"] for o in q["options"]]

    def test_candidates_are_marked_and_can_be_excluded_as_suggested(self):
        self.fake(flag={"愚痴": "愚痴"})
        self.w.list()
        lines, labels = self.screen()
        self.assertEqual(lines[1], "【候補】= AI が中身を読んで、外した方がよさそうと思った会話")
        self.assertEqual(lines[3:6], ["1. 請求書の集計をして", "2. 【候補: 愚痴】部長の愚痴を聞いてほしい", "3. 議事録のテンプレート"])
        self.assertEqual(labels, ["提案どおり外す（2）", agentlog.NONE_LABEL, agentlog.PASS_LABEL])
        self.w.answer_simple(exclude="提案どおり外す（2）")
        code, res = self.send()
        self.assertEqual((code, res["say"]), (0, "2 件送った・1 件外した"))
        self.assertEqual(self.inbox.sent_ids(), sorted([self.a.sid, self.c.sid]))
        self.assertEqual(self.w.state()["sessions"][self.b.sid]["d"], "excluded")

    def test_the_suggestion_is_only_an_option(self):
        # 提案があっても、答えなしでは送らない・「なし」なら全部送る
        self.fake(flag={"愚痴": "愚痴"})
        self.w.list()
        code, res = self.send()
        self.assertEqual(code, 1)
        self.assertEqual(self.inbox.objects, {})
        self.w.answer_simple(exclude=agentlog.NONE_LABEL)
        code, res = self.send()
        self.assertEqual((code, res["sent"]), (0, 3))

    def test_a_failed_check_falls_back_to_titles_only(self):
        for conf, env in (({"mode": "error"}, None), ({}, ""), ({"mode": "drop"}, None)):
            self.fake(**conf)
            if env is not None:
                os.environ[agentlog.JUDGE_BIN_ENV] = env     # claude が見つからない
            try:
                self.w.list()
            finally:
                os.environ[agentlog.JUDGE_BIN_ENV] = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                  "fake_claude.py")
            lines, labels = self.screen()
            self.assertEqual(lines[1], "AI の確認は今回できなかった。タイトルを見て選んでね", conf)
            self.assertEqual(labels, [agentlog.NONE_LABEL, agentlog.PASS_LABEL])
            self.assertFalse(any(it.get("flag") for it in self.w.pending()["items"]))
        self.w.answer_simple()
        code, res = self.send()
        self.assertEqual((code, res["sent"]), (0, 3))

    def test_the_whole_check_has_a_time_limit(self):
        saved = agentlog.JUDGE_BUDGET
        agentlog.JUDGE_BUDGET = 0.5
        self.fake(mode="slow", flag={"愚痴": "愚痴"})
        try:
            t0 = time.time()
            self.w.list()
        finally:
            agentlog.JUDGE_BUDGET = saved
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(self.screen()[0][1], "AI の確認は今回できなかった。タイトルを見て選んでね")

    def test_only_the_users_own_words_are_read(self):
        L = Lines().user("この CSV を整形して").assistant("AI の返事: 了解しました")
        L.tool("Bash", {"command": "cat list.csv"}, result="ツールの結果: 山田 090-1234-5678")
        L.user([{"type": "text", "text": "キーは sk-ant-api03-%s" % ("Q1w2E3r4T5" * 4)},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo"}}])
        self.w.subagent(self.w.write(L), "agent-a.jsonl", Lines().user("サブの指示").assistant())
        self.fake()
        self.w.list()
        calls = self.judged()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.text_of(L, calls[0]), "この CSV を整形して\n\nキーは [REDACTED:anthropic_key]")
        for word in ("AI の返事", "ツールの結果", "iVBOR", "サブの指示"):
            self.assertNotIn(word, calls[0]["stdin"])
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")       # ツールを持たない
        self.assertIn("--no-session-persistence", argv)              # 判定の会話を残さない
        self.assertEqual(calls[0]["judge_env"], "1")
        self.assertNotEqual(os.path.realpath(calls[0]["cwd"]), os.path.realpath(os.getcwd()))

    def test_long_conversations_keep_the_head_and_the_tail(self):
        L = Lines().user("先頭の指示 " + "あ" * 5000).assistant().user("い" * 5000 + " 末尾の指示").assistant()
        self.w.write(L)
        self.fake()
        self.w.list()
        text = self.text_of(L, self.judged()[0])
        self.assertLessEqual(len(text), agentlog.JUDGE_CHARS)
        self.assertTrue(text.startswith("先頭の指示") and text.endswith("末尾の指示"))

    def test_nudge_is_quiet_inside_the_check(self):
        os.environ[agentlog.JUDGE_ENV] = "1"
        try:
            code, out = self.w.run("nudge")
        finally:
            os.environ.pop(agentlog.JUDGE_ENV)
        self.assertEqual((code, out), (0, {}))
        self.assertIn("3 件", self.w.run("nudge")[1]["systemMessage"])


if __name__ == "__main__":
    unittest.main()
