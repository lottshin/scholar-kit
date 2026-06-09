import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from formatter import format_apa, format_chicago, format_gbt7714, format_mla, generate_reference_list  # noqa: E402
import literature  # noqa: E402


class FormatterAndDownloadReportTests(unittest.TestCase):
    def test_gbt7714_formats_journal_and_thesis_by_document_type(self):
        journal = {
            "authors": "张三; 李四; 王五; 赵六",
            "title": "人工智能赋能学术研究",
            "journal": "情报理论与实践",
            "year": 2025,
            "volume": "48",
            "issue": "2",
            "pages": "12-20",
            "doc_type": "journal",
        }
        thesis = {
            "authors": "朱兴财",
            "title": "生成式人工智能应用研究",
            "school": "暨南大学",
            "year": 2026,
            "doc_type": "thesis",
        }

        self.assertEqual(
            format_gbt7714(journal, 1),
            "[1] 张三, 李四, 王五, 等. 人工智能赋能学术研究[J]. "
            "情报理论与实践, 2025, 48(2): 12-20.",
        )
        self.assertEqual(
            format_gbt7714(thesis, 2),
            "[2] 朱兴财. 生成式人工智能应用研究[D]. 暨南大学, 2026.",
        )

    def test_apa_mla_and_chicago_are_available(self):
        paper = {
            "authors": "Jane Smith; John Doe",
            "title": "AI for Academic Search",
            "journal": "Journal of Research Tools",
            "year": 2024,
            "volume": "12",
            "issue": "3",
            "pages": "45-67",
            "doi": "10.1000/example",
            "doc_type": "journal",
        }

        self.assertIn("Smith, J.", format_apa(paper))
        self.assertIn("https://doi.org/10.1000/example", format_apa(paper))
        self.assertTrue(format_mla(paper).startswith('Smith, Jane, and John Doe. "AI for Academic Search."'))
        self.assertTrue(format_chicago(paper).startswith('Smith, Jane, and John Doe. "AI for Academic Search."'))
        self.assertIn("https://doi.org/10.1000/example", generate_reference_list([paper], "mla"))

    def test_download_report_marks_fallback_format(self):
        result = {
            "status": "success",
            "results": [
                {
                    "url": "https://kns.cnki.net/a",
                    "title": "下载成功",
                    "filename": "a.caj",
                    "format": "caj",
                    "requested_format": "pdf",
                    "fallback_used": True,
                },
            ],
            "errors": None,
        }
        session = [{
            "url": "https://kns.cnki.net/a",
            "authors": "张三",
            "title": "下载成功",
            "journal": "新闻大学",
            "year": 2024,
            "doc_type": "journal",
        }]

        report = literature.build_download_report(
            result,
            session_papers=session,
            requested_urls=["https://kns.cnki.net/a"],
            citation_style="gbt7714",
            file_format="pdf",
        )

        self.assertIn("[1] 张三. 下载成功[J]. 新闻大学, 2024.", report["downloaded_references"])
        self.assertIn("格式：CAJ", report["markdown"])
        self.assertIn("由 PDF 降级", report["markdown"])

    def test_batch_download_cli_falls_back_to_caj_for_pdf_button_failures(self):
        calls = []

        def fake_batch_download_cnki(urls, save_dir, file_format="pdf", **_kwargs):
            calls.append((tuple(urls), save_dir, file_format))
            if file_format == "pdf":
                return {
                    "status": "error",
                    "count": 0,
                    "save_dir": str(Path(save_dir) / "pdf"),
                    "requested_format": "pdf",
                    "results": [],
                    "errors": [{
                        "url": "https://kns.cnki.net/a",
                        "title": "下载失败",
                        "code": "DOWNLOAD_BTN_NOT_FOUND",
                        "error": "未找到pdf下载按钮",
                    }],
                }
            return {
                "status": "success",
                "count": 1,
                "save_dir": str(Path(save_dir) / "caj"),
                "requested_format": "caj",
                "results": [{
                    "url": "https://kns.cnki.net/a",
                    "title": "下载失败",
                    "filename": "a.caj",
                    "format": "caj",
                    "requested_format": "caj",
                }],
                "errors": None,
            }

        with patch.object(literature, "batch_download_cnki", side_effect=fake_batch_download_cnki):
            out = io.StringIO()
            with redirect_stdout(out):
                literature.cmd_batch_download(
                    argparse.Namespace(
                        urls=["https://kns.cnki.net/a"],
                        from_session=False,
                        top_n=None,
                        dir="./papers",
                        file_format="pdf",
                        fallback_format="caj",
                        citation_style="gbt7714",
                        report_output=None,
                        no_report=True,
                        project=None,
                    )
                )

        data = json.loads(out.getvalue())
        self.assertEqual(calls[0][2], "pdf")
        self.assertEqual(calls[1][2], "caj")
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["results"][0]["format"], "caj")
        self.assertTrue(data["results"][0]["fallback_used"])


if __name__ == "__main__":
    unittest.main()
