"""
AI Trading Bot v2 - KIS 시장 데이터 조회 (조회 전용 REST API)

KIS 공식 API 5종을 모아서 관리합니다:
1. 휴장일 조회 (CTCA0903R)
2. 업종지수 조회 (FHPUP02140000)
3. 등락률 순위 (FHPST01700000)
4. 외국인/기관 매매 (FHPTJ04400000)
5. 시가총액/밸류에이션 순위 (FHPST01790000)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Set

import aiohttp
from loguru import logger

from ...utils.kis_token_manager import get_token_manager


class KISMarketData:
    """KIS 시장 데이터 조회 (조회 전용 REST API)"""

    def __init__(self, token_manager=None):
        self._token_manager = token_manager or get_token_manager()
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Dict[str, datetime] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _get_headers(self, tr_id: str) -> Dict[str, str]:
        token = await self._token_manager.get_access_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._token_manager.app_key,
            "appsecret": self._token_manager.app_secret,
            "tr_id": tr_id,
        }

    def _is_cache_valid(self, key: str, ttl_seconds: int) -> bool:
        if key not in self._cache or key not in self._cache_ts:
            return False
        elapsed = (datetime.now() - self._cache_ts[key]).total_seconds()
        return elapsed < ttl_seconds

    def _set_cache(self, key: str, value: Any):
        self._cache[key] = value
        self._cache_ts[key] = datetime.now()

    # ============================================================
    # 1. 휴장일 조회 (CTCA0903R)
    # ============================================================

    async def fetch_holidays(self, year_month: str = "") -> Set[date]:
        """
        한국 시장 휴장일 조회

        Args:
            year_month: YYYYMM 형식 (비어 있으면 당월)

        Returns:
            휴장일 set (date 객체)

        API 주의: 1일 1회 호출 권장
        """
        if not year_month:
            year_month = datetime.now().strftime("%Y%m")

        cache_key = f"holidays_{year_month}"
        if self._is_cache_valid(cache_key, 86400):  # 1일 캐시
            return self._cache[cache_key]

        holidays: Set[date] = set()

        try:
            session = await self._get_session()
            headers = await self._get_headers("CTCA0903R")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/chk-holiday"

            params = {
                "BASS_DT": f"{year_month}01",
                "CTX_AREA_NK": "",
                "CTX_AREA_FK": "",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"휴장일 조회 실패: HTTP {resp.status}")
                    return holidays

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"휴장일 API 오류: {data.get('msg1')}")
                    return holidays

                output = data.get("output", [])

                for item in output:
                    bass_dt = item.get("bass_dt", "")
                    opnd_yn = item.get("opnd_yn", "")

                    if bass_dt and opnd_yn == "N":
                        try:
                            d = datetime.strptime(bass_dt, "%Y%m%d").date()
                            holidays.add(d)
                        except ValueError:
                            continue

            self._set_cache(cache_key, holidays)
            logger.info(f"[KISMarketData] 휴장일 조회 완료: {year_month} → {len(holidays)}일")
            return holidays

        except Exception as e:
            logger.error(f"휴장일 조회 오류: {e}")
            return holidays

    # ============================================================
    # 2. 업종지수 조회 (FHPUP02140000)
    # ============================================================

    async def fetch_sector_indices(self, market: str = "K") -> List[Dict]:
        """
        업종별 지수 조회

        Args:
            market: K=코스피, Q=코스닥

        Returns:
            [{업종명, 지수, 등락률, 거래량, 상승종목수, 하락종목수, ...}]
        """
        cache_key = f"sector_indices_{market}"
        if self._is_cache_valid(cache_key, 600):  # 10분 캐시
            return self._cache[cache_key]

        result: List[Dict] = []

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPUP02140000")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/inquire-index-category-price"

            params = {
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": "0001",
                "FID_COND_SCR_DIV_CODE": "20214",
                "FID_MRKT_CLS_CODE": market,
                "FID_BLNG_CLS_CODE": "0",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"업종지수 조회 실패: HTTP {resp.status}")
                    return result

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"업종지수 API 오류: {data.get('msg1')}")
                    return result

                # 업종지수 응답은 output2에 담김 (output1은 헤더 정보)
                output = data.get("output2", []) or data.get("output", [])

                for item in output:
                    sector_name = item.get("hts_kor_isnm", "").strip()
                    if not sector_name:
                        continue

                    result.append({
                        "name": sector_name,
                        "index": float(item.get("bstp_nmix_prpr", 0) or 0),
                        "change": float(item.get("bstp_nmix_prdy_vrss", 0) or 0),
                        "change_pct": float(item.get("bstp_nmix_prdy_ctrt", 0) or 0),
                        "volume": int(item.get("acml_vol", 0) or 0),
                        "trade_value": int(item.get("acml_tr_pbmn", 0) or 0),
                    })

            self._set_cache(cache_key, result)
            logger.info(f"[KISMarketData] 업종지수 조회 완료: {market} → {len(result)}개 업종")
            return result

        except Exception as e:
            logger.error(f"업종지수 조회 오류: {e}")
            return result

    # ============================================================
    # 3. 등락률 순위 (FHPST01700000)
    # ============================================================

    async def fetch_fluctuation_rank(self, limit: int = 30) -> List[Dict]:
        """
        등락률 순위 조회

        Returns:
            [{종목코드, 종목명, 현재가, 등락률, 거래량, ...}]
        """
        cache_key = "fluctuation_rank"
        if self._is_cache_valid(cache_key, 300):  # 5분 캐시
            return self._cache[cache_key][:limit]

        result: List[Dict] = []

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPST01700000")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/ranking/fluctuation"

            params = {
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20170",
                "fid_input_iscd": "0000",
                "fid_rank_sort_cls_code": "0",  # 상승률순
                "fid_input_cnt_1": "0",
                "fid_prc_cls_code": "0",
                "fid_input_price_1": "",
                "fid_input_price_2": "",
                "fid_vol_cnt": "",
                "fid_trgt_cls_code": "0",
                "fid_trgt_exls_cls_code": "0",
                "fid_div_cls_code": "0",
                "fid_rsfl_rate1": "",
                "fid_rsfl_rate2": "",
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"등락률 순위 조회 실패: HTTP {resp.status}")
                    return result

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"등락률 순위 API 오류: {data.get('msg1')}")
                    return result

                output = data.get("output", [])

                for item in output:
                    symbol = item.get("stck_shrn_iscd", "").strip()
                    name = item.get("hts_kor_isnm", "").strip()
                    if not symbol or not name:
                        continue

                    # 비정상 symbol 필터 (숫자 6자리만 허용)
                    if not symbol.isdigit() or len(symbol) != 6:
                        continue

                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "price": float(item.get("stck_prpr", 0) or 0),
                        "change_pct": float(item.get("prdy_ctrt", 0) or 0),
                        "volume": int(item.get("acml_vol", 0) or 0),
                        "trade_value": int(item.get("acml_tr_pbmn", 0) or 0),
                        "change": float(item.get("prdy_vrss", 0) or 0),
                    })

            self._set_cache(cache_key, result)
            logger.info(f"[KISMarketData] 등락률 순위 조회 완료: {len(result)}개 종목")
            return result[:limit]

        except Exception as e:
            logger.error(f"등락률 순위 조회 오류: {e}")
            return result

    # ============================================================
    # 4. 외국인/기관 매매 동향 (FHPTJ04400000)
    # ============================================================

    async def fetch_foreign_institution(
        self,
        market: str = "0001",
        investor: str = "1",
    ) -> List[Dict]:
        """
        외국인/기관 순매수 상위 종목 조회

        Args:
            market: 0001=코스피, 0002=코스닥
            investor: 1=외국인, 2=기관

        Returns:
            [{종목코드, 종목명, 순매수수량, 순매수금액, 현재가, 등락률, ...}]
        """
        investor_name = "외국인" if investor == "1" else "기관"
        cache_key = f"foreign_inst_{market}_{investor}"
        if self._is_cache_valid(cache_key, 600):  # 10분 캐시
            return self._cache[cache_key]

        result: List[Dict] = []

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHPTJ04400000")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/foreign-institution-total"

            params = {
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": market,
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_ETC_CLS_CODE": investor,
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"{investor_name} 매매동향 조회 실패: HTTP {resp.status}")
                    return result

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    logger.warning(f"{investor_name} 매매동향 API 오류: {data.get('msg1')}")
                    return result

                output = data.get("output", [])

                for item in output:
                    symbol = item.get("mksc_shrn_iscd", "").zfill(6)
                    name = item.get("hts_kor_isnm", "").strip()
                    if not symbol or not name:
                        continue

                    net_qty = int(item.get("ntby_qty", 0) or 0)

                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "net_buy_qty": net_qty,
                        "net_buy_amt": int(item.get("ntby_tr_pbmn", 0) or 0),
                        "price": float(item.get("stck_prpr", 0) or 0),
                        "change_pct": float(item.get("prdy_ctrt", 0) or 0),
                        "volume": int(item.get("acml_vol", 0) or 0),
                        "investor": investor_name,
                    })

            self._set_cache(cache_key, result)
            logger.info(
                f"[KISMarketData] {investor_name} 매매동향 조회 완료: {len(result)}개 종목"
            )
            return result

        except Exception as e:
            logger.error(f"{investor_name} 매매동향 조회 오류: {e}")
            return result

    # ============================================================
    # 5. 개별 종목 PER/PBR 조회 (FHKST01010100)
    # ============================================================

    async def fetch_stock_valuation(self, symbol: str) -> Optional[Dict]:
        """
        개별 종목 밸류에이션(PER/PBR/EPS/BPS) 조회

        Args:
            symbol: 종목코드 (6자리)

        Returns:
            {symbol, per, pbr, eps, bps, price, change_pct} 또는 None
        """
        cache_key = f"valuation_{symbol}"
        if self._is_cache_valid(cache_key, 1800):  # 30분 캐시
            return self._cache[cache_key]

        try:
            session = await self._get_session()
            headers = await self._get_headers("FHKST01010100")

            url = f"{self._token_manager.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"

            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            }

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()

                if data.get("rt_cd") != "0":
                    return None

                output = data.get("output", {})
                if not output:
                    return None

                result = {
                    "symbol": symbol,
                    "per": float(output.get("per", 0) or 0),
                    "pbr": float(output.get("pbr", 0) or 0),
                    "eps": float(output.get("eps", 0) or 0),
                    "bps": float(output.get("bps", 0) or 0),
                    "price": float(output.get("stck_prpr", 0) or 0),
                    "change_pct": float(output.get("prdy_ctrt", 0) or 0),
                }

                self._set_cache(cache_key, result)
                return result

        except Exception as e:
            logger.debug(f"종목 밸류에이션 조회 오류 ({symbol}): {e}")
            return None

    async def fetch_batch_valuations(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        복수 종목 PER/PBR 일괄 조회 (캐시 우선, 미캐시 건 병렬 API 호출)

        Args:
            symbols: 종목코드 리스트

        Returns:
            {symbol: {per, pbr, eps, bps, ...}} 딕셔너리
        """
        result: Dict[str, Dict] = {}
        to_fetch: List[str] = []

        # 캐시에 있는 건 즉시 반환
        cached_count = 0
        for sym in symbols:
            cache_key = f"valuation_{sym}"
            if self._is_cache_valid(cache_key, 1800):
                result[sym] = self._cache[cache_key]
                cached_count += 1
            else:
                to_fetch.append(sym)

        # 미캐시 건은 병렬 조회 (배치 단위로 API rate limit 준수)
        fetch_count = 0
        max_symbols = 30
        batch_size = 18  # RPS 18 한도 내 동시 호출

        # 최대 30건까지만 처리
        to_fetch = to_fetch[:max_symbols]

        # 배치 단위로 병렬 처리
        for i in range(0, len(to_fetch), batch_size):
            batch = to_fetch[i:i + batch_size]

            # 배치 내 종목들을 병렬로 조회
            tasks = [self.fetch_stock_valuation(sym) for sym in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 결과 수집
            for sym, val in zip(batch, batch_results):
                if isinstance(val, Exception):
                    logger.debug(f"[KISMarketData] {sym} 밸류에이션 조회 오류: {val}")
                    continue
                if val:
                    result[sym] = val
                    fetch_count += 1

            # 다음 배치 전 대기 (rate limit buffer)
            if i + batch_size < len(to_fetch):
                await asyncio.sleep(0.1)

        if to_fetch:
            logger.info(
                f"[KISMarketData] 밸류에이션 일괄 조회: 캐시 {cached_count}건 + "
                f"API {fetch_count}건 (병렬 처리)"
            )
        return result

    # ============================================================
    # 유틸리티
    # ============================================================

    def clear_cache(self):
        """캐시 초기화"""
        self._cache.clear()
        self._cache_ts.clear()

    async def close(self):
        """리소스 정리"""
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================
# 전역 인스턴스
# ============================================================

_kis_market_data: Optional[KISMarketData] = None


def get_kis_market_data() -> KISMarketData:
    """전역 KISMarketData 인스턴스"""
    global _kis_market_data
    if _kis_market_data is None:
        _kis_market_data = KISMarketData()
    return _kis_market_data
