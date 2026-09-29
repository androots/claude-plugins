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
4. **確認係を呼ぶ**。確認係は同時に 12 体まで。どれをいつ呼ぶかはスクリプトの出力の `launch` だけで決める（自分で数えたり選んだりしない）。
   `launch` の各要素ごとに Agent ツールを 1 回ずつ、**1 つのメッセージでまとめて**呼ぶ:
   - `subagent_type`: `send-to-nobu:checker`
   - `description`: 「確認係」
   - `prompt`: 要素の `prompt` をそのまま（書き換えない・足さない）

   確認係はバックグラウンドで動き、答えはあとから 1 体ずつ届く（届く順はばらばら）。ファイルの中身は自分では読まない。
   呼べなかった確認係（上限などで失敗）は、その要素の `ticket` で `unknown` とみなし、5 で渡す。
   `launch` が空（`checkers_total` が 0）なら、次の 5 は何も書かずに 1 回だけ行う。
5. **答えを控えに渡す**。確認係の答えが届いたら（1 体ずつでも、いくつかまとめてでもよい）、届いた分を Bash で渡す。
   1 行に 1 体、「札 verdict 分類 / 分類」の形で書く（**波かっこ `{}` と引用符は書かない**。安全チェックに止められるため）:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py checked --data-dir ${CLAUDE_PLUGIN_DATA} <<'SEND_TO_NOBU_RESULTS'
   3f9c2a7be41d ok
   8a01d4c9e2b7 caution third_party / other:社内の噂話
   5c7e19a0b3f4 unknown
   SEND_TO_NOBU_RESULTS
   ```

   - 札は確認係の答えの `ticket`、verdict は `verdict`、分類は `reasons` をそのまま（書き換えない・1 行で）。答えが JSON でない・失敗した確認係は、その確認係の札で `unknown`
   - 今のラウンドの札ではない答え（前のラウンドの答えが遅れて届いた等）は渡さない。渡すとまとめて止められる（終了コード 2）ので、それを外して渡し直す
   - 出力の `launch` が空でなければ、**すぐに** 4 と同じやり方で全部呼ぶ（答えが返って空いた枠の分。次の波）
   - 出力の `missing`（まだ答えが届いていない確認係）が空でない間は、**一覧を出さない**（波の間も出さない）。本人には「確認中（届いた数/`checkers_total`）」とだけ言い、残りの答えを待つ
   - `missing` が空になったら 6 へ。まとめはスクリプトがやる（会話ごとの結果・分類の重複まとめ・表示の言葉）
6. 最後の `checked` の出力の `display.text` の行を、**そのまま**先頭に出す（並べ替え・言い換え・まとめ直し・足し引きをしない）。
   続けて、一覧の出力の `items` を `n` の順に全件（省略しない）、`n. タイトル` の形で並べる（日付・サイズ・会話 ID は出さない）:

   ```
   （display.text の行をそのまま）

   1. タイトル
   2. タイトル（前に外した会話の続き。今回も外す。送るなら「2 も送る」）
   …

   外すものは？（番号 / なし）
   昨日の感想・質問は？（なければ なし）
   → /send-to-nobu に続けて書いてね
   ```

   - タイトルの後ろに短く添える: `previously_excluded`・`contains_excluded_copy`（`default_excluded`）→「前に外した会話の続き。今回も外す。送るなら『n も送る』」、
     `shares_history_with: [m]` か同じ `history_group` →「m と同じ履歴」、`group_cut` →「同じ履歴の会話が多すぎて一部だけ表示」
   - `first_run` なら先頭の行の前に「初回なので `since` 以降の分」
   - `remaining` が 1 以上なら一覧の下に「このあとに `remaining` 件ある（送ったあと /send-to-nobu で続き）」
   - `too_long_count` が 1 以上なら一覧の下に「確認しきれない長さの会話がほかに `too_long_count` 件ある（この一覧には出ていない）」
   - `note_already_sent` があれば（この会話で感想はもう送った）感想の行は出さず、「→ /send-to-nobu に続けて書いてね（例: /send-to-nobu 3 は外して）」

## 送信モード

1. 使うのは、**最後に一覧を出したあとの本人の返事だけ**（上の「本人の返事」と、そのあいだの返事）。
   前のラウンドの番号・「なし」・感想は、同じ会話の中にあっても使わない。そこから読み取る:
   - 外す番号（「なし」なら `none`）
   - 「n も送る」と言われた番号（`default_excluded` の会話・確認できなかった会話・確認しきれない長さの会話を送るとき）
   - 感想（本人の言葉のまま。最後の一覧に `note_already_sent` があれば聞かない。本人が新しく書いていれば送る）

   読み取れない・答えが足りないときは、足りない方だけ聞いて「`/send-to-nobu <返事>` で答えてね」と案内して終わる。
2. `mcp__plugin_send-to-nobu_agent-log-inbox__start_submission` を `{"tool": "claude-code"}` で呼ぶ → `upload_code` と `api_base`。
   ツールが見つからない・エラーなら、一覧モード 1 の 1 行を伝えて止まる。`upload_code` は本人に見せない。
3. Bash で実行する（Bash の timeout は 600000）。`<code>` と `<api_base>` は 2 の値をそのまま、`<exclude>` は外す番号をカンマ区切り（なしなら `none`）。
   「n も送る」があれば `--exclude` のあとに `--include <番号>` を足す。
   感想は本人の言葉だけを、そのままヒアドキュメントの中に入れる（言い換え・要約・敬語直し・補足をしない。AI の文を書き足さない）。
   **送る手順で詰まったこと・道具の不具合**（確認係が失敗した・答えが届かなかった、許可ダイアログが出た、終了コードが想定外だった など）に気づいたときだけ、
   感想のあとに区切りの行 `@@SEND_TO_NOBU_ASSISTANT_NOTE@@` を 1 行置き、その下に AI の報告を短く（1,000 字まで）書く。
   会話の中身・タイトル・確認係の理由・外した会話の番号・本人の様子は書かない（入っていると止められる。終了コード 2 なら書き直す）。
   本人の感想が「なし」でも報告があるときは、区切りの行から書く。感想も報告も無いなら、`--note-file -` からあと（ヒアドキュメントごと）を付けない。

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --code <code> --exclude <exclude> --api-base <api_base> --data-dir ${CLAUDE_PLUGIN_DATA} --note-file - <<'SEND_TO_NOBU_NOTE'
   （本人の感想をそのまま）
   @@SEND_TO_NOBU_ASSISTANT_NOTE@@
   （AI の報告。あるときだけ。無ければ区切りの行ごと書かない）
   SEND_TO_NOBU_NOTE
   ```

4. 終了コードで分ける:
   - 0 → 出力の JSON から 1 行で「`sent_count` 件送った・`excluded_count` 件外した」（0 件で感想だけなら「感想を送った」）。
     `deferred_unconfirmed` が 1 以上なら「確認できなかった `deferred_unconfirmed` 件は今回は送らず、次の一覧でもう一度確かめる」と添える。
     `remaining` が 1 以上なら「あと `remaining` 件ある。続けるなら /send-to-nobu」と続ける。
     `too_long_count` が 1 以上なら「確認しきれない長さの会話が `too_long_count` 件ある（送るなら、次の一覧でその番号を『n も送る』）」とだけ添える（番号は次の一覧で変わるので、ここでは書かない）
     （`remaining` が 0 なら続きには誘わない。確認しきれない長さの会話は、続けても確認係にかけられない）
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
- 感想を作文・要約・言い換えする・AI の文を混ぜる（感想は本人の言葉だけ。AI の報告は区切りの行の下だけ）
