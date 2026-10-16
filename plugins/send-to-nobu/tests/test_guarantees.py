# -*- coding: utf-8 -*-
"""0.8 の保証。すべて一時の設定ディレクトリと合成の会話ログ、偽の受け口・claude・Chrome・sips。

  1. 外した会話はレポートに入らない
  2. 送るものはページで見えるものと同じ
  3. 会話の本文は送らない
  4. 秘密は伏せる
  5. 引用は原文にある文字だけ
  6. キットが取れなくても写しで動く
  7. 送信用の会話でしか動かない
  8. 送らない・スクショを外すが反映される
  9. 費用の計算
"""

import datetime
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (CODE, SEND_CMD, SEND_CMD_NS, Base, Lines, World, agentlog, image_bytes,  # noqa: E402
                     usage)

KEY = "sk-ant-api03-" + "Q1w2E3r4T5y6U7i8O9p0" * 4


class ExcludedConversationsStayOut(Base):
    """1. 外す判定で外した会話は、レビュー役にも、ページにも、送るレポートにも入らない（件数だけ）。"""

    def test_an_excluded_conversation_is_nowhere(self):
        a, _ = self.conv("請求書の集計をして", base=time.time() - 3000)
        b = Lines(base=time.time() - 2000).user("部長の愚痴を聞いてほしい 夜勤トークン-B").assistant("つらいですね")
        b.artifact(self.w.html("guchi.html", "<p>愚痴メモ</p>")).assistant()
        self.w.write(b)
        c, _ = self.conv("議事録のテンプレート", base=time.time() - 1000)
        self.fake_judge(flag={"愚痴": "愚痴"})
        res = self.prepared()
        self.assertEqual((res["count"], res["removed"]), (2, 1))
        self.assertNotIn("夜勤トークン-B", self.review_text())
        self.assertNotIn("guchi.html", self.review_text())
        page = json.dumps(self.w.page_data(), ensure_ascii=False)
        self.assertNotIn("夜勤トークン-B", page)
        self.assertNotIn("愚痴", page)
        self.assertEqual([c["id"] for c in self.report()["convs"]], ["c1", "c2"])     # 残った会話だけに c1… を振る
        self.assertEqual(self.report()["numbers"]["removed"], 1)
        self.assertEqual(self.report()["shots"], {})                                     # 外した会話は撮らない
        self.w.decide()
        self.assertEqual(self.send()[0], 0)
        body = self.inbox.body()
        self.assertEqual(body["removed_count"], 1)
        self.assertNotIn("夜勤トークン-B", json.dumps(body, ensure_ascii=False))

    def test_unjudged_conversations_stay_for_the_person_to_decide(self):
        self.conv("請求書の集計", base=time.time() - 2000)
        self.conv("愚痴を聞いて", base=time.time() - 1000)
        self.fake_judge(mode="error")
        res = self.prepared()
        self.assertEqual((res["count"], res["removed"]), (2, 0))
        self.assertIn("2 本は私的かどうかを確かめられなかった", res["say"])
        self.assertEqual(self.w.last()["judge_failed"], 2)


class SentIsWhatThePageShows(Base):
    """2. 送るレポートは、ページに入れたレポートと 1 対 1 で同じ（ページだけの印 round と、画像の置き場所だけが違う）。"""

    def test_the_report_and_images_match_the_page(self):
        p = self.w.html("lp.html", "<h1>LP</h1><p>取引先 ABC 商事</p>")
        L = Lines(base=time.time() - 2000).user("LP を作って").assistant().artifact(p).assistant("公開した")
        self.w.write(L)
        self.conv("議事録をまとめて", base=time.time() - 1000)
        self.fake_review(hide=["ABC 商事"])
        self.prepared()
        page = self.w.page_data()
        self.w.decide()
        self.assertEqual(self.send()[0], 0)
        body = self.inbox.body()
        images = page.pop("images")
        self.assertTrue(page.pop("round").startswith("r-"))
        self.assertEqual(body["report"], page)
        self.assertEqual({im["name"]: image_bytes("x," + im["data_base64"]) for im in body["images"]},
                         {k: image_bytes(v) for k, v in images.items()})
        self.assertEqual(body["kit_version"], "v3")
        self.assertEqual(body["plugin_version"], agentlog.plugin_version())
        self.assertEqual(body["range"], page["range"])
        self.assertEqual(page["mode"], "person")
        self.assertEqual(sorted(page), sorted(["v", "mode", "kit_version", "who", "range", "day_label", "numbers",
                                               "advices", "praises", "convs", "shots"]))


class NoConversationBody(Base):
    """3. 会話そのもの（本人の発言・AI の返事・ツールの結果・アーティファクトの本文・会話 ID・場所）は送らない。
    本人の原文は、レビュー役が引いて照合を通った引用（80 字まで）だけ。"""

    def test_only_the_report_leaves(self):
        p = self.w.html("doc.html", "<p>アーティファクト本文トークン</p>")
        L = Lines(base=time.time() - 2000, cwd="/Users/alice/秘密の案件フォルダ")
        L.user("最初の指示").assistant("AI返事トークン").tool("Bash", {"command": "cat a.csv"}, result="ツール結果トークン")
        L.user("二つ目の本人発言トークン").assistant().artifact(p).assistant()
        L.meta("custom-title", customTitle="会話の題トークン")
        self.w.write(L)
        self.prepared()
        self.assertIn("二つ目の本人発言トークン", self.review_text())    # レビュー役は読む（この Mac の中）
        self.w.decide()
        self.send()
        text = json.dumps({k: v for k, v in self.inbox.body().items() if k != "images"}, ensure_ascii=False)
        for word in ("AI返事トークン", "ツール結果トークン", "二つ目の本人発言トークン", "アーティファクト本文トークン",
                     "会話の題トークン", "秘密の案件フォルダ", L.sid, self.w.current, "doc.html"):
            self.assertNotIn(word, text)
        self.assertIn("最初の指示", text)     # 偽のレビュー役が引いた引用（原文と照合済み）だけ


class SecretsAreMasked(Base):
    """4. キー類は、レビュー役・外す判定に渡す前にも、レポートに入れる前にも伏せる。"""

    def test_keys_never_reach_the_ai_or_the_report(self):
        p = self.w.html("keys.html", "<p>設定 %s</p>" % KEY)
        L = Lines().user("キーは %s で設定して" % KEY).assistant("了解 %s" % KEY)
        L.tool("Bash", {"command": "env"}, result="TOKEN=%s" % KEY).artifact(p).assistant()
        L.meta("custom-title", customTitle="鍵 %s の設定" % KEY)
        self.w.write(L)
        self.fake_review(output={
            "advices": [{"type": "narabe", "conv": "c1", "say": "鍵は %s" % KEY, "key": KEY,
                         "quotes": ["キーは %s で設定して" % KEY], "count": "1", "n": 1, "items": [],
                         "next_label": "x", "yesterday": KEY, "next": "x", "prompt": "使う鍵 %s" % KEY}],
            "praises": [], "convs": [], "hide": {}})
        self.prepared()
        for call in self.calls():
            self.assertNotIn(KEY, call["stdin"])
        self.assertIn("[REDACTED:anthropic_key]", self.review_text())
        self.assertNotIn(KEY, self.w.page_html())
        adv = self.report()["advices"][0]
        self.assertEqual(adv["scene"]["quotes"], ["キーは [REDACTED:anthropic_key] で設定して"])
        self.assertEqual(adv["prompt"], "使う鍵 [REDACTED:anthropic_key]")
        self.assertEqual(adv["key"], "[REDACTED:anthropic_key]")
        self.assertTrue(self.report()["convs"][0]["title"].startswith("鍵 [REDACTED:anthropic_k"))
        self.w.decide()
        self.send()
        self.assertNotIn(KEY, json.dumps(self.inbox.body(), ensure_ascii=False))


class QuotesComeFromTheOriginal(Base):
    """5. 引用は、本人の原文にある文字だけ（80 字まで）。AI が作った文・AI の返事の文は捨てる。"""

    def test_only_original_words(self):
        long = "見出しを 12 個、" + "あ" * 120
        L = Lines().user("提案書の見出しを\n  直して。もう少し短く").assistant("AI が言った文").user(long).assistant()
        self.w.write(L)
        self.fake_review(output={
            "advices": [{"type": "narabe", "conv": "c1", "say": "並べる", "key": "", "count": "", "n": 2, "items": [],
                         "next_label": "", "yesterday": "", "next": "", "prompt": "",
                         "quotes": ["提案書の見出しを 直して", "「もう少し短く」", "言っていない文", "AI が言った文",
                                    "提案書の…もう少し短く", long, "あ", 42]}],
            "praises": [], "convs": [], "hide": {}})
        self.prepared()
        quotes = self.report()["advices"][0]["scene"]["quotes"]
        self.assertEqual(quotes, ["提案書の見出しを 直して", "もう少し短く", "提案書の", long[:79] + "…"])
        for q in quotes:
            self.assertLessEqual(len(q), agentlog.QUOTE_MAX)


class KitCopy(Base):
    """6. キットは毎回取りにいく（If-None-Match）。取れない朝は前回の写しで動く。写しも無ければ止まって案内する。"""

    def test_fresh_same_copy_and_none(self):
        self.conv("請求書の集計")
        self.prepared()
        self.assertEqual(self.inbox.kit_requests, [None])
        self.assertEqual(self.w.last()["kit_from"], "fresh")
        self.prepared()                                       # 同じ版 → 304 で写しを使う
        self.assertEqual(self.inbox.kit_requests[-1], '"v3"')
        self.assertEqual(self.w.last()["kit_from"], "same")
        self.inbox.kit_mode = "unavailable"                  # サーバーにキットが無い（503）
        self.prepared()
        self.assertEqual(self.w.last()["kit_from"], "copy")
        self.inbox.kit_mode = "broken"                       # 形の違うキットは使わない
        self.prepared()
        self.assertEqual((self.w.last()["kit_from"], self.report()["kit_version"]), ("copy", "v3"))
        os.unlink(os.path.join(self.w.data, "kit", "kit.json"))
        code, res = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("キット", res["say"])
        self.assertIsNone(self.w.pending())

    def test_a_new_version_replaces_the_copy(self):
        self.conv("請求書の集計")
        self.prepared()
        self.inbox.kit = dict(self.inbox.kit, version="v4")
        self.prepared()
        self.assertEqual((self.w.last()["kit_from"], self.report()["kit_version"]), ("fresh", "v4"))
        self.assertEqual(agentlog.load_kit_copy(self.w.data)["version"], "v4")

    def test_a_page_template_needs_exactly_one_slot(self):
        self.conv("請求書の集計")
        for page in ("<html>no slot</html>", "__REPORT__ __REPORT__"):
            kit = json.loads(json.dumps(self.inbox.kit))
            kit["files"]["page.html"] = page
            self.assertIsNone(agentlog.valid_kit(kit))


class SendConversationOnly(Base):
    """7. 作るのも送るのも、送信用の会話（/send-to-nobu で始めた会話か send-to-nobu のルーティン）の中だけ。
    送信用の会話・レポートを読んだ会話は、レポートに入れない。"""

    def test_prepare_is_refused_in_an_ordinary_conversation(self):
        L = Lines().user("作業する").assistant().user(SEND_CMD).user("スキル本文", isMeta=True)
        self.w.write(L, project=World.SEND_PROJECT)
        self.conv("ふつうの会話")
        code, res = self.prepare(session=L.sid)
        self.assertEqual(code, 1)
        self.assertIn("新しい会話", res["say"])
        self.assertEqual(self.inbox.kit_requests, [])      # キットも取りにいかない
        self.assertIsNone(self.w.pending())

    def test_the_routine_counts_and_other_routines_do_not(self):
        routine = ('<scheduled-task name="send-to-nobu" file="/Users/alice/.claude/scheduled-tasks/send-to-nobu/SKILL.md">\n'
                   "This is an automated run of a scheduled task.\n\nsend-to-nobu スキルで今日のレポートを作る\n</scheduled-task>")
        R = Lines(base=time.time() - 60).user(routine).assistant("作る")
        self.w.write(R, project=World.SEND_PROJECT)
        other = '<scheduled-task name="daily-check" file="/x/SKILL.md">\nsend-to-nobu も作って\n</scheduled-task>'
        O = Lines(base=time.time() - 60).user(other).assistant()
        self.w.write(O, project=World.SEND_PROJECT)
        self.conv("ふつうの会話")
        self.assertEqual(self.prepare(session=O.sid)[0], 1)
        res = self.prepared(session=R.sid)
        self.assertEqual(res["count"], 2)       # ふつうの会話と、daily-check の会話（人の指示で始まる会話として）
        self.assertNotIn("send-to-nobu スキルで", self.review_text())

    def test_send_conversations_and_report_readers_are_not_collected(self):
        for cmd in (SEND_CMD, SEND_CMD_NS):
            self.w.write(Lines().user(cmd).user("スキル本文", isMeta=True).assistant().user("ついでに質問").assistant())
        self.conv("ふつうの会話")
        self.assertEqual(self.prepared()["count"], 1)
        for leaked in (json.dumps(self.w.pending(), ensure_ascii=False), self.w.page_html()):
            peek = Lines().user("調べて").assistant()
            peek.tool("Read", {"file_path": "x"}, result=leaked[-3000:])
            self.w.write(peek)
        sub, path = self.conv("サブエージェントに調べさせた")
        self.w.subagent(path, "agent-x.jsonl", Lines().user("読んで").tool("Read", {"file_path": "p"},
                                                                           result=self.w.page_html()[-500:]))
        self.w.next_day()
        self.assertEqual(self.prepared()["count"], 1)

    def test_send_from_another_conversation_is_refused(self):
        self.conv("ふつうの会話")
        self.prepared()
        self.w.decide()
        other = Lines().user(SEND_CMD).user("スキル本文", isMeta=True)
        self.w.write(other, project=World.SEND_PROJECT)
        code, res = self.send(session=other.sid)
        self.assertEqual(code, 1)
        self.assertIn("見つからない", res["say"])
        self.assertEqual(self.inbox.reports, [])


class DecisionIsApplied(Base):
    """8. ページで「送らない」にした会話（とそのアドバイス・よかった・スクショ）と、外したスクショは送らない。
    答えが無い・前の回・レポートに無いものを指す答えでは、何も送らない。"""

    def setUp(self):
        super().setUp()
        for i, name in enumerate(("a.html", "b.html", "c.html")):
            p = self.w.html(name, "<p>ページ %s</p>" % name)
            L = Lines(base=time.time() - 3000 + i * 1000).user("指示 %d" % i).assistant().artifact(p).assistant()
            self.w.write(L)
        self.prepared()
        self.assertEqual(sorted(self.report()["shots"]), ["s1.jpg", "s2.jpg", "s3.jpg"])

    def assertNothingSent(self, code, res):
        self.assertNotEqual(code, 0, res)
        self.assertEqual(self.inbox.reports, [])
        self.assertIsNotNone(self.w.pending())      # 控えは残す（押し直して送れる）
        self.assertIsNone((self.w.state() or {}).get("sent_until"))

    def test_excluded_conversations_and_hidden_shots(self):
        self.w.decide(exclude=["c1"], hide_shots=["s3.jpg"])
        code, res = self.send()
        self.assertEqual(code, 0, res)
        body = self.inbox.body()
        rep = body["report"]
        self.assertEqual([c["id"] for c in rep["convs"]], ["c2", "c3"])
        self.assertEqual(sorted(rep["shots"]), ["s2.jpg"])
        self.assertEqual([im["name"] for im in body["images"]], ["s2.jpg"])
        c3 = rep["convs"][1]
        self.assertEqual((c3["shots"], "img" in c3["thumb"]), ([], False))
        self.assertEqual(rep["advices"], [])              # 偽のレビュー役はアドバイスを c1 に付けた
        self.assertEqual([p["conv"] for p in rep["praises"]], ["c3"])
        self.assertEqual((rep["numbers"]["convs"], rep["numbers"]["removed"], body["removed_count"]), (2, 1, 1))
        self.assertEqual(res["say"], "今朝のレポートを送った（会話 2 本・スクショ 1 枚・送らなかった会話 1 本）")

    def test_without_a_decision(self):
        code, res = self.send()
        self.assertNothingSent(code, res)
        self.assertIn("まだ押されていない", res["say"])
        self.assertIn("--answer-dir", res["next"])

    def test_a_decision_from_an_earlier_page(self):
        self.w.decide(round="r-20261015-0700-abcdef")
        code, res = self.send()
        self.assertNothingSent(code, res)
        self.assertIn("前のページ", res["say"])

    def test_answers_that_point_outside_the_report(self):
        rnd = self.w.pending()["round"]
        for raw in ({"round": rnd, "exclude": ["c9"]}, {"round": rnd, "exclude": [1]},
                    {"round": rnd, "exclude": [], "hide_shots": ["s9.jpg"]}, {"round": rnd, "exclude": "c1"},
                    {"round": rnd}, ["c1"]):
            self.w.decide(raw=raw)
            code, res = self.send()
            self.assertNothingSent(code, res)

    def test_a_decision_is_used_once_and_the_range_moves_on(self):
        rng = self.w.pending()["range"]
        self.w.decide()
        self.assertEqual(self.send()[0], 0)
        self.assertFalse(os.path.exists(agentlog.decision_path(self.w.answers)))
        self.assertEqual(self.w.state()["sent_until"], rng[1])
        code, res = self.send()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.inbox.reports), 1)
        # 次のレポートは、前回送ったところから
        self.w.next_day()
        code, res = self.prepare()
        self.assertEqual(res["count"], 0)

    def test_the_ai_cannot_pass_the_decision(self):
        self.w.decide()
        code, res = self.w.run("send", "--code", CODE, "--api-base", self.inbox.base, "--exclude", "c1")
        self.assertEqual(code, 2)
        self.assertIn("コマンドの形が違う", res["next"])
        self.assertEqual(self.inbox.reports, [])


class CostIsCounted(Base):
    """9. 費用 = message.usage × キットの単価。同じ応答の行は 1 回。サブエージェントも数える。今月の累計・1 日平均。"""

    def test_the_numbers(self):
        now = time.time()
        L = Lines(base=now - 3000)
        L.user("集計して")
        # 1 つの応答が 2 行（考え中と本文）に分かれて書かれる。usage は同じ応答 id で 1 回だけ数える
        L.ai([{"type": "thinking", "thinking": "x", "signature": "s"}], mid="msg_A", use=usage(i=10, o=5))
        L.ai([{"type": "text", "text": "した"}], mid="msg_A", use=usage(i=1000, o=2000, cr=10000, c5=4000, c1=1000))
        L.user("もう 1 つ").assistant("した", model="claude-sonnet-5-5", use=usage(i=2000, o=1000))
        L.assistant("知らないモデル", model="claude-mystery-9", use=usage(i=10 ** 6, o=10 ** 6))
        path = self.w.write(L)
        self.w.subagent(path, "agent-a.jsonl", Lines(base=now - 2500).user("サブ").assistant(
            "サブ", use=usage(o=4000)).encode())
        opus = (1000 * 5 + 2000 * 25 + 10000 * 0.5 + 4000 * 6.25 + 1000 * 10) / 1e6 + 4000 * 25 / 1e6
        sonnet = (2000 * 3 + 1000 * 15) / 1e6
        early = 0.0
        start = agentlog.month_start(now)
        if now - start > 3 * 86400:      # 今月の、範囲より前の会話（今月の累計だけに入る）
            E = Lines(base=start + 3600).user("月初の作業").assistant(use=usage(o=100000))
            self.w.write(E, mtime=start + 7200)
            early = 100000 * 25 / 1e6
        self.prepared(now=now)
        n = self.report()["numbers"]
        days = (datetime.date.fromtimestamp(now) - datetime.date.fromtimestamp(start)).days + 1
        self.assertAlmostEqual(n["cost_usd"], round(opus + sonnet, 2), places=6)
        self.assertAlmostEqual(n["cost_month_usd"], round(opus + sonnet + early, 2), places=6)
        self.assertAlmostEqual(n["cost_avg_usd"], round((opus + sonnet + early) / days, 2), places=6)
        self.assertEqual((n["model"], n["convs"]), ("Opus 5.5", 1))
        self.assertEqual(self.report()["convs"][0]["model"], "Opus 5.5")
        self.assertEqual(self.w.last()["unpriced_models"], ["claude-mystery-9"])

    def test_cost_helpers(self):
        # キットの prices.json の引き方: 一番長い前方一致 → fallback。zero は 0。inference_geo の倍率
        table = agentlog.price_table({
            "unit": "usd_per_mtok", "zero": ["<synthetic>"], "multipliers": {"inference_geo_us": 1.1},
            "models": {"claude-opus-5": {"label": "Opus 5", "input": 4}, "claude-opus-5-5": {"label": "Opus 5.5", "input": 5},
                       "claude-haiku-4-5": {"input": 1}},
            "fallback": {"label": "不明", "input": 2}})
        M = 10 ** 6
        self.assertEqual(agentlog.usage_cost("claude-opus-5-5[1m]", {"input_tokens": M}, table), (5.0, True))
        self.assertEqual(agentlog.usage_cost("claude-opus-5-20260101", {"input_tokens": M}, table), (4.0, True))
        self.assertEqual(agentlog.usage_cost("claude-haiku-4-5-20251001", {"input_tokens": M}, table), (1.0, True))
        self.assertEqual(agentlog.usage_cost("claude-x", {"input_tokens": M}, table), (2.0, False))
        self.assertEqual(agentlog.usage_cost("<synthetic>", {"input_tokens": M}, table), (0.0, True))
        cost, _ = agentlog.usage_cost("claude-opus-5-5", {"input_tokens": M, "inference_geo": "us"}, table)
        self.assertAlmostEqual(cost, 5.5)
        self.assertAlmostEqual(agentlog.usage_cost("claude-opus-5-5", {"cache_creation_input_tokens": M},
                                                   {"claude-opus-5-5": {"cache_write_5m": 6.25}})[0], 6.25)
        self.assertEqual(agentlog.usage_cost("claude-x", {"input_tokens": 1}, {"claude-y": {"input": 1}}), (None, False))
        self.assertEqual(agentlog.model_label("claude-opus-5-5-20261001", table), "Opus 5.5")
        self.assertEqual(agentlog.model_label("claude-new-9", table), "New 9")     # fallback の「不明」は名札にしない
        self.assertEqual(agentlog.model_label("claude-haiku-4-5-20251001", {}), "Haiku 4.5")
        self.assertEqual(agentlog.model_label("claude-opus-5-5", {}), "Opus 5.5")
        self.assertEqual(agentlog.model_label("claude-fable-5", {}), "Fable 5")


if __name__ == "__main__":
    unittest.main()
