"""
섹터 모멘텀 프로바이더 (Phase 3)

SEPA 스코어 5번 항목: 개별 종목 change_20d 대신 섹터 ETF 모멘텀으로 교체.

아키텍처:
  1. Stock → 섹터 매핑  : pykrx WICS 분류 (7일 캐시), 폴백 키워드 매핑
  2. 섹터 ETF 모멘텀    : KODEX ETF 20일 수익률 (30분 캐시, KIS API 사용)
  3. 스코어 변환       : 0~10pt (SEPA 점수 체계와 동일 스케일)

섹터 ETF 매핑 (KODEX 시리즈, 거래량 상위 상품 기준):
  반도체    → 091160 (KODEX 반도체)
  IT/전기전자 → 169060 (KODEX IT)
  자동차    → 091180 (KODEX 자동차)
  2차전지   → 305720 (KODEX 2차전지산업)
  바이오    → 244580 (KODEX 바이오)
  건설      → 102960 (KODEX 건설)
  조선      → 105370 (KODEX 조선)
  철강      → 069500 (KODEX 철강)
  은행/금융  → 091170 (KODEX 은행)
  화학/에너지 → 117460 (KODEX 에너지화학)
  방산      → 475330 (KODEX K-방산)
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger


# ── KODEX 섹터 ETF 매핑 (섹터명 → ETF 코드) ─────────────────────────────────
SECTOR_ETF_MAP: Dict[str, str] = {
    "반도체":     "091160",  # KODEX 반도체
    "IT":         "169060",  # KODEX IT
    "자동차":     "091180",  # KODEX 자동차
    "2차전지":    "305720",  # KODEX 2차전지산업
    "바이오":     "244580",  # KODEX 바이오
    "건설":       "102960",  # KODEX 건설
    "조선":       "105370",  # KODEX 조선
    "철강":       "069500",  # KODEX 철강
    "은행":       "091170",  # KODEX 은행
    "화학":       "117460",  # KODEX 에너지화학
    "방산":       "475330",  # KODEX K-방산
}

# ── 키워드 기반 폴백 매핑 (종목명 → 섹터명) ──────────────────────────────────
_KEYWORD_SECTOR: List[Tuple[List[str], str]] = [
    (["반도체", "하이닉스", "삼성전자", "마이크론", "텔레칩스", "HPSP", "솔브레인", "주성엔지니어링", "한미반도체"], "반도체"),
    (["배터리", "에너지솔루션", "SDI", "이노베이션", "LG화학", "포스코퓨처", "에코프로", "일진머티리얼", "엘앤에프"], "2차전지"),
    (["현대차", "기아", "모비스", "자동차", "GM", "부품"], "자동차"),
    (["셀트리온", "삼성바이오", "유한양행", "한미약품", "제약", "바이오", "메디", "헬스케어"], "바이오"),
    (["조선", "현대중공업", "한화오션", "삼성중공업", "HJ중공업"], "조선"),
    (["철강", "포스코", "현대제철", "동국제강"], "철강"),
    (["은행", "하나금융", "KB금융", "신한지주", "우리금융", "NH금융", "카카오뱅크"], "은행"),
    (["화학", "롯데케미칼", "금호석유", "SK이노베이션", "에너지"], "화학"),
    (["한화", "방산", "항공우주", "한국항공", "LIG넥스", "현대로템"], "방산"),
    (["건설", "현대건설", "GS건설", "대우건설", "HDC", "포스코건설"], "건설"),
]

# ── 캐시 경로 ─────────────────────────────────────────────────────────────────
_CACHE_DIR = Path.home() / ".cache" / "ai_trader"
_SECTOR_MAP_CACHE = _CACHE_DIR / "sector_map.json"     # stock → sector (7일)
_ETF_MOMENTUM_CACHE = _CACHE_DIR / "etf_momentum.json" # ETF 수익률 (30분)

# ── TTL 상수 ──────────────────────────────────────────────────────────────────
_SECTOR_MAP_TTL = 7 * 24 * 3600   # 7일 (pykrx WICS)
_ETF_MOMENTUM_TTL = 30 * 60        # 30분 (ETF 가격)


def _load_json_cache(path: Path, ttl: int) -> Optional[dict]:
    """JSON 캐시 로드. TTL 만료 시 None 반환."""
    try:
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        if time.time() - mtime > ttl:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_json_cache(path: Path, data: dict) -> None:
    """JSON 캐시 저장."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug(f"[SectorMomentum] 캐시 저장 실패: {e}")


def _keyword_sector(stock_name: str) -> Optional[str]:
    """종목명 키워드 기반 섹터 분류 (폴백용)."""
    for keywords, sector in _KEYWORD_SECTOR:
        if any(kw in stock_name for kw in keywords):
            return sector
    return None


class SectorMomentumProvider:
    """
    섹터 모멘텀 프로바이더

    사용법:
        provider = SectorMomentumProvider(broker=kis_broker)
        score = await provider.get_sepa_score(symbol="005930", name="삼성전자")
        # → 0~10 점 (SEPA 항목 5)
    """

    def __init__(self, broker=None):
        self._broker = broker
        self._sector_map: Dict[str, str] = {}     # ticker → sector_name
        self._etf_momentum: Dict[str, float] = {} # sector_name → 20d_pct
        self._etf_last_fetch: float = 0.0
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_sepa_score(self, symbol: str, name: str = "") -> float:
        """
        SEPA 섹터 모멘텀 점수 (0~10pt).

        섹터 ETF 20일 수익률:
          +15% 이상 → 10점
          +10% 이상 → 7점
          +5%  이상 → 4점
          0%   이상 → 2점
          음수         → 0점
          데이터 없음   → 3점 (중립, 페널티 없음)
        """
        sector = await self._get_sector(symbol, name)
        if not sector:
            return 3.0  # 섹터 미분류 → 중립

        momentum = await self._get_etf_momentum(sector)
        return self._momentum_to_score(momentum)

    async def get_sector(self, symbol: str, name: str = "") -> Optional[str]:
        """종목의 섹터명 반환 (외부 호출용)."""
        return await self._get_sector(symbol, name)

    async def get_all_sector_momentum(self) -> Dict[str, float]:
        """전체 섹터 ETF 모멘텀 맵 반환 (섹터명 → 20d 수익률%)."""
        await self._refresh_etf_momentum()
        return dict(self._etf_momentum)

    async def get_sector_map_batch(self, symbols: List[str]) -> Dict[str, str]:
        """
        복수 종목의 섹터 매핑 일괄 반환 (ticker → sector_name).

        3계층 폴백 활용: 인메모리 캐시 → pykrx WICS(7일 캐시) → 키워드 폴백.
        stock_screener 등 외부에서 섹터 다양성 체크에 사용.
        """
        result: Dict[str, str] = {}
        missing: List[str] = []

        # 1. 인메모리 캐시에서 즉시 반환
        for s in symbols:
            if s in self._sector_map:
                result[s] = self._sector_map[s]
            else:
                missing.append(s)

        if not missing:
            return result

        # 2. 파일 캐시 확인
        cached = _load_json_cache(_SECTOR_MAP_CACHE, _SECTOR_MAP_TTL)
        still_missing: List[str] = []
        for s in missing:
            if cached and s in cached:
                result[s] = cached[s]
                self._sector_map[s] = cached[s]
            else:
                still_missing.append(s)

        if not still_missing:
            return result

        # 3. pykrx WICS 조회 (전체 시장 매핑)
        pykrx_map = await self._fetch_pykrx_sector_map()
        if pykrx_map:
            self._sector_map.update(pykrx_map)
            _save_json_cache(_SECTOR_MAP_CACHE, {**(cached or {}), **pykrx_map})
            for s in still_missing:
                if s in pykrx_map:
                    result[s] = pykrx_map[s]

        logger.debug(f"[SectorMomentum] 배치 섹터 매핑: 요청 {len(symbols)}개 → 매핑 {len(result)}개")
        return result

    # ── 내부 구현 ─────────────────────────────────────────────────────────────

    async def _get_sector(self, symbol: str, name: str = "") -> Optional[str]:
        """종목 → 섹터명 (캐시 → pykrx → 키워드 폴백 순)."""
        # 1. 인메모리 캐시
        if symbol in self._sector_map:
            return self._sector_map[symbol]

        # 2. 파일 캐시
        cached = _load_json_cache(_SECTOR_MAP_CACHE, _SECTOR_MAP_TTL)
        if cached and symbol in cached:
            sector = cached[symbol]
            self._sector_map[symbol] = sector
            return sector

        # 3. pykrx WICS 조회 (영업일에만 동작)
        pykrx_map = await self._fetch_pykrx_sector_map()
        if pykrx_map:
            if symbol in pykrx_map:
                self._sector_map.update(pykrx_map)
                _save_json_cache(_SECTOR_MAP_CACHE, {**( cached or {}), **pykrx_map})
                return pykrx_map.get(symbol)

        # 4. 키워드 매핑 폴백
        if name:
            sector = _keyword_sector(name)
            if sector:
                self._sector_map[symbol] = sector
                # 키워드 매핑 결과는 영속화 (별도 캐시에 누적)
                combined = {**(cached or {}), **self._sector_map}
                _save_json_cache(_SECTOR_MAP_CACHE, combined)
                return sector

        return None

    async def _fetch_pykrx_sector_map(self) -> Dict[str, str]:
        """pykrx WICS 업종 분류 조회 (ticker → 한글 섹터명)."""
        try:
            from pykrx import stock as pykrx_stock

            today = datetime.now()
            # 영업일 조정: 오늘이 주말이면 가장 최근 금요일로
            if today.weekday() >= 5:
                days_back = today.weekday() - 4  # 5=토(-1), 6=일(-2)
                today = today - timedelta(days=days_back)
            date_str = today.strftime("%Y%m%d")

            sector_map: Dict[str, str] = {}

            async def _fetch(market: str):
                try:
                    df = await asyncio.to_thread(
                        pykrx_stock.get_market_sector_classifications, date_str, market=market
                    )
                    if df is None or df.empty:
                        return
                    # 컬럼: 업종명 확인
                    sector_col = None
                    for col in ["업종명", "업종", "종류"]:
                        if col in df.columns:
                            sector_col = col
                            break
                    if not sector_col:
                        return
                    for ticker, row in df.iterrows():
                        raw_sector = str(row[sector_col])
                        normalized = self._normalize_sector(raw_sector)
                        if normalized:
                            sector_map[str(ticker).zfill(6)] = normalized
                except Exception as e:
                    logger.debug(f"[SectorMomentum] pykrx {market} 조회 실패: {e}")

            await asyncio.gather(_fetch("KOSPI"), _fetch("KOSDAQ"), return_exceptions=True)
            if sector_map:
                logger.info(f"[SectorMomentum] pykrx WICS 조회 완료: {len(sector_map)}종목")
            return sector_map

        except Exception as e:
            logger.debug(f"[SectorMomentum] pykrx 로드 실패: {e}")
            return {}

    def _normalize_sector(self, raw: str) -> Optional[str]:
        """pykrx 업종명 → 내부 섹터명 정규화."""
        _MAP = {
            "반도체": "반도체",
            "IT하드웨어": "IT",
            "소프트웨어": "IT",
            "자동차": "자동차",
            "자동차부품": "자동차",
            "에너지장비": "2차전지",
            "이차전지": "2차전지",
            "제약": "바이오",
            "바이오": "바이오",
            "헬스케어": "바이오",
            "조선": "조선",
            "철강": "철강",
            "금속": "철강",
            "은행": "은행",
            "보험": "은행",
            "화학": "화학",
            "에너지": "화학",
            "건설": "건설",
            "방산": "방산",
            "항공우주": "방산",
        }
        for key, sector in _MAP.items():
            if key in raw:
                return sector
        return None

    async def _get_etf_momentum(self, sector: str) -> Optional[float]:
        """섹터 ETF 20일 수익률 (%). 캐시 30분."""
        await self._refresh_etf_momentum()
        return self._etf_momentum.get(sector)

    async def _refresh_etf_momentum(self) -> None:
        """ETF 모멘텀 갱신 (30분 캐시)."""
        now = time.time()
        if now - self._etf_last_fetch < _ETF_MOMENTUM_TTL:
            return  # 캐시 유효

        # 파일 캐시 시도
        cached = _load_json_cache(_ETF_MOMENTUM_CACHE, _ETF_MOMENTUM_TTL)
        if cached:
            self._etf_momentum = cached
            self._etf_last_fetch = now
            return

        # KIS API로 ETF 가격 조회
        if not self._broker:
            return

        results: Dict[str, float] = {}
        for sector, etf_ticker in SECTOR_ETF_MAP.items():
            try:
                momentum = await self._calc_etf_momentum(etf_ticker)
                if momentum is not None:
                    results[sector] = momentum
            except Exception as e:
                logger.debug(f"[SectorMomentum] {sector}({etf_ticker}) 실패: {e}")

        if results:
            self._etf_momentum = results
            self._etf_last_fetch = now
            _save_json_cache(_ETF_MOMENTUM_CACHE, results)
            logger.info(
                f"[SectorMomentum] ETF 모멘텀 갱신: {len(results)}개 섹터 | "
                + " ".join(f"{s}={v:+.1f}%" for s, v in sorted(results.items(), key=lambda x: -x[1])[:5])
            )

    async def _calc_etf_momentum(self, etf_ticker: str) -> Optional[float]:
        """ETF 20일 수익률 계산 (KIS get_daily_prices 사용)."""
        try:
            prices = await self._broker.get_daily_prices(etf_ticker, days=25)
            if not prices or len(prices) < 2:
                return None

            # 최신 종가
            latest = prices[0]
            current_price = float(latest.get("stck_clpr") or latest.get("close") or 0)
            if current_price <= 0:
                return None

            # 20일 전 종가 (인덱스 기준 최대 20번째)
            idx_20d = min(20, len(prices) - 1)
            old = prices[idx_20d]
            old_price = float(old.get("stck_clpr") or old.get("close") or 0)
            if old_price <= 0:
                return None

            return (current_price - old_price) / old_price * 100

        except Exception as e:
            logger.debug(f"[SectorMomentum] ETF {etf_ticker} 가격 조회 실패: {e}")
            return None

    @staticmethod
    def _momentum_to_score(momentum: Optional[float]) -> float:
        """
        20일 수익률 → SEPA 점수 (0~10pt).

        Piecewise linear interpolation — 앵커 포인트 간 선형 보간.
        앵커: (-5%, 0pt), (0%, 2pt), (5%, 4pt), (10%, 7pt), (15%, 10pt)
        경계값은 기존 이산 함수와 동일, 중간값만 부드러워짐.
        """
        if momentum is None:
            return 3.0  # 데이터 없음 → 중립

        # 앵커 포인트 (momentum_pct, score)
        anchors = [(-5.0, 0.0), (0.0, 2.0), (5.0, 4.0), (10.0, 7.0), (15.0, 10.0)]

        # 범위 밖 클램프
        if momentum <= anchors[0][0]:
            return anchors[0][1]
        if momentum >= anchors[-1][0]:
            return anchors[-1][1]

        # 구간 찾아 선형 보간
        for i in range(len(anchors) - 1):
            x0, y0 = anchors[i]
            x1, y1 = anchors[i + 1]
            if x0 <= momentum <= x1:
                t = (momentum - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)

        return 3.0  # fallback (도달 불가)
