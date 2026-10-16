# -*- coding: utf-8 -*-
"""会話ログの読み方（実データ由来の癖）・範囲・朝の知らせ・ページ・送り方。すべて一時の設定ディレクトリと合成の会話ログ。"""

import datetime
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CODE, CWD, Base, Lines, REPORT_RE, World, agentlog, iso  # noqa: E402

NASTY = "</script><script>alert(1)</script> <!-- &      </SCRIPT >"


def builtin(name):
    """組み込みコマンドの行（<command-name> が先頭）。"""
    return "<command-name>/%s</command-name>\n<command-message>%s</command-message>\n<command-args></command-args>" % (
        name, name)


class Reading(Base):
    def titles(self):
        return re.findall(r"^=== 会話 c\d+ .*「(.*)」$", self.review_text(), re.M)

    def test_what_counts_as_a_human_instruction(self):
        # 人の指示でないものだけの会話は集めない
        L = Lines().user(builtin("model")).user("<local-command-stdout>Set model</local-command-stdout>")
        L.user("ピアから", origin={"kind": "peer"}).user("<task-notification>x</task-notification>")
        L.user("メタ", isMeta=True).user("サイドチェーン", isSidechain=True).user("[Request interrupted by user]")
        L.user("<system-reminder>注意</system-reminder>").tool("Bash", {"command": "ls"})
        L.user([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}])
        L.assistant()
        self.w.write(L)
        self.assertEqual(self.prepared()["count"], 0)
        # 組み込みコマンドのあとの指示・スキル呼び出し・! コマンド・作業中に打った文は人の指示
        for first in ("<system-reminder>注意</system-reminder>\n本当の指示",
                      "<command-message>deck</command-message>\n<command-name>/deck</command-name>\n"
                      "<command-args>来週の資料</command-args>", "<bash-input>ls -la</bash-input>"):
            self.w.write(Lines().user(builtin("context")).assistant("内訳").user(first).assistant())
        Q = Lines().meta("attachment", attachment={"type": "queued_command", "commandMode": "prompt",
                                                   "prompt": "作業中に打った文"}, isSidechain=False,
                         timestamp=iso(time.time() - 100))
        self.w.write(Q.assistant())
        self.w.next_day()
        self.prepared()
        self.assertEqual(sorted(self.titles()), sorted(["本当の指示", "/deck 来週の資料", "!ls -la", "作業中に打った文"]))

    def test_title_prefers_last_custom_then_last_ai_title(self):
        a = Lines().user("最初の指示").assistant().meta("ai-title", aiTitle="AI の題").meta("custom-title", customTitle="自分の題")
        a.meta("ai-title", aiTitle="あとの AI の題")
        b = Lines().user("x").assistant().meta("ai-title", aiTitle="一つ目").meta("ai-title", aiTitle="最後の AI の題")
        for L in (a, b):
            self.w.write(L)
        self.prepared()
        self.assertEqual(sorted(self.titles()), sorted(["自分の題", "最後の AI の題"]))

    def test_the_first_report_covers_24_hours_and_then_from_the_last_send(self):
        now = time.time()
        old, _ = self.conv("2 日前の会話", base=now - 2 * 86400)
        self.w.write(Lines(sid=old.sid, base=now - 2 * 86400).user("2 日前の会話").assistant()
                     .user("今日の続き", at=now - 600).assistant(at=now - 590))
        recent, _ = self.conv("きのうの会話", base=now - 20 * 3600)
        self.prepared(now=now)
        self.assertEqual(sorted(self.titles()), sorted(["2 日前の会話", "きのうの会話"]))
        self.assertNotIn("[本人] 2 日前の会話", self.review_text())     # 範囲より前の発言は入れない
        self.assertIn("[本人] 今日の続き", self.review_text())
        rng = self.report()["range"]
        self.assertAlmostEqual(datetime.datetime.fromisoformat(rng["from"]).timestamp(), now - 86400, delta=1)
        self.w.decide()
        self.send(now=now)
        self.assertEqual(self.w.state()["sent_until"], now)
        # 翌朝: 前回送ったところから
        later = now + 3600
        L = Lines(base=now + 60).user("送ったあとの会話").assistant()
        self.w.write(L)
        self.w.next_day()
        self.prepared(now=later)
        self.assertEqual(self.titles(), ["送ったあとの会話"])

    def test_the_old_per_conversation_state_is_dropped(self):
        self.conv("会話")
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "state.json"), "w") as f:
            json.dump({"v": 1, "baseline": time.time() - 7 * 86400,
                       "sessions": {"x": {"d": "excluded", "offset": 1}}}, f)
        os.makedirs(os.path.join(self.w.data, "review", "1-abcdefgh"))
        self.assertEqual(self.prepared()["count"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.w.data, "review")))
        self.w.decide()
        self.send()
        self.assertEqual(sorted(self.w.state()), ["last_report", "sent_day", "sent_until", "v"])

    def test_a_broken_state_stops(self):
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "state.json"), "w") as f:
            f.write("{broken")
        code, res = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("state.json", res["say"])

    def test_automatic_runs_and_the_current_conversation_are_not_collected(self):
        self.w.write(Lines(entrypoint="sdk-cli").user("自動実行").assistant())
        mixed = Lines(entrypoint="sdk-cli").user("自動で始めた").assistant()
        mixed.entrypoint = "cli"
        self.w.write(mixed.user("人が続けた").assistant())
        self.w.append(os.path.join(self.w.projects, World.SEND_PROJECT, self.w.current + ".jsonl"),
                      Lines(sid=self.w.current).user("いまの会話で話す").assistant())
        self.prepared()
        self.assertEqual(self.titles(), ["自動で始めた"])

    def test_invalid_utf8_and_a_half_written_last_line(self):
        L = Lines().user("前の指示").assistant()
        L.raw(b'{"isSidechain":false,"type":"user","message":{"role":"user","content":"\xe3\x81 \xff \xe5\xa3\x8a"},'
              b'"uuid":"u-bad","timestamp":"' + iso(time.time() - 60).encode() + b'","cwd":"' + CWD.encode() + b'"}')
        L.raw(b"{not json at all")
        path = self.w.write(L)
        self.w.append(path, '{"type":"user","message":{"content":"書きかけ'.encode("utf-8"))
        self.prepared()
        self.assertNotIn("書きかけ", self.review_text())
        self.assertIn("�", self.review_text())

    def test_nothing_to_report(self):
        code, res = self.prepare()
        self.assertEqual((code, res["count"], res["next"]), (0, 0, agentlog.END))
        self.assertIsNone(self.w.pending())


class Nudge(Base):
    def nudge(self, now, session=None):
        return self.w.run("nudge", now=now, session=session)[1].get("systemMessage")

    def test_once_a_day_with_6am_as_the_boundary(self):
        self.conv("会話")
        day = datetime.datetime.now().replace(hour=7, minute=0, second=0, microsecond=0).timestamp()
        self.assertIn("今朝のレポートがまだ", self.nudge(day))
        self.assertIsNone(self.nudge(day + 3600))
        self.assertIsNone(self.nudge(day + 22 * 3600))           # 翌朝 5 時はまだ同じ日

    def test_quiet_after_sending_and_when_nothing_happened(self):
        self.assertIsNone(self.nudge(time.time()))
        self.assertFalse(os.path.exists(os.path.join(self.w.data, "nudged.json")))
        self.w.write(Lines(entrypoint="sdk-cli").user("自動").assistant())
        self.assertIsNone(self.nudge(time.time()))
        self.conv("会話")
        self.prepared()
        self.w.decide()
        self.send()
        self.assertIsNone(self.nudge(time.time() + 60))         # 今朝はもう送った

    def test_quiet_inside_the_check(self):
        self.conv("会話")
        os.environ[agentlog.JUDGE_ENV] = "1"
        try:
            self.assertEqual(self.w.run("nudge"), (0, {}))
        finally:
            os.environ.pop(agentlog.JUDGE_ENV)

    def test_never_gets_in_the_way(self):
        for argv in (["nudge"], ["nudge", "--data-dir", "/etc"], ["nudge", "--bogus"]):
            out = io.StringIO()
            self.assertEqual(agentlog.main(argv, stdout=out), 0)
            self.assertEqual(out.getvalue(), "")
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "state.json"), "w") as f:
            f.write("{broken")
        self.conv("会話")
        self.assertIsNone(self.nudge(time.time()))


class Page(Base):
    def test_the_page_and_its_marker(self):
        L = Lines().user("x").assistant().meta("custom-title", customTitle=NASTY)
        self.w.write(L)
        self.fake_review(output={"advices": [], "praises": [], "convs": [], "hide": {}})
        res = self.prepared()
        self.assertEqual(res["file"], os.path.join(self.w.data, "page", os.path.basename(res["file"])))
        self.assertRegex(os.path.basename(res["file"]), r"^send-to-nobu-\d{8}-\d{4}\.html$")
        self.assertRegex(res["round"], r"^r-\d{8}-\d{4}-[0-9a-f]{6}$")
        self.assertEqual(res[agentlog.LIST_MARKER], 1)
        self.assertIn("--answer-dir", res["next"])
        self.assertNotIn(self.w.data, res["next"])
        html = self.w.page_html()
        raw = REPORT_RE.search(html).group(1)
        for ch in ("<", ">", "&", " ", " "):
            self.assertNotIn(ch, raw)
        data = json.loads(raw)
        self.assertEqual(data["convs"][0]["title"], agentlog.squash(NASTY, 24))
        self.assertEqual((data["round"], data["who"], data["mode"]), (res["round"], {"label": "alice"}, "person"))
        self.assertEqual(html.count("__REPORT__"), 0)
        self.assertTrue(html.rstrip().endswith("<!-- %s -->" % agentlog.LIST_MARKER))   # 目印はデータの外に

    def test_the_json_escapes_what_html_and_js_would_read(self):
        value = {"t": "a b </script>&<!--", "q": "\ud800"}
        text = agentlog.page_json(value)
        self.assertIsNone(re.search("[<>&  ]", text))
        self.assertEqual(json.loads(text), value)
        text.encode("utf-8")

    def test_only_the_latest_page_is_kept_and_old_answers_go(self):
        self.conv("会話")
        self.prepared()
        d = os.path.join(self.w.data, "page")
        old = os.path.join(d, "send-to-nobu-20260101-0700.html")
        with open(old, "w") as f:
            f.write("old")
        os.makedirs(os.path.dirname(agentlog.decision_path(d)), exist_ok=True)
        for path in (agentlog.decision_path(d), agentlog.result_path(d)):
            with open(path, "w") as f:
                f.write("{}")
        self.prepared()
        self.assertEqual(len([n for n in os.listdir(d) if n.endswith(".html")]), 1)
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(agentlog.decision_path(d)))

    def test_send_writes_the_result_for_the_page(self):
        self.conv("一つ目", base=time.time() - 2000)
        self.conv("二つ目", base=time.time() - 1000)
        rnd = self.prepared()["round"]
        self.w.decide(exclude=["c2"])
        code, res = self.send()
        self.assertEqual(code, 0, res)
        # ページの契約: send/result = {round, at, report_id, sent, excluded, hidden_shots, image_count}
        self.assertEqual(sorted(res["result"]), sorted(["round", "at", "report_id", "sent", "excluded", "hidden_shots",
                                                        "image_count"]))
        self.assertEqual({k: res["result"][k] for k in ("round", "report_id", "sent", "excluded", "hidden_shots",
                                                        "image_count")},
                         {"round": rnd, "report_id": "20261016T073000Z-1a2b3c4d", "sent": ["c1"], "excluded": ["c2"],
                          "hidden_shots": [], "image_count": 0})
        self.assertEqual(agentlog.read_json(res["result_file"], None), res["result"])
        self.assertEqual(res["next"], agentlog.NEXT_RESULT)
        self.assertIsNone(self.w.pending())


class Sending(Base):
    def setUp(self):
        super().setUp()
        self.conv("調べて")
        self.prepared()
        self.w.decide()

    def test_a_lost_reply_counts_as_done_on_retry(self):
        self.inbox.lose_first = True     # 受領票は置けたが 502。リトライで 409 already_finished → 成功
        code, res = self.send()
        self.assertEqual(code, 0, res)
        self.assertIsNotNone(self.w.state()["sent_until"])

    def test_a_used_or_expired_code_asks_for_a_new_one(self):
        code, res = self.w.run("send", "--code", "x" * 26, "--api-base", self.inbox.base)
        self.assertEqual((code, res), (4, {"next": agentlog.NEXT_NEW_CODE}))
        self.assertIsNone(self.w.state())
        self.inbox.finished = True       # 前に使った券（もうレポートを送った）
        code, res = self.send()
        self.assertEqual((code, res), (4, {"next": agentlog.NEXT_NEW_CODE}))
        self.inbox.finished = False
        self.assertEqual(self.send()[0], 0)     # 新しい券で。答えはもう一度読む（押し直させない）

    def test_only_our_server(self):
        code, res = self.w.run("send", "--code", CODE, "--api-base", "https://example.com")
        self.assertEqual((code, res["next"]), (2, agentlog.NEXT_CODE))
        code, res = self.w.run("send", "--api-base", self.inbox.base)
        self.assertEqual(code, 2)
        self.assertEqual(self.inbox.reports, [])

    def test_data_dir_must_be_the_plugins_own(self):
        for bad in ("", os.path.join(self.w.tmp, "elsewhere", "send-to-nobu-x"),
                    os.path.join(self.w.tmp, "plugins", "data", "other-plugin")):
            out = io.StringIO()
            self.assertEqual(agentlog.main(["prepare", "--data-dir", bad], stdout=out), 1)
            self.assertIn("データの置き場所", json.loads(out.getvalue())["say"])

    def test_the_real_entrypoint_uses_the_env(self):
        # 子プロセスは本物の送り先だけを許すので、env の読み方だけを見る（偽の受け口には行かない）
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.w.tmp, CLAUDE_CODE_SESSION_ID=self.w.current,
                   CLAUDE_PLUGIN_DATA="/somewhere/else")
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "agentlog.py")
        argv = [sys.executable, script, "prepare", "--data-dir", self.w.data, "--code", CODE, "--api-base",
                self.inbox.base]
        r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, json.loads(r.stdout)["next"]), (2, agentlog.NEXT_CODE), r.stdout + r.stderr)
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=dict(env, CLAUDE_CODE_SESSION_ID=""))
        self.assertIn("いまの会話がわからない", json.loads(r.stdout)["say"])
        self.assertEqual(self.inbox.kit_requests, [None])     # setUp の 1 回だけ


if __name__ == "__main__":
    unittest.main()
