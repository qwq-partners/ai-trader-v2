"""
AI Trading Bot v2 - SEPA 트렌드 템플릿 스윙 전략

미너비니(Minervini) 트렌드 템플릿 기반 추세추종 전략.
강한 상승 추세 내에서 눌림목 진입 → 추세 유지 시 보유.

진입 조건 (SEPA):
  1. MA50 > MA150 > MA200 (추세 정렬)
  2. 가격 > MA50
  3. MA200 상승 추세
  4. 52주 저점 대비 +30% 이상
  5. 52주 고점 대비 -25% 이내
  6. 외국인+기관 순매수 (우선)

청산 조건:
  1. MA50 하향 이탈 → 매도
  2. 손절: -5%
  3. 보유기간 10일 초과 → 강제 청산
"""

from decimal import Decimal
from typing import Dict, List, Optional, Any

from loguru import logger

from .base import BaseStrategy, StrategyConfig
from ..core.types import (
    Signal, Position, OrderSide, SignalStrength, StrategyType
)


class SEPATrendStrategy(BaseStrategy):
    """미너비니 SEPA 트렌드 템플릿 스윙 전략"""

    def __init__(self, config: Optional[StrategyConfig] = None):
        if config is None:
            config = StrategyConfig(
                name="SEPATrend",
                strategy_type=StrategyType.SEPA_TREND,
                stop_loss_pct=5.0,       # default.yml sepa_trend.stop_loss_pct 와 통일 (구 3.5→5.0)
                take_profit_pct=8.0,     # 변경: 15%->8% (현실적 익절 목표)
                min_score=70.0,
            )
        super().__init__(config)

        self.max_holding_days = config.params.get("max_holding_days", 10)

    async def generate_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position] = None
    ) -> Optional[Signal]:
        """실시간 시그널 생성 (스윙 전략에서는 미사용)"""
        return None

    def calculate_score(self, symbol: str) -> float:
        """신호 점수 계산"""
        return 0.0

    async def generate_batch_signals(self, candidates: List) -> List[Signal]:
        """
        배치 분석 결과 → Signal 리스트

        Args:
            candidates: SwingCandidate 리스트 (SEPA 조건 통과)

        Returns:
            Signal 리스트
        """
        signals = []
        all_scores: List[tuple] = []  # (score, symbol, name) — 분포 확인용

        for candidate in candidates:
            try:
                score = self._calculate_sepa_score(candidate)
                all_scores.append((score, candidate.symbol, candidate.name))

                if score < self.config.min_score:
                    continue

                # ATR 기반 동적 손절/익절 (변경: 손절 축소, 익절 현실화)
                atr = candidate.indicators.get("atr_14")
                if atr is not None and atr > 0:
                    stop_pct = max(2.5, min(5.0, atr * 1.5))   # 변경: 3~7%->2.5~5%, x2->x1.5
                    target_pct = max(3.0, min(8.0, atr * 3.0))  # 변경: 5~15%->3~8%, x4->x3
                    candidate.stop_price = candidate.entry_price * Decimal(str(1 - stop_pct / 100))
                    candidate.target_price = candidate.entry_price * Decimal(str(1 + target_pct / 100))

                if score >= 85:
                    strength = SignalStrength.VERY_STRONG
                elif score >= 75:
                    strength = SignalStrength.STRONG
                else:
                    strength = SignalStrength.NORMAL

                atr_pct_value = candidate.indicators.get("atr_14", 0)
                atr_pct_value = atr_pct_value if atr_pct_value is not None else 0

                signal = Signal(
                    symbol=candidate.symbol,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy=StrategyType.SEPA_TREND,
                    price=candidate.entry_price,
                    target_price=candidate.target_price,
                    stop_price=candidate.stop_price,
                    score=score,
                    confidence=score / 100.0,
                    reason=f"SEPA트렌드: {', '.join(candidate.reasons[:3])}",
                    metadata={
                        "strategy_name": self.name,
                        "candidate_name": candidate.name,
                        "indicators": candidate.indicators,
                        "atr_pct": atr_pct_value,
                    },
                )
                signals.append(signal)

                logger.info(
                    f"[SEPA] 시그널: {candidate.symbol} {candidate.name} "
                    f"점수={score:.0f} MRS={candidate.indicators.get('mrs', 'N/A')} "
                    f"LCI={candidate.indicators.get('lci', 'N/A')}"
                )

            except Exception as e:
                logger.warning(f"[SEPA] {candidate.symbol} 시그널 생성 실패: {e}")

        # 점수 분포 요약 로그 (min_score 미달 원인 파악용)
        if all_scores:
            all_scores.sort(reverse=True)
            top = all_scores[:10]
            passed = sum(1 for s, _, _ in all_scores if s >= self.config.min_score)
            logger.info(
                f"[SEPA] 점수분포: 전체={len(all_scores)}개, "
                f"통과={passed}개 (min={self.config.min_score}), "
                f"평균={sum(s for s,_,_ in all_scores)/len(all_scores):.1f}, "
                f"최고={all_scores[0][0]:.1f}"
            )
            logger.info(f"[SEPA] 상위 10개 점수:")
            for score, sym, name in top:
                lci = None
                # lci 찾기
                for c in candidates:
                    if c.symbol == sym:
                        lci = c.indicators.get("lci")
                        break
                mark = "✅" if score >= self.config.min_score else "  "
                logger.info(
                    f"  {mark} {sym} {name}: {score:.1f}pt  LCI={f'{lci:.2f}' if lci is not None else 'None'}"
                )

        return signals

    def _calculate_sepa_score(self, candidate) -> float:
        """
        SEPA 트렌드 점수 계산 (0-100)

        - 기술적 (SEPA, MA정렬, 52w위치, MRS, MA5>MA20): 40점
        - 수급 LCI z-score 기반: 20점
        - 재무 (ROE 중심 축소): 10점 (20→10, 단기스윙에 PER/PBR 영향 제한적)
        - 거래량 모멘텀: 10점 (신규, 추세 확인용)
        - 섹터 모멘텀: 10점
        합계: ~40+20+10+10+10 = ~90점 (min_score 60 기준 충분한 변별력)
        """
        ind = candidate.indicators
        score = 0.0

        # 1. 기술적 (40점)
        # SEPA 통과 기본 점수: 15점
        if ind.get("sepa_pass"):
            score += 15

        # MA 정렬 강도: MA50과 MA200 사이 거리 (7점)
        ma50 = ind.get("ma50")
        ma200 = ind.get("ma200")
        if ma50 is not None and ma200 is not None and ma200 > 0:
            spread = (ma50 - ma200) / ma200 * 100
            if spread > 10:
                score += 7
            elif spread > 5:
                score += 5
            elif spread > 0:
                score += 3

        # 52주 고점 근접도 (7점) — 신고가 돌파 임박 = 추세 강도
        close = ind.get("close")
        high_52w = ind.get("high_52w")
        if close is not None and high_52w is not None and high_52w > 0:
            from_high = (close - high_52w) / high_52w * 100
            if from_high >= -5:
                score += 7
            elif from_high >= -10:
                score += 5
            elif from_high >= -15:
                score += 3

        # MRS 맨스필드 상대강도 (5점)
        mrs = ind.get("mrs")
        mrs_slope = ind.get("mrs_slope", 0)
        if mrs is not None:
            if mrs > 0 and mrs_slope > 0:
                score += 5
            elif mrs > 0:
                score += 3

        # MA5 > MA20 정렬 보너스 (3점)
        if ind.get("ma5_above_ma20", False):
            score += 3

        # 2. 수급 LCI z-score 기반 (20점)
        lci = ind.get("lci")
        if lci is not None:
            if lci > 1.5:
                score += 20
            elif lci > 1.0:
                score += 15
            elif lci > 0.5:
                score += 10
            elif lci > 0:
                score += 5
            # lci <= 0: 0점
        else:
            # LCI 미계산 시 폴백 (프리장/데이터 미수집 등)
            foreign_net = ind.get("foreign_net_buy", 0) or 0
            inst_net = ind.get("inst_net_buy", 0) or 0
            if foreign_net > 0 or inst_net > 0:
                # 외국인/기관 순매수 데이터 있는 경우 (장중/종가 스캔 시)
                supply_score = (10 if foreign_net > 0 else 0) + (10 if inst_net > 0 else 0)
                score += min(supply_score, 20)
            else:
                # 수급 데이터 완전 미존재 (프리장 08:20 등) → 중립값 (페널티 없음)
                # 데이터 없음 ≠ 수급 없음 — 불이익 주지 않음
                score += 5

        # 3. 재무 (10점) — ROE 중심으로 축소, PER/PBR은 단기스윙에 영향 제한적
        per = ind.get("per", 0)
        pbr = ind.get("pbr", 0)
        roe = ind.get("roe", 0)

        if per and 0 < per < 20:
            score += 2
        elif per and 0 < per < 30:
            score += 1

        if pbr and 0 < pbr < 3:
            score += 2
        elif pbr and 0 < pbr < 5:
            score += 1

        if roe and roe > 10:
            score += 6   # ROE에 집중 (수익성 좋은 회사의 추세가 더 강함)
        elif roe and roe > 5:
            score += 3

        # 4. 거래량 모멘텀 (10점) — 추세 확인: 거래량이 수반되지 않은 상승은 허상
        vol_ratio = (ind.get("vol_ratio") or ind.get("volume_ratio") or
                     ind.get("vol_inrt") or 0)
        try:
            vol_ratio = float(vol_ratio)
        except (TypeError, ValueError):
            vol_ratio = 0.0
        if vol_ratio > 2.0:
            score += 10
        elif vol_ratio > 1.5:
            score += 7
        elif vol_ratio > 1.0:
            score += 4

        # 5. 섹터 모멘텀 (10점) — Phase 3
        # 우선순위: sector_momentum_score(KODEX ETF 기반) > change_20d(개별 종목 20일 수익률)
        # sector_momentum_score: batch_analyzer._scan_and_build()에서 주입 (0~10pt)
        # change_20d 폴백: technical.py에서 계산된 개별 종목 20일 수익률 기반 변환
        sm_score = ind.get("sector_momentum_score")
        if sm_score is not None:
            # ETF 기반 섹터 모멘텀 (SectorMomentumProvider.get_sepa_score 결과)
            score += max(0.0, min(10.0, float(sm_score)))
        else:
            # 폴백: 개별 종목 change_20d → 섹터 모멘텀 대리 지표
            change_20d = ind.get("change_20d", 0) or 0
            try:
                change_20d = float(change_20d)
            except (TypeError, ValueError):
                change_20d = 0.0
            if change_20d > 20:
                score += 10
            elif change_20d > 10:
                score += 7
            elif change_20d > 5:
                score += 4
            elif change_20d > 0:
                score += 2

        return min(score, 100)
