---
name: send-to-nobu
description: 自分の Claude Code の会話を のぶろう に送る。今日のページ（未送信の会話の一覧）を作り、本人がページで決めて「これで送る」を押したら送る。毎朝のルーティンからも使う
model: sonnet
allowed-tools:
  - Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py *)
  - Artifact
  - ArtifactData
  - mcp__plugin_send-to-nobu_agent-log-inbox__start_submission
---

# 会話を のぶろう に送る

スクリプトが今日のページを作り、本人がページで送らない会話と感想を決める。本人の答えはページの db にあり、スクリプトがそれを読んで送る。

この会話でまだページを作っていなければ、作る:

1. Bash: `/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py page --data-dir ${CLAUDE_PLUGIN_DATA}`
2. 出力の `file` を Artifact で公開する。capabilities は `{"db": {}, "room": {}}`、icon は `send`。room 付きを断られたら `{"db": {}}` で公開し直す（ページが「この会話に『送って』と打つ」案内に切り替わる）
3. 本人に、ページを作ったこと・決めたら「これで送る」を押すことを URL を添えて伝えて終わる

ページから「これで送る」の知らせが来た、または本人が送ってと言ったら、送る:

1. ArtifactData で、この会話で公開したページの collection `send`・doc `decision` を、`out_dir` に `${CLAUDE_PLUGIN_DATA}/page` を付けて get する。無ければ「ページでまだ押されていない」と伝えて終わる
2. `mcp__plugin_send-to-nobu_agent-log-inbox__start_submission` を `{"tool": "claude-code"}` で呼び、`upload_code` と `api_base` を受け取る。使えなければ「/mcp で agent-log-inbox にログイン（会社の Google アカウント）してから、もう一度『送って』と言ってね」と伝えて終わる
3. Bash: `/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --data-dir ${CLAUDE_PLUGIN_DATA} --code <upload_code> --api-base <api_base>`
4. 出力に `result_file` があれば、ArtifactData で同じページの `send`・`result` に `file_path` で set する（ページが受け取り票に変わる）。あとは出力の `next` に従う

守ること:

- ページの知らせを受けたら、このスキルを Skill ツールで呼び直してから送る（呼び直さないと、送る手順に許可の確認が出る）
- 本人の答えはファイルのままスクリプトに渡す。番号や感想を自分で書き写さない。会話ログやページの中身を自分で読まない
- 送る手順で詰まった・道具の不具合に気づいたときだけ、send の末尾に ` --assistant-note - <<'NOTE'` を付けて短く書く（会話の中身・タイトルは書かない）
- `upload_code` は見せない。コマンドや JSON の話を本人にしない
