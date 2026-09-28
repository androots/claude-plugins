#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""send-to-nobu: Claude Code の会話ログを のぶろう に送るための処理一式。

python3 3.9 の標準ライブラリだけで動く（macOS の /usr/bin/python3 を想定）。

サブコマンド:
  nudge  SessionStart フック。未送信の会話があれば 1 日 1 回だけ 1 行知らせる
  list   未送信の会話の一覧を出し、控えを保存する（--preview で人の指示の抜粋）
  send   控えに沿って、マスク → gzip → 引換券でアップロード → 送信票

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
PREVIEW_CHARS = 160         # 抜粋 1 つの最大文字数
PREVIEW_BUDGET = 24000      # 一覧全体の抜粋の文字数の目安（会話が多い日は 1 会話あたりを減らす）
TITLE_FALLBACK_CHARS = 40
TITLE_MAX = 200
PROJECT_MAX = 300
NOTE_MAX = 20000
NUDGE_HEAD_BYTES = 1024 * 1024  # nudge が「送信用の会話か」を見るために読む先頭の量
NUDGE_TAIL_BYTES = 256 * 1024

UPLOAD_BATCH = 100
PUT_WORKERS = 4
RETRIES = 3                 # 最初の 1 回 + リトライ 3 回
BACKOFF_BASE = 1.0          # 1, 2, 4 秒（テストでは小さくする）
RETRYABLE_PUT_STATUS = {408, 429}
# 送り先はこれだけ（引数や env では広げられない。テストはコードから差し替える）
ALLOWED_API_BASES = ("https://agent-log-inbox-mcp.androots.co.jp",)
GZIP_LEVEL = 6

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIRM_SHARED = 3
EXIT_UNAUTHORIZED = 4


class Fail(Exception):
    """利用者（AI）に 1 行で伝えるエラー。"""

    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.message = message
        self.code = code


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
    if rest in ("", "Z", "z"):
        pass
    else:
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


def projects_dir(override=None):
    if override:
        return override
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def resolve_data_dir(arg):
    """データディレクトリは --data-dir で受け取る。env の CLAUDE_PLUGIN_DATA は読まない
    （Bash ツールの env には他プラグインの値が漏れていることがある）。"""
    if not arg:
        return os.path.join(os.path.expanduser("~"), ".claude", "send-to-nobu")
    path = os.path.abspath(os.path.expanduser(arg))
    # 置換されずに他プラグインのディレクトリを指したときに、そこへ書かないための歯止め
    if "send-to-nobu" not in os.path.basename(path.rstrip(os.sep)):
        raise Fail("--data-dir がこのプラグインのディレクトリではない: %s" % os.path.basename(path), EXIT_USAGE)
    return path


def current_session(arg):
    return arg or os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def home_short(path):
    if not isinstance(path, str) or not path:
        return ""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


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
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + ending, counts


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
_NOT_HUMAN_PREFIXES = (
    "[Request interrupted by user",
    "<task-notification>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)
_LOCAL_STDOUT_PREFIXES = ("<local-command-stdout>", "<local-command-stderr>")


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
      human … 人の指示
      cmd   … 先頭が <command-name>。次の user 行が <local-command-stdout> なら組み込みコマンド
      None  … 人の指示ではない
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
        return "cmd", t
    return "human", t


def _is_local_stdout(d):
    t = user_text(d)
    return isinstance(t, str) and t.lstrip().startswith(_LOCAL_STDOUT_PREFIXES)


_TAG_RE = {
    name: re.compile(r"<%s>(.*?)</%s>" % (name, name), re.S)
    for name in ("command-name", "command-args", "bash-input")
}


def display_text(text):
    """抜粋・タイトル用に整える（スキル呼び出しは `/name 引数` の形に）。マスク前。"""
    head = text.lstrip()
    if head.startswith(("<command-message>", "<command-name>")):
        m = _TAG_RE["command-name"].search(text)
        if m:
            a = _TAG_RE["command-args"].search(text)
            return (m.group(1).strip() + " " + (a.group(1).strip() if a else "")).strip()
    if head.startswith("<bash-input>"):
        m = _TAG_RE["bash-input"].search(text)
        if m:
            return "!" + m.group(1).strip()
    return text


def squash(text, limit):
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


class Scan(object):
    """会話ファイル 1 本を読んだ結果。"""

    def __init__(self):
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

    def add_prompt(self, text):
        if self.first_kind is None:
            self.first_kind = "human"
        self.prompt_count += 1
        if len(self.first_prompts) < PREVIEW_MAX:
            self.first_prompts.append(text)
        self.last_prompts.append(text)

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
        if self.prompt_count <= limit:
            return list(self.first_prompts[:self.prompt_count])
        head = (limit * 2 + 2) // 3
        tail = limit - head
        tail_items = list(self.last_prompts)[-tail:] if tail else []
        return self.first_prompts[:head] + tail_items


def scan_session(path, head_bytes=None, stop_at_first=False):
    """会話ファイルを先頭から読む。head_bytes があればそのくらいまで。"""
    s = Scan()
    pending_cmd = None
    with open(path, "rb") as fh:
        for i, raw in enumerate(fh):
            if head_bytes is not None and s.end >= head_bytes:
                break
            d = parse_line(raw)
            if not raw.endswith(b"\n") and d is None:
                break  # 書きかけの最終行
            s.end += len(raw)
            if d is None:
                continue
            u = d.get("uuid")
            if isinstance(u, str):
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
            elif t == "custom-title" and isinstance(d.get("customTitle"), str):
                s.custom_title = d["customTitle"]
            elif t == "ai-title" and isinstance(d.get("aiTitle"), str):
                s.ai_title = d["aiTitle"]
            if t != "user" or d.get("isSidechain") is True:
                continue
            if pending_cmd is not None:
                if _is_local_stdout(d):
                    pending_cmd = None  # 組み込みコマンド（/model /mcp など）
                    continue
                s.add_prompt(pending_cmd)
                pending_cmd = None
            kind, text = classify(d)
            if kind == "send":
                if s.first_kind is None:
                    s.first_kind = "send"
            elif kind == "cmd":
                pending_cmd = text
            elif kind == "human":
                s.add_prompt(text)
            if stop_at_first and s.first_kind is not None:
                return s
    if pending_cmd is not None:
        s.add_prompt(pending_cmd)
    return s


def has_turn_after(path, offset):
    """offset 以降に user / assistant 行があるか（閉じただけで足されたメタ行は数えない）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size < offset:
        return True  # 書き直された
    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            d = parse_line(raw)
            if d is not None and d.get("type") in ("user", "assistant"):
                return True
    return False


def tail_last_ts(path, size):
    """末尾だけ読んで、最後の user / assistant 行の時刻を返す（nudge 用の軽い版）。"""
    start = max(0, size - NUDGE_TAIL_BYTES)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read(size - start)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    best = None
    for raw in lines:
        d = parse_line(raw) if raw.strip() else None
        if d is not None and d.get("type") in ("user", "assistant"):
            ts = parse_ts(d.get("timestamp"))
            if ts is not None and (best is None or ts > best):
                best = ts
    return best


def subagent_files(session_dir):
    """<session>/subagents/ 以下の送る対象（.jsonl と .json）。[(rel, path, size)]。

    入れ子（workflows/wf_*/…）・.meta.json・journal.jsonl も含む。tool-results/ などは送らない。
    """
    root = os.path.join(session_dir, "subagents")
    out = []
    if not os.path.isdir(root) or os.path.islink(root):
        return out
    for dp, dns, fns in os.walk(root):
        rel_dir = os.path.relpath(dp, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        dns[:] = sorted(d for d in dns if SEG_RE.match(d) and d not in (".", "..")
                        and len(parts) + 2 <= MAX_REL_DEPTH)
        for fn in sorted(fns):
            if not (fn.endswith(".jsonl") or fn.endswith(".json")):
                continue
            if not SEG_RE.match(fn) or fn in (".", ".."):
                continue
            segs = parts + [fn]
            if len(segs) > MAX_REL_DEPTH:
                continue
            p = os.path.join(dp, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            out.append(("/".join(segs), p, st.st_size))
    return out


def sub_signature(files):
    return [len(files), sum(f[2] for f in files)]


def iter_sessions(pdir):
    """projects/*/<uuid>.jsonl を列挙する。同じ会話 ID が複数あれば新しい方。"""
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


def uuid_hash(u):
    return hashlib.sha256(u.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 状態


def state_path(data_dir):
    return os.path.join(data_dir, "state.json")


def pending_path(data_dir):
    return os.path.join(data_dir, "pending.json")


def excluded_path(data_dir):
    return os.path.join(data_dir, "excluded-uuids.json")


def load_state(data_dir, now):
    """状態を読む。無ければ「いま − 7 日」を基準に作って保存する。"""
    st = read_json(state_path(data_dir), None)
    if not isinstance(st, dict) or not isinstance(st.get("baseline"), (int, float)):
        st = {"v": 1, "baseline": now - FIRST_RUN_DAYS * 86400, "sessions": {}}
        write_json_atomic(state_path(data_dir), st)
    if not isinstance(st.get("sessions"), dict):
        st["sessions"] = {}
    return st


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
                sig = sub_signature(subagent_files(path[:-len(".jsonl")]))
                if sig == [rec.get("sub_n", 0), rec.get("sub_bytes", 0)]:
                    continue
            out.append((sid, path, st, rec))
        elif st.st_mtime >= baseline:
            out.append((sid, path, st, None))
    return out


# ---------------------------------------------------------------- nudge


def count_unsent_fast(state, pdir, current):
    """stat と状態の比較 + 先頭・末尾の少しだけで数える（起動を遅らせない）。"""
    n = 0
    for sid, path, st, rec in candidates(state, pdir, current):
        if rec is not None:
            n += 1
            continue
        s = scan_session(path, head_bytes=NUDGE_HEAD_BYTES, stop_at_first=True)
        if s.first_kind == "send":
            continue  # 送信用の会話
        if s.first_kind is None and s.end < NUDGE_HEAD_BYTES:
            continue  # 最後まで読んでも人の指示が無い
        # 先頭で決まらない大きな会話（自動の作業が長く続いてから人の指示が来る等）は数えておく
        last = tail_last_ts(path, st.st_size)
        if (last if last is not None else st.st_mtime) < state["baseline"]:
            continue  # 古い会話を開いて閉じただけ
        n += 1
    return n


def cmd_nudge(args, out):
    try:
        now = args.now if args.now is not None else time.time()
        data_dir = resolve_data_dir(args.data_dir)
        state = load_state(data_dir, now)
        today = day_key(now)
        if state.get("nudged_day") == today:
            return 0
        n = count_unsent_fast(state, projects_dir(args.projects_dir), current_session(None))
        if n <= 0:
            return 0
        msg = "未送信の会話が %d 件 → /send-to-nobu で のぶろう に送れます" % n
        out.write(json.dumps({"systemMessage": msg}, ensure_ascii=False) + "\n")
        fresh = read_json(state_path(data_dir), state)
        if not isinstance(fresh, dict):
            fresh = state
        fresh["nudged_day"] = today
        write_json_atomic(state_path(data_dir), fresh)
    except Exception:
        pass  # 起動を邪魔しない
    return 0


# ---------------------------------------------------------------- list


def build_list(state, pdir, current, excluded_store):
    """一覧の中身を作る。(items, scans)。items は控えにそのまま入る形。"""
    rows = []
    for sid, path, st, rec in candidates(state, pdir, current):
        try:
            s = scan_session(path)
        except OSError:
            continue
        if s.prompt_count == 0 or s.first_kind == "send":
            continue
        last = s.last_ts if s.last_ts is not None else st.st_mtime
        if rec is None and last < state["baseline"]:
            continue  # 基準より前の会話を開いて閉じただけ
        subs = subagent_files(path[:-len(".jsonl")])
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
        for h in hashes:
            excluded_hashes.setdefault(h, set()).add(esid)

    items = []
    for idx, (last, sid, path, st, rec, s, subs) in enumerate(rows):
        n = idx + 1
        sig = sub_signature(subs)
        item = {
            "n": n,
            "session_id": sid,
            "path": path,
            "offset": s.end,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "sub_n": sig[0],
            "sub_bytes": sig[1],
            "title": s.title(),
            "project": squash(mask_text(home_short(s.cwd))[0], PROJECT_MAX),
            "last_activity": iso_utc(last),
            "last_ts": last,
            "prompt_count": s.prompt_count,
            "total_bytes": st.st_size + sig[1],
            "shares": sorted(o + 1 for o in shares.get(idx, ())),
            "previously_excluded": bool(rec and rec.get("d") == "excluded"),
            "contains_excluded_copy": any(
                (excluded_hashes.get(uuid_hash(u), set()) - {sid}) for u in s.head_uuids),
        }
        items.append(item)
    return items, [r[5] for r in rows]


def cmd_list(args, out):
    now = args.now if args.now is not None else time.time()
    data_dir = resolve_data_dir(args.data_dir)
    state = load_state(data_dir, now)
    current = current_session(args.session)
    excluded_store = read_json(excluded_path(data_dir), {})
    if not isinstance(excluded_store, dict):
        excluded_store = {}
    items, scans = build_list(state, projects_dir(args.projects_dir), current, excluded_store)

    pending = {"v": 1, "session": current, "created_at": iso_utc(now), "created_ts": now,
               "items": [{k: v for k, v in it.items() if k not in ("prompt_count", "total_bytes", "last_ts")}
                         for it in items]}
    write_json_atomic(pending_path(data_dir), pending)

    per_item = PREVIEW_MAX
    if items:
        per_item = max(3, min(PREVIEW_MAX, PREVIEW_BUDGET // (len(items) * PREVIEW_CHARS)))
    shown = []
    for it, s in zip(items, scans):
        row = {
            "n": it["n"],
            "session_id": it["session_id"],
            "title": it["title"],
            "project": it["project"],
            "updated": local_short(it["last_ts"]),
            "size": human_size(it["total_bytes"]),
            "prompts": it["prompt_count"],
        }
        if it["sub_n"]:
            row["subagent_files"] = it["sub_n"]
        if it["previously_excluded"]:
            row["previously_excluded"] = True
        if it["contains_excluded_copy"]:
            row["contains_excluded_copy"] = True
        if it["shares"]:
            row["shares_history_with"] = it["shares"]
        if args.preview:
            picked = s.prompts_for_preview(per_item)
            row["preview"] = [squash(mask_text(display_text(t))[0], PREVIEW_CHARS) for t in picked]
            if s.prompt_count > len(picked):
                row["preview_omitted"] = s.prompt_count - len(picked)
        shown.append(row)
    result = {"count": len(shown), "items": shown}
    if not state["sessions"]:
        result["first_run"] = True
        result["since"] = local_short(state["baseline"])
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


def pack_jsonl(src, dst):
    """JSONL を 1 行ずつマスクして gzip（mtime=0 で sha256 を安定させる）。

    戻り値 (gzip 後のバイト数, sha256, {種類: 件数}, 読んだ位置)。書きかけの最終行は含めない。
    """
    counts = {}
    end = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        w = _HashWriter(fout)
        with gzip.GzipFile(filename="", mode="wb", fileobj=w, mtime=0, compresslevel=GZIP_LEVEL) as gz:
            for raw in fin:
                if not raw.endswith(b"\n") and parse_line(raw) is None:
                    break
                line, c = mask_blob(raw)
                gz.write(line)
                end += len(raw)
                if c:
                    _merge_counts(counts, c)
        digest = w.h.hexdigest()
    return os.path.getsize(dst), digest, counts, end


def pack_json(src, dst):
    """.json（1 つの JSON）を丸ごとマスクして gzip。"""
    with open(src, "rb") as f:
        raw = f.read()
    masked, counts = mask_blob(raw)
    data = gzip.compress(masked, compresslevel=GZIP_LEVEL, mtime=0)
    with open(dst, "wb") as f:
        f.write(data)
    return len(data), hashlib.sha256(data).hexdigest(), counts, len(raw)


def pack_session(item, tmp):
    """送る会話 1 本（本体 + サブエージェント）を一時ディレクトリに固める。"""
    path = item["path"]
    try:
        st = os.stat(path)
    except OSError:
        raise Fail("会話ファイルが見つからない（%d 番）。/send-to-nobu で一覧を出し直して" % item["n"])
    sid = item["session_id"]
    subs = subagent_files(path[:-len(".jsonl")])
    base = os.path.join(tmp, sid)
    os.makedirs(base)
    size, digest, counts, end = pack_jsonl(path, base + ".jsonl.gz")
    files = [{"session_id": sid, "rel": None, "bytes": size, "sha256": digest, "local": base + ".jsonl.gz"}]
    sub_meta = []
    for i, (rel, p, _size) in enumerate(subs):
        dst = os.path.join(base, "%04d.gz" % i)
        packer = pack_jsonl if rel.endswith(".jsonl") else pack_json
        b, d, c, _ = packer(p, dst)
        _merge_counts(counts, c)
        files.append({"session_id": sid, "rel": rel, "bytes": b, "sha256": d, "local": dst})
        sub_meta.append({"rel": rel, "bytes": b, "sha256": d})
    sig = sub_signature(subs)
    return {
        "item": item,
        "files": files,
        "sent": {
            "session_id": sid,
            "bytes": size,
            "sha256": digest,
            "title": item.get("title", "")[:TITLE_MAX],
            "project": item.get("project", "")[:PROJECT_MAX],
            "last_activity": item.get("last_activity"),
            "redactions": sum(counts.values()),
            "subagents": sub_meta,
        },
        "record": {"offset": end, "size": st.st_size, "mtime": int(st.st_mtime),
                   "sub_n": sig[0], "sub_bytes": sig[1]},
        "counts": counts,
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


def _open(req, timeout):
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()))
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


def api_post(api_base, path, body, code, ok_codes=()):
    """引換券つきで JSON を POST する。5xx と通信エラーだけ指数バックオフでリトライ（4xx はしない）。

    ok_codes のエラーは成功扱い（/v1/finish の already_finished = 送信票はもう置かれている）。
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
            if api_code in ok_codes:
                return {}
            if e.code >= 500 and attempt < RETRIES:
                _backoff(attempt)
                continue
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
    GCS の案内どおり 408・429・5xx と通信エラーはリトライ。"""
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


# ---------------------------------------------------------------- send


def parse_exclude(text, numbers):
    t = unicodedata.normalize("NFKC", (text or "")).strip().lower()
    if t in ("", "none", "なし", "無し", "ない", "0"):
        return set()
    out = set()
    for tok in re.split(r"[\s,、，;/]+", t):
        if not tok:
            continue
        m = re.match(r"^(\d+)(?:-(\d+))?$", tok)
        if not m:
            raise Fail("--exclude は番号をカンマ区切りで（なければ none）: %s" % tok, EXIT_USAGE)
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        for n in range(lo, hi + 1):
            if n not in numbers:
                raise Fail("%d 番は一覧にない" % n, EXIT_USAGE)
            out.add(n)
    return out


def read_note(args, stdin):
    if not args.note_file:
        return ""
    if args.note_file == "-":
        data = stdin.read()
    else:
        with open(args.note_file, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
    return data.strip()


def load_pending(data_dir, current, now):
    p = read_json(pending_path(data_dir), None)
    if not isinstance(p, dict) or not isinstance(p.get("items"), list):
        raise Fail("一覧の控えがない。先に /send-to-nobu で一覧を出して", EXIT_USAGE)
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
        with open(item["path"], "rb") as fh:
            pos = 0
            for raw in fh:
                pos += len(raw)
                if pos > limit:
                    break
                d = parse_line(raw)
                if d is not None and isinstance(d.get("uuid"), str):
                    out.add(uuid_hash(d["uuid"]))
    except OSError:
        pass
    return sorted(out)


def record_decisions(data_dir, now, packed, excluded_items):
    """送った / 外したを、判断した時点の位置で状態に記録する。"""
    state = load_state(data_dir, now)
    sessions = state["sessions"]
    store = read_json(excluded_path(data_dir), {})
    if not isinstance(store, dict):
        store = {}
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


def cmd_send(args, out, stdin):
    now = args.now if args.now is not None else time.time()
    data_dir = resolve_data_dir(args.data_dir)
    api_base = check_api_base(args.api_base) if args.api_base else None
    current = current_session(args.session)
    pending = load_pending(data_dir, current, now)
    items = {int(it["n"]): it for it in pending["items"]}
    excluded = parse_exclude(args.exclude, items)
    send_items = [items[n] for n in sorted(items) if n not in excluded]
    excluded_items = [items[n] for n in sorted(excluded)]

    if not args.confirm_shared:
        pairs = sorted({(it["n"], m) for it in send_items for m in it.get("shares", []) if m in excluded})
        if pairs:
            desc = "、".join("%d 番と %d 番" % pr for pr in pairs)
            raise Fail("%s は同じ履歴を共有している。外した方の中身も、送る方から届く。"
                       "了承なら --confirm-shared を付けてやり直す（止めるなら両方外す）" % desc, EXIT_CONFIRM_SHARED)

    note = read_note(args, stdin)
    if len(note) > NOTE_MAX:
        raise Fail("感想が長すぎる（%d 文字まで）" % NOTE_MAX, EXIT_USAGE)
    note = mask_text(note)[0]

    if not send_items and not note:
        # 全部外して感想もない日: サーバーには何も送らず、外したことだけ覚える
        record_decisions(data_dir, now, [], excluded_items)
        remove_quietly(pending_path(data_dir))
        out.write(json.dumps({"submission_id": None, "sent_count": 0, "excluded_count": len(excluded_items),
                              "subagent_count": 0, "bytes": 0, "redactions": 0}, ensure_ascii=False) + "\n")
        return 0

    if not args.code or not api_base:
        raise Fail("--code と --api-base がない。start_submission で受け取って", EXIT_USAGE)
    code = args.code.strip()

    tmp = tempfile.mkdtemp(prefix="pack-", dir=data_dir)
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
            jobs.append((u, f["local"], f["bytes"]))
        put_all(jobs)

        body = {"sent": [p["sent"] for p in packed], "excluded_count": len(excluded_items),
                "note": note, "plugin_version": plugin_version()}
        res = api_post(api_base, "/v1/finish", body, code, ok_codes=("already_finished",))

        record_decisions(data_dir, now, packed, excluded_items)
        remove_quietly(pending_path(data_dir))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    result = {
        "submission_id": res.get("submission_id"),
        "sent_count": len(packed),
        "excluded_count": len(excluded_items),
        "subagent_count": sum(len(p["sent"]["subagents"]) for p in packed),
        "bytes": sum(f["bytes"] for f in files),
        "redactions": sum(p["sent"]["redactions"] for p in packed),
    }
    out.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


# ---------------------------------------------------------------- main


def build_parser():
    p = argparse.ArgumentParser(prog="agentlog.py", description="send-to-nobu の処理")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--data-dir", default=None, help="状態を置くディレクトリ（${CLAUDE_PLUGIN_DATA}）")
        sp.add_argument("--projects-dir", default=None, help=argparse.SUPPRESS)
        sp.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)

    sp = sub.add_parser("nudge", help="未送信の会話があれば 1 日 1 回知らせる（フック用）")
    common(sp)

    sp = sub.add_parser("list", help="未送信の会話の一覧")
    common(sp)
    sp.add_argument("--preview", action="store_true", help="人の指示の抜粋（マスク済み）も出す")
    sp.add_argument("--session", default=None, help="いまの会話 ID（省略時は CLAUDE_CODE_SESSION_ID）")

    sp = sub.add_parser("send", help="一覧の控えに沿って送る")
    common(sp)
    sp.add_argument("--code", default=None, help="start_submission の upload_code")
    sp.add_argument("--exclude", required=True, help="外す番号（カンマ区切り）か none")
    sp.add_argument("--note-file", default=None, help="感想のファイル（- で標準入力）")
    sp.add_argument("--api-base", default=None, help="start_submission の api_base")
    sp.add_argument("--confirm-shared", action="store_true", help="履歴を共有する会話の片方だけ外すのを了承済み")
    sp.add_argument("--session", default=None, help="いまの会話 ID（省略時は CLAUDE_CODE_SESSION_ID）")
    return p


def main(argv=None, stdin=None, stdout=None, stderr=None):
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    if argv is not None and argv[:1] == ["nudge"]:
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
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help(stderr)
        return EXIT_USAGE
    if args.cmd == "nudge":
        return cmd_nudge(args, stdout)
    try:
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
    sys.exit(main(sys.argv[1:]))
