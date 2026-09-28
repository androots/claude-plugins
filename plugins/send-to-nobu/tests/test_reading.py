# -*- coding: utf-8 -*-
"""会話の読み方・一覧・nudge。すべて合成フィクスチャ。"""

import datetime
import io
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CWD, Lines, World, agentlog, iso  # noqa: E402

ANTHROPIC = "sk-ant-api03-" + "Q1w2E3r4T5y6U7i8O9p0" * 4
SEND_CMD = "<command-message>send-to-nobu</command-message>\n<command-name>/send-to-nobu</command-name>"
SEND_CMD_NS = ("<command-message>send-to-nobu:send-to-nobu</command-message>\n"
               "<command-name>/send-to-nobu:send-to-nobu</command-name>")


def builtin(name, out="ok"):
    """組み込みコマンド: <command-name> が先頭で、次の行が <local-command-stdout>。"""
    return ("<command-name>/%s</command-name>\n            <command-message>%s</command-message>\n"
            "            <command-args></command-args>" % (name, name),
            "<local-command-stdout>%s</local-command-stdout>" % out)


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def by_sid(self, res):
        return {it["session_id"]: it for it in res["items"]}

    def exclude_all_locally(self):
        """全部外して感想なし（サーバーに送らない）で判断を記録する。"""
        res = self.w.list()
        nums = ",".join(str(it["n"]) for it in res["items"]) or "none"
        code, out, err = self.w.run("send", "--exclude", nums, "--confirm-shared")
        self.assertEqual(code, 0, err)
        return res


class ListTest(Base):
    def test_normal(self):
        L = Lines()
        L.user("請求書の集計をしたい").assistant().tool_result().assistant()
        L.user("CSV を読み込んで").assistant()
        L.meta("ai-title", aiTitle="請求書の集計")
        L.user("月ごとに合計して").assistant()
        L.meta("custom-title", customTitle="請求 集計 9月")
        L.meta("ai-title", aiTitle="あとから来た AI タイトル")
        L.meta("last-prompt", lastPrompt="月ごとに合計して")
        path = self.w.write(L)
        res = self.w.list()
        self.assertEqual(res["count"], 1)
        self.assertTrue(res["first_run"])
        it = res["items"][0]
        self.assertEqual(it["n"], 1)
        self.assertEqual(it["session_id"], L.sid)
        self.assertEqual(it["title"], "請求 集計 9月")  # custom-title が ai-title に勝つ
        self.assertEqual(it["project"], "~/work/billing")
        self.assertEqual(it["prompts"], 3)
        self.assertEqual(it["preview"], ["請求書の集計をしたい", "CSV を読み込んで", "月ごとに合計して"])
        self.assertNotIn("shares_history_with", it)
        p = self.w.pending()
        self.assertEqual(p["session"], self.w.current)
        self.assertEqual(p["items"][0]["offset"], os.path.getsize(path))
        self.assertEqual(p["items"][0]["path"], path)

    def test_title_fallbacks(self):
        a = Lines().user("x").assistant().meta("ai-title", aiTitle="AI の題").meta("ai-title", aiTitle="最後の AI の題")
        b = Lines(base=time.time() - 1800).user("  とても長い最初の指示。" + "あ" * 60 + "\n二行目").assistant()
        self.w.write(a)
        self.w.write(b)
        items = self.by_sid(self.w.list())
        self.assertEqual(items[a.sid]["title"], "最後の AI の題")
        self.assertEqual(items[b.sid]["title"], ("とても長い最初の指示。" + "あ" * 60)[:40])

    def test_without_preview(self):
        self.w.write(Lines().user("指示").assistant())
        res = self.w.list(preview=False)
        self.assertNotIn("preview", res["items"][0])

    def test_compact(self):
        L = Lines().user("最初の指示").assistant()
        L.system("compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 1000})
        L.user("This session is being continued from a previous conversation.", isCompactSummary=True,
               isVisibleInTranscriptOnly=True)
        L.user("圧縮後の指示").assistant()
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(it["prompts"], 2)
        self.assertEqual(it["preview"], ["最初の指示", "圧縮後の指示"])

    def test_fork_shares_history_both_ways(self):
        a = Lines().user("元の会話").assistant().user("続き").assistant()
        b = Lines(base=time.time() - 1800).copy_from(a).user("分岐してから").assistant()
        c = Lines(base=time.time() - 900).user("無関係").assistant()
        self.w.write(a)
        self.w.write(b)
        self.w.write(c)
        items = self.by_sid(self.w.list())
        self.assertEqual(items[a.sid]["n"], 1)
        self.assertEqual(items[a.sid]["shares_history_with"], [items[b.sid]["n"]])
        self.assertEqual(items[b.sid]["shares_history_with"], [items[a.sid]["n"]])
        self.assertNotIn("shares_history_with", items[c.sid])

    def test_fork_after_compact_is_found_from_the_other_side(self):
        # 分岐した側の先頭が、元の会話の途中（圧縮後）の行のコピーでも見つける
        a = Lines()
        for i in range(250):
            a.user("指示 %d" % i).assistant()
        b = Lines(base=time.time() - 60)
        for row in a.rows[-20:]:
            b.rows.append(dict(row, sessionId=b.sid))
        b.user("続きから").assistant()
        self.w.write(a)
        self.w.write(b)
        items = self.by_sid(self.w.list())
        self.assertEqual(items[a.sid]["shares_history_with"], [items[b.sid]["n"]])
        self.assertEqual(items[b.sid]["shares_history_with"], [items[a.sid]["n"]])

    def test_time_reversal(self):
        now = time.time()
        L = Lines()
        L.user("一つ目", at=now - 100).assistant(at=now - 50)
        L.user("二つ目", at=now - 5000).assistant(at=now - 4000)
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(it["preview"], ["一つ目", "二つ目"])  # 並べ替えない
        self.assertEqual(self.w.pending()["items"][0]["last_activity"], agentlog.iso_utc(agentlog.parse_ts(iso(now - 50))))

    def test_meta_only(self):
        L = Lines().meta("ai-title", aiTitle="題だけ").meta("last-prompt", lastPrompt="x").meta("mode", mode="default")
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_send_command_first_is_hidden(self):
        for cmd in (SEND_CMD, SEND_CMD_NS):
            L = Lines().user(cmd).user("スキル本文", isMeta=True).assistant("一覧").user("なし").assistant()
            self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_send_command_later_is_listed_but_not_counted(self):
        L = Lines().user("作業する").assistant().user(SEND_CMD).user("スキル本文", isMeta=True).assistant()
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(it["prompts"], 1)
        self.assertEqual(it["preview"], ["作業する"])

    def test_builtin_first(self):
        cmd, out = builtin("model", "Set model to opus")
        L = Lines().user(cmd).user(out).user("本題の指示").assistant()
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(it["prompts"], 1)
        self.assertEqual(it["title"], "本題の指示")

    def test_only_builtins_is_hidden(self):
        L = Lines()
        for name in ("mcp", "login", "model"):
            cmd, out = builtin(name)
            L.user(cmd).user(out)
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_skill_invocation_counts(self):
        L = Lines().user("<command-message>deck</command-message>\n<command-name>/deck</command-name>\n"
                         "<command-args>来週の資料</command-args>")
        L.user("スキル本文", isMeta=True).assistant()
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(it["prompts"], 1)
        self.assertEqual(it["preview"], ["/deck 来週の資料"])

    def test_non_human_lines(self):
        L = Lines().user("最初の指示").assistant().tool_result()
        L.user("[Request interrupted by user]")
        L.user("<system-reminder>注意</system-reminder>")
        L.user("<system-reminder>注意</system-reminder>\n本当の指示")
        L.user("<task-notification><task-id>1</task-id></task-notification>")
        L.user("<bash-input>ls</bash-input>")
        L.user("<bash-stdout>a.txt</bash-stdout><bash-stderr></bash-stderr>")
        L.user("<local-command-stdout>x</local-command-stdout>")
        L.user("ピアから", origin={"kind": "peer", "from": "x", "body": "y"})
        L.user("<task-notification>終わった</task-notification>", origin={"kind": "task-notification"})
        L.user("人が打った", origin={"kind": "human"})
        L.user("メタ", isMeta=True)
        L.user("サイドチェーン", isSidechain=True)
        L.user_blocks([{"type": "image", "source": {"type": "base64", "data": "AAAA"}}])
        L.user_blocks([{"type": "text", "text": "ブロックの指示"}, {"type": "image", "source": {}}])
        L.assistant()
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(it["preview"], ["最初の指示", "本当の指示", "!ls", "人が打った", "ブロックの指示"])
        self.assertEqual(it["prompts"], 5)

    def test_peer_and_notifications_only_is_hidden(self):
        L = Lines().user("ピアから", origin={"kind": "peer"}).assistant()
        L.user("<task-notification>x</task-notification>", origin={"kind": "task-notification"}).assistant()
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_nested_subagents(self):
        L = Lines().user("調べて").assistant()
        path = self.w.write(L)
        sub = Lines().user("サブの指示").assistant()
        self.w.subagent(path, "agent-a1b2.jsonl", sub)
        self.w.subagent(path, "agent-a1b2.meta.json", b'{"agentType":"Explore"}')
        self.w.subagent(path, "workflows/wf_abc-123/agent-c3.jsonl", sub)
        self.w.subagent(path, "workflows/wf_abc-123/journal.jsonl", b'{"step":1}\n')
        self.w.subagent(path, "bad name!.jsonl", sub)                # 名前が契約外 → 送らない
        self.w.subagent(path, "notes.txt", b"x")                     # 拡張子が違う → 送らない
        self.w.subagent(path, "a/b/c/d/e/f/deep.jsonl", sub)         # 深さ 7 → 送らない
        tool_results = os.path.join(path[:-6], "tool-results")
        os.makedirs(tool_results)
        with open(os.path.join(tool_results, "t.json"), "w") as f:
            f.write("{}")
        rels = [r for r, _, _ in agentlog.subagent_files(path[:-6])]
        self.assertEqual(rels, ["agent-a1b2.jsonl", "agent-a1b2.meta.json",
                                "workflows/wf_abc-123/agent-c3.jsonl", "workflows/wf_abc-123/journal.jsonl"])
        it = self.w.list()["items"][0]
        self.assertEqual(it["subagent_files"], 4)
        self.assertEqual(it["prompts"], 1)  # サブエージェントの指示は数えない

    def test_invalid_utf8(self):
        L = Lines().user("前の指示").assistant()
        L.raw(b'{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":'
              b'"\xe3\x81 \xff \xe5\xa3\x8a\xe3\x82\x8c\xe3\x81\x9f"},"uuid":"u-bad","timestamp":"'
              + iso(time.time() - 60).encode() + b'","cwd":"' + CWD.encode() + b'"}')
        L.raw(b"{not json at all")
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(it["prompts"], 2)
        self.assertIn("�", it["preview"][1])

    def test_partial_last_line_is_not_read(self):
        L = Lines().user("指示").assistant()
        path = self.w.write(L)
        size = os.path.getsize(path)
        self.w.append(path, '{"type":"user","message":{"content":"書きかけ'.encode("utf-8"))
        self.w.list()
        self.assertEqual(self.w.pending()["items"][0]["offset"], size)

    def test_secrets_masked_in_title_and_preview(self):
        L = Lines().user("キーは %s で" % ANTHROPIC).assistant()
        self.w.write(L)
        out = json.dumps(self.w.list(), ensure_ascii=False)
        self.assertNotIn(ANTHROPIC, out)
        self.assertNotIn(ANTHROPIC, json.dumps(self.w.pending(), ensure_ascii=False))
        it = self.w.list()["items"][0]
        self.assertEqual(it["title"], "キーは [REDACTED:anthropic_key] で")

    def test_current_session_is_hidden(self):
        L = Lines(sid=self.w.current).user("いまの会話").assistant()
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_first_run_baseline(self):
        old = time.time() - 10 * 86400
        L = Lines(base=old).user("古い会話").assistant()
        path = self.w.write(L, mtime=old + 600)
        self.assertEqual(self.w.list()["count"], 0)
        # 後で触られたら出す
        self.w.append(path, Lines(sid=L.sid).user("また使った").assistant())
        it = self.w.list()["items"][0]
        self.assertEqual(it["prompts"], 2)

    def test_old_session_opened_and_closed_is_hidden(self):
        old = time.time() - 10 * 86400
        L = Lines(base=old).user("古い会話").assistant()
        path = self.w.write(L, mtime=old + 600)
        self.w.append(path, Lines(sid=L.sid).meta("last-prompt", lastPrompt="x").meta("mode", mode="default"))
        self.assertEqual(self.w.list()["count"], 0)

    def test_preview_is_limited(self):
        L = Lines()
        for i in range(40):
            L.user("指示 %02d " % i + "長" * 300).assistant()
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(len(it["preview"]), 15)
        self.assertEqual(it["preview_omitted"], 25)
        self.assertTrue(all(len(p) <= 160 for p in it["preview"]))
        self.assertTrue(it["preview"][0].startswith("指示 00"))
        self.assertTrue(it["preview"][-1].startswith("指示 39"))


class DecisionTest(Base):
    def test_closed_only_meta_growth_is_not_unsent(self):
        L = Lines().user("指示").assistant()
        path = self.w.write(L)
        self.exclude_all_locally()
        self.assertEqual(self.w.list()["count"], 0)
        # 閉じただけでメタ行が増えた
        self.w.append(path, Lines(sid=L.sid).meta("last-prompt", lastPrompt="指示").meta("ai-title", aiTitle="題"),
                      mtime=time.time() + 5)
        self.assertEqual(self.w.list()["count"], 0)
        # user 行が増えたら出す（前に外した印つき）
        self.w.append(path, Lines(sid=L.sid).user("続き").assistant())
        it = self.w.list()["items"][0]
        self.assertTrue(it["previously_excluded"])
        self.assertEqual(it["prompts"], 2)

    def test_excluded_copy_flag(self):
        a = Lines().user("外したい相談").assistant().user("続き").assistant()
        self.w.write(a)
        self.exclude_all_locally()
        b = Lines(base=time.time() - 60).copy_from(a).user("分岐して別の話").assistant()
        self.w.write(b)
        items = self.by_sid(self.w.list())
        self.assertEqual(list(items), [b.sid])
        self.assertTrue(items[b.sid]["contains_excluded_copy"])
        self.assertNotIn("previously_excluded", items[b.sid])

    def test_subagent_growth_after_decision(self):
        L = Lines().user("指示").assistant()
        path = self.w.write(L)
        self.w.subagent(path, "agent-a1.jsonl", Lines().user("サブ").assistant())
        self.exclude_all_locally()
        self.w.subagent(path, "agent-b2.jsonl", Lines().user("サブ 2").assistant())
        self.w.append(path, Lines(sid=L.sid).meta("last-prompt", lastPrompt="x"), mtime=time.time() + 5)
        it = self.w.list()["items"][0]
        self.assertEqual(it["subagent_files"], 2)

    def test_nothing_sent_records_exclusion(self):
        L = Lines().user("指示").assistant()
        self.w.write(L)
        self.w.list()
        code, out, err = self.w.run("send", "--exclude", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["excluded_count"], 1)
        self.assertEqual(self.w.state()["sessions"][L.sid]["d"], "excluded")
        self.assertIsNone(self.w.pending())


class NudgeTest(Base):
    def nudge(self, now):
        code, out, err = self.w.run("nudge", now=now)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        return json.loads(out) if out.strip() else None

    def local_ts(self, days, hour, minute=0):
        d = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return (d + datetime.timedelta(days=days, hours=hour, minutes=minute)).timestamp()

    def test_once_per_day_with_6am_boundary(self):
        self.w.write(Lines().user("指示").assistant())
        msg = self.nudge(self.local_ts(0, 10))
        self.assertEqual(list(msg), ["systemMessage"])
        self.assertIn("1 件", msg["systemMessage"])
        self.assertIn("/send-to-nobu", msg["systemMessage"])
        self.assertIsNone(self.nudge(self.local_ts(0, 23)))
        self.assertIsNone(self.nudge(self.local_ts(1, 5, 30)))   # 翌朝 6 時前は同じ日
        self.assertIsNotNone(self.nudge(self.local_ts(1, 6, 30)))

    def test_zero_prints_nothing_and_does_not_mark_the_day(self):
        self.assertIsNone(self.nudge(time.time()))
        self.assertNotIn("nudged_day", self.w.state())
        self.w.write(Lines().user("指示").assistant())
        self.assertIsNotNone(self.nudge(time.time()))

    def test_counts_match_list(self):
        old = time.time() - 10 * 86400
        self.w.write(Lines().user("ふつう").assistant())
        self.w.write(Lines().meta("ai-title", aiTitle="題だけ"))
        self.w.write(Lines().user(SEND_CMD).assistant())
        cmd, out = builtin("model")
        self.w.write(Lines().user(cmd).user(out).user("本題").assistant())
        self.w.write(Lines(base=old).user("古い").assistant(), mtime=old)
        p = self.w.write(Lines(base=old).user("古いのを開いただけ").assistant(), mtime=old)
        self.w.append(p, Lines().meta("mode", mode="default"))
        msg = self.nudge(time.time())
        self.assertIn("2 件", msg["systemMessage"])
        self.assertEqual(self.w.list()["count"], 2)

    def test_first_prompt_far_from_the_head_is_counted(self):
        # 組み込みコマンドのあと自動の作業が長く続き、人の指示が先頭の読み取り量より後ろにある
        saved = agentlog.NUDGE_HEAD_BYTES
        agentlog.NUDGE_HEAD_BYTES = 4000
        try:
            cmd, out = builtin("review")
            L = Lines().user(cmd).user(out).user("自動のプロンプト", isMeta=True)
            for i in range(30):
                L.assistant("作業 %d" % i).tool_result("x" * 300)
            L.user("本題").assistant()
            self.w.write(L)
            self.w.write(Lines().user("ピアだけ", origin={"kind": "peer"}).assistant())
            msg = self.nudge(time.time())
            self.assertIn("1 件", msg["systemMessage"])
            self.assertEqual(self.w.list()["count"], 1)
        finally:
            agentlog.NUDGE_HEAD_BYTES = saved

    def test_counts_grown_decided_sessions(self):
        L = Lines().user("指示").assistant()
        path = self.w.write(L)
        self.exclude_all_locally()
        self.assertIsNone(self.nudge(time.time()))
        self.w.append(path, Lines(sid=L.sid).meta("last-prompt", lastPrompt="x"), mtime=time.time() + 5)
        self.assertIsNone(self.nudge(time.time()))
        self.w.append(path, Lines(sid=L.sid).user("続き").assistant())
        self.assertIsNotNone(self.nudge(time.time()))

    def test_errors_are_swallowed(self):
        out = io.StringIO()
        code = agentlog.main(["nudge", "--data-dir", os.path.join(self.w.tmp, "other-plugin")], stdout=out)
        self.assertEqual((code, out.getvalue()), (0, ""))
        code = agentlog.main(["nudge", "--bogus"], stdout=out)
        self.assertEqual((code, out.getvalue()), (0, ""))
        blocker = os.path.join(self.w.tmp, "send-to-nobu-file")
        open(blocker, "w").close()
        code = agentlog.main(["nudge", "--data-dir", os.path.join(blocker, "send-to-nobu")], stdout=out)
        self.assertEqual((code, out.getvalue()), (0, ""))


class StatusTest(Base):
    def status(self, **kw):
        code, out, err = self.w.run("status", **kw)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_pending_is_per_session_and_expires(self):
        self.assertEqual(self.status(), {"pending": False})
        self.w.write(Lines().user("指示").assistant())
        self.w.list()
        self.assertEqual(self.status(), {"pending": True, "count": 1})
        self.assertEqual(self.status(session="another"), {"pending": False})
        self.assertEqual(self.status(now=time.time() + 7 * 3600), {"pending": False})
        code, out, err = self.w.run("send", "--exclude", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.status(), {"pending": False})

    def test_empty_list_still_leaves_pending(self):
        self.w.list()
        self.assertEqual(self.status(), {"pending": True, "count": 0})

    def test_second_call_keeps_send_session_hidden(self):
        # /send-to-nobu → 一覧 → /send-to-nobu <返事> と続けた会話は、あとで一覧に出ない
        L = Lines().user(SEND_CMD).user("スキル本文", isMeta=True).assistant("一覧")
        L.user(SEND_CMD + "\n<command-args>2 は外して。感想: 迷った</command-args>")
        L.user("スキル本文", isMeta=True).assistant("送った")
        L.user("ありがとう").assistant()  # /send-to-nobu を付けない普通の返事が続いても
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)


class CliTest(Base):
    def test_subprocess_uses_session_env(self):
        L = Lines().user("いまの会話").assistant()
        self.w.write(L)
        self.w.write(Lines().user("別の会話").assistant())
        script = os.path.join(os.path.dirname(agentlog.__file__), "agentlog.py")
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID=L.sid)
        r = subprocess.run([sys.executable, script, "list", "--data-dir", self.w.data,
                            "--projects-dir", self.w.projects], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout.decode("utf-8"))
        self.assertEqual(res["count"], 1)
        self.assertNotEqual(res["items"][0]["session_id"], L.sid)
        r = subprocess.run([sys.executable, script, "send", "--exclude", "none", "--data-dir", self.w.data,
                            "--code", "x", "--api-base", agentlog.ALLOWED_API_BASES[0]],
                           capture_output=True, env=dict(os.environ, CLAUDE_CODE_SESSION_ID="other"))
        self.assertEqual(r.returncode, agentlog.EXIT_USAGE)
        self.assertIn("別の会話", r.stderr.decode("utf-8"))

    def test_rejects_foreign_data_dir(self):
        o, e = io.StringIO(), io.StringIO()
        code = agentlog.main(["list", "--data-dir", os.path.join(self.w.tmp, "other-plugin-data"),
                              "--projects-dir", self.w.projects], stdout=o, stderr=e)
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertIn("--data-dir", e.getvalue())


if __name__ == "__main__":
    unittest.main()
