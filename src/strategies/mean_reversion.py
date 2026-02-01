"""
AI Trading Bot v2 - 평균 회귀 전략 (Mean Reversion)

과매도 구간에서 반등을 노리는 역추세 전략입니다.

전략 원리:
1. RSI 30 이하 과매도 종목 탐지
2. 3일 이상 연속 하락 (-10% 이상)
3. 거래량 급증으로 바닥 신호 확인
4. 첫 양봉 출현 시 매수

주의사항:
- 펀더멘털 문제가 있는 종목 제외 (뉴스 확인)
- 하락 추세가 강할 경우 추가 손실 가능
- 리스크 높음, 포지션 작게 가져가기

적용 시간대:
- 정규장 전 시간대
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Set, Any
from loguru import logger

from .base import BaseStrategy, StrategyConfig
from ..core.types import (
    Signal, Position,
    OrderSide, SignalStrength, StrategyType
)


@dataclass
class MeanReversionConfig(StrategyConfig):
    """평균 회귀 전략 설정"""
    name: str = "MeanReversion"
    strategy_type: StrategyType = StrategyType.MEAN_REVERSION

    # 과매도 조건
    max_rsi: float = 30.0             # 최대 RSI (이하일 때 진입)
    min_decline_pct: float = -10.0    # 최소 하락률 (3일 기준)
    min_decline_days: int = 3         # 최소 연속 하락일

    # 진입 조건
    bullish_candle_required: bool = True  # 양봉 필수 여부
    min_volume_ratio: float = 1.5         # 최소 거래량 비율 (바닥 신호)
    max_drawdown_from_high: float = 30.0  # 고점 대비 최대 낙폭 (%)

    # 청산 조건
    stop_loss_pct: float = 3.0        # 손절 (역추세라 넓게)
    take_profit_pct: float = 5.0      # 익절
    trailing_stop_pct: float = 2.0    # 트레일링 스탑

    # 리스크 조정
    position_size_multiplier: float = 0.5  # 포지션 크기 축소 (리스크 높음)

    # 시간 제한
    trading_start_time: str = "09:30"
    trading_end_time: str = "15:00"


class MeanReversionStrategy(BaseStrategy):
    """
    평균 회귀 전략 (급락 반등)

    과매도된 종목의 기술적 반등을 노립니다.

    매수 조건:
    - RSI 30 이하
    - 3일 연속 하락 (-10% 이상)
    - 거래량 급증 (바닥 신호)
    - 양봉 출현

    매도 조건:
    - 익절: +5%
    - 손절: -3%
    - 트레일링 스탑: 고점 대비 -2%
    """

    def __init__(self, config: Optional[MeanReversionConfig] = None):
        config = config or MeanReversionConfig()
        super().__init__(config)
        self.mr_config = config

        # 과매도 종목 추적
        self._oversold_stocks: Dict[str, Dict[str, Any]] = {}
        # symbol -> {"rsi": float, "decline_pct": float, "detected_at": datetime}

        # 블랙리스트 (펀더멘털 문제 종목, TTL 24시간)
        self._blacklist: Dict[str, datetime] = {}  # symbol -> added_at
        self._blacklist_ttl_hours: int = 24

    async def generate_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position] = None
    ) -> Optional[Signal]:
        """매매 신호 생성"""
        # 블랙리스트 체크 (TTL 만료 항목 자동 정리)
        if symbol in self._blacklist:
            added_at = self._blacklist[symbol]
            if (datetime.now() - added_at).total_seconds() < self._blacklist_ttl_hours * 3600:
                return None
            else:
                del self._blacklist[symbol]  # TTL 만료

        indicators = self.get_indicators(symbol)

        if not indicators:
            return None

        # 포지션 있는 경우 청산 체크
        if position and position.quantity > 0:
            return await self._check_exit_signal(symbol, current_price, position, indicators)

        # 포지션 없는 경우 진입 체크
        return await self._check_entry_signal(symbol, current_price, indicators)

    async def _check_entry_signal(
        self,
        symbol: str,
        current_price: Decimal,
        indicators: Dict[str, float]
    ) -> Optional[Signal]:
        """진입 신호 체크"""
        # 시간대 체크
        if not self._is_trading_time():
            return None

        price = float(current_price)
        rsi = indicators.get("rsi_14")
        if rsi is None:
            return None  # RSI 미계산 시 진입하지 않음
        change_3d = indicators.get("change_3d", 0)
        change_1d = indicators.get("change_1d", 0)
        vol_ratio = indicators.get("vol_ratio", 1)
        high_52w = indicators.get("high_52w", 0)

        # 1. RSI 과매도 체크
        if rsi > self.mr_config.max_rsi:
            return None

        # 2. 하락폭 체크
        if change_3d > self.mr_config.min_decline_pct:
            return None

        # 3. 고점 대비 낙폭 체크 (52주 고가 미계산 시 스킵)
        if high_52w and high_52w > 0:
            drawdown = (price - high_52w) / high_52w * 100
            if drawdown < -self.mr_config.max_drawdown_from_high:
                logger.debug(f"[MeanReversion] {symbol} 낙폭 과대 ({drawdown:.1f}%)")
                return None

        # 4. 양봉 체크 (오늘 상승 마감)
        if self.mr_config.bullish_candle_required:
            if change_1d <= 0:
                return None

        # 5. 거래량 체크 (바닥 신호)
        if vol_ratio < self.mr_config.min_volume_ratio:
            return None

        # 과매도 종목 추적
        if symbol not in self._oversold_stocks:
            self._oversold_stocks[symbol] = {
                "rsi": rsi,
                "decline_pct": change_3d,
                "detected_at": datetime.now(),
            }
            logger.info(f"[MeanReversion] 과매도 감지: {symbol} RSI={rsi:.0f}, 3일 {change_3d:+.1f}%")

        # 신호 강도 결정 (역추세라 신중하게)
        if rsi < 20:
            strength = SignalStrength.STRONG
        elif rsi < 25:
            strength = SignalStrength.NORMAL
        else:
            strength = SignalStrength.WEAK

        # 점수 계산
        score = self._calculate_entry_score(rsi, change_3d, vol_ratio, change_1d)

        # 목표가 & 손절가
        target_price = Decimal(str(price * (1 + self.mr_config.take_profit_pct / 100)))
        stop_price = Decimal(str(price * (1 - self.mr_config.stop_loss_pct / 100)))

        reason = (
            f"과매도 반등: RSI={rsi:.0f}, "
            f"3일 {change_3d:+.1f}%, 오늘 {change_1d:+.1f}%, "
            f"거래량 {vol_ratio:.1f}x"
        )

        logger.info(f"[MeanReversion] 진입 신호: {symbol} - {reason}")

        signal = self.create_signal(
            symbol=symbol,
            side=OrderSide.BUY,
            strength=strength,
            price=current_price,
            score=score,
            reason=reason,
            target_price=target_price,
            stop_price=stop_price,
        )

        # 역추세 전략은 포지션 축소 (리스크 관리)
        signal.metadata["position_multiplier"] = self.mr_config.position_size_multiplier

        return signal

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
        평균회귀 전략은 RSI 과매수 청산만 자체 처리합니다.
        """
        price = float(current_price)
        entry_price = float(position.avg_price)

        if entry_price <= 0:
            return None

        pnl_pct = (price - entry_price) / entry_price * 100

        # RSI 과매수 진입 시 청산 (전략 고유 조건)
        rsi = indicators.get("rsi_14", 50)
        if rsi > 70 and pnl_pct > 0:
            return self.create_signal(
                symbol=symbol,
                side=OrderSide.SELL,
                strength=SignalStrength.NORMAL,
                price=current_price,
                score=75.0,
                reason=f"RSI 과매수 청산: RSI={rsi:.0f}, 수익 +{pnl_pct:.1f}%",
            )

        return None

    def _calculate_entry_score(
        self,
        rsi: float,
        decline_pct: float,
        vol_ratio: float,
        today_change: float
    ) -> float:
        """진입 점수 계산"""
        score = 0.0

        # RSI 깊이 (35점) - 낮을수록 좋음
        if rsi < 20:
            score += 35
        elif rsi < 25:
            score += 28
        elif rsi < 30:
            score += 20
        else:
            score += 10

        # 낙폭 (25점) - 적당히 빠진 게 좋음 (-10% ~ -20%)
        decline_abs = abs(decline_pct)
        if 10 <= decline_abs <= 20:
            score += 25
        elif 8 <= decline_abs <= 25:
            score += 18
        else:
            score += 10

        # 거래량 (20점) - 바닥 확인
        score += min(vol_ratio * 5, 20)

        # 오늘 반등 강도 (20점)
        if today_change >= 3:
            score += 20
        elif today_change >= 2:
            score += 15
        elif today_change >= 1:
            score += 10
        else:
            score += 5

        return min(score, 100.0)

    def _is_trading_time(self) -> bool:
        """거래 가능 시간 체크"""
        now = datetime.now()
        current_time = now.strftime("%H:%M")

        return self.mr_config.trading_start_time <= current_time <= self.mr_config.trading_end_time

    def calculate_score(self, symbol: str) -> float:
        """신호 점수 계산"""
        indicators = self.get_indicators(symbol)
        if not indicators:
            return 0.0

        rsi = indicators.get("rsi_14")
        if rsi is None:
            return 0.0  # RSI 미계산 시 점수 0
        change_3d = indicators.get("change_3d", 0)
        vol_ratio = indicators.get("vol_ratio", 1)
        change_1d = indicators.get("change_1d", 0)

        # RSI 조건 미충족
        if rsi > self.mr_config.max_rsi:
            return 0.0

        return self._calculate_entry_score(rsi, change_3d, vol_ratio, change_1d)

    def add_to_blacklist(self, symbol: str, reason: str = ""):
        """블랙리스트에 추가 (펀더멘털 문제 종목, TTL 적용)"""
        self._blacklist[symbol] = datetime.now()
        logger.info(f"[MeanReversion] 블랙리스트 추가: {symbol} - {reason} (TTL: {self._blacklist_ttl_hours}h)")

    def remove_from_blacklist(self, symbol: str):
        """블랙리스트에서 제거"""
        self._blacklist.pop(symbol, None)

    def get_oversold_stocks(self) -> Dict[str, Dict[str, Any]]:
        """현재 추적 중인 과매도 종목"""
        return self._oversold_stocks.copy()

    def clear_oversold_stocks(self):
        """과매도 종목 초기화"""
        self._oversold_stocks.clear()
