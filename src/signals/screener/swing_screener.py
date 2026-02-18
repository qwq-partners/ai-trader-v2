"""
AI Trading Bot v2 - 스윙 모멘텀 스크리너

장 마감 후 배치 스캔: 유니버스 선정 → FDR 일봉 → 기술적 지표 → 전략별 필터 → 복합 점수.
기존 stock_screener.py(단기 급등 필터)와 독립.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from loguru import logger

from ...indicators.technical import TechnicalIndicators


@dataclass
class SwingCandidate:
    """스윙 매매 후보 종목"""
    symbol: str
    name: str
    strategy: str  # "rsi2_reversal" | "sepa_trend"
    score: float  # 0-100
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    indicators: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


class SwingScreener:
    """스윙 모멘텀 종목 스크리너"""

    def __init__(self, broker, kis_market_data, stock_master=None):
        self._broker = broker
        self._kis_market_data = kis_market_data
        self._stock_master = stock_master
        self._indicators = TechnicalIndicators()
        self._kospi_closes: List[float] = []  # 벤치마크 KOSPI 종가 (MRS용)

    async def run_full_scan(self) -> List[SwingCandidate]:
        """
        전체 스캔: 유니버스 → 지표 → 필터 → 점수

        Returns:
            점수 순 정렬된 SwingCandidate 리스트
        """
        logger.info("[스윙스크리너] 전체 스캔 시작...")

        # 0단계: 벤치마크 지수(KOSPI) 로드 (MRS 계산용)
        await self._load_benchmark_index()

        # 1단계: 유니버스 선정
        universe = await self._build_universe()
        logger.info(f"[스윙스크리너] 유니버스: {len(universe)}개 종목")

        if not universe:
            logger.warning("[스윙스크리너] 유니버스 비어있음")
            return []

        # 2단계: FDR 일봉 + 기술적 지표 계산
        candidates_data = await self._calculate_all_indicators(universe)
        logger.info(f"[스윙스크리너] 지표 계산 완료: {len(candidates_data)}개 종목")

        # 3단계: 전략별 필터
        rsi2_candidates = self._filter_rsi2_reversal(candidates_data)
        sepa_candidates = self._filter_sepa_trend(candidates_data)
        logger.info(
            f"[스윙스크리너] 필터 결과: RSI2={len(rsi2_candidates)}개, SEPA={len(sepa_candidates)}개"
        )

        # 4단계: LCI z-score 계산 + 수급/재무 점수
        all_candidates = rsi2_candidates + sepa_candidates
        self._compute_lci_zscore(all_candidates)
        scored = await self._apply_composite_score(all_candidates)

        # 점수 순 정렬
        scored.sort(key=lambda c: c.score, reverse=True)

        logger.info(f"[스윙스크리너] 최종 후보: {len(scored)}개 종목")
        for c in scored[:5]:
            logger.info(
                f"  {c.symbol} {c.name}: 점수={c.score:.0f} 전략={c.strategy} "
                f"진입={c.entry_price:,.0f} 손절={c.stop_price:,.0f}"
            )

        return scored

    async def _build_universe(self) -> List[Dict[str, str]]:
        """
        1단계: 유니버스 선정 (150-250종목)

        소스:
        - KOSPI200 + KOSDAQ150 (거래대금 상위 200개)
        - 등락률 상위
        - 외국인/기관 순매수

        필터:
        - 거래대금 1억+, ETF 제외, 가격 2000원+
        """
        universe = {}  # symbol → {"symbol", "name"}

        # KOSPI200 + KOSDAQ150 (StockMaster) — 200종목으로 확대
        if self._stock_master:
            try:
                top_stocks = await self._stock_master.get_top_stocks(limit=200)
                for symbol in top_stocks:
                    name = await self._stock_master.get_name(symbol) or symbol
                    # ETF/ETN/파생상품 제외 (이름 기반)
                    if self._should_exclude(name):
                        logger.debug(f"[스크리닝] ETF/ETN 제외: {name}({symbol})")
                        continue
                    universe[symbol] = {"symbol": symbol, "name": name}
            except Exception as e:
                logger.warning(f"[스윙스크리너] StockMaster 조회 실패: {e}")

        # 등락률 순위
        if self._kis_market_data:
            try:
                ranked = await self._kis_market_data.fetch_fluctuation_rank(limit=50)
                for item in ranked:
                    symbol = item.get("symbol", item.get("stck_shrn_iscd", ""))
                    if not symbol:
                        continue
                    name = item.get("name", item.get("hts_kor_isnm", symbol))
                    price = float(item.get("price", item.get("stck_prpr", 0)))
                    # ETF/ETN/파생상품 제외
                    if self._should_exclude(name):
                        logger.debug(f"[스크리닝] ETF/ETN 제외: {name}({symbol})")
                        continue
                    if price < 2000:
                        continue
                    universe[symbol] = {"symbol": symbol, "name": name}
            except Exception as e:
                logger.warning(f"[스윙스크리너] 등락률 순위 조회 실패: {e}")

            # 외국인 순매수
            try:
                foreign = await self._kis_market_data.fetch_foreign_institution(investor="1")
                for item in foreign[:30]:
                    symbol = item.get("symbol", item.get("stck_shrn_iscd", ""))
                    name = item.get("name", item.get("hts_kor_isnm", symbol))
                    if symbol and not self._should_exclude(name):
                        universe[symbol] = {"symbol": symbol, "name": name}
                    elif symbol and self._should_exclude(name):
                        logger.debug(f"[스크리닝] ETF/ETN 제외: {name}({symbol})")
            except Exception as e:
                logger.debug(f"[스윙스크리너] 외국인 순매수 조회 실패: {e}")

            # 기관 순매수
            try:
                inst = await self._kis_market_data.fetch_foreign_institution(investor="2")
                for item in inst[:30]:
                    symbol = item.get("symbol", item.get("stck_shrn_iscd", ""))
                    name = item.get("name", item.get("hts_kor_isnm", symbol))
                    if symbol and not self._should_exclude(name):
                        universe[symbol] = {"symbol": symbol, "name": name}
                    elif symbol and self._should_exclude(name):
                        logger.debug(f"[스크리닝] ETF/ETN 제외: {name}({symbol})")
            except Exception as e:
                logger.debug(f"[스윙스크리너] 기관 순매수 조회 실패: {e}")

        return list(universe.values())

    async def _calculate_all_indicators(
        self, universe: List[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        """
        2단계: FDR 일봉 1년 조회 + 기술적 지표 계산

        FDR(FinanceDataReader)로 1년치 일봉 조회 (KIS API 60일 제한 우회).
        """
        results = []
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        loop = asyncio.get_event_loop()

        for stock in universe:
            symbol = stock["symbol"]
            name = stock["name"]

            try:
                # FDR 일봉 조회 (동기 → 비동기 래핑)
                df = await loop.run_in_executor(
                    None, self._fetch_fdr_data, symbol, start_date
                )

                if df is None or len(df) < 50:
                    logger.debug(f"[스윙스크리너] {symbol} 데이터 부족 ({len(df) if df is not None else 0}일)")
                    continue

                # DataFrame → List[Dict]
                daily_data = []
                for _, row in df.iterrows():
                    daily_data.append({
                        "date": row.name.strftime("%Y%m%d") if hasattr(row.name, 'strftime') else str(row.name),
                        "open": float(row.get("Open", 0)),
                        "high": float(row.get("High", 0)),
                        "low": float(row.get("Low", 0)),
                        "close": float(row.get("Close", 0)),
                        "volume": int(row.get("Volume", 0)),
                    })

                # 거래대금 필터: 30일 평균 10억원 이상
                recent_30 = daily_data[-30:] if len(daily_data) >= 30 else daily_data
                avg_trade_value = sum(
                    d["close"] * d["volume"] for d in recent_30
                ) / len(recent_30)
                if avg_trade_value < 1_000_000_000:
                    logger.debug(
                        f"[스윙스크리너] {symbol} 거래대금 부족: "
                        f"{avg_trade_value/1e8:.0f}억 (<10억)"
                    )
                    continue

                # 기술적 지표 계산
                indicators = self._indicators.calculate_all(symbol, daily_data)
                if not indicators:
                    continue

                # MRS 계산 (벤치마크 데이터 있을 경우)
                if self._kospi_closes:
                    stock_closes = [float(d["close"]) for d in daily_data]
                    mrs_result = self._indicators.calculate_mrs(
                        stock_closes, self._kospi_closes, period=20
                    )
                    if mrs_result:
                        indicators["mrs"] = mrs_result["mrs"]
                        indicators["mrs_slope"] = mrs_result["mrs_slope"]

                results.append({
                    "symbol": symbol,
                    "name": name,
                    "indicators": indicators,
                    "daily_data": daily_data,
                })

            except Exception as e:
                logger.debug(f"[스윙스크리너] {symbol} 지표 계산 실패: {e}")

        return results

    @staticmethod
    def _fetch_fdr_data(symbol: str, start_date: str):
        """FDR 일봉 조회 (동기)"""
        try:
            import FinanceDataReader as fdr
            df = fdr.DataReader(symbol, start_date)
            return df
        except Exception as e:
            logger.debug(f"[FDR] {symbol} 조회 실패: {e}")
            return None

    def _filter_rsi2_reversal(
        self, candidates_data: List[Dict[str, Any]]
    ) -> List[SwingCandidate]:
        """3단계: RSI-2 역추세 필터"""
        results = []

        for data in candidates_data:
            ind = data["indicators"]

            # RSI-2 진입 조건 체크
            rsi2_pass, rsi_val, reason = self._indicators.check_rsi2_entry(ind)
            if not rsi2_pass:
                continue

            close = Decimal(str(ind.get("close", 0)))
            if close <= 0:
                continue

            # 손절: -5%
            stop_price = close * Decimal("0.95")
            # 목표: RSI(2) > 70 도달 시 (보통 +3~8%)
            target_price = close * Decimal("1.05")

            candidate = SwingCandidate(
                symbol=data["symbol"],
                name=data["name"],
                strategy="rsi2_reversal",
                score=0,  # 4단계에서 계산
                entry_price=close,
                stop_price=stop_price,
                target_price=target_price,
                indicators=ind,
                reasons=[reason],
            )
            results.append(candidate)

        return results

    def _filter_sepa_trend(
        self, candidates_data: List[Dict[str, Any]]
    ) -> List[SwingCandidate]:
        """3단계: SEPA 트렌드 필터"""
        results = []

        for data in candidates_data:
            ind = data["indicators"]

            # SEPA 조건 체크
            sepa_pass = ind.get("sepa_pass", False)
            sepa_reasons = ind.get("sepa_reasons", [])
            if not sepa_pass:
                continue

            # MA5 > MA20: 필수 아닌 보너스 (단기 눌림목에서도 진입 기회 확보)
            # → sepa_trend.py 점수 계산에서 가점으로 반영

            close = Decimal(str(ind.get("close", 0)))
            if close <= 0:
                continue

            # 손절: -5%
            stop_price = close * Decimal("0.95")
            # 목표: +10%
            target_price = close * Decimal("1.10")

            candidate = SwingCandidate(
                symbol=data["symbol"],
                name=data["name"],
                strategy="sepa_trend",
                score=0,
                entry_price=close,
                stop_price=stop_price,
                target_price=target_price,
                indicators=ind,
                reasons=sepa_reasons,
            )
            results.append(candidate)

        return results

    async def _apply_composite_score(
        self, candidates: List[SwingCandidate]
    ) -> List[SwingCandidate]:
        """
        4단계: 복합 점수 (0-100)

        | 카테고리 | 비중 | 내용 |
        |---------|------|------|
        | 기술적 | 40% | RSI 위치, MA 정렬, BB 위치 |
        | 수급 | 30% | 외국인+기관 순매수 |
        | 재무 | 20% | PER/PBR/ROE |
        | 섹터 | 10% | 섹터 모멘텀 |
        """
        for candidate in candidates:
            ind = candidate.indicators

            # 수급 데이터 보강
            if self._kis_market_data:
                try:
                    valuation = await self._kis_market_data.fetch_stock_valuation(candidate.symbol)
                    if valuation:
                        ind["per"] = valuation.get("per", 0)
                        ind["pbr"] = valuation.get("pbr", 0)
                        ind["roe"] = valuation.get("roe", 0)
                    await asyncio.sleep(0.1)  # rate limit
                except Exception:
                    pass

            # 점수는 전략의 generate_batch_signals에서 계산하므로
            # 여기서는 기본 기술적 점수만 설정
            score = self._base_technical_score(ind, candidate.strategy)
            candidate.score = score

        return candidates

    def _base_technical_score(self, ind: Dict[str, Any], strategy: str) -> float:
        """기본 기술적 점수 (전략에서 상세 점수 재계산)"""
        score = 50.0  # 기본값

        if strategy == "rsi2_reversal":
            rsi_2 = ind.get("rsi_2")
            if rsi_2 is not None:
                if rsi_2 < 5:
                    score += 20
                elif rsi_2 < 10:
                    score += 10

            ma200 = ind.get("ma200")
            close = ind.get("close", 0)
            if ma200 and close and close > ma200:
                score += 10

        elif strategy == "sepa_trend":
            if ind.get("sepa_pass"):
                score += 15

            # 52주 고점 근접
            high_52w = ind.get("high_52w", 0)
            close = ind.get("close", 0)
            if high_52w and close:
                from_high = (close - high_52w) / high_52w * 100
                if from_high >= -10:
                    score += 10

        return min(score, 100)

    @staticmethod
    def _should_exclude(name: str) -> bool:
        """ETF/ETN/관리종목/정리매매 제외 판단"""
        upper = name.upper()
        exclude_keywords_upper = [
            # ETF 운용사 브랜드 (대문자 비교)
            "KODEX", "TIGER", "KBSTAR", "ARIRANG", "KOSEF",
            "HANARO", "SOL", "KINDEX", "ACE", "PLUS", "RISE",
            "BNK", "TIMEFOLIO", "WOORI", "FOCUS", "TREX",
            "SMART", "MASTER",
            # ETF/ETN 키워드
            "ETF", "ETN",
            # 파생상품 키워드
            "인버스", "레버리지", "선물", "채권", "원유", "금선물",
        ]
        exclude_keywords_any = [
            # 관리종목/정리매매 (원문 포함 검사)
            "관리", "정리매매", "투자주의",
        ]
        if any(kw in upper for kw in exclude_keywords_upper):
            return True
        if any(kw in name for kw in exclude_keywords_any):
            return True
        return False

    async def _load_benchmark_index(self):
        """벤치마크 지수(KOSPI) 1년치 로드 (MRS 계산용)"""
        try:
            loop = asyncio.get_event_loop()
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
            kospi_df = await loop.run_in_executor(
                None, self._fetch_fdr_data, "KS11", start_date
            )
            if kospi_df is not None and len(kospi_df) >= 50:
                self._kospi_closes = [float(row["Close"]) for _, row in kospi_df.iterrows()]
                logger.info(f"[스윙스크리너] KOSPI 벤치마크 로드: {len(self._kospi_closes)}일")
            else:
                self._kospi_closes = []
                logger.warning("[스윙스크리너] KOSPI 벤치마크 로드 실패")
        except Exception as e:
            self._kospi_closes = []
            logger.warning(f"[스윙스크리너] KOSPI 벤치마크 로드 오류: {e}")

    def _compute_lci_zscore(self, candidates: List[SwingCandidate]):
        """
        전체 후보의 외국인/기관 순매수 → z-score → LCI 계산

        LCI = 0.5 * z(foreign) + 0.5 * z(inst)
        """
        if not candidates:
            return

        all_foreign = [c.indicators.get("foreign_net_buy", 0) or 0 for c in candidates]
        all_inst = [c.indicators.get("inst_net_buy", 0) or 0 for c in candidates]

        def zscore_list(values: List[float]) -> List[float]:
            n = len(values)
            if n < 2:
                return [0.0] * n
            mean = sum(values) / n
            variance = sum((v - mean) ** 2 for v in values) / n
            std = variance ** 0.5
            if std < 1e-10:
                return [0.0] * n
            return [(v - mean) / std for v in values]

        z_foreign = zscore_list(all_foreign)
        z_inst = zscore_list(all_inst)

        # 수급 데이터 전무(std≈0) 시 z-score 전부 0 → LCI=None으로 설정하여 폴백 경로 활성화
        all_zero = all(z == 0.0 for z in z_foreign) and all(z == 0.0 for z in z_inst)
        for i, c in enumerate(candidates):
            if all_zero:
                c.indicators["lci"] = None
            else:
                lci = 0.5 * z_foreign[i] + 0.5 * z_inst[i]
                c.indicators["lci"] = round(lci, 3)
