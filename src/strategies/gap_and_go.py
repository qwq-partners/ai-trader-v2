"""
AI Trading Bot v2 - 갭상승 추종 전략 (Gap & Go)

갭상승 후 눌림목에서 매수하여 추가 상승을 노리는 전략입니다.

전략 원리:
1. 장 시작 시 +2% 이상 갭상승 종목 탐지
2. 첫 30분 고가 형성 확인
3. 눌림목(고가 대비 조정)에서 매수
4. VWAP 지지 확인

적용 시간대:
- 정규장 초반 (09:00~10:30)
- 갭 형성 후 30분~1시간 이내
"""

from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from typing import Dict, List, Optional, Set, Any
from loguru import logger

from .base import BaseStrategy, StrategyConfig
from ..core.types import (
    Signal, Position,
    OrderSide, SignalStrength, StrategyType
)


@dataclass
class GapAndGoConfig(StrategyConfig):
    """갭상승 추종 전략 설정"""
    name: str = "GapAndGo"
    strategy_type: StrategyType = StrategyType.GAP_AND_GO

    # 갭 조건
    min_gap_pct: float = 2.0          # 최소 갭 상승률 (%)
    max_gap_pct: float = 10.0         # 최대 갭 (과열 방지)

    # 진입 조건
    pullback_pct: float = 1.0         # 눌림목 기준 (고가 대비 %)
    entry_delay_minutes: int = 30     # 갭 발생 후 진입 대기 시간
    min_volume_ratio: float = 2.0     # 최소 거래량 비율

    # VWAP 조건
    vwap_support_tolerance: float = 1.0  # VWAP 지지 허용 오차 (%) — 0.5→1.0: VWAP 이탈 허용 확대

    # 청산 조건
    stop_loss_pct: float = 1.5        # 손절 (갭 시작점 이탈)
    take_profit_pct: float = 4.0      # 익절
    trailing_stop_pct: float = 1.5    # 트레일링 스탑

    # 시간 제한
    entry_start_time: str = "09:20"   # 진입 시작 시간 — 09:30→09:20: entry_delay 축소 반영
    entry_end_time: str = "11:30"     # 진입 종료 시간 — 11:00→11:30: 기회 확대


class GapAndGoStrategy(BaseStrategy):
    """
    갭상승 추종 전략 (Gap & Go)

    장 시작 시 갭상승 종목을 찾아 눌림목에서 매수합니다.

    매수 조건:
    - 갭상승 +2% ~ +10%
    - 첫 30분 고가 형성 후 눌림목
    - 거래량 200% 이상
    - VWAP 지지 확인

    매도 조건:
    - 익절: +4%
    - 손절: -1.5% (또는 갭 시작점 이탈)
    - 트레일링 스탑: 고점 대비 -1.5%
    """

    def __init__(self, config: Optional[GapAndGoConfig] = None):
        config = config or GapAndGoConfig()
        super().__init__(config)
        self.gap_config = config

        # 갭상승 종목 추적
        self._gap_stocks: Dict[str, Dict[str, Any]] = {}
        # symbol -> {"gap_pct": float, "open_price": Decimal, "high_price": Decimal, "detected_at": datetime}

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

    async def _check_entry_signal(
        self,
        symbol: str,
        current_price: Decimal,
        indicators: Dict[str, float]
    ) -> Optional[Signal]:
        """진입 신호 체크"""
        # 시간대 체크
        if not self._is_entry_time():
            return None

        price = float(current_price)
        prev_close = indicators.get("prev_close", 0)
        open_price = indicators.get("open", price)

        if prev_close <= 0:
            return None

        # 갭 계산
        gap_pct = (open_price - prev_close) / prev_close * 100

        # 갭 조건 확인
        if gap_pct < self.gap_config.min_gap_pct:
            return None
        if gap_pct > self.gap_config.max_gap_pct:
            logger.debug(f"[Gap&Go] {symbol} 갭 과열 ({gap_pct:.1f}%)")
            return None

        # 갭 종목 추적
        if symbol not in self._gap_stocks:
            self._gap_stocks[symbol] = {
                "gap_pct": gap_pct,
                "open_price": Decimal(str(open_price)),
                "high_price": Decimal(str(open_price)),
                "detected_at": datetime.now(),
            }
            logger.info(f"[Gap&Go] 갭상승 감지: {symbol} +{gap_pct:.1f}%")
        else:
            # 고가 업데이트
            if current_price > self._gap_stocks[symbol]["high_price"]:
                self._gap_stocks[symbol]["high_price"] = current_price

        gap_info = self._gap_stocks[symbol]

        # 진입 대기 시간 체크
        elapsed = (datetime.now() - gap_info["detected_at"]).total_seconds() / 60
        if elapsed < self.gap_config.entry_delay_minutes:
            return None

        # 눌림목 체크
        high_price = float(gap_info["high_price"])
        pullback_pct = (high_price - price) / high_price * 100

        if pullback_pct < self.gap_config.pullback_pct:
            return None  # 아직 눌림이 부족

        if pullback_pct > self.gap_config.pullback_pct * 3:
            return None  # 너무 많이 빠짐 (갭 실패)

        # 거래량 조건
        vol_ratio = indicators.get("vol_ratio", 0)
        if vol_ratio < self.gap_config.min_volume_ratio:
            return None

        # VWAP 지지 확인 (있는 경우)
        vwap = indicators.get("vwap", 0)
        vwap_bonus = 0.0
        if vwap > 0:
            vwap_distance = (price - vwap) / vwap * 100
            if vwap_distance < -self.gap_config.vwap_support_tolerance:
                return None  # VWAP 아래로 이탈
            # VWAP 위에서 지지받는 경우 점수 보너스
            if 0 <= vwap_distance <= 1.0:
                vwap_bonus = 10.0  # VWAP 근접 지지 (최적)
            elif vwap_distance > 1.0:
                vwap_bonus = 5.0   # VWAP 위 (양호)

        # 신호 강도 결정
        if gap_pct >= 5:
            strength = SignalStrength.VERY_STRONG
        elif gap_pct >= 3:
            strength = SignalStrength.STRONG
        else:
            strength = SignalStrength.NORMAL

        # 점수 계산
        score = self._calculate_entry_score(gap_pct, pullback_pct, vol_ratio) + vwap_bonus

        # 손절가: 갭 시작점, VWAP, 또는 고정 % 중 현재가 미만인 후보 중 가장 높은 값
        gap_start = float(gap_info["open_price"])
        stop_by_gap = gap_start * 0.995  # 갭 시작점 -0.5%
        stop_by_pct = price * (1 - self.gap_config.stop_loss_pct / 100)
        stop_candidates = [stop_by_pct]  # 고정 %는 항상 현재가 미만
        if stop_by_gap < price:  # 갭 시작점이 현재가 미만일 때만 후보
            stop_candidates.append(stop_by_gap)
        if vwap > 0 and vwap < price:
            stop_by_vwap = vwap * 0.995  # VWAP -0.5%
            stop_candidates.append(stop_by_vwap)
        stop_price = Decimal(str(max(stop_candidates)))

        # 목표가
        target_price = Decimal(str(price * (1 + self.gap_config.take_profit_pct / 100)))

        reason = f"갭+{gap_pct:.1f}% 눌림 {pullback_pct:.1f}%, 거래량 {vol_ratio:.1f}x"

        logger.info(f"[Gap&Go] 진입 신호: {symbol} - {reason}")

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
        갭 전략은 갭 시작점 이탈만 자체 처리합니다.
        """
        price = float(current_price)

        # 갭 시작점 이탈 체크 (전략 고유 청산)
        if symbol in self._gap_stocks:
            gap_start = float(self._gap_stocks[symbol]["open_price"])
            if price < gap_start * 0.99:  # 갭 시작점 -1%
                return self.create_signal(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    strength=SignalStrength.STRONG,
                    price=current_price,
                    score=90.0,
                    reason=f"갭 시작점 이탈: {price:.0f} < {gap_start:.0f}",
                )

        return None

    def _calculate_entry_score(
        self,
        gap_pct: float,
        pullback_pct: float,
        vol_ratio: float
    ) -> float:
        """진입 점수 계산"""
        score = 0.0

        # 갭 크기 (35점)
        if 3 <= gap_pct <= 6:
            score += 35  # 최적 갭
        elif 2 <= gap_pct <= 8:
            score += 25
        else:
            score += 15

        # 눌림목 깊이 (35점)
        # 1~2% 눌림이 최적
        if 1 <= pullback_pct <= 2:
            score += 35
        elif 0.5 <= pullback_pct <= 3:
            score += 25
        else:
            score += 10

        # 거래량 (30점)
        score += min(vol_ratio * 5, 30)

        return min(score, 100.0)

    def _is_entry_time(self) -> bool:
        """진입 가능 시간 체크"""
        now = datetime.now()
        current_time = now.strftime("%H:%M")

        return self.gap_config.entry_start_time <= current_time <= self.gap_config.entry_end_time

    def calculate_score(self, symbol: str) -> float:
        """신호 점수 계산"""
        indicators = self.get_indicators(symbol)
        if not indicators:
            return 0.0

        prev_close = indicators.get("prev_close", 0)
        open_price = indicators.get("open", 0)
        vol_ratio = indicators.get("vol_ratio", 0)

        if prev_close <= 0:
            return 0.0

        gap_pct = (open_price - prev_close) / prev_close * 100

        if gap_pct < self.gap_config.min_gap_pct:
            return 0.0

        return self._calculate_entry_score(gap_pct, 1.0, vol_ratio)

    def get_gap_stocks(self) -> Dict[str, Dict[str, Any]]:
        """현재 추적 중인 갭상승 종목"""
        return self._gap_stocks.copy()

    def clear_gap_stocks(self):
        """갭상승 종목 초기화 (매일 리셋용)"""
        self._gap_stocks.clear()
        logger.info("[Gap&Go] 갭 종목 리스트 초기화")
