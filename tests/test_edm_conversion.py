import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from main import build_edm_output_path, convert_report_html_to_edm


SAMPLE_REPORT_HTML = """\
<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <title>Sample</title>
  <style>.meta td{padding:8px;}</style>
</head>
<body>
  <div class="wrapper">
    <table class="container" role="presentation">
      <tr>
        <td class="header"><h1>Weekly EDM Subject</h1></td>
      </tr>
      <tr>
        <td class="meta">
          <table role="presentation">
            <tr>
              <td><strong>Report Range</strong>2026-06-01 ~ 2026-06-07</td>
              <td><strong>Generated At</strong>2026-06-07 08:00:00</td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td>
          <p class="analysis-major"><strong>重大變動提醒：</strong>供應鏈需求持續增溫</p>
          <img src="https://example.com/news.jpg" alt="news" />
          <div class="keyword-list"><span class="keyword-chip">Edge AI</span></div>
          <ul class="brief-list"><li>產業合作案增加</li></ul>
          <h3>結論與建議</h3>
          <ul class="signal-list"><li>持續追蹤供應鏈</li></ul>
          <h3>下個區間追蹤</h3>
          <ul class="signal-list"><li>留意新產品發表</li></ul>
        </td>
      </tr>
      <tr>
        <td class="source-wrap">
          <table class="source-table" role="presentation">
            <tbody>
              <tr>
                <td>1</td><td>Advantech</td><td>新一代邊緣平台發布</td><td>Example News</td><td>2026-06-06</td>
                <td><a href="https://example.com/a">查看</a></td>
              </tr>
            </tbody>
          </table>
        </td>
      </tr>
    </table>
  </div>
</body>
</html>
"""


class EdmConversionTests(unittest.TestCase):
    def test_convert_report_html_to_edm_enforces_table_inline_rules(self) -> None:
        edm_html = convert_report_html_to_edm(SAMPLE_REPORT_HTML, "Fallback Title")
        soup = BeautifulSoup(edm_html, "html.parser")

        self.assertIn("Weekly EDM Subject", edm_html)
        self.assertNotIn("<style", edm_html.lower())
        self.assertNotIn("<div", edm_html.lower())
        self.assertIsNotNone(soup.find("table"))

        for image in soup.find_all("img"):
            self.assertTrue(image.get("width"))
            self.assertIn("display:block", image.get("style", ""))

    def test_build_edm_output_path_adds_suffix(self) -> None:
        path = Path("/tmp/reports/report.html")
        self.assertEqual(build_edm_output_path(path), Path("/tmp/reports/report-edm.html"))


if __name__ == "__main__":
    unittest.main()
