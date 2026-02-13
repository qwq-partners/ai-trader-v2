"""
네이버 뉴스 기반 종목 검증기

진입 전 해당 종목의 최근 뉴스를 확인하여:
- 재료 없는 급등 필터링 (뉴스 없음 → confidence 하락)
- 부정적 뉴스 감지 (유상증자, 감사의견 등 → confidence 하락)
- 긍정적 뉴스 확인 (수주, 호실적 등 → confidence 상승)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

import aiohttp
from loguru import logger


@dataclass
class NewsCheckResult:
    """뉴스 검증 결과"""
    has_news: bool = False
    positive_count: int = 0
    negative_count: int = 0
    sentiment_score: float = 0.0  # -1.0 ~ +1.0
    confidence_adjustment: float = 0.0  # 진입 confidence 조정값


# HTML 태그 제거 패턴
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;")

POSITIVE_KEYWORDS = [
    "급등", "수주", "호실적", "자사주", "외국인매수", "신고가",
    "흑자전환", "실적개선", "매출증가", "영업이익", "순이익",
    "대규모수주", "계약체결", "특허", "FDA", "승인",
    "배당", "자사주매입", "목표가상향", "투자의견상향",
]

NEGATIVE_KEYWORDS = [
    "급락", "유상증자", "전환사채", "감사의견", "부도", "상장폐지",
    "횡령", "배임", "관리종목", "투자주의", "투자경고",
    "적자전환", "적자확대", "실적악화", "매출감소",
    "감자", "워크아웃", "법정관리", "회생절차",
    "목표가하향", "투자의견하향", "공매도",
]


class NewsVerifier:
    """네이버 뉴스 API를 활용한 종목 뉴스 검증기"""

    NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"

    def __init__(self):
        self._client_id = os.getenv("NAVER_CLIENT_ID", "")
        self._client_secret = os.getenv("NAVER_CLIENT_SECRET", "")
        self._enabled = bool(self._client_id and self._client_secret)
        # 캐시: symbol → (result, timestamp)
        self._cache: Dict[str, Tuple[NewsCheckResult, datetime]] = {}
        self._cache_ttl = timedelta(minutes=10)

        if not self._enabled:
            logger.warning("[뉴스검증] NAVER_CLIENT_ID/SECRET 미설정 → 뉴스 검증 비활성화")

    def _clean_cache(self):
        """만료된 캐시 항목 정리"""
        now = datetime.now()
        expired = [k for k, (_, ts) in self._cache.items() if now - ts > self._cache_ttl]
        for k in expired:
            del self._cache[k]

    async def check_news(self, symbol: str, stock_name: str) -> NewsCheckResult:
        """종목 뉴스 확인 및 감성 분석"""
        if not self._enabled:
            return NewsCheckResult()

        # 캐시 확인
        now = datetime.now()
        if symbol in self._cache:
            cached_result, cached_time = self._cache[symbol]
            if now - cached_time < self._cache_ttl:
                return cached_result

        # 주기적 캐시 정리
        if len(self._cache) > 200:
            self._clean_cache()

        result = await self._fetch_and_analyze(stock_name)
        self._cache[symbol] = (result, now)
        return result

    async def _fetch_and_analyze(self, stock_name: str) -> NewsCheckResult:
        """네이버 뉴스 API 호출 및 분석"""
        headers = {
            "X-Naver-Client-Id": self._client_id,
            "X-Naver-Client-Secret": self._client_secret,
        }
        params = {
            "query": f"{stock_name} 주가",
            "display": 10,
            "sort": "date",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.NAVER_NEWS_URL,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"[뉴스검증] API 응답 {resp.status}: {stock_name}")
                        return NewsCheckResult()

                    data = await resp.json()
        except Exception as e:
            logger.debug(f"[뉴스검증] API 오류 ({stock_name}): {e}")
            return NewsCheckResult()

        items = data.get("items", [])
        if not items:
            # 뉴스 없음 → 재료 없는 급등 가능성
            return NewsCheckResult(
                has_news=False,
                confidence_adjustment=-0.20,
            )

        # 키워드 매칭
        positive_count = 0
        negative_count = 0

        for item in items:
            title = _HTML_TAG_RE.sub("", item.get("title", ""))
            title = _HTML_ENTITY_RE.sub("", title)
            description = _HTML_TAG_RE.sub("", item.get("description", ""))
            description = _HTML_ENTITY_RE.sub("", description)
            text = f"{title} {description}"

            for kw in POSITIVE_KEYWORDS:
                if kw in text:
                    positive_count += 1
                    break  # 기사당 1회만 카운트

            for kw in NEGATIVE_KEYWORDS:
                if kw in text:
                    negative_count += 1
                    break

        # 감성 점수 계산
        total = positive_count + negative_count
        if total > 0:
            sentiment_score = (positive_count - negative_count) / total
        else:
            sentiment_score = 0.0

        # confidence 조정값 결정
        if negative_count > positive_count:
            confidence_adj = -0.15
        elif positive_count > 0 and negative_count == 0:
            if positive_count >= 3:
                confidence_adj = 0.15  # 압도적 긍정
            else:
                confidence_adj = 0.10  # 긍정
        else:
            confidence_adj = 0.0  # 혼재 또는 중립

        return NewsCheckResult(
            has_news=True,
            positive_count=positive_count,
            negative_count=negative_count,
            sentiment_score=sentiment_score,
            confidence_adjustment=confidence_adj,
        )
