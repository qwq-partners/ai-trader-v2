"""
AI Trading Bot v2 - 종목 스크리너

동적으로 후보종목을 발굴합니다.

스크리닝 기준:
1. 거래량 급증 종목 (전일 대비 200%+)
2. 등락률 상위 종목 (상승률 상위)
3. 신고가 돌파 종목 (20일/52주)
4. 테마 뉴스 관련 종목 (LLM 추출)

데이터 소스:
- KIS Open API (1차)
- 네이버 금융 크롤링 (백업/보조)
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set, Any, Tuple
import aiohttp
from bs4 import BeautifulSoup
from loguru import logger

from ...utils.kis_token_manager import get_token_manager
from ...data.providers.kis_market_data import KISMarketData, get_kis_market_data


# ============================================================
# 네이버 금융 URL
# ============================================================
NAVER_FINANCE_BASE = "https://finance.naver.com"
NAVER_VOLUME_RANK = f"{NAVER_FINANCE_BASE}/sise/sise_quant.naver"       # 거래량 상위
NAVER_RISE_RANK = f"{NAVER_FINANCE_BASE}/sise/sise_rise.naver"         # 상승률 상위


@dataclass
class ScreenedStock:
    """스크리닝된 종목"""
    symbol: str
    name: str = ""
    price: float = 0
    change_pct: float = 0
    volume: int = 0
    volume_ratio: float = 0  # 전일 대비 거래량 비율
    score: float = 0
    reasons: List[str] = field(default_factory=list)
    screened_at: datetime = field(default_factory=datetime.now)

    def __hash__(self):
        return hash(self.symbol)

    def __eq__(self, other):
        return self.symbol == other.symbol


class StockScreener:
    """
    종목 스크리너

    KIS API와 LLM을 활용하여 매매 후보 종목을 실시간 발굴합니다.
    """

    # ETF/ETN 브랜드 및 키워드 (대문자 비교)
    _ETF_BRANDS = {
        "KODEX", "TIGER", "KOSEF", "ARIRANG", "KBSTAR", "HANARO",
        "SOL", "ACE", "PLUS", "RISE", "BNK", "TIMEFOLIO", "WOORI",
        "FOCUS", "TREX",
    }
    _ETF_KEYWORDS = {"ETF", "ETN", "레버리지", "인버스", "선물", "채권", "원유", "금선물"}

    @staticmethod
    def _is_etf_etn(name: str) -> bool:
        """종목명 기반 ETF/ETN/파생상품 판별"""
        upper = name.upper()
        for brand in StockScreener._ETF_BRANDS:
            if upper.startswith(brand):
                return True
        for kw in StockScreener._ETF_KEYWORDS:
            if kw.upper() in upper:
                return True
        return False

    def __init__(self, kis_market_data: Optional[KISMarketData] = None, stock_master=None):
        self._token_manager = get_token_manager()
        self._session: Optional[aiohttp.ClientSession] = None
        self._kis_market_data = kis_market_data
        self._stock_master = stock_master  # StockMaster 인스턴스 (종목 DB)

        # 캐시
        self._cache: Dict[str, List[ScreenedStock]] = {}
        self._cache_time: Dict[str, datetime] = {}
        self._cache_ttl = 1800  # 30분 (프리장 중 캐시 유지)

        # 종목코드→이름 역매핑 (O(1) 조회용)
        # stock_master가 있으면 DB 캐시 활용, 없으면 KNOWN_STOCKS 폴백
        self._code_to_name: Dict[str, str] = {}
        self._refresh_code_to_name()

        # 설정
        self.min_volume_ratio = 2.0  # 최소 거래량 비율
        self.min_change_pct = 1.0    # 최소 등락률
        self.max_change_pct = 15.0   # 최대 등락률 (과열 제외)

    def set_stock_master(self, stock_master):
        """stock_master 인스턴스 설정 (런타임에서 주입)"""
        self._stock_master = stock_master
        self._refresh_code_to_name()

    def _refresh_code_to_name(self):
        """종목코드→이름 매핑 갱신 (stock_master DB 우선, KNOWN_STOCKS 폴백)"""
        if self._stock_master and hasattr(self._stock_master, '_name_cache') and self._stock_master._name_cache:
            # stock_master의 이름→코드 캐시를 역매핑
            self._code_to_name = {
                code: name for name, code in self._stock_master._name_cache.items()
            }
            logger.debug(f"[Screener] code_to_name 갱신: stock_master DB ({len(self._code_to_name)}종목)")
        else:
            # 폴백: KNOWN_STOCKS (~40개)
            self._code_to_name = {
                code: name for name, code in self.KNOWN_STOCKS.items()
            }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout_sec = int(os.environ.get("KIS_API_TIMEOUT_SECONDS", "15"))
            timeout = aiohttp.ClientTimeout(total=timeout_sec)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _get_headers(self, tr_id: str) -> Dict[str, str]:
        """API 헤더 생성"""
        token = await self._token_manager.get_access_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._token_manager.app_key,
            "appsecret": self._token_manager.app_secret,
            "tr_id": tr_id,
        }

    # ============================================================
    # 거래량 급증 종목
    # ============================================================

    async def screen_volume_surge(self, limit: int = 30) -> List[ScreenedStock]:
        """
        거래량 급증 종목 스크리닝

        전일 대비 거래량 200% 이상 종목
        """
        cache_key = "volume_surge"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        stocks = []
        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPST01710000")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"

            params = {
                "FID_COND_MRKT_DIV_CODE": "J",  # 주식
                "FID_COND_SCR_DIV_CODE": "20101",
                "FID_INPUT_ISCD": "0000",  # 전체
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "0",
                "FID_VOL_CNT": "0",
                "FID_INPUT_DATE_1": "",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"거래량 순위 조회 실패: {resp.status}")
                    return stocks

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"거래량 순위 API 오류: {data.get('msg1')}")
                    return stocks

                output = data.get("output", [])

                for item in output[:limit]:
                    symbol = item.get("mksc_shrn_iscd", "").zfill(6)
                    name = item.get("hts_kor_isnm", "")
                    price = float(item.get("stck_prpr", 0) or 0)
                    change_pct = float(item.get("prdy_ctrt", 0) or 0)
                    volume = int(item.get("acml_vol", 0) or 0)
                    vol_inrt = float(item.get("vol_inrt", 0) or 0)  # 거래량 증가율

                    # 거래량 비율 계산 (증가율 + 100 = 비율)
                    volume_ratio = (vol_inrt + 100) / 100 if vol_inrt else 1.0

                    # 필터링
                    if price < 1000:  # 1,000원 미만 동전주 항상 제외
                        continue
                    if volume_ratio < self.min_volume_ratio:
                        continue
                    if change_pct < 0:  # 하락 종목 제외
                        continue
                    if change_pct > self.max_change_pct:  # 과열 종목 제외
                        continue

                    score = min(volume_ratio * 10 + change_pct * 5, 100)

                    stocks.append(ScreenedStock(
                        symbol=symbol,
                        name=name,
                        price=price,
                        change_pct=change_pct,
                        volume=volume,
                        volume_ratio=volume_ratio,
                        score=score,
                        reasons=[f"거래량 {volume_ratio:.1f}배", f"등락률 {change_pct:+.2f}%"],
                    ))

            # 점수 순 정렬
            stocks.sort(key=lambda x: x.score, reverse=True)
            self._update_cache(cache_key, stocks)

            logger.info(f"[Screener] 거래량 급증 종목 {len(stocks)}개 발굴")
            return stocks

        except Exception as e:
            logger.error(f"거래량 스크리닝 오류: {e}")
            return stocks

    # ============================================================
    # 등락률 상위 종목
    # ============================================================

    async def screen_top_gainers(self, limit: int = 30) -> List[ScreenedStock]:
        """
        등락률 상위 종목 스크리닝

        상승률 상위 종목 (1% ~ 15%)
        """
        cache_key = "top_gainers"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        stocks = []
        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPST01710000")  # 거래량 순위와 동일한 TR

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"

            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20101",  # 상승률 순
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "0",
                "FID_VOL_CNT": "0",
                "FID_INPUT_DATE_1": "",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"등락률 순위 조회 실패: {resp.status}")
                    return stocks

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"등락률 순위 API 오류: {data.get('msg1')}")
                    return stocks

                output = data.get("output", [])

                for item in output[:limit]:
                    symbol = item.get("mksc_shrn_iscd", "").zfill(6)
                    name = item.get("hts_kor_isnm", "")
                    price = float(item.get("stck_prpr", 0) or 0)
                    change_pct = float(item.get("prdy_ctrt", 0) or 0)
                    volume = int(item.get("acml_vol", 0) or 0)

                    # 필터링
                    if price < 1000:  # 1,000원 미만 동전주 항상 제외
                        continue
                    if change_pct < self.min_change_pct:
                        continue
                    if change_pct > self.max_change_pct:
                        continue

                    score = min(change_pct * 8, 100)

                    stocks.append(ScreenedStock(
                        symbol=symbol,
                        name=name,
                        price=price,
                        change_pct=change_pct,
                        volume=volume,
                        score=score,
                        reasons=[f"등락률 {change_pct:+.2f}%"],
                    ))

            stocks.sort(key=lambda x: x.score, reverse=True)
            self._update_cache(cache_key, stocks)

            logger.info(f"[Screener] 등락률 상위 종목 {len(stocks)}개 발굴")
            return stocks

        except Exception as e:
            logger.error(f"등락률 스크리닝 오류: {e}")
            return stocks

    # ============================================================
    # 신고가 돌파 종목
    # ============================================================

    async def screen_new_highs(self, limit: int = 20) -> List[ScreenedStock]:
        """
        신고가 돌파 종목 스크리닝

        52주 신고가 또는 20일 신고가 돌파 종목
        """
        cache_key = "new_highs"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        stocks = []
        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPST01720000")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/capture-uplowprice"

            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "10301",  # 신고가
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": "0",     # 정렬 구분 (필수)
                "FID_INPUT_CNT_1": "0",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "0",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"신고가 종목 조회 실패: {resp.status}")
                    return stocks

                data = await resp.json()

                rt_cd = data.get("rt_cd")
                if rt_cd != "0":
                    if rt_cd:
                        logger.warning(
                            f"신고가 API 오류: rt_cd={rt_cd}, "
                            f"msg_cd={data.get('msg_cd')}, msg={data.get('msg1', '(없음)')}"
                        )
                    else:
                        logger.debug("[Screener] 신고가 API: 장 마감 후 빈 응답")
                    return stocks

                output = data.get("output", [])

                for item in output[:limit]:
                    symbol = item.get("mksc_shrn_iscd", "").zfill(6)
                    name = item.get("hts_kor_isnm", "")
                    price = float(item.get("stck_prpr", 0) or 0)
                    change_pct = float(item.get("prdy_ctrt", 0) or 0)
                    volume = int(item.get("acml_vol", 0) or 0)

                    # 동전주/과열 제외
                    if price < 1000:  # 1,000원 미만 동전주 항상 제외
                        continue
                    if change_pct > self.max_change_pct:
                        continue

                    score = 70 + min(change_pct * 2, 30)  # 신고가 기본 70점

                    stocks.append(ScreenedStock(
                        symbol=symbol,
                        name=name,
                        price=price,
                        change_pct=change_pct,
                        volume=volume,
                        score=score,
                        reasons=["신고가 돌파", f"등락률 {change_pct:+.2f}%"],
                    ))

            stocks.sort(key=lambda x: x.score, reverse=True)
            self._update_cache(cache_key, stocks)

            logger.info(f"[Screener] 신고가 종목 {len(stocks)}개 발굴")
            return stocks

        except Exception as e:
            logger.error(f"신고가 스크리닝 오류: {type(e).__name__}: {e}")
            return stocks

    # ============================================================
    # 등락률 순위 (KIS FHPST01700000)
    # ============================================================

    async def screen_fluctuation_rank(self, limit: int = 30) -> List[ScreenedStock]:
        """
        등락률 순위 기반 스크리닝 (KISMarketData 활용)

        FHPST01700000 API로 등락률 상위 종목 조회
        """
        cache_key = "fluctuation_rank"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key][:limit]

        stocks = []
        kmd = self._kis_market_data or get_kis_market_data()

        try:
            raw = await kmd.fetch_fluctuation_rank(limit=limit)

            for item in raw:
                symbol = item.get("symbol", "")
                name = item.get("name", "")
                price = item.get("price", 0)
                change_pct = item.get("change_pct", 0)
                volume = item.get("volume", 0)

                if price < 1000 or change_pct < 0 or change_pct > self.max_change_pct:
                    continue

                score = min(change_pct * 7 + 20, 100)

                stocks.append(ScreenedStock(
                    symbol=symbol,
                    name=name,
                    price=price,
                    change_pct=change_pct,
                    volume=volume,
                    score=score,
                    reasons=[f"등락률순위 {change_pct:+.2f}%"],
                ))

            stocks.sort(key=lambda x: x.score, reverse=True)
            self._update_cache(cache_key, stocks)
            logger.info(f"[Screener] 등락률 순위 {len(stocks)}개 발굴")
            return stocks[:limit]

        except Exception as e:
            logger.error(f"등락률 순위 스크리닝 오류: {e}")
            return stocks

    # ============================================================
    # 외국인 순매수 상위 (KIS FHPTJ04400000)
    # ============================================================

    async def screen_foreign_buying(self, limit: int = 20) -> List[ScreenedStock]:
        """
        외국인 순매수 상위 종목 스크리닝

        FHPTJ04400000 API로 외국인 순매수 상위 조회
        """
        cache_key = "foreign_buying"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key][:limit]

        stocks = []
        kmd = self._kis_market_data or get_kis_market_data()

        try:
            # 코스피 + 코스닥 외국인 순매수 병합
            raw_kospi = await kmd.fetch_foreign_institution(market="0001", investor="1")
            raw_kosdaq = await kmd.fetch_foreign_institution(market="0002", investor="1")
            raw = raw_kospi + raw_kosdaq

            for item in raw:
                symbol = item.get("symbol", "")
                name = item.get("name", "")
                price = item.get("price", 0)
                change_pct = item.get("change_pct", 0)
                volume = item.get("volume", 0)
                net_buy_qty = item.get("net_buy_qty", 0)

                if price < 1000 or net_buy_qty <= 0:
                    continue

                score = min(60 + change_pct * 3, 100)

                stocks.append(ScreenedStock(
                    symbol=symbol,
                    name=name,
                    price=price,
                    change_pct=change_pct,
                    volume=volume,
                    score=score,
                    reasons=[f"외국인 순매수 {net_buy_qty:,}주"],
                ))

            stocks.sort(key=lambda x: x.score, reverse=True)
            self._update_cache(cache_key, stocks)
            logger.info(f"[Screener] 외국인 순매수 {len(stocks)}개 발굴")
            return stocks[:limit]

        except Exception as e:
            logger.error(f"외국인 순매수 스크리닝 오류: {e}")
            return stocks

    # ============================================================
    # 밸류에이션 기반 스크리닝 (KIS FHPST01790000)
    # ============================================================

    async def _apply_valuation_bonus(self, all_stocks: Dict[str, "ScreenedStock"]):
        """
        기존 후보 종목들의 PER/PBR을 개별 조회하여 저평가 종목에 보너스 부여

        FHKST01010100 API로 종목별 PER/PBR을 조회합니다.
        """
        kmd = self._kis_market_data or get_kis_market_data()

        try:
            # 점수 높은 순으로 정렬하여 상위 30개 조회
            symbols = sorted(all_stocks.keys(), key=lambda s: all_stocks[s].score, reverse=True)[:30]
            valuations = await kmd.fetch_batch_valuations(symbols)

            bonus_cnt = 0
            for symbol, val in valuations.items():
                if symbol not in all_stocks:
                    continue

                per = val.get("per", 0)
                pbr = val.get("pbr", 0)

                # 저PER (0 < PER <= 15) + 저PBR (0 < PBR < 1.0) 보너스
                bonus = 0
                reasons = []
                if 0 < per <= 15:
                    bonus += 8
                    reasons.append(f"저PER({per:.1f})")
                if 0 < pbr < 1.0:
                    bonus += 5
                    reasons.append(f"저PBR({pbr:.2f})")

                if bonus > 0:
                    all_stocks[symbol].score += bonus
                    all_stocks[symbol].reasons.extend(reasons)
                    bonus_cnt += 1

            if bonus_cnt:
                logger.info(f"[Screener] 밸류에이션 보너스 {bonus_cnt}개 적용")

        except Exception as e:
            logger.warning(f"[Screener] 밸류에이션 조회 오류 (무시): {e}")

    # ============================================================
    # 네이버 금융 크롤링 (백업 데이터 소스)
    # ============================================================

    async def _naver_crawl(self, url: str, params: dict = None) -> Optional[BeautifulSoup]:
        """네이버 금융 페이지 크롤링"""
        try:
            session = await self._get_session()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ko-KR,ko;q=0.9",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"네이버 금융 크롤링 실패: {resp.status}")
                    return None

                html = await resp.text()
                return BeautifulSoup(html, "html.parser")

        except Exception as e:
            logger.error(f"네이버 크롤링 오류: {e}")
            return None

    def _parse_naver_table(self, soup: BeautifulSoup, reason_prefix: str = "") -> List[ScreenedStock]:
        """네이버 금융 테이블 파싱"""
        stocks = []

        try:
            # 테이블 찾기 (다중 선택자로 안정성 확보)
            table = soup.find("table", {"class": "type_2"})
            if not table:
                table = soup.find("table", {"class": "type2"})
            if not table:
                table = soup.select_one("table.type_2, table.type2, div.box_type_l table")
            if not table:
                logger.warning(f"[스크리너] 네이버 금융 테이블 구조 변경 감지 ('{reason_prefix}' 파싱 실패)")
                return stocks

            rows = table.find_all("tr")

            for row in rows:
                try:
                    cols = row.find_all("td")
                    if len(cols) < 10:
                        continue

                    # 종목명/코드 추출
                    name_tag = cols[1].find("a")
                    if not name_tag:
                        continue

                    name = name_tag.text.strip()
                    href = name_tag.get("href", "")

                    # 종목코드 추출 (href에서)
                    symbol_match = re.search(r"code=(\d{6})", href)
                    if not symbol_match:
                        continue
                    symbol = symbol_match.group(1)

                    # 현재가
                    price_text = cols[2].text.strip().replace(",", "")
                    try:
                        price = float(price_text)
                    except (ValueError, TypeError):
                        price = 0

                    # 등락률
                    change_pct_text = cols[4].text.strip().replace("%", "").replace("+", "")
                    try:
                        change_pct = float(change_pct_text)
                    except (ValueError, TypeError):
                        change_pct = 0

                    # 거래량
                    volume_text = cols[5].text.strip().replace(",", "")
                    volume = int(volume_text) if volume_text.isdigit() else 0

                    # 필터링
                    if change_pct < 0:  # 하락 종목 제외
                        continue
                    if change_pct > self.max_change_pct:  # 과열 제외
                        continue

                    # 점수 계산
                    score = min(change_pct * 6 + 30, 100)

                    reasons = [f"등락률 {change_pct:+.2f}%"]
                    if reason_prefix:
                        reasons.insert(0, reason_prefix)

                    stocks.append(ScreenedStock(
                        symbol=symbol,
                        name=name,
                        price=price,
                        change_pct=change_pct,
                        volume=volume,
                        score=score,
                        reasons=reasons,
                    ))

                except Exception as e:
                    continue

        except Exception as e:
            logger.error(f"네이버 테이블 파싱 오류: {e}")

        return stocks

    async def naver_volume_rank(self, limit: int = 30) -> List[ScreenedStock]:
        """
        네이버 금융 - 거래량 상위 종목

        https://finance.naver.com/sise/sise_quant.naver
        """
        cache_key = "naver_volume"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        stocks = []
        try:
            soup = await self._naver_crawl(NAVER_VOLUME_RANK)
            if not soup:
                return stocks

            stocks = self._parse_naver_table(soup, "거래량 상위")

            # 점수 조정 (거래량 기반)
            for i, stock in enumerate(stocks[:limit]):
                stock.score = max(100 - i * 2, 50)  # 순위 기반 점수
                stock.reasons.append(f"거래량순위 {i+1}위")

            stocks = stocks[:limit]
            self._update_cache(cache_key, stocks)

            logger.info(f"[Screener/Naver] 거래량 상위 {len(stocks)}개 발굴")
            return stocks

        except Exception as e:
            logger.error(f"네이버 거래량 크롤링 오류: {e}")
            return stocks

    async def naver_rise_rank(self, limit: int = 30) -> List[ScreenedStock]:
        """
        네이버 금융 - 상승률 상위 종목

        https://finance.naver.com/sise/sise_rise.naver
        """
        cache_key = "naver_rise"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        stocks = []
        try:
            soup = await self._naver_crawl(NAVER_RISE_RANK)
            if not soup:
                return stocks

            stocks = self._parse_naver_table(soup, "상승률 상위")

            # 점수 조정
            for i, stock in enumerate(stocks[:limit]):
                stock.score = max(100 - i * 2, 50)
                stock.reasons.append(f"상승률순위 {i+1}위")

            stocks = stocks[:limit]
            self._update_cache(cache_key, stocks)

            logger.info(f"[Screener/Naver] 상승률 상위 {len(stocks)}개 발굴")
            return stocks

        except Exception as e:
            logger.error(f"네이버 상승률 크롤링 오류: {e}")
            return stocks

    async def naver_new_high(self, limit: int = 30) -> List[ScreenedStock]:
        """
        네이버 금융 - 신고가 종목

        참고: 네이버 금융에서 신고가 페이지가 폐쇄됨.
        상승률 상위 종목 중 고가 근접 종목으로 대체.
        """
        cache_key = "naver_new_high"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        # 신고가 페이지 없음 - 상승률 상위에서 높은 등락률 종목으로 대체
        stocks = []
        try:
            # 상승률 상위에서 가져온 종목 중 등락률 10% 이상을 신고가 후보로
            rise_stocks = await self.naver_rise_rank(limit=50)
            for stock in rise_stocks:
                if stock.change_pct >= 10.0:  # 10% 이상 상승 = 신고가 가능성 높음
                    new_stock = ScreenedStock(
                        symbol=stock.symbol,
                        name=stock.name,
                        price=stock.price,
                        change_pct=stock.change_pct,
                        volume=stock.volume,
                        score=min(stock.score + 15, 100),
                        reasons=["신고가 후보", f"등락률 {stock.change_pct:+.2f}%"],
                    )
                    stocks.append(new_stock)

            stocks = stocks[:limit]
            self._update_cache(cache_key, stocks)

            if stocks:
                logger.info(f"[Screener/Naver] 신고가 후보 {len(stocks)}개 발굴")
            return stocks

        except Exception as e:
            logger.error(f"네이버 신고가 후보 추출 오류: {e}")
            return stocks

    # ============================================================
    # 뉴스 기반 종목 추출 (LLM)
    # ============================================================

    # 주요 종목 이름→코드 매핑 (LLM 종목명 변환용)
    KNOWN_STOCKS = {
        "삼성전자": "005930", "SK하이닉스": "000660", "현대차": "005380",
        "LG에너지솔루션": "373220", "삼성바이오로직스": "207940", "삼성바이오": "207940",
        "셀트리온": "068270", "네이버": "035420", "카카오": "035720",
        "기아": "000270", "포스코홀딩스": "005490", "삼성SDI": "006400",
        "KB금융": "105560", "한화에어로스페이스": "012450", "한화에어로": "012450",
        "HD현대중공업": "329180", "에코프로BM": "247540", "에코프로비엠": "247540",
        "에코프로": "086520", "LG화학": "051910", "현대모비스": "012330",
        "POSCO홀딩스": "005490", "SK이노베이션": "096770", "LG전자": "066570",
        "삼성물산": "028260", "한국전력": "015760", "하나금융지주": "086790",
        "신한지주": "055550", "SK텔레콤": "017670", "KT": "030200",
        "한미반도체": "042700", "두산에너빌리티": "034020", "포스코퓨처엠": "003670",
        "알테오젠": "196170", "HD현대": "267250", "LIG넥스원": "079550",
        "한국항공우주": "047810", "삼성중공업": "010140", "하이브": "352820",
        "크래프톤": "259960", "SK바이오팜": "326030", "카카오뱅크": "323410",
        "SK": "034730", "LG": "003550", "한화솔루션": "009830",
        "현대건설": "000720", "만도": "204320", "한국타이어앤테크놀로지": "161390",
    }

    async def _get_stock_hints_for_llm(self) -> str:
        """LLM 프롬프트용 종목 힌트 생성 (stock_master DB 우선, KNOWN_STOCKS 폴백)"""
        if self._stock_master:
            try:
                top = await self._stock_master.get_top_stocks(limit=60)
                if top:
                    return "\n".join([f"  {name}={code}" for name, code in top])
            except Exception as e:
                logger.debug(f"[Screener] stock_master 힌트 조회 실패: {e}")
        # 폴백
        return "\n".join(
            [f"  {name}={code}" for name, code in list(self.KNOWN_STOCKS.items())[:30]]
        )

    async def _resolve_stock_name(self, name: str) -> str:
        """종목명 → 종목코드 변환 (stock_master DB 우선, KNOWN_STOCKS 폴백)"""
        # 1차: stock_master DB 조회
        if self._stock_master:
            try:
                code = await self._stock_master.lookup_ticker(name)
                if code:
                    return code
            except Exception:
                pass
        # 2차: KNOWN_STOCKS 폴백
        return self.KNOWN_STOCKS.get(name, "")

    async def extract_stocks_from_news(
        self,
        news_titles: List[str],
        llm_manager=None
    ) -> List[ScreenedStock]:
        """
        뉴스에서 종목코드 추출 (LLM 사용)

        뉴스 제목을 분석하여 관련 종목을 추출합니다.
        """
        if not news_titles or not llm_manager:
            return []

        stocks = []
        try:
            # 뉴스 제목 모음
            titles_text = "\n".join([f"- {t}" for t in news_titles[:20]])

            known_stocks_hint = await self._get_stock_hints_for_llm()

            prompt = f"""다음 한국 주식시장 뉴스 제목들을 분석하여 관련 종목을 추출해주세요.

{titles_text}

참고 - 주요 종목코드:
{known_stocks_hint}

다음 JSON 형식으로 응답하세요:
{{
  "stocks": [
    {{
      "name": "종목명",
      "symbol": "6자리 종목코드 (모르면 빈 문자열)",
      "reason": "뉴스 연관 이유 (20자 이내)"
    }}
  ]
}}

규칙:
1. 뉴스에 직접 언급되거나 강하게 연관된 종목만 추출
2. 종목명은 반드시 포함 (종목코드는 위 목록에 있으면 기재, 없으면 빈 문자열)
3. 최대 10개 종목만 추출
4. 불확실한 종목은 제외"""

            from ...utils.llm import LLMTask
            result = await llm_manager.complete_json(prompt, task=LLMTask.THEME_DETECTION)

            if "error" in result:
                logger.error(f"뉴스 종목 추출 LLM 오류: {result.get('error')}")
                return stocks

            for item in result.get("stocks", []):
                name = str(item.get("name", "")).strip()
                symbol = str(item.get("symbol", "")).strip()
                reason = item.get("reason", "뉴스 언급")

                # 종목명으로 코드 변환 시도
                if not symbol or not symbol.isdigit() or len(symbol) != 6:
                    resolved = await self._resolve_stock_name(name)
                    if resolved:
                        symbol = resolved
                        logger.debug(f"[Screener] 종목명→코드 변환: {name} → {symbol}")
                    else:
                        logger.debug(f"[Screener] 종목코드 미확인, 스킵: {name}")
                        continue

                symbol = symbol.zfill(6)

                # 유효성 검사
                if len(symbol) != 6 or not symbol.isdigit():
                    continue

                stocks.append(ScreenedStock(
                    symbol=symbol,
                    name=name,
                    score=75,  # 뉴스 기반 기본 점수
                    reasons=[f"뉴스: {reason}"],
                ))

            logger.info(f"[Screener] 뉴스 기반 종목 {len(stocks)}개 추출")
            return stocks

        except Exception as e:
            logger.error(f"뉴스 종목 추출 오류: {e}")
            return stocks

    # ============================================================
    # 통합 스크리닝
    # ============================================================

    async def screen_all(
        self,
        llm_manager=None,
        news_titles: List[str] = None,
        use_naver: bool = True,
        min_price: float = 0,
        theme_detector=None,
    ) -> List[ScreenedStock]:
        """
        모든 스크리닝 실행 및 통합

        여러 스크리닝 결과를 병합하여 최종 후보 종목 리스트 반환

        Args:
            llm_manager: LLM 매니저 (뉴스 종목 추출용)
            news_titles: 뉴스 제목 리스트
            use_naver: 네이버 금융 크롤링 사용 여부 (기본 True)
            min_price: 최소 가격 필터
            theme_detector: ThemeDetector 인스턴스 (뉴스 호재 종목 추가용)
        """
        all_stocks: Dict[str, ScreenedStock] = {}
        # 소스 카운트 추적 (정규화용)
        source_counts: Dict[str, int] = {}

        def merge_stock(stock: ScreenedStock, weight: float = 1.0):
            """
            종목 병합 헬퍼 (소스 카운트 기반 정규화)

            여러 스크리닝에서 나타난 종목은 신뢰도가 높으므로
            소스 수를 추적하여 최종 점수 정규화 시 반영합니다.
            """
            if stock.symbol not in all_stocks:
                all_stocks[stock.symbol] = stock
                source_counts[stock.symbol] = 1
            else:
                # 가중치 적용한 점수 누적
                all_stocks[stock.symbol].score += stock.score * weight
                all_stocks[stock.symbol].reasons.extend(stock.reasons)
                source_counts[stock.symbol] += 1

        # ============================================================
        # 1. KIS API 스크리닝 (병렬 호출)
        # ============================================================
        kis_results = await asyncio.gather(
            self.screen_volume_surge(limit=20),
            self.screen_top_gainers(limit=20),
            self.screen_new_highs(limit=15),
            self.screen_fluctuation_rank(limit=20),
            self.screen_foreign_buying(limit=20),
            return_exceptions=True,
        )

        kis_weights = [0.5, 0.3, 0.4, 0.3, 0.4]
        kis_success = False
        for res, weight in zip(kis_results, kis_weights):
            if isinstance(res, Exception):
                logger.error(f"KIS 스크리닝 예외: {res}")
                continue
            if res:
                kis_success = True
                for stock in res:
                    merge_stock(stock, weight)

        # ============================================================
        # 2. 네이버 금융 크롤링 (병렬 호출, KIS 실패 시 주력)
        # ============================================================
        if use_naver:
            naver_weight = 1.0 if not kis_success else 0.3

            if not kis_success:
                logger.info("[Screener] KIS API 실패, 네이버 금융으로 대체")

            # naver_volume + naver_rise 병렬 (naver_new_high는 naver_rise 캐시 의존)
            naver_vr = await asyncio.gather(
                self.naver_volume_rank(limit=20),
                self.naver_rise_rank(limit=20),
                return_exceptions=True,
            )

            naver_vr_weights = [0.4, 0.3]
            for res, w in zip(naver_vr, naver_vr_weights):
                if isinstance(res, Exception):
                    logger.error(f"네이버 스크리닝 예외: {res}")
                    continue
                for stock in res:
                    merge_stock(stock, w * naver_weight)

            # 신고가 후보 (naver_rise 캐시 활용)
            try:
                naver_high = await self.naver_new_high(limit=15)
                for stock in naver_high:
                    merge_stock(stock, 0.4 * naver_weight)
            except Exception as e:
                logger.error(f"네이버 신고가 스크리닝 예외: {e}")

        # ============================================================
        # 3. 뉴스 기반 (선택적)
        # ============================================================
        # theme_detector가 있으면 호재 종목을 직접 추가 (LLM 호출 스킵으로 중복 제거)
        if theme_detector:
            try:
                sentiments = theme_detector.get_all_stock_sentiments()
                news_added = 0
                for symbol, data in sentiments.items():
                    # impact: -10 ~ +10 스케일 (방향 + 강도 통합)
                    impact = data.get("impact", 0)
                    if data.get("direction") == "bullish" and impact >= 5:
                        reason = data.get("reason", "뉴스 호재")
                        score_bonus = min(impact * 8, 80)  # 10→80
                        # 종목명 역매핑 (O(1))
                        stock_name = self._code_to_name.get(symbol, "")
                        stock = ScreenedStock(
                            symbol=symbol,
                            name=stock_name,
                            score=score_bonus,
                            reasons=[f"뉴스 호재: {reason}"],
                        )
                        merge_stock(stock, 0.6)
                        # 뉴스 보너스
                        if symbol in all_stocks:
                            all_stocks[symbol].score += 15
                        news_added += 1
                if news_added:
                    logger.info(f"[Screener] 뉴스 호재 종목 {news_added}개 추가 (theme_detector)")
            except Exception as e:
                logger.warning(f"[Screener] theme_detector 연동 오류: {e}")
        elif llm_manager and news_titles:
            news_stocks = await self.extract_stocks_from_news(news_titles, llm_manager)
            for stock in news_stocks:
                merge_stock(stock, 0.5)
                # 뉴스 보너스
                if stock.symbol in all_stocks:
                    all_stocks[stock.symbol].score += 15

        # ============================================================
        # 4. 밸류에이션 보너스 (저PER/저PBR 종목 가점)
        # ============================================================
        await self._apply_valuation_bonus(all_stocks)

        # ============================================================
        # 5. 결과 정리
        # ============================================================
        result = list(all_stocks.values())

        # ETF/ETN/파생상품 제거 → 단일 종목만 추천
        before_cnt = len(result)
        result = [s for s in result if not self._is_etf_etn(s.name)]
        filtered_cnt = before_cnt - len(result)
        if filtered_cnt:
            logger.info(f"[Screener] ETF/ETN {filtered_cnt}개 제외")

        # 최소 가격 필터 (소형주/저가주 제외)
        if min_price > 0:
            before_price = len(result)
            result = [s for s in result if s.price >= min_price]
            price_filtered = before_price - len(result)
            if price_filtered:
                logger.info(f"[Screener] {min_price:,.0f}원 미만 {price_filtered}개 제외")

        # ============================================================
        # 점수 정규화 (소스 수 기반)
        # ============================================================
        if result:
            # 1. 소스 수 기반 신뢰도 보너스 적용 (최대 +20점)
            for stock in result:
                source_cnt = source_counts.get(stock.symbol, 1)
                if source_cnt >= 3:
                    bonus = 20  # 3개 이상 소스
                elif source_cnt == 2:
                    bonus = 10  # 2개 소스
                else:
                    bonus = 0   # 1개 소스
                stock.score += bonus

            # 2. 0-100 범위로 정규화
            scores = [s.score for s in result]
            min_score = min(scores)
            max_score = max(scores)

            if max_score > min_score:
                for stock in result:
                    # 정규화: (score - min) / (max - min) * 100
                    normalized = (stock.score - min_score) / (max_score - min_score) * 100
                    stock.score = normalized
                logger.debug(
                    f"[Screener] 점수 정규화 완료: {min_score:.1f}~{max_score:.1f} → 0~100"
                )

        result.sort(key=lambda x: x.score, reverse=True)

        # 중복 reason 제거
        for stock in result:
            stock.reasons = list(dict.fromkeys(stock.reasons))

        source = "KIS+Naver" if kis_success and use_naver else ("Naver" if use_naver else "KIS")
        logger.info(f"[Screener] 통합 스크리닝 완료: {len(result)}개 종목 (소스: {source})")

        # 결과가 있으면 캐시 저장
        if result:
            self._update_cache("screen_all", result)
        elif not result and self._is_cache_valid("screen_all"):
            # 결과가 없으면 이전 캐시 활용
            cached = self._cache.get("screen_all", [])
            if cached:
                logger.info(f"[Screener] 스크리닝 결과 0건 → 이전 캐시 {len(cached)}건 활용")
                return cached

        return result

    # ============================================================
    # 캐시 관리
    # ============================================================

    def _is_cache_valid(self, key: str) -> bool:
        """캐시 유효성 검사"""
        if key not in self._cache or key not in self._cache_time:
            return False
        elapsed = (datetime.now() - self._cache_time[key]).total_seconds()
        return elapsed < self._cache_ttl

    def _update_cache(self, key: str, data: List[ScreenedStock]):
        """캐시 업데이트"""
        self._cache[key] = data
        self._cache_time[key] = datetime.now()

    def clear_cache(self):
        """캐시 초기화"""
        self._cache.clear()
        self._cache_time.clear()

    async def close(self):
        """리소스 정리"""
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================
# 전역 인스턴스
# ============================================================

_screener: Optional[StockScreener] = None


def get_screener() -> StockScreener:
    """전역 스크리너 인스턴스"""
    global _screener
    if _screener is None:
        _screener = StockScreener()
    return _screener
