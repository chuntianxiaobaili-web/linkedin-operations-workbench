from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault.py"


class VaultCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name)

    def write(self, path: str, text: str) -> None:
        file = self.vault / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8", newline="\n")

    def invoke(self, *arguments, status=0):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault), *arguments],
            check=False,
            capture_output=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, status, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_index_uses_dated_operations_and_excludes_drafts(self):
        self.write("00-首页.md", "# 首页\n[[2026-11-07 领英运营记录]]")
        self.write("07-账号运营记录/2026-11-07 领英运营记录.md", "# 已入库")
        self.write("07-账号运营记录/2026-11-17 领英运营记录.md", "# 新记录")
        self.write("07-账号运营记录/2026-11-18 后续内容草稿.md", "# 新草稿")
        self.write("06-更新与证据/来源登记.json", json.dumps(
            {"accountExecution": {"lastUpdated": "2026-11-01"}}
        ))
        initial = self.invoke("index")
        self.assertEqual(initial["latest_operation_record_date"], "2026-11-17")
        self.assertEqual(initial["historical_staleness"], {
            "original_home_latest_linked": "2026-11-07",
            "source_register_account_date": "2026-11-01",
        })
        self.write("08-模型归纳/方法卡索引.md", "# 方法卡索引")
        self.assertFalse(self.invoke("verify", status=1)["index_fresh"])
        self.invoke("index")
        output = self.vault / "00-导航" / "当前索引.md"
        self.assertIn("[[08-模型归纳/方法卡索引|方法卡索引]]",
                      output.read_text(encoding="utf-8"))
        self.assertTrue(self.invoke("verify")["passed"])

    def test_search_matches_requested_scope_and_terms(self):
        self.write("02-主动获客/采购询问.md", "# 采购询问\n用一个具体问题验证采购需求。\n")
        self.write("07-账号运营记录/2026-11-17 领英运营记录.md",
                   "# 实际记录\n对方未确认采购需求。\n")
        self.write("01-运营方法/内容方法.md", "# 内容方法\n采购互动。\n")
        result = self.invoke("search", "采购 需求", "--scope", "methods")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["path"], "02-主动获客/采购询问.md")
        self.assertEqual(result["results"][0]["evidence"][1]["line"], 2)

    def test_verify_reports_broken_link_and_modified_bundle_file(self):
        note = "02-主动获客/研究.md"
        body = "# 研究\n[[00-首页]]\n[图](<../附件/原图.png>)\n"
        self.write("00-首页.md", "# 首页\n")
        self.write(note, body)
        self.write("附件/原图.png", "image placeholder")
        self.write("00-导航/打包完整性.json", json.dumps(
            {"original_packed_sha256": {note: hashlib.sha256(body.encode()).hexdigest()}}
        ))
        self.invoke("index")
        self.assertTrue(self.invoke("verify")["passed"])
        self.write(note, body + "\n[[缺失的笔记]]\n")
        checked = self.invoke("verify", status=1)
        self.assertEqual(checked["bundle_manifest"]["changed"], [note])
        self.assertEqual(checked["links"]["broken"][0]["target"], "缺失的笔记")


if __name__ == "__main__":
    unittest.main()
