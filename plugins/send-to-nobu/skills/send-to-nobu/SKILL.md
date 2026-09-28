---
name: send-to-nobu
description: 自分の Claude Code の会話ログを のぶろう に送る（毎朝 1 回）。一覧を見て、外すものと昨日の感想・質問を 1 回で答える
disable-model-invocation: true
allowed-tools:
  - mcp__plugin_send-to-nobu_agent-log-inbox__whoami
  - Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py list *)
---

# 会話ログを のぶろう に送る（確認と質問）

本人が決めるのは「外すもの」と「話すこと」の 2 つだけ。あなたは一覧を見せて、1 回の返事をもらう。

## 手順

1. `mcp__plugin_send-to-nobu_agent-log-inbox__whoami` を呼ぶ。ツールが見つからない・エラーなら未ログインとみなし、
   次の 1 行だけをそのまま伝えて止まる（ほかの手順に進まない）:
   「`/mcp` で agent-log-inbox にログイン（会社の Google アカウント）してから、もう一度 /send-to-nobu と打ってね」
2. Bash で次をそのまま実行する（書き換えない・前後にコマンドを足さない・パスをクオートしない）:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py list --preview --data-dir ${CLAUDE_PLUGIN_DATA}
   ```

3. `count` が 0 なら、一覧の代わりに「送る会話はないよ」と言って、5 の 2 行目だけを聞く。
4. 一覧を見せる。初期値は全部送る。JSON は見せない。1 会話 1〜2 行:
   - `番号. タイトル（updated・size）` と、preview から読んだ 1 行の要約
   - 気になる点があるときだけ一言添える: 個人的な相談・人事や評価・健康・家族・お金・お客さまの個人情報っぽいもの・パスワード類
   - フラグがあれば必ず平易に添える:
     - `previously_excluded` →「前に外した会話の続き。送ると前に外した部分も届く」
     - `contains_excluded_copy` →「前に外した会話の中身を引き継いでいる」
     - `shares_history_with: [n]` →「n 番と同じ履歴を含む。片方だけ外しても、もう片方から届く」
   - `first_run` なら先頭に「初回なので `since` 以降の分」と一言
5. 最後に 1 回で聞く（AskUserQuestion などの選択ダイアログは使わない）:

   ```
   外すものは？（番号 / なし）
   昨日の感想・わからなかったこと・質問ある？（なければ なし）
   ```

6. 返事で両方そろったら、Skill ツールで `send-to-nobu:deliver` を呼ぶ。片方しか答えていなければ、足りない方だけ聞き直す。
   0 件の日に感想も「なし」なら、何も送らずに終わる。

## 禁止

- 会話ログ（`~/.claude/projects` の JSONL）を Read・cat・grep などで直接読む
- 一覧にない会話を送る。本人が外した会話を送る
- 感想を作文・要約・言い換えする（本人の言葉のまま deliver で送る）
