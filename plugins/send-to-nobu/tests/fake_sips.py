#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""偽の sips（テストが agentlog.SIPS を差し替える）。--out の先に「JPEG の印 + 元のバイト列」を書く。"""
import sys

argv = sys.argv[1:]
out = argv[argv.index("--out") + 1]
src = argv[argv.index("--out") - 1]
assert argv[:6] == ["-s", "format", "jpeg", "-s", "formatOptions", "70"], argv
assert argv[6:8] == ["--resampleWidth", "1100"], argv
with open(src, "rb") as f:
    data = f.read()
with open(out, "wb") as f:
    f.write(b"\xff\xd8\xff\xe0" + data)
