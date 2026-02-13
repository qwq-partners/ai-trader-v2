"""
DART 공시 기반 종목 검증기

진입 전 최근 7일 공시를 확인하여:
- 위험 공시 차단 (유상증자, 전환사채, 감사의견거절 등)
- 긍정 공시 가점 (자기주식취득, 대규모수주 등)
"""

from __future__ import annotations

import io
import os
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from loguru import logger


@dataclass
class DartCheckResult:
    """DART 공시 검증 결과"""
    has_risk: bool = False
    risk_disclosures: List[str] = field(default_factory=list)
    positive_disclosures: List[str] = field(default_factory=list)
    risk_level: str = "none"  # "none" / "warning" / "block"
    confidence_adjustment: float = 0.0


BLOCK_KEYWORDS = [
    "유상증자", "전환사채", "감사의견거절", "관리종목", "감자",
    "부도", "회생절차", "상장폐지", "투자주의환기",
    "횡령", "배임", "소송",
]

WARNING_KEYWORDS = [
    "전환권행사", "신주인수권행사", "주식관련사채",
    "최대주주변경", "임원퇴임",
]

POSITIVE_KEYWORDS = [
    "자기주식취득", "대규모수주", "실적호전",
    "자사주매입", "배당결정", "신규시설투자",
]


class DartChecker:
    """DART OpenAPI를 활용한 공시 검증기"""

    DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
    DART_CORPCODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
    CACHE_DIR = Path.home() / ".cache" / "ai_trader"
    CORPCODE_CACHE = CACHE_DIR / "dart_corp_code.xml"

    def __init__(self):
        self._api_key = os.getenv("DART_API_KEY", "")
        self._enabled = bool(self._api_key)
        # stock_code(6자리) → corp_code(8자리)
        self._corp_code_map: Dict[str, str] = {}
        # 캐시: symbol → (result, timestamp)
        self._cache: Dict[str, Tuple[DartCheckResult, datetime]] = {}
        self._cache_ttl = timedelta(minutes=30)

        if not self._enabled:
            logger.warning("[DART] DART_API_KEY 미설정 → 공시 검증 비활성화")

    def _clean_cache(self):
        """만료된 캐시 항목 정리"""
        now = datetime.now()
        expired = [k for k, (_, ts) in self._cache.items() if now - ts > self._cache_ttl]
        for k in expired:
            del self._cache[k]

    async def ensure_corp_code_map(self):
        """corp_code 매핑 구축 (로컬 캐시 → API 다운로드)"""
        if not self._enabled:
            return

        if self._corp_code_map:
            return

        # 로컬 캐시 확인 (24시간 TTL)
        if self.CORPCODE_CACHE.exists():
            cache_age = datetime.now() - datetime.fromtimestamp(self.CORPCODE_CACHE.stat().st_mtime)
            if cache_age < timedelta(hours=24):
                try:
                    self._parse_corp_code_xml(self.CORPCODE_CACHE.read_bytes())
                    logger.info(f"[DART] corp_code 매핑 로드 (캐시): {len(self._corp_code_map)}개")
                    return
                except Exception as e:
                    logger.warning(f"[DART] 캐시 파싱 실패: {e}")

        # API 다운로드
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.DART_CORPCODE_URL,
                    params={"crtfc_key": self._api_key},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[DART] corpCode 다운로드 실패: {resp.status}")
                        return

                    zip_data = await resp.read()

            # ZIP 해제 → XML 파싱
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                xml_filename = zf.namelist()[0]
                xml_data = zf.read(xml_filename)

            # 로컬 캐시 저장
            self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self.CORPCODE_CACHE.write_bytes(xml_data)

            self._parse_corp_code_xml(xml_data)
            logger.info(f"[DART] corp_code 매핑 다운로드 완료: {len(self._corp_code_map)}개")

        except Exception as e:
            logger.warning(f"[DART] corpCode 다운로드 오류: {e}")

    def _parse_corp_code_xml(self, xml_data: bytes):
        """XML에서 stock_code → corp_code 매핑 추출"""
        root = ET.fromstring(xml_data)
        mapping = {}
        for corp in root.iter("list"):
            stock_code = (corp.findtext("stock_code") or "").strip()
            corp_code = (corp.findtext("corp_code") or "").strip()
            if stock_code and corp_code:
                mapping[stock_code] = corp_code
        self._corp_code_map = mapping

    async def check_disclosures(self, symbol: str, days: int = 7) -> DartCheckResult:
        """최근 공시 확인"""
        if not self._enabled or not self._corp_code_map:
            return DartCheckResult()

        # 캐시 확인
        now = datetime.now()
        if symbol in self._cache:
            cached_result, cached_time = self._cache[symbol]
            if now - cached_time < self._cache_ttl:
                return cached_result

        # 주기적 캐시 정리
        if len(self._cache) > 200:
            self._clean_cache()

        # symbol에서 6자리 종목코드 추출 (예: "005930" 또는 "A005930")
        stock_code = symbol.replace("A", "").strip()
        corp_code = self._corp_code_map.get(stock_code)
        if not corp_code:
            # 매핑 없음 → 통과
            return DartCheckResult()

        result = await self._fetch_and_analyze(corp_code, days)
        self._cache[symbol] = (result, now)
        return result

    async def _fetch_and_analyze(self, corp_code: str, days: int) -> DartCheckResult:
        """DART API 호출 및 분석"""
        end_de = datetime.now().strftime("%Y%m%d")
        bgn_de = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

        params = {
            "crtfc_key": self._api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_count": 20,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.DART_LIST_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"[DART] API 응답 {resp.status}: corp={corp_code}")
                        return DartCheckResult()

                    data = await resp.json()
        except Exception as e:
            logger.debug(f"[DART] API 오류 (corp={corp_code}): {e}")
            return DartCheckResult()

        # status "000" = 정상, "013" = 조회 결과 없음
        status = data.get("status", "")
        if status == "013" or status != "000":
            return DartCheckResult()

        disclosures = data.get("list", [])
        if not disclosures:
            return DartCheckResult()

        risk_list = []
        positive_list = []
        has_block = False

        for disc in disclosures:
            report_nm = disc.get("report_nm", "")

            for kw in BLOCK_KEYWORDS:
                if kw in report_nm:
                    risk_list.append(report_nm)
                    has_block = True
                    break

            for kw in WARNING_KEYWORDS:
                if kw in report_nm:
                    risk_list.append(report_nm)
                    break

            for kw in POSITIVE_KEYWORDS:
                if kw in report_nm:
                    positive_list.append(report_nm)
                    break

        # 결과 결정
        if has_block:
            return DartCheckResult(
                has_risk=True,
                risk_disclosures=risk_list,
                positive_disclosures=positive_list,
                risk_level="block",
                confidence_adjustment=-1.0,  # 특수값: 진입 차단
            )
        elif risk_list:
            return DartCheckResult(
                has_risk=True,
                risk_disclosures=risk_list,
                positive_disclosures=positive_list,
                risk_level="warning",
                confidence_adjustment=-0.10,
            )
        elif positive_list:
            return DartCheckResult(
                has_risk=False,
                risk_disclosures=[],
                positive_disclosures=positive_list,
                risk_level="none",
                confidence_adjustment=0.10,
            )
        else:
            return DartCheckResult()
