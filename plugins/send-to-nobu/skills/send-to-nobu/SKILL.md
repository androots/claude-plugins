---
name: send-to-nobu
description: 自分の Claude Code の会話ログを のぶろう に送る（毎朝 1 回）。新しい会話で /send-to-nobu と打つだけ。外すものと感想は選択画面で答える
disable-model-invocation: true
model: opus
argument-hint: "[選択画面が使えなかったときだけ: 外す番号と感想]"
allowed-tools:
  - mcp__plugin_send-to-nobu_agent-log-inbox__whoami
  - mcp__plugin_send-to-nobu_agent-log-inbox__start_submission
  - Bash(/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py *)
  - Read(/${CLAUDE_PLUGIN_DATA}/review/**)
  - Read(~/.claude/plugins/data/send-to-nobu-androots/review/**)
---

# 会話ログを のぶろう に送る

本人が打つのは /send-to-nobu の 1 回だけ。決めるのは「外すもの」と「話すこと」で、それは選択画面で答えてもらう。
途中の段取り（コマンド・ツール名・JSON・ファイル）は口に出さない。

本人の返事（/send-to-nobu のあとに書いたもの）:「$ARGUMENTS」

コマンドはすべて、書き換えない・前後にコマンドを足さない・パスをクオートしない。
スクリプトが失敗したら（終了コードが 0 以外）、下に書いた場合を除き、エラーの 1 行を平易に伝えて止まる。
**送り終わるまでターンを終えない**。途中でターンを終えると、このあとのコマンドに許可の確認が出てしまう。
確認係の答えを待つあいだに本人に言うのは「確認中（届いた数/`checkers_total`）」だけ。言うときも、同じメッセージの中で次のツールを呼ぶ（文だけで止まらない）。

## 0. モードを決める

Bash で実行する:

```
/usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py status --data-dir ${CLAUDE_PLUGIN_DATA}
```

- 終了コード 6 →「この会話では使えないので、新しい会話を始めて最初に /send-to-nobu と打ってね」とだけ伝えて止まる
- 上の「本人の返事」があり、`pending` が true → **返事で送る**（選択画面が使えなかったときの逃げ道。下の方）
- それ以外 → **一覧から送る**

## 一覧から送る

1. `mcp__plugin_send-to-nobu_agent-log-inbox__whoami` を呼ぶ。ツールが見つからない・エラーなら未ログインとみなし、
   次の 1 行だけをそのまま伝えて止まる:
   「`/mcp` で agent-log-inbox にログイン（会社の Google アカウント）してから、もう一度 /send-to-nobu と打ってね」
2. Bash で実行する:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py list --data-dir ${CLAUDE_PLUGIN_DATA}
   ```

3. **確認係を呼ぶ**。確認係は同時に 12 体まで。どれをいつ呼ぶかはスクリプトの出力の `launch` だけで決める（自分で数えたり選んだりしない）。
   `launch` の各要素ごとに Agent ツールを 1 回ずつ、**1 つのメッセージでまとめて**呼ぶ:
   - `subagent_type`: `send-to-nobu:checker`
   - `description`: 「確認係」
   - `prompt`: 要素の `prompt` をそのまま（書き換えない・足さない）

   確認係はバックグラウンドで動き、答えはあとから 1 体ずつ届く（届く順はばらばら）。ファイルの中身は自分では読まない。
   呼べなかった確認係（上限などで失敗）は、その要素の `ticket` で `unknown` とみなし、4 で渡す。
   `launch` が空なら、4 の `checked --wait` を呼ぶ（確認係がいない・もう答えがそろっているときは待たずに返る）。
4. **答えを控えに渡す**。確認係の答えが届いたら（1 体ずつでも、いくつかまとめてでもよい）、届いた分を Bash で渡す。
   1 行に 1 体、「札 verdict 分類 / 分類」の形で書く（**波かっこ `{}` と引用符は書かない**。安全チェックに止められるため）:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py checked --data-dir ${CLAUDE_PLUGIN_DATA} <<'SEND_TO_NOBU_RESULTS'
   3f9c2a7be41d ok
   8a01d4c9e2b7 caution third_party / other:社内の噂話
   5c7e19a0b3f4 unknown
   SEND_TO_NOBU_RESULTS
   ```

   - 札は確認係の答えの `ticket`、verdict は `verdict`、分類は `reasons` をそのまま（書き換えない・1 行で）。答えが JSON でない・失敗した確認係は、その確認係の札で `unknown`
   - 今の一覧の札ではない答え（前の一覧の答えが遅れて届いた等）は渡さない。渡すとまとめて止められる（終了コード 2）ので、それを外して渡し直す
   - 出力の `launch` が空でなければ、**すぐに** 3 と同じやり方で全部呼ぶ（答えが返って空いた枠の分。次の波）
   - 出力の `missing`（まだ答えが届いていない確認係）が空でない間は、一覧を出さない。渡す答えが無いときは、答えを書かずに次のコマンドで待つ
     （12 秒ほど待って、今の `missing` と `launch` を返す）。待っているあいだに届いた答えは、次の checked で渡す:

     ```
     /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py checked --wait --data-dir ${CLAUDE_PLUGIN_DATA}
     ```

   - 投げてから 5 分たっても答えの無い確認係は、スクリプトが「確認できなかった」にする（出力に `timed_out`。空いた枠には次の波が出る）。
     一覧全体も 15 分で打ち切る。自分では打ち切らない。時間切れの確認係の答えがあとから届いたら、`missing` が空になる前なら渡す
   - `missing` が空になったら（出力に `display` と `ask` が付く）5 へ
5. **選択画面で聞く**。AskUserQuestion を呼ぶ。`questions` は最後の `checked` の出力の `ask.questions` をそのまま渡す
   （質問・選択肢を書き換えない・足さない・減らさない。`answers` は付けない）。一覧（結論と番号つきのタイトル）は最初の質問の中に
   入っていて選択画面に出るので、本文に書き写さない。
   AskUserQuestion が使えない（ツールが無い・エラー）、または本人が答えずに閉じたときは、`display.text` の行、空行、`display.items` の行を
   そのまま書き、次の 1 行を伝えて終わる（ここだけはターンを終える）:
   「選択画面で答えられなかったので、`/send-to-nobu <返事>` で答えてね（例: /send-to-nobu 3 は外して。感想: ○○）」
6. **送る**。答えが返ったら、`mcp__plugin_send-to-nobu_agent-log-inbox__start_submission` を `{"tool": "claude-code"}` で呼ぶ → `upload_code` と `api_base`。
   ツールが見つからない・エラーなら、1 の 1 行を伝えて止まる。`upload_code` は本人に見せない。
   Bash で実行する（Bash の timeout は 600000）。`<code>` と `<api_base>` は受け取った値をそのまま:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --code <code> --api-base <api_base> --data-dir ${CLAUDE_PLUGIN_DATA}
   ```

   外す番号・送る番号・感想は**渡さない**（スクリプトが選択画面の答えを会話ログから読む）。
   **送る手順で詰まったこと・道具の不具合**（確認係が失敗した・答えが届かなかった、許可ダイアログが出た、終了コードが想定外だった など）に
   気づいたときだけ、次の形で AI の報告を短く（1,000 字まで）添える。会話の中身・タイトル・確認係の理由・外した会話の番号・本人の様子は書かない
   （入っていると止められる。終了コード 2 なら書き直す）:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --code <code> --api-base <api_base> --data-dir ${CLAUDE_PLUGIN_DATA} --note-file - <<'SEND_TO_NOBU_NOTE'
   @@SEND_TO_NOBU_ASSISTANT_NOTE@@
   （AI の報告）
   SEND_TO_NOBU_NOTE
   ```

7. 終了コードで分ける:
   - 0 → 出力の JSON から 1 行で「`sent_count` 件送った・`excluded_count` 件外した」（0 件で感想だけなら「感想を送った」）。
     `deferred_unconfirmed` が 1 以上なら「確認できなかった `deferred_unconfirmed` 件は今回は送っていない（次の /send-to-nobu でもう一度確かめる）」、
     `too_long_count` が 1 以上なら「確認しきれない長さの `too_long_count` 件は今回は送っていない」、
     `unanswered_excluded` が 1 以上なら「未回答だったので、気をつけた方がいい `unanswered_excluded` 件は外した」と添える。それで終わり
   - 3（同じ履歴を共有している）→ 出力の `ask.questions` をそのまま AskUserQuestion で聞き、答えのあと 6 の send をもう一度（同じ `<code>`）
   - 7（答えが読めない。一覧に無い番号など）→ エラーの 1 行を伝え、5 と同じ `ask.questions` で聞き直し、答えのあと send をもう一度（1 回だけ）
   - 4（引換券が使えない・使用済み）→ 6 の start_submission からもう一度だけ
   - 5（答えが見つからない）→ 5 の最後の 1 行を伝えて終わる
   - それ以外 → 何が起きたかを 1 行で。「状態は変えていないので、次の /send-to-nobu でまた出る」と添える

## 返事で送る（選択画面が使えなかったとき）

1. 使うのは、**最後に一覧を出したあとの本人の返事だけ**（上の「本人の返事」と、そのあいだの返事）。そこから読み取る:
   - 外す番号（「なし」なら `none`）
   - 「n も送る」と言われた番号（前に外した会話の続き・確認できなかった会話・確認しきれない長さや量の会話を送るとき）
   - 感想（本人の言葉のまま）

   読み取れない・答えが足りないときは、足りない方だけ聞いて「`/send-to-nobu <返事>` で答えてね」と案内して終わる。
2. `mcp__plugin_send-to-nobu_agent-log-inbox__start_submission` を `{"tool": "claude-code"}` で呼ぶ（一覧から送る 6 と同じ）。
3. Bash で実行する（Bash の timeout は 600000）。`<exclude>` は外す番号をカンマ区切り（なしなら `none`）。
   「n も送る」があれば `--exclude` のあとに `--include <番号>` を足す。感想は本人の言葉だけを、そのままヒアドキュメントの中に入れる
   （言い換え・要約・敬語直し・補足をしない）。AI の報告は一覧から送る 6 と同じく、区切りの行の下に。感想も報告も無いなら `--note-file -` からあとを付けない:

   ```
   /usr/bin/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentlog.py send --code <code> --exclude <exclude> --api-base <api_base> --data-dir ${CLAUDE_PLUGIN_DATA} --note-file - <<'SEND_TO_NOBU_NOTE'
   （本人の感想をそのまま）
   @@SEND_TO_NOBU_ASSISTANT_NOTE@@
   （AI の報告。あるときだけ。無ければ区切りの行ごと書かない）
   SEND_TO_NOBU_NOTE
   ```

4. 終了コードは一覧から送る 7 と同じ。ただし 3（同じ履歴を共有している）は中身を平易に伝えて、
   「それでも送るなら `/send-to-nobu それでも送る`、やめるなら `/send-to-nobu 両方外す`」と案内して終わる。
   次の呼び出しで了承なら、同じ答えで 3 に `--confirm-shared` を付ける。5 は一覧を見せ直して、質問だけして終わる

## 禁止

- 会話ログ（`~/.claude/projects` の JSONL）や確認用ファイルを自分で Read・cat・grep などで読む（読むのは確認係だけ）
- 本人の答えを待たずに送る。選択画面の質問を書き換える・自分で答えを入れる
- 感想を作文・要約・言い換えする・AI の文を混ぜる（感想は本人の言葉だけ。AI の報告は区切りの行の下だけ）
