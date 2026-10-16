# -*- coding: utf-8 -*-
"""アーティファクトを見つけて、見える文字を抜き、撮ってぼかす。Chrome と sips は偽物（撮ったページの中身が画像に入る）。"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Base, Lines, agentlog, image_bytes  # noqa: E402


class Shots(Base):
    def chrome_calls(self):
        try:
            with open(self.chrome_record, encoding="utf-8") as f:
                return [json.loads(x) for x in f]
        except OSError:
            return []

    def setUp(self):
        super().setUp()
        self.chrome_record = os.path.join(self.w.tmp, "chrome.jsonl")
        os.environ["FAKE_CHROME"] = json.dumps({"record": self.chrome_record})

    def test_shots_are_blurred_where_the_reviewer_says(self):
        p = self.w.html("lp.html", '<h1>新商品</h1><p>取引先 ABC 商事</p><img src="hero.png">')
        L = Lines().user("LP を作って").assistant().artifact(p).assistant("公開した")
        self.w.write(L)
        self.fake_review(hide=["ABC 商事", "卸値 </script> 120 円"])
        self.prepared()
        rep = self.report()
        self.assertEqual(rep["shots"], {"s1.jpg": {"conv": "c1", "hidden": [agentlog.HIDE_LABEL]}})
        self.assertEqual(rep["convs"][0]["shots"], ["s1.jpg"])
        self.assertEqual(rep["convs"][0]["thumb"]["img"], "s1.jpg")
        img = image_bytes(self.w.page_data()["images"]["s1.jpg"]).decode("utf-8", "replace")
        self.assertTrue(img.startswith("\ufffd\ufffd\ufffd"))       # JPEG の印（偽の sips）
        self.assertIn('<base href="file://%s/">' % self.w.work, img)    # 元のフォルダの画像も読めるように
        self.assertIn('"ABC 商事"', img)                                # ぼかす文字を JS に渡している
        self.assertIn("卸値 \\u003c/script\\u003e 120 円", img)        # JS の中で script が閉じない
        self.assertIn("blur(7px)", img)
        self.assertEqual(img.count("<script>"), 1)
        shot = [a for a in self.chrome_calls() if any(x.startswith("--screenshot=") for x in a)][0]
        for flag in ("--headless=new", "--use-mock-keychain", "--window-size=1280,800"):
            self.assertIn(flag, shot)
        self.assertTrue(any(x.startswith("--user-data-dir=") for x in shot))
        # 見える文字は描いたあとの DOM から（Chrome があるとき）
        self.assertTrue(any("--dump-dom" in a for a in self.chrome_calls()))
        self.assertIn("取引先 ABC 商事", self.review_text())

    def test_which_publishes_count(self):
        keep = [self.w.html("p%d.html" % i, "<p>%d</p>" % i) for i in range(4)]
        missing = os.path.join(self.w.work, "消えた.html")
        png = os.path.join(self.w.work, "x.png")
        open(png, "w").close()
        L = Lines().user("いろいろ作って").assistant()
        for p in keep:
            L.artifact(p)
        L.artifact(keep[0])                                       # 同じファイルの更新は 1 つ
        L.artifact(missing).artifact(png)
        L.tool("Artifact", {"action": "read", "url": "https://claude.ai/code/artifact/x"})
        L.tool("Artifact", {"url": "https://claude.ai/code/artifact/x", "file_path": keep[1], "asset": True})
        L.tool("Write", {"file_path": keep[2]})
        L.assistant()
        self.w.write(L)
        self.prepared()
        text = self.review_text()
        names = [x.split("（")[1].rstrip("）") for x in text.splitlines() if x.startswith("--- アーティファクト")]
        self.assertEqual(len(names), agentlog.SHOTS_PER_CONV)       # 会話ごとに新しい 3 つまで
        self.assertEqual(sorted(names), ["p0.html", "p2.html", "p3.html"])
        self.assertEqual(len(self.report()["shots"]), 3)

    def test_relative_paths_use_the_cwd(self):
        self.w.html("rel.html", "<p>相対</p>")
        L = Lines(cwd=self.w.work).user("作って").assistant().artifact("rel.html").assistant()
        self.w.write(L)
        self.prepared()
        self.assertIn("--- アーティファクト a1（rel.html）", self.review_text())

    def test_no_chrome_means_no_shots_but_text_from_the_file(self):
        agentlog.CHROME = os.path.join(self.w.tmp, "no-chrome")
        p = self.w.html("lp.html", "<p>ファイルの文字</p><script>var a='スクリプト'</script>")
        self.w.write(Lines().user("作って").assistant().artifact(p).assistant())
        self.prepared()
        self.assertEqual(self.report()["shots"], {})
        self.assertNotIn("images", self.report())
        self.assertEqual(self.w.page_data()["images"], {})
        self.assertIn("ファイルの文字", self.review_text())
        self.assertNotIn("スクリプト", self.review_text())
        self.w.decide()
        self.assertEqual(self.send()[0], 0)
        self.assertEqual(self.inbox.body()["images"], [])

    def test_a_failed_shot_is_skipped_and_names_stay_in_order(self):
        os.environ["FAKE_CHROME"] = json.dumps({"record": self.chrome_record, "fail": "撮れない"})
        ps = [self.w.html("a.html", "<p>撮れない</p>"), self.w.html("b.html", "<p>撮れる</p>")]
        L = Lines().user("作って").assistant()
        for p in ps:
            L.artifact(p)
        self.w.write(L.assistant())
        self.prepared()
        self.assertEqual(sorted(self.report()["shots"]), ["s1.jpg"])
        self.assertIn("撮れる", image_bytes(self.w.page_data()["images"]["s1.jpg"]).decode("utf-8", "replace"))

    def test_too_many_shots_are_capped(self):
        saved = agentlog.SHOT_MAX
        agentlog.SHOT_MAX = 2
        try:
            for i in range(3):
                p = self.w.html("p%d.html" % i, "<p>%d</p>" % i)
                self.w.write(Lines(base=time.time() - 3000 + i * 100).user("作って %d" % i).assistant().artifact(p))
            self.prepared()
        finally:
            agentlog.SHOT_MAX = saved
        self.assertEqual(sorted(self.report()["shots"]), ["s1.jpg", "s2.jpg"])

    def test_inject_blur_places_the_base_and_the_script(self):
        out = agentlog.inject_blur("<!doctype html><html><HEAD><title>x</title></HEAD><body><p>a</p></BODY></html>",
                                   "/tmp/a b", ["秘密"])
        self.assertTrue(out.startswith('<!doctype html><html><HEAD><base href="file:///tmp/a%20b/">'))
        self.assertIn('</script></BODY>', out)
        bare = agentlog.inject_blur("<p>a</p>", "/tmp", [])
        self.assertTrue(bare.startswith('<base href="file:///tmp/">') and bare.endswith("</script>"))

    def test_visible_text(self):
        t = agentlog.visible_text("<title>題</title><style>.a{}</style><h1>見出し&amp;</h1><div>本文</div>"
                                  "<template>型</template><noscript>JS</noscript>", 100)
        self.assertEqual(t, "題\n見出し&\n本文")
        self.assertEqual(len(agentlog.visible_text("<p>%s</p>" % ("あ" * 3000))), agentlog.ARTIFACT_CHARS)


if __name__ == "__main__":
    unittest.main()
