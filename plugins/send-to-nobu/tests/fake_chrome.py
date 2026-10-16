#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""偽の headless Chrome（テストが agentlog.CHROME を差し替える）。

--dump-dom: 開いたファイルの中身をそのまま出す。--screenshot=PATH: 「PNG の印 + 開いたページの中身」を書く
（撮る前に差し込んだ JS と <base> を、テストが画像から確かめられるように）。
env の FAKE_CHROME（JSON）: record = 引数を足すファイル、fail = この語を含むページは撮らない
"""
import json
import os
import sys
import urllib.parse
import urllib.request

argv = sys.argv[1:]
conf = json.loads(os.environ.get("FAKE_CHROME") or "{}")
if conf.get("record"):
    with open(conf["record"], "a", encoding="utf-8") as f:
        f.write(json.dumps(argv, ensure_ascii=False) + "\n")
url = argv[-1]
path = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
with open(path, "rb") as f:
    page = f.read()
if conf.get("fail") and conf["fail"].encode("utf-8") in page:
    sys.exit(1)
if "--dump-dom" in argv:
    sys.stdout.buffer.write(page)
    sys.exit(0)
for a in argv:
    if a.startswith("--screenshot="):
        with open(a.split("=", 1)[1], "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + page)
