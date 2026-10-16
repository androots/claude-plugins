# -*- coding: utf-8 -*-
"""レビュー役（ツールを持たない claude -p・Opus）への入力と、出力の整え方。claude は偽物。"""

import json
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Base, Lines, agentlog  # noqa: E402


class ReviewInput(Base):
    def test_the_summary_follows_the_day1_format(self):
        t0 = time.time() - 4000
        p = self.w.html("banner.html", "<h1>秋のキャンペーン</h1><script>var x='スクリプトの中身'</script>")
        A = Lines(base=t0).user("バナーを作って。横 1200×628").assistant("作ります").artifact(p)
        A.assistant("指定どおりのバナーを作った").user([
            {"type": "text", "text": "この画面みたいに"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo"}}])
        A.tool("Read", {"file_path": "x"}).tool("Read", {"file_path": "y"}).tool("mcp__claude-in-chrome__navigate", {})
        A.assistant("直した").user("[Request interrupted by user]").compact("auto").user("続き").assistant()
        A.compact("manual", "バナーは 1200×628・見出しは赤").user("戻る").assistant()
        self.w.write(A)
        B = Lines(base=t0 + 2000).user("<command-message>deck</command-message>\n<command-name>/deck</command-name>\n"
                                       "<command-args>来週の資料</command-args>").assistant("作った")
        B.meta("custom-title", customTitle="来週の資料づくり")
        self.w.write(B)
        self.prepared()
        text = self.review_text()
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("日付: ") and "会話 2 本" in lines[0])
        self.assertTrue(lines[1].startswith("表記: [本人] = 本人の発言"))
        heads = [x for x in lines if x.startswith("=== 会話 ")]
        self.assertRegex(heads[0], r"^=== 会話 c1  \d\d:\d\d–\d\d:\d\d  Opus 5\.5 high  「バナーを作って。横 1200×628」$")
        self.assertRegex(heads[1], r"^=== 会話 c2  .*「来週の資料づくり」$")
        block = text.split(heads[0])[1].split(heads[1])[0]
        self.assertEqual([x for x in block.strip().splitlines()], [
            "[本人] バナーを作って。横 1200×628",
            "[AI] 指定どおりのバナーを作った (Artifact)",
            "[本人] （画像を 1 枚貼って）この画面みたいに",
            "[AI] 直した (Read ×2, navigate)",
            "〔本人が AI の作業を途中で止めた〕",
            "〔自動 compact: 会話が長くなり、AI が自動で要約した〕",
            "[本人] 続き",
            "[AI] 了解",
            "[本人] /compact バナーは 1200×628・見出しは赤",
            "[本人] 戻る",
            "[AI] 了解",
            "--- アーティファクト a1（banner.html）",
            "banner.html\n秋のキャンペーン".split("\n")[0],
            "秋のキャンペーン"])
        self.assertNotIn("スクリプトの中身", text)
        self.assertIn("[本人] /deck 来週の資料", text)
        self.assertNotIn("要約の本文", text)

    def test_the_call(self):
        self.conv("請求書の集計")
        self.prepared()
        call = self.calls("review")[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for flag in ("--no-session-persistence", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")   # 本人の CLAUDE.md・フック・MCP を読ませない
        kit = self.inbox.kit["files"]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), kit["schema.json"])
        system = argv[argv.index("--system-prompt") + 1]
        self.assertTrue(system.startswith(kit["rubric.md"].strip()))
        self.assertIn(kit["examples.md"].strip(), system)
        for t in kit["catalog.json"]["types"]:
            self.assertIn("- %s: %s" % (t["id"], t["name"]), system)
        self.assertIn("- free:", system)
        self.assertEqual(call["judge_env"], "1")
        self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(os.getcwd()))

    def test_ids_go_by_time(self):
        late, _ = self.conv("あとの会話", base=time.time() - 1000)
        early, _ = self.conv("さきの会話", base=time.time() - 3000)
        self.prepared()
        heads = re.findall(r"^=== 会話 (c\d+) .*「(.*)」$", self.review_text(), re.M)
        self.assertEqual(heads, [("c1", "さきの会話"), ("c2", "あとの会話")])

    def test_a_big_day_is_squeezed_per_conversation(self):
        saved = agentlog.REVIEW_CHARS
        agentlog.REVIEW_CHARS = 6000
        try:
            small, _ = self.conv("小さい会話", base=time.time() - 5000)
            L = Lines(base=time.time() - 4000).user("大きい会話の最初")
            for i in range(200):
                L.assistant("返事 %d" % i).user("直し %03d " % i + "あ" * 50)
            L.assistant().user("大きい会話の最後").assistant()
            self.w.write(L)
            self.prepared()
        finally:
            agentlog.REVIEW_CHARS = saved
        text = self.review_text()
        self.assertLessEqual(len(text), 6000 + 400)
        self.assertIn("[本人] 小さい会話", text)
        self.assertIn("[本人] 大きい会話の最初", text)
        self.assertIn("[本人] 大きい会話の最後", text)
        self.assertRegex(text, r"（途中の \d+ 行を省いた）")

    def test_a_long_message_keeps_the_head(self):
        self.conv("会議の文字起こし " + "あ" * 3000)
        self.prepared()
        line = [x for x in self.review_text().splitlines() if x.startswith("[本人] 会議")][0]
        self.assertTrue(line.endswith("…（全 %d 字）" % len("会議の文字起こし " + "あ" * 3000)))
        self.assertLess(len(line), agentlog.HUMAN_MAX + 30)


class ReviewOutput(Base):
    def setUp(self):
        super().setUp()
        self.conv("見出しを直して", base=time.time() - 2000)
        self.conv("議事録", base=time.time() - 1000)

    def test_retry_once_then_succeed(self):
        self.fake_review(fail=1)
        res = self.prepared()
        self.assertEqual(len(self.calls("review")), 2)
        self.assertEqual(len(self.report()["advices"]), 1)
        self.assertNotIn("アドバイスを作れなかった", res["say"])

    def test_two_failures_make_a_page_without_advice(self):
        p = self.w.html("x.html", "<p>x</p>")
        self.w.write(Lines().user("ページを作って").assistant().artifact(p).assistant())
        self.fake_review(fail=2)
        res = self.prepared()
        self.assertEqual(len(self.calls("review")), 2)
        rep = self.report()
        self.assertEqual((rep["advices"], rep["praises"], rep["shots"]), ([], [], {}))   # 何を伏せるか分からないので撮らない
        self.assertEqual([c["title"] for c in rep["convs"]], ["ページを作って", "見出しを直して", "議事録"])
        self.assertIn("アドバイスを作れなかった", res["say"])
        self.assertEqual(self.w.last()["review"], "failed")
        self.assertEqual([e[:6] for e in self.w.last()["review_errors"]], ["failed", "failed"])

    def test_the_output_is_shaped_to_the_report(self):
        self.fake_review(output={
            "advices": [
                {"type": "makaseru", "conv": "c1", "say": "あ" * 50, "key": "目的", "quotes": [], "count": "26 往復です。とても長い説明",
                 "n": -1, "items": ["一" * 20], "next_label": "x", "yesterday": "y", "next": "z",
                 "prompt": "目的と好みを渡して"},
                {"type": "narabe", "conv": "c9", "say": "無い会話", "key": "", "quotes": [], "count": "", "n": 1,
                 "items": [], "next_label": "", "yesterday": "", "next": "", "prompt": ""},
                {"type": "free", "conv": "c2", "say": "自由", "key": "無い肝", "quotes": [], "count": "", "n": 0,
                 "items": [], "next_label": "", "yesterday": "", "next": "", "prompt": "p", "free_title": "その他の型"}],
            "praises": [{"conv": "c1", "say": "よい", "first": True}, {"conv": "c1", "say": "同じ会話"},
                        {"conv": "c2", "say": "別の会話", "first": "yes"}],
            "convs": [{"id": "c1", "title": "見出し", "flow": ["a", "b", 3], "thumb": "pdf", "gaveup": True,
                       "memo": {"verdict": "言う", "why": "理由"}},
                      {"id": "c2", "title": "議事", "flow": [], "thumb": "doc", "gaveup": False,
                       "memo": {"verdict": "たぶん", "why": "x"}}],
            "hide": {}})
        self.prepared()
        rep = self.report()
        a1, a2 = rep["advices"]
        self.assertEqual((a1["type"], len(a1["say"]), a1["scene"]["n"], a1["scene"]["count"], a1["today"]),
                         ("free", 30, None, "26 往復です。とても…", True))       # 型に無い → free
        self.assertEqual(a1["free_title"], agentlog.squash(a1["say"], 12))
        self.assertEqual(len(a1["scene"]["items"][0]), 12)
        self.assertEqual(a2["free_title"], "その他の型")
        self.assertEqual((a2["conv"], a2["key"], a2["today"]), ("c2", "", False))
        self.assertEqual([(p["conv"], p["first"]) for p in rep["praises"]], [("c1", True), ("c2", False)])
        c1, c2 = rep["convs"]
        self.assertEqual((c1["flow"], c1["thumb"], c1["gaveup"], c1["memo"]),
                         (["a", "b"], {"kind": "doc"}, True, {"verdict": "言う", "why": "理由"}))
        self.assertNotIn("memo", c2)

    def test_a_reply_that_is_not_an_object_is_a_failure(self):
        for out in ([], "text", None):
            self.fake_review(output=out)
            self.prepared()
            self.assertEqual(self.w.last()["review"], "failed", out)


if __name__ == "__main__":
    unittest.main()
