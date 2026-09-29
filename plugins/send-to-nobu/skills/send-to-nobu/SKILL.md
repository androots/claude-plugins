---
name: send-to-nobu
description: 自分の Claude Code の会話を のぶろう に送る（毎朝、新しい会話で /send-to-nobu）。送らない会話と感想は選択画面で答える
disable-model-invocation: true
allowed-tools:
  - mcp__plugin_send-to-nobu_agent-log-inbox__start_submission
  - Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py *)
---

# 会話を のぶろう に送る

本人が一覧を見て送らない会話を選び、感想を書く。残りをスクリプトが送る。本人の答えはスクリプトが会話ログから直接読む。

1. `mcp__plugin_send-to-nobu_agent-log-inbox__start_submission` を `{"tool": "claude-code"}` で呼び、`upload_code` と `api_base` を受け取る。使えなければ「/mcp で agent-log-inbox にログイン（会社の Google アカウント）してから、もう一度 /send-to-nobu と打ってね」と伝えて終わる
2. Bash: `/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py list --data-dir ${CLAUDE_PLUGIN_DATA}`
3. あとは出力の `next` に従う。send はこの形で、`<upload_code>` と `<api_base>` に 1 の値をそのまま入れる:
   `/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --data-dir ${CLAUDE_PLUGIN_DATA} --code <upload_code> --api-base <api_base>`

守ること:

- 送り終わるまでターンを終えない（途中で終えると、残りの手順に許可の確認が出る）
- 選択画面は `ask.questions` をそのまま渡す。書き換えない・答えを入れない・一覧を本文に書き写さない
- 外す番号や感想は渡さない（スクリプトが答えを読む）。会話ログや控えのファイルを自分で読まない
- 送る手順で詰まった・道具の不具合に気づいたときだけ、send の末尾に ` --assistant-note - <<'NOTE'` を付けて短く書く（会話の中身・タイトルは書かない）
- `upload_code` は見せない。コマンドや JSON の話を本人にしない
