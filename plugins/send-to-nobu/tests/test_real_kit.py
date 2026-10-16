# -*- coding: utf-8 -*-
"""本物のキット（ai-iinkai の send-to-nobu/dist/kit/。python3 kit/build.py で組む）でも、ページが組めて壊れないこと。

キットの置き場所は env の SEND_TO_NOBU_REAL_KIT（無ければ ~/Dev/GHDev/ai-iinkai/send-to-nobu/dist/publish/v1）。
見つからなければ飛ばす。Chrome があれば、本物の headless Chrome で描いて中身が出ることも確かめる。
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Base, Lines, agentlog  # noqa: E402

REAL_KIT = os.environ.get("SEND_TO_NOBU_REAL_KIT") or os.path.expanduser(
    "~/Dev/GHDev/ai-iinkai/send-to-nobu/dist/publish/v1")
REAL_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def load_real_kit():
    files = {}
    for name in agentlog.KIT_FILES:
        with open(os.path.join(REAL_KIT, name), encoding="utf-8") as f:
            files[name] = json.load(f) if name.endswith(".json") else f.read()
    return {"version": "v1", "files": files}


@unittest.skipUnless(all(os.path.isfile(os.path.join(REAL_KIT, n)) for n in agentlog.KIT_FILES),
                     "本物のキットが無い（SEND_TO_NOBU_REAL_KIT）")
class RealKit(Base):
    def setUp(self):
        super().setUp()
        self.inbox.kit = load_real_kit()
        p = self.w.html("lp.html", "<h1>新商品 LP</h1><p>取引先 ABC 商事</p>")
        A = Lines(base=time.time() - 3000).user("LP を作って </script> & <b>").assistant().artifact(p).assistant("公開した")
        self.w.write(A)
        B = Lines(base=time.time() - 2000).user("議事録をまとめて").assistant()
        B.meta("custom-title", customTitle="</script><script>alert(1)</script>")
        self.w.write(B)
        self.fake_review(output={
            "advices": [{"type": "narabe", "conv": "c1", "say": "並べて選ぶ", "key": "アーティファクト",
                         "quotes": ["LP を作って"], "count": "3 往復", "n": 3, "items": ["見出し"],
                         "next_label": "3 案を 1 枚で", "yesterday": "1 つずつ", "next": "並べる",
                         "prompt": "案をアーティファクトに並べて"},
                        {"type": "free", "conv": "c2", "say": "自由な型", "key": "", "quotes": [], "count": "", "n": 0,
                         "items": [], "next_label": "", "yesterday": "", "next": "", "prompt": "p",
                         "free_title": "その他の型"}],
            "praises": [{"conv": "c2", "say": "よかった", "first": True}],
            "convs": [{"id": "c1", "title": "LP", "flow": ["頼んだ", "できた"], "thumb": "art", "gaveup": False,
                       "memo": {"verdict": "言う", "why": "並べる場面"}},
                      {"id": "c2", "title": "議事録", "flow": ["頼んだ", "できた"], "thumb": "doc", "gaveup": False,
                       "memo": {"verdict": "言わない", "why": "1 往復"}}],
            "hide": {"a1": ["ABC 商事"]}})

    def test_the_real_template(self):
        res = self.prepared()
        html = self.w.page_html()
        self.assertEqual(html.count(agentlog.PAGE_SLOT), 0)
        raw = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S).group(1)
        data = json.loads(raw)
        self.assertEqual((data["mode"], data["round"]), ("person", res["round"]))
        self.assertEqual(data["advices"][1]["free_title"], "その他の型")
        self.assertEqual(sorted(data["images"]), ["s1.jpg"])
        self.w.decide(exclude=["c2"])
        code, out = self.send()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["result"]["sent"], ["c1"])

    @unittest.skipUnless(os.access(REAL_CHROME, os.X_OK), "Chrome が無い")
    def test_the_page_draws_in_chrome(self):
        self.prepared()
        tmp = tempfile.mkdtemp(prefix="stn-real-")
        try:
            page = os.path.join(tmp, "page.html")
            with open(page, "w", encoding="utf-8") as f:
                f.write(self.w.page_html())
            saved = agentlog.CHROME
            agentlog.CHROME = REAL_CHROME
            try:
                dom = agentlog.run_chrome(["--dump-dom", agentlog.file_url(page)], tmp, 60).decode("utf-8", "replace")
            finally:
                agentlog.CHROME = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        text = agentlog.visible_text(dom, 100000)
        for word in ("alice", "並べて選ぶ", "自由な型", "議事録"):
            self.assertIn(word, text)
        self.assertIn("その他の型", dom)        # free の型の名札（アイコンの中）
        self.assertNotIn("alert(1)</script>", dom.split('id="report-data"')[0])   # 題で script が閉じていない


if __name__ == "__main__":
    unittest.main()
