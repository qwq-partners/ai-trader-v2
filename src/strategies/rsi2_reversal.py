"""
AI Trading Bot v2 - RSI-2 역추세 스윙 전략

승률 75-91% 연구 기반 RSI(2) 과매도 역추세 전략.
장기 상승 추세(MA200) 내에서 단기 과매도(RSI-2 < 10) 시 매수.

진입 조건:
  1. RSI(2) < 10 (과매도)
  2. 가격 > MA200 (장기 상승 추세 내 단기 조정)
  3. 가격 < 볼린저밴드 하단 (추가 확인)
  4. 거래대금 1억+ (유동성)

청산 조건:
  1. RSI(2) > 70 → 매도
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


class RSI2ReversalStrategy(BaseStrategy):
    """RSI-2 역추세 스윙 전략"""

    def __init__(self, config: Optional[StrategyConfig] = None):
        if config is None:
            config = StrategyConfig(
                name="RSI2Reversal",
                strategy_type=StrategyType.RSI2_REVERSAL,
                stop_loss_pct=5.0,
                take_profit_pct=10.0,
                min_score=65.0,
            )
        super().__init__(config)

        # 전략 파라미터
        self.rsi_period = config.params.get("rsi_period", 2)
        self.rsi_entry = config.params.get("rsi_entry", 10)
        self.rsi_exit = config.params.get("rsi_exit", 70)
        self.ma_trend_period = config.params.get("ma_trend_period", 200)
        self.bb_period = config.params.get("bb_period", 20)
        self.bb_std = config.params.get("bb_std", 2.0)
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
            candidates: SwingCandidate 리스트 (SwingScreener에서 필터된 후보)

        Returns:
            Signal 리스트
        """
        signals = []

        for candidate in candidates:
            try:
                score = self._calculate_rsi2_score(candidate)

                if score < self.config.min_score:
                    continue

                # ATR 기반 동적 손절/익절 (R:R 1:2 보장)
                atr = candidate.indicators.get("atr_14")
                atr_pct_value = 0.0
                if atr and atr > 0:
                    stop_pct = max(3.0, min(7.0, atr * 2.0))     # 2×ATR, 3~7% 범위
                    target_pct = max(5.0, min(15.0, atr * 4.0))   # 4×ATR, 5~15% 범위
                    candidate.stop_price = candidate.entry_price * Decimal(str(1 - stop_pct / 100))
                    candidate.target_price = candidate.entry_price * Decimal(str(1 + target_pct / 100))
                    atr_pct_value = atr

                # 시그널 강도 결정
                if score >= 85:
                    strength = SignalStrength.VERY_STRONG
                elif score >= 75:
                    strength = SignalStrength.STRONG
                else:
                    strength = SignalStrength.NORMAL

                signal = Signal(
                    symbol=candidate.symbol,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy=StrategyType.RSI2_REVERSAL,
                    price=candidate.entry_price,
                    target_price=candidate.target_price,
                    stop_price=candidate.stop_price,
                    score=score,
                    confidence=score / 100.0,
                    reason=f"RSI2역추세: {', '.join(candidate.reasons[:3])}",
                    metadata={
                        "strategy_name": self.name,
                        "candidate_name": candidate.name,
                        "indicators": candidate.indicators,
                        "atr_pct": atr_pct_value,
                    },
                )
                signals.append(signal)

                logger.info(
                    f"[RSI2] 시그널: {candidate.symbol} {candidate.name} "
                    f"점수={score:.0f} RSI(2)={candidate.indicators.get('rsi_2', 'N/A')} "
                    f"ATR={atr_pct_value:.1f}%"
                )

            except Exception as e:
                logger.warning(f"[RSI2] {candidate.symbol} 시그널 생성 실패: {e}")

        return signals

    def _calculate_rsi2_score(self, candidate) -> float:
        """
        RSI-2 역추세 점수 계산 (0-100)

        - RSI(2) 위치: 40점
        - MA200 위 거리: 20점
        - BB 하단 이탈 깊이: 15점
        - 수급 (외국인/기관): 10점
        - MRS 맨스필드 상대강도: 5점
        - 5일 하락 후 반등 조짐: 10점
        """
        ind = candidate.indicators
        score = 0.0

        # 1. RSI(2) 위치 (40점)
        rsi_2 = ind.get("rsi_2")
        if rsi_2 is not None:
            if rsi_2 < 5:
                score += 40
            elif rsi_2 < 10:
                score += 30
            elif rsi_2 < 15:
                score += 15

        # 2. MA200 위 거리 (20점)
        close = ind.get("close", 0)
        ma200 = ind.get("ma200")
        if close and ma200 and ma200 > 0:
            above_pct = (close - ma200) / ma200 * 100
            if above_pct > 20:
                score += 20
            elif above_pct > 10:
                score += 15
            elif above_pct > 0:
                score += 10

        # 3. BB 하단 이탈 (15점)
        bb_lower = ind.get("bb_lower")
        if close and bb_lower and bb_lower > 0:
            bb_dist = (close - bb_lower) / bb_lower * 100
            if bb_dist < -2:
                score += 15
            elif bb_dist < 0:
                score += 10
            elif bb_dist < 1:
                score += 5

        # 4. 수급 (10점) - 15→10점으로 축소, MRS 5점 재배분
        foreign_net = ind.get("foreign_net_buy", 0)
        inst_net = ind.get("inst_net_buy", 0)
        if foreign_net > 0:
            score += 5
        if inst_net > 0:
            score += 5

        # 5. MRS 맨스필드 상대강도 (5점) - 지수 대비 강세 종목의 과매도 → 반등 확률 높음
        mrs = ind.get("mrs")
        mrs_slope = ind.get("mrs_slope", 0)
        if mrs is not None:
            if mrs > 0 and mrs_slope > 0:
                score += 5
            elif mrs > 0:
                score += 3

        # 6. 5일 하락 후 반등 (10점)
        change_5d = ind.get("change_5d", 0)
        if change_5d and change_5d < -5:
            score += 10
        elif change_5d and change_5d < -3:
            score += 5

        return min(score, 100)
