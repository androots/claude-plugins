# androots の Claude Code プラグイン

## send-to-nobu

毎朝「今朝のレポート」ができる。前の日の Claude Code の使い方に、AI が「アドバイス 3 つ・よかったこと・やったこと」を付けたページ。自分の Mac の中で作り、送らないものを決めて「送る」を押すと、レポートだけが のぶろう に届く。会話そのものは送らない。

### 入れ方（1 回だけ）

Claude Code で順に打つ。

1. `/plugin marketplace add androots/claude-plugins`
2. `/plugin install send-to-nobu@androots`
3. `/reload-plugins`
4. `/mcp` → `agent-log-inbox` の「連携させる」→ 会社の Google アカウントでログイン（ブラウザが `localhost` の HTTP ERROR 431 になったら、そのアドレスをシークレットウィンドウに貼って開く）
5. `/plugin` → Marketplaces → `androots` → 自動更新を ON（最初は OFF）

### 毎朝のルーティン（Claude の Desktop アプリ）

Desktop アプリの Code タブで、予定タスク（ルーティン）を 1 つ作る。

- 名前: `send-to-nobu`（この名前で始まっていないと動かない）
- 毎日 7:00
- 指示: `send-to-nobu スキルで今朝のレポートを作る`

朝になると、サイドバーに send-to-nobu の会話ができている。開くと横にページが出る（作るのに数分かかる）。

1. アドバイス・よかったこと・やったことを見る
2. 送りたくない会話は「送らない」に、見せたくないスクショは外す
3. 「送る」を押す → 確認が出たら送る
4. 受け取り票に変わったら終わり

確認が出ない画面では「この会話に『送って』と打つ」と案内が出る。そのとおりに打てば送る。

### ルーティンを使わないとき

新しい会話で `/send-to-nobu` と打つと、同じページができる。

その日最初に Claude Code を開いたとき、今朝のレポートをまだ送っていなければ「今朝のレポートがまだ」と 1 行出る。

### 何が届くか

- 届くのは、ページで見えているレポートだけ。ページと送るものは 1 対 1 で同じ
  - アドバイス（どの会話の話か・昨日 → 次は・貼って使えるプロンプト・自分の発言の短い引用）
  - よかったこと、やったこと（会話ごとの題・流れ・時刻・モデル・AI のメモ）
  - 数字（昨日の費用・今月の累計・1 日平均・会話の数・主なモデル）
  - アーティファクトのスクショ（社名や数字など見せない方がいい所はぼかしてある）
- 会話そのもの（自分の発言の全文・AI の返事・ツールの結果・ファイルの中身）は届かない。自分の発言は、アドバイスの中の短い引用（80 字まで・原文にある文字だけ）だけ
- 私的な話・愚痴・人事・健康や家族・個人のお金・他人の個人情報・仕事以外の会話は、作る前に自分の Mac の中で外す。外した会話はレポートに入らない（何本外したかだけ）
- 「送らない」にした会話は、その会話のアドバイス・よかったこと・スクショごと届かない。外したスクショも届かない
- API キー・トークン・秘密鍵は `[REDACTED:…]` に伏せる
- 外す判定とアドバイスは、自分の Claude（ふだんの Claude Code と同じログイン）が自分の Mac の中で作る。そのために会話が のぶろう や受け口のサーバーに届くことはない。判定とアドバイスの会話は履歴に残らない
- ページは自分の claude.ai のアーティファクト（自分しか開けない）。見るのは のぶろう だけ。届いたレポートは 400 日で消える

### やめ方

`/plugin uninstall send-to-nobu@androots`

### 動く環境

Mac。macOS の `/usr/bin/python3`（コマンドライン デベロッパツール）を使う。入っていなければ、初回に macOS がインストールを案内する。スクショは Google Chrome が入っていれば撮る（無ければスクショなしのレポートになる）。

---

## 開発メモ（androots 内向け）

- 設計の正は ai-iinkai の `_spec/maiasa/ARCH.md`（0.8「毎朝のレポート」）。契約（名前・形・置き場所）を変えるときはサーバー・キットと同時に
- 作り: スキル（入口だけ）＋ `scripts/agentlog.py`（`nudge` / `prepare` / `send`）＋ フック（SessionStart の startup → `nudge`）。次に何をするかはスクリプトの出力の `next` が AI に教える。スキルに手順の分岐を足さない
- 中身はキットで配る: `GET /v1/kit` で rubric.md・examples.md・catalog.json・schema.json・prices.json・page.html を取り、データディレクトリの `kit/kit.json` に写しを置く（`If-None-Match` で 304）。取れない朝は写しで動く。ページのひな形はプラグインに持たない（テスト用の最小のひな形だけ `tests/kit/`）。プラグインの版上げは動かし方を変えるときだけ
- `prepare` の流れ: キット → 前回送った `sent_until` から今まで（初回は 24 時間前から）の会話を集める（送信用の会話・自動実行・レポートを読んだ会話・いまの会話は除く）→ 外す判定 → 費用 → アーティファクトの見える文字 → レビュー役 → 撮ってぼかす → ページ（キットの page.html の `__REPORT__` に mode=person のレポートと画像の data URI）→ 控え（`pending.json`）。全体で 560 秒まで（スキルの Bash は 600 秒）
- 読む AI とツールを持つ AI を分ける。中身を読むのは `prepare` の中から呼ぶツールなしの `claude -p` だけ。空の一時フォルダで `--tools ""`・`--setting-sources ""`・`--strict-mcp-config`・`--safe-mode`・`--no-session-persistence` を付ける（付けないと本人の CLAUDE.md・フック・MCP が入り、入力が十数万トークン増えて判断も汚れる）
  - 外す判定: Sonnet。理由は 7 つ＋「仕事以外」。線はプラグインの中（サーバーから変えられない）。確かめられなかった会話は残し、本人がページで決める
  - レビュー役: Opus（`claude-opus-5-5`）。system = rubric.md ＋ examples.md ＋ 型の一覧、出力 = schema.json。入力は `_spec/maiasa/test/day1.txt` の書き方の要約（会話 c1… を時刻順・本人の発言・AI の返事の頭・道具・compact・貼った画像・アーティファクトの見える文字の頭）。大きい日は会話ごとに頭と尻を残して詰める。失敗したら 1 回だけやり直し、だめならアドバイスなしのページ（スクショも撮らない）
- 並列に立てた会話（同じ題か同じ最初の指示で、直前の 1 本から 10 分以内なら鎖のようにつなぐ。最初の発言の origin が人でない会話どうしは 30 分以内。1 つのまとまりは最初の 1 本から 3 時間まで）は 1 行にまとめ、`convs[].group.n` に本数を入れる。題・流れ・メモは代表（中身の一番多い 1 本）から、要約も代表の 1 本だけを渡す
- ぼかしの札: レビュー役の `hide` が `{text, label}` なら label を `shots[name].hidden` に入れる（text は送らない）。文字列だけの形（キット v1）は「伏せた文字」
- 時間: 外す判定とレビュー役の合計で 8 分まで、prepare 全体で 560 秒まで。レビュー役に 240 秒残らなければ、残り時間に合わせて要約を詰める
- 引用（quotes）は AI の出力をそのまま使わない。本人の原文（伏せたもの）と照合し、原文にある部分だけを 80 字に切る
- 費用: assistant 行の `message.usage` × キットの prices.json（$/MTok）。単価は models のキーで一番長く前方一致するもの → 無ければ fallback。`zero` のモデルは 0、`multipliers.inference_geo_<地域>` は `usage.inference_geo` で掛ける。応答 id で 1 つにする（1 応答が複数行に書かれる）。サブエージェント・送信用の会話も含む。主なモデル = 範囲の中で費用が一番多いもの
- スクショ: headless Chrome（`--headless=new`・`--use-mock-keychain`・一時プロファイル・1280×800）で、元のフォルダを `<base>` にし、レビュー役の `hide` の文字列を含む要素に blur と札を付ける JS を差し込んで撮る → `sips` で JPEG（幅 1100・品質 70）。Chrome か sips が無ければ撮らない
- 本人の答えはページが db の `send/decision`（`{round, exclude, hide_shots, at}`）に書く。AI は ArtifactData の get に `out_dir`（スクラッチパッド）を付けてファイルに保存し、`send --answer-dir` に渡す。`send` は答えを除いたレポートと画像を `POST /v1/report` に送り、`sent_until` を進め、`result.json`（`{round, at, report_id, sent, excluded, hidden_shots, image_count}`）を書く。ページの送る印は `data.round`（prepare が必ず入れる）。AI がそれを `send/result` に set してページが受け取り票に変わる
- 守ること（テストで保証。`tests/test_guarantees.py`）
  1. 外した会話はレポートに入らない
  2. 送るものはページで見えるものと同じ
  3. 会話の本文は送らない
  4. 秘密は伏せる
  5. 引用は原文にある文字だけ
  6. キットが取れなくても写しで動く
  7. 送信用の会話（`/send-to-nobu` か send-to-nobu のルーティン）でしか動かない
  8. 送らない・スクショを外すが反映される（答えが無い・前の回・レポートに無いものを指す答えでは送らない）
  9. 費用の計算
- データディレクトリ: `state.json`（`sent_until`・`sent_day`）・`pending.json`（今のページの控え）・`kit/kit.json`（キットの写し）・`page/`（最新のページ）・`shots/`（送るまでのスクショ）・`nudged.json`・`last-prepare.json`（件数・秒数・AI の費用だけ。中身は書かない）
- サーバーとの取り決めは ARCH.md の 4 章（契約 v3）。v2 の `/v1/uploads`・`/v1/finish` はもう使わない
- テスト: `cd plugins/send-to-nobu && /usr/bin/python3 -m unittest discover -s tests`（3.9・標準ライブラリだけ）。一時の設定ディレクトリと合成の会話ログだけで走り、読む projects が一時ディレクトリでなければ止まる。claude・Chrome・sips は偽物（`tests/fake_*.py`）、受け口は v3 の偽物、キットは `tests/kit/` のダミー。本物のキット（ai-iinkai の `send-to-nobu/dist/publish/v1/`）があれば `tests/test_real_kit.py` がそれでも組んで、Chrome で描けることを確かめる（場所は env の `SEND_TO_NOBU_REAL_KIT`）
- 形式: `claude plugin validate --strict .` と `claude plugin validate --strict plugins/send-to-nobu`
- 利用者に更新を届けるには `plugins/send-to-nobu/.claude-plugin/plugin.json` の `version` を上げる
