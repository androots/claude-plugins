# -*- coding: utf-8 -*-
"""並列に立てた会話のまとまり・ぼかしの札・時間の上限。"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Base, Lines, agentlog, image_bytes  # noqa: E402


class Groups(Base):
    def parallel(self, n, base, title="AIR ONE 発売ページ（比較）", step=60, origin=None, extra_turns=0):
        out = []
        for i in range(n):
            L = Lines(base=base + i * step)
            kw = {"origin": origin} if origin else {}
            L.user("発売ページを比較して %d" % i, **kw).assistant()
            for k in range(extra_turns if i == 1 else 0):
                L.user("追加の直し %d" % k).assistant()
            L.meta("custom-title", customTitle=title)
            self.w.write(L)
            out.append(L)
        return out

    def test_parallel_conversations_become_one_row(self):
        t0 = time.time() - 5000
        p = self.w.html("cmp.html", "<p>比較</p>")
        group = self.parallel(3, t0, extra_turns=2)
        L = Lines(base=t0 + 30).user("発売ページを比較して 9").assistant().artifact(p).assistant()
        L.meta("custom-title", customTitle="AIR ONE 発売ページ（比較）")
        self.w.write(L)
        self.conv("別の仕事", base=t0 + 100)
        res = self.prepared()
        self.assertEqual(res["count"], 5)
        rep = self.report()
        rows = rep["convs"]
        self.assertEqual(len(rows), 2)
        g = [r for r in rows if r.get("group")][0]
        self.assertEqual(g["group"], {"n": 4})
        self.assertEqual(rep["numbers"]["convs"], 5)
        self.assertEqual(g["shots"], ["s1.jpg"])           # まとめた会話のアーティファクトも代表に寄せる
        text = self.review_text()
        self.assertEqual(text.count("=== 会話 "), 2)
        self.assertIn("×4（同じ頼み方で並列に立てた 4 本。中身は代表の 1 本。", text)
        self.assertIn("[本人] 追加の直し 1", text)           # 代表は中身の一番多い 1 本
        self.assertNotIn("発売ページを比較して 2", text)
        self.assertIn("会話 5 本", text.splitlines()[0])
        # まとめた行を送らないにすると、まとめた本数ぶん外れる
        self.w.decide(exclude=[g["id"]])
        code, out = self.send()
        self.assertEqual(code, 0, out)
        body = self.inbox.body()
        self.assertEqual((body["report"]["numbers"]["convs"], body["removed_count"]), (1, 4))

    def test_a_count_written_into_the_title_is_removed(self):
        self.parallel(3, time.time() - 3000)
        self.conv("ひとつだけの会話 x 2", base=time.time() - 1000)
        self.fake_review(output={"advices": [], "praises": [], "hide": {}, "convs": [
            {"id": "c1", "title": "AIR ONE 発売ページ ×3", "flow": ["a", "b"], "thumb": "doc", "gaveup": False,
             "memo": {"verdict": "言わない", "why": "x"}},
            {"id": "c2", "title": "ひとつだけの会話 x 2", "flow": ["a", "b"], "thumb": "doc", "gaveup": False,
             "memo": {"verdict": "言わない", "why": "x"}}]})
        self.prepared()
        self.assertIn("title には数を入れない", self.review_text())
        rows = self.report()["convs"]
        self.assertEqual((rows[0]["title"], rows[0]["group"]), ("AIR ONE 発売ページ", {"n": 3}))
        self.assertEqual(rows[1]["title"], "ひとつだけの会話 x 2")     # まとめていない行は触らない

    def test_strip_count(self):
        for raw in ("AIR ONE 発売ページ ×12", "AIR ONE 発売ページ x 12", "AIR ONE 発売ページ（12 本）",
                    "AIR ONE 発売ページ (×12本)", "AIR ONE 発売ページ・12本", "AIR ONE 発売ページ ✕１２", "AIR ONE 発売ページ"):
            self.assertEqual(agentlog.strip_count(raw), "AIR ONE 発売ページ", raw)
        self.assertEqual(agentlog.strip_count("Windows 11"), "Windows 11")
        self.assertEqual(agentlog.strip_count("×3"), "×3")              # 数だけなら残す（呼び出し側が元の題に戻す）

    def test_far_apart_or_different_ones_stay_apart(self):
        t0 = time.time() - 7000
        self.parallel(2, t0, step=11 * 60)                  # 同じ題でも 10 分より離れていればまとめない
        self.parallel(2, t0 + 3000, title="別の題", step=30)
        self.prepared()
        self.assertEqual([r.get("group", {}).get("n", 1) for r in self.report()["convs"]], [1, 1, 2])

    def test_a_chain_within_10_minutes_joins_but_stops_at_3_hours(self):
        t0 = time.time() - 6 * 3600
        self.parallel(20, t0, step=9 * 60)          # 9 分おきに 20 本 = 171 分 → 1 つにつながる
        self.parallel(3, t0 + 5 * 3600, step=60)    # 同じ頼み方でも午後の別の仕事は別のまとまり（間が空く）
        self.prepared()
        self.assertEqual([r.get("group", {}).get("n", 1) for r in self.report()["convs"]], [20, 3])

    def test_a_long_chain_is_cut_at_3_hours(self):
        t0 = time.time() - 8 * 3600
        self.parallel(40, t0, step=9 * 60)          # 9 分おきに 40 本 = 351 分。3 時間ごとに切る
        self.prepared()
        ns = [r.get("group", {}).get("n", 1) for r in self.report()["convs"]]
        self.assertEqual((sum(ns), len(ns)), (40, 2))
        self.assertEqual(ns[0], 21)                 # 0〜180 分の 21 本

    def test_conversations_started_from_another_conversation(self):
        t0 = time.time() - 7000
        # 別の会話から立てた会話（最初の発言の origin が人でない）は 30 分まで。題は AI が付けたもので揃わない
        for i in range(3):
            L = Lines(base=t0 + i * 25 * 60)        # 25 分おき（人の会話なら 10 分で切れる）
            L.user("調べて: 競合 3 社の価格", origin={"kind": "peer"}).user("調べて: 競合 3 社の価格").assistant()
            L.meta("ai-title", aiTitle="題 %d" % i)
            self.w.write(L)
        self.prepared()
        self.assertEqual([r.get("group", {}).get("n", 1) for r in self.report()["convs"]], [3])

    def test_quotes_can_come_from_any_member(self):
        self.parallel(2, time.time() - 3000)
        self.fake_review(output={"advices": [{"type": "narabe", "conv": "c1", "say": "x", "key": "", "count": "",
                                              "n": 0, "items": [], "next_label": "", "yesterday": "", "next": "",
                                              "prompt": "", "quotes": ["発売ページを比較して 0", "発売ページを比較して 1"]}],
                                 "praises": [], "convs": [], "hide": {}})
        self.prepared()
        self.assertEqual(self.report()["advices"][0]["scene"]["quotes"], ["発売ページを比較して 0", "発売ページを比較して 1"])


class HideLabels(Base):
    def setUp(self):
        super().setUp()
        p = self.w.html("lp.html", "<p>取引先 ABC 商事 卸値 120 円</p>")
        self.w.write(Lines().user("LP を作って").assistant().artifact(p).assistant())

    def test_labels_are_sent_and_the_text_is_not(self):
        # キット v2: hide = {"a1": [{"text", "label"}]}
        self.fake_review(hide=[{"text": "ABC 商事", "label": "社名"}, {"text": "卸値 120 円", "label": "卸値"},
                               {"text": "ABC 商事", "label": "社名"}])
        self.prepared()
        self.assertEqual(self.report()["shots"]["s1.jpg"]["hidden"], ["社名", "卸値"])
        self.assertIn("ABC 商事".encode("utf-8"), image_bytes(self.w.page_data()["images"]["s1.jpg"]))  # ぼかす JS には渡る
        self.assertNotIn("ABC 商事", json.dumps(self.report(), ensure_ascii=False))
        self.w.decide()
        self.send()
        body = self.inbox.body()
        self.assertNotIn("ABC 商事", json.dumps(body["report"], ensure_ascii=False))
        self.assertEqual(body["report"]["shots"]["s1.jpg"]["hidden"], ["社名", "卸値"])

    def test_v1_strings_still_work(self):
        self.fake_review(hide=["ABC 商事"])
        self.prepared()
        self.assertEqual(self.report()["shots"]["s1.jpg"]["hidden"], [agentlog.HIDE_LABEL])


class TimeLimit(Base):
    def setUp(self):
        super().setUp()
        self.saved = (agentlog.AI_BUDGET, agentlog.REVIEW_CHARS, agentlog.REVIEW_MIN_CHARS)
        for i in range(6):
            L = Lines(base=time.time() - 6000 + i * 600).user("仕事 %d " % i + "あ" * 400)
            for k in range(10):
                L.assistant("返事").user("直し %d " % k + "い" * 200)
            self.w.write(L.assistant())

    def tearDown(self):
        agentlog.AI_BUDGET, agentlog.REVIEW_CHARS, agentlog.REVIEW_MIN_CHARS = self.saved
        super().tearDown()

    def test_the_summary_is_squeezed_when_time_is_short(self):
        agentlog.REVIEW_CHARS, agentlog.REVIEW_MIN_CHARS = 12000, 2000
        self.prepared()
        full = len(self.review_text())
        agentlog.AI_BUDGET = 120.0          # レビュー役に残る時間が REVIEW_FULL_TIME の半分ほど
        self.prepared()
        short = len(self.review_text())
        self.assertLess(short, full)
        self.assertLessEqual(short, 12000 * 120 / agentlog.REVIEW_FULL_TIME + 600)
        self.assertGreaterEqual(self.w.last()["review_chars"], 2000)

    def test_no_time_left_means_no_review(self):
        agentlog.AI_BUDGET = 30.0
        res = self.prepared()
        self.assertEqual(self.calls("review"), [])
        self.assertIn("アドバイスを作れなかった", res["say"])

    def test_the_whole_prepare_fits_in_the_bash_timeout(self):
        self.assertLessEqual(agentlog.PREPARE_BUDGET + 30, 600)
        self.assertLessEqual(agentlog.AI_BUDGET, 480)


if __name__ == "__main__":
    unittest.main()
