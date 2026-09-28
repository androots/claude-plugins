# androots の Claude Code プラグイン

## send-to-nobu

Claude Code の会話ログを のぶろう に送るプラグイン。毎朝 `/send-to-nobu` と打つと、昨日までの会話の一覧が出る。
「外すもの」と「昨日の感想・わからなかったこと・質問」を `/send-to-nobu` に続けて 1 回答えるだけで送れる。

### 何が送られるか

- 選んだ会話まるごと（Claude とのやりとり・ツールの結果・サブエージェントの会話も含む）
- 画像・PDF の中身は送らない（スクリーンショットなど。「ここに画像があった」という印だけ残る）
- API キー・トークン・秘密鍵のような文字列は、送る前に `[REDACTED:…]` に伏せる
- 外した会話は、タイトルも中身も送らない（「何件外したか」だけ）
- 感想・質問は、あなたが書いた言葉のまま

### 誰が見るか・いつ消えるか

- 見るのは のぶろう だけ。公開はしない
- 最後に送ってから 90 日で消える

### 外し方

一覧の番号を答えるだけ（例: `/send-to-nobu 2, 5 は外して`）。外した会話は、そのあと続きを書かない限り二度と一覧に出ない。

- 外した会話に続きを書くと、また一覧に出る。そのときは最初から「外す」になっている（送るなら `/send-to-nobu 3 も送る` のように書く）
- 一覧を出すとき、確認係（Claude Sonnet）が会話を最後まで読んで、気をつけた方がいい会話（人の悪口・個人的な相談・お客さまの情報・キー類など）を種類だけ教える。そこだけ確かめて決めればいい
- 一覧を見たあとに書き足した分は、そのときは送らない（あとで /send-to-nobu したときの一覧に出る）

### 入れ方（1 回だけ）

Claude Code で順に打つ。

1. `/plugin marketplace add androots/claude-plugins`
2. `/plugin install send-to-nobu@androots`
3. `/reload-plugins`
4. `/mcp` → `agent-log-inbox` を選んでログイン（会社の Google アカウント）
5. `/plugin` → Marketplaces → `androots` → 自動更新を ON（最初は OFF になっている）

### 毎朝の使い方

その日最初に Claude Code を起動すると「未送信の会話が N 件 → /send-to-nobu」と 1 行出る。

1. **新しい会話**で `/send-to-nobu` → 一覧が出る（ほかの話の途中では使えない）
2. `/send-to-nobu 3 は外して。感想: ○○` のように、`/send-to-nobu` に続けて返事を書く（外すものが無ければ `なし`）
3. 「3 件送った・1 件外した」で終わり。一度に出るのは 15 件まで。「あと N 件ある」と出たら、同じ会話でもう一度 `/send-to-nobu`

### やめ方

`/plugin uninstall send-to-nobu@androots`。送った・外したの記録もいっしょに消える。

### 動く環境

Mac。macOS の `/usr/bin/python3`（コマンドライン デベロッパツール）を使う。入っていなければ、初回の `/send-to-nobu` で macOS がインストールを案内する。

---

## 開発メモ（androots 内向け）

- サーバーとの取り決め（MCP ツール・HTTP API・保存先）は、サーバー側のドキュメントが正。変えるときはサーバーと同時に直す
- テスト: `cd plugins/send-to-nobu && /usr/bin/python3 -m unittest`（macOS の Python 3.9 で通ること。標準ライブラリだけ）
- 形式の確認: `claude plugin validate --strict .` と `claude plugin validate --strict plugins/send-to-nobu`
- 利用者に更新を届けるには `plugins/send-to-nobu/.claude-plugin/plugin.json` の `version` を上げる（上げないと届かない）
