from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from string import Template
from textwrap import dedent
from typing import Any, Iterable
from urllib.parse import quote_plus
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = BASE_DIR / "templates" / "email_template.html"
LEGACY_REPORT_TITLE = "競品市場情報每日報告"
DEFAULT_REPORT_TITLE = "IPC / Edge AI 競品每週新聞 Email"
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
RSS_TIMEOUT_SECONDS = 15
GEMINI_MAX_RETRIES = 3
GEMINI_RETRYABLE_MARKERS = ("503", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "429")
AMBIGUOUS_COMPETITOR_NAMES = frozenset({"arbor", "yuan"})
IMPACT_LEVELS = ("★★★★★", "★★★★☆", "★★★☆☆", "★★☆☆☆", "★☆☆☆☆")
LEGACY_SYSTEM_INSTRUCTION = (
    "你是一位專業的工業電腦 (IPC) 與嵌入式 AI 產業分析師，熟悉邊緣運算、工業自動化及競品市場動態。"
    "你的任務是協助用戶分析競爭對手的最新新聞，提供客觀、精確且具洞察力的每日競品情報。"
    "回應時請使用繁體中文，條列清晰，並以產業視角提供有價值的觀察與建議。"
)
DEFAULT_SYSTEM_INSTRUCTION = (
    "你是一位專業的工業電腦 (IPC) 與嵌入式 AI 產業分析師，熟悉邊緣運算、工業自動化及競品市場動態。"
    "你的任務是協助用戶整理競爭對手的每週新聞，輸出適合高階主管快速閱讀的 email 摘要。"
    "回應時請使用繁體中文、結論先行、避免流水帳，並以產業視角提供具行動性的觀察與建議。"
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
    return output_dir / f"weekly-email-{timestamp}.html"


def load_settings(
    template_override: str | None,
    news_days_override: int | None = None,
    per_company_limit_override: int | None = None,
) -> ReportSettings:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model_name = os.getenv("GEMINI_MODEL", DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME
    system_instruction = os.getenv("LLM_SYSTEM_INSTRUCTION", DEFAULT_SYSTEM_INSTRUCTION).strip() or DEFAULT_SYSTEM_INSTRUCTION
    if system_instruction == LEGACY_SYSTEM_INSTRUCTION:
        system_instruction = DEFAULT_SYSTEM_INSTRUCTION
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
    if report_title == LEGACY_REPORT_TITLE:
        report_title = DEFAULT_REPORT_TITLE
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


def build_week_range_label(generated_at: datetime, news_days: int) -> str:
    end_date = generated_at.date()
    start_date = (generated_at - timedelta(days=max(news_days - 1, 0))).date()
    return f"{start_date:%Y/%m/%d} - {end_date:%Y/%m/%d}"


def normalize_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    normalized = str(value).strip()
    return normalized or default


def normalize_summary_text(value: str | None, fallback: str = "無摘要") -> str:
    text = re.sub(r"\s+", " ", strip_html_tags(value)).strip()
    return text or fallback


def normalize_impact_level(value: Any, default: str = "★★★☆☆") -> str:
    normalized = normalize_text(value, default)
    return normalized if normalized in IMPACT_LEVELS else default


def build_news_digest_line(entry, index: int) -> str:
    published_at = entry_published_at(entry)
    published_text = published_at.astimezone(timezone.utc).strftime("%Y-%m-%d") if published_at else "未知日期"
    return dedent(
        f"""
        [{index}]
        公司：{entry_company_text(entry)}
        標題：{normalize_text(entry.get('title'), '（無標題）')}
        來源：{entry_source_text(entry)}
        日期：{published_text}
        摘要：{normalize_summary_text(entry.get('summary'))}
        連結：{normalize_text(entry.get('link'), '無連結')}
        """
    ).strip()


def build_weekly_email_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "email_subject",
            "week_range",
            "top_keywords",
            "major_shift",
            "executive_summary",
            "heat_ranking",
            "news_sections",
            "conclusion_actions",
            "watchlist",
        ],
        "properties": {
            "email_subject": {
                "type": "string",
                "description": "適合作為每週新聞 email 主旨的一句話標題。",
            },
            "week_range": {
                "type": "string",
                "description": "本次週報涵蓋的日期區間，例如 2026/04/02 - 2026/04/08。",
            },
            "top_keywords": {
                "type": "array",
                "description": "本週最重要的 3 個趨勢關鍵詞。",
                "minItems": 3,
                "maxItems": 3,
                "items": {"type": "string"},
            },
            "major_shift": {
                "type": "string",
                "description": "用一句話總結本週最重要或最震撼的變化。",
            },
            "executive_summary": {
                "type": "array",
                "description": "給高階主管的 2 到 3 點結論式摘要。",
                "minItems": 2,
                "maxItems": 3,
                "items": {"type": "string"},
            },
            "heat_ranking": {
                "type": "array",
                "description": "競爭熱度排行，列出 3 到 5 個重要對象。",
                "minItems": 3,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["impact_level", "company_or_tech", "core_issue"],
                    "properties": {
                        "impact_level": {
                            "type": "string",
                            "enum": list(IMPACT_LEVELS),
                            "description": "影響等級，使用五級星等字串。",
                        },
                        "company_or_tech": {
                            "type": "string",
                            "description": "企業名稱、技術名稱或其組合。",
                        },
                        "core_issue": {
                            "type": "string",
                            "description": "一句話描述其核心議題。",
                        },
                    },
                },
            },
            "news_sections": {
                "type": "array",
                "description": "依主題分組的新聞概覽，建議 2 到 4 組。",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "items"],
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "區塊標題，例如 [市場合作]、[技術亮點]。",
                        },
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "company_or_tech",
                                    "impact_level",
                                    "headline",
                                    "key_summary",
                                    "why_it_matters",
                                    "suggested_watch",
                                ],
                                "properties": {
                                    "company_or_tech": {
                                        "type": "string",
                                        "description": "該則新聞的公司或技術焦點。",
                                    },
                                    "impact_level": {
                                        "type": "string",
                                        "enum": list(IMPACT_LEVELS),
                                        "description": "影響評估星等。",
                                    },
                                    "headline": {
                                        "type": "string",
                                        "description": "簡化過、適合 email 閱讀的新聞標題。",
                                    },
                                    "key_summary": {
                                        "type": "string",
                                        "description": "一句話描述重點。",
                                    },
                                    "why_it_matters": {
                                        "type": "string",
                                        "description": "說明這則新聞對市場或競品格局的重要性。",
                                    },
                                    "suggested_watch": {
                                        "type": "string",
                                        "description": "建議持續觀察的後續訊號。",
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "conclusion_actions": {
                "type": "array",
                "description": "結論與建議，給管理層的 2 到 4 點建議。",
                "minItems": 2,
                "maxItems": 4,
                "items": {"type": "string"},
            },
            "watchlist": {
                "type": "array",
                "description": "下週應追蹤的 2 到 4 個訊號。",
                "minItems": 2,
                "maxItems": 4,
                "items": {"type": "string"},
            },
        },
    }


def build_fallback_analysis_payload(news_entries, news_days: int, generated_at: datetime, reason: str) -> dict[str, Any]:
    week_range = build_week_range_label(generated_at, news_days)
    top_entries = list(news_entries[:3])
    top_keywords = ["Edge AI", "Industrial PC", "競品動態"]
    heat_ranking = []

    for index, entry in enumerate(top_entries, start=1):
        heat_ranking.append(
            {
                "impact_level": IMPACT_LEVELS[min(index - 1, len(IMPACT_LEVELS) - 1)],
                "company_or_tech": entry_company_text(entry),
                "core_issue": normalize_title(normalize_text(entry.get("title"), "本週重點新聞")),
            }
        )

    while len(heat_ranking) < 3:
        heat_ranking.append(
            {
                "impact_level": IMPACT_LEVELS[min(len(heat_ranking), len(IMPACT_LEVELS) - 1)],
                "company_or_tech": "待追蹤",
                "core_issue": "目前可用新聞不足，建議保留人工補充欄位。",
            }
        )

    section_items = []
    for entry in news_entries[: min(len(news_entries), 4)]:
        section_items.append(
            {
                "company_or_tech": entry_company_text(entry),
                "impact_level": "★★★☆☆",
                "headline": normalize_title(normalize_text(entry.get("title"), "（無標題）")),
                "key_summary": normalize_summary_text(entry.get("summary"), "此則新聞可作為本週競品動態觀察樣本。"),
                "why_it_matters": "建議結合產品布局、合作對象與產業位置評估其後續影響。",
                "suggested_watch": "觀察後續是否延伸到新產品、專案落地或財務表現。",
            }
        )

    if not section_items:
        section_items.append(
            {
                "company_or_tech": "本週無重大新聞",
                "impact_level": "★☆☆☆☆",
                "headline": "本週沒有足夠新聞可供分析",
                "key_summary": "目前未取得足夠的競品新聞，建議擴大關鍵詞或延長觀察區間。",
                "why_it_matters": "資料量不足會降低摘要判斷力，容易讓結論失真。",
                "suggested_watch": "下週可調整搜尋範圍或補充其他來源。",
            }
        )

    return {
        "email_subject": f"IPC / Edge AI 每週新聞摘要 | {week_range}",
        "week_range": week_range,
        "top_keywords": top_keywords,
        "major_shift": reason,
        "executive_summary": [
            f"本次整理涵蓋近 {news_days} 天的 IPC / Edge AI 競品新聞。",
            f"共納入 {len(news_entries)} 則新聞，建議優先查看競爭熱度排行與重點分組。",
            "若模型輸出異常，建議保留人工覆核與主題微調流程。",
        ],
        "heat_ranking": heat_ranking,
        "news_sections": [{"title": "[新聞概覽]", "items": section_items}],
        "conclusion_actions": [
            "先用本週的熱度排行判斷哪些競品值得進一步追蹤。",
            "若要提升判讀品質，建議逐步增加摘要與日期等上下文欄位。",
        ],
        "watchlist": [
            "觀察高熱度新聞是否在下週出現第二波延伸消息。",
            "持續追蹤 AI 伺服器、開放自動化與組織整合相關主題。",
        ],
    }


def normalize_string_list(values: Any, fallback: list[str], min_items: int, max_items: int | None = None) -> list[str]:
    normalized: list[str] = []
    if isinstance(values, list):
        for value in values:
            text = normalize_text(value)
            if text:
                normalized.append(text)

    if not normalized:
        normalized = list(fallback)

    if max_items is not None:
        normalized = normalized[:max_items]

    while len(normalized) < min_items:
        normalized.append(fallback[min(len(normalized), len(fallback) - 1)])

    return normalized


def normalize_heat_ranking(values: Any, fallback: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []

    if isinstance(values, list):
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                continue
            normalized.append(
                {
                    "impact_level": normalize_impact_level(value.get("impact_level"), fallback[min(index, len(fallback) - 1)]["impact_level"]),
                    "company_or_tech": normalize_text(value.get("company_or_tech"), fallback[min(index, len(fallback) - 1)]["company_or_tech"]),
                    "core_issue": normalize_text(value.get("core_issue"), fallback[min(index, len(fallback) - 1)]["core_issue"]),
                }
            )
            if len(normalized) >= 5:
                break

    if len(normalized) < 3:
        normalized.extend(fallback[len(normalized) : 3])

    return normalized[:5]


def normalize_news_sections(values: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_sections: list[dict[str, Any]] = []

    if isinstance(values, list):
        for section_index, value in enumerate(values):
            if not isinstance(value, dict):
                continue

            section_title = normalize_text(value.get("title"), fallback[min(section_index, len(fallback) - 1)]["title"])
            items: list[dict[str, str]] = []
            raw_items = value.get("items")

            if isinstance(raw_items, list):
                for item_index, raw_item in enumerate(raw_items):
                    if not isinstance(raw_item, dict):
                        continue

                    fallback_item = fallback[min(section_index, len(fallback) - 1)]["items"][0]
                    items.append(
                        {
                            "company_or_tech": normalize_text(raw_item.get("company_or_tech"), fallback_item["company_or_tech"]),
                            "impact_level": normalize_impact_level(raw_item.get("impact_level"), fallback_item["impact_level"]),
                            "headline": normalize_text(raw_item.get("headline"), fallback_item["headline"]),
                            "key_summary": normalize_text(raw_item.get("key_summary"), fallback_item["key_summary"]),
                            "why_it_matters": normalize_text(raw_item.get("why_it_matters"), fallback_item["why_it_matters"]),
                            "suggested_watch": normalize_text(raw_item.get("suggested_watch"), fallback_item["suggested_watch"]),
                        }
                    )
                    if len(items) >= 4:
                        break

            if not items:
                items = [dict(fallback[min(section_index, len(fallback) - 1)]["items"][0])]

            normalized_sections.append({"title": section_title, "items": items})
            if len(normalized_sections) >= 4:
                break

    if not normalized_sections:
        normalized_sections = fallback[:1]

    return normalized_sections


def normalize_analysis_payload(payload: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return fallback

    normalized = {
        "email_subject": normalize_text(payload.get("email_subject"), fallback["email_subject"]),
        "week_range": normalize_text(payload.get("week_range"), fallback["week_range"]),
        "top_keywords": normalize_string_list(payload.get("top_keywords"), fallback["top_keywords"], min_items=3, max_items=3),
        "major_shift": normalize_text(payload.get("major_shift"), fallback["major_shift"]),
        "executive_summary": normalize_string_list(
            payload.get("executive_summary"),
            fallback["executive_summary"],
            min_items=2,
            max_items=3,
        ),
        "heat_ranking": normalize_heat_ranking(payload.get("heat_ranking"), fallback["heat_ranking"]),
        "news_sections": normalize_news_sections(payload.get("news_sections"), fallback["news_sections"]),
        "conclusion_actions": normalize_string_list(
            payload.get("conclusion_actions"),
            fallback["conclusion_actions"],
            min_items=2,
            max_items=4,
        ),
        "watchlist": normalize_string_list(payload.get("watchlist"), fallback["watchlist"], min_items=2, max_items=4),
    }
    return normalized


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


def fetch_entries_for_query(query: str, company: str, phase: str, timeout: int = RSS_TIMEOUT_SECONDS):
    feedparser = import_feedparser()
    rss_url = build_rss_url(query)
    print(f"🌐 [{company}] {phase} RSS start | timeout={timeout}s")
    print(f"   URL: {rss_url}")

    request = Request(
        rss_url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )

    started_at = time.monotonic()
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()

    feed = feedparser.parse(payload)
    elapsed = time.monotonic() - started_at
    print(f"✅ [{company}] {phase} RSS done | entries={len(feed.entries)} | {elapsed:.1f}s")
    return list(feed.entries)


def fetch_entries_with_logging(query: str, company: str, phase: str, timeout: int = RSS_TIMEOUT_SECONDS):
    try:
        return fetch_entries_for_query(query, company=company, phase=phase, timeout=timeout)
    except (TimeoutError, HTTPError, URLError, OSError) as exc:
        print(f"⚠️ [{company}] {phase} RSS failed | {exc.__class__.__name__}: {exc}")
        return []
    except Exception as exc:
        print(f"⚠️ [{company}] {phase} unexpected RSS error | {exc.__class__.__name__}: {exc}")
        return []


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
        print(f"\n🔎 現在處理公司: {company}")
        strict_query = build_company_query(company, target_keyword, news_days, broadened=False)
        strict_entries = filter_recent_entries(
            fetch_entries_with_logging(strict_query, company=company, phase="strict"),
            news_days,
            news_limit=None,
        )
        strict_entries = tag_entries_with_company(strict_entries[:per_company_news_limit], company)

        if strict_entries:
            print(f"✅ [{company}] strict 命中 {len(strict_entries)} 則，略過 fallback")
            strict_hit_companies += 1
            all_entries.extend(strict_entries)
            continue

        if company.casefold() in AMBIGUOUS_COMPETITOR_NAMES:
            print(f"ℹ️ [{company}] 名稱較模糊，strict 無結果後不做 fallback")
            continue

        fallback_query = build_company_query(company, target_keyword, news_days, broadened=True)
        fallback_entries = filter_recent_entries(
            fetch_entries_with_logging(fallback_query, company=company, phase="fallback"),
            news_days,
            news_limit=None,
        )
        fallback_entries = [entry for entry in fallback_entries if entry_matches_relevance(entry, local_terms, company)]
        fallback_entries = tag_entries_with_company(fallback_entries[:per_company_news_limit], company)

        if fallback_entries:
            print(f"✅ [{company}] fallback 命中 {len(fallback_entries)} 則")
            fallback_hit_companies += 1
            all_entries.extend(fallback_entries)
            continue

        print(f"ℹ️ [{company}] strict / fallback 都沒有可用結果，已跳過")

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


def build_analysis_prompt(competitors: tuple[str, ...], news_entries, news_days: int, generated_at: datetime) -> str:
    week_range = build_week_range_label(generated_at, news_days)
    news_lines = [build_news_digest_line(entry, index) for index, entry in enumerate(news_entries, start=1)]

    return dedent(
        f"""
        你要根據提供的新聞資料，為高階主管撰寫一封「IPC / Edge AI 競品每週新聞 email」。
        觀察對象：{', '.join(competitors)}
        日期區間：{week_range}
        新聞視窗：近 {news_days} 天

        請遵守以下原則：
        1. 僅根據提供的新聞內容進行推論，不要虛構不存在的公司動作或數字。
        2. 內容要結論先行，避免流水帳，不要只是重寫新聞標題。
        3. `top_keywords` 請輸出 3 個短詞，不要是完整句子。
        4. `major_shift` 必須是一句話，點出本週最大的變化。
        5. `executive_summary` 請輸出 2 到 3 點，適合主管 1 分鐘內讀完。
        6. `heat_ranking` 請以市場影響度排序。
        7. `news_sections` 請優先使用這些區塊名稱做分組：[市場合作]、[組織 / 財報動態]、[技術亮點]、[產品 / 解決方案進展]。
        8. 每則新聞項目都要回答：發生什麼、為何重要、建議觀察什麼。

        新聞清單：
        {chr(10).join(news_lines)}
        """
    ).strip()


def analyze_with_gemini(
    api_key: str,
    model_name: str,
    system_instruction: str,
    competitors: tuple[str, ...],
    news_entries,
    news_days: int,
    generated_at: datetime,
) -> dict[str, Any]:
    if not news_entries:
        return build_fallback_analysis_payload(news_entries, news_days, generated_at, "本週無足夠的 IPC / Edge AI 競品新聞，未送出 Gemini 分析。")

    if not api_key:
        return build_fallback_analysis_payload(news_entries, news_days, generated_at, "⚠️ GEMINI_API_KEY 未設定，已略過 Gemini 分析。")

    genai, _ = import_gemini()
    client = genai.Client(api_key=api_key)

    fallback = build_fallback_analysis_payload(news_entries, news_days, generated_at, "模型暫時無法提供完整摘要，已回退為系統預設週報結構。")
    prompt = build_analysis_prompt(competitors, news_entries, news_days, generated_at)
    generate_content_kwargs = {"model": model_name, "contents": prompt}
    config: dict[str, Any] = {
        "response_mime_type": "application/json",
        "response_json_schema": build_weekly_email_schema(),
    }
    if system_instruction:
        config["system_instruction"] = system_instruction
    generate_content_kwargs["config"] = config

    response = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 2):
        try:
            print(f"🤖 Gemini 分析中... attempt {attempt}/{GEMINI_MAX_RETRIES + 1}")
            response = client.models.generate_content(**generate_content_kwargs)
            break
        except Exception as exc:
            error_text = str(exc)
            is_retryable = any(marker in error_text.upper() for marker in GEMINI_RETRYABLE_MARKERS)
            if not is_retryable or attempt > GEMINI_MAX_RETRIES:
                print(f"⚠️ Gemini 分析失敗，不再重試 | {exc.__class__.__name__}: {exc}")
                return build_fallback_analysis_payload(news_entries, news_days, generated_at, "Gemini 服務暫時不可用，已改用預設週報骨架。")

            backoff_seconds = min(2 ** (attempt - 1), 8)
            print(
                f"⚠️ Gemini 暫時不可用，將在 {backoff_seconds}s 後重試 "
                f"({attempt}/{GEMINI_MAX_RETRIES}) | {exc.__class__.__name__}: {exc}"
            )
            time.sleep(backoff_seconds)

    if response is None:
        return build_fallback_analysis_payload(news_entries, news_days, generated_at, "Gemini 服務暫時不可用，已改用預設週報骨架。")

    text = getattr(response, "text", "").strip()
    if not text:
        return build_fallback_analysis_payload(news_entries, news_days, generated_at, "Gemini 沒有回傳內容，已改用預設週報骨架。")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return build_fallback_analysis_payload(news_entries, news_days, generated_at, "Gemini 回傳格式不穩定，已改用預設週報骨架。")

    return normalize_analysis_payload(payload, fallback)


def render_keyword_tags(keywords: list[str]) -> str:
    tags = "".join(f'<span class="keyword-chip">{html.escape(keyword)}</span>' for keyword in keywords)
    return f'<div class="keyword-list">{tags}</div>' if tags else ""


def render_bullet_list(items: list[str], class_name: str) -> str:
    bullets = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f'<ul class="{class_name}">{bullets}</ul>' if bullets else ""


def render_heat_ranking_rows(heat_ranking: list[dict[str, str]]) -> str:
    rows = []
    for item in heat_ranking:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['impact_level'])}</td>"
            f"<td>{html.escape(item['company_or_tech'])}</td>"
            f"<td>{html.escape(item['core_issue'])}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_news_sections(analysis: dict[str, Any]) -> str:
    sections_html: list[str] = []

    for section in analysis["news_sections"]:
        items_html: list[str] = []
        for item in section["items"]:
            items_html.append(
                "<article class=\"news-card\">"
                f"<div class=\"news-meta\"><span class=\"news-company\">{html.escape(item['company_or_tech'])}</span>"
                f"<span class=\"news-impact\">{html.escape(item['impact_level'])}</span></div>"
                f"<h4>{html.escape(item['headline'])}</h4>"
                f"<p><strong>關鍵摘要：</strong>{html.escape(item['key_summary'])}</p>"
                f"<p><strong>為何重要：</strong>{html.escape(item['why_it_matters'])}</p>"
                f"<p><strong>建議觀察：</strong>{html.escape(item['suggested_watch'])}</p>"
                "</article>"
            )

        sections_html.append(
            "<section class=\"analysis-section\">"
            f"<h3>{html.escape(section['title'])}</h3>"
            f"<div class=\"news-card-list\">{''.join(items_html)}</div>"
            "</section>"
        )

    return "".join(sections_html)


def render_analysis_block(analysis: dict[str, Any]) -> str:
    return (
        '<div class="analysis-email">'
        '<section class="analysis-hero">'
        '<p class="analysis-kicker">核心要聞：一分鐘洞察</p>'
        f'<p class="analysis-major"><strong>重大變動提醒：</strong>{html.escape(analysis["major_shift"])}</p>'
        f'{render_keyword_tags(analysis["top_keywords"])}'
        f'{render_bullet_list(analysis["executive_summary"], "brief-list")}'
        '</section>'
        '<section class="analysis-section">'
        '<h3>競爭熱度排行</h3>'
        '<table class="heat-table" role="presentation">'
        '<thead><tr><th>影響等級</th><th>受訪企業 / 技術</th><th>核心議題</th></tr></thead>'
        f'<tbody>{render_heat_ranking_rows(analysis["heat_ranking"])}</tbody>'
        '</table>'
        '</section>'
        f'{render_news_sections(analysis)}'
        '<section class="analysis-section">'
        '<h3>結論與建議</h3>'
        f'{render_bullet_list(analysis["conclusion_actions"], "signal-list")}'
        '</section>'
        '<section class="analysis-section">'
        '<h3>下週追蹤</h3>'
        f'{render_bullet_list(analysis["watchlist"], "signal-list")}'
        '</section>'
        '</div>'
    )


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
    analysis: dict[str, Any],
    output_path: Path,
) -> dict[str, str]:
    week_range = normalize_text(analysis.get("week_range"), build_week_range_label(generated_at, settings.news_days))
    return {
        "report_title": html.escape(settings.report_title),
        "generated_at": html.escape(generated_at.strftime("%Y-%m-%d %H:%M:%S")),
        "model_name": html.escape(settings.model_name),
        "report_path": html.escape(output_path.name),
        "email_subject": html.escape(normalize_text(analysis.get("email_subject"), settings.report_title)),
        "week_range": html.escape(week_range),
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


def build_metadata_payload(
    settings: ReportSettings,
    generated_at: datetime,
    entries,
    analysis: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    return {
        "report_title": settings.report_title,
        "email_subject": normalize_text(analysis.get("email_subject"), settings.report_title),
        "week_range": normalize_text(analysis.get("week_range"), build_week_range_label(generated_at, settings.news_days)),
        "generated_at": generated_at.astimezone().isoformat(timespec="seconds"),
        "model_name": settings.model_name,
        "target_keyword": settings.target_keyword,
        "news_days": settings.news_days,
        "per_company_news_limit": settings.per_company_news_limit,
        "news_count": len(entries),
        "competitors": list(settings.competitors),
        "report_path": output_path.name,
        "major_shift": normalize_text(analysis.get("major_shift")),
        "top_keywords": list(analysis.get("top_keywords") or []),
        "executive_summary": list(analysis.get("executive_summary") or []),
        "conclusion_actions": list(analysis.get("conclusion_actions") or []),
        "watchlist": list(analysis.get("watchlist") or []),
    }


def write_metadata(metadata_output_path: Path, payload: dict[str, Any]) -> None:
    metadata_output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def print_console_summary(settings: ReportSettings, generated_at: datetime, entries, analysis: dict[str, Any], output_path: Path) -> None:
    print("\n" + "=" * 50)
    print(f"        🏆 {settings.report_title} 🏆")
    print("=" * 50)
    print(f"📅 生成時間: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🗓️ 搜尋區間: 近 {settings.news_days} 天")
    print(f"🏢 每家公司上限: {settings.per_company_news_limit} 則")
    print(f"📰 Email Subject: {analysis.get('email_subject', settings.report_title)}")
    print(f"🔥 重大變動: {analysis.get('major_shift', '無')}")
    for item in analysis.get("executive_summary", []):
        print(f"- {item}")
    print("=" * 50)
    print(f"\n📄 HTML 報告已寫入: {output_path}")

    if entries:
        print("\n報告生成完畢。連結參考：")
        for entry in entries:
            print(f"🔗 [{entry_company_text(entry)}] {entry.get('title') or '（無標題）'} -> {entry.get('link') or ''}")
    else:
        print("\n今日無相關重要新聞。")


def generate_report(settings: ReportSettings, output_path: Path, metadata_output_path: Path | None = None) -> Path:
    entries, query, rss_url = fetch_competitor_news(
        settings.competitors,
        settings.target_keyword,
        settings.news_limit,
        settings.news_days,
        settings.per_company_news_limit,
    )
    generated_at = datetime.now().astimezone()

    analysis = analyze_with_gemini(
        settings.api_key,
        settings.model_name,
        settings.system_instruction,
        settings.competitors,
        entries,
        settings.news_days,
        generated_at,
    )

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
    if metadata_output_path is not None:
        metadata_payload = build_metadata_payload(settings, generated_at, entries, analysis, output_path)
        write_metadata(metadata_output_path, metadata_payload)
    print_console_summary(settings, generated_at, entries, analysis, output_path)
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an IPC / Edge AI weekly email digest as HTML.")
    parser.add_argument("--output", help="HTML report output path")
    parser.add_argument("--metadata-output", help="Optional JSON metadata output path")
    parser.add_argument("--template", help="Override the HTML report template path")
    parser.add_argument("--news-days", type=int, default=None, help="News lookback window in days")
    parser.add_argument("--per-company-limit", type=int, default=None, help="Maximum news items kept for each company")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file()
    settings = load_settings(args.template, args.news_days, args.per_company_limit)
    output_path = build_output_path(args.output)
    metadata_output_path = Path(args.metadata_output).expanduser() if args.metadata_output else None
    generate_report(settings, output_path, metadata_output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
