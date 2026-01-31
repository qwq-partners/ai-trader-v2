"""
AI Trading Bot v2 - 분할 익절/청산 관리자

포지션별로 분할 익절을 관리합니다.

분할 익절 전략:
1. +3% 도달 → 50% 익절
2. +5% 도달 → 25% 추가 익절
3. 나머지 25% → 트레일링 스탑으로 수익 극대화

수수료 포함 계산으로 실제 순수익 기준 청산
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from enum import Enum
from loguru import logger

from ..core.types import Position, OrderSide, Signal, SignalStrength
from ..utils.fee_calculator import FeeCalculator, get_fee_calculator


class ExitStage(Enum):
    """익절 단계"""
    NONE = "none"               # 익절 전
    FIRST = "first"             # 1차 익절 (50%)
    SECOND = "second"           # 2차 익절 (25%)
    TRAILING = "trailing"       # 트레일링 (나머지 25%)


@dataclass
class ExitConfig:
    """청산 설정"""
    # 분할 익절 설정
    enable_partial_exit: bool = True

    # 1차 익절 (50%)
    first_exit_pct: float = 3.0       # 목표 수익률 (%)
    first_exit_ratio: float = 0.5     # 청산 비율 (50%)

    # 2차 익절 (25%)
    second_exit_pct: float = 5.0      # 목표 수익률 (%)
    second_exit_ratio: float = 0.5    # 남은 물량의 50% = 전체의 25%

    # 손절
    stop_loss_pct: float = 2.0        # 최대 손실률 (%)

    # 트레일링 스탑
    trailing_stop_pct: float = 1.5    # 고점 대비 하락률 (%)
    trailing_activate_pct: float = 2.0  # 트레일링 활성화 수익률

    # 수수료 포함 계산
    include_fees: bool = True


@dataclass
class PositionExitState:
    """포지션별 청산 상태"""
    symbol: str
    entry_price: Decimal
    original_quantity: int
    remaining_quantity: int
    current_stage: ExitStage = ExitStage.NONE
    highest_price: Decimal = Decimal("0")
    total_realized_pnl: Decimal = Decimal("0")
    exit_history: List[Dict] = field(default_factory=list)
    # 전략별 청산 파라미터 (None이면 글로벌 ExitConfig 사용)
    stop_loss_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None


class ExitManager:
    """
    분할 익절/청산 관리자

    수수료를 포함한 순수익 기준으로 분할 익절을 관리합니다.
    """

    def __init__(self, config: Optional[ExitConfig] = None):
        self.config = config or ExitConfig()
        self.fee_calc = get_fee_calculator()

        # 포지션별 청산 상태
        self._states: Dict[str, PositionExitState] = {}

    def register_position(
        self,
        position: Position,
        stop_loss_pct: Optional[float] = None,
        trailing_stop_pct: Optional[float] = None,
    ):
        """
        포지션 등록

        Args:
            position: 포지션 객체
            stop_loss_pct: 전략별 손절 비율 (None이면 글로벌 config 사용)
            trailing_stop_pct: 전략별 트레일링 비율 (None이면 글로벌 config 사용)
        """
        if position.symbol in self._states:
            # 추가매수: 수량/평단가 변경 반영
            state = self._states[position.symbol]
            if position.quantity != state.remaining_quantity:
                old_qty = state.remaining_quantity
                state.entry_price = position.avg_price
                state.original_quantity = position.quantity
                state.remaining_quantity = position.quantity
                logger.debug(
                    f"[ExitManager] 포지션 업데이트: {position.symbol} "
                    f"{old_qty}주 → {position.quantity}주, "
                    f"평단가={position.avg_price:,.0f}원"
                )
            return

        self._states[position.symbol] = PositionExitState(
            symbol=position.symbol,
            entry_price=position.avg_price,
            original_quantity=position.quantity,
            remaining_quantity=position.quantity,
            highest_price=position.current_price or position.avg_price,
            stop_loss_pct=stop_loss_pct,
            trailing_stop_pct=trailing_stop_pct,
        )
        logger.debug(
            f"[ExitManager] 포지션 등록: {position.symbol} "
            f"(SL={stop_loss_pct or self.config.stop_loss_pct}%, "
            f"TS={trailing_stop_pct or self.config.trailing_stop_pct}%)"
        )

    def update_price(self, symbol: str, current_price: Decimal) -> Optional[Tuple[str, int, str]]:
        """
        가격 업데이트 및 청산 신호 확인

        Returns:
            (action, quantity, reason) 또는 None
            action: "sell_partial" | "sell_all" | None
        """
        if symbol not in self._states:
            return None

        state = self._states[symbol]

        if state.remaining_quantity <= 0:
            return None

        # 고가 업데이트
        if current_price > state.highest_price:
            state.highest_price = current_price

        # 순손익률 계산 (수수료 포함)
        if self.config.include_fees:
            _, net_pnl_pct = self.fee_calc.calculate_net_pnl(
                state.entry_price, current_price, state.remaining_quantity
            )
        else:
            net_pnl_pct = float((current_price - state.entry_price) / state.entry_price * 100)

        # 1. 손절 체크 (최우선, 전략별 파라미터 우선)
        sl_pct = state.stop_loss_pct or self.config.stop_loss_pct
        if net_pnl_pct <= -sl_pct:
            return self._create_exit(
                state, "sell_all", state.remaining_quantity,
                f"손절: {net_pnl_pct:.2f}% (수수료 포함)"
            )

        # 2. 분할 익절
        if self.config.enable_partial_exit:
            exit_signal = self._check_partial_exit(state, current_price, net_pnl_pct)
            if exit_signal:
                return exit_signal

        # 3. 트레일링 스탑 (수익 구간에서만, 전략별 파라미터 우선)
        if net_pnl_pct >= self.config.trailing_activate_pct:
            trailing_pct = float((current_price - state.highest_price) / state.highest_price * 100)
            ts_pct = state.trailing_stop_pct or self.config.trailing_stop_pct
            if trailing_pct <= -ts_pct:
                return self._create_exit(
                    state, "sell_all", state.remaining_quantity,
                    f"트레일링: 고점 대비 {trailing_pct:.2f}%"
                )

        return None

    def _check_partial_exit(
        self,
        state: PositionExitState,
        current_price: Decimal,
        net_pnl_pct: float
    ) -> Optional[Tuple[str, int, str]]:
        """분할 익절 체크"""

        # 1차 익절: 아직 1차 익절 전이고, 수익률 도달
        if state.current_stage == ExitStage.NONE:
            if net_pnl_pct >= self.config.first_exit_pct:
                exit_qty = int(state.original_quantity * self.config.first_exit_ratio)
                exit_qty = min(exit_qty, state.remaining_quantity)

                # 소량 보유 시 (1주 등) 분할 불가 → 전량 매도
                if exit_qty <= 0:
                    exit_qty = state.remaining_quantity

                state.current_stage = ExitStage.FIRST
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"1차 익절 ({self.config.first_exit_ratio*100:.0f}%): {net_pnl_pct:.2f}%"
                )

        # 2차 익절: 1차 완료 후, 추가 수익률 도달
        elif state.current_stage == ExitStage.FIRST:
            if net_pnl_pct >= self.config.second_exit_pct:
                exit_qty = int(state.remaining_quantity * self.config.second_exit_ratio)
                exit_qty = min(exit_qty, state.remaining_quantity)

                # 소량 잔여 시 전량 매도
                if exit_qty <= 0:
                    exit_qty = state.remaining_quantity

                state.current_stage = ExitStage.TRAILING
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"2차 익절: {net_pnl_pct:.2f}%"
                )

        return None

    def _create_exit(
        self,
        state: PositionExitState,
        action: str,
        quantity: int,
        reason: str
    ) -> Tuple[str, int, str]:
        """청산 신호 생성"""
        # 히스토리 기록
        state.exit_history.append({
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "quantity": quantity,
            "reason": reason,
            "remaining_before": state.remaining_quantity,
        })

        # 수량 업데이트는 실제 체결 후에 처리
        return (action, quantity, reason)

    def on_fill(self, symbol: str, sold_quantity: int, fill_price: Decimal):
        """체결 후 상태 업데이트"""
        if symbol not in self._states:
            return

        state = self._states[symbol]

        # 실현 손익 계산
        pnl, _ = self.fee_calc.calculate_net_pnl(
            state.entry_price, fill_price, sold_quantity
        )
        state.total_realized_pnl += pnl

        # 남은 수량 업데이트
        state.remaining_quantity -= sold_quantity

        logger.info(
            f"[ExitManager] {symbol} 청산: {sold_quantity}주 @ {fill_price:,.0f}원, "
            f"실현손익: {pnl:+,.0f}원, 남은 수량: {state.remaining_quantity}주"
        )

        # 완전 청산 시 상태 제거
        if state.remaining_quantity <= 0:
            total_pnl = state.total_realized_pnl
            del self._states[symbol]
            logger.info(f"[ExitManager] {symbol} 완전 청산, 총 실현손익: {total_pnl:+,.0f}원")

    def get_state(self, symbol: str) -> Optional[PositionExitState]:
        """포지션 청산 상태 조회"""
        return self._states.get(symbol)

    def get_all_states(self) -> Dict[str, PositionExitState]:
        """모든 포지션 상태 조회"""
        return self._states.copy()


# 전역 인스턴스
_exit_manager: Optional[ExitManager] = None


def get_exit_manager() -> ExitManager:
    """전역 청산 관리자"""
    global _exit_manager
    if _exit_manager is None:
        _exit_manager = ExitManager()
    return _exit_manager
