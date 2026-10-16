# -*- coding: utf-8 -*-
"""外す判定（ツールを持たない claude -p が、私的・仕事以外の会話を外す）。claude は偽物（tests/fake_claude.py）。"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import FAKE_CLAUDE, Base, Lines, agentlog  # noqa: E402


class Judge(Base):
    def says_of(self, call, word):
        """判定の入力のうち、word を含む会話のやりとり [(誰, 文)]。"""
        for c in json.loads(call["stdin"]):
            if any(word in s["text"] for s in c["says"]):
                return [(s["who"], s["text"]) for s in c["says"]]
        raise AssertionError(word)

    def test_what_is_read(self):
        # 本人の発言・AI の返事・ツールの結果（範囲の中だけ）。画像・思考・サブエージェントは読まない
        L = Lines().user("この CSV を整形して").assistant("AI の返事: 了解しました")
        L.ai([{"type": "thinking", "thinking": "AI の思考", "signature": "x"}])
        L.tool("Bash", {"command": "cat list.csv"}, result="ツールの結果: 山田 090-1234-5678 " + "長" * 1000)
        L.tool("Read", {"file_path": "a.png"}, result=[
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo"}}])
        L.user([{"type": "text", "text": "キーは sk-ant-api03-%s" % ("Q1w2E3r4T5" * 4)},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo"}}])
        L.assistant("長い返事 " + "ん" * 3000)
        self.w.subagent(self.w.write(L), "agent-a.jsonl", Lines().user("サブの指示").assistant())
        self.prepared()
        calls = self.calls("judge")
        self.assertEqual(len(calls), 1)
        says = self.says_of(calls[0], "CSV")
        self.assertEqual([w for w, _ in says], ["本人", "AI", "ツール", "本人", "AI"])
        self.assertEqual(says[1], ("AI", "AI の返事: 了解しました"))
        self.assertEqual(len(says[2][1]), agentlog.JUDGE_TOOL_MAX)
        self.assertEqual(says[3], ("本人", "キーは [REDACTED:anthropic_key]"))
        self.assertEqual(len(says[4][1]), agentlog.JUDGE_AI_MAX)
        for word in ("AI の思考", "iVBOR", "サブの指示", "sk-ant-api03"):
            self.assertNotIn(word, calls[0]["stdin"])
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--tools") + 1], "")       # ツールを持たない
        for flag in ("--no-session-persistence", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], agentlog.JUDGE_SYSTEM)
        schema = json.loads(argv[argv.index("--json-schema") + 1])["properties"]["results"]["items"]
        self.assertIn("仕事以外", schema["properties"]["reason"]["enum"])
        self.assertEqual(calls[0]["judge_env"], "1")
        self.assertNotEqual(os.path.realpath(calls[0]["cwd"]), os.path.realpath(os.getcwd()))

    def test_only_lines_inside_the_range_are_read(self):
        now = time.time()
        L = Lines().user("きのうより前の話 古い指示", at=now - 3 * 86400).assistant(at=now - 3 * 86400 + 60)
        L.user("今日の指示", at=now - 600).assistant(at=now - 540)
        self.w.write(L)
        self.prepared(now=now)
        says = self.says_of(self.calls("judge")[0], "今日の指示")
        self.assertNotIn("古い指示", json.dumps(says, ensure_ascii=False))

    def test_not_work_is_excluded(self):
        self.conv("請求書の集計", base=time.time() - 2000)
        self.conv("週末の旅行の宿を探して", base=time.time() - 1000)
        self.fake_judge(flag={"旅行": "仕事以外"})
        res = self.prepared()
        self.assertEqual((res["count"], res["removed"]), (1, 1))
        self.assertNotIn("旅行", self.review_text())

    def test_a_partly_failed_check_keeps_the_unchecked(self):
        saved = agentlog.JUDGE_BATCH
        agentlog.JUDGE_BATCH = 1
        self.conv("愚痴を聞いて", base=time.time() - 3000)
        self.conv("議事録", base=time.time() - 2000)
        self.conv("請求書", base=time.time() - 1000)
        self.fake_judge(flag={"愚痴": "愚痴"}, fail_on="議事録")
        try:
            res = self.prepared()
        finally:
            agentlog.JUDGE_BATCH = saved
        self.assertEqual((res["count"], res["removed"]), (2, 1))
        self.assertIn("1 本は私的かどうかを確かめられなかった", res["say"])

    def test_broken_results_count_as_unchecked(self):
        self.conv("A", base=time.time() - 2000)
        self.conv("B", base=time.time() - 1000)
        for conf, env in (({"mode": "drop"}, None), ({}, "")):
            self.fake_judge(**conf)
            if env is not None:
                os.environ[agentlog.JUDGE_BIN_ENV] = env     # claude が見つからない
            try:
                res = self.prepared()
            finally:
                os.environ[agentlog.JUDGE_BIN_ENV] = FAKE_CLAUDE
            self.assertEqual((res["count"], self.w.last()["judge_failed"]), (2, 2), conf)

    def test_the_check_has_a_time_limit(self):
        saved = agentlog.JUDGE_BUDGET
        agentlog.JUDGE_BUDGET = 0.5
        self.conv("A")
        self.fake_judge(mode="slow")
        try:
            t0 = time.time()
            self.prepared()
        finally:
            agentlog.JUDGE_BUDGET = saved
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(self.w.last()["judge_failed"], 1)

    def test_long_conversations_keep_the_head_and_the_tail(self):
        L = Lines().user("先頭の指示 " + "あ" * 5000).assistant()
        for i in range(30):
            L.user("途中 %d " % i + "う" * 100).assistant()
        L.user("い" * 5000 + " 末尾の指示").assistant()
        self.w.write(L)
        self.prepared()
        says = [t for _, t in self.says_of(self.calls("judge")[0], "先頭の指示")]
        self.assertLessEqual(sum(len(x) for x in says), agentlog.JUDGE_CHARS)
        self.assertTrue(says[0].startswith("先頭の指示") and says[-2].endswith("末尾の指示"))

    def test_shorten_says_keeps_positions(self):
        says = ["a" * 10, "b" * 10, "c" * 10, "d" * 10]
        self.assertEqual(agentlog.shorten_says(says, 100), (says, [0, 1, 2, 3]))
        self.assertEqual(agentlog.shorten_says(says, 20), (["a" * 10, "d" * 10], [0, 3]))


if __name__ == "__main__":
    unittest.main()
