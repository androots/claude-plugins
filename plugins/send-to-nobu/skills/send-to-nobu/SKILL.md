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
  - Read(/${CLAUDE_PLUGIN_DATA}/review/**)
  - Read(~/.claude/plugins/data/send-to-nobu-androots/review/**)
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
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py list --data-dir ${CLAUDE_PLUGIN_DATA}
   ```

3. `count` が 0 なら「送る会話はないよ。感想や質問があれば `/send-to-nobu 感想: ○○` で送れる」と言って終わる。
4. **確認係を呼ぶ**。`items` の各会話の `review` の各まとまり（ファイル名の配列）ごとに、Agent ツールを 1 回ずつ、
   **1 つのメッセージで全部まとめて**呼ぶ（並列。バックグラウンドにしない。全部の結果がそろうまで待つ）:
   - `subagent_type`: `send-to-nobu:checker`
   - `prompt`: 「次のファイルを全部読んで判定して:」＋ `review_dir` とファイル名をつないだ絶対パスを 1 行に 1 つ

   ファイルの中身は自分では読まない。確認係の答えは JSON（`verdict` と `reasons`）だけを使う。
5. 会話ごとにまとめる（会話のまとまりが複数あるときは、全部の答えを合わせる）:
   - 確認できなかった: どれか 1 つでも答えが無い・失敗・JSON でない・`unknown`、または `review_error` がある。黙って問題なしにしない
   - 気をつけた方がいい: 確認できていて、どれかが `caution`、または `detect` に `card`・`secret` がある
     （card →「ツールの結果にカード番号らしきもの」、secret →「ツールの結果にキー・トークン類（送るときは伏せる）」）
   - ほかは、会話の本文に気になる点なし
   - `detect` の `email`・`phone` は判定に入れず、下の 1 行で番号だけ知らせる

   そのあと Bash で、確認できた会話の番号を控えに書く（確認できなかった会話はどちらにも書かない。今回は送らず、次の一覧でもう一度確かめる）:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py checked --ok <番号> --caution <番号> --data-dir ${CLAUDE_PLUGIN_DATA}
   ```

   `<番号>` はカンマ区切り。無ければ `none`。
6. 結論から短く見せる（日付・サイズ・会話 ID は出さない。**JSON の `n` のまま、全件を省略せず、並べ替えずに**）:

   ```
   15 件のうち、気をつけた方がいいのは 3 件、確認できなかったのは 1 件。
   ・4, 7：クライアント案件の名前とデータ構成
   ・11：アカウント ID
   ・9：確認できなかった（今回は送らず、次の一覧でもう一度確かめる。今回送るなら「9 も送る」）
   ほかの 12 件は、会話の本文に気になる点なし（ツールの結果は機械の検出だけ）。
   （ツールの結果に メールアドレスらしきもの: 3, 8 ／ 電話番号らしきもの: 8）

   1. タイトル
   2. タイトル（前に外した会話の続き。今回も外す。送るなら「2 も送る」）
   …

   外すものは？（番号 / なし）
   昨日の感想・質問は？（なければ なし）
   → /send-to-nobu に続けて書いてね
   ```

   - 同じ理由の会話は番号をまとめる。理由は確認係の言葉を短く。中身の具体的な値は書かない
   - 確認できなかった会話が無ければ 1 行目の「、確認できなかったのは N 件」は書かない
   - 気をつけるもの・確認できなかったものが無ければ、1 行目を「15 件、会話の本文に気になる点は見当たらなかった（ツールの結果は機械の検出だけ）。」にする
   - （ツールの結果に …）の行は `email`・`phone` がある会話があるときだけ出す
   - 行の後ろに短く添える: `previously_excluded`・`contains_excluded_copy`（`default_excluded`）→「前に外した会話の続き。今回も外す。送るなら『n も送る』」、
     `shares_history_with: [m]` か同じ `history_group` →「m と同じ履歴」、`group_cut` →「同じ履歴の会話が多すぎて一部だけ表示」
   - `first_run` なら 1 行目の前に「初回なので `since` 以降の分」
   - `remaining` が 1 以上なら一覧の下に「このあとに `remaining` 件ある（送ったあと /send-to-nobu で続き）」
   - `note_already_sent` があれば（この会話で感想はもう送った）感想の行は出さず、「→ /send-to-nobu に続けて書いてね（例: /send-to-nobu 3 は外して）」

## 送信モード

1. 使うのは、**最後に一覧を出したあとの本人の返事だけ**（上の「本人の返事」と、そのあいだの返事）。
   前のラウンドの番号・「なし」・感想は、同じ会話の中にあっても使わない。そこから読み取る:
   - 外す番号（「なし」なら `none`）
   - 「n も送る」と言われた番号（`default_excluded` の会話・確認できなかった会話を送るとき）
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
     `deferred_unconfirmed` が 1 以上なら「確認できなかった `deferred_unconfirmed` 件は今回は送らず、次の一覧でもう一度確かめる」と添える。
     `remaining` が 1 以上なら「あと `remaining` 件ある。続けるなら /send-to-nobu」と続ける
   - 3（同じ履歴を共有している）→ 中身を平易に伝えて終わる。「それでも送るなら `/send-to-nobu それでも送る`、
     やめるなら `/send-to-nobu 両方外す`」と案内する。次の呼び出しで了承なら、この一覧への同じ答えで 3 に `--confirm-shared` を付ける
   - 4（引換券が使えない・使用済み）→ 2 からもう一度だけやり直す
   - 5（まだ本人の返事が無い）→ 一覧を見せ直して、質問だけして終わる
   - それ以外 → 何が起きたかを 1 行で。「状態は変えていないので、明日また一覧に出る」と添える

本人が /send-to-nobu を付けずに普通に返事したときも、送信モードの手順で進めてよい（確認のダイアログが出る）。
許可されずに止まったら「`/send-to-nobu <返事>` の形でもう一度答えてね（それなら確認は出ない）」とだけ案内する。

## 禁止

- 会話ログ（`~/.claude/projects` の JSONL）や確認用ファイルを自分で Read・cat・grep などで読む（読むのは確認係だけ）
- 本人の返事を待たずに送る。一覧にない会話を送る。本人が外した会話を送る
- 感想を作文・要約・言い換えする（本人の言葉のまま送る）
