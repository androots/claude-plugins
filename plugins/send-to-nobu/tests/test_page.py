# -*- coding: utf-8 -*-
"""今日のページ: テンプレにデータを流し込むだけ。データの形・壊れないこと・前のページを残さないこと・send の結果。"""

import datetime
import json
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import DATA_RE, Lines, World, agentlog  # noqa: E402
from test_guarantees import Base  # noqa: E402

REAL_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "page.html")
NASTY = "</script><script>alert(1)</script> <!-- &    </SCRIPT >"


def parse_iso(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z").timestamp()


class Page(Base):
    def test_the_data_on_the_page(self):
        t0 = time.time() - 7200
        home = Lines(base=t0, cwd=os.path.expanduser("~"))
        home.user("一つ目", at=t0).assistant(at=t0 + 60).user("二つ目", at=t0 + 600).assistant(at=t0 + 30 * 60)
        self.w.write(home)
        quick = Lines(base=t0 + 3600).user("すぐ終わった").assistant()
        self.w.write(quick)
        res = self.w.page()
        data = self.w.page_data()
        self.assertEqual(res["file"], os.path.join(self.w.data, "page", os.path.basename(res["file"])))
        self.assertTrue(os.path.isabs(res["file"]))
        self.assertRegex(os.path.basename(res["file"]), r"^send-to-nobu-\d{8}-\d{4}\.html$")
        self.assertRegex(res["round"], r"^r-\d{8}-\d{4}-[0-9a-f]{6}$")
        self.assertEqual((res["count"], res[agentlog.LIST_MARKER]), (2, 1))
        self.assertIn(os.path.join(self.w.data, "page"), res["next"])
        self.assertEqual((data["v"], data["round"], data["judge"], data["failed"], data["more"]),
                         (1, res["round"], "ok", 0, 0))
        self.assertEqual(data[agentlog.LIST_MARKER], 1)      # ページを読んだ会話も一覧に出さない
        self.assertEqual(data["round"], self.w.pending()["round"])
        a, b = data["items"]
        self.assertEqual({k: a[k] for k in ("n", "title", "project", "minutes", "turns", "flag")},
                         {"n": 1, "title": "一つ目", "project": "", "minutes": 30, "turns": 2, "flag": None})
        self.assertEqual({k: b[k] for k in ("n", "title", "project", "minutes", "turns", "flag")},
                         {"n": 2, "title": "すぐ終わった", "project": "billing", "minutes": 1, "turns": 1, "flag": None})
        self.assertAlmostEqual(parse_iso(a["start"]), t0, delta=1)
        self.assertAlmostEqual(parse_iso(data["range"]["from"]), t0, delta=1)
        self.assertAlmostEqual(parse_iso(data["range"]["to"]), parse_iso(data["made_at"]), delta=1)
        self.assertNotIn("session_id", json.dumps(data))      # 会話 ID・パスは控えにだけ

    def test_a_title_that_closes_the_script_does_not_break_the_page(self):
        L = Lines().user("x").assistant().meta("custom-title", customTitle=NASTY)
        self.w.write(L, project="-Users-alice-work-a-b")
        self.w.page()
        html = self.w.page_html()
        raw = DATA_RE.search(html).group(1)
        for ch in ("<", ">", "&", " ", " "):
            self.assertNotIn(ch, raw)
        self.assertEqual(json.loads(raw)["items"][0]["title"], agentlog.squash(NASTY, agentlog.TITLE_MAX))
        self.assertEqual(html.lower().count("</script>"), 2)    # テンプレのぶんだけ
        self.assertEqual(html.count("__STN_DATA__"), 0)

    def test_the_json_escapes_what_html_and_js_would_read(self):
        value = {"t": "a\u2028b\u2029</script>&<!--", "q": "\ud800"}
        text = agentlog.page_json(value)
        self.assertIsNone(re.search("[<>&\u2028\u2029]", text))
        self.assertEqual(json.loads(text), value)
        text.encode("utf-8")

    def test_a_broken_surrogate_in_a_title(self):
        L = Lines().user("x").assistant()
        L.raw(b'{"type":"custom-title","customTitle":"\\ud800 \xe5\xa3\x8a"}')
        self.w.write(L)
        self.w.page()
        self.assertEqual(self.w.page_data()["items"][0]["title"], "\ud800 壊")

    def test_only_the_latest_page_is_kept_and_old_answers_go(self):
        self.conv("会話")
        self.w.page()
        d = os.path.join(self.w.data, "page")
        old = os.path.join(d, "send-to-nobu-20260101-0700.html")
        with open(old, "w") as f:
            f.write("old")
        self.w.decide()
        with open(agentlog.result_path(self.w.data), "w") as f:
            f.write("{}")
        self.w.page()
        self.assertEqual(len([n for n in os.listdir(d) if n.endswith(".html")]), 1)
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(agentlog.decision_path(self.w.data)))
        self.assertFalse(os.path.exists(agentlog.result_path(self.w.data)))

    def test_a_template_without_one_slot_stops(self):
        self.conv("会話")
        for body in ("<html>no slot</html>", "__STN_DATA__ __STN_DATA__"):
            with open(agentlog.TEMPLATE, "w") as f:
                f.write(body)
            code, res = self.w.run("page")
            self.assertEqual(code, 1)
            self.assertIn("ひな形", res["say"])
            self.assertIsNone(self.w.pending())

    def test_send_writes_the_result_for_the_page(self):
        self.conv("一つ目", base=time.time() - 2000)
        self.conv("二つ目", base=time.time() - 1000)
        rnd = self.w.page()["round"]
        self.w.decide(exclude=[2], note="よかった")
        code, res = self.send()
        self.assertEqual(code, 0, res)
        self.assertEqual({k: res["result"][k] for k in ("round", "sent", "excluded", "note")},
                         {"round": rnd, "sent": 1, "excluded": 1, "note": True})
        parse_iso(res["result"]["at"])
        self.assertEqual(res["result_file"], agentlog.result_path(self.w.data))
        self.assertEqual(agentlog.read_json(res["result_file"], None), res["result"])
        self.assertEqual(res["next"], agentlog.NEXT_RESULT)
        self.assertIsNone(self.w.pending())


@unittest.skipUnless(os.path.exists(REAL_TEMPLATE), "scripts/page.html がまだ無い")
class RealTemplate(Base):
    """本物のテンプレでも、データの入れ口が 1 か所で、流し込んだデータが読める。"""

    def test_the_real_template(self):
        with open(REAL_TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("__STN_DATA__"), 1)
        self.assertRegex(text, r'<script type="application/json" id="stn-data">__STN_DATA__</script>')
        agentlog.TEMPLATE = None
        L = Lines().user("x").assistant().meta("custom-title", customTitle=NASTY)
        self.w.write(L)
        self.w.page()
        raw = re.search(r'<script type="application/json" id="stn-data">(.*?)</script>', self.w.page_html(), re.S)
        self.assertEqual(json.loads(raw.group(1))["items"][0]["title"], agentlog.squash(NASTY, agentlog.TITLE_MAX))


if __name__ == "__main__":
    unittest.main()
