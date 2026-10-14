# androots の Claude Code プラグイン

## send-to-nobu

毎朝「今日のページ」ができる。昨日までの Claude Code の会話が並び、AI が中身を読んで、外した方がよさそうな会話に注意を付けてある。送りたくない会話を「送らない」にして、感想を書いて「これで送る」を押すと、残りが のぶろう に届く。

### 入れ方（1 回だけ）

Claude Code で順に打つ。

1. `/plugin marketplace add androots/claude-plugins`
2. `/plugin install send-to-nobu@androots`
3. `/reload-plugins`
4. `/mcp` → `agent-log-inbox` を選んでログイン（会社の Google アカウント）
5. `/plugin` → Marketplaces → `androots` → 自動更新を ON（最初は OFF）

### 毎朝のルーティン（Claude の Desktop アプリ）

Desktop アプリの Code タブで、予定タスク（ルーティン）を 1 つ作る。

- 名前: `send-to-nobu`（この名前で始まっていないと動かない）
- 毎日 7:00
- 指示: `send-to-nobu スキルで今日のページを作る`

朝になると、サイドバーに send-to-nobu の会話ができている。開くと横にページが出る。

1. 「決めること」に注意の付いた会話が出る（最初から「送らない」）。送ってよければ「送る」に
2. ほかの会話は最初から「送る」。送りたくないものだけオフに
3. 感想・質問があれば書いて「これで送る」→ 確認が出たら送る
4. 受け取り票に変わったら終わり

確認が出ない画面では「この会話に『送って』と打つ」と案内が出る。そのとおりに打てば送る。

### ルーティンを使わないとき

新しい会話で `/send-to-nobu` と打つと、同じページができる。

その日最初に Claude Code を開くと「未送信の会話が N 件」と 1 行出る。

### 何が届くか

- 外さなかった会話まるごと（ツールの結果・サブエージェントの会話も）。一覧を出した時点までの中身だけ
- API キー・トークン・秘密鍵は `[REDACTED:…]` に伏せてから送る。画像・PDF は中身を抜いて「ここにあった」という印だけ
- 「送らない」にした会話はタイトルも中身も届かない（何件外したかだけ）。外した会話は、続きを書いてもページに出ない
- 感想・質問は、書いた言葉のまま
- 注意は、自分の Claude（ふだんの Claude Code と同じログイン）が、自分の打った発言だけを読んで付ける。この確認のために中身が のぶろう や受け口のサーバーに届くことはない。確認の会話は履歴に残らない。確認できなかった日は、その旨がページに出て、タイトルだけで選ぶ\n- ページは自分の claude.ai のアーティファクト（自分しか開けない）。注意の会話には、引っかかった自分の発言が 1 行だけ出る
- 見るのは のぶろう だけ。最後に送ってから 90 日で消える

### やめ方

`/plugin uninstall send-to-nobu@androots`

### 動く環境

Mac。macOS の `/usr/bin/python3`（コマンドライン デベロッパツール）を使う。入っていなければ、初回に macOS がインストールを案内する。

---

## 開発メモ（androots 内向け）

- 作り: スキル（入口だけ）＋ `scripts/agentlog.py`（`nudge` / `page` / `send`）＋ `scripts/page.html`（今日のページのひな形）＋ フック（SessionStart の startup → `nudge`）。次に何をするかはスクリプトの出力の `next` が AI に教える。スキルに手順の分岐を足さない
- ページは AI に書かせない。`page` がひな形の `__STN_DATA__` に JSON を流し込んだ HTML を書き、AI は Artifact で公開するだけ（capabilities `{db, room}`。room を断られたら db だけ）
- 本人の答えはページが db の `send/decision` に書く。AI は ArtifactData の get に `out_dir` を付けてファイルに保存し、`send` がそのファイルを読む。AI に番号や感想を中継させない。送ったら `send` が書く `result.json` を AI が `send/result` に set し、ページが受け取り票に変わる
- 「これで送る」は room の `sendToClaudeSession({deliver: "send"})` で、ページの横の会話（ルーティンの会話）にターンを起こす。使えない画面では「この会話に『送って』と打つ」案内に落ちる。引換券（upload_code）は 60 分で切れるので、送るときに取る
- ルーティン（Desktop の予定タスク）は最初の発言が `<scheduled-task name="…">` になる。名前が `send-to-nobu` で始まるものだけ送信用の会話として扱う
- 外す候補の提案は、読む AI とツールを持つ AI を分ける。中身を読むのは `page` の中から呼ぶツールなしの `claude -p`（`--safe-mode`・`--no-session-persistence`）だけ。線は「その話題が会話の中心のときだけ」。返すのは理由の語（決まった語）・ひとこと（20 字）・引っかかった発言の位置。引用の文は AI の出力ではなく、スクリプトが本人の発言から抜く。サーバー側で読む案は「外した会話はサーバーに届かない」を壊すので採らない。失敗しても提案なしで続ける
- 守ること（テストで保証。`tests/test_guarantees.py`）
  1. 外した会話は送らない。送るのはページに載せた会話だけ、ページを作った時点までの中身だけ
  2. 本人の答えなしに送らない（答えのファイルが無い・別のページの答え・ページに無い番号・別の会話からの send は止める。答えは 1 回だけ使う）
  3. 送る前にキー類を伏せ、画像・PDF の base64 を抜く
  4. 感想は本人の言葉だけ。AI のメモは `assistant_note` に
  5. 送信用の会話とページのデータを含む会話は一覧に出さない。ページは送信用の会話（`/send-to-nobu` か send-to-nobu のルーティン）でしか作らない
  6. 前に外した会話は、続きが書かれても外したまま
- データディレクトリ: `state.json`（送った・外した会話とその時の位置）・`pending.json`（今のページの控え）・`nudged.json`・`page/`（最新のページ・答え・結果）
- サーバーとの取り決め（MCP ツール・HTTP API・保存先）は ai-iinkai の `mcp-hub/README.md`「agent-log-inbox」節の「契約（v2）」が正。変えるときはサーバーと同時に
- テスト: `cd plugins/send-to-nobu && /usr/bin/python3 -m unittest discover -s tests`（3.9・標準ライブラリだけ）。一時の設定ディレクトリと合成の会話ログだけで走り、読む projects が一時ディレクトリでなければ止まる。判定の claude は偽物（`tests/fake_claude.py`、env の `SEND_TO_NOBU_CLAUDE` で差し替え）
- 形式: `claude plugin validate --strict .` と `claude plugin validate --strict plugins/send-to-nobu`
- 利用者に更新を届けるには `plugins/send-to-nobu/.claude-plugin/plugin.json` の `version` を上げる
