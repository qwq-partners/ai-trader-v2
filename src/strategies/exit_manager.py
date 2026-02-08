"""
AI Trading Bot v2 - 분할 익절/청산 관리자

포지션별로 분할 익절을 관리합니다.

분할 익절 전략 (3단계):
1. +3.0% 도달 → 30% 익절 (빠른 수익 확보)
2. +6.0% 도달 → 28% 추가 익절 (중간 목표)
3. +10.0% 도달 → 21% 추가 익절
4. 나머지 21% → 트레일링 스탑으로 수익 극대화

트레일링 스탑:
- 활성화: +3.0% 이상 수익 시
- 청산: 고점 대비 -2.5% 하락 시

ATR 기반 동적 손절:
- 변동성 낮음(ATR 1%) → 3.0% 손절
- 변동성 보통(ATR 2%) → 4.0% 손절
- 변동성 높음(ATR 3%+) → 6.0% 손절 (상한)

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
from ..indicators.atr import calculate_atr, calculate_dynamic_stop_loss


class ExitStage(Enum):
    """익절 단계"""
    NONE = "none"               # 익절 전
    FIRST = "first"             # 1차 익절 (30%)
    SECOND = "second"           # 2차 익절 (28%)
    THIRD = "third"             # 3차 익절 (21%)
    TRAILING = "trailing"       # 트레일링 (나머지 21%)


@dataclass
class ExitConfig:
    """청산 설정"""
    # 분할 익절 설정
    enable_partial_exit: bool = True

    # 1차 익절 (30%) — 빠른 수익 확보
    first_exit_pct: float = 3.0       # 목표 수익률 (%) — 빠른 수익 확정
    first_exit_ratio: float = 0.30    # 청산 비율 (30%)

    # 2차 익절 (28% 추가 = 전체 58%)
    second_exit_pct: float = 6.0      # 목표 수익률 (%)
    second_exit_ratio: float = 0.40   # 남은 70%의 40% = 전체의 28%

    # 3차 익절 (21% 추가 = 전체 79%)
    third_exit_pct: float = 10.0      # 목표 수익률 (%)
    third_exit_ratio: float = 0.5     # 남은 42%의 50% = 전체의 21%

    # 손절 — ATR 기반 동적 손절
    stop_loss_pct: float = 4.0        # 기본 손실률 (%) — 손실 크기 축소
    enable_dynamic_stop: bool = True  # ATR 기반 동적 손절 활성화
    atr_multiplier: float = 2.0       # ATR 배수
    min_stop_pct: float = 3.0         # 최소 손절폭 (%)
    max_stop_pct: float = 6.0         # 최대 손절폭 (%)

    # 트레일링 스탑
    trailing_stop_pct: float = 2.5    # 고점 대비 하락률 (%)
    trailing_activate_pct: float = 3.0  # 트레일링 활성화 수익률 (%) — 1차 익절과 동시 활성화

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
    # ATR 기반 동적 손절
    atr_pct: Optional[float] = None
    dynamic_stop_pct: Optional[float] = None


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
        price_history: Optional[Dict[str, List[Decimal]]] = None,
    ):
        """
        포지션 등록

        Args:
            position: 포지션 객체
            stop_loss_pct: 전략별 손절 비율 (None이면 글로벌 config 사용)
            trailing_stop_pct: 전략별 트레일링 비율 (None이면 글로벌 config 사용)
            price_history: 가격 히스토리 {"high": [...], "low": [...], "close": [...]}
                           ATR 계산용, None이면 고정 손절 사용
        """
        if position.symbol in self._states:
            # 추가매수: 수량/평단가 변경 반영
            state = self._states[position.symbol]
            if position.quantity != state.remaining_quantity:
                old_qty = state.remaining_quantity
                state.entry_price = position.avg_price
                state.original_quantity = position.quantity
                state.remaining_quantity = position.quantity
                # 추가매수 시 익절 단계 초기화 및 고가 재계산
                state.current_stage = ExitStage.NONE
                state.highest_price = position.current_price or position.avg_price
                logger.debug(
                    f"[ExitManager] 포지션 업데이트(추가매수): {position.symbol} "
                    f"{old_qty}주 → {position.quantity}주, "
                    f"평단가={position.avg_price:,.0f}원, "
                    f"stage→NONE, highest→{state.highest_price:,.0f}원"
                )
            return

        # ATR 계산 및 동적 손절 설정
        atr_pct = None
        dynamic_stop = None
        if self.config.enable_dynamic_stop and price_history:
            try:
                highs = price_history.get("high", [])
                lows = price_history.get("low", [])
                closes = price_history.get("close", [])

                if highs and lows and closes:
                    atr_pct = calculate_atr(highs, lows, closes, period=14)
                    if atr_pct:
                        dynamic_stop = calculate_dynamic_stop_loss(
                            atr_pct,
                            min_stop=self.config.min_stop_pct,
                            max_stop=self.config.max_stop_pct,
                            multiplier=self.config.atr_multiplier
                        )
                        logger.info(
                            f"[ExitManager] {position.symbol} ATR 기반 손절: "
                            f"ATR={atr_pct:.2f}% → 손절={dynamic_stop:.2f}%"
                        )
            except Exception as e:
                logger.warning(f"[ExitManager] {position.symbol} ATR 계산 실패: {e}")

        # 현재 수익률 기반으로 초기 단계 결정 (재시작/재등록 시)
        initial_stage = ExitStage.NONE
        current_price = position.current_price or position.avg_price
        if position.avg_price and position.avg_price > 0 and current_price > position.avg_price:
            pnl_pct = float((current_price - position.avg_price) / position.avg_price * 100)
            if pnl_pct >= self.config.third_exit_pct:
                initial_stage = ExitStage.TRAILING
                logger.info(
                    f"[ExitManager] {position.symbol} 수익률 +{pnl_pct:.1f}% → "
                    f"트레일링 단계로 등록 (고점={current_price:,.0f}원)"
                )
            elif pnl_pct >= self.config.second_exit_pct:
                initial_stage = ExitStage.THIRD
                logger.info(
                    f"[ExitManager] {position.symbol} 수익률 +{pnl_pct:.1f}% → "
                    f"3차 익절 완료 단계로 등록"
                )
            elif pnl_pct >= self.config.first_exit_pct:
                initial_stage = ExitStage.FIRST
                logger.info(
                    f"[ExitManager] {position.symbol} 수익률 +{pnl_pct:.1f}% → "
                    f"1차 익절 완료 단계로 등록"
                )

        self._states[position.symbol] = PositionExitState(
            symbol=position.symbol,
            entry_price=position.avg_price,
            original_quantity=position.quantity,
            remaining_quantity=position.quantity,
            current_stage=initial_stage,
            highest_price=current_price,
            stop_loss_pct=stop_loss_pct,
            trailing_stop_pct=trailing_stop_pct,
            atr_pct=atr_pct,
            dynamic_stop_pct=dynamic_stop,
        )

        effective_stop = dynamic_stop or stop_loss_pct or self.config.stop_loss_pct
        logger.debug(
            f"[ExitManager] 포지션 등록: {position.symbol} "
            f"(SL={effective_stop:.2f}%, "
            f"TS={trailing_stop_pct or self.config.trailing_stop_pct}%, "
            f"stage={initial_stage.value})"
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

        # 순손익률 계산 (수수료 포함, float로 통일)
        if self.config.include_fees:
            _, net_pnl_pct = self.fee_calc.calculate_net_pnl(
                state.entry_price, current_price, state.remaining_quantity
            )
            net_pnl_pct = float(net_pnl_pct)
        else:
            net_pnl_pct = float((current_price - state.entry_price) / state.entry_price * 100)

        # 1. 손절 체크 (최우선, 동적 손절 → 전략별 → 글로벌 순서)
        sl_pct = state.dynamic_stop_pct or state.stop_loss_pct or self.config.stop_loss_pct
        if net_pnl_pct <= -sl_pct:
            atr_info = f", ATR={state.atr_pct:.2f}%" if state.atr_pct else ""
            return self._create_exit(
                state, "sell_all", state.remaining_quantity,
                f"손절: {net_pnl_pct:.2f}% (SL={sl_pct:.2f}%{atr_info})"
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
        """분할 익절 체크 (3단계)"""

        # 1차 익절: +2% 도달 → 25% 매도
        if state.current_stage == ExitStage.NONE:
            if net_pnl_pct >= self.config.first_exit_pct:
                exit_qty = max(1, int(state.original_quantity * self.config.first_exit_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                state.current_stage = ExitStage.FIRST
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"1차 익절 ({self.config.first_exit_ratio*100:.0f}%): {net_pnl_pct:.2f}%"
                )

        # 2차 익절: +4% 도달 → 35% 추가 매도
        elif state.current_stage == ExitStage.FIRST:
            if net_pnl_pct >= self.config.second_exit_pct:
                exit_qty = max(1, int(state.remaining_quantity * self.config.second_exit_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                state.current_stage = ExitStage.SECOND
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"2차 익절 ({self.config.second_exit_ratio*100:.0f}%): {net_pnl_pct:.2f}%"
                )

        # 3차 익절: +6% 도달 → 20% 추가 매도
        elif state.current_stage == ExitStage.SECOND:
            if net_pnl_pct >= self.config.third_exit_pct:
                exit_qty = max(1, int(state.remaining_quantity * self.config.third_exit_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                state.current_stage = ExitStage.THIRD
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"3차 익절 ({self.config.third_exit_ratio*100:.0f}%): {net_pnl_pct:.2f}%"
                )

        # 3차 익절 완료 후 트레일링으로 전환
        elif state.current_stage == ExitStage.THIRD:
            # 3차 익절 후 일정 수익률 이상이면 트레일링 단계로
            if net_pnl_pct >= self.config.third_exit_pct + 1.0:
                state.current_stage = ExitStage.TRAILING
                logger.info(
                    f"[ExitManager] {state.symbol} 트레일링 단계 진입 "
                    f"(+{net_pnl_pct:.2f}%, 고점={state.highest_price:,.0f}원)"
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

        # 남은 수량 업데이트 (과다 체결 방어) — PnL 계산 전에 보정
        if sold_quantity > state.remaining_quantity:
            logger.warning(
                f"[ExitManager] {symbol} 매도수량({sold_quantity}) > 보유수량({state.remaining_quantity}), 보정"
            )
            sold_quantity = state.remaining_quantity

        # 실현 손익 계산 (보정된 수량 기준)
        pnl, _ = self.fee_calc.calculate_net_pnl(
            state.entry_price, fill_price, sold_quantity
        )
        state.total_realized_pnl += pnl
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

    def remove_position(self, symbol: str) -> bool:
        """포지션 상태 제거 (유령 포지션 정리용)

        Returns:
            bool: 제거 성공 여부
        """
        if symbol in self._states:
            del self._states[symbol]
            logger.debug(f"[ExitManager] 포지션 상태 제거: {symbol}")
            return True
        return False


# 전역 인스턴스
_exit_manager: Optional[ExitManager] = None


def get_exit_manager() -> ExitManager:
    """전역 청산 관리자"""
    global _exit_manager
    if _exit_manager is None:
        _exit_manager = ExitManager()
    return _exit_manager
