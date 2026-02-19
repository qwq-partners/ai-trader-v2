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
                stop_loss_pct=3.5,      # 변경: 5%->3.5% (손절 축소, 손익비 개선)
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

        for candidate in candidates:
            try:
                score = self._calculate_sepa_score(candidate)

                if score < self.config.min_score:
                    continue

                # ATR 기반 동적 손절/익절 (변경: 손절 축소, 익절 현실화)
                atr = candidate.indicators.get("atr_14")
                if atr and atr > 0:
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

                atr_pct_value = candidate.indicators.get("atr_14", 0) or 0

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

        return signals

    def _calculate_sepa_score(self, candidate) -> float:
        """
        SEPA 트렌드 점수 계산 (0-100)

        - 기술적 (SEPA, MA정렬, 52w위치, MRS, MA5>MA20): 40점
        - 수급 LCI z-score 기반: 20점 (변경: 30->20, 기술적 조건과 균형)
        - 재무 (PER/PBR/ROE): 20점
        - 섹터 모멘텀: 10점
        """
        ind = candidate.indicators
        score = 0.0

        # 1. 기술적 (40점)
        # SEPA 통과 기본 점수: 15점 (20→15, MRS 5점 재배분)
        if ind.get("sepa_pass"):
            score += 15

        # MA 정렬 강도: MA50과 MA200 사이 거리 (7점)
        ma50 = ind.get("ma50", 0)
        ma200 = ind.get("ma200", 0)
        if ma50 and ma200 and ma200 > 0:
            spread = (ma50 - ma200) / ma200 * 100
            if spread > 10:
                score += 7
            elif spread > 5:
                score += 5
            elif spread > 0:
                score += 3

        # 52주 고점 근접도 (7점)
        close = ind.get("close", 0)
        high_52w = ind.get("high_52w", 0)
        if close and high_52w and high_52w > 0:
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

        # 2. 수급 LCI z-score 기반 (20점) - 변경: 30->20, 수급만으로 신호 생성 방지
        lci = ind.get("lci")
        if lci is not None:
            if lci > 1.5:
                score += 20  # 변경: 30->20 (기술적 조건과 균형)
            elif lci > 1.0:
                score += 15  # 변경: 22->15
            elif lci > 0.5:
                score += 10  # 변경: 15->10
            elif lci > 0:
                score += 5   # 변경: 8->5
            # lci <= 0: 0점
        else:
            # LCI 미계산 시 기존 방식 폴백 (변경: 축소)
            foreign_net = ind.get("foreign_net_buy", 0)
            inst_net = ind.get("inst_net_buy", 0)
            supply_score = 0
            if foreign_net > 0:
                supply_score += 10
            if inst_net > 0:
                supply_score += 10
            score += min(supply_score, 20)  # LCI 경로와 동일 상한

        # 3. 재무 (20점)
        per = ind.get("per", 0)
        pbr = ind.get("pbr", 0)
        roe = ind.get("roe", 0)

        if per and 0 < per < 20:
            score += 7
        elif per and 0 < per < 30:
            score += 4

        if pbr and 0 < pbr < 3:
            score += 6
        elif pbr and 0 < pbr < 5:
            score += 3

        if roe and roe > 10:
            score += 7
        elif roe and roe > 5:
            score += 4

        # 4. 섹터 모멘텀 (10점)
        sector_score = ind.get("sector_momentum", 0)
        if sector_score > 0:
            score += min(sector_score / 10, 10)

        return min(score, 100)
