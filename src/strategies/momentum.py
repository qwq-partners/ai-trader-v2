"""
AI Trading Bot v2 - 모멘텀 브레이크아웃 전략

20일 고가 돌파 시 매수, 트레일링 스탑으로 청산
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any
from loguru import logger

from .base import BaseStrategy, StrategyConfig
from ..core.types import (
    Signal, Position, Price,
    OrderSide, SignalStrength, StrategyType
)
from ..core.event import MarketDataEvent


@dataclass
class MomentumConfig(StrategyConfig):
    """모멘텀 전략 설정"""
    name: str = "MomentumBreakout"
    strategy_type: StrategyType = StrategyType.MOMENTUM_BREAKOUT

    # 브레이크아웃 조건
    breakout_period: int = 20        # 돌파 기준 기간 (일)
    confirm_candles: int = 2         # 돌파 확인 캔들 수
    min_breakout_pct: float = 0.5    # 최소 돌파 비율 (%)

    # 거래량 조건
    volume_surge_ratio: float = 2.0  # 거래량 급증 기준 (평균 대비)

    # 모멘텀 점수 가중치
    weight_price_momentum: float = 40.0
    weight_volume_momentum: float = 30.0
    weight_high_proximity: float = 20.0
    weight_theme: float = 10.0

    # 청산 조건
    stop_loss_pct: float = 2.0       # 손절 (%)
    take_profit_pct: float = 5.0     # 익절 (%)
    trailing_stop_pct: float = 1.5   # 트레일링 스탑 (%)

    # 시간대 제한
    trading_start_time: str = "09:05" # 시작 시간 (장 개시 직후)
    trading_end_time: str = "15:20"   # 종료 시간 (마감 10분 전)


class MomentumBreakoutStrategy(BaseStrategy):
    """
    모멘텀 브레이크아웃 전략

    매수 조건:
    - 20일 고가 돌파
    - 거래량 200% 이상 급증
    - 모멘텀 점수 70점 이상

    매도 조건:
    - 익절: +5%
    - 손절: -2%
    - 트레일링 스탑: 고점 대비 -1.5%
    """

    def __init__(self, config: Optional[MomentumConfig] = None):
        config = config or MomentumConfig()
        super().__init__(config)
        self.momentum_config = config

        # 브레이크아웃 추적
        self._breakout_candidates: Dict[str, datetime] = {}  # 돌파 후보
        self._confirmed_breakouts: Dict[str, float] = {}     # 확정 돌파 (가격)

        # 테마 정보 (외부에서 주입)
        self._hot_themes: Dict[str, float] = {}  # 종목 -> 테마 점수

    async def generate_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position] = None
    ) -> Optional[Signal]:
        """매매 신호 생성"""
        indicators = self.get_indicators(symbol)

        if not indicators:
            return None

        # 포지션 있는 경우 청산 체크
        if position and position.quantity > 0:
            return await self._check_exit_signal(symbol, current_price, position, indicators)

        # 포지션 없는 경우 진입 체크
        return await self._check_entry_signal(symbol, current_price, indicators)

    def _is_trading_time(self) -> bool:
        """거래 가능 시간 체크"""
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        return self.momentum_config.trading_start_time <= current_time <= self.momentum_config.trading_end_time

    async def _check_entry_signal(
        self,
        symbol: str,
        current_price: Decimal,
        indicators: Dict[str, float]
    ) -> Optional[Signal]:
        """진입 신호 체크"""
        if not self._is_trading_time():
            return None

        price = float(current_price)

        # 최소 가격 필터
        if price < self.config.min_price:
            return None

        # 20일 고가 체크
        high_20d = indicators.get("high_20d", 0)
        if high_20d <= 0:
            return None

        # 브레이크아웃 체크
        breakout_pct = (price - high_20d) / high_20d * 100 if high_20d > 0 else 0
        is_breaking = breakout_pct >= self.momentum_config.min_breakout_pct

        if not is_breaking:
            # 돌파 후보에서 제거
            self._breakout_candidates.pop(symbol, None)
            return None

        # 거래량 체크
        vol_ratio = indicators.get("vol_ratio", 0)
        if vol_ratio < self.momentum_config.volume_surge_ratio:
            return None

        # 모멘텀 점수 계산
        score = self.calculate_score(symbol)
        if score < self.config.min_score:
            return None

        # 신호 강도 결정
        if score >= 85:
            strength = SignalStrength.VERY_STRONG
        elif score >= 70:
            strength = SignalStrength.STRONG
        else:
            strength = SignalStrength.NORMAL

        # 목표가 & 손절가 계산
        target_price = Decimal(str(price * (1 + self.momentum_config.take_profit_pct / 100)))
        stop_price = Decimal(str(price * (1 - self.momentum_config.stop_loss_pct / 100)))

        # 신호 생성
        reason = (
            f"20일 고가 돌파 +{breakout_pct:.1f}%, "
            f"거래량 {vol_ratio:.1f}x, "
            f"점수 {score:.0f}"
        )

        return self.create_signal(
            symbol=symbol,
            side=OrderSide.BUY,
            strength=strength,
            price=current_price,
            score=score,
            reason=reason,
            target_price=target_price,
            stop_price=stop_price,
        )

    async def _check_exit_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Position,
        indicators: Dict[str, float]
    ) -> Optional[Signal]:
        """
        청산 신호 체크

        기계적 청산(손절/익절/트레일링)은 ExitManager가 전담합니다.
        모멘텀 전략은 별도 전략 고유 청산 조건이 없으므로 None 반환.
        """
        return None

    def calculate_score(self, symbol: str) -> float:
        """모멘텀 점수 계산 (0~100)"""
        indicators = self.get_indicators(symbol)
        if not indicators:
            return 0.0

        score = 0.0
        cfg = self.momentum_config

        # 1. 가격 모멘텀 (40점)
        change_1d = indicators.get("change_1d", 0)
        change_5d = indicators.get("change_5d", 0)
        change_20d = indicators.get("change_20d", 0)

        price_score = 0.0
        if change_1d > 0:
            price_score += 10
        if change_5d > 0:
            price_score += 15
        if change_20d > 0:
            price_score += 15

        score += min(price_score, cfg.weight_price_momentum)

        # 2. 거래량 모멘텀 (30점)
        vol_ratio = indicators.get("vol_ratio", 0)
        volume_score = min(vol_ratio * 10, cfg.weight_volume_momentum)
        score += volume_score

        # 3. 신고가 근접도 (20점)
        high_proximity = indicators.get("high_proximity", 0)
        if high_proximity > 0.95:
            score += 20
        elif high_proximity > 0.90:
            score += 15
        elif high_proximity > 0.80:
            score += 10

        # 4. 테마 연관성 (10점)
        theme_score = self._hot_themes.get(symbol, 0)
        if theme_score > 0:
            score += min(theme_score / 10, cfg.weight_theme)

        return min(score, 100.0)

    def set_hot_themes(self, theme_scores: Dict[str, float]):
        """핫 테마 종목 설정"""
        self._hot_themes = theme_scores

    def get_breakout_candidates(self) -> Dict[str, datetime]:
        """브레이크아웃 후보 목록"""
        return self._breakout_candidates.copy()
