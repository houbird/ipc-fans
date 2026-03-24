from __future__ import annotations

import argparse
import html
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Template
from textwrap import dedent
from typing import Iterable
from urllib.parse import quote_plus


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = BASE_DIR / "templates" / "report_template.html"
DEFAULT_REPORT_TITLE = "競品市場情報每日報告"
DEFAULT_MODEL_NAME = "gemini-3-flash-preview"
DEFAULT_COMPETITORS = ("Advantech", "Axiomtek", "Adlink")
DEFAULT_TARGET_KEYWORD = "Edge AI OR Industrial PC OR Embedded"
DEFAULT_NEWS_LIMIT = 8
DEFAULT_NEWS_DAYS = 7


@dataclass(frozen=True)
class ReportSettings:
    api_key: str
    model_name: str
    competitors: tuple[str, ...]
    target_keyword: str
    news_limit: int
    news_days: int
    report_title: str
    template_path: Path


def load_env_file(paths: Iterable[Path] | None = None) -> None:
    env_paths = tuple(paths) if paths is not None else (BASE_DIR / ".env", BASE_DIR / ".env.local")
    initial_keys = set(os.environ)

    for env_path in env_paths:
        if not env_path.exists():
            continue

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in initial_keys:
                continue

            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


def parse_csv_env(value: str | None, default: Iterable[str]) -> tuple[str, ...]:
    if not value or not value.strip():
        return tuple(item.strip() for item in default if str(item).strip())

    items = [item.strip() for item in value.split(",")]
    parsed = tuple(item for item in items if item)
    if parsed:
        return parsed
    return tuple(item.strip() for item in default if str(item).strip())


def parse_positive_int(value: str | None, default: int, field_name: str) -> int:
    if not value:
        return default

    try:
        parsed = int(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid integer value for {field_name}: {value!r}") from exc

    if parsed <= 0:
        raise SystemExit(f"{field_name} must be greater than zero.")
    return parsed


def validate_positive_int(value: int, flag_name: str) -> int:
    if value <= 0:
        raise SystemExit(f"{flag_name} must be greater than zero.")
    return value


def resolve_template_path(raw_path: str | None) -> Path:
    candidate = Path(raw_path).expanduser() if raw_path else DEFAULT_TEMPLATE_PATH
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate


def build_output_path(raw_output: str | None) -> Path:
    if raw_output:
        return Path(raw_output).expanduser()

    output_dir = Path(os.getenv("REPORT_OUTPUT_DIR", ".")).expanduser()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return output_dir / f"report-{timestamp}.html"


def load_settings(template_override: str | None, news_days_override: int | None = None) -> ReportSettings:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model_name = os.getenv("GEMINI_MODEL", DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME
    competitors = parse_csv_env(os.getenv("COMPETITORS"), DEFAULT_COMPETITORS)
    target_keyword = os.getenv("TARGET_KEYWORD", DEFAULT_TARGET_KEYWORD).strip() or DEFAULT_TARGET_KEYWORD
    news_limit = parse_positive_int(os.getenv("NEWS_LIMIT"), DEFAULT_NEWS_LIMIT, "NEWS_LIMIT")
    news_days = parse_positive_int(os.getenv("NEWS_DAYS"), DEFAULT_NEWS_DAYS, "NEWS_DAYS")
    if news_days_override is not None:
        news_days = validate_positive_int(news_days_override, "--news-days")
    report_title = os.getenv("REPORT_TITLE", DEFAULT_REPORT_TITLE).strip() or DEFAULT_REPORT_TITLE
    template_path = resolve_template_path(template_override or os.getenv("REPORT_TEMPLATE_PATH"))

    return ReportSettings(
        api_key=api_key,
        model_name=model_name,
        competitors=competitors,
        target_keyword=target_keyword,
        news_limit=news_limit,
        news_days=news_days,
        report_title=report_title,
        template_path=template_path,
    )


def import_feedparser():
    try:
        import feedparser
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'feedparser'. Install it with `pip install feedparser` "
            "or `pip install -r requirements.txt` before generating reports."
        ) from exc

    return feedparser


def import_gemini():
    try:
        import google.generativeai as genai
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'google-generativeai'. Install it with "
            "`pip install google-generativeai` or `pip install -r requirements.txt`."
        ) from exc

    return genai


def build_query(competitors: Iterable[str], target_keyword: str, news_days: int) -> str:
    base_query = f"({' OR '.join(competitors)}) AND ({target_keyword})"
    # Google News accepts the `when:Nd` syntax for a rolling lookback window.
    return f"{base_query} when:{news_days}d"


def fetch_competitor_news(competitors: tuple[str, ...], target_keyword: str, news_limit: int, news_days: int):
    feedparser = import_feedparser()
    query = build_query(competitors, target_keyword, news_days)
    rss_url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"

    feed = feedparser.parse(rss_url)
    entries = list(feed.entries[:news_limit])
    print(f"--- 🔍 偵測到 {len(entries)} 則相關動態 ---\n")
    return entries, query


def entry_source_text(entry) -> str:
    source = entry.get("source")
    if isinstance(source, dict):
        return source.get("title") or source.get("text") or source.get("href") or "未知來源"
    if source:
        return str(source)
    return "未知來源"


def build_analysis_prompt(competitors: tuple[str, ...], news_entries) -> str:
    news_lines = [
        f"- 標題: {entry.get('title') or '（無標題）'} (來源: {entry_source_text(entry)})"
        for entry in news_entries
    ]

    return dedent(
        f"""
        你現在是一位專業的工業電腦 (IPC) 產業分析師。
        以下是競爭對手 ({', '.join(competitors)}) 的最新新聞標題。
        請針對這些資訊進行「每日競品情報摘要」。

        格式要求：
        1. 項目分類：區分為 [產品發佈]、[市場合作]、[財報動態] 或 [技術亮點]。
        2. 影響評估：以 1-5 顆星標示對市場觀察或產品規劃的潛在影響。
        3. 關鍵摘要：用一句話說明該新聞的重點。
        4. 建議觀察：後續應關注哪些訊號？

        新聞清單：
        {chr(10).join(news_lines)}
        """
    ).strip()


def analyze_with_gemini(api_key: str, model_name: str, competitors: tuple[str, ...], news_entries) -> str:
    if not news_entries:
        return "今日無相關重要新聞，未送出 Gemini 分析。"

    if not api_key:
        return "⚠️ GEMINI_API_KEY 未設定，已略過 Gemini 分析。"

    genai = import_gemini()
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    prompt = build_analysis_prompt(competitors, news_entries)

    try:
        response = model.generate_content(prompt)
    except Exception as exc:
        return f"❌ AI 辨識發生錯誤: {exc}"

    text = getattr(response, "text", "").strip()
    return text or "⚠️ Gemini 沒有回傳任何內容。"


def render_analysis_block(analysis: str) -> str:
    content = html.escape(analysis or "⚠️ 尚未取得分析內容。")
    return f'<pre class="analysis-block">{content}</pre>'


def render_news_rows(entries) -> str:
    if not entries:
        return '<tr class="empty-state"><td colspan="5">今日無相關重要新聞。</td></tr>'

    rows = []
    for index, entry in enumerate(entries, start=1):
        title = html.escape(str(entry.get("title") or "（無標題）"))
        source = html.escape(entry_source_text(entry))
        published = html.escape(str(entry.get("published") or entry.get("updated") or "未知時間"))
        link = html.escape(str(entry.get("link") or "#"), quote=True)

        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td class=\"title-cell\">{title}</td>"
            f"<td>{source}</td>"
            f"<td>{published}</td>"
            f"<td><a href=\"{link}\" target=\"_blank\" rel=\"noreferrer\">查看</a></td>"
            "</tr>"
        )

    return "\n".join(rows)


def build_template_context(
    settings: ReportSettings,
    generated_at: datetime,
    query: str,
    rss_url: str,
    entries,
    analysis: str,
    output_path: Path,
) -> dict[str, str]:
    return {
        "report_title": html.escape(settings.report_title),
        "generated_at": html.escape(generated_at.strftime("%Y-%m-%d %H:%M:%S")),
        "model_name": html.escape(settings.model_name),
        "report_path": html.escape(output_path.name),
        "competitors": html.escape("、".join(settings.competitors)),
        "target_keyword": html.escape(settings.target_keyword),
        "query": html.escape(query),
        "rss_url": html.escape(rss_url, quote=True),
        "news_window": html.escape(f"近 {settings.news_days} 天"),
        "news_count": str(len(entries)),
        "analysis_block": render_analysis_block(analysis),
        "news_rows": render_news_rows(entries),
    }


def render_report(template_path: Path, context: dict[str, str]) -> str:
    if not template_path.exists():
        raise SystemExit(f"Report template not found: {template_path}")

    template_text = template_path.read_text(encoding="utf-8")
    return Template(template_text).substitute(context)


def write_report(output_path: Path, html_report: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_report, encoding="utf-8")


def print_console_summary(settings: ReportSettings, generated_at: datetime, entries, analysis: str, output_path: Path) -> None:
    print("\n" + "=" * 50)
    print(f"        🏆 {settings.report_title} 🏆")
    print("=" * 50)
    print(f"📅 生成時間: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🗓️ 搜尋區間: 近 {settings.news_days} 天")
    print(analysis)
    print("=" * 50)
    print(f"\n📄 HTML 報告已寫入: {output_path}")

    if entries:
        print("\n報告生成完畢。連結參考：")
        for entry in entries:
            print(f"🔗 {entry.get('title') or '（無標題）'} -> {entry.get('link') or ''}")
    else:
        print("\n今日無相關重要新聞。")


def generate_report(settings: ReportSettings, output_path: Path) -> Path:
    entries, query = fetch_competitor_news(
        settings.competitors,
        settings.target_keyword,
        settings.news_limit,
        settings.news_days,
    )
    generated_at = datetime.now()

    analysis = analyze_with_gemini(settings.api_key, settings.model_name, settings.competitors, entries)

    template_context = build_template_context(
        settings=settings,
        generated_at=generated_at,
        query=query,
        rss_url=f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        entries=entries,
        analysis=analysis,
        output_path=output_path,
    )
    report_html = render_report(settings.template_path, template_context)
    write_report(output_path, report_html)
    print_console_summary(settings, generated_at, entries, analysis, output_path)
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an IPC competitor report as HTML.")
    parser.add_argument("--output", help="HTML report output path")
    parser.add_argument("--template", help="Override the HTML report template path")
    parser.add_argument("--news-days", type=int, default=None, help="News lookback window in days")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file()
    settings = load_settings(args.template, args.news_days)
    output_path = build_output_path(args.output)
    generate_report(settings, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
