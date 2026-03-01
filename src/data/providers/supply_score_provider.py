"""
AI Trading Bot v2 - 5일 누적 수급 스코어 제공자

pykrx market-wide 수급 데이터를 5 영업일 분 축적해 종목별 정규화 점수(0~100) 산출.
기존 SupplyTrendDetector(80종목 한정)를 보완해 KOSPI+KOSDAQ 전종목 커버.

점수 구성:
  외국인 연속 순매수 일수  → 최대 +30pt (5일↑=30 / 3일↑=20 / 1일↑=8)
  기관 연속 순매수 일수    → 최대 +25pt (5일↑=25 / 3일↑=15 / 1일↑=6)
  동시 매집 보너스        → +15pt (외국인+기관 3일↑ 동시)
  규모 보너스             → 최대 +15pt (5일 누적 주수 기준)
  가속 보너스             → +15pt (최근 2일 > 이전 3일)

캐시: ~/.cache/ai_trader/supply_daily_YYYYMMDD.json (일별 저장)
갱신: 15:40 장 마감 후 / 또는 08:15 아침 스캔 전 (전일 데이터)
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger


CACHE_DIR = Path.home() / ".cache" / "ai_trader"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOOKBACK_DAYS = 5       # 5 영업일


def _get_trading_dates(n: int) -> List[str]:
    """최근 n 영업일 날짜 목록 반환 (오늘 제외, 최신→오래된 순)"""
    dates = []
    d = datetime.now().date() - timedelta(days=1)
    while len(dates) < n:
        if d.weekday() < 5:  # 월~금
            dates.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return dates  # 최신→오래된 순


class SupplyScoreProvider:
    """
    5일 누적 외국인/기관 수급 스코어 제공자.

    사용 예:
        provider = SupplyScoreProvider()
        await provider.ensure_loaded()          # 데이터 확보
        score = provider.get_score("005930")    # 0~100점
        meta  = provider.get_meta("005930")     # 상세 정보
    """

    def __init__(self):
        # {date_str: {symbol: {"foreign": int, "inst": int}}}
        self._daily: Dict[str, Dict[str, Dict[str, int]]] = {}
        self._loaded_dates: List[str] = []   # 로드된 날짜 목록 (최신→오래된 순)
        self._score_cache: Dict[str, float] = {}    # 계산 결과 캐시
        self._meta_cache: Dict[str, dict] = {}
        self._ready = False

    # ── 캐시 IO ───────────────────────────────────────────────────────────────

    def _cache_path(self, date_str: str) -> Path:
        return CACHE_DIR / f"supply_daily_{date_str}.json"

    def _save_day(self, date_str: str, data: Dict[str, Dict[str, int]]):
        try:
            self._cache_path(date_str).write_text(
                json.dumps(data, ensure_ascii=False)
            )
        except Exception as e:
            logger.debug(f"[수급5일] 캐시 저장 실패 {date_str}: {e}")

    def _load_day(self, date_str: str) -> Optional[Dict[str, Dict[str, int]]]:
        path = self._cache_path(date_str)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return None

    @property
    def is_ready(self) -> bool:
        return self._ready and bool(self._daily)

    # ── 데이터 로드 / 갱신 ────────────────────────────────────────────────────

    async def ensure_loaded(self, force_refresh_today: bool = False) -> bool:
        """
        5일치 데이터 확보 (캐시 우선, 없으면 pykrx 호출).

        Args:
            force_refresh_today: True이면 오늘 날짜 데이터 강제 재조회
        Returns:
            True if data is ready
        """
        dates = _get_trading_dates(LOOKBACK_DAYS)
        # force_refresh_today: 가장 최신 날짜(오늘의 전일)를 재조회
        if force_refresh_today and dates:
            self._daily.pop(dates[0], None)  # 메모리에서 제거 → 재조회 유도

        for date_str in dates:
            if date_str in self._daily:
                continue  # 이미 로드됨

            # 캐시 파일 확인 (force_refresh_today=True이면 최신 날짜 캐시 무시)
            if force_refresh_today and date_str == dates[0]:
                pass  # 캐시 스킵, 바로 pykrx 조회
            else:
                cached = self._load_day(date_str)
                if cached:
                    self._daily[date_str] = cached
                    logger.debug(f"[수급5일] 캐시 로드: {date_str} {len(cached)}종목")
                    continue

            # pykrx 조회
            logger.info(f"[수급5일] pykrx 조회: {date_str}")
            try:
                day_data = await asyncio.to_thread(
                    self._fetch_pykrx_day, date_str
                )
                if day_data:
                    self._daily[date_str] = day_data
                    self._save_day(date_str, day_data)
                    logger.info(f"[수급5일] 로드 완료: {date_str} {len(day_data)}종목")
            except Exception as e:
                logger.warning(f"[수급5일] {date_str} 조회 실패: {e}")

        self._loaded_dates = [d for d in dates if d in self._daily]

        if self._loaded_dates:
            self._score_cache.clear()
            self._meta_cache.clear()
            self._ready = True
            logger.info(
                f"[수급5일] 준비 완료: {len(self._loaded_dates)}일치 "
                f"({self._loaded_dates[-1]}~{self._loaded_dates[0]})"
            )
        return self._ready

    @staticmethod
    def _fetch_pykrx_day(date_str: str) -> Dict[str, Dict[str, int]]:
        """
        pykrx로 특정 날짜의 KOSPI+KOSDAQ 전종목 외국인/기관 순매수 조회.
        동기 함수 — asyncio.to_thread()로 호출할 것.
        """
        import pykrx.stock as pykrx_stock

        result: Dict[str, Dict[str, int]] = {}
        configs = [
            ("KOSPI",  "외국인", "foreign"),
            ("KOSDAQ", "외국인", "foreign"),
            ("KOSPI",  "기관합계", "inst"),
            ("KOSDAQ", "기관합계", "inst"),
        ]

        for market, investor, field in configs:
            try:
                df = pykrx_stock.get_market_net_purchases_of_equities(
                    date_str, date_str, market, investor
                )
                if df is None or df.empty:
                    continue
                for ticker, row in df.iterrows():
                    sym = str(ticker).zfill(6)
                    if sym not in result:
                        result[sym] = {"foreign": 0, "inst": 0}
                    result[sym][field] += int(row.get("순매수거래량", 0) or 0)
            except Exception as e:
                logger.debug(f"[수급5일] pykrx {market}/{investor} {date_str}: {e}")

        return result

    # ── 점수 계산 ─────────────────────────────────────────────────────────────

    def _compute(self, symbol: str) -> Tuple[float, dict]:
        """
        종목별 5일 수급 점수 계산.

        Returns:
            (score: float, meta: dict)
        """
        if not self._loaded_dates:
            return 0.0, {}

        # 날짜 순서: 최신→오래된 (loaded_dates[0]이 최신)
        foreign_series = []
        inst_series = []
        for date_str in self._loaded_dates:
            day = self._daily.get(date_str, {}).get(symbol, {})
            foreign_series.append(day.get("foreign", 0))
            inst_series.append(day.get("inst", 0))

        # 연속 순매수 일수 (최신부터 역산)
        def streak(series: List[int]) -> int:
            count = 0
            for v in series:
                if v > 0:
                    count += 1
                else:
                    break
            return count

        foreign_streak = streak(foreign_series)
        inst_streak = streak(inst_series)

        # 5일 누적
        foreign_5d = sum(foreign_series)
        inst_5d = sum(inst_series)

        # 가속 여부: 최근 2일 합 > 이전 3일 합
        is_accelerating = False
        if len(foreign_series) >= 4:
            recent_2 = foreign_series[0] + foreign_series[1]
            prev_3 = sum(foreign_series[2:min(5, len(foreign_series))])
            if recent_2 > 0 and prev_3 > 0:
                is_accelerating = recent_2 > prev_3 * 0.8  # 최근이 과거보다 강하면

        # ── 점수 산출 ──────────────────────────────────────────────────────
        score = 0.0

        # 외국인 연속 순매수 (최대 30pt)
        if foreign_streak >= 5:
            score += 30
        elif foreign_streak >= 3:
            score += 20
        elif foreign_streak >= 1:
            score += 8

        # 기관 연속 순매수 (최대 25pt)
        if inst_streak >= 5:
            score += 25
        elif inst_streak >= 3:
            score += 15
        elif inst_streak >= 1:
            score += 6

        # 동시 매집 보너스 (최대 15pt)
        if foreign_streak >= 3 and inst_streak >= 3:
            score += 15
        elif foreign_streak >= 2 and inst_streak >= 2:
            score += 8

        # 규모 보너스: 5일 누적 (최대 15pt)
        total_5d = foreign_5d + inst_5d
        if total_5d > 0:
            score += min(15.0, total_5d / 50_000)  # 5만 주당 1pt

        # 가속 보너스 (15pt)
        if is_accelerating:
            score += 15

        score = min(100.0, max(0.0, score))

        meta = {
            "foreign_streak": foreign_streak,
            "inst_streak":    inst_streak,
            "foreign_5d":     foreign_5d,
            "inst_5d":        inst_5d,
            "total_5d":       total_5d,
            "is_accelerating": is_accelerating,
            "score":          score,
            "days_loaded":    len(self._loaded_dates),
        }
        return score, meta

    def get_score(self, symbol: str) -> float:
        """종목 5일 수급 점수 (0~100). 데이터 없으면 0."""
        if not self.is_ready:
            return 0.0
        if symbol not in self._score_cache:
            score, meta = self._compute(symbol)
            self._score_cache[symbol] = score
            self._meta_cache[symbol] = meta
        return self._score_cache[symbol]

    def get_meta(self, symbol: str) -> dict:
        """종목 5일 수급 상세 정보."""
        if not self.is_ready:
            return {}
        if symbol not in self._meta_cache:
            self.get_score(symbol)  # 계산 트리거
        return self._meta_cache.get(symbol, {})

    def get_bonus(self, symbol: str, max_bonus: float = 15.0) -> float:
        """
        스크리너/전략 점수에 더할 보너스.

        score 100 → max_bonus pt 선형 매핑.
        외국인 연속 3일↑ AND 기관 연속 3일↑ 면 동시 매집 추가 보너스.
        """
        score = self.get_score(symbol)
        if score <= 0:
            return 0.0
        meta = self._meta_cache.get(symbol, {})

        # 선형 매핑
        bonus = (score / 100.0) * max_bonus

        # 외국인+기관 동시 3일 이상이면 추가 5pt
        if meta.get("foreign_streak", 0) >= 3 and meta.get("inst_streak", 0) >= 3:
            bonus += 5.0

        return round(min(bonus, max_bonus + 5), 1)

    def top_n(self, n: int = 30) -> List[dict]:
        """점수 상위 N개 종목 반환 (대시보드/로그용)."""
        if not self.is_ready:
            return []
        # 전체 종목 집합
        all_syms: set = set()
        for day_data in self._daily.values():
            all_syms.update(day_data.keys())

        results = []
        for sym in all_syms:
            score = self.get_score(sym)
            if score > 0:
                meta = self.get_meta(sym)
                results.append({"symbol": sym, **meta})

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:n]
