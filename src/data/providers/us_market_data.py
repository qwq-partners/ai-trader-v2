"""
AI Trading Bot v2 - US 시장 오버나이트 데이터 프로바이더

Yahoo Finance REST API를 통해 미국 시장 마감 데이터를 조회하여
한국 장 개장 전 테마 점수를 사전 부스트합니다.

핵심 원리:
  US 반도체 +3% → 한국 반도체 테마 +25점 부스트 → 장 시작과 동시에 관련 종목 포착

타임라인:
  06:00 KST - US 시장 마감 → Yahoo Finance 데이터 확정
  08:00 KST - 아침 레포트에 US 섹션 포함
  09:00 KST - ThemeDetector에서 캐시된 US 데이터로 부스트

API 호출 횟수: 하루 1회 (Yahoo Finance)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from loguru import logger


# ──────────────────────────────────────────────────────────────────
# 추적 대상 심볼 (~40개)
# ──────────────────────────────────────────────────────────────────
US_SYMBOLS: List[str] = [
    # 주요 지수
    "^GSPC", "^IXIC", "^SOX", "^DJI",
    # 섹터 ETF
    "XLK", "SMH", "SOXX", "XLV", "XLE", "ICLN", "URNM", "IBB", "XBI",
    "LIT", "TAN", "BOTZ", "ITA",
    # 반도체/AI
    "NVDA", "AMD", "TSM", "ASML", "INTC",
    # 원전/SMR
    "SMR", "OKLO", "CEG", "VST",
    # EV/배터리
    "TSLA", "RIVN", "LCID",
    # 빅테크
    "AAPL", "MSFT", "GOOG", "META", "AMZN",
    # 클린에너지
    "ENPH", "FSLR",
    # 방산
    "LMT", "RTX", "NOC", "GD",
]

# ──────────────────────────────────────────────────────────────────
# US → 한국 테마 매핑
# ──────────────────────────────────────────────────────────────────
# 각 한국 테마에 대응하는 US 심볼 그룹
US_KOREA_SECTOR_MAP: Dict[str, Dict] = {
    "AI/반도체": {
        "symbols": ["^SOX", "SMH", "SOXX", "NVDA", "AMD", "TSM", "ASML", "INTC"],
        # (임계값, 부스트점수) 리스트 — 내림차순
        "thresholds": [(5.0, 35), (3.0, 25), (2.0, 15), (1.0, 8)],
    },
    "원자력": {
        "symbols": ["URNM", "SMR", "OKLO", "CEG", "VST"],
        "thresholds": [(8.0, 40), (5.0, 30), (3.0, 20), (2.0, 10)],
    },
    "2차전지": {
        "symbols": ["TSLA", "LIT", "RIVN", "LCID"],
        "thresholds": [(5.0, 25), (3.0, 15), (2.0, 8)],
    },
    "바이오": {
        "symbols": ["XLV", "IBB", "XBI"],
        "thresholds": [(4.0, 25), (2.5, 15), (1.5, 8)],
    },
    "탄소중립": {
        "symbols": ["ICLN", "TAN", "ENPH", "FSLR"],
        "thresholds": [(5.0, 25), (3.0, 15), (2.0, 8)],
    },
    "로봇": {
        "symbols": ["BOTZ"],
        "thresholds": [(5.0, 25), (3.0, 15), (2.0, 8)],
    },
    "인터넷/플랫폼": {
        "symbols": ["META", "GOOG", "AMZN"],
        "thresholds": [(5.0, 20), (3.0, 15), (2.0, 8)],
    },
    "방산": {
        "symbols": ["ITA", "LMT", "RTX", "NOC", "GD"],
        "thresholds": [(5.0, 25), (3.0, 15), (1.5, 8)],
    },
}

# 지수 심볼 (종합 심리 판단용)
INDEX_SYMBOLS = ["^GSPC", "^IXIC", "^SOX", "^DJI"]
INDEX_NAMES = {
    "^GSPC": "S&P500",
    "^IXIC": "NASDAQ",
    "^SOX": "반도체(SOX)",
    "^DJI": "다우",
}


class USMarketData:
    """미국 시장 오버나이트 데이터 (Yahoo Finance REST)"""

    YAHOO_BASE_URL = "https://query2.finance.yahoo.com"
    CACHE_TTL = 86400  # 1일 (초)

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Optional[datetime] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self._session

    def _is_cache_valid(self) -> bool:
        if not self._cache or self._cache_ts is None:
            return False
        elapsed = (datetime.now() - self._cache_ts).total_seconds()
        return elapsed < self.CACHE_TTL

    # ──────────────────────────────────────────────────────────────
    # 1. fetch_us_market_summary
    # ──────────────────────────────────────────────────────────────
    async def fetch_us_market_summary(self) -> Dict[str, Dict]:
        """
        Yahoo Finance에서 전체 US 심볼 시세 조회 (1회 호출).

        Returns:
            {심볼: {price, change, change_pct, name, ...}}
        """
        if self._is_cache_valid():
            return self._cache

        result = await self._fetch_via_v7()
        if not result:
            logger.warning("[USMarket] v7 API 실패, v8 spark 폴백 시도")
            result = await self._fetch_via_v8_spark()

        if result:
            self._cache = result
            self._cache_ts = datetime.now()
            logger.info(f"[USMarket] {len(result)}개 심볼 시세 조회 완료")
        else:
            logger.warning("[USMarket] US 시장 데이터 조회 실패")

        return result or {}

    async def _fetch_via_v7(self) -> Optional[Dict[str, Dict]]:
        """Yahoo Finance v7 quote API"""
        try:
            session = await self._get_session()
            symbols_str = ",".join(US_SYMBOLS)
            url = f"{self.YAHOO_BASE_URL}/v7/finance/quote"
            params = {
                "symbols": symbols_str,
                "fields": "symbol,shortName,regularMarketPrice,"
                          "regularMarketChange,regularMarketChangePercent,"
                          "regularMarketVolume",
            }

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.debug(f"[USMarket] v7 HTTP {resp.status}")
                    return None
                data = await resp.json()

            quotes = data.get("quoteResponse", {}).get("result", [])
            if not quotes:
                return None

            result: Dict[str, Dict] = {}
            for q in quotes:
                symbol = q.get("symbol", "")
                if not symbol:
                    continue
                result[symbol] = {
                    "price": q.get("regularMarketPrice", 0),
                    "change": q.get("regularMarketChange", 0),
                    "change_pct": q.get("regularMarketChangePercent", 0),
                    "name": q.get("shortName", symbol),
                    "volume": q.get("regularMarketVolume", 0),
                }
            return result if result else None

        except Exception as e:
            logger.debug(f"[USMarket] v7 조회 오류: {e}")
            return None

    async def _fetch_via_v8_spark(self) -> Optional[Dict[str, Dict]]:
        """Yahoo Finance v8 spark API (폴백). 20개 제한 → 청크 분할 호출."""
        try:
            session = await self._get_session()
            url = f"{self.YAHOO_BASE_URL}/v8/finance/spark"
            chunk_size = 20  # Yahoo v8 spark 심볼 제한

            result: Dict[str, Dict] = {}

            for i in range(0, len(US_SYMBOLS), chunk_size):
                chunk = US_SYMBOLS[i:i + chunk_size]
                params = {
                    "symbols": ",".join(chunk),
                    "range": "1d",
                    "interval": "1d",
                }

                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.debug(f"[USMarket] v8 spark chunk HTTP {resp.status}")
                        continue
                    data = await resp.json()

                if not data:
                    continue

                self._parse_v8_spark_data(data, result)

            return result if result else None

        except Exception as e:
            logger.debug(f"[USMarket] v8 spark 조회 오류: {e}")
            return None

    @staticmethod
    def _parse_v8_spark_data(data: Dict, result: Dict[str, Dict]):
        """v8 spark 응답 파싱 (flat / nested 형식 자동 감지)"""
        if "spark" in data:
            # 레거시 형식: {"spark": {"result": [...]}}
            items = data.get("spark", {}).get("result", [])
            if not items:
                return
            for item in items:
                symbol = item.get("symbol", "")
                response_data = item.get("response", [{}])
                if not response_data:
                    continue
                meta = response_data[0].get("meta", {})
                prev_close = meta.get("chartPreviousClose", 0) or meta.get("previousClose", 0)
                current = meta.get("regularMarketPrice", 0)
                if prev_close and current:
                    change = current - prev_close
                    change_pct = (change / prev_close) * 100
                else:
                    change = 0
                    change_pct = 0
                result[symbol] = {
                    "price": current,
                    "change": change,
                    "change_pct": change_pct,
                    "name": symbol,
                    "volume": meta.get("regularMarketVolume", 0),
                }
        else:
            # 현행 형식: {symbol: {close: [...], chartPreviousClose: ...}}
            for symbol, item in data.items():
                if not isinstance(item, dict):
                    continue
                closes = item.get("close", [])
                current = closes[-1] if closes else 0
                prev_close = item.get("chartPreviousClose", 0) or item.get("previousClose", 0)

                if prev_close and current:
                    change = current - prev_close
                    change_pct = (change / prev_close) * 100
                else:
                    change = 0
                    change_pct = 0

                result[symbol] = {
                    "price": current,
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                    "name": symbol,
                    "volume": 0,
                }

    # ──────────────────────────────────────────────────────────────
    # 2. get_sector_signals — US 등락률 → 한국 테마 부스트
    # ──────────────────────────────────────────────────────────────
    async def get_sector_signals(self) -> Dict[str, Dict]:
        """
        US 등락률 → 한국 테마 부스트 점수 변환.

        Returns:
            {테마명: {boost, us_avg_pct, us_max_pct, top_movers}}
        """
        quotes = await self.fetch_us_market_summary()
        if not quotes:
            return {}

        signals: Dict[str, Dict] = {}

        for theme_name, mapping in US_KOREA_SECTOR_MAP.items():
            symbols = mapping["symbols"]
            thresholds = mapping["thresholds"]

            # 해당 그룹의 등락률 수집
            pcts: List[Tuple[str, float]] = []
            for sym in symbols:
                q = quotes.get(sym)
                if q:
                    pcts.append((sym, q["change_pct"]))

            if not pcts:
                continue

            pct_values = [p for _, p in pcts]
            avg_pct = sum(pct_values) / len(pct_values)
            max_pct = max(pct_values)
            min_pct = min(pct_values)

            # 상위 무버 (|등락률| 큰 순)
            sorted_movers = sorted(pcts, key=lambda x: abs(x[1]), reverse=True)
            top_movers = [
                f"{sym}({pct:+.1f}%)" for sym, pct in sorted_movers[:3]
            ]

            # 부스트 계산
            # 상승: max_pct가 임계값 이상이면 양수 부스트
            # 하락: 양수 부스트 없고 avg_pct가 음수이면 음수 부스트
            boost = 0
            if max_pct > 0:
                ref_pct = max_pct
                for threshold, score in thresholds:
                    if ref_pct >= threshold:
                        boost = score
                        break
            if boost == 0 and avg_pct < 0:
                ref_pct = abs(avg_pct)
                for threshold, score in thresholds:
                    if ref_pct >= threshold:
                        boost = -score
                        break

            if boost != 0:
                signals[theme_name] = {
                    "boost": boost,
                    "us_avg_pct": round(avg_pct, 2),
                    "us_max_pct": round(max_pct, 2),
                    "us_min_pct": round(min_pct, 2),
                    "top_movers": top_movers,
                }

        return signals

    # ──────────────────────────────────────────────────────────────
    # 3. get_overnight_signal — 종합 시그널
    # ──────────────────────────────────────────────────────────────
    async def get_overnight_signal(self) -> Dict[str, Any]:
        """
        종합 오버나이트 시그널.

        Returns:
            {
                sentiment: "bullish" | "bearish" | "neutral",
                indices: {S&P500: ..., NASDAQ: ..., ...},
                sector_signals: {테마: {boost, ...}},
                summary: "요약 텍스트",
            }
        """
        quotes = await self.fetch_us_market_summary()
        if not quotes:
            return {
                "sentiment": "neutral",
                "indices": {},
                "sector_signals": {},
                "summary": "US 시장 데이터 조회 실패",
            }

        # 1. 지수 등락률
        indices: Dict[str, Dict] = {}
        idx_pcts: List[float] = []
        for sym in INDEX_SYMBOLS:
            q = quotes.get(sym)
            if q:
                display_name = INDEX_NAMES.get(sym, sym)
                indices[display_name] = {
                    "price": q["price"],
                    "change": round(q["change"], 2),
                    "change_pct": round(q["change_pct"], 2),
                }
                idx_pcts.append(q["change_pct"])

        # 2. 시장 심리 판단
        if idx_pcts:
            avg_idx = sum(idx_pcts) / len(idx_pcts)
            if avg_idx >= 1.0:
                sentiment = "bullish"
            elif avg_idx <= -1.0:
                sentiment = "bearish"
            else:
                sentiment = "neutral"
        else:
            sentiment = "neutral"
            avg_idx = 0

        # 3. 섹터 시그널
        sector_signals = await self.get_sector_signals()

        # 4. 요약 텍스트 생성
        summary_parts = []
        sentiment_kr = {"bullish": "강세", "bearish": "약세", "neutral": "보합"}.get(
            sentiment, "보합"
        )
        summary_parts.append(f"US 시장 {sentiment_kr} 마감")

        # 지수 요약
        idx_strs = []
        for name, info in indices.items():
            idx_strs.append(f"{name} {info['change_pct']:+.1f}%")
        if idx_strs:
            summary_parts.append(f"({', '.join(idx_strs)})")

        # 테마 영향 요약
        boosted_themes = [
            f"{t}({s['boost']:+d})" for t, s in sector_signals.items()
        ]
        if boosted_themes:
            summary_parts.append(f"→ 한국 테마 영향: {', '.join(boosted_themes)}")

        return {
            "sentiment": sentiment,
            "indices": indices,
            "sector_signals": sector_signals,
            "summary": " ".join(summary_parts),
        }

    async def close(self):
        """세션 정리"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


# ──────────────────────────────────────────────────────────────────
# 싱글톤
# ──────────────────────────────────────────────────────────────────
_us_market_data: Optional[USMarketData] = None


def get_us_market_data() -> USMarketData:
    """싱글톤 USMarketData 반환"""
    global _us_market_data
    if _us_market_data is None:
        _us_market_data = USMarketData()
    return _us_market_data
