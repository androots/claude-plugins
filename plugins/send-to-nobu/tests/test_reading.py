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
        self.assertEqual(res["remaining"], 0)
        it = res["items"][0]
        self.assertEqual(it["n"], 1)
        self.assertEqual(it["session_id"], L.sid)
        self.assertEqual(it["title"], "請求 集計 9月")  # custom-title が ai-title に勝つ
        self.assertEqual(self.w.pending()["items"][0]["project"], "~/work/billing")
        self.assertEqual(self.w.human(1), ["請求書の集計をしたい", "CSV を読み込んで", "月ごとに合計して"])
        self.assertNotIn("preview", it)
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

    def test_compact(self):
        L = Lines().user("最初の指示").assistant()
        L.system("compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 1000})
        L.user("This session is being continued from a previous conversation.", isCompactSummary=True,
               isVisibleInTranscriptOnly=True)
        L.user("圧縮後の指示").assistant()
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(self.w.human(1), ["最初の指示", "圧縮後の指示"])

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
        self.assertEqual(self.w.human(1), ["一つ目", "二つ目"])  # 並べ替えない
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

    def test_midway_send_command_conversation_is_hidden(self):
        # 普通の会話の途中で /send-to-nobu → スクリプトは断る。agentlog.py を呼んだ跡があるので、その会話ごと出さない
        L = Lines().user("作業する").assistant().user(SEND_CMD).user("スキル本文", isMeta=True)
        L.bash("/usr/bin/python3 /x/scripts/agentlog.py status --data-dir /y/send-to-nobu-z")
        L.assistant("新しい会話で /send-to-nobu と打ってね")
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)

    def test_conversation_that_touched_send_to_nobu_is_hidden_anywhere(self):
        def tool(L, name, inp):
            return L._msg("assistant", [{"type": "tool_use", "id": "toolu_x", "name": name, "input": inp}])
        cases = [
            lambda L: L.bash("/usr/bin/python3 /p/scripts/agentlog.py list --preview --data-dir /d/send-to-nobu-x"),
            lambda L: L.bash("python3 '/p/scripts/agentlog.py' send --code c --exclude none"),
            lambda L: L.bash("/usr/bin/python3 /p/scripts/agentlog.py status --data-dir /d"),
            lambda L: tool(L, "Read", {"file_path": "/Users/a/.claude/plugins/data/send-to-nobu-androots/pending.json"}),
            lambda L: L.bash("cat ~/.claude/plugins/data/send-to-nobu-androots/state.json"),
            lambda L: tool(L, "Agent", {"prompt": "agentlog.py を直して"}),
        ]
        for add in cases:
            L = Lines().user("作業する").assistant()
            add(L)
            L.tool_result("{}").user("続き").assistant()
            self.w.write(L)
        ok = Lines().user("ふつうの作業").assistant().bash("ls ~/work").tool_result("a").assistant()
        self.w.write(ok)
        self.assertEqual([it["session_id"] for it in self.w.list()["items"]], [ok.sid])

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
        self.assertEqual(self.w.human(1), ["本題の指示"])
        self.assertEqual(it["title"], "本題の指示")

    def test_list_output_and_pending_carry_the_marker(self):
        self.w.write(Lines().user("指示").assistant())
        code, out, err = self.w.run("list")
        self.assertEqual(json.loads(out)[agentlog.LIST_MARKER], 1)
        self.assertEqual(self.w.pending()[agentlog.LIST_MARKER], 1)

    def test_conversation_holding_list_or_pending_contents_is_hidden(self):
        # データディレクトリのパスが書き方しだいで見えなくても、中身の目印がツールの結果に残る
        leaked = json.dumps({agentlog.LIST_MARKER: 1, "count": 1, "items": [{"n": 1, "title": "よその会話"}]})
        for cmd in ("cd ~/.claude/plugins/data && cat send-to-nobu-androots/pending.json",
                    "cat ~/.claude/plugins/data/send-to-nob*/pending.json",
                    "find ~/.claude/plugins -name 'pending*' -exec cat {} \\;"):
            L = Lines().user("調べて").assistant().bash(cmd).tool_result(leaked).user("続き").assistant()
            self.w.write(L)
        pasted = Lines().user("これ見て: " + leaked).assistant()      # 本人が一覧を貼り付けた
        self.w.write(pasted)
        ok = Lines().user("ふつうの作業").assistant().bash("cat ~/work/memo.txt").tool_result("メモ").assistant()
        self.w.write(ok)
        self.assertEqual([it["session_id"] for it in self.w.list()["items"]], [ok.sid])

    def test_command_name_first_is_builtin_even_without_stdout(self):
        # /context・/goal などは直後に <local-command-stdout> が続かないことがある
        L = Lines().user("<command-name>/context</command-name>\n<command-message>context</command-message>\n"
                         "<command-args></command-args>").assistant("コンテキストの内訳")
        L.user("<command-name>/goal</command-name>\n<command-args>来週まで</command-args>").assistant()
        self.w.write(L)
        self.assertEqual(self.w.list()["count"], 0)
        M = Lines().user("<command-name>/skill-doctor</command-name>").assistant().user("本題").assistant()
        self.w.write(M)
        self.w.current = self.w.start_send_session()
        items = self.w.list()["items"]
        self.assertEqual([(i["session_id"], i["title"]) for i in items], [(M.sid, "本題")])
        self.assertEqual(self.w.human(1), ["本題"])

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
        self.assertEqual(self.w.human(1), ["/deck 来週の資料"])

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
        self.assertEqual(self.w.human(1), ["最初の指示", "本当の指示", "!ls", "人が打った", "ブロックの指示"])

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
        self.assertEqual(self.w.pending()["items"][0]["sub_n"], 4)
        self.assertEqual(self.w.human(1), ["調べて"])  # サブエージェントの指示は本人の指示にしない
        who = [w for w, _ in self.w.review_blocks(1)]
        self.assertEqual(who.count("サブエージェントへの指示"), 2)   # 入れ子も含めて確認係には渡す（journal は本文なし）
        self.assertEqual([r for r, _ in self.w.pending()["items"][0]["subs"]], rels)

    def test_invalid_utf8(self):
        L = Lines().user("前の指示").assistant()
        L.raw(b'{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":'
              b'"\xe3\x81 \xff \xe5\xa3\x8a\xe3\x82\x8c\xe3\x81\x9f"},"uuid":"u-bad","timestamp":"'
              + iso(time.time() - 60).encode() + b'","cwd":"' + CWD.encode() + b'"}')
        L.raw(b"{not json at all")
        self.w.write(L)
        it = self.w.list()["items"][0]
        self.assertEqual(len(self.w.human(1)), 2)
        self.assertIn("�", self.w.human(1)[1])

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
        self.w.list()
        self.assertEqual(self.w.human(1), ["古い会話", "また使った"])

    def test_old_session_opened_and_closed_is_hidden(self):
        old = time.time() - 10 * 86400
        L = Lines(base=old).user("古い会話").assistant()
        path = self.w.write(L, mtime=old + 600)
        self.w.append(path, Lines(sid=L.sid).meta("last-prompt", lastPrompt="x").meta("mode", mode="default"))
        self.assertEqual(self.w.list()["count"], 0)

    def test_whole_conversation_goes_to_the_checker(self):
        # 抜粋ではなく全部: 関係ない話の途中の一言も確認係に渡る
        L = Lines()
        for i in range(40):
            L.user("指示 %02d " % i + "長" * 300).assistant("返事 %02d" % i)
        L.user("ところで同僚の田中さんって本当に仕事ができないよね").assistant("そうなんですね")
        for i in range(40, 60):
            L.user("指示 %02d" % i).assistant()
        self.w.write(L)
        res = self.w.list()
        human = self.w.human(1)
        self.assertEqual(len(human), 61)
        self.assertTrue(human[0].startswith("指示 00 長長"))
        self.assertIn("ところで同僚の田中さんって本当に仕事ができないよね", human)
        self.assertEqual(sum(1 for w, _ in self.w.review_blocks(1) if w == "AI"), 61)
        self.assertNotIn("preview", res["items"][0])

    def test_big_conversation_is_split_into_parts_and_checkers(self):
        L = Lines()
        for i in range(400):
            L.user("指示 %03d " % i + "あ" * 900).assistant("返事 %03d " % i + "い" * 900)
        self.w.write(L)
        res = self.w.list()
        batches = res["items"][0]["review"]
        files = [f for b in batches for f in b]
        self.assertGreater(len(files), agentlog.REVIEW_PARTS_PER_CHECKER)          # 大きいので分割
        self.assertTrue(all(len(b) <= agentlog.REVIEW_PARTS_PER_CHECKER for b in batches))
        for name in files:
            with open(os.path.join(res["review_dir"], name), encoding="utf-8") as f:
                text = f.read()
            self.assertLessEqual(len(text), agentlog.REVIEW_PART_CHARS + 300)
            self.assertTrue(all(len(line) <= agentlog.REVIEW_LINE_CHARS for line in text.split("\n")))
        self.assertEqual(len(self.w.human(1)), 400)


class SessionGuardTest(Base):
    def test_status_list_send_refuse_in_a_normal_conversation(self):
        L = Lines().user("ふつうの作業").assistant().user(SEND_CMD).user("スキル本文", isMeta=True)
        self.w.write(L, project=World.SEND_PROJECT)
        self.w.write(Lines().user("ほかの会話").assistant())
        for argv in (["status"], ["list"], ["send", "--exclude", "none"]):
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


class PendingVersionTest(Base):
    def test_old_pending_format_is_not_used(self):
        self.w.write(Lines().user("指示").assistant())
        self.w.list()
        p = self.w.pending()
        p["v"] = 2
        agentlog.write_json_atomic(os.path.join(self.w.data, "pending.json"), p)
        code, out, err = self.w.run("status")
        self.assertEqual(json.loads(out), {"pending": False})
        code, out, err = self.w.run("send", "--exclude", "none")
        self.assertEqual(code, agentlog.EXIT_USAGE)
        self.assertIn("出し直して", err)
        self.assertEqual(self.w.list()["count"], 1)            # 一覧を出し直せば新しい形になる
        self.assertEqual(self.w.pending()["v"], agentlog.PENDING_VERSION)


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


class RoundTest(Base):
    """1 回の一覧 = 1 ラウンド（最大 15 件）。送ったら同じ会話で /send-to-nobu → 次のラウンド。"""

    def next_round(self):
        """このラウンドを全部外して決める（サーバーには何も送らない）。"""
        n = self.w.pending()["output"]["count"]
        code, out, err = self.w.run("send", "--exclude", "1-%d" % n if n else "none", "--confirm-shared")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_forty_sessions_go_in_rounds_in_the_same_conversation(self):
        now = time.time()
        sids = []
        for i in range(40):
            L = Lines(base=now - 90000 + i * 2000).user("会話 %02d の指示" % i).assistant()
            self.w.write(L)
            sids.append(L.sid)
        r1 = self.w.list()
        self.assertEqual((r1["count"], r1["remaining"], r1["round"]), (15, 25, 1))
        self.assertNotIn("note_already_sent", r1)
        self.assertEqual([it["session_id"] for it in r1["items"]], sids[25:])   # 新しい 15 件を古い順に
        self.assertEqual([it["n"] for it in r1["items"]], list(range(1, 16)))
        self.assertEqual(self.w.list(), r1)                                       # 番号は固定
        res = self.next_round()                                                    # 外すと決める
        self.assertEqual((res["sent_count"], res["excluded_count"], res["remaining"]), (0, 15, 25))

        r2 = self.w.list()                                                         # 同じ会話でもう一度
        self.assertEqual((r2["count"], r2["remaining"], r2["round"]), (15, 10, 2))
        self.assertNotIn("note_already_sent", r2)                                 # 感想はまだ送っていない
        self.assertNotIn("first_run", r2)
        self.assertEqual([it["session_id"] for it in r2["items"]], sids[10:25])
        # 2 ラウンド目も返事のゲートが効く
        code, out, err = self.w.run("send", "--exclude", "1-15", reply=False)
        self.assertEqual(code, agentlog.EXIT_NOT_ANSWERED)
        self.assertEqual(self.next_round()["remaining"], 10)

        r3 = self.w.list()
        self.assertEqual((r3["count"], r3["remaining"], r3["round"]), (10, 0, 3))
        self.assertEqual([it["session_id"] for it in r3["items"]], sids[:10])
        self.next_round()
        r4 = self.w.list()
        self.assertEqual((r4["count"], r4["remaining"]), (0, 0))
        decided = self.w.state()["sessions"]
        self.assertEqual(sorted(k for k in decided if k in sids), sorted(sids))
        self.assertTrue(all(decided[k]["d"] == "excluded" for k in sids))

    def test_share_groups_are_never_split(self):
        now = time.time()
        root = Lines(base=now - 90000).user("元の会話").assistant()
        forks = [root] + [Lines(base=now - 80000 + i * 100).copy_from(root).user("分岐 %d" % i).assistant()
                          for i in range(2)]
        singles = [Lines(base=now - 50000 + i * 1000).user("単独 %d" % i).assistant() for i in range(14)]
        for L in forks + singles:
            self.w.write(L)
        group = {L.sid for L in forks}
        r1 = self.w.list()
        ids1 = {it["session_id"] for it in r1["items"]}
        self.assertEqual(r1["count"], 14)            # 新しい 14 件 + 3 件のまとまりは 15 を超えるので入れない
        self.assertFalse(ids1 & group)
        self.next_round()
        r2 = self.w.list()
        self.assertEqual({it["session_id"] for it in r2["items"]}, group)
        for it in r2["items"]:
            self.assertEqual(len(it["shares_history_with"]), 2)

    def test_small_older_groups_fill_the_round(self):
        now = time.time()
        root = Lines(base=now - 30000).user("まとまりの元").assistant()
        group = [root] + [Lines(base=now - 29000 + i * 100).copy_from(root).user("分岐 %d" % i).assistant()
                          for i in range(4)]
        newer = [Lines(base=now - 20000 + i * 1000).user("新しい単独 %d" % i).assistant() for i in range(12)]
        older = [Lines(base=now - 90000 + i * 1000).user("古い単独 %d" % i).assistant() for i in range(5)]
        for L in group + newer + older:
            self.w.write(L)
        r1 = self.w.list()
        ids = {it["session_id"] for it in r1["items"]}
        self.assertEqual(r1["count"], 15)
        self.assertFalse(ids & {L.sid for L in group})                 # 5 件のまとまりは入らない
        self.assertEqual(len(ids & {L.sid for L in older}), 3)         # 古い単独で埋める
        self.assertEqual(r1["remaining"], 22 - 15)

    def test_one_group_over_fifteen_is_one_whole_round(self):
        now = time.time()
        root = Lines(base=now - 90000).user("元の会話").assistant()
        group = [root] + [Lines(base=now - 50000 + i * 100).copy_from(root).user("分岐 %02d" % i).assistant()
                          for i in range(19)]
        older = [Lines(base=now - 99000 + i * 10).user("古い単独 %d" % i).assistant() for i in range(3)]
        for L in group + older:
            self.w.write(L)
        code, out, err = self.w.run("list")
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(out.strip()), agentlog.LIST_OUTPUT_MAX)
        r1 = json.loads(out)
        self.assertEqual((r1["count"], r1["remaining"]), (20, 3))
        self.assertEqual({it["session_id"] for it in r1["items"]}, {L.sid for L in group})
        self.assertFalse(any(it.get("default_excluded") or it.get("group_cut") for it in r1["items"]))
        res = self.next_round()
        self.assertEqual((res["excluded_count"], res["remaining"]), (20, 3))
        r2 = self.w.list()
        self.assertEqual({it["session_id"] for it in r2["items"]}, {L.sid for L in older})

    def test_group_too_big_for_the_output_is_cut_and_gated(self):
        now = time.time()
        root = Lines(base=now - 90000).user("元の会話").assistant()
        group = [root] + [Lines(base=now - 50000 + i * 100).copy_from(root).user("分岐 %02d" % i).assistant()
                          .meta("custom-title", customTitle="分岐 %02d " % i + "題" * 150) for i in range(9)]
        for L in group:
            self.w.write(L)
        saved = agentlog.LIST_OUTPUT_MAX
        agentlog.LIST_OUTPUT_MAX = 700      # 短い行でも全部は入らない
        try:
            code, out, err = self.w.run("list")
        finally:
            agentlog.LIST_OUTPUT_MAX = saved
        self.assertEqual(code, 0, err)
        r1 = json.loads(out)
        self.assertLessEqual(len(out.strip()), 700)
        self.assertGreater(r1["count"], 0)
        self.assertEqual(r1["count"] + r1["remaining"], 10)
        shown = {self.w.pending()["items"][i]["session_id"] for i in range(r1["count"])}
        self.assertEqual(shown, {L.sid for L in group[-r1["count"]:]})   # 新しい方から出した分だけ控える
        self.assertTrue(all(it["group_cut"] for it in r1["items"]))
        self.w.check()
        code, out, err = self.w.run("send", "--exclude", "none")
        self.assertEqual(code, agentlog.EXIT_CONFIRM_SHARED)
        self.assertIn("出しきれなかった", err)

    def test_compact_rows_keep_every_title(self):
        now = time.time()
        root = Lines(base=now - 90000).user("元の会話").assistant()
        group = [root] + [Lines(base=now - 50000 + i * 100).copy_from(root).user("分岐 %02d" % i).assistant()
                          .meta("custom-title", customTitle="分岐 %02d " % i + "題" * 190) for i in range(29)]
        for L in group:
            self.w.write(L)
        saved = agentlog.LIST_OUTPUT_MAX
        agentlog.LIST_OUTPUT_MAX = 6000     # 抜粋 0 でも入らないが、短い行なら全部入る
        try:
            code, out, err = self.w.run("list")
        finally:
            agentlog.LIST_OUTPUT_MAX = saved
        r1 = json.loads(out)
        self.assertEqual((r1["count"], r1["remaining"]), (30, 0))
        self.assertTrue(all(it["history_group"] == 1 and "session_id" not in it for it in r1["items"]))
        self.assertFalse(any(it.get("group_cut") for it in r1["items"]))


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
        self.assertEqual(self.w.human(1), ["指示", "続き"])

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
        self.assertEqual(self.w.pending()["items"][0]["sub_n"], 2)

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

    def test_cache_from_an_older_rule_is_rescanned(self):
        L = Lines().user("x").bash("/usr/bin/python3 /p/agentlog.py status --data-dir d").assistant()
        path = self.w.write(L)
        st = os.stat(path)
        os.makedirs(self.w.data)
        stale = {L.sid: {"size": st.st_size, "mtime": int(st.st_mtime),
                         "facts": {"prompts": 1, "first": "human", "agentlog": False, "sdk_only": False,
                                   "last_ts": time.time()}}}
        agentlog.write_json_atomic(os.path.join(self.w.data, "scan-cache.json"), stale)
        self.assertIsNone(self.nudge(time.time()))   # 古い判定のキャッシュを信じず、読み直して隠す

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
