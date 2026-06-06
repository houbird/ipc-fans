import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.assemble_pages_site import CadenceSpec, load_artifact_bundle, render_status_card


class _FakeGitHubClient:
    repo = "houbird/ipc-fans"

    def __init__(self, archive_bytes: bytes) -> None:
        self._archive_bytes = archive_bytes

    def get_latest_successful_run(self, workflow_file: str) -> dict[str, object] | None:
        return {"id": 123, "html_url": "https://github.com/houbird/ipc-fans/actions/runs/123"}

    def list_run_artifacts(self, run_id: int) -> list[dict[str, object]]:
        return [{"name": "report-daily", "expired": False, "archive_download_url": "https://example.com/artifact.zip"}]

    def download_bytes(self, url: str) -> bytes:
        return self._archive_bytes


def _build_artifact_zip() -> bytes:
    metadata = {
        "email_subject": "Daily report",
        "report_range": "2026-06-01 ~ 2026-06-02",
        "generated_at": "2026-06-02T10:00:00+08:00",
        "news_count": 4,
        "major_shift": "Major shift",
        "top_keywords": ["k1", "k2"],
        "executive_summary": ["s1"],
        "edm_report_path": "report-edm.html",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        zip_file.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        zip_file.writestr("report.html", "<html><body>report</body></html>")
        zip_file.writestr("report-edm.html", "<html><body>edm</body></html>")
    return buffer.getvalue()


class AssemblePagesSiteTests(unittest.TestCase):
    def test_load_artifact_bundle_publishes_edm_html(self) -> None:
        spec = CadenceSpec("daily", "1 Day", "report-daily.yml", "report-daily")
        client = _FakeGitHubClient(_build_artifact_zip())
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            report = load_artifact_bundle(client, spec, output_dir)

            self.assertIsNotNone(report)
            self.assertTrue((output_dir / "daily" / "index.html").exists())
            self.assertTrue((output_dir / "daily" / "latest.html").exists())
            self.assertTrue((output_dir / "daily" / "edm.html").exists())

            metadata = json.loads((output_dir / "data" / "daily.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["pages_edm_path"], "edm.html")
            self.assertEqual(metadata["edm_report_path"], "report-edm.html")

    def test_render_status_card_includes_edm_link(self) -> None:
        spec = CadenceSpec("daily", "1 Day", "report-daily.yml", "report-daily")
        with tempfile.TemporaryDirectory() as temp_dir:
            report = load_artifact_bundle(_FakeGitHubClient(_build_artifact_zip()), spec, Path(temp_dir))
            self.assertIsNotNone(report)
            card_html = render_status_card(report, fallback_title="fallback", path_prefix=".")
            self.assertIn("./daily/edm.html", card_html)


if __name__ == "__main__":
    unittest.main()
