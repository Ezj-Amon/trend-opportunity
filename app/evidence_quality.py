from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import tldextract


MIN_SUMMARY_CHARACTERS = 60
MIN_FULL_ARTICLE_CHARACTERS = 200

_DOMAIN_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())

_SIGNAL_PAGE_RULES: tuple[tuple[str, str], ...] = (
    ("s.weibo.com", "/weibo"),
    ("www.baidu.com", "/s"),
    ("m.baidu.com", "/s"),
    ("www.douyin.com", "/hot"),
    ("www.toutiao.com", "/trending"),
    ("search.bilibili.com", "/"),
    ("tieba.baidu.com", "/hottopic/"),
    ("news.google.com", "/rss/"),
)

_BOILERPLATE_MARKERS = (
    "粤icp备15030494号",
    "违法和不良信息举报电话",
    "举报邮箱",
    "bilibili是国内知名的视频弹幕网站",
    "enable javascript",
    "javascript is required",
    "请启用javascript",
    "请开启 javascript",
    "sign in to continue",
    "登录后查看",
    "扫码登录",
)

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "oc",
    "ref",
    "ref_src",
    "source",
    "spm",
}


@dataclass(frozen=True, slots=True)
class ContentValidation:
    accepted: bool
    content_level: str
    relevance_score: float
    reasons: tuple[str, ...]


def registrable_domain(url: str) -> str:
    hostname = (urlparse(url).hostname or "").casefold().strip(".")
    if not hostname:
        return ""
    result = _DOMAIN_EXTRACTOR(hostname)
    return result.top_domain_under_public_suffix or hostname


def canonicalize_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    hostname = parsed.hostname.casefold()
    try:
        port = parsed.port
    except ValueError:
        return ""
    netloc = hostname
    if port and not (
        (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse(
        (
            parsed.scheme.casefold(),
            netloc,
            path,
            "",
            urlencode(query, doseq=True),
            "",
        )
    )


def is_signal_page_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path.casefold() or "/"
    return any(
        hostname == rule_host and path.startswith(rule_path)
        for rule_host, rule_path in _SIGNAL_PAGE_RULES
    )


def _search_units(value: str) -> set[str]:
    folded = value.casefold()
    latin_words = set(re.findall(r"[a-z0-9]{2,}", folded))
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", folded)
    cjk_bigrams = {
        run[index : index + 2]
        for run in cjk_runs
        for index in range(max(0, len(run) - 1))
    }
    return latin_words | cjk_bigrams


def content_relevance_score(expected_title: str, candidate_text: str) -> float:
    expected = _search_units(expected_title)
    if not expected:
        return 1.0
    candidate = _search_units(candidate_text)
    if not candidate:
        return 0.0
    return round(len(expected & candidate) / len(expected), 4)


def validate_extracted_content(
    *,
    url: str,
    expected_title: str,
    extracted_title: str,
    text: str,
) -> ContentValidation:
    cleaned = re.sub(r"\s+", " ", text).strip()
    reasons: list[str] = []
    if is_signal_page_url(url):
        reasons.append("source URL is a search, hot-list, or trend landing page")
    folded = cleaned.casefold()
    matched_markers = [marker for marker in _BOILERPLATE_MARKERS if marker in folded]
    if matched_markers and len(cleaned) < 800:
        reasons.append("content is dominated by site boilerplate")
    relevance = content_relevance_score(
        expected_title,
        f"{extracted_title} {cleaned[:2000]}",
    )
    if len(_search_units(expected_title)) >= 2 and relevance < 0.2:
        reasons.append("extracted content is not relevant to the expected title")
    if len(cleaned) < MIN_SUMMARY_CHARACTERS:
        reasons.append("content is too short")

    if len(cleaned) >= MIN_FULL_ARTICLE_CHARACTERS:
        content_level = "full_article"
    elif len(cleaned) >= MIN_SUMMARY_CHARACTERS:
        content_level = "article_summary"
    else:
        content_level = "title_only"
    return ContentValidation(
        accepted=not reasons,
        content_level=content_level,
        relevance_score=relevance,
        reasons=tuple(reasons),
    )


def content_fingerprint(text: str) -> str:
    normalized = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text.casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_near_duplicate(left: str, right: str, threshold: float = 0.88) -> bool:
    def shingles(value: str) -> set[str]:
        normalized = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.casefold())
        if len(normalized) < 80:
            return set()
        return {
            normalized[index : index + 5]
            for index in range(len(normalized) - 4)
        }

    left_shingles = shingles(left)
    right_shingles = shingles(right)
    if not left_shingles or not right_shingles:
        return False
    similarity = len(left_shingles & right_shingles) / len(
        left_shingles | right_shingles
    )
    return similarity >= threshold
