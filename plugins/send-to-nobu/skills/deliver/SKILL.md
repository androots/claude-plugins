---
name: deliver
description: send-to-nobu の一覧に本人が「外すもの」と「感想」を答えた直後にだけ使う（送信）。それ以外では絶対に使わない
user-invocable: false
allowed-tools:
  - mcp__plugin_send-to-nobu_agent-log-inbox__start_submission
  - Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send *)
---

# 会話ログを送る

直前の /send-to-nobu で本人が答えた「外す番号」と「感想」だけを使う。

1. `mcp__plugin_send-to-nobu_agent-log-inbox__start_submission` を `{"tool": "claude-code"}` で呼ぶ → `upload_code` と `api_base`。
   `upload_code` は本人に見せない。
2. Bash で実行する（Bash の timeout は 600000）。`<code>` と `<api_base>` は 1 の値、`<exclude>` は外す番号をカンマ区切り（なしなら `none`）。
   感想は本人の言葉をそのまま、ヒアドキュメントの中に入れる（言い換え・要約・敬語直し・補足をしない）。
   感想が「なし」なら、`--note-file -` からあと（ヒアドキュメントごと）を付けない。

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --code <code> --exclude <exclude> --api-base <api_base> --data-dir ${CLAUDE_PLUGIN_DATA} --note-file - <<'SEND_TO_NOBU_NOTE'
   （本人の感想をそのまま）
   SEND_TO_NOBU_NOTE
   ```

3. 終了コードで分ける:
   - 0 → 出力の JSON から 1 行で「`sent_count` 件送った・`excluded_count` 件外した」（0 件で感想だけなら「感想を送った」）
   - 3（同じ履歴を共有している）→ エラーの中身を平易に伝えて「それでも送る？（外した方の中身も届く）」と聞く。
     了承なら、もう一度 Skill ツールで `send-to-nobu:deliver` を呼び直し、1 からやり直して 2 に `--confirm-shared` を付ける。
     断られたら、両方外すか聞く
   - 4（引換券が使えない）→ 1 からもう一度だけやり直す
   - それ以外 → 何が起きたかを 1 行で。「状態は変えていないので、明日また一覧に出る」と添える
