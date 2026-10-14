#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""判定用の偽の claude -p（テストが SEND_TO_NOBU_CLAUDE で差し替える）。

ふだんはすべて「ふつうの業務」。env の FAKE_JUDGE（JSON）で振る舞いを変える:
  flag    {やりとりに含まれる語: 理由}  その語を含む会話を候補にする（quote はその語を含む最初の位置）
  why     候補のひとこと（既定は理由と同じ語）
  quote   候補の quote をこの値にする（範囲外を試す）
  fail_on この語を含むバッチはエラーにする
  mode    error（is_error）・slow（3 秒待つ）・drop（最後の 1 件を返さない）
  record  このファイルに、受け取った引数と標準入力を JSON で書く
"""
import json
import os
import sys
import time

conf = json.loads(os.environ.get("FAKE_JUDGE") or "{}")
data = sys.stdin.read()
if conf.get("record"):
    with open(conf["record"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "stdin": data, "judge_env": os.environ.get("SEND_TO_NOBU_JUDGE"),
                            "cwd": os.getcwd()}, ensure_ascii=False) + "\n")
mode = conf.get("mode")
if mode == "error" or (conf.get("fail_on") and conf["fail_on"] in data):
    print(json.dumps({"type": "result", "is_error": True, "result": "API Error"}))
    sys.exit(1)
if mode == "slow":
    time.sleep(3)
results = []
for c in json.loads(data):
    hit = next(((w, r) for w, r in (conf.get("flag") or {}).items() if any(w in s["text"] for s in c["says"])), None)
    if hit:
        q = next(i for i, s in enumerate(c["says"]) if hit[0] in s["text"])
        results.append({"n": c["n"], "exclude": True, "reason": hit[1], "why": conf.get("why", hit[1]),
                        "quote": conf.get("quote", q)})
    else:
        results.append({"n": c["n"], "exclude": False, "reason": "ふつうの業務", "why": "", "quote": -1})
if mode == "drop":
    results = results[:-1]
print(json.dumps({"type": "result", "is_error": False, "structured_output": {"results": results}}, ensure_ascii=False))
