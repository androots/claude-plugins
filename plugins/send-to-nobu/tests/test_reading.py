# -*- coding: utf-8 -*-
"""会話の読み方・一覧・status・nudge。すべて合成フィクスチャ。"""

import datetime
import io
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CWD, SEND_CMD, SEND_CMD_NS, Lines, World, agentlog, iso, send_cmd  # noqa: E402

ANTHROPIC = "sk-ant-api03-" + "Q1w2E3r4T5y6U7i8O9p0" * 4


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
        """全部外して感想なし（サーバーに送らない）で判断を記録し、翌日の新しい会話を始める。"""
        res = self.w.list()
        nums = ",".join(str(it["n"]) for it in res["items"]) or "none"
        code, out, err = self.w.run("send", "--exclude", nums, "--confirm-shared")
        self.assertEqual(code, 0, err)
        self.w.current = self.w.start_send_session()
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
        self.assertNotIn("remaining", res)
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
        self.assertEqual(p["session_size"], os.path.getsize(self.w.session_file()))

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
        self.assertEqual(self.w.pending()["items"][0]["last_activity"],
                         agentlog.iso_utc(agentlog.parse_ts(iso(now - 50))))

    def test_meta_only(self):
        L = Lines().meta("ai-title", aiTitle="題だけ").meta("last-prompt", lastPrompt="x").meta("mode", mode="default")
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_send_command_first_is_hidden(self):
        for cmd in (SEND_CMD, SEND_CMD_NS):
            L = Lines().user(cmd).user("スキル本文", isMeta=True).assistant("一覧").user("なし").assistant()
            self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_midway_send_command_rejected_then_conversation_is_listed_without_the_list(self):
        # 普通の会話の途中で /send-to-nobu → スクリプトが断るので一覧はその会話に残らない。会話は翌日ふつうに出る
        L = Lines().user("作業する").assistant().user(SEND_CMD).user("スキル本文", isMeta=True)
        L.bash("/usr/bin/python3 /x/scripts/agentlog.py status --data-dir /y/send-to-nobu-z")
        L.assistant("新しい会話で /send-to-nobu と打ってね")
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(it["prompts"], 1)
        self.assertEqual(it["preview"], ["作業する"])

    def test_conversation_that_ran_list_or_send_is_hidden_anywhere(self):
        for cmd in ("/usr/bin/python3 /p/scripts/agentlog.py list --preview --data-dir /d/send-to-nobu-x",
                    "python3 '/p/scripts/agentlog.py' send --code c --exclude none"):
            L = Lines().user("作業する").assistant().bash(cmd).tool_result('{"count": 1}').user("続き").assistant()
            self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_sdk_only_is_hidden_but_mixed_is_listed(self):
        self.w.write(Lines(entrypoint="sdk-cli").user("自動実行のプロンプト").assistant())
        mixed = Lines(entrypoint="sdk-cli").user("自動で始めた").assistant()
        mixed.entrypoint = "cli"
        mixed.user("あとで人が続けた").assistant()
        self.w.write(mixed)
        items = self.by_sid(self.w.list())
        self.assertEqual(list(items), [mixed.sid])

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
        self.assertEqual([r for r, _ in self.w.pending()["items"][0]["subs"]], rels)

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
        self.w.append(self.w.session_file(), Lines(sid=self.w.current).user("いまの会話").assistant())
        self.assertEqual(self.w.list()["count"], 0)

    def test_first_run_baseline(self):
        old = time.time() - 10 * 86400
        L = Lines(base=old).user("古い会話").assistant()
        path = self.w.write(L, mtime=old + 600)
        self.assertEqual(self.w.list()["count"], 0)
        # 後で触られたら出す（別の日の新しい会話で）
        self.w.append(path, Lines(sid=L.sid).user("また使った").assistant())
        self.w.current = self.w.start_send_session()
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

    def test_output_is_capped_and_newest_are_kept(self):
        now = time.time()
        sids = []
        for i in range(80):
            L = Lines(base=now - 80000 + i * 900)
            for j in range(15):
                L.user("会話 %02d の指示 %02d " % (i, j) + "内容" * 100).assistant()
            L.meta("custom-title", customTitle="会話 %02d " % i + "題" * 150)
            self.w.write(L)
            sids.append(L.sid)
        code, out, err = self.w.run("list", "--preview")
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(out.strip()), agentlog.LIST_OUTPUT_MAX)
        res = json.loads(out)
        self.assertGreater(res["remaining"], 0)
        self.assertEqual(res["count"] + res["remaining"], 80)
        self.assertEqual([it["n"] for it in res["items"]], list(range(1, res["count"] + 1)))
        self.assertEqual([it["session_id"] for it in res["items"]], sids[-res["count"]:])  # 新しい方を残す
        p = self.w.pending()
        self.assertEqual([it["session_id"] for it in p["items"]], sids[-res["count"]:])  # 出した分だけ控える
        self.assertTrue(all(len(it["preview"]) == 1 for it in res["items"]))

    def test_previews_shrink_before_items_are_cut(self):
        now = time.time()
        for i in range(12):
            L = Lines(base=now - 50000 + i * 900)
            for j in range(15):
                L.user("指示 %02d " % j + "長" * 200).assistant()
            self.w.write(L)
        res = self.w.list()
        self.assertEqual(res["count"], 12)
        self.assertNotIn("remaining", res)
        self.assertLess(len(res["items"][0]["preview"]), 15)
        self.assertLessEqual(len(json.dumps(res, ensure_ascii=False)), agentlog.LIST_OUTPUT_MAX)


class SessionGuardTest(Base):
    def test_status_list_send_refuse_in_a_normal_conversation(self):
        L = Lines().user("ふつうの作業").assistant().user(SEND_CMD).user("スキル本文", isMeta=True)
        self.w.write(L, project=World.SEND_PROJECT)
        self.w.write(Lines().user("ほかの会話").assistant())
        for argv in (["status"], ["list", "--preview"], ["send", "--exclude", "none"]):
            code, out, err = self.w.run(*argv, session=L.sid)
            self.assertEqual(code, agentlog.EXIT_NOT_SEND_SESSION, argv)
            self.assertEqual(out, "")
            self.assertIn("新しい会話", err)
        self.assertIsNone(self.w.pending())

    def test_unknown_session_is_refused(self):
        code, out, err = self.w.run("list", session="11111111-2222-3333-4444-555555555555")
        self.assertEqual(code, agentlog.EXIT_NOT_SEND_SESSION)

    def test_session_id_is_required(self):
        for argv in (["status"], ["list"], ["send", "--exclude", "none"]):
            code, out, err = self.w.run(*argv, session="")
            self.assertEqual(code, agentlog.EXIT_USAGE, argv)
            self.assertIn("CLAUDE_CODE_SESSION_ID", err)

    def test_test_only_options_are_not_on_the_cli(self):
        for extra in (["--now", "1"], ["--projects-dir", "/tmp"], ["--session", self.w.current]):
            code, out, err = self.w.run("list", *extra)
            self.assertEqual(code, agentlog.EXIT_USAGE, extra)

    def test_data_dir_is_required_and_checked(self):
        o, e = io.StringIO(), io.StringIO()
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.w.current
        try:
            self.assertEqual(agentlog.main(["list"], stdout=o, stderr=e), agentlog.EXIT_USAGE)
            self.assertEqual(agentlog.main(["list", "--data-dir", ""], stdout=o, stderr=e), agentlog.EXIT_USAGE)
            self.assertEqual(agentlog.main(["list", "--data-dir", os.path.join(self.w.tmp, "other-plugin")],
                                           stdout=o, stderr=e), agentlog.EXIT_USAGE)
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertEqual(o.getvalue(), "")


class StateFileTest(Base):
    def test_unreadable_state_fails_and_is_kept(self):
        self.w.write(Lines().user("指示").assistant())
        self.w.list()
        with open(os.path.join(self.w.data, "state.json"), "w") as f:
            f.write("{broken")
        for argv in (["list"], ["send", "--exclude", "none"]):
            code, out, err = self.w.run(*argv)
            self.assertEqual(code, agentlog.EXIT_ERROR, argv)
            self.assertIn("state.json", err)
        with open(os.path.join(self.w.data, "state.json")) as f:
            self.assertEqual(f.read(), "{broken")

    def test_unreadable_excluded_store_fails(self):
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "excluded-uuids.json"), "w") as f:
            f.write("[1,2")
        code, out, err = self.w.run("list")
        self.assertEqual(code, agentlog.EXIT_ERROR)


class SymlinkTest(Base):
    def test_linked_session_dir_and_file_are_not_read(self):
        L = Lines().user("本物").assistant()
        path = self.w.write(L)
        outside = os.path.join(self.w.tmp, "outside")
        os.makedirs(os.path.join(outside, "subagents"))
        with open(os.path.join(outside, "subagents", "agent-x.jsonl"), "wb") as f:
            f.write(Lines().user("外のファイル").assistant().encode())
        os.symlink(outside, path[:-6])                                     # <sid>/ がリンク
        link_sid = Lines().sid
        os.symlink(path, os.path.join(os.path.dirname(path), link_sid + ".jsonl"))  # 会話ファイルがリンク
        os.symlink(os.path.dirname(path), os.path.join(self.w.projects, "-linked-project"))  # プロジェクトがリンク
        res = self.w.list()
        self.assertEqual([it["session_id"] for it in res["items"]], [L.sid])
        self.assertNotIn("subagent_files", res["items"][0])
        self.assertEqual(self.w.pending()["items"][0]["subs"], [])


class SameNumbersTest(Base):
    def test_list_twice_in_the_same_conversation_returns_the_same_items(self):
        self.w.write(Lines(base=time.time() - 7200).user("一つ目").assistant())
        first = self.w.list()
        self.w.write(Lines(base=time.time() - 600).user("あとから増えた会話").assistant())
        second = self.w.list()
        self.assertEqual(first, second)
        self.assertEqual(len(self.w.pending()["items"]), 1)
        # 新しい会話なら作り直す
        self.w.current = self.w.start_send_session()
        self.assertEqual(self.w.list()["count"], 2)


class DecisionTest(Base):
    def test_closed_only_meta_growth_is_not_unsent(self):
        L = Lines().user("指示").assistant()
        path = self.w.write(L)
        self.exclude_all_locally()
        self.assertEqual(self.w.list()["count"], 0)
        self.w.current = self.w.start_send_session()
        self.w.append(path, Lines(sid=L.sid).meta("last-prompt", lastPrompt="指示").meta("ai-title", aiTitle="題"),
                      mtime=time.time() + 5)
        self.assertEqual(self.w.list()["count"], 0)
        # user 行が増えたら出す（前に外した印つき・既定で外す）
        self.w.current = self.w.start_send_session()
        self.w.append(path, Lines(sid=L.sid).user("続き").assistant())
        it = self.w.list()["items"][0]
        self.assertTrue(it["previously_excluded"])
        self.assertTrue(it["default_excluded"])
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
        self.assertTrue(items[b.sid]["default_excluded"])
        self.assertNotIn("previously_excluded", items[b.sid])

    def test_excluded_store_alone_sets_previously_excluded(self):
        L = Lines().user("指示").assistant()
        self.w.write(L)
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "excluded-uuids.json"), "w") as f:
            json.dump({L.sid: []}, f)
        it = self.w.list()["items"][0]
        self.assertTrue(it["previously_excluded"])

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
        other = self.w.start_send_session()
        self.assertEqual(self.status(session=other), {"pending": False})
        self.assertEqual(self.status(now=time.time() + 7 * 3600), {"pending": False})
        code, out, err = self.w.run("send", "--exclude", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.status(), {"pending": False})

    def test_empty_list_still_leaves_pending(self):
        self.w.list()
        self.assertEqual(self.status(), {"pending": True, "count": 0})

    def test_send_session_stays_hidden_after_second_call_and_plain_reply(self):
        # /send-to-nobu → 一覧 → /send-to-nobu <返事> → 普通の返事、と続いても一覧に出ない
        L = Lines().user(SEND_CMD).user("スキル本文", isMeta=True).assistant("一覧")
        L.user(send_cmd("2 は外して。感想: 迷った")).user("スキル本文", isMeta=True).assistant("送った")
        L.user("ありがとう").assistant()
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)


class NudgeTest(Base):
    def nudge(self, now, data_dir=None):
        out = io.StringIO()
        saved = agentlog._now
        agentlog._now = lambda: now
        try:
            code = agentlog.main(["nudge", "--data-dir", self.w.data if data_dir is None else data_dir], stdout=out)
        finally:
            agentlog._now = saved
        self.assertEqual(code, 0)
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

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
        self.w.write(Lines().user("x").bash("/usr/bin/python3 /p/agentlog.py list --data-dir d").assistant())
        self.w.write(Lines(entrypoint="sdk-cli").user("自動").assistant())
        big = Lines().user(builtin("review")[0]).user(builtin("review")[1]).user("自動のプロンプト", isMeta=True)
        for i in range(40):
            big.assistant("作業 %d" % i).tool_result("x" * 40000)   # 人の指示が 1 MB より後ろ
        big.user("本題").assistant()
        self.w.write(big)
        # nudge は「新しい会話」を起動した直後に走る（いまの会話 ID は env）
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.w.current
        try:
            msg = self.nudge(time.time())
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertIn("3 件", msg["systemMessage"])
        self.assertEqual(self.w.list()["count"], 3)

    def test_uses_cache_on_the_next_day(self):
        self.w.write(Lines().user("指示").assistant())
        self.assertIsNotNone(self.nudge(self.local_ts(0, 10)))
        saved = agentlog.scan_session

        def boom(*a, **k):
            raise AssertionError("読み直した")
        agentlog.scan_session = boom
        try:
            self.assertIsNotNone(self.nudge(self.local_ts(1, 10)))   # 変わっていない会話は読まない
        finally:
            agentlog.scan_session = saved

    def test_counts_grown_decided_sessions(self):
        L = Lines().user("指示").assistant()
        path = self.w.write(L)
        self.exclude_all_locally()
        self.assertIsNone(self.nudge(time.time()))
        self.w.append(path, Lines(sid=L.sid).meta("last-prompt", lastPrompt="x"), mtime=time.time() + 5)
        self.assertIsNone(self.nudge(time.time()))
        self.w.append(path, Lines(sid=L.sid).user("続き").assistant())
        self.assertIsNotNone(self.nudge(time.time()))

    def test_empty_data_dir_does_nothing(self):
        self.w.write(Lines().user("指示").assistant())
        self.assertIsNone(self.nudge(time.time(), data_dir=""))
        self.assertFalse(os.path.exists(self.w.data))

    def test_errors_are_swallowed(self):
        out = io.StringIO()
        code = agentlog.main(["nudge", "--data-dir", os.path.join(self.w.tmp, "other-plugin")], stdout=out)
        self.assertEqual((code, out.getvalue()), (0, ""))
        code = agentlog.main(["nudge", "--bogus"], stdout=out)
        self.assertEqual((code, out.getvalue()), (0, ""))
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "state.json"), "w") as f:
            f.write("{broken")
        self.w.write(Lines().user("指示").assistant())
        self.assertIsNone(self.nudge(time.time()))


class CliTest(Base):
    def test_subprocess_uses_env_only(self):
        self.w.write(Lines().user("別の会話").assistant())
        script = os.path.join(os.path.dirname(agentlog.__file__), "agentlog.py")
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.w.tmp, CLAUDE_CODE_SESSION_ID=self.w.current)
        r = subprocess.run([sys.executable, script, "list", "--data-dir", self.w.data], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout.decode("utf-8"))
        self.assertEqual(res["count"], 1)
        other = self.w.start_send_session()
        r = subprocess.run([sys.executable, script, "send", "--exclude", "none", "--data-dir", self.w.data,
                            "--code", "x", "--api-base", agentlog.ALLOWED_API_BASES[0]],
                           capture_output=True, env=dict(env, CLAUDE_CODE_SESSION_ID=other))
        self.assertEqual(r.returncode, agentlog.EXIT_USAGE)
        self.assertIn("別の会話", r.stderr.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
