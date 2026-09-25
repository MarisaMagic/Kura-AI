"""文档类型台账：扩展名准入、旧格式拒绝、源码语言映射。"""

from __future__ import annotations

import unittest

from app.kb import kb_service
from app.utils import document_types as dt
from app.utils import upload_sniff


class AllowedExtensionTests(unittest.TestCase):
    def test_supported_formats(self):
        for name in ("a.pdf", "a.docx", "a.xlsx", "a.txt", "a.md", "a.csv", "a.py", "a.ts", "a.yaml"):
            self.assertTrue(dt.allowed_upload_extension(name), name)
            self.assertTrue(kb_service.allowed_upload_extension(name), name)

    def test_legacy_and_unknown_rejected(self):
        for name in ("a.doc", "a.xls", "a.exe", "noext"):
            self.assertFalse(dt.allowed_upload_extension(name), name)
        self.assertIn(".docx", dt.reject_reason("a.doc") or "")
        self.assertIn(".xlsx", dt.reject_reason("a.xls") or "")
        self.assertIsNone(dt.reject_reason("a.pdf"))
        self.assertIsNone(dt.reject_reason("a.unknownext"))


class LanguageMappingTests(unittest.TestCase):
    def test_extension_and_special_filename(self):
        self.assertEqual(dt.language_for_filename("a.py"), "python")
        self.assertEqual(dt.language_for_filename("a.TSX"), "tsx")
        self.assertEqual(dt.language_for_filename("a.d.ts"), "typescript")
        self.assertEqual(dt.language_for_filename("Dockerfile"), "dockerfile")
        self.assertEqual(dt.language_for_filename("CMakeLists.txt"), "cmake")
        self.assertEqual(dt.language_for_filename("a.unknown"), "")

    def test_doc_kind(self):
        self.assertEqual(dt.doc_kind("a.md"), "markdown")
        self.assertEqual(dt.doc_kind("a.csv"), "csv")
        self.assertEqual(dt.doc_kind("Makefile"), "code")
        self.assertEqual(dt.doc_kind("a.doc"), "legacy_doc")


class UploadSniffTests(unittest.TestCase):
    def test_code_files_pass_without_magic(self):
        for name in ("a.py", "a.ts", "a.yaml", "Dockerfile", "a.csv"):
            upload_sniff.assert_upload_magic(name, b"print(1)\n")

    def test_pdf_still_requires_magic(self):
        with self.assertRaises(ValueError):
            upload_sniff.assert_upload_magic("a.pdf", b"not a pdf")


if __name__ == "__main__":
    unittest.main()
