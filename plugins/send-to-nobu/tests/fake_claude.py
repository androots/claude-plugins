#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""偽の claude -p（テストが SEND_TO_NOBU_CLAUDE で差し替える）。--model sonnet は外す判定、それ以外はレビュー役。

外す判定は env の FAKE_JUDGE（JSON）で振る舞いを変える（ふだんはすべて「ふつうの業務」）:
  flag    {やりとりに含まれる語: 理由}  その語を含む会話を外す
  fail_on この語を含むバッチはエラーにする
  mode    error（is_error）・slow（3 秒待つ）・drop（最後の 1 件を返さない）
レビュー役は env の FAKE_REVIEW（JSON）:
  output  structured_output をこのまま返す（無ければ、入力の会話から作る）
  hide    作るときに、すべてのアーティファクトの hide に入れる文字列
  fail    最初の fail 回はエラーにする（回数は counter のファイルで数える）
共通: record  このファイルに、受け取った引数・標準入力・env・cwd を JSON で 1 行ずつ足す
"""
import json
import os
import re
import sys
import time

argv = sys.argv[1:]
kind = "judge" if argv[argv.index("--model") + 1] == "sonnet" else "review"
conf = json.loads(os.environ.get("FAKE_JUDGE" if kind == "judge" else "FAKE_REVIEW") or "{}")
data = sys.stdin.read()
if conf.get("record"):
    with open(conf["record"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "argv": argv, "stdin": data, "judge_env": os.environ.get("SEND_TO_NOBU_JUDGE"),
                            "cwd": os.getcwd()}, ensure_ascii=False) + "\n")


def done(so, cost):
    print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": cost, "structured_output": so},
                     ensure_ascii=False))
    sys.exit(0)


def fail():
    print(json.dumps({"type": "result", "is_error": True, "result": "API Error"}))
    sys.exit(1)


if kind == "judge":
    mode = conf.get("mode")
    if mode == "error" or (conf.get("fail_on") and conf["fail_on"] in data):
        fail()
    if mode == "slow":
        time.sleep(3)
    results = []
    for c in json.loads(data):
        hit = next((r for w, r in (conf.get("flag") or {}).items() if any(w in s["text"] for s in c["says"])), None)
        results.append({"n": c["n"], "exclude": bool(hit), "reason": hit or "ふつうの業務"})
    if mode == "drop":
        results = results[:-1]
    done({"results": results}, 0.01)

counter = conf.get("counter")
if counter:
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as f:
        f.write(str(n + 1))
    if n < conf.get("fail", 0):
        fail()
if "output" in conf:
    done(conf["output"], 0.5)
ids = re.findall(r"^=== 会話 (c\d+)", data, re.M)
first = {}
for cid, block in re.findall(r"^=== 会話 (c\d+)[^\n]*\n(.*?)(?=^=== |\Z)", data, re.M | re.S):
    m = re.search(r"^\[本人\] (.+)$", block, re.M)
    first[cid] = m.group(1) if m else ""
arts = re.findall(r"^--- アーティファクト (a\d+)", data, re.M)
so = {"advices": [], "praises": [], "convs": [], "hide": {a: list(conf.get("hide", [])) for a in arts}}
if ids:
    so["advices"].append({"type": "narabe", "conv": ids[0], "say": "直す所が多いときは、並べて選ぶ",
                          "key": "データベース付きのアーティファクト", "quotes": [first[ids[0]][:80]], "count": "3 往復",
                          "n": 3, "items": ["見出し"], "next_label": "3 案を 1 枚で選ぶ", "yesterday": "1 つずつ直していた",
                          "next": "並べて選ぶ", "prompt": "案を 3 つずつ、データベース付きのアーティファクトに並べて"})
    so["praises"].append({"conv": ids[-1], "say": "自分で /compact した", "first": True})
    so["convs"] = [{"id": i, "title": "題 " + i, "flow": ["頼んだ", "できた"], "thumb": "doc", "gaveup": False,
                    "memo": {"verdict": "言う" if i == ids[0] else "言わない", "why": "理由 " + i}} for i in ids]
done(so, 0.5)
