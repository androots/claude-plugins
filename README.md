# androots の Claude Code プラグイン

## send-to-nobu

Claude Code の会話ログを のぶろう に送るプラグイン。毎朝 `/send-to-nobu` と打つと、昨日までの会話の一覧が出る。
「外すもの」と「昨日の感想・わからなかったこと・質問」を 1 回答えるだけで送れる。

### 何が送られるか

- 選んだ会話まるごと（Claude とのやりとり・ツールの結果・サブエージェントの会話も含む）
- API キー・トークン・秘密鍵のような文字列は、送る前に `[REDACTED:…]` に伏せる
- 外した会話は、タイトルも中身も送らない（「何件外したか」だけ）
- 感想・質問は、あなたが書いた言葉のまま

### 誰が見るか・いつ消えるか

- 見るのは のぶろう だけ。公開はしない
- 最後に送ってから 90 日で消える

### 外し方

一覧の番号を答えるだけ（例: `2, 5`）。外した会話は、そのあと続きを書かない限り二度と一覧に出ない。
一覧に「気になる点」や「同じ履歴を含む」と出たら、そこだけ確かめて決めればいい。

### 入れ方（1 回だけ）

Claude Code で順に打つ。

1. `/plugin marketplace add androots/claude-plugins`
2. `/plugin install send-to-nobu@androots`
3. `/reload-plugins`
4. `/mcp` → `agent-log-inbox` を選んでログイン（会社の Google アカウント）
5. `/plugin` → Marketplaces → `androots` → 自動更新を ON（最初は OFF になっている）

初めて送るときに「`send-to-nobu:deliver` を使う？」と聞かれたら、「今後は聞かない」を選ぶ。

### 毎朝の使い方

その日最初に Claude Code を起動すると「未送信の会話が N 件 → /send-to-nobu」と 1 行出る。
新しい会話で `/send-to-nobu` → 一覧を見て 1 回答える → 「3 件送った・1 件外した」で終わり。

### やめ方

`/plugin uninstall send-to-nobu@androots`。送った・外したの記録もいっしょに消える。

### 動く環境

Mac。macOS の `/usr/bin/python3`（コマンドライン デベロッパツール）を使う。入っていなければ、初回の `/send-to-nobu` で macOS がインストールを案内する。
