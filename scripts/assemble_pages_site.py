#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

API_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "ipc-fans-pages-builder/1.0"


@dataclass(frozen=True)
class CadenceSpec:
    slug: str
    label: str
    workflow_file: str
    artifact_name: str


@dataclass(frozen=True)
class PublishedReport:
    spec: CadenceSpec
    metadata: dict[str, Any]
    run_id: int
    run_url: str


class GitHubClient:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token

    def _request(self, url: str, accept: str = "application/vnd.github+json") -> Request:
        return Request(
            url,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )

    def get_json(self, url: str) -> dict[str, Any]:
        with urlopen(self._request(url)) as response:
            return json.load(response)

    def download_bytes(self, url: str) -> bytes:
        try:
            with urlopen(self._request(url)) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 415:
                raise RuntimeError(
                    "GitHub artifact download returned HTTP 415. "
                    "The REST API currently expects the default GitHub JSON Accept header and returns a redirect URL for downloads."
                ) from exc
            raise

    def list_repository_runs(self, per_page: int = 100) -> list[dict[str, Any]]:
        url = f"{API_BASE_URL}/repos/{self.repo}/actions/runs?status=success&exclude_pull_requests=true&per_page={per_page}"
        payload = self.get_json(url)
        return list(payload.get("workflow_runs") or [])

    def get_latest_successful_run(self, workflow_file: str) -> dict[str, Any] | None:
        url = f"{API_BASE_URL}/repos/{self.repo}/actions/workflows/{workflow_file}/runs?status=success&per_page=1"
        payload = self.get_json(url)
        workflow_runs = payload.get("workflow_runs") or []
        if workflow_runs:
            return workflow_runs[0]

        workflow_path_markers = (
            f".github/workflows/{workflow_file}@",
            f"/.github/workflows/{workflow_file}@",
        )
        for run in self.list_repository_runs():
            run_path = str(run.get("path") or "")
            if any(marker in run_path for marker in workflow_path_markers):
                return run
        return None

    def list_run_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        url = f"{API_BASE_URL}/repos/{self.repo}/actions/runs/{run_id}/artifacts?per_page=100"
        payload = self.get_json(url)
        return list(payload.get("artifacts") or [])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble the GitHub Pages site from the latest daily and weekly report artifacts.")
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name format")
    parser.add_argument("--token", required=True, help="GitHub token with actions:read access")
    parser.add_argument("--output-dir", required=True, help="Directory where the final static site will be written")
    parser.add_argument("--daily-workflow-file", required=True)
    parser.add_argument("--daily-artifact-name", required=True)
    parser.add_argument("--weekly-workflow-file", required=True)
    parser.add_argument("--weekly-artifact-name", required=True)
    return parser.parse_args()


def load_artifact_bundle(client: GitHubClient, spec: CadenceSpec, output_dir: Path) -> PublishedReport | None:
    run = client.get_latest_successful_run(spec.workflow_file)
    if run is None:
        print(f"ℹ️ No successful run found for {spec.workflow_file}")
        return None

    run_id = int(run["id"])
    run_url = str(run.get("html_url") or f"https://github.com/{client.repo}/actions/runs/{run_id}")
    artifact = next(
        (
            item
            for item in client.list_run_artifacts(run_id)
            if item.get("name") == spec.artifact_name and not item.get("expired", False)
        ),
        None,
    )
    if artifact is None:
        print(f"ℹ️ Artifact {spec.artifact_name} not found in run {run_id}")
        return None

    archive_bytes = client.download_bytes(str(artifact["archive_download_url"]))

    with tempfile.TemporaryDirectory(prefix=f"ipc-fans-{spec.slug}-") as temp_dir:
        temp_path = Path(temp_dir)
        archive_path = temp_path / "artifact.zip"
        archive_path.write_bytes(archive_bytes)

        extract_dir = temp_path / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as zip_file:
            zip_file.extractall(extract_dir)

        metadata_path = next(extract_dir.rglob("metadata.json"), None)
        report_path = next(extract_dir.rglob("report.html"), None)
        if metadata_path is None or report_path is None:
            raise SystemExit(f"Artifact {spec.artifact_name} is missing report.html or metadata.json")

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        destination_dir = output_dir / spec.slug
        destination_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report_path, destination_dir / "index.html")
        shutil.copyfile(report_path, destination_dir / "latest.html")

        data_dir = output_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        data_path = data_dir / f"{spec.slug}.json"
        data_payload = {
            **metadata,
            "run_id": run_id,
            "run_url": run_url,
            "artifact_name": spec.artifact_name,
        }
        data_path.write_text(json.dumps(data_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return PublishedReport(spec=spec, metadata=data_payload, run_id=run_id, run_url=run_url)


def render_status_card(report: PublishedReport | None, fallback_title: str, path_prefix: str) -> str:
    if report is None:
        return (
            '<article class="card unavailable">'
            f"<h2>{html.escape(fallback_title)}</h2>"
            "<p class=\"status\">目前沒有可用輸出</p>"
            "<p class=\"muted\">請先執行對應排程或從 Actions 手動補跑。</p>"
            "</article>"
        )

    metadata = report.metadata
    summary_items = metadata.get("executive_summary") or []
    summary_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in summary_items[:3])
    keywords_html = "".join(
        f'<span class="chip">{html.escape(str(item))}</span>' for item in (metadata.get("top_keywords") or [])[:3]
    )
    generated_at = html.escape(str(metadata.get("generated_at") or "未知"))
    news_count = html.escape(str(metadata.get("news_count") or 0))
    major_shift = html.escape(str(metadata.get("major_shift") or "無"))
    title = html.escape(str(metadata.get("email_subject") or fallback_title))
    week_range = html.escape(str(metadata.get("week_range") or ""))

    return (
        '<article class="card">'
        f"<div class=\"card-head\"><span class=\"badge\">{html.escape(report.spec.label)}</span>"
        f"<a class=\"link\" href=\"{path_prefix}/{report.spec.slug}/\">開啟報告</a></div>"
        f"<h2>{title}</h2>"
        f"<p class=\"meta\">區間：{week_range or '未提供'} · 新聞數：{news_count} · 更新：{generated_at}</p>"
        f"<p class=\"major\">{major_shift}</p>"
        f"<div class=\"chips\">{keywords_html}</div>"
        f"<ul>{summary_html}</ul>"
        f"<p class=\"muted\"><a href=\"{html.escape(report.run_url, quote=True)}\">查看 workflow run</a></p>"
        "</article>"
    )


def build_index_html(reports: list[PublishedReport | None]) -> str:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    cards_html = "".join(
        render_status_card(
            report,
            fallback_title="IPC / Edge AI 競品報告",
            path_prefix=".",
        )
        for report in reports
    )
    return f"""<!doctype html>
<html lang=\"zh-Hant\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>IPC / Edge AI 報告入口</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --border: #dbe4f0;
      --text: #0f172a;
      --muted: #64748b;
      --brand: #1d4ed8;
      --brand-soft: #dbeafe;
      --warn: #b45309;
      --shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: linear-gradient(180deg, #f8fbff 0%, var(--bg) 100%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, \"Segoe UI\", \"Noto Sans TC\", sans-serif;
      line-height: 1.6;
    }}
    .page {{
      width: min(1100px, calc(100% - 32px));
      margin: 0 auto;
      padding: 40px 0 64px;
    }}
    .hero {{
      margin-bottom: 24px;
      padding: 28px;
      border: 1px solid var(--border);
      border-radius: 24px;
      background: linear-gradient(135deg, #0f172a 0%, #172554 55%, #1e3a8a 100%);
      color: #e2e8f0;
      box-shadow: var(--shadow);
    }}
    .eyebrow {{
      display: inline-block;
      margin-bottom: 12px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(219, 234, 254, 0.16);
      color: #93c5fd;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    h1 {{ margin: 0 0 10px; font-size: clamp(2rem, 4vw, 3rem); line-height: 1.15; }}
    .hero p {{ margin: 0; color: #cbd5e1; }}
    .cards {{ display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
    .card {{
      padding: 24px;
      border: 1px solid var(--border);
      border-radius: 22px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }}
    .card.unavailable {{ border-style: dashed; }}
    .card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 14px; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      background: var(--brand-soft);
      color: var(--brand);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .link {{ color: var(--brand); font-weight: 700; text-decoration: none; }}
    .card h2 {{ margin: 0 0 10px; font-size: 24px; line-height: 1.3; }}
    .meta {{ margin: 0 0 12px; color: var(--muted); font-size: 14px; }}
    .major {{ margin: 0 0 14px; font-weight: 700; color: #1e293b; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }}
    .chip {{
      display: inline-flex;
      padding: 4px 10px;
      border-radius: 999px;
      background: #eff6ff;
      color: var(--brand);
      font-size: 12px;
      font-weight: 700;
    }}
    ul {{ margin: 0; padding-left: 18px; }}
    li + li {{ margin-top: 8px; }}
    .muted {{ margin: 14px 0 0; color: var(--muted); font-size: 14px; }}
    .status {{ margin: 0 0 8px; font-weight: 700; color: var(--warn); }}
  </style>
</head>
<body>
  <main class=\"page\">
    <section class=\"hero\">
      <span class=\"eyebrow\">GitHub Pages Portal</span>
      <h1>IPC / Edge AI 競品報告入口</h1>
      <p>固定入口提供 daily 與 weekly 最新版本。完整歷史輸出保留在 GitHub Actions artifacts，Pages 僅承載最新可閱讀版本。</p>
      <p style=\"margin-top: 10px;\">站點更新時間：{html.escape(generated_at)}</p>
    </section>
    <section class=\"cards\">{cards_html}</section>
  </main>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = GitHubClient(repo=args.repo, token=args.token)
    specs = [
        CadenceSpec("daily", "Daily", args.daily_workflow_file, args.daily_artifact_name),
        CadenceSpec("weekly", "Weekly", args.weekly_workflow_file, args.weekly_artifact_name),
    ]
    reports = [load_artifact_bundle(client, spec, output_dir) for spec in specs]
    if all(report is None for report in reports):
        raise SystemExit("No successful daily or weekly report artifacts were found.")

    index_html = build_index_html(reports)
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
    print(f"✅ Pages site assembled at {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
