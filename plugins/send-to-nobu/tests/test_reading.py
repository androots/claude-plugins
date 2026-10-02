# -*- coding: utf-8 -*-
"""会話ログの読み方（実データ由来の癖）・朝の知らせ・送り方。すべて一時の設定ディレクトリと合成の会話ログ。"""

import datetime
import io
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CWD, FakeInbox, Lines, Patched, World, agentlog, iso  # noqa: E402
from test_guarantees import Base  # noqa: E402


def builtin(name):
    """組み込みコマンドの行（<command-name> が先頭）。"""
    return "<command-name>/%s</command-name>\n<command-message>%s</command-message>\n<command-args></command-args>" % (
        name, name)


class Reading(Base):
    def first_question(self):
        return self.w.pending()["questions"][0]["question"]

    def test_what_counts_as_a_human_instruction(self):
        # 人の指示でないものだけの会話は出さない
        L = Lines().user(builtin("model")).user("<local-command-stdout>Set model</local-command-stdout>")
        L.user("ピアから", origin={"kind": "peer"}).user("<task-notification>x</task-notification>")
        L.user("メタ", isMeta=True).user("サイドチェーン", isSidechain=True).user("[Request interrupted by user]")
        L.user("<system-reminder>注意</system-reminder>").tool("Bash", {"command": "ls"})
        L.user([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}])
        L.assistant()
        self.w.write(L)
        self.w.list()
        self.assertEqual(self.w.listed(), [])
        # 組み込みコマンドのあとの指示・スキル呼び出し・! コマンド・作業中に打った文は人の指示
        for first, title in (("<system-reminder>注意</system-reminder>\n本当の指示", "本当の指示"),
                             ("<command-message>deck</command-message>\n<command-name>/deck</command-name>\n"
                              "<command-args>来週の資料</command-args>", "/deck 来週の資料"),
                             ("<bash-input>ls -la</bash-input>", "!ls -la")):
            M = Lines().user(builtin("context")).assistant("内訳").user(first).assistant()
            self.w.write(M)
        Q = Lines().meta("attachment", attachment={"type": "queued_command", "commandMode": "prompt",
                                                   "prompt": "作業中に打った文"}, isSidechain=False)
        self.w.write(Q.assistant())
        self.w.next_day()
        self.w.list()
        self.assertEqual(sorted(self.w.titles().values()), sorted(["本当の指示", "/deck 来週の資料", "!ls -la", "作業中に打った文"]))

    def test_title_prefers_last_custom_then_last_ai_title(self):
        a = Lines().user("最初の指示").assistant().meta("ai-title", aiTitle="AI の題").meta("custom-title", customTitle="自分の題")
        a.meta("ai-title", aiTitle="あとの AI の題")
        b = Lines().user("x").assistant().meta("ai-title", aiTitle="一つ目").meta("ai-title", aiTitle="最後の AI の題")
        c = Lines().user("  とても長い最初の指示。" + "あ" * 60 + "\n二行目").assistant()
        for L in (a, b, c):
            self.w.write(L)
        self.w.list()
        titles = {it["session_id"]: it["title"] for it in self.w.pending()["items"]}
        self.assertEqual(titles[a.sid], "自分の題")
        self.assertEqual(titles[b.sid], "最後の AI の題")
        self.assertEqual(titles[c.sid], ("とても長い最初の指示。" + "あ" * 60)[:39] + "…")

    def test_lines_are_not_sorted_or_cut_by_time(self):
        now = time.time()
        L = Lines().user("一つ目", at=now - 100).assistant(at=now - 50).user("二つ目", at=now - 5000).assistant(at=now - 4000)
        path = self.w.write(L)
        self.w.list()
        self.assertEqual(self.w.pending()["items"][0]["last_activity"], agentlog.iso_utc(agentlog.parse_ts(iso(now - 50))))
        self.w.answer_simple()
        self.send()
        with open(path, "rb") as f:
            self.assertEqual(self.inbox.body(L.sid), f.read())

    def test_automatic_runs_are_not_listed_but_continued_ones_are(self):
        self.w.write(Lines(entrypoint="sdk-cli").user("自動実行").assistant())
        mixed = Lines(entrypoint="sdk-cli").user("自動で始めた").assistant()
        mixed.entrypoint = "cli"
        self.w.write(mixed.user("人が続けた").assistant())
        self.w.list()
        self.assertEqual(self.w.listed(), [mixed.sid])

    def test_first_run_is_the_last_7_days(self):
        old = time.time() - 10 * 86400
        path = self.w.write(Lines(base=old).user("古い会話").assistant(), mtime=old + 600)
        opened = Lines(base=old).user("開いて閉じただけ").assistant()
        opened_path = self.w.write(opened, mtime=old + 600)
        self.w.append(opened_path, Lines(sid=opened.sid).meta("last-prompt", lastPrompt="x").meta("mode", mode="default"))
        self.w.list()
        self.assertEqual(self.w.listed(), [])
        self.w.append(path, Lines(sid=os.path.basename(path)[:-6]).user("また使った").assistant())
        self.w.next_day()
        self.w.list()
        self.assertEqual(self.w.listed(), [os.path.basename(path)[:-6]])

    def test_subagents_are_read_recursively(self):
        L = Lines().user("調べて").assistant()
        path = self.w.write(L)
        sub = Lines().user("サブの指示").assistant()
        for rel in ("agent-a1.jsonl", "agent-a1.meta.json", "workflows/wf_1/agent-c3.jsonl", "workflows/wf_1/journal.jsonl",
                    "bad name!.jsonl", "notes.txt", "a/b/c/d/e/f/deep.jsonl"):
            self.w.subagent(path, rel, b'{"agentType":"Explore"}' if rel.endswith(".json") else sub.encode())
        os.makedirs(os.path.join(path[:-6], "tool-results"))
        with open(os.path.join(path[:-6], "tool-results", "t.json"), "w") as f:
            f.write("{}")
        self.w.list()
        self.w.answer_simple()
        code, res = self.send()
        self.assertEqual(code, 0, res)
        rels = [s["rel"] for s in self.finish()["sent"][0]["subagents"]]
        self.assertEqual(rels, ["agent-a1.jsonl", "agent-a1.meta.json", "workflows/wf_1/agent-c3.jsonl",
                                "workflows/wf_1/journal.jsonl"])

    def test_invalid_utf8_and_a_half_written_last_line(self):
        L = Lines().user("前の指示").assistant()
        L.raw(b'{"isSidechain":false,"type":"user","message":{"role":"user","content":"\xe3\x81 \xff \xe5\xa3\x8a"},'
              b'"uuid":"u-bad","timestamp":"' + iso(time.time() - 60).encode() + b'","cwd":"' + CWD.encode() + b'"}')
        L.raw(b"{not json at all")
        path = self.w.write(L)
        size = os.path.getsize(path)
        self.w.append(path, '{"type":"user","message":{"content":"書きかけ'.encode("utf-8"))
        self.w.list()
        self.assertEqual(self.w.pending()["items"][0]["offset"], size)
        self.w.answer_simple()
        self.send()
        body = self.inbox.body(L.sid)
        self.assertEqual(len(body), size)
        self.assertNotIn("書きかけ".encode("utf-8"), body)
        self.assertIn(b"\xff", body)    # 読めない文字も手を加えずに送る

    def test_the_selection_screen(self):
        self.w.write(Lines().user("請求書の集計").assistant())
        res = self.w.list()
        qs = res["ask"]["questions"]
        self.assertEqual(res["next"], agentlog.NEXT_ASK)
        self.assertEqual(len(qs), 2)
        self.assertEqual(qs[0]["question"].splitlines(), [
            "未送信の会話が 1 件ある。外したもの以外を のぶろう に送る。", "AI が中身を読んだ。外す候補はなし", "",
            "1. 請求書の集計", "",
            "送らない会話は？（外すなら入力欄に番号。例: 3, 5-7）"])
        self.assertEqual([o["label"] for o in qs[0]["options"]], [agentlog.NONE_LABEL, agentlog.PASS_LABEL])
        self.assertEqual([o["label"] for o in qs[1]["options"]], list(agentlog.NOTE_OPTIONS))
        self.assertFalse(qs[0]["multiSelect"] or qs[1]["multiSelect"])
        self.assertNotIn("session_id", json.dumps(res))     # 出力は質問だけ（会話 ID・パスは控えにだけ）

    def test_nothing_to_send_still_takes_the_note(self):
        res = self.w.list()
        self.assertEqual(len(res["ask"]["questions"]), 1)
        self.assertTrue(res["ask"]["questions"][0]["question"].startswith("送る会話はない"))
        self.w.answer_simple(exclude=None, note="質問: MCP って何？")
        code, res = self.send()
        self.assertEqual((code, res["say"]), (0, "感想を送った"))
        self.assertEqual(self.finish()["note"], "質問: MCP って何？")
        self.w.list()
        self.w.answer_simple(exclude=None, note="特になし")
        code, res = self.w.run("send")     # 送るものが無いときはサーバーに行かない（引換券も要らない）
        self.assertEqual((code, res["say"]), (0, "今回は何も送っていない"))


class Nudge(Base):
    def nudge(self, now, session=None):
        return self.w.run("nudge", now=now, session=session)[1].get("systemMessage")

    def test_once_a_day_with_6am_as_the_boundary(self):
        self.conv("未送信の会話")
        day = datetime.datetime.now().replace(hour=7, minute=0, second=0, microsecond=0).timestamp()
        self.assertIn("未送信の会話が 1 件", self.nudge(day))
        self.assertIsNone(self.nudge(day + 3600))
        self.assertIsNone(self.nudge(day + 22 * 3600))           # 翌朝 5 時はまだ同じ日
        self.assertIsNotNone(self.nudge(day + 24 * 3600))

    def test_counts_what_the_list_shows_and_is_quiet_when_zero(self):
        self.assertIsNone(self.nudge(time.time()))
        self.assertFalse(os.path.exists(os.path.join(self.w.data, "nudged.json")))
        self.conv("A")
        self.conv("B")
        self.w.write(Lines(entrypoint="sdk-cli").user("自動").assistant())
        self.assertIn("2 件", self.nudge(time.time(), session="00000000-0000-4000-8000-000000000000"))
        self.assertEqual(self.w.list()["count"], 2)

    def test_never_gets_in_the_way(self):
        for argv in (["nudge"], ["nudge", "--data-dir", "/etc"], ["nudge", "--bogus"]):
            out = io.StringIO()
            self.assertEqual(agentlog.main(argv, stdout=out), 0)
            self.assertEqual(out.getvalue(), "")
        os.makedirs(self.w.data)
        with open(os.path.join(self.w.data, "state.json"), "w") as f:
            f.write("{broken")
        self.assertIsNone(self.nudge(time.time()))


class Sending(Base):
    def setUp(self):
        super().setUp()
        self.L = Lines().user("調べて").assistant()
        self.path = self.w.write(self.L)

    def test_uploads_go_in_batches_and_the_finish_follows_the_contract(self):
        for i in range(120):
            self.w.subagent(self.path, "agent-%03d.jsonl" % i, Lines().user("サブ %d" % i).assistant())
        self.w.list()
        self.w.answer_simple(note="感想")
        code, res = self.send()
        self.assertEqual(code, 0, res)
        self.assertEqual(self.inbox.upload_batches, [100, 21])
        body = self.finish()
        self.assertEqual(sorted(body), ["excluded_count", "note", "plugin_version", "sent"])
        self.assertEqual(body["plugin_version"], "0.6.0")
        s = body["sent"][0]
        self.assertEqual(sorted(s), sorted(["session_id", "bytes", "sha256", "title", "project", "last_activity",
                                            "redactions", "subagents"]))
        self.assertEqual((s["title"], s["project"], len(s["subagents"])), ("調べて", "~/work/billing", 120))

    def test_a_lost_finish_reply_counts_as_done_on_retry(self):
        self.inbox.finish_lose_first = True     # 送信票は置けたが 502。リトライで 409 already_finished → 成功
        self.w.list()
        self.w.answer_simple()
        code, res = self.send()
        self.assertEqual((code, res["sent"]), (0, 1))

    def test_a_used_or_expired_code_asks_for_a_new_one(self):
        self.w.list()
        self.w.answer_simple()
        code, res = self.w.run("send", "--code", "x" * 26, "--api-base", self.inbox.base)
        self.assertEqual(code, 4)
        self.assertEqual(res, {"next": agentlog.NEXT_NEW_CODE})
        self.assertEqual(self.w.state()["sessions"], {})
        code, res = self.send()     # 新しい引換券で。答えはもう一度読む（聞き直さない）
        self.assertEqual((code, res["sent"]), (0, 1))

    def test_only_our_server_and_storage(self):
        self.w.list()
        self.w.answer_simple()
        code, res = self.w.run("send", "--code", FakeInbox.CODE, "--api-base", "https://example.com")
        self.assertEqual(code, 2)
        self.inbox.put_url_base = "http://127.0.0.1:9"      # 決まった場所ではないアップロード先
        code, res = self.send()
        self.assertEqual(code, 1)
        self.assertIn("アップロード先がおかしい", res["say"])
        self.inbox.put_url_base = None
        self.inbox.redirect_uploads = True                   # リダイレクトは追わない
        code, res = self.send()
        self.assertEqual((code, self.inbox.redirect_hits), (1, 0))
        self.assertEqual(self.inbox.objects, {})

    def test_same_content_packs_the_same(self):
        tmp = self.w.tmp
        it = {"session_id": self.L.sid, "offset": os.path.getsize(self.path), "subs": [],
              "title": "t", "project": "", "last_activity": None}
        os.makedirs(os.path.join(tmp, "a"))
        os.makedirs(os.path.join(tmp, "b"))
        self.assertEqual(agentlog.pack_session(it, os.path.join(tmp, "a"))[1],
                         agentlog.pack_session(it, os.path.join(tmp, "b"))[1])

    def test_data_dir_must_be_the_plugins_own(self):
        for bad in ("", os.path.join(self.w.tmp, "elsewhere", "send-to-nobu-x"),
                    os.path.join(self.w.tmp, "plugins", "data", "other-plugin")):
            out = io.StringIO()
            self.assertEqual(agentlog.main(["list", "--data-dir", bad], stdout=out), 1)
            self.assertIn("データの置き場所", json.loads(out.getvalue())["say"])

    def test_the_real_entrypoint_uses_the_env(self):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.w.tmp, CLAUDE_CODE_SESSION_ID=self.w.current,
                   CLAUDE_PLUGIN_DATA="/somewhere/else")
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "agentlog.py")
        r = subprocess.run([sys.executable, script, "list", "--data-dir", self.w.data], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["count"], 1)


if __name__ == "__main__":
    unittest.main()
