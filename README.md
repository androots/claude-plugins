# androots の Claude Code プラグイン

## send-to-nobu

毎朝 `/send-to-nobu` と打つと、昨日までの Claude Code の会話が一覧で出る。送りたくない会話を外して、感想・質問を書くと、残りが のぶろう に届く。

### 入れ方（1 回だけ）

Claude Code で順に打つ。

1. `/plugin marketplace add androots/claude-plugins`
2. `/plugin install send-to-nobu@androots`
3. `/reload-plugins`
4. `/mcp` → `agent-log-inbox` を選んでログイン（会社の Google アカウント）
5. `/plugin` → Marketplaces → `androots` → 自動更新を ON（最初は OFF）

### 毎朝

1. **新しい会話**で `/send-to-nobu`
2. 選択画面で 2 つ答える
   - 送らない会話 … 無ければ「なし（全部送る）」。外すなら入力欄に番号（例: `3, 5-7`）
   - 昨日使ってみてどうだった？ … わからなかったこと・質問もここに
3. 「21 件送った・3 件外した」と出たら終わり

その日最初に Claude Code を開くと「未送信の会話が N 件」と 1 行出る。

### 何が届くか

- 外さなかった会話まるごと（ツールの結果・サブエージェントの会話も）。一覧を出した時点までの中身だけ
- API キー・トークン・秘密鍵は `[REDACTED:…]` に伏せてから送る。画像・PDF は中身を抜いて「ここにあった」という印だけ
- 外した会話はタイトルも中身も届かない（何件外したかだけ）。外した会話は、続きを書いても一覧に出ない
- 感想・質問は、書いた言葉のまま
- タイトルの横の「連絡先あり」「カード番号あり」は、ツールの結果にメールアドレス・電話番号・カード番号らしきものがあった印。送るかを決める目安に
- 見るのは のぶろう だけ。最後に送ってから 90 日で消える

### やめ方

`/plugin uninstall send-to-nobu@androots`

### 動く環境

Mac。macOS の `/usr/bin/python3`（コマンドライン デベロッパツール）を使う。入っていなければ、初回に macOS がインストールを案内する。

---

## 開発メモ（androots 内向け）

- 作り: スキル（入口だけ）＋ `scripts/agentlog.py`（`nudge` / `list` / `send`）＋ フック（SessionStart の startup → `nudge`）。次に何をするかはスクリプトの出力の `next` が AI に教える。スキルに手順の分岐を足さない
- 本人の答えは、スクリプトが会話ログ（AskUserQuestion の `toolUseResult`）から直接読む。AI に番号や感想を中継させない
- 守ること（テストで保証。`tests/test_guarantees.py`）
  1. 外した会話は送らない。送るのは一覧で見せた会話だけ、一覧を出した時点までの中身だけ
  2. 本人の答えなしに送らない（離席で閉じた・空・AI が答えを入れた・一覧より前の答えは数えない。外す質問は明示の答えが要る）
  3. 送る前にキー類を伏せ、画像・PDF の base64 を抜く
  4. 感想は本人の言葉だけ。AI のメモは `assistant_note` に
  5. 送信用の会話と一覧の出力を含む会話は一覧に出さない。一覧は送信用の会話の中でしか出さない
  6. 前に外した会話は、続きが書かれても外したまま
- データディレクトリ: `state.json`（送った・外した会話とその時の位置。0.4 までと同じ形）・`pending.json`（今の一覧の控え）・`nudged.json`
- サーバーとの取り決め（MCP ツール・HTTP API・保存先）は ai-iinkai の `mcp-hub/README.md`「agent-log-inbox」節の「契約（v2）」が正。変えるときはサーバーと同時に
- テスト: `cd plugins/send-to-nobu && /usr/bin/python3 -m unittest`（3.9・標準ライブラリだけ）。一時の設定ディレクトリと合成の会話ログだけで走り、読む projects が一時ディレクトリでなければ止まる
- 対話の通し: `/usr/bin/python3 dev/e2e_tty.py`。本物の `claude` を疑似端末で動かし、キー入力で選択画面に答える（モデル・MCP・受け口は台本どおりの偽物、設定ディレクトリと HOME は一時）。許可ダイアログが出ないこと・離席で送らないことを見る。モデルの言うことの聞き具合は見ない（それは本番の通しで）
- 形式: `claude plugin validate --strict .` と `claude plugin validate --strict plugins/send-to-nobu`
- 利用者に更新を届けるには `plugins/send-to-nobu/.claude-plugin/plugin.json` の `version` を上げる
