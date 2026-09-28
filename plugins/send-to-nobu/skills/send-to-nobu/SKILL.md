---
name: send-to-nobu
description: 自分の Claude Code の会話ログを のぶろう に送る（毎朝 1 回）。新しい会話で /send-to-nobu → 一覧、/send-to-nobu <返事> → 送信
disable-model-invocation: true
model: opus
argument-hint: "[一覧を見たあとの返事: 外す番号と感想]"
allowed-tools:
  - mcp__plugin_send-to-nobu_agent-log-inbox__whoami
  - mcp__plugin_send-to-nobu_agent-log-inbox__start_submission
  - Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py *)
---

# 会話ログを のぶろう に送る

本人が決めるのは「外すもの」と「話すこと」の 2 つだけ。途中の段取り（モード・ツール名・コマンド・JSON）は口に出さない。

本人の返事（/send-to-nobu のあとに書いたもの）:「$ARGUMENTS」

コマンドはすべて、書き換えない・前後にコマンドを足さない・パスをクオートしない。
スクリプトが失敗したら（終了コードが 0 以外）、下に書いた場合を除き、エラーの 1 行を平易に伝えて止まる。

## 0. モードを決める

Bash で実行する:

```
/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py status --data-dir ${CLAUDE_PLUGIN_DATA}
```

- 終了コード 6 →「この会話では使えないので、新しい会話を始めて最初に /send-to-nobu と打ってね」とだけ伝えて止まる
- 上の「本人の返事」が空、または `pending` が false → **一覧モード**
- 「本人の返事」があり、`pending` が true → **送信モード**

## 一覧モード

1. `mcp__plugin_send-to-nobu_agent-log-inbox__whoami` を呼ぶ。ツールが見つからない・エラーなら未ログインとみなし、
   次の 1 行だけをそのまま伝えて止まる:
   「`/mcp` で agent-log-inbox にログイン（会社の Google アカウント）してから、もう一度 /send-to-nobu と打ってね」
2. Bash で実行する:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py list --preview --data-dir ${CLAUDE_PLUGIN_DATA}
   ```

3. `count` が 0 なら「送る会話はないよ。感想や質問があれば `/send-to-nobu 感想: ○○` で送れる」と言って終わる。
4. 一覧を見せる。**JSON の `n` のまま、全件を省略せず、並べ替えずに**出す。1 会話 1〜2 行:
   - `n. タイトル（updated・size）` と、preview から読んだ 1 行の要約
   - 気になる点があるときだけ一言添える: 個人的な相談・人事や評価・健康・家族・お金・お客さまの個人情報っぽいもの・パスワード類
   - フラグは必ず平易に添える:
     - `previously_excluded` →「前に外した会話の続き（今回も外す。送るなら『n も送る』と書いて）」
     - `contains_excluded_copy`（`previously_excluded` が無いとき）→「前に外した会話の中身を引き継いでいる（今回は外す。送るなら『n も送る』と書いて）」
     - `history_group`（同じ番号どうし）→「同じ履歴を含む。片方だけ外しても、もう片方から届く」
     - `group_cut` →「同じ履歴の会話が多すぎて全部は出せなかった。送ると出していない分の中身も届く」
     - `shares_history_with: [m]` →「m 番と同じ履歴を含む。片方だけ外しても、もう片方から届く」
   - `first_run` なら先頭に「初回なので `since` 以降の分」と一言
   - `remaining` が 1 以上なら最後に「この `count` 件のあとに `remaining` 件ある（送ったあと /send-to-nobu で続き）」と一言
5. 最後にこう聞いて終わる（AskUserQuestion などの選択ダイアログは使わない）:

   ```
   外すものは？（番号 / なし）
   昨日の感想・わからなかったこと・質問ある？（なければ なし）
   返事は /send-to-nobu に続けて書いてね（例: /send-to-nobu 3 は外して。感想: ○○）
   ```

   `note_already_sent` があるとき（この会話で感想はもう送った）は、感想は聞かずにこうだけ聞く:

   ```
   外すものは？（番号 / なし）
   返事は /send-to-nobu に続けて書いてね（例: /send-to-nobu 3 は外して）
   ```

## 送信モード

1. 使うのは、**最後に一覧を出したあとの本人の返事だけ**（上の「本人の返事」と、そのあいだの返事）。
   前のラウンドの番号・「なし」・感想は、同じ会話の中にあっても使わない。そこから読み取る:
   - 外す番号（「なし」なら `none`）
   - 「n も送る」と言われた番号（`default_excluded` の会話を送るとき）
   - 感想（本人の言葉のまま。最後の一覧に `note_already_sent` があれば聞かない。本人が新しく書いていれば送る）

   読み取れない・答えが足りないときは、足りない方だけ聞いて「`/send-to-nobu <返事>` で答えてね」と案内して終わる。
2. `mcp__plugin_send-to-nobu_agent-log-inbox__start_submission` を `{"tool": "claude-code"}` で呼ぶ → `upload_code` と `api_base`。
   ツールが見つからない・エラーなら、一覧モード 1 の 1 行を伝えて止まる。`upload_code` は本人に見せない。
3. Bash で実行する（Bash の timeout は 600000）。`<code>` と `<api_base>` は 2 の値をそのまま、`<exclude>` は外す番号をカンマ区切り（なしなら `none`）。
   「n も送る」があれば `--exclude` のあとに `--include <番号>` を足す。
   感想は本人の言葉をそのまま、ヒアドキュメントの中に入れる（言い換え・要約・敬語直し・補足をしない）。
   感想が「なし」なら、`--note-file -` からあと（ヒアドキュメントごと）を付けない。

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --code <code> --exclude <exclude> --api-base <api_base> --data-dir ${CLAUDE_PLUGIN_DATA} --note-file - <<'SEND_TO_NOBU_NOTE'
   （本人の感想をそのまま）
   SEND_TO_NOBU_NOTE
   ```

4. 終了コードで分ける:
   - 0 → 出力の JSON から 1 行で「`sent_count` 件送った・`excluded_count` 件外した」（0 件で感想だけなら「感想を送った」）。
     `remaining` が 1 以上なら「あと `remaining` 件ある。続けるなら /send-to-nobu」と続ける
   - 3（同じ履歴を共有している）→ 中身を平易に伝えて終わる。「それでも送るなら `/send-to-nobu それでも送る`、
     やめるなら `/send-to-nobu 両方外す`」と案内する。次の呼び出しで了承なら、この一覧への同じ答えで 3 に `--confirm-shared` を付ける
   - 4（引換券が使えない・使用済み）→ 2 からもう一度だけやり直す
   - 5（まだ本人の返事が無い）→ 一覧モード 5 の質問をして終わる
   - それ以外 → 何が起きたかを 1 行で。「状態は変えていないので、明日また一覧に出る」と添える

本人が /send-to-nobu を付けずに普通に返事したときも、送信モードの手順で進めてよい（確認のダイアログが出る）。
許可されずに止まったら「`/send-to-nobu <返事>` の形でもう一度答えてね（それなら確認は出ない）」とだけ案内する。

## 禁止

- 会話ログ（`~/.claude/projects` の JSONL）を Read・cat・grep などで直接読む
- 本人の返事を待たずに送る。一覧にない会話を送る。本人が外した会話を送る
- 感想を作文・要約・言い換えする（本人の言葉のまま送る）
