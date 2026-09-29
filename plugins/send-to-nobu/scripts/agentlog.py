#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""send-to-nobu: Claude Code の会話ログを のぶろう に送る。

  nudge  SessionStart フック。未送信の会話があれば 1 日 1 回だけ 1 行知らせる
  list   未送信の会話の一覧を選択画面（AskUserQuestion）の質問にして返し、控えに残す
  send   選択画面の答えを会話ログから直接読み、外さなかった会話を一覧の時点の中身で送る

出力は AI が読む JSON 1 行（stdout）。`say` は本人に伝える文、`next` は AI が次にすること。
python3 3.9 の標準ライブラリだけで動く（macOS の /usr/bin/python3）。
"""

import argparse
import concurrent.futures
import datetime
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

FIRST_RUN_DAYS = 7            # 初回は直近 7 日の会話だけ
DAY_START_HOUR = 6            # 朝の知らせの日の区切り（ローカル時刻）
NUDGE_BUDGET = 6.0            # 朝の知らせが会話を読む時間の上限（フックの timeout は 10 秒）
LIST_MAX = 30                 # 一覧に出す会話の数（選択画面に収まる行数。残りは次の一覧で）
TITLE_CHARS = 40              # 一覧に出すタイトルの長さ
TITLE_MAX, PROJECT_MAX = 200, 300
NOTE_MAX = 20000              # サーバーの上限
ASSISTANT_NOTE_MAX = 1000
ANSWER_WAIT = 10.0            # 答えの行が会話ログに書かれるのを待つ上限（秒。Claude Code は少し遅れて書く）
UPLOAD_BATCH = 100
PUT_WORKERS = 4
RETRIES = 3
BACKOFF_BASE = 1.0
PENDING_V = 5
# 一覧の出力と控えに入れる目印。これを含む会話（一覧を読んだ会話）は一覧に出さない
LIST_MARKER = "send_to_nobu_list"
_MARKER_B = LIST_MARKER.encode("ascii")

# 送り先はこれだけ（引数や env では広げられない。テストはコードから差し替える）
ALLOWED_API_BASES = ("https://agent-log-inbox-mcp.androots.co.jp",)
ALLOWED_PUT_PREFIXES = ("https://storage.googleapis.com/",)
CONFIG_DIR = None             # テストだけが差し替える（ふだんは $CLAUDE_CONFIG_DIR か ~/.claude）

NONE_LABEL = "なし（全部送る）"
ALL_LABEL = "全部送らない"
NOTE_QUESTION = "昨日使ってみてどうだった？わからなかったこと・質問も（自由に書くなら入力欄に）"
NOTE_OPTIONS = ("特になし", "順調に使えてる")

END = "say をそのまま本人に伝えて終わる"
NEXT_ASK = ("AskUserQuestion を ask.questions そのままで呼ぶ（書き換えない・answers を付けない・一覧を本文に書き写さない）。"
            "答えが返ったら、このターンのうちに send を実行する")
NEXT_REASK = "say を伝え、AskUserQuestion を ask.questions そのままでもう一度呼ぶ。答えが返ったら send をもう一度（同じ upload_code）"
NEXT_NEW_CODE = "start_submission をもう一度呼び、新しい upload_code で send をもう一度実行する（本人には聞き直さない）"
AGAIN = "もう一度 /send-to-nobu と打ってね"


class Stop(Exception):
    """AI に返して止まる。say は本人に伝える文、extra は出力に足すもの（next など）。"""

    def __init__(self, say, code=1, **extra):
        super().__init__(say or "")
        self.say, self.code, self.extra = say, code, extra


def emit(out, **fields):
    out.write(json.dumps({k: v for k, v in fields.items() if v is not None}, ensure_ascii=False) + "\n")


def _now():
    return time.time()


def _sleep(seconds):
    time.sleep(seconds)


# ---------------------------------------------------------------- 時刻・ファイル・場所


def parse_ts(value):
    """ISO 8601（末尾 Z かオフセット付き）→ epoch 秒。読めなければ None。"""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        base = datetime.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    m = re.match(r"^(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?$", value[19:])
    if not m:
        return None
    ts = base.replace(tzinfo=datetime.timezone.utc).timestamp() + float("0." + (m.group(1) or "0"))
    tz = m.group(2) or "Z"
    if tz not in ("Z", "z"):
        offset = (int(tz[1:3]) * 60 + int(tz[-2:])) * 60
        ts -= offset if tz[0] == "+" else -offset
    return ts


def iso_utc(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_key(now):
    """朝 6 時を日の区切りにした日付（ローカル時刻）。"""
    return (datetime.datetime.fromtimestamp(now) - datetime.timedelta(hours=DAY_START_HOUR)).date().isoformat()


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    """一時ファイル → rename で原子的に書く。"""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        remove_quietly(tmp)
        raise


def remove_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def open_nofollow(path):
    """シンボリックリンクを追わずに、ふつうのファイルだけを開く。"""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def config_dir():
    return CONFIG_DIR or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def projects_dir():
    return os.path.join(config_dir(), "projects")


def plugin_version():
    d = read_json(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               ".claude-plugin", "plugin.json"), {})
    v = d.get("version") if isinstance(d, dict) else None
    return v if isinstance(v, str) else "0.0.0"


def resolve_data_dir(arg):
    """--data-dir（スキルとフックが ${CLAUDE_PLUGIN_DATA} を渡す）。env の CLAUDE_PLUGIN_DATA は読まない
    （Bash の env には他プラグインの値が漏れていることがある）。<設定ディレクトリ>/plugins/data/ 直下だけ使う。"""
    path = os.path.abspath(os.path.expanduser((arg or "").strip())) if (arg or "").strip() else ""
    root = os.path.realpath(os.path.join(config_dir(), "plugins", "data"))
    if (not path or "send-to-nobu" not in os.path.basename(path)
            or os.path.dirname(os.path.realpath(path)) != root):
        raise Stop("うまく動かなかった（データの置き場所が違う）。のぶろう に知らせて")
    return path


def current_session():
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        raise Stop("うまく動かなかった（いまの会話がわからない）。のぶろう に知らせて")
    return sid


# ---------------------------------------------------------------- 秘密を伏せる

_PEM_HEAD = r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
_PEM_TAIL = r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
_PEM_BODY_FULL = r"(?:[A-Za-z0-9+/= \t\r\n:,._-]|\\{1,3}[rnt])*?"    # END まである形（エスケープされた改行も）
_PEM_BODY_CUT = r"(?:[A-Za-z0-9+/=\r\n]|\\{1,3}[rn])*"               # END の無い途中切れ

# (種類, その文字列を含む行だけ調べる目印, 正規表現, 残すグループ番号, 左の境界を見るか)。上から順にかける
SECRET_PATTERNS = [
    ("private_key", ("PRIVATE KEY",),
     _PEM_HEAD + "(?:" + _PEM_BODY_FULL + _PEM_TAIL + "|" + _PEM_BODY_CUT + ")", 0, False),
    ("anthropic_key", ("sk-ant-",), r"sk-ant-[A-Za-z0-9_-]{20,}", 0, True),
    ("github_token", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
     r"g(?:h[pousr]_[A-Za-z0-9]{30,}|ithub_pat_[A-Za-z0-9_]{30,})", 0, True),
    ("slack_token", ("xox",), r"xox[abprs]-[A-Za-z0-9-]{10,}", 0, True),
    ("aws_access_key", ("AKIA", "ASIA"), r"A(?:KIA|SIA)[0-9A-Z]{16}(?![0-9A-Za-z])", 0, True),
    # Google の API キーは 39 文字固定。右にも境界を取って、base64 の中の偶然の一致を拾わない
    ("google_api_key", ("AIza",), r"AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_+/=-])", 0, True),
    # sk- と Bearer は「数字を 1 つ以上含む」で文章の誤検知を防ぐ
    ("openai_key", ("sk-",), r"sk-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}", 0, True),
    ("bearer_token", ("earer",), r"([Bb]earer[ \t]+)(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{20,}", 1, True),
]
_COMPILED = [(kind, tuple(n.encode("ascii") for n in needles), needles, re.compile(p.encode("ascii")), re.compile(p),
              keep, bounded) for kind, needles, p, keep, bounded in SECRET_PATTERNS]
_SCREEN = "|".join(re.escape(n) for _, needles, _, _, _ in SECRET_PATTERNS for n in needles)
_SCREEN_B, _SCREEN_S = re.compile(_SCREEN.encode("ascii")), re.compile(_SCREEN)
_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_ALNUM_S, _ALNUM_B = frozenset(_ALNUM), frozenset(_ALNUM.encode("ascii"))
_ESC_S, _ESC_B = frozenset("nrtbf"), frozenset(b"nrtbf")


def _bounded(text, start, is_bytes):
    """左の境界: 直前が英数字でない。JSON 文字列の中の改行は `\\n` と書かれるので `\\nsk-ant-…` も通す。"""
    if start == 0:
        return True
    prev = text[start - 1]
    if prev not in (_ALNUM_B if is_bytes else _ALNUM_S):
        return True
    return start >= 2 and prev in (_ESC_B if is_bytes else _ESC_S) and text[start - 2] == (92 if is_bytes else "\\")


def _sub_one(text, pat, keep, bounded, rep, is_bytes):
    parts, pos, i, n = [], 0, 0, 0
    while True:
        m = pat.search(text, i)
        if m is None:
            break
        if bounded and not _bounded(text, m.start(), is_bytes):
            i = m.start() + 1
            continue
        parts += [text[pos:m.start()], (m.group(keep) + rep) if keep else rep]
        pos = i = m.end()
        n += 1
    if not n:
        return text, 0
    parts.append(text[pos:])
    return (b"" if is_bytes else "").join(parts), n


def _substitute(text, counts, is_bytes):
    for kind, needles_b, needles_s, pat_b, pat_s, keep, bounded in _COMPILED:
        if not any(nd in text for nd in (needles_b if is_bytes else needles_s)):
            continue
        label = "[REDACTED:%s]" % kind
        text, n = _sub_one(text, pat_b if is_bytes else pat_s, keep, bounded,
                           label.encode("ascii") if is_bytes else label, is_bytes)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text


def mask_text(text):
    """文字列の秘密を伏せる。(伏せた文字列, {種類: 件数})。"""
    if not isinstance(text, str) or not _SCREEN_S.search(text):
        return text, {}
    counts = {}
    return _substitute(text, counts, False), counts


def _mask_obj(obj, counts):
    if isinstance(obj, str):
        new, c = mask_text(obj)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        return new
    if isinstance(obj, list):
        return [_mask_obj(v, counts) for v in obj]
    if isinstance(obj, dict):
        return {k: _mask_obj(v, counts) for k, v in obj.items()}
    return obj


def _loads_lenient(raw):
    """bytes → JSON。不正な UTF-8 は置換文字にして読む。読めなければ ValueError。"""
    try:
        return json.loads(raw)
    except UnicodeDecodeError:
        return json.loads(raw.decode("utf-8", "replace"))


def _is_json(raw):
    try:
        _loads_lenient(raw)
        return True
    except ValueError:
        return False


def _dumps_line(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:  # 対になっていないサロゲートは \u エスケープで書く
        return json.dumps(obj, separators=(",", ":")).encode("ascii")


def mask_blob(raw):
    """JSON 1 つぶん（JSONL の 1 行か .json 全体）の秘密を伏せる。(バイト列, {種類: 件数})。

    生のバイト列に正規表現をかけ、一致しなければバイト単位でそのまま返す。
    置換で JSON が壊れたときだけ、元を読んで値ごとに伏せて書き直す。
    """
    if not _SCREEN_B.search(raw):
        return raw, {}
    counts = {}
    new = _substitute(raw, counts, True)
    if not counts:
        return raw, {}
    body = raw.rstrip(b"\r\n")
    if _is_json(new.rstrip(b"\r\n")) or not _is_json(body):
        return new, counts
    counts = {}
    return _dumps_line(_mask_obj(_loads_lenient(body), counts)) + raw[len(body):], counts


# ---------------------------------------------------------------- 画像・PDF の base64 を抜く

_MEDIA_TYPE_RE = re.compile(r"^[a-z]+/[A-Za-z0-9.+-]+$")
_OMITTED_PREFIX = "[OMITTED:"


def _b64_size(data):
    """base64 文字列の元のバイト数（デコードせずに長さから）。"""
    return max(len(data) * 3 // 4 - (2 if data.endswith("==") else 1 if data.endswith("=") else 0), 0)


def _omit_obj(obj, counter):
    """`source.type == "base64"` の data と、Read の画像結果 `{"base64", "type": "image/png"}` の base64 を印に置き換える。"""
    if isinstance(obj, dict):
        src = obj.get("source")
        if isinstance(src, dict) and src.get("type") == "base64":
            data = src.get("data")
            if isinstance(data, str) and not data.startswith(_OMITTED_PREFIX):
                media = src.get("media_type") if isinstance(src.get("media_type"), str) else "unknown"
                src["data"] = "%s%s %d bytes]" % (_OMITTED_PREFIX, media, _b64_size(data))
                counter[0] += 1
        data, media = obj.get("base64"), obj.get("type")
        if (isinstance(data, str) and not data.startswith(_OMITTED_PREFIX)
                and isinstance(media, str) and _MEDIA_TYPE_RE.match(media)):
            obj["base64"] = "%s%s %d bytes]" % (_OMITTED_PREFIX, media, _b64_size(data))
            counter[0] += 1
        values = obj.values()
    elif isinstance(obj, list):
        values = obj
    else:
        return
    for v in values:
        if isinstance(v, (dict, list)):
            _omit_obj(v, counter)


def omit_blob(raw):
    """JSON 1 つぶんから画像・PDF の base64 を抜く。(バイト列, 抜いた件数)。抜かなかった行はバイト単位でそのまま。"""
    if b'"base64"' not in raw:
        return raw, 0
    body = raw.rstrip(b"\r\n")
    try:
        obj = _loads_lenient(body)
    except ValueError:
        return raw, 0
    counter = [0]
    _omit_obj(obj, counter)
    if not counter[0]:
        return raw, 0
    return _dumps_line(obj) + raw[len(body):], counter[0]


def transform_blob(raw):
    """送る前の変換: 画像・PDF を抜く → 秘密を伏せる。(バイト列, {種類: 件数}, 抜いた件数)。"""
    line, omitted = omit_blob(raw)
    line, counts = mask_blob(line)
    return line, counts, omitted


# ---------------------------------------------------------------- 会話ログの読み方


def parse_line(raw):
    try:
        d = _loads_lenient(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


_SEND_CMD_RE = re.compile(r"<command-name>/?send-to-nobu(?::send-to-nobu)?</command-name>")
_SR_PREFIX_RE = re.compile(r"^\s*(?:<system-reminder>.*?</system-reminder>\s*)+", re.S)
_NOT_HUMAN_PREFIXES = ("[Request interrupted by user", "<task-notification>", "<bash-stdout>", "<bash-stderr>",
                       "<local-command-stdout>", "<local-command-stderr>")
_TAG_RE = {name: re.compile(r"<%s>(.*?)</%s>" % (name, name), re.S)
           for name in ("command-name", "command-args", "bash-input")}


def _texts(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return None


def _human(text, origin):
    """人の文なら前後の空白と先頭の system-reminder を除いて返す。人の文でなければ None。"""
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    t = _SR_PREFIX_RE.sub("", text, count=1).strip()
    # 組み込みコマンド（/model /context など）は <command-name> で始まる。スキル呼び出しは <command-message> で始まる
    if not t or t.startswith(_NOT_HUMAN_PREFIXES) or t.startswith("<command-name>"):
        return None
    return t


def classify(d):
    """メインの会話の 1 行 → (種類, テキスト)。種類は send（/send-to-nobu）・human（人の指示）・None。"""
    if d.get("isSidechain") is True:
        return None, None
    if d.get("type") == "attachment":   # 作業中に打った文
        a = d.get("attachment")
        if (not isinstance(a, dict) or a.get("type") != "queued_command" or a.get("commandMode") != "prompt"
                or a.get("isMeta") is True):
            return None, None
        text = _texts(a.get("prompt"))
        t = _human(text, a.get("origin")) if text else None
        return ("human", t) if t else (None, None)
    if d.get("type") != "user":
        return None, None
    text = _texts((d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None)
    if text is None:
        return None, None               # tool_result だけ・画像だけの行
    if _SEND_CMD_RE.search(text):
        return "send", text
    if d.get("isMeta") is True or d.get("isCompactSummary") is True:
        return None, None
    t = _human(text, d.get("origin"))
    return ("human", t) if t else (None, None)


def display_text(text):
    """タイトル用に整える（スキル呼び出しは `/name 引数`、! コマンドは `!コマンド`）。"""
    head = text.lstrip()
    if head.startswith("<command-message>"):
        m, a = _TAG_RE["command-name"].search(text), _TAG_RE["command-args"].search(text)
        if m:
            return (m.group(1).strip() + " " + (a.group(1).strip() if a else "")).strip()
    if head.startswith("<bash-input>"):
        m = _TAG_RE["bash-input"].search(text)
        if m:
            return "!" + m.group(1).strip()
    return text


def squash(text, limit):
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


# ツールの結果にある連絡先・カード番号らしきもの（本人が外すかを決める目印。中身は持たない）
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24}(?![A-Za-z0-9-])")
_EMAIL_IGNORE = re.compile(r"(?:^|[._+-])no-?reply[._+-]?|@(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net)"
                           r"|users\.noreply\.github\.com)$", re.I)
_PHONE_RE = re.compile(r"(?<![\d.-])(?:0\d{1,4}-\d{1,4}-\d{3,4}|0[5789]0\d{8}|\+81[- ]?\d{1,4}[- ]?\d{1,4}[- ]?\d{3,4})"
                       r"(?![\d.-])")
_CARD_RE = re.compile(r"(?<![\d.])(?:4\d{3}|5[1-5]\d{2}|2[2-7]\d{2}|3[47]\d{2}|35\d{2}|6\d{3})(?:[ -]?\d{4}){2}[ -]?\d{2,4}"
                      r"(?![\d.])")
MARK_LABELS = (("contact", "連絡先あり"), ("card", "カード番号あり"))


def _is_card(digits):
    """13〜19 桁・同じ数字の並びではない・Luhn のチェック数字が合う。"""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return 13 <= len(digits) <= 19 and len(set(digits)) > 1 and total % 10 == 0


def tool_marks(d, marks):
    """ツールの結果（tool_result・toolUseResult）の文字列から目印を marks に足す。"""
    content = d["message"].get("content") if isinstance(d.get("message"), dict) else None
    stack = [b.get("content") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
        if isinstance(content, list) else []
    stack.append(d.get("toolUseResult"))
    while stack and len(marks) < 2:
        o = stack.pop()
        if isinstance(o, dict):
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, str) and len(o) >= 6:
            if "contact" not in marks and (_PHONE_RE.search(o) or ("@" in o and any(
                    not _EMAIL_IGNORE.search(m.group(0)) for m in _EMAIL_RE.finditer(o)))):
                marks.add("contact")
            if "card" not in marks and any(_is_card(re.sub(r"\D", "", m.group(0))) for m in _CARD_RE.finditer(o)):
                marks.add("card")


class Scan(object):
    """会話ファイル 1 本を読んだ結果。"""

    def __init__(self):
        self.first = None           # 最初の人の指示の種類（send / human）
        self.first_text = None
        self.custom_title = self.ai_title = self.cwd = self.last_ts = None
        self.end = 0                # 読み終えた位置（書きかけの最終行は含めない）
        self.marker = False         # 一覧の出力や控えの中身が残っている
        self.sdk = self.other_entry = False
        self.marks = set()

    def title(self):
        """最後の custom-title > 最後の ai-title > 最初の指示。秘密は伏せる。"""
        for t in (self.custom_title, self.ai_title):
            if t and t.strip():
                return squash(mask_text(t)[0], TITLE_MAX)
        return squash(mask_text(display_text(self.first_text or ""))[0], TITLE_CHARS)

    def listable(self, rec, baseline, mtime):
        """一覧に出す会話か。人の指示で始まり・一覧を読んでいない・自動実行だけではない・基準より後に動いた。"""
        if self.first != "human" or self.marker or (self.sdk and not self.other_entry):
            return False
        return rec is not None or (self.last_ts if self.last_ts is not None else mtime) >= baseline


def scan(path, quick=False, baseline=None):
    """会話ファイルを先頭から読む。quick なら、最初の指示（と基準より後の時刻）がわかったら目印だけを探す。"""
    s = Scan()
    with open_nofollow(path) as fh:
        for raw in fh:
            if not raw.endswith(b"\n"):
                break   # 書きかけの最終行は読まない（送るのもここまで）
            if quick and s.first is not None and (baseline is None or (s.last_ts or 0) >= baseline):
                s.marker = s.marker or _MARKER_B in raw
                continue
            s.end += len(raw)
            s.marker = s.marker or _MARKER_B in raw
            d = parse_line(raw)
            if d is None:
                continue
            t = d.get("type")
            if t in ("user", "assistant"):
                ts = parse_ts(d.get("timestamp"))
                if ts is not None and (s.last_ts is None or ts > s.last_ts):
                    s.last_ts = ts     # 時刻は逆転することがあるので最大をとる（行は並べ替えない）
                if s.cwd is None and isinstance(d.get("cwd"), str):
                    s.cwd = d["cwd"]
                if d.get("entrypoint") == "sdk-cli":
                    s.sdk = True
                elif isinstance(d.get("entrypoint"), str):
                    s.other_entry = True
            if t == "custom-title" and isinstance(d.get("customTitle"), str):
                s.custom_title = d["customTitle"]
            elif t == "ai-title" and isinstance(d.get("aiTitle"), str):
                s.ai_title = d["aiTitle"]
            kind, text = classify(d)
            if kind and s.first is None:
                s.first, s.first_text = kind, text
            if not quick and t == "user" and len(s.marks) < 2:
                if b'"base64"' in raw:
                    _omit_obj(d, [0])
                tool_marks(d, s.marks)
    return s


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SEG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MAX_REL_DEPTH = 6


def iter_sessions():
    """projects/*/<uuid>.jsonl（リンクは追わない）。同じ会話 ID が複数あれば新しい方。[(sid, path, stat)]。"""
    found = {}
    try:
        projects = [p for p in os.scandir(projects_dir()) if p.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    for pe in projects:
        try:
            entries = list(os.scandir(pe.path))
        except OSError:
            continue
        for e in entries:
            sid = e.name[:-6]
            if not e.name.endswith(".jsonl") or not _UUID_RE.match(sid):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and (sid not in found or st.st_mtime > found[sid][1].st_mtime):
                found[sid] = (e.path, st)
    return [(sid, p, st) for sid, (p, st) in sorted(found.items())]


def find_session_path(sid):
    for s, path, _ in iter_sessions():
        if s == sid:
            return path
    return None


def session_dir_of(main_path):
    """会話ディレクトリ <sid>/。リンクなら None。"""
    d = main_path[:-len(".jsonl")]
    try:
        return d if stat.S_ISDIR(os.lstat(d).st_mode) else None
    except OSError:
        return None


def _rel_ok(rel):
    segs = rel.split("/")
    return (len(segs) <= MAX_REL_DEPTH and all(_SEG_RE.match(x) and x not in (".", "..") for x in segs)
            and rel.endswith((".jsonl", ".json")))


def subagent_files(session_dir):
    """<session>/subagents/ 以下の .jsonl と .json（入れ子も。リンクはたどらない）。[(rel, path, size)]。"""
    out = []
    root = os.path.join(session_dir, "subagents") if session_dir else None
    try:
        if root is None or not stat.S_ISDIR(os.lstat(root).st_mode):
            return out
    except OSError:
        return out
    for dp, dns, fns in os.walk(root):
        rel_dir = os.path.relpath(dp, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        dns[:] = sorted(x for x in dns if not os.path.islink(os.path.join(dp, x)))
        for fn in sorted(fns):
            rel = "/".join(parts + [fn])
            if not _rel_ok(rel):
                continue
            try:
                st = os.lstat(os.path.join(dp, fn))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out.append((rel, os.path.join(dp, fn), st.st_size))
    return out


def has_turn_after(path, offset):
    """offset 以降に user / assistant 行があるか（開いて閉じただけで足されるメタ行は数えない）。"""
    try:
        with open_nofollow(path) as fh:
            if os.fstat(fh.fileno()).st_size < offset:
                return True     # 書き直された
            fh.seek(offset)
            for raw in fh:
                d = parse_line(raw)
                if d is not None and d.get("type") in ("user", "assistant"):
                    return True
    except OSError:
        pass
    return False


def file_has_marker(path):
    try:
        with open_nofollow(path) as fh:
            return any(_MARKER_B in raw for raw in fh)
    except OSError:
        return False


# ---------------------------------------------------------------- 状態（送った・外した会話と、その時の位置）


def state_path(data_dir):
    return os.path.join(data_dir, "state.json")


def pending_path(data_dir):
    return os.path.join(data_dir, "pending.json")


def load_state(data_dir, now, create=False):
    """{"baseline": 初回の基準, "sessions": {sid: {"d": sent|excluded, "offset", "size", "mtime", "sub_n", "sub_bytes"}}}。
    0.4 までと同じ形（前の版で外した会話は、この版でも外したまま）。"""
    path = state_path(data_dir)
    if not os.path.lexists(path):
        st = {"v": 1, "baseline": now - FIRST_RUN_DAYS * 86400, "sessions": {}}
        if create:
            write_json(path, st)
        return st
    st = read_json(path, None)
    if not isinstance(st, dict) or not isinstance(st.get("baseline"), (int, float)) \
            or not isinstance(st.get("sessions"), dict):
        raise Stop("送った記録（state.json）が読めない。消さずに のぶろう に知らせて")
    return st


def _sub_sig(subs):
    return [len(subs), sum(x[-1] for x in subs)]


def candidates(state, current):
    """未送信かもしれない会話 [(sid, path, stat, 前の判断 or None)]。

    外した会話は、続きが書かれても出さない。送った会話は、送った位置より後に行が増えたか、サブエージェントが増えたときだけ。
    まだ判断していない会話は、初回の基準より後に触られたものだけ。
    """
    out = []
    for sid, path, st in iter_sessions():
        rec = state["sessions"].get(sid)
        if sid == current:
            continue
        if isinstance(rec, dict):
            if rec.get("d") == "excluded":
                continue
            if rec.get("size") == st.st_size and rec.get("mtime") == int(st.st_mtime):
                continue
            if (not has_turn_after(path, int(rec.get("offset") or 0))
                    and _sub_sig(subagent_files(session_dir_of(path))) == [rec.get("sub_n", 0), rec.get("sub_bytes", 0)]):
                continue
        elif st.st_mtime < state["baseline"]:
            continue
        else:
            rec = None
        out.append((sid, path, st, rec))
    return out


def cleanup_old(data_dir):
    """0.4 までの置きもの（確認係に読ませた本文など）を消す。"""
    review = os.path.join(data_dir, "review")
    try:
        if stat.S_ISDIR(os.lstat(review).st_mode):
            shutil.rmtree(review, ignore_errors=True)
    except OSError:
        pass
    for name in ("scan-cache.json", "excluded-uuids.json", "lock"):
        remove_quietly(os.path.join(data_dir, name))


# ---------------------------------------------------------------- nudge


def cmd_nudge(args, out):
    try:
        if not (args.data_dir or "").strip():
            return 0    # ${CLAUDE_PLUGIN_DATA} が置き換わらなかった
        data_dir = resolve_data_dir(args.data_dir)
        now = _now()
        mark = os.path.join(data_dir, "nudged.json")
        if read_json(mark, {}).get("day") == day_key(now):
            return 0
        state = load_state(data_dir, now)
        current = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None
        t0, n = time.time(), 0
        for sid, path, st, rec in candidates(state, current):
            if time.time() - t0 > NUDGE_BUDGET:
                break
            try:
                s = scan(path, quick=True, baseline=None if rec else state["baseline"])
            except OSError:
                continue
            n += s.listable(rec, state["baseline"], st.st_mtime)
        if n:
            emit(out, systemMessage="未送信の会話が %d 件 → 新しい会話で /send-to-nobu と打つと のぶろう に送れる" % n)
            write_json(mark, {"day": day_key(now)})
    except Exception:
        pass    # 起動を邪魔しない
    return 0


# ---------------------------------------------------------------- list


def require_send_session(sid):
    """いまの会話の最初の指示が /send-to-nobu であること。いまの会話ファイルを返す。"""
    path = find_session_path(sid)
    if path is None or scan(path, quick=True).first != "send":
        raise Stop("この会話では使えない。新しい会話を始めて、最初に /send-to-nobu と打ってね")
    return path


def home_short(path):
    home = os.path.expanduser("~")
    if not isinstance(path, str):
        return ""
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def build_questions(items, more):
    """選択画面の質問。1 問目の本文に、結論と番号つきのタイトル一覧を入れる（AI の本文に頼らず本人に見せる）。"""
    note_q = {"question": NOTE_QUESTION, "header": "感想", "multiSelect": False,
              "options": [{"label": NOTE_OPTIONS[0], "description": "感想・質問はなし"},
                          {"label": NOTE_OPTIONS[1], "description": "この一言を送る"}]}
    if not items:
        note_q["question"] = "送る会話はない（感想・質問だけ送れる）。\n\n" + NOTE_QUESTION
        return [note_q]
    lines = ["未送信の会話が %d 件ある。外したもの以外を のぶろう に送る。" % len(items), ""]
    for it in items:
        tags = "・".join(label for key, label in MARK_LABELS if key in it["marks"])
        lines.append("%d. %s%s" % (it["n"], squash(it["title"], TITLE_CHARS) or "（タイトルなし）",
                                   "（%s）" % tags if tags else ""))
    if more:
        lines.append("（ほかに %d 件。送ったあと、もう一度 /send-to-nobu で出る）" % more)
    lines += ["", "送らない会話は？（外すなら入力欄に番号。例: 3, 5-7）"]
    return [{"question": "\n".join(lines), "header": "送らない", "multiSelect": False,
             "options": [{"label": NONE_LABEL, "description": "外さずに送る"},
                         {"label": ALL_LABEL, "description": "今回はどれも送らない（感想は送れる）"}]},
            note_q]


def cmd_list(args, out):
    data_dir = resolve_data_dir(args.data_dir)
    sid = current_session()
    cur_path = require_send_session(sid)
    now = _now()
    cleanup_old(data_dir)
    state = load_state(data_dir, now, create=True)
    rows = []
    for csid, path, st, rec in candidates(state, sid):
        try:
            s = scan(path)
            subs = subagent_files(session_dir_of(path))
        except OSError:
            continue
        if not s.listable(rec, state["baseline"], st.st_mtime) or any(file_has_marker(p) for _, p, _ in subs):
            continue
        last = s.last_ts if s.last_ts is not None else st.st_mtime
        rows.append({"session_id": csid, "path": path, "offset": s.end, "size": st.st_size, "mtime": int(st.st_mtime),
                     "subs": [[rel, size] for rel, _, size in subs], "sub_n": len(subs),
                     "sub_bytes": sum(size for _, _, size in subs), "title": s.title(),
                     "project": squash(mask_text(home_short(s.cwd))[0], PROJECT_MAX),
                     "last_activity": iso_utc(last), "last_ts": last, "marks": sorted(s.marks)})
    rows.sort(key=lambda r: (r["last_ts"], r["session_id"]))
    items = rows[:LIST_MAX]     # 多すぎるときは古い方から。残りは送ったあとの一覧に出る
    for n, it in enumerate(items, 1):
        it["n"] = n
    questions = build_questions(items, len(rows) - len(items))
    with open_nofollow(cur_path) as fh:
        session_offset = os.fstat(fh.fileno()).st_size   # これより後の答えだけを数える
    write_json(pending_path(data_dir), {
        LIST_MARKER: 1, "v": PENDING_V, "session": sid, "session_offset": session_offset, "created": now,
        "questions": questions, "exclude_question": questions[0]["question"] if items else None,
        "note_question": questions[-1]["question"], "items": items, "more": len(rows) - len(items)})
    emit(out, **{LIST_MARKER: 1, "count": len(items), "ask": {"questions": questions}, "next": NEXT_ASK})
    return 0


# ---------------------------------------------------------------- 答えを会話ログから読む


def _shape(questions):
    """質問の形（質問文・複数選択・選択肢）。AI が書き換えた質問への答えは数えない。"""
    if not isinstance(questions, list):
        return None
    out = []
    for q in questions:
        if not isinstance(q, dict) or not isinstance(q.get("options"), list):
            return None
        out.append((q.get("question"), bool(q.get("multiSelect")),
                    tuple(o.get("label") if isinstance(o, dict) else None for o in q["options"])))
    return out


def read_answer(path, offset, questions, used=()):
    """offset（一覧を出した時点）より後で、questions をそのまま聞いた選択画面への本人の答え (answers, annotations, id)。

    数えないもの: AI が answers を入れて呼んだもの・離席で自動的に閉じたもの（afkTimeoutMs）・答えが空のもの・
    エラー・聞き直しに使った答え（used）。いくつもあれば最後のもの。無ければ None。
    """
    want, asked, found = _shape(questions), set(), None
    with open_nofollow(path) as fh:
        fh.seek(offset)
        for raw in fh:
            d = parse_line(raw)
            content = (d.get("message") or {}).get("content") if d and isinstance(d.get("message"), dict) else None
            if not isinstance(content, list) or d.get("isSidechain") is True:
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if d.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                    inp = b.get("input")
                    if (isinstance(inp, dict) and "answers" not in inp and "annotations" not in inp
                            and isinstance(b.get("id"), str) and _shape(inp.get("questions")) == want):
                        asked.add(b["id"])
                elif (d.get("type") == "user" and b.get("type") == "tool_result" and b.get("tool_use_id") in asked
                      and b.get("tool_use_id") not in used):
                    r = d.get("toolUseResult")
                    if (not b.get("is_error") and isinstance(r, dict) and "afkTimeoutMs" not in r
                            and isinstance(r.get("answers"), dict) and _shape(r.get("questions")) == want
                            and any(isinstance(v, str) and v.strip() for v in r["answers"].values())):
                        found = (r["answers"], r.get("annotations") if isinstance(r.get("annotations"), dict) else {},
                                 b["tool_use_id"])
    return found


_NUMS_RE = re.compile(r"^[\d\s,、・と番~-]+$")


def parse_numbers(text, count):
    """入力欄の文 → 番号の集合。「なし」は空。番号・範囲・区切り以外が混じる・一覧に無い番号は None（聞き直す）。"""
    t = unicodedata.normalize("NFKC", text or "").replace("〜", "~").strip()
    if t.lower() in ("なし", "none"):
        return set()
    if not _NUMS_RE.match(t):
        return None
    out = set()
    for m in re.finditer(r"(\d+)(?:\s*[-~]\s*(\d+))?", t):
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= lo <= hi <= count:
            return None
        out.update(range(lo, hi + 1))
    return out or None


def _annotation(annotations, question):
    a = annotations.get(question)
    a = a.get("notes") if isinstance(a, dict) else a
    return a.strip() if isinstance(a, str) else ""


def decide(pending, answers, annotations):
    """答え → (外す番号, 感想)。外す質問に明示の答えが無い・番号が読めないときは聞き直す（Stop）。"""
    exclude, count = set(), len(pending["items"])
    q = pending.get("exclude_question")
    if q:
        a = answers.get(q).strip() if isinstance(answers.get(q), str) else ""
        extra = _annotation(annotations, q)
        got = (set() if a == NONE_LABEL else set(range(1, count + 1)) if a == ALL_LABEL
               else parse_numbers(a, count) if a else None)
        if got is not None and extra:
            more = parse_numbers(extra, count)
            got = None if more is None else got | more
        if got is None:
            said = a if a and a not in (NONE_LABEL, ALL_LABEL) else extra
            say = ("「%s」は番号として読めなかった。外すなら入力欄に番号だけ（例: 3, 5-7）、外さないなら「%s」を選んでね"
                   % (squash(said, 30), NONE_LABEL) if said
                   else "送らない会話の質問に答えがなかった。「%s」か番号で答えてね" % NONE_LABEL)
            raise Stop(say, code=3, ask={"questions": pending["questions"]}, next=NEXT_REASK)
        exclude = got
    nq = pending["note_question"]
    a = answers.get(nq).strip() if isinstance(answers.get(nq), str) else ""
    note = "\n".join(t for t in ("" if a == NOTE_OPTIONS[0] else a, _annotation(annotations, nq)) if t)
    return exclude, note


# ---------------------------------------------------------------- 固めて送る


def _main_path_ok(it):
    """控えの会話ファイルが projects/<dir>/<sid>.jsonl そのもの（リンクでない・ふつうのファイル）か。"""
    path, sid = it.get("path"), it.get("session_id")
    if not isinstance(path, str) or not _UUID_RE.match(sid or "") or os.path.basename(path) != sid + ".jsonl":
        return None
    expected = os.path.join(os.path.realpath(projects_dir()), os.path.basename(os.path.dirname(path)), sid + ".jsonl")
    try:
        ok = stat.S_ISREG(os.lstat(path).st_mode) and os.path.realpath(path) == expected
    except OSError:
        return None
    return path if ok else None


def _sub_path_ok(session_dir, rel):
    p = os.path.join(session_dir, "subagents", *rel.split("/")) if session_dir and _rel_ok(rel) else None
    try:
        if p and stat.S_ISREG(os.lstat(p).st_mode) and \
                os.path.realpath(p) == os.path.join(os.path.realpath(session_dir), "subagents", *rel.split("/")):
            return p
    except OSError:
        pass
    return None


def pack_file(src, dst, limit, jsonl):
    """src の先頭 limit バイト（一覧の時点の中身）を、画像を抜いて秘密を伏せて gzip（mtime=0）。
    .json は丸ごと読めたときだけ。戻り値 {"bytes", "sha256", "redactions", "local"} か None。"""
    redactions = 0
    with open_nofollow(src) as fin:
        if jsonl:
            chunks = _lines_upto(fin, limit)
        else:
            raw = fin.read(limit)
            if len(raw) != limit or not _is_json(raw):
                return None
            chunks = [raw]
        with open(dst, "wb") as fout, gzip.GzipFile(filename="", mode="wb", fileobj=fout, mtime=0,
                                                    compresslevel=6) as gz:
            for raw in chunks:
                line, counts, _ = transform_blob(raw)
                redactions += sum(counts.values())
                gz.write(line)
    h = hashlib.sha256()
    with open(dst, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return {"bytes": os.path.getsize(dst), "sha256": h.hexdigest(), "redactions": redactions, "local": dst}


def _lines_upto(fh, limit):
    pos = 0
    for raw in fh:
        if pos + len(raw) > limit:
            return
        pos += len(raw)
        yield raw


def pack_session(it, tmp):
    """送る会話 1 本を一覧の時点の中身で固める。(ファイルの並び, 送信票の 1 件)。"""
    path = _main_path_ok(it)
    if path is None or os.lstat(path).st_size < it["offset"]:
        raise Stop("一覧のあとで会話のファイルが動いた。%s" % AGAIN)
    sid = it["session_id"]
    main = pack_file(path, os.path.join(tmp, sid + ".jsonl.gz"), it["offset"], True)
    files = [dict(main, session_id=sid, rel=None)]
    sdir = session_dir_of(path)
    for i, (rel, size) in enumerate(it["subs"]):
        p = _sub_path_ok(sdir, rel)
        r = pack_file(p, os.path.join(tmp, "%s-%04d.gz" % (sid, i)), size, rel.endswith(".jsonl")) if p else None
        if r:
            files.append(dict(r, session_id=sid, rel=rel))
    sent = {"session_id": sid, "bytes": main["bytes"], "sha256": main["sha256"], "title": it["title"][:TITLE_MAX],
            "project": it["project"][:PROJECT_MAX], "last_activity": it["last_activity"],
            "redactions": sum(f["redactions"] for f in files),
            "subagents": [{"rel": f["rel"], "bytes": f["bytes"], "sha256": f["sha256"]} for f in files[1:]]}
    return files, sent


class ApiError(Stop):
    def __init__(self, status, api_code, path):
        if status == 401 or api_code in ("unauthorized", "already_finished"):
            super().__init__(None, code=4, next=NEXT_NEW_CODE)
        else:
            super().__init__("送れなかった（サーバーが受け付けなかった: %s %s %s）。%s。続くなら のぶろう に知らせて"
                             % (path, status, api_code or "-", AGAIN))


_OPENER = None


def _ssl_context():
    """CA 証明書が読めない Python（python.org 版で証明書を入れていない等）は macOS のシステムの束を使う。"""
    ctx = ssl.create_default_context()
    try:
        empty = ctx.cert_store_stats().get("x509_ca", 0) == 0
    except Exception:
        empty = True
    if empty and os.path.exists("/etc/ssl/cert.pem"):
        ctx.load_verify_locations(cafile="/etc/ssl/cert.pem")
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトは追わない（引換券やファイルが別の場所に渡らないように）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener(use_proxy=True):
    handlers = [_NoRedirect(), urllib.request.HTTPSHandler(context=_ssl_context())]
    if not use_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _open(req, timeout):
    global _OPENER
    if _OPENER is None:
        _OPENER = build_opener()
    return _OPENER.open(req, timeout=timeout)


def _api_code(e):
    try:
        err = json.loads(e.read().decode("utf-8", "replace")).get("error")
        return str(err.get("code") or "") if isinstance(err, dict) else ""
    except Exception:
        return ""


def api_post(api_base, path, body, code, retry_ok_codes=()):
    """引換券つきで JSON を POST。5xx と通信エラーだけリトライする。retry_ok_codes のエラーは 2 回目以降だけ成功扱い
    （/v1/finish の already_finished = 前の試行で送信票が置けていた）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + code, "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "send-to-nobu/%s" % plugin_version()}
    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(api_base + path, data=data, method="POST", headers=headers)
            with _open(req, 60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            api_code = _api_code(e)
            if api_code in retry_ok_codes and attempt > 0:
                return {}
            if e.code >= 500 and attempt < RETRIES:
                time.sleep(BACKOFF_BASE * 2 ** attempt)
                continue
            raise ApiError(e.code, api_code, path)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            if attempt < RETRIES:
                time.sleep(BACKOFF_BASE * 2 ** attempt)
                continue
            raise Stop("送れなかった（サーバーにつながらない: %s）。%s" % (type(getattr(e, "reason", None) or e).__name__, AGAIN))


def put_file(upload, local, size):
    """署名 URL に PUT（返ってきた headers をそのまま）。408・429・5xx と通信エラーはリトライ。"""
    headers = dict(upload.get("headers") or {}, **{"Content-Length": str(size)})
    last = "?"
    for attempt in range(RETRIES + 1):
        try:
            with open(local, "rb") as fh:
                req = urllib.request.Request(upload["url"], data=fh, method=upload.get("method") or "PUT",
                                             headers=headers)
                with _open(req, 300) as r:
                    r.read()
            return
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if not (e.code in (408, 429) or e.code >= 500):
                break
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            last = type(getattr(e, "reason", None) or e).__name__
        if attempt < RETRIES:
            time.sleep(BACKOFF_BASE * 2 ** attempt)
    raise Stop("送れなかった（アップロードに失敗: %s）。%s" % (last, AGAIN))


def upload_all(api_base, code, files):
    """/v1/uploads で署名 URL をもらい、並列 4 本で PUT する。"""
    urls = {}
    for i in range(0, len(files), UPLOAD_BATCH):
        body = {"files": [{"session_id": f["session_id"], "rel": f["rel"], "bytes": f["bytes"], "sha256": f["sha256"]}
                          for f in files[i:i + UPLOAD_BATCH]]}
        for u in api_post(api_base, "/v1/uploads", body, code).get("uploads") or []:
            if isinstance(u, dict):
                urls[(u.get("session_id"), u.get("rel") or None)] = u
    jobs = []
    for f in files:
        u = urls.get((f["session_id"], f["rel"]))
        if not u or not isinstance(u.get("url"), str) or not u["url"].startswith(ALLOWED_PUT_PREFIXES):
            raise Stop("送れなかった（サーバーが返したアップロード先がおかしい）。%s" % AGAIN)
        jobs.append((u, f["local"], f["bytes"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=PUT_WORKERS) as ex:
        for fut in [ex.submit(put_file, *j) for j in jobs]:
            fut.result()


def record(data_dir, now, sent, excluded):
    """送った・外したを、一覧の時点の位置で記録する。"""
    state = load_state(data_dir, now, create=True)
    for d, its in (("sent", sent), ("excluded", excluded)):
        for it in its:
            state["sessions"][it["session_id"]] = {"d": d, "at": iso_utc(now), "offset": it["offset"],
                                                   "size": it["size"], "mtime": it["mtime"],
                                                   "sub_n": it["sub_n"], "sub_bytes": it["sub_bytes"]}
    write_json(state_path(data_dir), state)


def result_say(sent, excluded, note, more):
    ex = "%d 件外した" % excluded
    if sent:
        say = "%d 件送った" % sent + ("・" + ex if excluded else "")
    elif note:
        say = "感想を送った" + ("（会話は %s）" % ex if excluded else "")
    else:
        say = "今回は何も送っていない" + ("（%s）" % ex if excluded else "")
    return say + ("。ほかに %d 件ある。続けるなら %s" % (more, AGAIN) if more else "")


def cmd_send(args, out, stdin):
    data_dir = resolve_data_dir(args.data_dir)
    sid = current_session()
    now = _now()
    pending = read_json(pending_path(data_dir), None)
    if not isinstance(pending, dict) or pending.get("v") != PENDING_V or pending.get("session") != sid:
        raise Stop("一覧が見つからない。%s" % AGAIN)
    path, used = find_session_path(sid), pending.get("used") or []
    deadline = time.time() + ANSWER_WAIT
    while True:
        got = read_answer(path, pending["session_offset"], pending["questions"], used) if path else None
        if got or time.time() >= deadline:
            break
        _sleep(0.5)
    if not got:
        raise Stop("選択画面の答えがなかったので、何も送っていない。送るときは %s" % AGAIN)
    try:
        exclude, note = decide(pending, got[0], got[1])
    except Stop:
        pending["used"] = used + [got[2]]       # 聞き直した答えは二度と読まない（新しい答えを待つ）
        write_json(pending_path(data_dir), pending)
        raise
    note = mask_text(note)[0][:NOTE_MAX]
    items = pending["items"]
    excluded = [it for it in items if it["n"] in exclude]
    to_send = [it for it in items if it["n"] not in exclude]
    anote = stdin.read() if args.assistant_note == "-" else (args.assistant_note or "")
    anote = mask_text(anote.strip())[0][:ASSISTANT_NOTE_MAX]
    folded = re.sub(r"\s+", "", anote)
    if any(len(t) >= 4 and t in folded for t in (re.sub(r"\s+", "", it["title"]) for it in items)):
        anote = ""      # AI のメモに一覧のタイトルが入っていたら送らない
    if to_send or note:
        api_base = (args.api_base or "").strip().rstrip("/")
        if api_base not in ALLOWED_API_BASES or not (args.code or "").strip():
            raise Stop(None, code=2, next="start_submission の upload_code と api_base をそのまま渡して send をもう一度")
        tmp = tempfile.mkdtemp(prefix="send-to-nobu-")
        try:
            packed = [pack_session(it, tmp) for it in to_send]
            upload_all(api_base, args.code.strip(), [f for files, _ in packed for f in files])
            body = {"sent": [s for _, s in packed], "excluded_count": len(excluded), "note": note,
                    "plugin_version": plugin_version()}
            if anote:
                body["assistant_note"] = anote
            api_post(api_base, "/v1/finish", body, args.code.strip(), retry_ok_codes=("already_finished",))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    record(data_dir, now, to_send, excluded)
    remove_quietly(pending_path(data_dir))
    emit(out, say=result_say(len(to_send), len(excluded), note, pending.get("more")), next=END,
         sent=len(to_send), excluded=len(excluded))
    return 0


# ---------------------------------------------------------------- main


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Stop(None, code=2, next="コマンドの形が違う（%s）。スキルに書いた形のまま実行し直す" % message)


def main(argv=None, stdin=None, stdout=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    p = _Parser(prog="agentlog.py")
    sub = p.add_subparsers(dest="cmd")
    for name in ("nudge", "list", "send"):
        sp = sub.add_parser(name)
        sp.add_argument("--data-dir")
        if name == "send":
            sp.add_argument("--code")
            sp.add_argument("--api-base")
            sp.add_argument("--assistant-note", help="AI のメモ（- なら標準入力から）")
    try:
        args = p.parse_args(argv)
        if args.cmd == "nudge":
            return cmd_nudge(args, stdout)
        if args.cmd == "list":
            return cmd_list(args, stdout)
        if args.cmd == "send":
            return cmd_send(args, stdout, stdin)
        raise Stop(None, code=2, next="サブコマンド（list か send）を付けて実行する")
    except Stop as e:
        if argv[:1] == ["nudge"]:
            return 0    # 起動を邪魔しない
        emit(stdout, say=e.say, **dict({"next": END if e.say else None}, **e.extra))
        return e.code
    except Exception as e:  # 想定外でも本文は出さない（調べるときは SEND_TO_NOBU_DEBUG=1）
        if os.environ.get("SEND_TO_NOBU_DEBUG"):
            import traceback
            traceback.print_exc()
        emit(stdout, say="うまく動かなかった（%s）。のぶろう に知らせて" % type(e).__name__, next=END)
        return 1


if __name__ == "__main__":
    sys.exit(main())
