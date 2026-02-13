"""
종목 검증 오케스트레이터

뉴스 검증 + DART 공시 검증 + MCP 기반 수급/공매도/트렌드 검증을
병렬 실행하여 진입 전 종목의 펀더멘털 리스크를 평가합니다.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from .news_verifier import NewsVerifier, NewsCheckResult
from .dart_checker import DartChecker, DartCheckResult


@dataclass
class SupplyDemandResult:
    """수급 검증 결과 (외국인/기관 순매수)"""
    foreign_net_buying: bool = False
    institutional_net_buying: bool = False
    confidence_adjustment: float = 0.0


@dataclass
class ShortSellingResult:
    """공매도 검증 결과"""
    in_top50: bool = False
    confidence_adjustment: float = 0.0


@dataclass
class TrendBuzzResult:
    """검색 트렌드 검증 결과"""
    trend_direction: str = "neutral"  # rising / falling / neutral
    confidence_adjustment: float = 0.0


@dataclass
class ValidationResult:
    """종목 검증 결과"""
    approved: bool = True
    confidence_adjustment: float = 0.0
    block_reason: str = ""
    news_result: Optional[NewsCheckResult] = None
    dart_result: Optional[DartCheckResult] = None
    supply_demand_result: Optional[SupplyDemandResult] = None
    short_selling_result: Optional[ShortSellingResult] = None
    trend_buzz_result: Optional[TrendBuzzResult] = None


class StockValidator:
    """뉴스/공시/수급/공매도/트렌드 기반 종목 검증 통합 관리자"""

    # 캐시 TTL (초)
    _SUPPLY_DEMAND_TTL = 1800    # 30분
    _TREND_BUZZ_TTL = 7200       # 2시간
    _CACHE_MAX_SIZE = 300

    def __init__(self):
        self.news_verifier = NewsVerifier()
        self.dart_checker = DartChecker()

        # MCP 매니저 (lazy init)
        self._mcp_manager = None

        # MCP 결과 캐시: {key: (timestamp, result)}
        self._supply_demand_cache: Dict[str, Tuple[float, SupplyDemandResult]] = {}
        self._trend_buzz_cache: Dict[str, Tuple[float, TrendBuzzResult]] = {}

    async def initialize(self):
        """초기화 (DART corp_code + MCP 매니저)"""
        try:
            await self.dart_checker.ensure_corp_code_map()
        except Exception as e:
            logger.warning(f"[종목검증] DART 초기화 실패 (무시): {e}")

        # MCP 매니저 연결
        try:
            from src.utils.mcp_client import get_mcp_manager
            self._mcp_manager = get_mcp_manager()
            # initialize()는 run_trader에서 이미 호출되므로 여기서는 참조만 저장
        except ImportError:
            logger.debug("[종목검증] MCP SDK 미설치 → MCP 검증 비활성")
            self._mcp_manager = None
        except Exception as e:
            logger.warning(f"[종목검증] MCP 매니저 연결 실패 (무시): {e}")
            self._mcp_manager = None

        logger.info("[종목검증] StockValidator 초기화 완료")

    async def validate(self, symbol: str, stock_name: str) -> ValidationResult:
        """
        종목 검증 (뉴스 + 공시 + 수급 + 공매도 + 트렌드 병렬 실행)

        Args:
            symbol: 종목코드 (예: "005930")
            stock_name: 종목명 (예: "삼성전자")

        Returns:
            ValidationResult: 검증 결과
        """
        try:
            # 5개 검증 병렬 실행
            news_result, dart_result, sd_result, ss_result, tb_result = await asyncio.gather(
                self._safe_check_news(symbol, stock_name),
                self._safe_check_dart(symbol),
                self._safe_check_supply_demand(symbol),
                self._safe_check_short_selling(symbol),
                self._safe_check_trend_buzz(stock_name),
            )

            # DART block 공시 → 즉시 차단
            if dart_result.risk_level == "block":
                reason = f"위험 공시 감지: {', '.join(dart_result.risk_disclosures[:3])}"
                logger.info(f"[종목검증] {symbol} {stock_name} 차단: {reason}")
                return ValidationResult(
                    approved=False,
                    confidence_adjustment=-1.0,
                    block_reason=reason,
                    news_result=news_result,
                    dart_result=dart_result,
                    supply_demand_result=sd_result,
                    short_selling_result=ss_result,
                    trend_buzz_result=tb_result,
                )

            # confidence 조정 합산 (범위 제한: -0.30 ~ +0.25)
            total_adj = (
                news_result.confidence_adjustment
                + dart_result.confidence_adjustment
                + sd_result.confidence_adjustment
                + ss_result.confidence_adjustment
                + tb_result.confidence_adjustment
            )
            total_adj = max(-0.30, min(0.25, total_adj))

            return ValidationResult(
                approved=True,
                confidence_adjustment=total_adj,
                news_result=news_result,
                dart_result=dart_result,
                supply_demand_result=sd_result,
                short_selling_result=ss_result,
                trend_buzz_result=tb_result,
            )

        except Exception as e:
            # 예외 시 통과 (API 실패가 거래를 막지 않음)
            logger.debug(f"[종목검증] {symbol} 검증 예외 (통과): {e}")
            return ValidationResult(approved=True)

    # ───────────────────── 기존 검증 (뉴스/DART) ─────────────────────

    async def _safe_check_news(self, symbol: str, stock_name: str) -> NewsCheckResult:
        """뉴스 검증 (예외 안전)"""
        try:
            return await self.news_verifier.check_news(symbol, stock_name)
        except Exception as e:
            logger.debug(f"[종목검증] 뉴스 검증 오류 ({symbol}): {e}")
            return NewsCheckResult()

    async def _safe_check_dart(self, symbol: str) -> DartCheckResult:
        """DART 공시 검증 (예외 안전)"""
        try:
            return await self.dart_checker.check_disclosures(symbol)
        except Exception as e:
            logger.debug(f"[종목검증] DART 검증 오류 ({symbol}): {e}")
            return DartCheckResult()

    # ───────────────────── MCP 기반 검증 (수급/공매도/트렌드) ─────────────────────

    async def _safe_check_supply_demand(self, symbol: str) -> SupplyDemandResult:
        """외국인/기관 수급 검증 (캐시 30분, 예외 안전)"""
        if not self._mcp_manager or not self._mcp_manager.is_server_available("pykrx"):
            return SupplyDemandResult()

        # 캐시 확인
        cached = self._get_cache(self._supply_demand_cache, symbol, self._SUPPLY_DEMAND_TTL)
        if cached is not None:
            return cached

        try:
            result = await self._fetch_supply_demand(symbol)
            self._set_cache(self._supply_demand_cache, symbol, result, self._CACHE_MAX_SIZE)
            return result
        except Exception as e:
            logger.debug(f"[종목검증] 수급 검증 오류 ({symbol}): {e}")
            return SupplyDemandResult()

    async def _safe_check_short_selling(self, symbol: str) -> ShortSellingResult:
        """공매도 상위 검증 (pykrx-mcp v0.1.3에 도구 미제공 → 즉시 기본값)"""
        # 향후 pykrx-mcp에 공매도 도구 추가 시 캐시 로직 복원
        return ShortSellingResult()

    async def _safe_check_trend_buzz(self, stock_name: str) -> TrendBuzzResult:
        """검색 트렌드 검증 (캐시 2시간, 예외 안전)"""
        if not self._mcp_manager or not self._mcp_manager.is_server_available("naver_search"):
            return TrendBuzzResult()

        cached = self._get_cache(self._trend_buzz_cache, stock_name, self._TREND_BUZZ_TTL)
        if cached is not None:
            return cached

        try:
            result = await self._fetch_trend_buzz(stock_name)
            self._set_cache(self._trend_buzz_cache, stock_name, result, self._CACHE_MAX_SIZE)
            return result
        except Exception as e:
            logger.debug(f"[종목검증] 트렌드 검증 오류 ({stock_name}): {e}")
            return TrendBuzzResult()

    # ───────────────────── MCP 도구 호출 ─────────────────────

    async def _fetch_supply_demand(self, symbol: str) -> SupplyDemandResult:
        """pykrx-mcp로 종목별 투자자 유형 거래대금 조회 (외국인/기관 순매수 판별)"""
        # pykrx는 장중 당일 데이터 미제공 → 최근 3거래일 조회 (주말/공휴일 대비)
        today = datetime.now()
        start_date = (today - timedelta(days=5)).strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")

        resp = await self._mcp_manager.call_tool(
            "pykrx", "get_market_trading_value_by_date",
            {"ticker": symbol, "start_date": start_date, "end_date": end_date}
        )
        if not resp:
            return SupplyDemandResult()

        data = self._parse_mcp_text(resp)
        if not data:
            return SupplyDemandResult()

        result = SupplyDemandResult()

        # 데이터에서 투자자별 순매수 금액 추출
        foreign_val = self._extract_investor_value(data, ["외국인합계", "외국인"])
        inst_val = self._extract_investor_value(data, ["기관합계", "금융투자", "보험", "투신", "연기금등"])

        if foreign_val is not None and foreign_val > 0:
            result.foreign_net_buying = True
            result.confidence_adjustment += 0.10
            logger.debug(f"[종목검증] {symbol} 외국인 순매수 ({foreign_val:,.0f}원) → +0.10")

        if inst_val is not None and inst_val > 0:
            result.institutional_net_buying = True
            result.confidence_adjustment += 0.05
            logger.debug(f"[종목검증] {symbol} 기관 순매수 ({inst_val:,.0f}원) → +0.05")

        return result

    async def _fetch_short_selling(self, symbol: str) -> ShortSellingResult:
        """공매도 상위 조회 (현재 pykrx-mcp에 도구 미제공 → 기본값)"""
        # pykrx-mcp v0.1.3에는 공매도 관련 도구가 없음
        # 향후 get_shorting_volume_top50 등이 추가되면 여기서 호출
        return ShortSellingResult()

    async def _fetch_trend_buzz(self, stock_name: str) -> TrendBuzzResult:
        """naver-search-mcp로 검색 트렌드 조회"""
        today = datetime.now()
        start_date = (today - timedelta(days=14)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        resp = await self._mcp_manager.call_tool(
            "naver_search", "datalab_search",
            {
                "startDate": start_date,
                "endDate": end_date,
                "timeUnit": "date",
                "keywordGroups": [
                    {"groupName": stock_name, "keywords": [stock_name, f"{stock_name} 주가"]}
                ],
            }
        )
        if not resp:
            return TrendBuzzResult()

        data = self._parse_mcp_text(resp)
        return self._analyze_trend(data)

    # ───────────────────── 유틸리티 ─────────────────────

    def _parse_mcp_text(self, result: Any) -> Any:
        """MCP CallToolResult에서 텍스트 추출 + JSON 파싱"""
        try:
            # MCP 에러 응답 체크
            if hasattr(result, "isError") and result.isError:
                return None

            if hasattr(result, "content") and result.content:
                text = ""
                for block in result.content:
                    if hasattr(block, "text"):
                        text += block.text
                if text:
                    parsed = json.loads(text)
                    # pykrx 에러 응답 체크 (isError=false이지만 error 키 존재)
                    if isinstance(parsed, dict) and "error" in parsed:
                        logger.debug(f"[종목검증] MCP 응답 에러: {parsed['error']}")
                        return None
                    return parsed
            return None
        except (json.JSONDecodeError, AttributeError):
            return None

    def _extract_investor_value(self, data: Any, keys: list) -> Optional[float]:
        """투자자 유형별 거래대금 추출 (여러 키 중 첫 매칭)"""
        try:
            # 데이터 구조: {"data": [{"날짜": ..., "외국인합계": 123, ...}]} 또는 직접 dict
            rows = data
            if isinstance(data, dict):
                rows = data.get("data", data.get("results", [data]))
            if isinstance(rows, list) and rows:
                row = rows[-1] if isinstance(rows[-1], dict) else rows[0]
            elif isinstance(rows, dict):
                row = rows
            else:
                return None

            # 합계 키 먼저 시도, 없으면 개별 키 합산
            for key in keys[:2]:  # 합계 키 우선 (외국인합계, 기관합계)
                if key in row:
                    val = row[key]
                    return float(val) if val is not None else None

            # 개별 키 합산 (금융투자+보험+투신+연기금등)
            total = 0.0
            found = False
            for key in keys[2:]:
                if key in row and row[key] is not None:
                    total += float(row[key])
                    found = True
            return total if found else None

        except (TypeError, ValueError, IndexError, KeyError):
            return None

    def _analyze_trend(self, data: Any) -> TrendBuzzResult:
        """네이버 DataLab 트렌드 데이터 분석"""
        if not data or not isinstance(data, dict):
            return TrendBuzzResult()

        try:
            results = data.get("results", [])
            if not results:
                return TrendBuzzResult()

            items = results[0].get("data", [])
            if len(items) < 14:
                return TrendBuzzResult()

            # 최근 7일 vs 이전 7일 비교
            recent = [item.get("ratio", 0) for item in items[-7:]]
            previous = [item.get("ratio", 0) for item in items[-14:-7]]

            recent_avg = sum(recent) / len(recent) if recent else 0
            previous_avg = sum(previous) / len(previous) if previous else 0

            if previous_avg == 0:
                return TrendBuzzResult()

            change_rate = (recent_avg - previous_avg) / previous_avg

            if change_rate >= 0.3:  # 30% 이상 상승
                logger.debug(f"[종목검증] 검색 트렌드 상승 ({change_rate:.1%}) → +0.05")
                return TrendBuzzResult(trend_direction="rising", confidence_adjustment=0.05)
            elif change_rate <= -0.3:  # 30% 이상 하락
                logger.debug(f"[종목검증] 검색 트렌드 하락 ({change_rate:.1%}) → -0.05")
                return TrendBuzzResult(trend_direction="falling", confidence_adjustment=-0.05)

            return TrendBuzzResult()

        except Exception:
            return TrendBuzzResult()

    # ───────────────────── 캐시 관리 ─────────────────────

    def _get_cache(self, cache: Dict, key: str, ttl: float) -> Optional[Any]:
        """TTL 기반 캐시 조회"""
        if key in cache:
            ts, value = cache[key]
            if time.monotonic() - ts < ttl:
                return value
            del cache[key]
        return None

    def _set_cache(self, cache: Dict, key: str, value: Any, max_size: int):
        """캐시 저장 (크기 상한 초과 시 오래된 항목 제거)"""
        if len(cache) >= max_size:
            # 가장 오래된 항목 제거
            oldest_key = min(cache, key=lambda k: cache[k][0])
            del cache[oldest_key]
        cache[key] = (time.monotonic(), value)


# 전역 싱글톤 (클래스 정의 이후 배치)
_stock_validator: Optional[StockValidator] = None


def get_stock_validator() -> StockValidator:
    """전역 싱글톤 StockValidator 반환"""
    global _stock_validator
    if _stock_validator is None:
        _stock_validator = StockValidator()
    return _stock_validator
