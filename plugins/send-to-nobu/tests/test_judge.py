# -*- coding: utf-8 -*-
"""AI の確認（ツールを持たない claude -p が外す候補を提案する）。claude は偽物（tests/fake_claude.py）。"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import FAKE_CLAUDE, Lines, agentlog  # noqa: E402
from test_guarantees import Base  # noqa: E402


class Judge(Base):
    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.w.tmp, "judge.jsonl")
        self.a, _ = self.conv("請求書の集計をして", base=time.time() - 3000)
        self.b, _ = self.conv("この表を整形して", "部長の愚痴を聞いてほしい\n昨日も怒鳴られた", base=time.time() - 2000)
        self.c, _ = self.conv("議事録のテンプレート", base=time.time() - 1000)

    def tearDown(self):
        os.environ.pop("FAKE_JUDGE", None)
        os.environ[agentlog.JUDGE_BIN_ENV] = FAKE_CLAUDE
        super().tearDown()

    def fake(self, **conf):
        os.environ["FAKE_JUDGE"] = json.dumps(dict(conf, record=self.record), ensure_ascii=False)

    def judged(self):
        """偽の claude が受け取った呼び出し [{argv, stdin, judge_env, cwd}]。"""
        with open(self.record, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def says_of(self, L, call):
        n = {it["session_id"]: it["n"] for it in self.w.pending()["items"]}[L.sid]
        return {c["n"]: c["says"] for c in json.loads(call["stdin"])}[n]

    def flags(self):
        return {it["n"]: it["flag"] for it in self.w.page_data()["items"]}

    def test_candidates_carry_the_reason_a_word_and_the_users_own_line(self):
        self.fake(flag={"愚痴": "愚痴"}, why="上司への不満が中心")
        self.w.page()
        data = self.w.page_data()
        self.assertEqual((data["judge"], data["failed"]), ("ok", 0))
        self.assertEqual(self.flags(), {1: None, 3: None, 2: {
            "reason": "愚痴", "why": "上司への不満が中心", "quote": "部長の愚痴を聞いてほしい 昨日も怒鳴られた"}})
        # 候補でも、決めるのは本人（ページで押した答えだけで外す）
        self.w.decide(exclude=[])
        code, res = self.send()
        self.assertEqual((code, res["sent"]), (0, 3))

    def test_a_long_word_is_cut_and_a_long_line_is_shortened(self):
        self.conv("お金の相談 " + "い" * 200, base=time.time() - 500)
        self.fake(flag={"お金": "個人のお金"}, why="家計の相談。ローンと貯金と保険の見直しについて長く")
        self.w.page()
        flag = self.flags()[4]
        self.assertEqual(flag["why"], "家計の相談。ローンと貯金と保険の見直しについて長く"[:agentlog.WHY_MAX])
        self.assertEqual(len(flag["quote"]), agentlog.QUOTE_MAX)
        self.assertTrue(flag["quote"].startswith("お金の相談") and flag["quote"].endswith("…"))

    def test_a_quote_outside_the_says_is_empty(self):
        for q in (5, -1, -3):
            self.fake(flag={"愚痴": "愚痴"}, quote=q)
            self.w.page()
            self.assertEqual(self.flags()[2]["quote"], "", q)
            self.assertEqual(self.flags()[2]["reason"], "愚痴")

    def test_a_failed_check_falls_back_to_titles_only(self):
        for conf, env in (({"mode": "error"}, None), ({}, ""), ({"mode": "drop"}, None)):
            self.fake(**conf)
            if env is not None:
                os.environ[agentlog.JUDGE_BIN_ENV] = env     # claude が見つからない
            try:
                self.w.page()
            finally:
                os.environ[agentlog.JUDGE_BIN_ENV] = FAKE_CLAUDE
            data = self.w.page_data()
            self.assertEqual((data["judge"], data["failed"]), ("failed", 3), conf)
            self.assertEqual(list(self.flags().values()), [None] * 3)
        self.w.decide()
        code, res = self.send()
        self.assertEqual((code, res["sent"]), (0, 3))

    def test_a_partly_failed_check(self):
        saved = agentlog.JUDGE_BATCH
        agentlog.JUDGE_BATCH = 1
        self.fake(flag={"愚痴": "愚痴"}, fail_on="議事録")
        try:
            self.w.page()
        finally:
            agentlog.JUDGE_BATCH = saved
        data = self.w.page_data()
        self.assertEqual((data["judge"], data["failed"]), ("partial", 1))
        self.assertEqual(self.flags()[2]["reason"], "愚痴")

    def test_the_whole_check_has_a_time_limit(self):
        saved = agentlog.JUDGE_BUDGET
        agentlog.JUDGE_BUDGET = 0.5
        self.fake(mode="slow", flag={"愚痴": "愚痴"})
        try:
            t0 = time.time()
            self.w.page()
        finally:
            agentlog.JUDGE_BUDGET = saved
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(self.w.page_data()["judge"], "failed")

    def test_only_the_users_own_words_are_read(self):
        L = Lines().user("この CSV を整形して").assistant("AI の返事: 了解しました")
        L.tool("Bash", {"command": "cat list.csv"}, result="ツールの結果: 山田 090-1234-5678")
        L.user([{"type": "text", "text": "キーは sk-ant-api03-%s" % ("Q1w2E3r4T5" * 4)},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo"}}])
        self.w.subagent(self.w.write(L), "agent-a.jsonl", Lines().user("サブの指示").assistant())
        self.fake()
        self.w.page()
        calls = self.judged()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.says_of(L, calls[0]), ["この CSV を整形して", "キーは [REDACTED:anthropic_key]"])
        for word in ("AI の返事", "ツールの結果", "iVBOR", "サブの指示"):
            self.assertNotIn(word, calls[0]["stdin"])
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")       # ツールを持たない
        self.assertIn("--no-session-persistence", argv)              # 判定の会話を残さない
        self.assertEqual(argv[argv.index("--system-prompt") + 1], agentlog.JUDGE_SYSTEM)
        schema = json.loads(argv[argv.index("--json-schema") + 1])["properties"]["results"]["items"]
        self.assertEqual(schema["required"], ["n", "exclude", "reason", "why", "quote"])
        self.assertEqual(calls[0]["judge_env"], "1")
        self.assertNotEqual(os.path.realpath(calls[0]["cwd"]), os.path.realpath(os.getcwd()))

    def test_long_conversations_keep_the_head_and_the_tail_and_quote_the_original(self):
        L = Lines().user("先頭の指示 " + "あ" * 5000).assistant()
        for i in range(30):
            L.user("途中 %d " % i + "う" * 100).assistant()
        L.user("い" * 5000 + " 末尾の指示 愚痴").assistant()
        self.w.write(L)
        self.fake(flag={"愚痴": "愚痴"})
        self.w.page()
        says = self.says_of(L, self.judged()[0])
        self.assertLessEqual(sum(len(x) for x in says), agentlog.JUDGE_CHARS)
        self.assertTrue(says[0].startswith("先頭の指示") and says[-1].endswith("末尾の指示 愚痴"))
        self.assertLess(len(says), 32)
        # quote は渡した位置（末尾）でなく、元の発言の位置から本人の発言そのものを抜く
        n = {it["session_id"]: it["n"] for it in self.w.pending()["items"]}[L.sid]
        self.assertEqual(self.flags()[n]["quote"], ("い" * 5000)[:agentlog.QUOTE_MAX - 1] + "…")

    def test_shorten_says_keeps_positions(self):
        says = ["a" * 10, "b" * 10, "c" * 10, "d" * 10]
        self.assertEqual(agentlog.shorten_says(says, 100), (says, [0, 1, 2, 3]))
        shown, where = agentlog.shorten_says(says, 20)
        self.assertEqual((shown, where), (["a" * 10, "d" * 10], [0, 3]))
        shown, where = agentlog.shorten_says(["x" * 50], 20)
        self.assertEqual((len(shown[0]), where), (10, [0]))

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
