"""
AI Trading Bot v2 - 뉴스/테마 저장소

PostgreSQL을 사용하여 뉴스 기사와 테마 히스토리를 저장합니다.

테이블:
- news_articles: 수집된 뉴스 기사
- theme_history: 테마 탐지 히스토리
- theme_stocks: 테마-종목 매핑
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
import asyncpg
from loguru import logger


@dataclass
class NewsArticle:
    """뉴스 기사"""
    title: str
    content: str = ""
    url: str = ""
    source: str = ""
    published_at: Optional[datetime] = None
    collected_at: Optional[datetime] = None
    keywords: List[str] = None
    sentiment_score: Optional[float] = None
    id: Optional[int] = None

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []
        if self.collected_at is None:
            self.collected_at = datetime.now()


@dataclass
class ThemeRecord:
    """테마 기록"""
    theme_name: str
    score: float
    news_count: int = 0
    keywords: List[str] = None
    detected_at: Optional[datetime] = None
    id: Optional[int] = None

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []
        if self.detected_at is None:
            self.detected_at = datetime.now()


class NewsStorage:
    """
    뉴스/테마 저장소

    PostgreSQL을 사용하여 뉴스와 테마 데이터를 영구 저장합니다.
    """

    def __init__(self, database_url: Optional[str] = None):
        self._database_url = database_url or os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/ai_db"
        )
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> bool:
        """DB 연결"""
        try:
            # 기존 풀이 있으면 먼저 닫기 (커넥션 누수 방지)
            if self._pool:
                try:
                    await self._pool.close()
                except Exception as e:
                    logger.debug(f"기존 DB 풀 닫기 실패 (무시): {e}")
                self._pool = None

            self._pool = await asyncpg.create_pool(
                self._database_url,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )
            logger.info("[NewsStorage] PostgreSQL 연결 완료")
            return True
        except Exception as e:
            logger.error(f"[NewsStorage] DB 연결 실패: {e}")
            return False

    async def disconnect(self):
        """DB 연결 해제"""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("[NewsStorage] PostgreSQL 연결 해제")

    async def _ensure_connected(self):
        """연결 보장"""
        if not self._pool:
            await self.connect()

    # ============================================================
    # 뉴스 저장/조회
    # ============================================================

    async def save_news(self, article: NewsArticle) -> Optional[int]:
        """뉴스 기사 저장"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                result = await conn.fetchval("""
                    INSERT INTO news_articles
                        (title, content, url, source, published_at, collected_at, keywords, sentiment_score)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (title, published_at) DO UPDATE SET
                        collected_at = EXCLUDED.collected_at
                    RETURNING id
                """,
                    article.title,
                    article.content,
                    article.url,
                    article.source,
                    article.published_at,
                    article.collected_at or datetime.now(),
                    article.keywords,
                    article.sentiment_score,
                )
                return result
        except Exception as e:
            logger.error(f"[NewsStorage] 뉴스 저장 실패: {e}")
            return None

    async def save_news_batch(self, articles: List[NewsArticle]) -> int:
        """뉴스 기사 배치 저장"""
        await self._ensure_connected()

        saved_count = 0
        try:
            async with self._pool.acquire() as conn:
                for article in articles:
                    try:
                        await conn.execute("""
                            INSERT INTO news_articles
                                (title, content, url, source, published_at, collected_at, keywords, sentiment_score)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT (title, published_at) DO NOTHING
                        """,
                            article.title,
                            article.content,
                            article.url,
                            article.source,
                            article.published_at,
                            article.collected_at or datetime.now(),
                            article.keywords,
                            article.sentiment_score,
                        )
                        saved_count += 1
                    except Exception as e:
                        logger.debug(f"뉴스 저장 건너뜀: {e}")

            logger.info(f"[NewsStorage] 뉴스 {saved_count}/{len(articles)}건 저장")
            return saved_count

        except Exception as e:
            logger.error(f"[NewsStorage] 뉴스 배치 저장 실패: {e}")
            return saved_count

    async def get_recent_news(
        self,
        hours: int = 24,
        limit: int = 100
    ) -> List[NewsArticle]:
        """최근 뉴스 조회"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, title, content, url, source, published_at, collected_at, keywords, sentiment_score
                    FROM news_articles
                    WHERE collected_at > $1
                    ORDER BY collected_at DESC
                    LIMIT $2
                """, datetime.now() - timedelta(hours=hours), limit)

                return [
                    NewsArticle(
                        id=row['id'],
                        title=row['title'],
                        content=row['content'] or "",
                        url=row['url'] or "",
                        source=row['source'] or "",
                        published_at=row['published_at'],
                        collected_at=row['collected_at'],
                        keywords=row['keywords'] or [],
                        sentiment_score=row['sentiment_score'],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[NewsStorage] 뉴스 조회 실패: {e}")
            return []

    async def search_news(
        self,
        keyword: str,
        days: int = 7,
        limit: int = 50
    ) -> List[NewsArticle]:
        """키워드로 뉴스 검색"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, title, content, url, source, published_at, collected_at, keywords, sentiment_score
                    FROM news_articles
                    WHERE (title ILIKE $1 OR content ILIKE $1)
                      AND collected_at > $2
                    ORDER BY collected_at DESC
                    LIMIT $3
                """, f"%{keyword}%", datetime.now() - timedelta(days=days), limit)

                return [
                    NewsArticle(
                        id=row['id'],
                        title=row['title'],
                        content=row['content'] or "",
                        url=row['url'] or "",
                        source=row['source'] or "",
                        published_at=row['published_at'],
                        collected_at=row['collected_at'],
                        keywords=row['keywords'] or [],
                        sentiment_score=row['sentiment_score'],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[NewsStorage] 뉴스 검색 실패: {e}")
            return []

    async def get_news_count(self, hours: int = 24) -> int:
        """기간 내 뉴스 수 조회"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                count = await conn.fetchval("""
                    SELECT COUNT(*) FROM news_articles
                    WHERE collected_at > $1
                """, datetime.now() - timedelta(hours=hours))
                return count or 0
        except Exception as e:
            logger.error(f"[NewsStorage] 뉴스 수 조회 실패: {e}")
            return 0

    # ============================================================
    # 테마 히스토리 저장/조회
    # ============================================================

    async def save_theme(self, theme: ThemeRecord) -> Optional[int]:
        """테마 기록 저장"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                result = await conn.fetchval("""
                    INSERT INTO theme_history (theme_name, score, news_count, keywords, detected_at)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                """,
                    theme.theme_name,
                    theme.score,
                    theme.news_count,
                    theme.keywords,
                    theme.detected_at or datetime.now(),
                )
                return result
        except Exception as e:
            logger.error(f"[NewsStorage] 테마 저장 실패: {e}")
            return None

    async def save_themes_batch(self, themes: List[ThemeRecord]) -> int:
        """테마 배치 저장"""
        await self._ensure_connected()

        saved_count = 0
        try:
            async with self._pool.acquire() as conn:
                for theme in themes:
                    try:
                        await conn.execute("""
                            INSERT INTO theme_history (theme_name, score, news_count, keywords, detected_at)
                            VALUES ($1, $2, $3, $4, $5)
                        """,
                            theme.theme_name,
                            theme.score,
                            theme.news_count,
                            theme.keywords,
                            theme.detected_at or datetime.now(),
                        )
                        saved_count += 1
                    except Exception as e:
                        logger.debug(f"테마 저장 건너뜀: {e}")

            logger.debug(f"[NewsStorage] 테마 {saved_count}개 저장")
            return saved_count

        except Exception as e:
            logger.error(f"[NewsStorage] 테마 배치 저장 실패: {e}")
            return saved_count

    async def get_theme_history(
        self,
        theme_name: str,
        days: int = 7,
        limit: int = 100
    ) -> List[ThemeRecord]:
        """테마 히스토리 조회"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, theme_name, score, news_count, keywords, detected_at
                    FROM theme_history
                    WHERE theme_name = $1 AND detected_at > $2
                    ORDER BY detected_at DESC
                    LIMIT $3
                """, theme_name, datetime.now() - timedelta(days=days), limit)

                return [
                    ThemeRecord(
                        id=row['id'],
                        theme_name=row['theme_name'],
                        score=row['score'],
                        news_count=row['news_count'],
                        keywords=row['keywords'] or [],
                        detected_at=row['detected_at'],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[NewsStorage] 테마 히스토리 조회 실패: {e}")
            return []

    async def get_hot_themes(
        self,
        hours: int = 24,
        min_score: float = 70,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """핫 테마 조회 (최근 기간 내 평균 점수 기준)"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        theme_name,
                        AVG(score) as avg_score,
                        MAX(score) as max_score,
                        SUM(news_count) as total_news,
                        COUNT(*) as detection_count,
                        MAX(detected_at) as last_detected
                    FROM theme_history
                    WHERE detected_at > $1
                    GROUP BY theme_name
                    HAVING AVG(score) >= $2
                    ORDER BY avg_score DESC
                    LIMIT $3
                """, datetime.now() - timedelta(hours=hours), min_score, limit)

                return [
                    {
                        "theme_name": row['theme_name'],
                        "avg_score": float(row['avg_score']),
                        "max_score": float(row['max_score']),
                        "total_news": row['total_news'],
                        "detection_count": row['detection_count'],
                        "last_detected": row['last_detected'],
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[NewsStorage] 핫 테마 조회 실패: {e}")
            return []

    async def get_theme_trend(
        self,
        theme_name: str,
        days: int = 7
    ) -> List[Dict[str, Any]]:
        """테마 트렌드 조회 (일별 평균 점수)"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        DATE(detected_at) as date,
                        AVG(score) as avg_score,
                        MAX(score) as max_score,
                        SUM(news_count) as total_news
                    FROM theme_history
                    WHERE theme_name = $1 AND detected_at > $2
                    GROUP BY DATE(detected_at)
                    ORDER BY date DESC
                """, theme_name, datetime.now() - timedelta(days=days))

                return [
                    {
                        "date": row['date'],
                        "avg_score": float(row['avg_score']),
                        "max_score": float(row['max_score']),
                        "total_news": row['total_news'],
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[NewsStorage] 테마 트렌드 조회 실패: {e}")
            return []

    # ============================================================
    # 테마-종목 매핑
    # ============================================================

    async def save_theme_stock(self, theme_name: str, symbol: str) -> bool:
        """테마-종목 매핑 저장"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO theme_stocks (theme_name, symbol)
                    VALUES ($1, $2)
                    ON CONFLICT (theme_name, symbol) DO NOTHING
                """, theme_name, symbol)
                return True
        except Exception as e:
            logger.error(f"[NewsStorage] 테마-종목 매핑 저장 실패: {e}")
            return False

    async def get_theme_stocks(self, theme_name: str) -> List[str]:
        """테마 관련 종목 조회"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT symbol FROM theme_stocks
                    WHERE theme_name = $1
                    ORDER BY added_at
                """, theme_name)

                return [row['symbol'] for row in rows]
        except Exception as e:
            logger.error(f"[NewsStorage] 테마 종목 조회 실패: {e}")
            return []

    async def get_stock_themes(self, symbol: str) -> List[str]:
        """종목의 테마 목록 조회"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT theme_name FROM theme_stocks
                    WHERE symbol = $1
                """, symbol)

                return [row['theme_name'] for row in rows]
        except Exception as e:
            logger.error(f"[NewsStorage] 종목 테마 조회 실패: {e}")
            return []

    # ============================================================
    # 통계
    # ============================================================

    async def get_stats(self) -> Dict[str, Any]:
        """저장소 통계"""
        await self._ensure_connected()

        try:
            async with self._pool.acquire() as conn:
                news_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM news_articles"
                )
                news_24h = await conn.fetchval(
                    "SELECT COUNT(*) FROM news_articles WHERE collected_at > $1",
                    datetime.now() - timedelta(hours=24)
                )
                theme_count = await conn.fetchval(
                    "SELECT COUNT(DISTINCT theme_name) FROM theme_history"
                )
                theme_24h = await conn.fetchval(
                    "SELECT COUNT(*) FROM theme_history WHERE detected_at > $1",
                    datetime.now() - timedelta(hours=24)
                )

                return {
                    "total_news": news_count or 0,
                    "news_24h": news_24h or 0,
                    "total_themes": theme_count or 0,
                    "theme_records_24h": theme_24h or 0,
                }
        except Exception as e:
            logger.error(f"[NewsStorage] 통계 조회 실패: {e}")
            return {}


# ============================================================
# 전역 인스턴스
# ============================================================

_news_storage: Optional[NewsStorage] = None


async def get_news_storage() -> NewsStorage:
    """전역 뉴스 저장소"""
    global _news_storage
    if _news_storage is None:
        _news_storage = NewsStorage()
        await _news_storage.connect()
    return _news_storage
