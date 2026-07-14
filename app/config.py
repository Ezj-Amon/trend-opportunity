from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    newsnow_base_url: str
    source_ids: tuple[str, ...]
    analysis_top_n: int
    overseas_analysis_top_n: int
    google_trends_geos: tuple[str, ...]
    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_user_agent: str
    reddit_subreddits: tuple[str, ...]
    schedule_minutes: int
    enable_scheduler: bool
    openai_api_key: str | None
    openai_base_url: str | None
    openai_model: str
    feishu_webhook_url: str | None
    feishu_secret: str | None
    public_base_url: str
    admin_token: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        sources = tuple(
            part.strip()
            for part in os.getenv(
                "TREND_SOURCES",
                "weibo,zhihu,baidu,douyin,toutiao,bilibili-hot-search,coolapk,tieba,"
                "hackernews,producthunt,github-trending-today",
            ).split(",")
            if part.strip()
        )
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "data/trends.db")),
            newsnow_base_url=os.getenv(
                "NEWSNOW_BASE_URL", "https://newsnow.busiyi.world"
            ).rstrip("/"),
            source_ids=sources,
            analysis_top_n=max(1, min(int(os.getenv("ANALYSIS_TOP_N", "5")), 20)),
            overseas_analysis_top_n=max(
                1, min(int(os.getenv("OVERSEAS_ANALYSIS_TOP_N", "5")), 20)
            ),
            google_trends_geos=tuple(
                part.strip().upper()
                for part in os.getenv("GOOGLE_TRENDS_GEOS", "US,GB,DE,JP").split(",")
                if part.strip()
            ),
            reddit_client_id=os.getenv("REDDIT_CLIENT_ID") or None,
            reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET") or None,
            reddit_user_agent=os.getenv(
                "REDDIT_USER_AGENT", "TrendOpportunityLab/0.2 by local-seller"
            ),
            reddit_subreddits=tuple(
                part.strip()
                for part in os.getenv(
                    "REDDIT_SUBREDDITS",
                    "BuyItForLife,gadgets,HomeImprovement,shutupandtakemymoney",
                ).split(",")
                if part.strip()
            ),
            schedule_minutes=max(10, int(os.getenv("SCHEDULE_MINUTES", "120"))),
            enable_scheduler=_bool_env("ENABLE_SCHEDULER"),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            feishu_webhook_url=os.getenv("FEISHU_WEBHOOK_URL") or None,
            feishu_secret=os.getenv("FEISHU_SECRET") or None,
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip(
                "/"
            ),
            admin_token=os.getenv("ADMIN_TOKEN") or None,
        )
