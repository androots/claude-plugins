# -*- coding: utf-8 -*-
"""画像・文書の base64 を外す変換。データはすべて作り物。"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import agentlog  # noqa: E402

PNG = base64.b64encode(b"\x89PNG" + b"\x00" * 3000).decode()      # 3004 bytes
PDF = base64.b64encode(b"%PDF-1.7" + b"x" * 1001).decode()         # 1009 bytes


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


class OmitTest(unittest.TestCase):
    def test_image_block_in_user_message(self):
        row = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "この画面見て"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}]},
            "uuid": "u1"}
        new, n = agentlog.omit_blob(dumps(row))
        self.assertEqual(n, 1)
        obj = json.loads(new)
        block = obj["message"]["content"][1]
        self.assertEqual(block["source"], {"type": "base64", "media_type": "image/png",
                                           "data": "[OMITTED:image/png 3004 bytes]"})
        self.assertEqual(obj["message"]["content"][0]["text"], "この画面見て")
        self.assertTrue(new.endswith(b"\n"))

    def test_tool_result_image_and_read_tool_file(self):
        # スクリーンショット: tool_result の中の画像と、toolUseResult.file.base64 に同じ画像がもう 1 度
        row = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": PNG}}]}]},
            "toolUseResult": {"type": "image", "file": {"base64": PNG, "type": "image/jpeg",
                                                        "originalSize": 3004, "dimensions": {"w": 1, "h": 1}}},
            "uuid": "u2"}
        new, n = agentlog.omit_blob(dumps(row))
        self.assertEqual(n, 2)
        obj = json.loads(new)
        self.assertEqual(obj["toolUseResult"]["file"]["base64"], "[OMITTED:image/jpeg 3004 bytes]")
        self.assertEqual(obj["toolUseResult"]["file"]["originalSize"], 3004)
        self.assertEqual(obj["toolUseResult"]["file"]["dimensions"], {"w": 1, "h": 1})
        self.assertLess(len(new), 1000)

    def test_document_block(self):
        row = {"type": "user", "message": {"content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": PDF}}]}}
        new, n = agentlog.omit_blob(dumps(row))
        self.assertEqual(n, 1)
        self.assertIn(b"[OMITTED:application/pdf 1009 bytes]", new)

    def test_untouched_lines_are_byte_identical(self):
        for row in ({"type": "user", "message": {"content": "base64 って何？"}},
                    {"type": "user", "message": {"content": [{"type": "image", "source": {"type": "url", "url": "https://x"}}]}}):
            raw = dumps(row)
            new, n = agentlog.omit_blob(raw)
            self.assertIs(new, raw)
            self.assertEqual(n, 0)

    def test_read_tool_pdf_result(self):
        # Read の PDF の結果は file の中に type が無い。media type が無くても base64 は抜く（ラベルは unknown）
        row = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": PDF}}]}]},
            "toolUseResult": {"type": "pdf", "file": {"filePath": "/tmp/a.pdf", "base64": PDF, "originalSize": 1009}}}
        new, n = agentlog.omit_blob(dumps(row))
        self.assertEqual(n, 2)
        self.assertNotIn(PDF[:100].encode(), new)
        obj = json.loads(new)
        self.assertEqual(obj["toolUseResult"]["file"], {"filePath": "/tmp/a.pdf", "originalSize": 1009,
                                                        "base64": "[OMITTED:unknown 1009 bytes]"})

    def test_any_base64_string_is_omitted(self):
        for row in ({"type": "user", "toolUseResult": {"encoding": "base64", "base64": "QUJD"}},
                    {"type": "user", "x": {"base64": "QUJD", "type": "text"}}):
            new, n = agentlog.omit_blob(dumps(row))
            self.assertEqual(n, 1)
            self.assertIn(b"[OMITTED:unknown 3 bytes]", new)

    def test_already_omitted_is_not_counted_again(self):
        row = {"message": {"content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                     "data": "[OMITTED:image/png 3 bytes]"}}]}}
        raw = dumps(row)
        self.assertEqual(agentlog.omit_blob(raw), (raw, 0))

    def test_lone_surrogate_falls_back_to_ascii_json(self):
        raw = (b'{"t":"\\ud800 x","message":{"content":[{"type":"image","source":{"type":"base64",'
               b'"media_type":"image/png","data":"' + PNG.encode() + b'"}}]}}\n')
        new, n = agentlog.omit_blob(raw)
        self.assertEqual(n, 1)
        self.assertEqual(json.loads(new)["t"], "\ud800 x")

    def test_transform_omits_then_masks(self):
        key = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 4
        row = {"message": {"content": [{"type": "text", "text": "キー %s" % key},
                                       {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                    "data": PNG}}]}}
        new, counts, omitted = agentlog.transform_blob(dumps(row))
        self.assertEqual((counts, omitted), ({"anthropic_key": 1}, 1))
        self.assertNotIn(key.encode(), new)
        json.loads(new)

    def test_b64_size(self):
        for n in range(0, 10):
            s = base64.b64encode(b"x" * n).decode()
            self.assertEqual(agentlog._b64_size(s), n)


if __name__ == "__main__":
    unittest.main()
