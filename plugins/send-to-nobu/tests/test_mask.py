# -*- coding: utf-8 -*-
"""秘密のマスク。鍵はすべてテスト用の作り物。"""

import json
import re
import unittest

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import agentlog  # noqa: E402

ANTHROPIC = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 4
OPENAI = "sk-proj-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2"
AWS = "AKIA" + "ABCDEFGHIJ234567"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GITHUB_PAT = "github_pat_" + "11ABCDEFG0" + "x" * 40
SLACK = "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
GOOGLE = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
BEARER = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PEM = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7Vx1b2c3\nd4e5f6g7h8i9j0K1L2M3N4O5P6==\n"
       "-----END RSA PRIVATE KEY-----")


def line(text):
    """本文 text を持つ JSONL の 1 行（生のバイト列）。"""
    obj = {"type": "user", "message": {"role": "user", "content": text}, "uuid": "u-1"}
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def content(raw):
    return json.loads(raw.decode("utf-8", "replace"))["message"]["content"]


class MaskLineTest(unittest.TestCase):
    def assertMasked(self, secret, kind):
        raw = line("鍵はこれ: %s です" % secret)
        new, counts = agentlog.mask_blob(raw)
        self.assertNotIn(secret.encode(), new)
        self.assertEqual(counts, {kind: 1})
        self.assertIn("[REDACTED:%s]" % kind, content(new))
        self.assertTrue(new.endswith(b"\n"))

    def test_each_kind(self):
        self.assertMasked(ANTHROPIC, "anthropic_key")
        self.assertMasked(OPENAI, "openai_key")
        self.assertMasked(AWS, "aws_access_key")
        self.assertMasked(GITHUB, "github_token")
        self.assertMasked(GITHUB_PAT, "github_token")
        self.assertMasked(SLACK, "slack_token")
        self.assertMasked(GOOGLE, "google_api_key")
        self.assertMasked(PEM, "private_key")

    def test_bearer_keeps_scheme(self):
        raw = line("curl -H 'Authorization: Bearer %s'" % BEARER)
        new, counts = agentlog.mask_blob(raw)
        self.assertEqual(counts, {"bearer_token": 1})
        self.assertIn("Bearer [REDACTED:bearer_token]", content(new))

    def test_escaped_newline_before_key(self):
        # JSON の中では改行が `\n` と書かれる。直後の sk-ant- を見逃さない
        raw = line("export KEY=\n%s\nnext" % ANTHROPIC)
        self.assertIn(b"\\n" + ANTHROPIC.encode(), raw)
        new, counts = agentlog.mask_blob(raw)
        self.assertEqual(counts, {"anthropic_key": 1})
        self.assertEqual(content(new), "export KEY=\n[REDACTED:anthropic_key]\nnext")

    def test_left_boundary(self):
        # 直前が日本語なら拾う（境界は ASCII の英数字だけで見る）
        new, counts = agentlog.mask_blob(line("キーは%sです" % ANTHROPIC))
        self.assertEqual(counts, {"anthropic_key": 1})
        # 直前が英数字の途中なら拾わない。ただし後ろに境界のある鍵があればそちらは拾う
        new, counts = agentlog.mask_blob(line("abc%s" % ANTHROPIC))
        self.assertEqual(counts, {})
        new, counts = agentlog.mask_blob(line("x%s %s" % (AWS, AWS)))
        self.assertEqual(counts, {"aws_access_key": 1})
        self.assertIn("x%s [REDACTED:aws_access_key]" % AWS, content(new))

    def test_google_key_inside_base64_is_not_masked(self):
        blob = "iVBORw0KGgoAAAANSUhEUgAA/" + GOOGLE + "Qx7Rb2Kc9" + "+AAAA"
        new, counts = agentlog.mask_blob(line(blob))
        self.assertEqual(counts, {})
        new, counts = agentlog.mask_blob(line("AAAA/" + GOOGLE + "/AAAA+" + GOOGLE + "+AAAA=="))
        self.assertEqual(counts, {})
        new, counts = agentlog.mask_blob(line("key=%s&x=1" % GOOGLE))
        self.assertEqual(counts, {"google_api_key": 1})

    def test_pem_truncated_without_end(self):
        raw = line("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\nAAAA")
        new, counts = agentlog.mask_blob(raw)
        self.assertEqual(counts, {"private_key": 1})
        self.assertNotIn(b"b3BlbnNzaC1rZXkt", new)
        self.assertEqual(content(new), "[REDACTED:private_key]")

    def test_pem_double_escaped(self):
        # JSON 文字列の中にさらに JSON（ツール結果など）: `\\n` になる
        inner = json.dumps({"key": PEM})
        raw = line(inner)
        new, counts = agentlog.mask_blob(raw)
        self.assertEqual(counts, {"private_key": 1})
        self.assertNotIn(b"MIIEpAIBAAKCAQEA", new)
        json.loads(content(new))  # 中の JSON も壊れない

    def test_prose_is_not_masked(self):
        for text in ("sk-learn-is-a-python-library-for-ml", "Bearer authentication-is-a-scheme-name",
                     "task-management-for-everyone-here", "AKIAが何かを調べる", "BEGIN PRIVATE KEY の話"):
            raw = line(text)
            new, counts = agentlog.mask_blob(raw)
            self.assertEqual(counts, {}, text)
            self.assertIs(new, raw)

    def test_unmatched_line_is_byte_identical(self):
        raw = line("ふつうの会話") + b""
        new, counts = agentlog.mask_blob(raw)
        self.assertIs(new, raw)

    def test_invalid_utf8_line_keeps_other_bytes(self):
        raw = b'{"type":"user","message":{"content":"\xff\xfe broken ' + ANTHROPIC.encode() + b'"},"uuid":"x"}\n'
        new, counts = agentlog.mask_blob(raw)
        self.assertEqual(counts, {"anthropic_key": 1})
        self.assertTrue(new.startswith(b'{"type":"user","message":{"content":"\xff\xfe broken [REDACTED:'))

    def test_several_in_one_line(self):
        raw = line("%s と %s と %s" % (ANTHROPIC, AWS, GITHUB))
        new, counts = agentlog.mask_blob(raw)
        self.assertEqual(counts, {"anthropic_key": 1, "aws_access_key": 1, "github_token": 1})
        self.assertEqual(sum(counts.values()), 3)

    def test_fallback_when_regex_breaks_json(self):
        # 置換で JSON が壊れた行だけ、値ごとにマスクして書き直す
        saved = agentlog._COMPILED
        saved_screen = (agentlog._SCREEN_B, agentlog._SCREEN_S)
        try:
            pat = r'secret"?:?"?[a-z0-9]+'
            agentlog._COMPILED = [("test_secret", (b"secret",), ("secret",), re.compile(pat.encode()),
                                   re.compile(pat), 0, True)]
            agentlog._SCREEN_B, agentlog._SCREEN_S = re.compile(b"secret"), re.compile("secret")
            raw = b'{"secret":"abc123","other":"x secret99 y"}\n'
            new, counts = agentlog.mask_blob(raw)
            obj = json.loads(new)
            self.assertEqual(obj, {"secret": "abc123", "other": "x [REDACTED:test_secret] y"})
            self.assertEqual(counts, {"test_secret": 1})
            self.assertTrue(new.endswith(b"\n"))
        finally:
            agentlog._COMPILED = saved
            agentlog._SCREEN_B, agentlog._SCREEN_S = saved_screen

    def test_mask_text_for_preview(self):
        t, counts = agentlog.mask_text("これ使って %s" % OPENAI)
        self.assertEqual(t, "これ使って [REDACTED:openai_key]")
        self.assertEqual(counts, {"openai_key": 1})


if __name__ == "__main__":
    unittest.main()
