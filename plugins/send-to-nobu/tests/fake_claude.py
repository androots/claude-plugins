#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""判定用の偽の claude -p（テストと対話の通しが SEND_TO_NOBU_CLAUDE で差し替える）。

ふだんはすべて「ふつうの業務」。env の FAKE_JUDGE（JSON）で振る舞いを変える:
  flag   {本文に含まれる語: 理由}  その語を含む会話を候補にする
  mode   error（is_error）・slow（3 秒待つ）・drop（最後の 1 件を返さない）
  record このファイルに、受け取った引数と標準入力を JSON で書く
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
if mode == "error":
    print(json.dumps({"type": "result", "is_error": True, "result": "API Error"}))
    sys.exit(1)
if mode == "slow":
    time.sleep(3)
results = []
for c in json.loads(data):
    reason = next((r for word, r in (conf.get("flag") or {}).items() if word in c["text"]), None)
    results.append({"n": c["n"], "exclude": bool(reason), "reason": reason or "ふつうの業務"})
if mode == "drop":
    results = results[:-1]
print(json.dumps({"type": "result", "is_error": False, "structured_output": {"results": results}}, ensure_ascii=False))
