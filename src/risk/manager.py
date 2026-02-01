"""
AI Trading Bot v2 - 리스크 관리자

포지션 크기 계산, 손절/익절 관리, 일일 손실 제한
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
from loguru import logger

from ..core.types import (
    Order, Position, Portfolio, Signal, RiskMetrics, RiskConfig,
    OrderSide, SignalStrength
)
from ..core.event import (
    SignalEvent, FillEvent, RiskAlertEvent, StopTriggeredEvent,
    Event, EventType
)


@dataclass
class DailyStats:
    """일일 거래 통계"""
    date: date = field(default_factory=date.today)
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    consecutive_losses: int = 0
    peak_equity: Decimal = Decimal("0")


class RiskManager:
    """
    리스크 관리자

    주요 기능:
    - 포지션 크기 계산
    - 손절/익절 가격 계산
    - 일일 손실 제한 체크
    - 최대 포지션 수 제한
    - 연속 손실 관리
    """

    def __init__(self, config: RiskConfig, initial_capital: Decimal):
        self.config = config
        self.initial_capital = initial_capital

        # 리스크 메트릭스
        self.metrics = RiskMetrics()

        # 일일 통계
        self.daily_stats = DailyStats(peak_equity=initial_capital)

        # 경고 임계값
        self._warn_threshold_pct = config.daily_max_loss_pct * 0.7  # 70%에서 경고

        max_pos = self.get_effective_max_positions(initial_capital)
        logger.info(
            f"RiskManager 초기화: 일일손실한도={config.daily_max_loss_pct}%, "
            f"최대포지션={max_pos}개 (설정={config.max_positions}, 동적={'ON' if config.dynamic_max_positions else 'OFF'}), "
            f"최대비율={config.max_position_pct}%, 최소금액={config.min_position_value:,}원"
        )

    def get_effective_max_positions(self, equity: Decimal = None) -> int:
        """
        자산 규모 기반 실효 최대 포지션 수

        dynamic_max_positions가 True이면:
          - 종목당 최소 금액(min_position_value)을 기준으로 수용 가능한 종목 수 계산
          - config.max_positions를 상한으로 적용
        """
        if not self.config.dynamic_max_positions:
            return self.config.max_positions

        if equity is None:
            equity = self.initial_capital
        equity_f = float(equity) if isinstance(equity, Decimal) else float(equity)

        if equity_f <= 0 or self.config.min_position_value <= 0:
            return self.config.max_positions

        # 가용 자산 = 총 자산 - 현금 예비금
        investable = equity_f * (1 - self.config.min_cash_reserve_pct / 100)
        # 종목당 목표 금액 = base_position_pct 기준
        target_per_position = equity_f * (self.config.base_position_pct / 100)
        # 최소 금액보다 작으면 최소 금액 사용
        per_position = max(target_per_position, self.config.min_position_value)

        dynamic_max = max(3, int(investable / per_position))
        # config 상한 적용 (config.max_positions를 ceiling으로)
        return min(dynamic_max, self.config.max_positions)

    # ============================================================
    # 포지션 크기 계산
    # ============================================================

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio: Portfolio,
        current_price: Decimal
    ) -> int:
        """
        신호 강도에 따른 포지션 크기 계산

        Args:
            signal: 매매 신호
            portfolio: 현재 포트폴리오
            current_price: 현재가

        Returns:
            매수 수량
        """
        if current_price <= 0:
            return 0

        equity = portfolio.total_equity
        if equity <= 0:
            return 0

        # 기본 포지션 비율 (config에서 가져옴, 기본 10%)
        base_pct = Decimal(str(self.config.base_position_pct / 100))

        # 신호 강도에 따른 조정
        strength_multiplier = {
            SignalStrength.VERY_STRONG: Decimal("2.0"),
            SignalStrength.STRONG: Decimal("1.5"),
            SignalStrength.NORMAL: Decimal("1.0"),
            SignalStrength.WEAK: Decimal("0.5"),
        }.get(signal.strength, Decimal("1.0"))

        # 최종 포지션 비율
        position_pct = min(
            base_pct * strength_multiplier,
            Decimal(str(self.config.max_position_pct / 100))
        )

        # 포지션 금액
        position_value = equity * position_pct

        # 가용 현금 체크
        available = self._get_available_cash(portfolio)
        if position_value > available:
            position_value = available

        # 최소 포지션 금액 체크
        min_val = Decimal(str(self.config.min_position_value))
        if position_value < min_val:
            logger.debug(
                f"포지션 금액 미달: {position_value:,.0f}원 < 최소 {min_val:,.0f}원"
            )
            return 0

        # 수량 계산
        quantity = int(position_value / current_price)

        logger.debug(
            f"포지션 크기 계산: 강도={signal.strength.value}, "
            f"비율={float(position_pct)*100:.1f}%, 금액={position_value:,.0f}원, "
            f"수량={quantity}주"
        )

        return max(quantity, 0)

    def _get_available_cash(self, portfolio: Portfolio) -> Decimal:
        """가용 현금 (최소 예비금 제외)"""
        min_reserve = portfolio.total_equity * Decimal(str(self.config.min_cash_reserve_pct / 100))
        return max(portfolio.cash - min_reserve, Decimal("0"))

    # ============================================================
    # 손절/익절 가격 계산
    # ============================================================

    def calculate_stop_loss(
        self,
        entry_price: Decimal,
        side: OrderSide,
        volatility: Optional[float] = None
    ) -> Decimal:
        """
        손절 가격 계산

        Args:
            entry_price: 진입가
            side: 매매 방향
            volatility: 변동성 (선택, ATR 기반)

        Returns:
            손절가
        """
        stop_pct = self.config.default_stop_loss_pct / 100

        # 변동성 기반 조정 (선택적)
        if volatility and volatility > 5:
            # 변동성 높으면 손절폭 확대
            stop_pct = min(stop_pct * 1.5, 0.05)  # 최대 5%

        if side == OrderSide.BUY:
            return entry_price * (1 - Decimal(str(stop_pct)))
        else:
            return entry_price * (1 + Decimal(str(stop_pct)))

    def calculate_take_profit(
        self,
        entry_price: Decimal,
        side: OrderSide,
        signal_strength: SignalStrength = SignalStrength.NORMAL
    ) -> Decimal:
        """
        익절 가격 계산

        Args:
            entry_price: 진입가
            side: 매매 방향
            signal_strength: 신호 강도

        Returns:
            익절가
        """
        base_pct = self.config.default_take_profit_pct / 100

        # 신호 강도에 따른 조정
        if signal_strength == SignalStrength.VERY_STRONG:
            target_pct = base_pct * 1.5  # 강한 신호면 목표 상향
        elif signal_strength == SignalStrength.WEAK:
            target_pct = base_pct * 0.7  # 약한 신호면 목표 하향
        else:
            target_pct = base_pct

        if side == OrderSide.BUY:
            return entry_price * (1 + Decimal(str(target_pct)))
        else:
            return entry_price * (1 - Decimal(str(target_pct)))

    def calculate_trailing_stop(
        self,
        highest_price: Decimal,
        side: OrderSide
    ) -> Decimal:
        """트레일링 스탑 가격 계산"""
        trail_pct = self.config.trailing_stop_pct / 100

        if side == OrderSide.BUY:
            return highest_price * (1 - Decimal(str(trail_pct)))
        else:
            return highest_price * (1 + Decimal(str(trail_pct)))

    # ============================================================
    # 거래 가능 여부 체크
    # ============================================================

    def can_open_position(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        price: Decimal,
        portfolio: Portfolio
    ) -> Tuple[bool, str]:
        """
        포지션 오픈 가능 여부 체크

        Returns:
            (가능 여부, 거부 사유)
        """
        # 1. 일일 손실 한도 체크
        if self._is_daily_loss_limit_hit(portfolio):
            return False, f"일일 손실 한도 도달 ({self.config.daily_max_loss_pct}%)"

        # 2. 일일 거래 횟수 체크
        if self.daily_stats.trades >= self.config.daily_max_trades:
            return False, f"일일 거래 횟수 한도 ({self.config.daily_max_trades}회)"

        # 3. 최대 포지션 수 체크 (동적 계산)
        effective_max = self.get_effective_max_positions(portfolio.total_equity)
        if symbol not in portfolio.positions:
            if len(portfolio.positions) >= effective_max:
                return False, f"최대 포지션 수 도달 ({len(portfolio.positions)}/{effective_max}개)"

        # 4. 포지션 크기 체크
        position_value = price * quantity
        max_value = portfolio.total_equity * Decimal(str(self.config.max_position_pct / 100))
        if position_value > max_value:
            return False, f"포지션 크기 초과 ({position_value:,.0f} > {max_value:,.0f})"

        # 5. 현금 체크 (매수 시)
        if side == OrderSide.BUY:
            required = position_value * Decimal("1.001")  # 수수료 여유
            available = self._get_available_cash(portfolio)
            if required > available:
                return False, f"현금 부족 ({available:,.0f} < {required:,.0f})"

        # 6. 연속 손실 체크
        if self.daily_stats.consecutive_losses >= 5:
            return False, f"연속 손실 ({self.daily_stats.consecutive_losses}회) - 거래 중단"

        return True, ""

    def _is_daily_loss_limit_hit(self, portfolio: Portfolio) -> bool:
        """일일 손실 한도 도달 여부 (현재 자산 기준)"""
        equity = portfolio.total_equity
        if equity <= 0:
            return False

        daily_pnl_pct = float(portfolio.daily_pnl / equity * 100)
        return daily_pnl_pct <= -self.config.daily_max_loss_pct

    # ============================================================
    # 이벤트 처리
    # ============================================================

    def on_fill(self, fill_event: FillEvent, portfolio: Portfolio) -> List[Event]:
        """체결 이벤트 처리"""
        events = []

        # 일일 통계 업데이트
        self.daily_stats.trades += 1

        # 일일 손실 체크 (현재 자산 기준)
        equity = portfolio.total_equity
        daily_pnl_pct = float(portfolio.daily_pnl / equity * 100) if equity > 0 else 0.0

        # 경고 임계값 체크
        if daily_pnl_pct <= -self._warn_threshold_pct:
            events.append(RiskAlertEvent(
                source="risk_manager",
                alert_type="daily_loss_warning",
                message=f"일일 손실 경고: {daily_pnl_pct:.1f}%",
                current_value=daily_pnl_pct,
                threshold=-self._warn_threshold_pct,
                action="warn"
            ))

        # 한도 도달 체크
        if daily_pnl_pct <= -self.config.daily_max_loss_pct:
            events.append(RiskAlertEvent(
                source="risk_manager",
                alert_type="daily_loss_limit",
                message=f"일일 손실 한도 도달: {daily_pnl_pct:.1f}%",
                current_value=daily_pnl_pct,
                threshold=-self.config.daily_max_loss_pct,
                action="block"
            ))
            self.metrics.is_daily_loss_limit_hit = True
            self.metrics.can_trade = False

        # 메트릭스 업데이트
        self.metrics.daily_loss = portfolio.daily_pnl
        self.metrics.daily_loss_pct = daily_pnl_pct
        self.metrics.daily_trades = self.daily_stats.trades

        return events

    def check_position_stops(
        self,
        position: Position,
        current_price: Decimal
    ) -> Optional[StopTriggeredEvent]:
        """포지션 손절/익절 체크"""
        if position.quantity <= 0:
            return None

        price = current_price
        entry_price = position.avg_price

        if entry_price <= 0:
            return None

        pnl_pct = float((price - entry_price) / entry_price * 100)

        # 손절 체크
        if position.stop_loss and price <= position.stop_loss:
            return StopTriggeredEvent(
                source="risk_manager",
                symbol=position.symbol,
                trigger_type="stop_loss",
                trigger_price=position.stop_loss,
                current_price=price,
                position_side="long"
            )

        # 익절 체크
        if position.take_profit and price >= position.take_profit:
            return StopTriggeredEvent(
                source="risk_manager",
                symbol=position.symbol,
                trigger_type="take_profit",
                trigger_price=position.take_profit,
                current_price=price,
                position_side="long"
            )

        # 트레일링 스탑 체크
        if position.trailing_stop_pct and position.highest_price:
            trailing_stop = self.calculate_trailing_stop(
                position.highest_price, OrderSide.BUY
            )
            if price <= trailing_stop:
                return StopTriggeredEvent(
                    source="risk_manager",
                    symbol=position.symbol,
                    trigger_type="trailing_stop",
                    trigger_price=trailing_stop,
                    current_price=price,
                    position_side="long"
                )

        return None

    # ============================================================
    # 유틸리티
    # ============================================================

    def reset_daily_stats(self):
        """일일 통계 초기화"""
        self.daily_stats = DailyStats(peak_equity=self.initial_capital)
        self.metrics = RiskMetrics()
        self.metrics.can_trade = True
        logger.info("일일 리스크 통계 초기화")

    def record_trade_result(self, pnl: Decimal):
        """거래 결과 기록"""
        if pnl > 0:
            self.daily_stats.wins += 1
            self.daily_stats.consecutive_losses = 0
        else:
            self.daily_stats.losses += 1
            self.daily_stats.consecutive_losses += 1

        self.daily_stats.total_pnl += pnl
        self.metrics.consecutive_losses = self.daily_stats.consecutive_losses

    def get_risk_summary(self) -> Dict[str, Any]:
        """리스크 요약"""
        return {
            "can_trade": self.metrics.can_trade,
            "daily_loss_pct": self.metrics.daily_loss_pct,
            "daily_trades": self.daily_stats.trades,
            "consecutive_losses": self.daily_stats.consecutive_losses,
            "win_rate": (
                self.daily_stats.wins / self.daily_stats.trades * 100
                if self.daily_stats.trades > 0 else 0
            ),
            "total_pnl": float(self.daily_stats.total_pnl),
        }
