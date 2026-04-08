from __future__ import annotations

import argparse
import html
import os
import re
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from string import Template
from textwrap import dedent
from typing import Iterable
from urllib.parse import quote_plus


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = BASE_DIR / "templates" / "report_template.html"
DEFAULT_REPORT_TITLE = "競品市場情報每日報告"
DEFAULT_MODEL_NAME = "gemini-3-flash-preview"
DEFAULT_COMPETITORS = (
    "AAEON",
    "Adlink",
    "Advantech",
    "Arbor",
    "Asrock",
    "Avalue",
    "Axiomtek",
    "BizLink",
    "Congatec",
    "GIGAIPC",
    "iBase",
    "iEi",
    "JWIPC",
    "Kontron",
    "Lanner",
    "moxa",
    "Neousys",
    "Nexcom",
    "OnLogic",
    "Supermicro",
    "Syslogic",
    "Vecow",
    "yuan",
    "avermedia",
    "Auvidea",
    "connecttech",
    "miivii",
)
DEFAULT_TARGET_KEYWORD = "Edge AI OR Industrial PC OR Embedded"
DEFAULT_NEWS_LIMIT = 8
DEFAULT_NEWS_PER_COMPANY_LIMIT = 3
DEFAULT_NEWS_DAYS = 7
AMBIGUOUS_COMPETITOR_NAMES = frozenset({"arbor", "yuan"})
DEFAULT_SYSTEM_INSTRUCTION = (
    "你是一位專業的工業電腦 (IPC) 與嵌入式 AI 產業分析師，熟悉邊緣運算、工業自動化及競品市場動態。"
    "你的任務是協助用戶分析競爭對手的最新新聞，提供客觀、精確且具洞察力的每日競品情報。"
    "回應時請使用繁體中文，條列清晰，並以產業視角提供有價值的觀察與建議。"
)


@dataclass(frozen=True)
class ReportSettings:
    api_key: str
    model_name: str
    system_instruction: str
    competitors: tuple[str, ...]
    target_keyword: str
    news_limit: int
    per_company_news_limit: int
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


def load_settings(
    template_override: str | None,
    news_days_override: int | None = None,
    per_company_limit_override: int | None = None,
) -> ReportSettings:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model_name = os.getenv("GEMINI_MODEL", DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME
    system_instruction = os.getenv("LLM_SYSTEM_INSTRUCTION", DEFAULT_SYSTEM_INSTRUCTION).strip() or DEFAULT_SYSTEM_INSTRUCTION
    competitors = parse_csv_env(os.getenv("COMPETITORS"), DEFAULT_COMPETITORS)
    target_keyword = os.getenv("TARGET_KEYWORD", DEFAULT_TARGET_KEYWORD).strip() or DEFAULT_TARGET_KEYWORD
    news_limit = parse_positive_int(os.getenv("NEWS_LIMIT"), DEFAULT_NEWS_LIMIT, "NEWS_LIMIT")
    per_company_news_limit = parse_positive_int(
        os.getenv("NEWS_PER_COMPANY_LIMIT"),
        DEFAULT_NEWS_PER_COMPANY_LIMIT,
        "NEWS_PER_COMPANY_LIMIT",
    )
    news_days = parse_positive_int(os.getenv("NEWS_DAYS"), DEFAULT_NEWS_DAYS, "NEWS_DAYS")
    if news_days_override is not None:
        news_days = validate_positive_int(news_days_override, "--news-days")
    if per_company_limit_override is not None:
        per_company_news_limit = validate_positive_int(per_company_limit_override, "--per-company-limit")
    report_title = os.getenv("REPORT_TITLE", DEFAULT_REPORT_TITLE).strip() or DEFAULT_REPORT_TITLE
    template_path = resolve_template_path(template_override or os.getenv("REPORT_TEMPLATE_PATH"))

    return ReportSettings(
        api_key=api_key,
        model_name=model_name,
        system_instruction=system_instruction,
        competitors=competitors,
        target_keyword=target_keyword,
        news_limit=news_limit,
        per_company_news_limit=per_company_news_limit,
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
        from google import genai
        from google.genai import types as genai_types
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'google-genai'. Install it with "
            "`pip install google-genai` or `pip install -r requirements.txt`."
        ) from exc

    return genai, genai_types


def build_query(competitors: Iterable[str], target_keyword: str, news_days: int) -> str:
    base_query = f"({' OR '.join(competitors)}) AND ({target_keyword})"
    # Google News accepts the `when:Nd` syntax for a rolling lookback window.
    return f"{base_query} when:{news_days}d"


def build_rss_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"


def parse_target_keyword_phrases(target_keyword: str) -> tuple[str, ...]:
    phrases = [segment.strip().strip('"') for segment in re.split(r"\s+OR\s+", target_keyword, flags=re.IGNORECASE)]
    parsed = tuple(phrase for phrase in phrases if phrase)
    return parsed or (target_keyword.strip(),)


def unique_strings(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []

    for item in items:
        normalized = item.strip()
        if not normalized:
            continue

        key = normalized.casefold()
        if key in seen:
            continue

        seen.add(key)
        result.append(normalized)

    return tuple(result)


def build_expanded_keyword_terms(target_keyword: str) -> tuple[str, ...]:
    phrases = parse_target_keyword_phrases(target_keyword)
    expanded_terms: list[str] = list(phrases)

    for phrase in phrases:
        lowered = phrase.casefold()
        if "edge ai" in lowered:
            expanded_terms.extend(["AI", "Edge"])
        if "industrial pc" in lowered:
            expanded_terms.extend(["Industrial", "IPC"])
        if "embedded" in lowered:
            expanded_terms.extend(["Embedded"])

    expanded_terms.extend(["AI", "IPC"])
    return unique_strings(expanded_terms)


def build_local_relevance_terms(target_keyword: str) -> tuple[str, ...]:
    expanded_terms = list(build_expanded_keyword_terms(target_keyword))

    for phrase in parse_target_keyword_phrases(target_keyword):
        lowered = phrase.casefold()
        if "edge ai" in lowered:
            expanded_terms.extend(["邊緣", "邊緣運算"])
        if "industrial pc" in lowered:
            expanded_terms.extend(["工業", "工業電腦"])
        if "embedded" in lowered:
            expanded_terms.extend(["嵌入式"])

    return unique_strings(expanded_terms)


def format_google_news_term(term: str) -> str:
    return f'"{term}"' if re.search(r"\s", term) else term


def build_company_query(company: str, target_keyword: str, news_days: int, broadened: bool = False) -> str:
    keyword_terms = build_expanded_keyword_terms(target_keyword) if broadened else parse_target_keyword_phrases(target_keyword)
    keyword_clause = " OR ".join(format_google_news_term(term) for term in keyword_terms)
    return f'"{company}" ({keyword_clause}) when:{news_days}d'


def entry_published_at(entry) -> datetime | None:
    for parsed_key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed_value = entry.get(parsed_key)
        if parsed_value:
            return datetime.fromtimestamp(timegm(parsed_value), tz=timezone.utc)

    for raw_key in ("published", "updated", "created"):
        raw_value = entry.get(raw_key)
        if not raw_value:
            continue

        try:
            parsed_datetime = parsedate_to_datetime(str(raw_value))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue

        if parsed_datetime.tzinfo is None:
            return parsed_datetime.replace(tzinfo=timezone.utc)
        return parsed_datetime.astimezone(timezone.utc)

    return None


def filter_recent_entries(entries, news_days: int, news_limit: int | None = None, now: datetime | None = None):
    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(days=news_days)
    recent_entries = []

    for entry in entries:
        published_at = entry_published_at(entry)
        if published_at is None:
            continue
        if published_at >= cutoff:
            recent_entries.append((published_at, entry))

    recent_entries.sort(key=lambda item: item[0], reverse=True)
    if news_limit is None:
        return [entry for _, entry in recent_entries]
    return [entry for _, entry in recent_entries[:news_limit]]


def strip_html_tags(value: str | None) -> str:
    return re.sub(r"<[^>]+>", " ", value or " ")


def build_entry_search_text(entry) -> str:
    parts = [
        str(entry.get("title") or ""),
        str(entry_source_text(entry)),
        strip_html_tags(str(entry.get("summary") or "")),
    ]
    return re.sub(r"\s+", " ", " ".join(parts)).casefold().strip()


def entry_matches_relevance(entry, local_terms: tuple[str, ...], company: str) -> bool:
    search_text = build_entry_search_text(entry)
    if not any(term.casefold() in search_text for term in local_terms):
        return False

    if company.casefold() not in AMBIGUOUS_COMPETITOR_NAMES:
        return True

    strong_terms = (
        "edge ai",
        "industrial",
        "industrial pc",
        "embedded",
        "ipc",
        "邊緣",
        "邊緣運算",
        "工業",
        "工業電腦",
        "嵌入式",
    )
    return any(term.casefold() in search_text for term in strong_terms)


def normalize_title(title: str) -> str:
    """Return the core part of a news title by stripping trailing source attribution.

    Removes patterns like ` - SourceName` or ` | Author | Section - SourceName`
    that aggregators and syndication services append to the original headline.
    """
    normalized = title.strip()
    # Iteratively strip trailing ` | something` and ` - something` segments
    # until the title stabilises, to handle patterns like `Title | Author | Section - Source`.
    # Require at least one whitespace before the separator so that intra-word hyphens
    # (e.g. "AI-Powered") are not stripped.
    while True:
        prev = normalized
        normalized = re.sub(r"\s+\|[^|]*$", "", normalized).strip()
        normalized = re.sub(r"\s+-[^-]+$", "", normalized).strip()
        if normalized == prev:
            break
    return normalized


def dedupe_entries(entries):
    seen: dict[tuple[str, str], int] = {}
    seen_normalized: dict[str, int] = {}
    deduped = []

    for entry in entries:
        key = (str(entry.get("link") or ""), str(entry.get("title") or ""))
        norm_key = normalize_title(str(entry.get("title") or "")).casefold()

        existing_idx: int | None = None
        if key in seen:
            existing_idx = seen[key]
        elif norm_key and norm_key in seen_normalized:
            existing_idx = seen_normalized[norm_key]

        if existing_idx is not None:
            existing_entry = deduped[existing_idx]
            merged_companies = unique_strings([*entry_company_names(existing_entry), *entry_company_names(entry)])
            if merged_companies:
                existing_entry["companies"] = merged_companies
                existing_entry["company"] = "、".join(merged_companies)
            continue

        idx = len(deduped)
        seen[key] = idx
        if norm_key:
            seen_normalized[norm_key] = idx
        deduped.append(entry)

    return deduped


def fetch_entries_for_query(query: str):
    feedparser = import_feedparser()
    feed = feedparser.parse(build_rss_url(query))
    return list(feed.entries)


def entry_company_names(entry) -> tuple[str, ...]:
    raw_companies = entry.get("companies")
    if isinstance(raw_companies, (list, tuple)):
        parsed_companies = unique_strings(str(company) for company in raw_companies if str(company).strip())
        if parsed_companies:
            return parsed_companies

    raw_company = str(entry.get("company") or "").strip()
    if raw_company:
        return (raw_company,)
    return ()


def entry_company_text(entry) -> str:
    companies = entry_company_names(entry)
    return "、".join(companies) if companies else "未知公司"


def tag_entries_with_company(entries, company: str):
    tagged_entries = []
    for entry in entries:
        tagged_entry = dict(entry)
        tagged_entry["companies"] = unique_strings([company, *entry_company_names(tagged_entry)])
        tagged_entry["company"] = "、".join(tagged_entry["companies"])
        tagged_entries.append(tagged_entry)
    return tagged_entries


def fetch_competitor_news(
    competitors: tuple[str, ...],
    target_keyword: str,
    news_limit: int,
    news_days: int,
    per_company_news_limit: int,
):
    local_terms = build_local_relevance_terms(target_keyword)
    all_entries = []
    strict_hit_companies = 0
    fallback_hit_companies = 0

    for company in competitors:
        strict_query = build_company_query(company, target_keyword, news_days, broadened=False)
        strict_entries = filter_recent_entries(fetch_entries_for_query(strict_query), news_days, news_limit=None)
        strict_entries = tag_entries_with_company(strict_entries[:per_company_news_limit], company)

        if strict_entries:
            strict_hit_companies += 1
            all_entries.extend(strict_entries)
            continue

        if company.casefold() in AMBIGUOUS_COMPETITOR_NAMES:
            continue

        fallback_query = build_company_query(company, target_keyword, news_days, broadened=True)
        fallback_entries = filter_recent_entries(fetch_entries_for_query(fallback_query), news_days, news_limit=None)
        fallback_entries = [entry for entry in fallback_entries if entry_matches_relevance(entry, local_terms, company)]
        fallback_entries = tag_entries_with_company(fallback_entries[:per_company_news_limit], company)

        if fallback_entries:
            fallback_hit_companies += 1
            all_entries.extend(fallback_entries)

    entries = dedupe_entries(all_entries)
    entries.sort(key=lambda entry: entry_published_at(entry) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    entries = entries[:news_limit]

    query_summary = (
        f'Per-company query: "<company>" ({target_keyword}) when:{news_days}d; '
        f'fallback: "<company>" ({" OR ".join(format_google_news_term(term) for term in build_expanded_keyword_terms(target_keyword))}) '
        f'when:{news_days}d; local date/relevance filter enabled; '
        f'per-company cap: {per_company_news_limit}'
    )
    print(
        f"--- 🔍 逐家公司搜尋完成：strict 命中 {strict_hit_companies} 家，fallback 命中 {fallback_hit_companies} 家，"
        f"每家公司最多 {per_company_news_limit} 則，近 {news_days} 天保留 {len(entries)} 則相關動態 ---\n"
    )
    return entries, query_summary, "https://news.google.com/"


def entry_source_text(entry) -> str:
    source = entry.get("source")
    if isinstance(source, dict):
        return source.get("title") or source.get("text") or source.get("href") or "未知來源"
    if source:
        return str(source)
    return "未知來源"


def build_analysis_prompt(competitors: tuple[str, ...], news_entries) -> str:
    news_lines = [
        f"- 公司: {entry_company_text(entry)} | 標題: {entry.get('title') or '（無標題）'} (來源: {entry_source_text(entry)})"
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


def analyze_with_gemini(api_key: str, model_name: str, system_instruction: str, competitors: tuple[str, ...], news_entries) -> str:
    if not news_entries:
        return "今日無相關重要新聞，未送出 Gemini 分析。"

    if not api_key:
        return "⚠️ GEMINI_API_KEY 未設定，已略過 Gemini 分析。"

    genai, genai_types = import_gemini()
    client = genai.Client(api_key=api_key)

    prompt = build_analysis_prompt(competitors, news_entries)
    generate_content_kwargs = {"model": model_name, "contents": prompt}
    if system_instruction:
        generate_content_kwargs["config"] = genai_types.GenerateContentConfig(system_instruction=system_instruction)

    try:
        response = client.models.generate_content(**generate_content_kwargs)
    except Exception as exc:
        return f"❌ AI 辨識發生錯誤: {exc}"

    text = getattr(response, "text", "").strip()
    return text or "⚠️ Gemini 沒有回傳任何內容。"


def render_analysis_block(analysis: str) -> str:
    content = html.escape(analysis or "⚠️ 尚未取得分析內容。")
    return f'<pre class="analysis-block">{content}</pre>'


def render_news_rows(entries) -> str:
    if not entries:
        return '<tr class="empty-state"><td colspan="6">今日無相關重要新聞。</td></tr>'

    rows = []
    for index, entry in enumerate(entries, start=1):
        company = html.escape(entry_company_text(entry))
        title = html.escape(str(entry.get("title") or "（無標題）"))
        source = html.escape(entry_source_text(entry))
        published = html.escape(str(entry.get("published") or entry.get("updated") or "未知時間"))
        link = html.escape(str(entry.get("link") or "#"), quote=True)

        rows.append(
            "<tr>"
            f"<td class=\"index-cell\">{index}</td>"
            f"<td class=\"company-cell\">{company}</td>"
            f"<td class=\"title-cell\">{title}</td>"
            f"<td class=\"source-cell\">{source}</td>"
            f"<td class=\"time-cell\">{published}</td>"
            f"<td class=\"link-cell\"><a href=\"{link}\" target=\"_blank\" rel=\"noreferrer\">查看</a></td>"
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
    print(f"🏢 每家公司上限: {settings.per_company_news_limit} 則")
    print(analysis)
    print("=" * 50)
    print(f"\n📄 HTML 報告已寫入: {output_path}")

    if entries:
        print("\n報告生成完畢。連結參考：")
        for entry in entries:
            print(f"🔗 [{entry_company_text(entry)}] {entry.get('title') or '（無標題）'} -> {entry.get('link') or ''}")
    else:
        print("\n今日無相關重要新聞。")


def generate_report(settings: ReportSettings, output_path: Path) -> Path:
    entries, query, rss_url = fetch_competitor_news(
        settings.competitors,
        settings.target_keyword,
        settings.news_limit,
        settings.news_days,
        settings.per_company_news_limit,
    )
    generated_at = datetime.now()

    analysis = analyze_with_gemini(settings.api_key, settings.model_name, settings.system_instruction, settings.competitors, entries)

    template_context = build_template_context(
        settings=settings,
        generated_at=generated_at,
        query=query,
        rss_url=rss_url,
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
    parser.add_argument("--per-company-limit", type=int, default=None, help="Maximum news items kept for each company")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file()
    settings = load_settings(args.template, args.news_days, args.per_company_limit)
    output_path = build_output_path(args.output)
    generate_report(settings, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
