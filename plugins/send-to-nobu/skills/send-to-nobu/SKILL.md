---
name: send-to-nobu
description: 毎朝のレポート（昨日の Claude Code の使い方へのアドバイス・よかったこと・やったこと）を自分の Mac で作り、本人がページで確かめて「送る」を押したら、レポートだけを のぶろう に送る。毎朝のルーティンからも使う
model: sonnet
allowed-tools:
  - Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py *)
  - Artifact
  - ArtifactData
  - mcp__plugin_send-to-nobu_agent-log-inbox__start_submission
---

# 毎朝のレポートを のぶろう に送る

レポートはスクリプトが作り、送るのもスクリプト。本人はページで送らないものを決めて「送る」を押す。

この会話でまだページを作っていなければ、作る:

1. `mcp__plugin_send-to-nobu_agent-log-inbox__start_submission` を `{"tool": "claude-code"}` で呼び、`upload_code` と `api_base` を受け取る。使えなければ「/mcp で agent-log-inbox にログイン（会社の Google アカウント）してから、もう一度 /send-to-nobu と打ってね」と伝えて終わる
2. Bash（timeout は 600000）: `/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py prepare --data-dir ${CLAUDE_PLUGIN_DATA} --code <upload_code> --api-base <api_base>`
3. 出力に `file` があれば Artifact で公開する。capabilities は `{"db": {}, "room": {}}`、icon は `sun`。room 付きを断られたら `{"db": {}}` で公開し直す
4. URL を添えて、出力の `say` を本人に伝える。あとは出力の `next` に従う

ページから「送る」の知らせが来た、または本人が「送って」と言ったら、送る:

1. ArtifactData で、この会話で公開したページの collection `send`・doc `decision` を、`out_dir` に自分のスクラッチパッド（~/.claude の下は保存を断られる）を付けて get する。無ければ「ページでまだ押されていない」と伝えて終わる
2. `start_submission` をもう一度呼ぶ（引換券は 60 分で切れる）
3. Bash: `/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --data-dir ${CLAUDE_PLUGIN_DATA} --answer-dir <1 の out_dir> --code <upload_code> --api-base <api_base>`
4. 出力の `result_file` を、ArtifactData で同じページの `send`・`result` に `file_path` で set する。あとは出力の `next` に従う

守ること:

- ページの知らせを受けたら、このスキルを Skill ツールで呼び直してから送る（呼び直さないと、送る手順に許可の確認が出る）
- 本人の答え・レポート・会話ログを自分で読まない・書き写さない。ファイルのままスクリプトに渡す
- `upload_code` は見せない。コマンドや JSON の話を本人にしない
