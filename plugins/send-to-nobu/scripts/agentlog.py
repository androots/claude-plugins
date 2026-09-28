#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""send-to-nobu: Claude Code の会話ログを のぶろう に送るための処理一式。

python3 3.9 の標準ライブラリだけで動く（macOS の /usr/bin/python3 を想定）。

サブコマンド:
  nudge  SessionStart フック。未送信の会話があれば 1 日 1 回だけ 1 行知らせる
  status いまの会話に有効な一覧の控えがあるか（スキルが一覧モードか送信モードかを決める）
  list   未送信の会話のうち 1 ラウンド分（最大 15 件）の一覧を出し、控えを保存する（--preview で抜粋）
  send   控えに沿って、画像の base64 を外す → 秘密を伏せる → gzip → 引換券でアップロード → 送信票

status / list / send は「いまの会話の最初の人の指示が /send-to-nobu」の会話でしか動かない。
いまの会話 ID は env の CLAUDE_CODE_SESSION_ID だけから取る。

出力は AI が読む前提の短い JSON（stdout）。エラーは stderr に 1 行 + 非 0 終了。
本物の会話本文を stdout に出すのは `list --preview` のマスク済み抜粋だけ。
"""

import argparse
import collections
import concurrent.futures
import datetime
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import ssl
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SEG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MAX_REL_DEPTH = 6

FIRST_RUN_DAYS = 7          # 初回は「いま − 7 日」より前から触られていない会話を出さない
DAY_START_HOUR = 6          # nudge の日の区切り（ローカル時刻）
PENDING_TTL = 6 * 3600      # 一覧の控えの有効期限
HEAD_LINES = 200            # 分岐コピーの判定に使う先頭の行数
PREVIEW_MAX = 15            # 1 会話あたりの抜粋の最大数
ROUND_SIZE = 15             # 1 回の一覧（1 ラウンド）の件数。同じ履歴のまとまりは分けない
PREVIEW_LEVELS = (15, 10, 6, 3, 1, 0)  # 出力が大きいときは抜粋をこの順に減らす
PREVIEW_CHARS = 160         # 抜粋 1 つの最大文字数
LIST_OUTPUT_MAX = 20000     # 一覧の出力全体の上限（Bash ツールの 30,000 文字で切れないように）
TITLE_FALLBACK_CHARS = 40
TITLE_MAX = 200
PROJECT_MAX = 300
NOTE_MAX = 20000
FACTS_VERSION = 4          # 一覧に出すかの判定を変えたら上げる（nudge のキャッシュを読み直させる）
PENDING_VERSION = 5        # 一覧の控えの形を変えたら上げる
# 一覧の出力と控えに入れる目印。会話ファイルの生のバイト列にこれがある会話は（どう読んだにせよ
# 一覧や控えの中身が残っているので）一覧から隠す
LIST_MARKER = "send_to_nobu_list"
_LIST_MARKER_B = LIST_MARKER.encode("ascii")

UPLOAD_BATCH = 100
PUT_WORKERS = 4
RETRIES = 3                 # 最初の 1 回 + リトライ 3 回
BACKOFF_BASE = 1.0          # 1, 2, 4 秒（テストでは小さくする）
RETRYABLE_PUT_STATUS = {408, 429}
GZIP_LEVEL = 6

# 送り先はこれだけ（引数や env では広げられない。テストはコードから差し替える）
ALLOWED_API_BASES = ("https://agent-log-inbox-mcp.androots.co.jp",)
ALLOWED_PUT_PREFIXES = ("https://storage.googleapis.com/",)

# 会話ログの場所。None なら $CLAUDE_CONFIG_DIR/projects か ~/.claude/projects（テストはコードから差し替える）
PROJECTS_DIR = None

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIRM_SHARED = 3
EXIT_UNAUTHORIZED = 4
EXIT_NOT_ANSWERED = 5
EXIT_NOT_SEND_SESSION = 6


class Fail(Exception):
    """利用者（AI）に 1 行で伝えるエラー。"""

    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.message = message
        self.code = code


def _now():
    """いまの時刻（テストで差し替える）。"""
    return time.time()


# ---------------------------------------------------------------- 時刻・入出力


def parse_ts(value):
    """ISO 8601（末尾 Z かオフセット付き）→ epoch 秒。読めなければ None。"""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        base = datetime.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    rest = value[19:]
    frac = 0.0
    m = re.match(r"\.(\d+)", rest)
    if m:
        frac = float("0." + m.group(1))
        rest = rest[m.end():]
    offset = 0
    if rest not in ("", "Z", "z"):
        m = re.match(r"^([+-])(\d{2}):?(\d{2})$", rest)
        if not m:
            return None
        offset = (int(m.group(2)) * 60 + int(m.group(3))) * 60
        if m.group(1) == "-":
            offset = -offset
    return base.replace(tzinfo=datetime.timezone.utc).timestamp() + frac - offset


def iso_utc(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_short(ts):
    d = datetime.datetime.fromtimestamp(ts)
    return "%d/%d %02d:%02d" % (d.month, d.day, d.hour, d.minute)


def day_key(now):
    """朝 6 時を日の区切りにした日付（ローカル時刻）。"""
    d = datetime.datetime.fromtimestamp(now) - datetime.timedelta(hours=DAY_START_HOUR)
    return d.date().isoformat()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return str(n)


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, obj):
    """一時ファイル → rename で原子的に書く。"""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def remove_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def plugin_version():
    d = read_json(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), {})
    v = d.get("version") if isinstance(d, dict) else None
    return v if isinstance(v, str) else "0.0.0"


def projects_dir():
    if PROJECTS_DIR:
        return PROJECTS_DIR
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def resolve_data_dir(arg):
    """データディレクトリは --data-dir で受け取る（スキルとフックが ${CLAUDE_PLUGIN_DATA} を渡す）。
    env の CLAUDE_PLUGIN_DATA は読まない（Bash ツールの env には他プラグインの値が漏れていることがある）。"""
    if not arg or not arg.strip():
        raise Fail("--data-dir がない", EXIT_USAGE)
    path = os.path.abspath(os.path.expanduser(arg.strip()))
    # 置換されずに他プラグインのディレクトリを指したときに、そこへ書かないための歯止め
    if "send-to-nobu" not in os.path.basename(path.rstrip(os.sep)):
        raise Fail("--data-dir がこのプラグインのディレクトリではない: %s" % os.path.basename(path), EXIT_USAGE)
    return path


def session_id_from_env():
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        raise Fail("いまの会話 ID（CLAUDE_CODE_SESSION_ID）が無い。Claude Code の中で /send-to-nobu から使って", EXIT_USAGE)
    return sid


def home_short(path):
    if not isinstance(path, str) or not path:
        return ""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


# ---------------------------------------------------------------- ファイルを安全に開く


def open_nofollow(path):
    """シンボリックリンクを追わずに、ふつうのファイルだけを開く。"""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file: %s" % os.path.basename(path))
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _real_projects():
    return os.path.realpath(projects_dir())


def check_main_path(path, sid):
    """会話ファイルが projects/<dir>/<sid>.jsonl そのもの（途中にリンクが無い・ふつうのファイル）か。"""
    if not isinstance(path, str) or not UUID_RE.match(sid or ""):
        raise Fail("会話の控えが壊れている。/send-to-nobu で一覧を出し直して")
    name = os.path.basename(path)
    proj = os.path.basename(os.path.dirname(path))
    expected = os.path.join(_real_projects(), proj, name)
    try:
        st = os.lstat(path)
    except OSError:
        raise Fail("会話ファイルが見つからない。/send-to-nobu で一覧を出し直して")
    if name != sid + ".jsonl" or os.path.realpath(path) != expected or not stat.S_ISREG(st.st_mode):
        raise Fail("会話ファイルの場所がおかしい（リンクなど）ので送らない")
    return expected


def session_dir_of(main_path):
    """会話ディレクトリ <sid>/。リンクや projects の外なら None。"""
    d = main_path[:-len(".jsonl")]
    try:
        st = os.lstat(d)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    if os.path.realpath(d) != os.path.join(os.path.realpath(os.path.dirname(main_path)), os.path.basename(d)):
        return None
    return d


def check_rel(rel):
    segs = rel.split("/") if isinstance(rel, str) else []
    if (not segs or len(segs) > MAX_REL_DEPTH or any(not SEG_RE.match(s) or s in (".", "..") for s in segs)
            or not (rel.endswith(".jsonl") or rel.endswith(".json"))):
        return None
    return segs


def sub_path(session_dir, rel):
    """サブエージェントのファイル。途中にリンクが無い・ふつうのファイルのときだけパスを返す。"""
    segs = check_rel(rel)
    if session_dir is None or segs is None:
        return None
    p = os.path.join(session_dir, "subagents", *segs)
    expected = os.path.join(os.path.realpath(session_dir), "subagents", *segs)
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or os.path.realpath(p) != expected:
        return None
    return p


# ---------------------------------------------------------------- 秘密のマスク

_PEM_HEAD = r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
_PEM_TAIL = r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
# END まである形: 本文は base64・空白・ヘッダー行の文字・エスケープされた改行
_PEM_BODY_FULL = r"(?:[A-Za-z0-9+/= \t\r\n:,._-]|\\{1,3}[rnt])*?"
# END の無い途中切れ: base64 とエスケープされた改行が続く限り
_PEM_BODY_CUT = r"(?:[A-Za-z0-9+/=\r\n]|\\{1,3}[rn])*"

# (種類, その文字列を含む行だけ調べる目印, 正規表現, 残すグループ番号, 左の境界を見るか)。上から順にかける。
# 正規表現は目印の文字列から始める（先頭に後読みを置くと全位置で試して遅い。境界は _bounded で見る）
SECRET_PATTERNS = [
    ("private_key", ("PRIVATE KEY",),
     _PEM_HEAD + "(?:" + _PEM_BODY_FULL + _PEM_TAIL + "|" + _PEM_BODY_CUT + ")", 0, False),
    ("anthropic_key", ("sk-ant-",), r"sk-ant-[A-Za-z0-9_-]{20,}", 0, True),
    ("github_token", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
     r"g(?:h[pousr]_[A-Za-z0-9]{30,}|ithub_pat_[A-Za-z0-9_]{30,})", 0, True),
    ("slack_token", ("xox",), r"xox[abprs]-[A-Za-z0-9-]{10,}", 0, True),
    ("aws_access_key", ("AKIA", "ASIA"), r"A(?:KIA|SIA)[0-9A-Z]{16}(?![0-9A-Za-z])", 0, True),
    # Google の API キーは 39 文字固定。右にも境界を取って、画像などの base64 の中の偶然の一致を拾わない
    ("google_api_key", ("AIza",), r"AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_+/=-])", 0, True),
    # sk- と Bearer は「数字を 1 つ以上含む」先読みで文章の誤検知を防ぐ
    ("openai_key", ("sk-",), r"sk-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}", 0, True),
    ("bearer_token", ("earer",), r"([Bb]earer[ \t]+)(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{20,}", 1, True),
]


def _compile_patterns():
    out = []
    for kind, needles, pattern, keep, bounded in SECRET_PATTERNS:
        out.append((kind, tuple(n.encode("ascii") for n in needles), needles,
                    re.compile(pattern.encode("ascii")), re.compile(pattern), keep, bounded))
    return out


_COMPILED = _compile_patterns()
_SCREEN = "|".join(re.escape(n) for _, needles, _, _, _ in SECRET_PATTERNS for n in needles)
_SCREEN_B = re.compile(_SCREEN.encode("ascii"))
_SCREEN_S = re.compile(_SCREEN)

_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_ALNUM_S, _ALNUM_B = frozenset(_ALNUM), frozenset(_ALNUM.encode("ascii"))
_ESC_S, _ESC_B = frozenset("nrtbf"), frozenset(b"nrtbf")


def _bounded(text, start, is_bytes):
    """左の境界: 直前が英数字でない。JSON 文字列の中の改行は `\n` と書かれるので `\nsk-ant-…` も通す。"""
    if start == 0:
        return True
    prev = text[start - 1]
    if prev not in (_ALNUM_B if is_bytes else _ALNUM_S):
        return True
    backslash = 92 if is_bytes else "\\"
    return start >= 2 and prev in (_ESC_B if is_bytes else _ESC_S) and text[start - 2] == backslash


def _sub_one(text, pat, keep, bounded, rep, is_bytes):
    parts = []
    pos = i = n = 0
    while True:
        m = pat.search(text, i)
        if m is None:
            break
        if bounded and not _bounded(text, m.start(), is_bytes):
            i = m.start() + 1
            continue
        parts.append(text[pos:m.start()])
        parts.append((m.group(keep) + rep) if keep else rep)
        pos = i = m.end()
        n += 1
    if not n:
        return text, 0
    parts.append(text[pos:])
    return (b"" if is_bytes else "").join(parts), n


def _substitute(text, counts, is_bytes):
    for kind, needles_b, needles_s, pat_b, pat_s, keep, bounded in _COMPILED:
        needles = needles_b if is_bytes else needles_s
        if not any(nd in text for nd in needles):
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
        _merge_counts(counts, c)
        return new
    if isinstance(obj, list):
        return [_mask_obj(v, counts) for v in obj]
    if isinstance(obj, dict):
        return {k: _mask_obj(v, counts) for k, v in obj.items()}
    return obj


def _merge_counts(dst, src):
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def _loads_lenient(raw):
    """bytes → JSON。不正な UTF-8 は置換文字にして読む。読めなければ例外。"""
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
    """JSON 1 つぶん（JSONL の 1 行、または .json ファイル全体）の秘密を伏せる。

    生のバイト列に正規表現をかける。一致しなかったらバイト単位でそのまま返す。
    置換後に JSON として読めなくなったときだけ、元を読んで値ごとに伏せて書き直す。
    """
    if not _SCREEN_B.search(raw):
        return raw, {}
    counts = {}
    new = _substitute(raw, counts, True)
    if not counts:
        return raw, {}
    body = raw.rstrip(b"\r\n")
    ending = raw[len(body):]
    if _is_json(new.rstrip(b"\r\n")) or not _is_json(body):
        return new, counts
    obj = _loads_lenient(body)
    counts = {}
    obj = _mask_obj(obj, counts)
    return _dumps_line(obj) + ending, counts


# ---------------------------------------------------------------- 画像・文書の base64 を外す

_MEDIA_TYPE_RE = re.compile(r"^[a-z]+/[A-Za-z0-9.+-]+$")
_OMITTED_PREFIX = "[OMITTED:"


def _b64_size(data):
    """base64 文字列の元のバイト数（デコードせずに長さから）。"""
    n = len(data) * 3 // 4
    if data.endswith("=="):
        n -= 2
    elif data.endswith("="):
        n -= 1
    return max(n, 0)


def _omit_obj(obj, counter):
    """画像・文書の base64 を短い印に置き換える（その場で書き換える。ほかのキーは残す）。

    - `source.type == "base64"` のブロック（本文・tool_result の中の画像や PDF）の `data`
    - Read ツールの画像の結果 `{"base64": …, "type": "image/png", …}` の `base64`（同じ画像がもう 1 度入っている）
    """
    if isinstance(obj, dict):
        src = obj.get("source")
        if isinstance(src, dict) and src.get("type") == "base64":
            data = src.get("data")
            if isinstance(data, str) and not data.startswith(_OMITTED_PREFIX):
                media = src.get("media_type") if isinstance(src.get("media_type"), str) else "unknown"
                src["data"] = "%s%s %d bytes]" % (_OMITTED_PREFIX, media, _b64_size(data))
                counter[0] += 1
        data = obj.get("base64")
        media = obj.get("type")
        if (isinstance(data, str) and not data.startswith(_OMITTED_PREFIX)
                and isinstance(media, str) and _MEDIA_TYPE_RE.match(media)):
            obj["base64"] = "%s%s %d bytes]" % (_OMITTED_PREFIX, media, _b64_size(data))
            counter[0] += 1
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _omit_obj(v, counter)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                _omit_obj(v, counter)


def omit_blob(raw):
    """JSON 1 つぶんから画像・文書の base64 を外す。(バイト列, 置き換えた件数)。

    `"base64"` を含まない行はバイト単位でそのまま。置き換えた行だけ JSON を書き直す（壊さない）。
    """
    if b'"base64"' not in raw:
        return raw, 0
    body = raw.rstrip(b"\r\n")
    ending = raw[len(body):]
    try:
        obj = _loads_lenient(body)
    except ValueError:
        return raw, 0
    counter = [0]
    _omit_obj(obj, counter)
    if not counter[0]:
        return raw, 0
    return _dumps_line(obj) + ending, counter[0]


def transform_blob(raw):
    """送る前の変換: 画像・文書の base64 を外す → 秘密を伏せる。(バイト列, {種類: 件数}, 外した件数)。"""
    line, omitted = omit_blob(raw)
    line, counts = mask_blob(line)
    return line, counts, omitted


# ---------------------------------------------------------------- 会話の読み方


def parse_line(raw):
    """JSONL の 1 行 → dict。読めない行は None。"""
    try:
        d = _loads_lenient(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


_SEND_CMD_RE = re.compile(r"<command-name>/?send-to-nobu(?::send-to-nobu)?</command-name>")
_SR_PREFIX_RE = re.compile(r"^\s*(?:<system-reminder>.*?</system-reminder>\s*)+", re.S)
# tool_use の入力にこれが出る会話は一覧から隠す（一覧や控えの中身が残っているかもしれない）
_TOUCH_MARKERS = ("agentlog.py", "plugins/data/send-to-nobu")
_NOT_HUMAN_PREFIXES = (
    "[Request interrupted by user",
    "<task-notification>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)
_TAG_RE = {
    name: re.compile(r"<%s>(.*?)</%s>" % (name, name), re.S)
    for name in ("command-name", "command-args", "bash-input")
}


def user_text(d):
    """user 行のテキスト。テキストが無い（tool_result だけ・画像だけ）なら None。"""
    m = d.get("message")
    if not isinstance(m, dict):
        return None
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get("text") for b in c
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return None


def classify(d):
    """メインの会話の user 行を分類する。

    戻り値 (種類, テキスト)。種類は
      send  … 送信コマンド（/send-to-nobu）
      human … 人の指示（スキル呼び出しは <command-message> で始まる）
      None  … 人の指示ではない（<command-name> で始まる組み込みコマンドを含む）
    """
    if d.get("type") != "user" or d.get("isSidechain") is True:
        return None, None
    text = user_text(d)
    if text is None:
        return None, None
    # 送信コマンドの判定は、ほかの除外より先にやる
    if _SEND_CMD_RE.search(text):
        return "send", text
    if d.get("isMeta") is True or d.get("isCompactSummary") is True:
        return None, None
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None, None
    t = text
    if t.lstrip().startswith("<system-reminder>"):
        t = _SR_PREFIX_RE.sub("", t, count=1)
    t = t.strip()
    if not t or t.startswith(_NOT_HUMAN_PREFIXES):
        return None, None
    if t.startswith("<command-name>"):
        return None, None  # 組み込みコマンド（/model /context /goal など。直後に stdout が無いものもある）
    return "human", t


def _command_args(text):
    m = _TAG_RE["command-args"].search(text or "")
    return m.group(1).strip() if m else ""


def _touches_send_to_nobu(d):
    """assistant 行の tool_use の入力に agentlog.py（どのサブコマンドでも）や send-to-nobu のデータディレクトリが出るか。"""
    m = d.get("message")
    content = m.get("content") if isinstance(m, dict) else None
    if not isinstance(content, list):
        return False
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            try:
                text = json.dumps(b.get("input"), ensure_ascii=False)
            except (TypeError, ValueError):
                continue
            if any(mk in text for mk in _TOUCH_MARKERS):
                return True
    return False


def display_text(text):
    """抜粋・タイトル用に整える（スキル呼び出しは `/name 引数` の形に）。マスク前。"""
    head = text.lstrip()
    if head.startswith(("<command-message>", "<command-name>")):
        m = _TAG_RE["command-name"].search(text)
        if m:
            return (m.group(1).strip() + " " + _command_args(text)).strip()
    if head.startswith("<bash-input>"):
        m = _TAG_RE["bash-input"].search(text)
        if m:
            return "!" + m.group(1).strip()
    return text


def squash(text, limit):
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


class Scan(object):
    """会話ファイル 1 本を読んだ結果。light なら一覧に出すかの判定に要るものだけ。"""

    def __init__(self, light=False):
        self.light = light
        self.prompt_count = 0
        self.first_prompts = []
        self.last_prompts = collections.deque(maxlen=PREVIEW_MAX)
        self.first_kind = None      # 最初に出てきた「人の指示 or 送信コマンド」
        self.custom_title = None
        self.ai_title = None
        self.cwd = None
        self.last_ts = None         # user / assistant 行の時刻の最大（並べ替えない・切らない）
        self.uuids = set()
        self.head_uuids = set()
        self.end = 0                # 読み終えた位置（書きかけの最終行は含めない）
        self.touched = False        # agentlog.py や send-to-nobu のデータを触った跡
        self.saw_sdk = False        # entrypoint: sdk-cli の行
        self.saw_other_entry = False

    def add_prompt(self, text):
        if self.first_kind is None:
            self.first_kind = "human"
        self.prompt_count += 1
        if self.light:
            return
        if len(self.first_prompts) < PREVIEW_MAX:
            self.first_prompts.append(text)
        self.last_prompts.append(text)

    def facts(self):
        """一覧に出すかを決める事実（nudge のキャッシュにもそのまま入る）。"""
        return {"prompts": self.prompt_count, "first": self.first_kind, "touched": self.touched,
                "sdk_only": self.saw_sdk and not self.saw_other_entry, "last_ts": self.last_ts}

    def title(self):
        """最後の custom-title > 最後の ai-title > 最初の人の指示の先頭 40 文字。マスクしてから切る。"""
        if self.custom_title and self.custom_title.strip():
            t, limit = self.custom_title, TITLE_MAX
        elif self.ai_title and self.ai_title.strip():
            t, limit = self.ai_title, TITLE_MAX
        elif self.first_prompts:
            t, limit = display_text(self.first_prompts[0]), TITLE_FALLBACK_CHARS
        else:
            return ""
        return re.sub(r"\s+", " ", mask_text(t)[0]).strip()[:limit]

    def prompts_for_preview(self, limit):
        """先頭寄り + 末尾寄りで最大 limit 個（並びは会話の順）。"""
        if limit <= 0:
            return []
        if self.prompt_count <= limit:
            return list(self.first_prompts[:self.prompt_count])
        head = (limit * 2 + 2) // 3
        tail = limit - head
        tail_items = list(self.last_prompts)[-tail:] if tail else []
        return self.first_prompts[:head] + tail_items


def listable(facts, rec, baseline, mtime):
    """一覧に出す会話か（list と nudge で同じ判定）。"""
    if not facts["prompts"] or facts["first"] == "send" or facts["touched"] or facts["sdk_only"]:
        return False
    if rec is None:
        last = facts["last_ts"] if facts["last_ts"] is not None else mtime
        if last < baseline:
            return False  # 基準より前の会話を開いて閉じただけ
    return True


def scan_session(path, light=False, stop_at_first=False):
    """会話ファイルを先頭から読む。"""
    s = Scan(light=light)
    with open_nofollow(path) as fh:
        for i, raw in enumerate(fh):
            d = parse_line(raw)
            if not raw.endswith(b"\n") and d is None:
                break  # 書きかけの最終行
            s.end += len(raw)
            if not s.touched and _LIST_MARKER_B in raw:
                s.touched = True  # 一覧や控えの中身が、どれかのツールの結果に残っている
            if d is None:
                continue
            u = d.get("uuid")
            if not light and isinstance(u, str):
                s.uuids.add(u)
                if i < HEAD_LINES:
                    s.head_uuids.add(u)
            t = d.get("type")
            if t in ("user", "assistant"):
                ts = parse_ts(d.get("timestamp"))
                if ts is not None and (s.last_ts is None or ts > s.last_ts):
                    s.last_ts = ts
                if s.cwd is None and isinstance(d.get("cwd"), str):
                    s.cwd = d["cwd"]
                ep = d.get("entrypoint")
                if ep == "sdk-cli":
                    s.saw_sdk = True
                elif isinstance(ep, str):
                    s.saw_other_entry = True
                if t == "assistant" and not s.touched and _touches_send_to_nobu(d):
                    s.touched = True
            elif t == "custom-title" and isinstance(d.get("customTitle"), str):
                s.custom_title = d["customTitle"]
            elif t == "ai-title" and isinstance(d.get("aiTitle"), str):
                s.ai_title = d["aiTitle"]
            if t != "user" or d.get("isSidechain") is True:
                continue
            kind, text = classify(d)
            if kind == "send":
                if s.first_kind is None:
                    s.first_kind = "send"
            elif kind == "human":
                s.add_prompt(text)
            if stop_at_first and s.first_kind is not None:
                return s
    return s


def has_turn_after(path, offset):
    """offset 以降に user / assistant 行があるか（閉じただけで足されたメタ行は数えない）。"""
    try:
        with open_nofollow(path) as fh:
            size = os.fstat(fh.fileno()).st_size
            if size < offset:
                return True  # 書き直された
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                d = parse_line(raw)
                if d is not None and d.get("type") in ("user", "assistant"):
                    return True
    except OSError:
        return False
    return False


def reply_after(path, offset):
    """offset 以降に本人の返事（人の指示、または引数付きの送信コマンド）があるか。"""
    with open_nofollow(path) as fh:
        fh.seek(offset)
        for raw in fh:
            d = parse_line(raw)
            if d is None:
                continue
            kind, text = classify(d)
            if kind == "human" or (kind == "send" and _command_args(text)):
                return True
    return False


def subagent_files(session_dir):
    """<session>/subagents/ 以下の送る対象（.jsonl と .json）。[(rel, path, size)]。

    入れ子（workflows/wf_*/…）・.meta.json・journal.jsonl も含む。tool-results/ などは送らない。
    リンクはたどらない。
    """
    out = []
    if session_dir is None:
        return out
    root = os.path.join(session_dir, "subagents")
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return out
    except OSError:
        return out
    for dp, dns, fns in os.walk(root):
        rel_dir = os.path.relpath(dp, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        dns[:] = sorted(d for d in dns if SEG_RE.match(d) and d not in (".", "..")
                        and not os.path.islink(os.path.join(dp, d)) and len(parts) + 2 <= MAX_REL_DEPTH)
        for fn in sorted(fns):
            rel = "/".join(parts + [fn])
            if check_rel(rel) is None:
                continue
            p = os.path.join(dp, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out.append((rel, p, st.st_size))
    return out


def sub_signature(files):
    return [len(files), sum(f[-1] for f in files)]


def iter_sessions(pdir):
    """projects/*/<uuid>.jsonl を列挙する（リンクは追わない）。同じ会話 ID が複数あれば新しい方。"""
    found = {}
    try:
        projects = list(os.scandir(pdir))
    except OSError:
        return []
    for pe in projects:
        try:
            if not pe.is_dir(follow_symlinks=False):
                continue
            entries = list(os.scandir(pe.path))
        except OSError:
            continue
        for e in entries:
            name = e.name
            if not name.endswith(".jsonl") or not UUID_RE.match(name[:-6]):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            sid = name[:-6]
            if sid not in found or st.st_mtime > found[sid][1].st_mtime:
                found[sid] = (e.path, st)
    return [(sid, p, st) for sid, (p, st) in found.items()]


def find_session_path(sid):
    """いまの会話のファイル projects/*/<sid>.jsonl。"""
    pdir = projects_dir()
    best = None
    try:
        projects = list(os.scandir(pdir))
    except OSError:
        return None
    for pe in projects:
        try:
            if not pe.is_dir(follow_symlinks=False):
                continue
            p = os.path.join(pe.path, sid + ".jsonl")
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and (best is None or st.st_mtime > best[1]):
            best = (p, st.st_mtime)
    return best[0] if best else None


def require_send_session(sid):
    """いまの会話の最初の人の指示が /send-to-nobu であること（普通の会話の途中では使わせない）。"""
    path = find_session_path(sid)
    s = scan_session(path, light=True, stop_at_first=True) if path else None
    if s is None or s.first_kind != "send":
        raise Fail("この会話では使えない。新しい会話を始めて、最初に /send-to-nobu と打ってね", EXIT_NOT_SEND_SESSION)
    return path


def uuid_hash(u):
    return hashlib.sha256(u.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 状態


def state_path(data_dir):
    return os.path.join(data_dir, "state.json")


def pending_path(data_dir):
    return os.path.join(data_dir, "pending.json")


def excluded_path(data_dir):
    return os.path.join(data_dir, "excluded-uuids.json")


def cache_path(data_dir):
    return os.path.join(data_dir, "scan-cache.json")


def load_state(data_dir, now):
    """状態を読む。無ければ「いま − 7 日」を基準に作る。あるのに読めなければ失敗（黙って作り直さない）。"""
    path = state_path(data_dir)
    if not os.path.lexists(path):
        st = {"v": 1, "baseline": now - FIRST_RUN_DAYS * 86400, "sessions": {}}
        write_json_atomic(path, st)
        return st
    st = read_json(path, None)
    if (not isinstance(st, dict) or not isinstance(st.get("baseline"), (int, float))
            or not isinstance(st.get("sessions"), dict)):
        raise Fail("状態ファイル（state.json）が読めない。消さずに のぶろう に知らせて")
    return st


def load_excluded_store(data_dir):
    path = excluded_path(data_dir)
    if not os.path.lexists(path):
        return {}
    store = read_json(path, None)
    if not isinstance(store, dict):
        raise Fail("外した会話の記録（excluded-uuids.json）が読めない。消さずに のぶろう に知らせて")
    return store


def load_cache(data_dir):
    c = read_json(cache_path(data_dir), {})
    return c if isinstance(c, dict) else {}


def _stat_unchanged(rec, st):
    return rec.get("size") == st.st_size and rec.get("mtime") == int(st.st_mtime)


def candidates(state, pdir, current):
    """未送信かもしれない会話。(sid, path, stat, 前回の判断 or None)。

    判断済みの会話は「判断時の位置より後ろに user / assistant 行が増えた」か
    「サブエージェントが増えた」ときだけ。判断していない会話は基準以降に触られたものだけ。
    """
    sessions = state["sessions"]
    baseline = state["baseline"]
    out = []
    for sid, path, st in iter_sessions(pdir):
        if sid == current:
            continue
        rec = sessions.get(sid)
        if isinstance(rec, dict):
            if _stat_unchanged(rec, st):
                continue
            if not has_turn_after(path, int(rec.get("offset", 0))):
                sig = sub_signature(subagent_files(session_dir_of(path)))
                if sig == [rec.get("sub_n", 0), rec.get("sub_bytes", 0)]:
                    continue
            out.append((sid, path, st, rec))
        elif st.st_mtime >= baseline:
            out.append((sid, path, st, None))
    return out


def cached_facts(cache, sid, path, st):
    """(size, mtime) が同じならキャッシュの事実を使う。違えば読み直してキャッシュを更新する。"""
    c = cache.get(sid)
    if isinstance(c, dict) and c.get("size") == st.st_size and c.get("mtime") == int(st.st_mtime) \
            and c.get("v") == FACTS_VERSION and isinstance(c.get("facts"), dict):
        return c["facts"]
    facts = scan_session(path, light=True).facts()
    cache[sid] = {"v": FACTS_VERSION, "size": st.st_size, "mtime": int(st.st_mtime), "facts": facts}
    return facts


# ---------------------------------------------------------------- nudge


def cmd_nudge(args, out):
    try:
        if not args.data_dir or not args.data_dir.strip():
            return 0  # 置換されなかった。~/.claude などに落ちない
        data_dir = resolve_data_dir(args.data_dir)
        now = _now()
        state = load_state(data_dir, now)
        today = day_key(now)
        if state.get("nudged_day") == today:
            return 0
        current = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None
        old_cache = load_cache(data_dir)
        cache = {}
        n = 0
        for sid, path, st, rec in candidates(state, projects_dir(), current):
            if sid in old_cache:
                cache[sid] = old_cache[sid]
            try:
                facts = cached_facts(cache, sid, path, st)
            except OSError:
                continue
            if listable(facts, rec, state["baseline"], st.st_mtime):
                n += 1
        if cache != old_cache:
            write_json_atomic(cache_path(data_dir), cache)
        if n <= 0:
            return 0
        msg = "未送信の会話が %d 件 → 新しい会話で /send-to-nobu と打つと のぶろう に送れます" % n
        out.write(json.dumps({"systemMessage": msg}, ensure_ascii=False) + "\n")
        fresh = read_json(state_path(data_dir), None)
        if isinstance(fresh, dict):
            fresh["nudged_day"] = today
            write_json_atomic(state_path(data_dir), fresh)
    except Exception:
        pass  # 起動を邪魔しない
    return 0


# ---------------------------------------------------------------- status


def valid_pending(data_dir, current, now):
    """同じ会話 ID・6 時間以内の一覧の控え。無ければ None。"""
    p = read_json(pending_path(data_dir), None)
    if (not isinstance(p, dict) or p.get("v") != PENDING_VERSION or not isinstance(p.get("items"), list)
            or p.get("session") != current):
        return None
    created = p.get("created_ts")
    if not isinstance(created, (int, float)) or now - created > PENDING_TTL or now < created - 300:
        return None
    return p


def cmd_status(args, out):
    data_dir = resolve_data_dir(args.data_dir)
    sid = session_id_from_env()
    require_send_session(sid)
    p = valid_pending(data_dir, sid, _now())
    out.write(json.dumps({"pending": True, "count": len(p["items"])} if p else {"pending": False}) + "\n")
    return 0


# ---------------------------------------------------------------- list


def build_list(state, current, excluded_store, cache):
    """一覧に出せる会話をすべて集める（古い順）。(items, scans)。"""
    rows = []
    for sid, path, st, rec in candidates(state, projects_dir(), current):
        try:
            s = scan_session(path)
        except OSError:
            continue
        facts = s.facts()
        cache[sid] = {"v": FACTS_VERSION, "size": st.st_size, "mtime": int(st.st_mtime), "facts": facts}
        if not listable(facts, rec, state["baseline"], st.st_mtime):
            continue
        last = s.last_ts if s.last_ts is not None else st.st_mtime
        subs = subagent_files(session_dir_of(path))
        rows.append((last, sid, path, st, rec, s, subs))
    rows.sort(key=lambda r: (r[0], r[1]))

    # 分岐コピー: 一方の先頭 200 行の uuid が、もう一方のどこかに出てくる
    head_index = collections.defaultdict(set)
    for idx, row in enumerate(rows):
        for u in row[5].head_uuids:
            head_index[u].add(idx)
    shares = collections.defaultdict(set)
    for idx, row in enumerate(rows):
        for u in row[5].uuids:
            for other in head_index.get(u, ()):
                if other != idx:
                    shares[idx].add(other)
                    shares[other].add(idx)
    excluded_hashes = {}
    for esid, hashes in excluded_store.items():
        for h in (hashes if isinstance(hashes, list) else []):
            excluded_hashes.setdefault(h, set()).add(esid)

    items = []
    for idx, (last, sid, path, st, rec, s, subs) in enumerate(rows):
        sig = sub_signature(subs)
        items.append({
            "session_id": sid,
            "path": path,
            "offset": s.end,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "subs": [[rel, size] for rel, _, size in subs],
            "sub_n": sig[0],
            "sub_bytes": sig[1],
            "title": s.title(),
            "project": squash(mask_text(home_short(s.cwd))[0], PROJECT_MAX),
            "last_activity": iso_utc(last),
            "last_ts": last,
            "prompt_count": s.prompt_count,
            "total_bytes": st.st_size + sig[1],
            "shares_idx": sorted(shares.get(idx, ())),
            "previously_excluded": bool((rec and rec.get("d") == "excluded") or sid in excluded_store),
            "contains_excluded_copy": any(
                (excluded_hashes.get(uuid_hash(u), set()) - {sid}) for u in s.head_uuids),
        })
    return items, [r[5] for r in rows]


def share_groups(items):
    """shares_history_with でつながった会話のまとまり。新しい順（まとまりの中の一番新しい会話で比べる）。"""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for idx, it in enumerate(items):
        for o in it["shares_idx"]:
            a, b = find(idx), find(o)
            if a != b:
                parent[a] = b
    groups = collections.defaultdict(list)
    for idx in range(len(items)):
        groups[find(idx)].append(idx)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[-1], reverse=True)


def select_round(items):
    """1 ラウンドに入れる会話（添字の昇順）。

    同じ履歴のまとまりは分けない。新しいまとまりから ROUND_SIZE 件まで詰める（入らないまとまりは飛ばして、
    もっと古い小さなまとまりで埋める）。一番新しいまとまりだけで ROUND_SIZE を超えるなら、そのまとまりだけ。
    """
    groups = share_groups(items)
    if not groups:
        return []
    if len(groups[0]) > ROUND_SIZE:
        return groups[0]
    chosen = []
    for g in groups:
        if len(chosen) + len(g) <= ROUND_SIZE:
            chosen.extend(g)
        if len(chosen) == ROUND_SIZE:
            break
    return sorted(chosen)


def render_round(items, scans, shown, level, preview, state, remaining, compact=False, cut=False, header=None):
    """items のうち shown（添字の昇順）を 1 から番号を振って、出力と控えの形にする。

    compact: 抜粋・会話 ID・プロジェクトを省き、タイトルを短くし、同じ履歴は番号の並びでなく group で示す。
    cut: 同じ履歴のまとまりが大きすぎて一部しか出せなかった。出した会話を送るには --confirm-shared が要る。
    """
    number = {idx: k + 1 for k, idx in enumerate(shown)}
    group_of = {}
    if compact:
        for gno, g in enumerate(sorted((g for g in share_groups(items) if len(g) > 1 and any(i in number for i in g)),
                                       key=lambda g: min(number[i] for i in g if i in number)), 1):
            for i in g:
                group_of[i] = gno
    rows, pend = [], []
    for idx in shown:
        it, s = items[idx], scans[idx]
        n = number[idx]
        shares = sorted(number[o] for o in it["shares_idx"] if o in number)
        default_excluded = it["previously_excluded"] or it["contains_excluded_copy"]
        if compact:
            row = {"n": n, "title": it["title"][:TITLE_FALLBACK_CHARS], "updated": local_short(it["last_ts"]),
                   "size": human_size(it["total_bytes"]), "prompts": it["prompt_count"]}
        else:
            row = {"n": n, "session_id": it["session_id"], "title": it["title"], "project": it["project"],
                   "updated": local_short(it["last_ts"]), "size": human_size(it["total_bytes"]),
                   "prompts": it["prompt_count"]}
            if it["sub_n"]:
                row["subagent_files"] = it["sub_n"]
        if it["previously_excluded"]:
            row["previously_excluded"] = True
        if it["contains_excluded_copy"]:
            row["contains_excluded_copy"] = True
        if default_excluded:
            row["default_excluded"] = True
        if compact:
            if idx in group_of:
                row["history_group"] = group_of[idx]
        elif shares:
            row["shares_history_with"] = shares
        if cut:
            row["group_cut"] = True
        if preview and not compact:
            picked = s.prompts_for_preview(level)
            row["preview"] = [squash(mask_text(display_text(t))[0], PREVIEW_CHARS) for t in picked]
            if s.prompt_count > len(picked):
                row["preview_omitted"] = s.prompt_count - len(picked)
        rows.append(row)
        p = {k: v for k, v in it.items() if k not in ("prompt_count", "total_bytes", "last_ts", "shares_idx")}
        p.update({"n": n, "shares": shares, "group_cut": cut})
        pend.append(p)
    result = {LIST_MARKER: 1}
    result.update(header or {})
    result.update({"count": len(rows), "items": rows, "remaining": remaining})
    if not state["sessions"]:
        result["first_run"] = True
        result["since"] = local_short(state["baseline"])
    return result, pend


def fit_round(items, scans, preview, state, header=None):
    """1 ラウンドを選び、出力が LIST_OUTPUT_MAX 文字に収まるように抜粋を減らす。

    抜粋 0 でも収まらなければ行を短くする（全件のタイトル行は出す）。それでも収まらないほど大きなまとまりは
    新しい会話から入るだけ出す。出さなかった会話は控えに入れない（送らない）。出した会話にも同じ履歴が
    入っているので、送るには --confirm-shared を要る。
    """
    shown = select_round(items)
    remaining = len(items) - len(shown)

    def fits(result):
        return len(json.dumps(result, ensure_ascii=False)) <= LIST_OUTPUT_MAX

    for level in (PREVIEW_LEVELS if preview else (0,)):
        result, pend = render_round(items, scans, shown, level, preview, state, remaining, header=header)
        if fits(result):
            return result, pend
    result, pend = render_round(items, scans, shown, 0, preview, state, remaining, compact=True, header=header)
    if fits(result):
        return result, pend
    for m in range(len(shown) - 1, 0, -1):
        part = shown[-m:]
        result, pend = render_round(items, scans, part, 0, preview, state, remaining + len(shown) - m,
                                    compact=True, cut=True, header=header)
        if fits(result):
            return result, pend
    return render_round(items, scans, [], 0, preview, state, len(items), header=header)


def file_size(path):
    with open_nofollow(path) as fh:
        return os.fstat(fh.fileno()).st_size


def cmd_list(args, out):
    data_dir = resolve_data_dir(args.data_dir)
    sid = session_id_from_env()
    cur_path = require_send_session(sid)
    now = _now()
    state = load_state(data_dir, now)
    store = load_excluded_store(data_dir)

    pending = valid_pending(data_dir, sid, now)
    if pending and isinstance(pending.get("output"), dict):
        # 同じ会話の中では同じ一覧・同じ番号を返す（返事の待ち受けはここから数え直す）
        pending["session_size"] = file_size(cur_path)
        write_json_atomic(pending_path(data_dir), pending)
        out.write(json.dumps(pending["output"], ensure_ascii=False) + "\n")
        return 0

    old_cache = load_cache(data_dir)
    cache = dict(old_cache)
    items, scans = build_list(state, sid, store, cache)
    # この会話で何ラウンド目か・感想をもう送ったか（2 ラウンド目以降は感想を聞かない。前の答えを使い回させない）
    conv = conversation_record(state, sid)
    header = {"round": int(conv.get("rounds") or 0) + 1}
    if conv.get("note_sent"):
        header["note_already_sent"] = True
    result, pend_items = fit_round(items, scans, args.preview, state, header)
    if cache != old_cache:
        write_json_atomic(cache_path(data_dir), cache)
    pending = {LIST_MARKER: 1, "v": PENDING_VERSION, "session": sid, "created_at": iso_utc(now), "created_ts": now,
               "session_size": file_size(cur_path), "items": pend_items, "output": result,
               "remaining": result["remaining"]}
    write_json_atomic(pending_path(data_dir), pending)
    out.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


# ---------------------------------------------------------------- pack


class _HashWriter(object):
    """書いたバイト列の sha256 を取りながらファイルに書く。"""

    def __init__(self, fh):
        self.fh = fh
        self.h = hashlib.sha256()

    def write(self, b):
        self.h.update(b)
        return self.fh.write(b)

    def flush(self):
        self.fh.flush()


def pack_jsonl(src, dst, limit):
    """JSONL を先頭から limit バイトまで、1 行ずつ変換して gzip（mtime=0 で sha256 を安定させる）。

    limit をまたぐ行と書きかけの最終行は含めない（一覧の時点より後ろは送らない）。
    戻り値 {"bytes": gzip 後, "sha256", "counts": 伏せた件数, "omitted": 外した件数, "end": 読んだ位置}。
    """
    counts = {}
    omitted = 0
    end = 0
    with open_nofollow(src) as fin, open(dst, "wb") as fout:
        w = _HashWriter(fout)
        with gzip.GzipFile(filename="", mode="wb", fileobj=w, mtime=0, compresslevel=GZIP_LEVEL) as gz:
            for raw in fin:
                if end + len(raw) > limit:
                    break
                if not raw.endswith(b"\n") and parse_line(raw) is None:
                    break
                line, c, o = transform_blob(raw)
                gz.write(line)
                end += len(raw)
                omitted += o
                if c:
                    _merge_counts(counts, c)
        digest = w.h.hexdigest()
    return {"bytes": os.path.getsize(dst), "sha256": digest, "counts": counts, "omitted": omitted, "end": end}


def pack_json(src, dst, limit):
    """.json（1 つの JSON）を一覧の時点のサイズまで読んで変換して gzip。読めなければ None（送らない）。"""
    with open_nofollow(src) as f:
        raw = f.read(limit)
    if len(raw) != limit or not _is_json(raw):
        return None
    data, counts, omitted = transform_blob(raw)
    gz = gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)
    with open(dst, "wb") as f:
        f.write(gz)
    return {"bytes": len(gz), "sha256": hashlib.sha256(gz).hexdigest(), "counts": counts, "omitted": omitted,
            "end": len(raw)}


def pack_session(item, tmp):
    """送る会話 1 本を、一覧の時点の中身（本体は offset まで・サブエージェントは当時のファイルとサイズまで）で固める。"""
    sid = item["session_id"]
    path = check_main_path(item.get("path"), sid)
    if os.lstat(path).st_size < int(item["offset"]):
        raise Fail("会話ファイルが一覧のあとで書き換わった（%d 番）。/send-to-nobu で一覧を出し直して" % item["n"])
    base = os.path.join(tmp, sid)
    os.makedirs(base)
    main = pack_jsonl(path, base + ".jsonl.gz", int(item["offset"]))
    counts = dict(main["counts"])
    omitted = main["omitted"]
    files = [{"session_id": sid, "rel": None, "bytes": main["bytes"], "sha256": main["sha256"],
              "local": base + ".jsonl.gz"}]
    sub_meta = []
    sdir = session_dir_of(path)
    for i, (rel, size) in enumerate(item.get("subs") or []):
        p = sub_path(sdir, rel)
        if p is None:
            continue  # 消えた・リンクになった
        dst = os.path.join(base, "%04d.gz" % i)
        r = (pack_jsonl if rel.endswith(".jsonl") else pack_json)(p, dst, int(size))
        if r is None:
            continue
        _merge_counts(counts, r["counts"])
        omitted += r["omitted"]
        files.append({"session_id": sid, "rel": rel, "bytes": r["bytes"], "sha256": r["sha256"], "local": dst})
        sub_meta.append({"rel": rel, "bytes": r["bytes"], "sha256": r["sha256"]})
    return {
        "item": item,
        "files": files,
        "sent": {
            "session_id": sid,
            "bytes": main["bytes"],
            "sha256": main["sha256"],
            "title": item.get("title", "")[:TITLE_MAX],
            "project": item.get("project", "")[:PROJECT_MAX],
            "last_activity": item.get("last_activity"),
            "redactions": sum(counts.values()),
            "subagents": sub_meta,
        },
        # 続き（一覧のあとに増えた分）は翌日の一覧に出る
        "record": {"offset": main["end"], "size": item["size"], "mtime": item["mtime"],
                   "sub_n": item["sub_n"], "sub_bytes": item["sub_bytes"]},
        "counts": counts,
        "omitted": omitted,
    }


# ---------------------------------------------------------------- HTTP


class ApiError(Fail):
    def __init__(self, status, code, message):
        exit_code = EXIT_UNAUTHORIZED if code == "unauthorized" or status == 401 else EXIT_ERROR
        super().__init__(message, exit_code)
        self.status = status
        self.api_code = code


def _user_agent():
    return "send-to-nobu/%s" % plugin_version()


SYSTEM_CA_BUNDLE = "/etc/ssl/cert.pem"
_OPENER = None


def _ssl_context():
    """既定の検証先で CA 証明書が読めない Python（python.org 版で証明書を入れていない等）は
    macOS のシステムバンドルに落とす。"""
    ctx = ssl.create_default_context()
    try:
        empty = ctx.cert_store_stats().get("x509_ca", 0) == 0
    except Exception:
        empty = True
    if empty and os.path.exists(SYSTEM_CA_BUNDLE):
        ctx.load_verify_locations(cafile=SYSTEM_CA_BUNDLE)
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトは追わない（引換券やファイルが別のホストに渡らないように）。"""

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


def _backoff(attempt):
    time.sleep(BACKOFF_BASE * (2 ** attempt))


def _read_api_error(e):
    try:
        payload = json.loads(e.read().decode("utf-8", "replace"))
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return str(err.get("code") or ""), str(err.get("message") or "")
    except Exception:
        pass
    return "", ""


def api_post(api_base, path, body, code, retry_ok_codes=()):
    """引換券つきで JSON を POST する。5xx と通信エラーだけ指数バックオフでリトライ（4xx はしない）。

    retry_ok_codes のエラーは、同じ送信のリトライ（2 回目以降）のときだけ成功扱い
    （/v1/finish の already_finished = 前の試行で送信票が置けていた）。1 回目から返ってきたら使用済みの引換券。
    """
    url = api_base.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + code, "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": _user_agent()}
    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with _open(req, 60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            api_code, message = _read_api_error(e)
            if api_code in retry_ok_codes and attempt > 0:
                return {}
            if api_code == "already_finished":
                raise ApiError(401, "unauthorized", "その引換券は使用済み。start_submission からやり直して（%s %d）"
                               % (path, e.code))
            if e.code >= 500 and attempt < RETRIES:
                _backoff(attempt)
                continue
            if 300 <= e.code < 400:
                message = "サーバーが別の場所へ飛ばそうとしたので止めた"
            if e.code == 401 or api_code == "unauthorized":
                message = "引換券が使えない（期限切れか使用済み）。start_submission からやり直して"
            raise ApiError(e.code, api_code, "%s（%s %d %s）" % (message or "サーバーが受け付けなかった",
                                                                  path, e.code, api_code or "-"))
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            if attempt < RETRIES:
                _backoff(attempt)
                continue
            reason = getattr(e, "reason", None)
            raise Fail("サーバーにつながらない（%s）: %s" % (path, type(reason or e).__name__))
    raise Fail("サーバーにつながらない（%s）" % path)


def put_file(upload, local, size):
    """署名 URL に PUT する。headers はそのまま付ける。ファイルからストリーミング。
    GCS の案内どおり 408・429・5xx と通信エラーはリトライ。リダイレクトは追わない。"""
    headers = dict(upload.get("headers") or {})
    headers["Content-Length"] = str(size)
    method = upload.get("method") or "PUT"
    last = None
    for attempt in range(RETRIES + 1):
        try:
            with open(local, "rb") as fh:
                req = urllib.request.Request(upload["url"], data=fh, method=method, headers=headers)
                with _open(req, 300) as r:
                    r.read()
            return
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if (e.code in RETRYABLE_PUT_STATUS or e.code >= 500) and attempt < RETRIES:
                _backoff(attempt)
                continue
            break
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            last = type(getattr(e, "reason", None) or e).__name__
            if attempt < RETRIES:
                _backoff(attempt)
                continue
            break
    raise Fail("アップロードに失敗した（%s）" % last)


def put_all(jobs):
    """(upload, local, size) を並列 4 本まで PUT。1 つでも失敗したら止める。"""
    if not jobs:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=PUT_WORKERS) as ex:
        futures = [ex.submit(put_file, *j) for j in jobs]
        done, pending = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_EXCEPTION)
        for f in pending:
            f.cancel()
        for f in futures:
            if f.done() and not f.cancelled() and f.exception() is not None:
                raise f.exception()


def check_api_base(api_base):
    """api_base は AI を経由して渡るので、決まった送り先以外は拒否する（引換券とファイルを外に出さない）。"""
    base = (api_base or "").strip().rstrip("/")
    if base not in ALLOWED_API_BASES:
        raise Fail("--api-base が決まった送り先ではない（start_submission の api_base をそのまま渡して）", EXIT_USAGE)
    return base


def check_put_url(url):
    if not isinstance(url, str) or not any(url.startswith(p) for p in ALLOWED_PUT_PREFIXES):
        raise Fail("サーバーが返したアップロード先が決まった場所ではないので止めた")


# ---------------------------------------------------------------- send


def parse_numbers(text, numbers, what):
    """番号の並び（1,3 / 1 3 / 2-4）。none / なし は空。読めない・逆順・一覧にない番号はエラー（失敗側に倒す）。"""
    t = unicodedata.normalize("NFKC", text or "").strip()
    if t.lower() == "none" or t == "なし":
        return set()
    if not t:
        raise Fail("--%s が空。番号をカンマ区切りで（なければ none）" % what, EXIT_USAGE)
    out = set()
    for tok in re.split(r"[\s,、]+", t):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
        if not m:
            raise Fail("--%s が読めない: %s（番号をカンマ区切りで。なければ none）" % (what, tok), EXIT_USAGE)
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if hi < lo:
            raise Fail("--%s の範囲が逆順: %s" % (what, tok), EXIT_USAGE)
        for n in range(lo, hi + 1):
            if n not in numbers:
                raise Fail("%d 番は一覧にない" % n, EXIT_USAGE)
            out.add(n)
    if not out:
        raise Fail("--%s が読めない（番号をカンマ区切りで。なければ none）" % what, EXIT_USAGE)
    return out


def read_note(args, stdin):
    if args.note_file is None:
        return ""
    if args.note_file != "-":
        raise Fail("--note-file は - （標準入力）だけ", EXIT_USAGE)
    return stdin.read().strip()


def load_pending(data_dir, current, now):
    p = read_json(pending_path(data_dir), None)
    if not isinstance(p, dict) or not isinstance(p.get("items"), list):
        raise Fail("一覧の控えがない。先に /send-to-nobu で一覧を出して", EXIT_USAGE)
    if p.get("v") != PENDING_VERSION:
        raise Fail("一覧の控えが古い形。/send-to-nobu で一覧を出し直して", EXIT_USAGE)
    if p.get("session") != current:
        raise Fail("一覧の控えが別の会話のもの。この会話で /send-to-nobu をやり直して", EXIT_USAGE)
    created = p.get("created_ts")
    if not isinstance(created, (int, float)) or now - created > PENDING_TTL or now < created - 300:
        raise Fail("一覧の控えが古い（6 時間以上前）。/send-to-nobu で一覧を出し直して", EXIT_USAGE)
    return p


def excluded_uuid_hashes(item):
    """外した会話の uuid の短いハッシュ（外したときの位置まで）。"""
    out = set()
    limit = int(item.get("offset", 0))
    try:
        path = check_main_path(item.get("path"), item.get("session_id"))
        with open_nofollow(path) as fh:
            pos = 0
            for raw in fh:
                pos += len(raw)
                if pos > limit:
                    break
                d = parse_line(raw)
                if d is not None and isinstance(d.get("uuid"), str):
                    out.add(uuid_hash(d["uuid"]))
    except (OSError, Fail):
        pass
    return sorted(out)


def conversation_record(state, sid):
    c = state.get("conversations")
    rec = c.get(sid) if isinstance(c, dict) else None
    return rec if isinstance(rec, dict) else {}


def record_decisions(data_dir, now, packed, excluded_items, sid, note_sent):
    """送った / 外したを、判断した時点（一覧の時点）の位置で状態に記録する。この会話のラウンド数と感想の有無も。"""
    state = load_state(data_dir, now)
    sessions = state["sessions"]
    convs = state.get("conversations") if isinstance(state.get("conversations"), dict) else {}
    conv = dict(convs.get(sid) or {})
    conv["rounds"] = int(conv.get("rounds") or 0) + 1
    conv["note_sent"] = bool(conv.get("note_sent") or note_sent)
    conv["at"] = now
    convs[sid] = conv
    # 古い会話の記録は捨てる（30 日）
    state["conversations"] = {k: v for k, v in convs.items()
                              if isinstance(v, dict) and now - float(v.get("at") or 0) < 30 * 86400}
    store = load_excluded_store(data_dir)
    at = iso_utc(now)
    for p in packed:
        rec = dict(p["record"])
        rec.update({"d": "sent", "at": at})
        sessions[p["item"]["session_id"]] = rec
        store.pop(p["item"]["session_id"], None)
    for it in excluded_items:
        sessions[it["session_id"]] = {"d": "excluded", "at": at, "offset": it["offset"], "size": it["size"],
                                      "mtime": it["mtime"], "sub_n": it["sub_n"], "sub_bytes": it["sub_bytes"]}
        store[it["session_id"]] = excluded_uuid_hashes(it)
    write_json_atomic(excluded_path(data_dir), store)
    write_json_atomic(state_path(data_dir), state)


_CLEANUP_DIRS = []


def _on_signal(signum, frame):
    """SIGTERM などで止められても一時ディレクトリを消す。"""
    for d in list(_CLEANUP_DIRS):
        shutil.rmtree(d, ignore_errors=True)
    os._exit(128 + signum)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep_old_packs(data_dir, now):
    """前回の送信が途中で止まって残った一時ディレクトリを消す。"""
    try:
        names = os.listdir(data_dir)
    except OSError:
        return
    for name in names:
        if not name.startswith("pack-"):
            continue
        p = os.path.join(data_dir, name)
        m = re.match(r"^pack-(\d+)-", name)
        try:
            old = now - os.lstat(p).st_mtime > 86400
        except OSError:
            continue
        if old or not m or not _pid_alive(int(m.group(1))):
            shutil.rmtree(p, ignore_errors=True)


def cmd_send(args, out, stdin):
    data_dir = resolve_data_dir(args.data_dir)
    sid = session_id_from_env()
    cur_path = require_send_session(sid)
    api_base = check_api_base(args.api_base) if args.api_base is not None else None
    if args.note_file is not None and args.note_file != "-":
        raise Fail("--note-file は - （標準入力）だけ", EXIT_USAGE)
    now = _now()
    load_state(data_dir, now)
    load_excluded_store(data_dir)
    pending = load_pending(data_dir, sid, now)

    # 一覧を出したあとに本人の返事が無ければ送らない（同じターンで勝手に送らせない）
    if not reply_after(cur_path, int(pending.get("session_size") or 0)):
        raise Fail("まだ本人の返事が無い。一覧を見せて、返事を待ってから送って", EXIT_NOT_ANSWERED)

    items = {int(it["n"]): it for it in pending["items"]}
    excluded = parse_numbers(args.exclude, items, "exclude")
    included = parse_numbers(args.include, items, "include") if args.include is not None else set()
    if excluded & included:
        raise Fail("同じ番号が --exclude と --include の両方にある: %s" % ",".join(map(str, sorted(excluded & included))),
                   EXIT_USAGE)
    # 前に外した会話の続き・前に外した会話を引き継いだ会話は、--include で明示されない限り外す
    # 前に外した会話の続き・前に外した会話を引き継いだ会話は、--include で明示されない限り外す
    default_excluded = {n for n, it in items.items() if it.get("previously_excluded") or it.get("contains_excluded_copy")}
    excluded |= (default_excluded - included)
    send_items = [items[n] for n in sorted(items) if n not in excluded]
    excluded_items = [items[n] for n in sorted(excluded)]

    if not args.confirm_shared:
        cut = [it["n"] for it in send_items if it.get("group_cut")]
        if cut:
            raise Fail("%s 番は、大きすぎて一覧に出しきれなかった会話と同じ履歴を含む。送るとその中身も届く。"
                       "了承なら --confirm-shared を付けてやり直す" % "、".join(map(str, cut)), EXIT_CONFIRM_SHARED)
        pairs = sorted({(it["n"], m) for it in send_items for m in it.get("shares", []) if m in excluded})
        if pairs:
            desc = "、".join("%d 番と %d 番" % pr for pr in pairs)
            raise Fail("%s は同じ履歴を共有している。外した方の中身も、送る方から届く。"
                       "了承なら --confirm-shared を付けてやり直す（止めるなら両方外す）" % desc, EXIT_CONFIRM_SHARED)

    note = mask_text(read_note(args, stdin))[0]
    if len(note) > NOTE_MAX:
        raise Fail("感想が長すぎる（%d 文字まで）" % NOTE_MAX, EXIT_USAGE)

    if not send_items and not note:
        # 全部外して感想もない日: サーバーには何も送らず、外したことだけ覚える
        record_decisions(data_dir, now, [], excluded_items, sid, False)
        remove_quietly(pending_path(data_dir))
        out.write(json.dumps({"submission_id": None, "sent_count": 0, "excluded_count": len(excluded_items),
                              "subagent_count": 0, "bytes": 0, "redactions": 0, "omitted": 0,
                              "remaining": int(pending.get("remaining") or 0)}, ensure_ascii=False) + "\n")
        return 0

    if not args.code or not api_base:
        raise Fail("--code と --api-base がない。start_submission で受け取って", EXIT_USAGE)
    code = args.code.strip()

    sweep_old_packs(data_dir, now)
    tmp = tempfile.mkdtemp(prefix="pack-%d-" % os.getpid(), dir=data_dir)
    _CLEANUP_DIRS.append(tmp)
    saved = {}
    for signame in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, signame, None)
        if signum is not None:
            try:
                saved[signum] = signal.signal(signum, _on_signal)
            except ValueError:  # メインスレッド以外
                pass
    try:
        packed = [pack_session(it, tmp) for it in send_items]
        files = [f for p in packed for f in p["files"]]

        uploads = {}
        for i in range(0, len(files), UPLOAD_BATCH):
            chunk = files[i:i + UPLOAD_BATCH]
            body = {"files": [{"session_id": f["session_id"], "rel": f["rel"], "bytes": f["bytes"],
                               "sha256": f["sha256"]} for f in chunk]}
            res = api_post(api_base, "/v1/uploads", body, code)
            for u in (res.get("uploads") or []):
                if isinstance(u, dict):
                    uploads[(u.get("session_id"), u.get("rel") or None)] = u
        jobs = []
        for f in files:
            u = uploads.get((f["session_id"], f["rel"]))
            if not u or not u.get("url"):
                raise Fail("サーバーからアップロード先が返ってこなかった")
            check_put_url(u["url"])
            jobs.append((u, f["local"], f["bytes"]))
        put_all(jobs)

        body = {"sent": [p["sent"] for p in packed], "excluded_count": len(excluded_items),
                "note": note, "plugin_version": plugin_version()}
        res = api_post(api_base, "/v1/finish", body, code, retry_ok_codes=("already_finished",))

        record_decisions(data_dir, now, packed, excluded_items, sid, bool(note))
        remove_quietly(pending_path(data_dir))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _CLEANUP_DIRS.remove(tmp)
        for signum, handler in saved.items():
            signal.signal(signum, handler)

    result = {
        "submission_id": res.get("submission_id"),
        "sent_count": len(packed),
        "excluded_count": len(excluded_items),
        "subagent_count": sum(len(p["sent"]["subagents"]) for p in packed),
        "bytes": sum(f["bytes"] for f in files),
        "redactions": sum(p["sent"]["redactions"] for p in packed),
        "omitted": sum(p["omitted"] for p in packed),
        "remaining": int(pending.get("remaining") or 0),
    }
    out.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


# ---------------------------------------------------------------- main


def build_parser():
    p = argparse.ArgumentParser(prog="agentlog.py", description="send-to-nobu の処理")
    sub = p.add_subparsers(dest="cmd")

    def data_dir(sp):
        sp.add_argument("--data-dir", default=None, help="状態を置くディレクトリ（${CLAUDE_PLUGIN_DATA}）")

    data_dir(sub.add_parser("nudge", help="未送信の会話があれば 1 日 1 回知らせる（フック用）"))
    data_dir(sub.add_parser("status", help="いまの会話に有効な一覧の控えがあるか"))

    sp = sub.add_parser("list", help="未送信の会話の一覧")
    data_dir(sp)
    sp.add_argument("--preview", action="store_true", help="人の指示の抜粋（マスク済み）も出す")

    sp = sub.add_parser("send", help="一覧の控えに沿って送る")
    data_dir(sp)
    sp.add_argument("--code", default=None, help="start_submission の upload_code")
    sp.add_argument("--exclude", required=True, help="外す番号（カンマ区切り）か none")
    sp.add_argument("--include", default=None, help="既定で外す会話のうち、送る番号")
    sp.add_argument("--note-file", default=None, help="感想は標準入力から（- だけ）")
    sp.add_argument("--api-base", default=None, help="start_submission の api_base")
    sp.add_argument("--confirm-shared", action="store_true", help="履歴を共有する会話の片方だけ外すのを了承済み")
    return p


def main(argv=None, stdin=None, stdout=None, stderr=None):
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if argv[:1] == ["nudge"]:
        # フックは引数の不備でも起動を邪魔しない（usage も出さない）
        saved, sys.stderr = sys.stderr, open(os.devnull, "w")
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            return 0
        finally:
            sys.stderr.close()
            sys.stderr = saved
        return cmd_nudge(args, stdout)
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    if args.cmd is None:
        parser.print_help(stderr)
        return EXIT_USAGE
    try:
        if args.cmd == "status":
            return cmd_status(args, stdout)
        if args.cmd == "list":
            return cmd_list(args, stdout)
        return cmd_send(args, stdout, stdin)
    except Fail as e:
        stderr.write("send-to-nobu: " + e.message + "\n")
        return e.code
    except Exception as e:  # 想定外でも本文は出さず 1 行で（調べるときは SEND_TO_NOBU_DEBUG=1）
        if os.environ.get("SEND_TO_NOBU_DEBUG"):
            import traceback
            traceback.print_exc(file=stderr)
        stderr.write("send-to-nobu: 想定外のエラー（%s）\n" % type(e).__name__)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
