"""
AI Trading Bot v2 - 리스크 관리자

포지션 크기 계산, 손절/익절 관리, 일일 손실 제한
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
from loguru import logger
import json
from pathlib import Path

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

        # 일일 손익 저장 경로
        cache_dir = Path.home() / ".cache" / "ai_trader"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._daily_stats_path = cache_dir / "daily_stats.json"

        # 리스크 메트릭스
        self.metrics = RiskMetrics()

        # 일일 통계
        self.daily_stats = DailyStats(peak_equity=initial_capital)

        # 일일 손익 로드 (프로세스 재시작 시)
        self._load_daily_stats()

        # 경고 임계값
        self._warn_threshold_pct = config.daily_max_loss_pct * 0.7  # 70%에서 경고

        # 당일 재진입 금지: 손절된 종목 추적 (symbol -> 손절 시각)
        self._stop_loss_today: Dict[str, datetime] = {}
        self._cooldown_minutes = 60  # 손절 후 재진입 금지 시간 (분)

        max_pos = self.get_effective_max_positions(initial_capital)
        logger.info(
            f"RiskManager 초기화: 일일손실한도={config.daily_max_loss_pct}%, "
            f"최대포지션={max_pos}개 (설정={config.max_positions}, 동적={'ON' if config.dynamic_max_positions else 'OFF'}), "
            f"최대비율={config.max_position_pct}%, 최소금액={config.min_position_value:,}원"
        )

    def get_effective_max_positions(self, equity: Decimal = None, available_cash: float = None) -> int:
        """
        자산 규모 기반 실효 최대 포지션 수

        dynamic_max_positions가 True이면:
          - 종목당 최소 금액(min_position_value)을 기준으로 수용 가능한 종목 수 계산
          - config.max_positions를 상한으로 적용
        flex_extra_positions > 0 이면:
          - 가용현금이 총자산의 flex_cash_threshold_pct 이상이면 추가 슬롯 허용
        """
        if not self.config.dynamic_max_positions:
            max_pos = self.config.max_positions
        else:
            if equity is None:
                equity = self.initial_capital
            equity_f = float(equity) if isinstance(equity, Decimal) else float(equity)

            if equity_f <= 0 or self.config.min_position_value <= 0:
                max_pos = self.config.max_positions
            else:
                # 가용 자산 = 총 자산 - 현금 예비금
                investable = equity_f * (1 - self.config.min_cash_reserve_pct / 100)
                # 종목당 목표 금액 = base_position_pct 기준
                target_per_position = equity_f * (self.config.base_position_pct / 100)
                # 최소 금액보다 작으면 최소 금액 사용
                per_position = max(target_per_position, self.config.min_position_value)

                dynamic_max = max(1, int(investable / per_position))
                # config 상한 적용 (config.max_positions를 ceiling으로)
                max_pos = min(dynamic_max, self.config.max_positions)

        # Flex: 가용현금 여유 시 추가 슬롯
        flex = getattr(self.config, 'flex_extra_positions', 0)
        if flex > 0 and available_cash is not None:
            equity_f = float(equity) if equity is not None else float(self.initial_capital)
            if equity_f > 0:
                avail_ratio = available_cash / equity_f * 100
                threshold = getattr(self.config, 'flex_cash_threshold_pct', 10.0)
                if avail_ratio >= threshold and available_cash >= self.config.min_position_value:
                    max_pos = min(max_pos + flex, self.config.max_positions + flex)

        return max_pos

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
        신호 강도에 따른 포지션 크기 계산 (하락장 축소 포함)

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

        # 기본 포지션 비율 (config에서 가져옴, 기본 15%)
        base_pct = Decimal(str(self.config.base_position_pct / 100))

        # 신호 강도에 따른 조정
        strength_multiplier = {
            SignalStrength.VERY_STRONG: Decimal("2.0"),
            SignalStrength.STRONG: Decimal("1.5"),
            SignalStrength.NORMAL: Decimal("1.0"),
            SignalStrength.WEAK: Decimal("0.5"),
        }.get(signal.strength, Decimal("1.0"))

        # 하락장 포지션 축소 (-3% ~ -5% 구간에서 50% 축소)
        # 미실현 손익 포함 (일일손실 한도 체크와 동일 기준)
        effective_pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        daily_pnl_pct = float(effective_pnl / equity * 100) if equity > 0 else 0.0
        drawdown_multiplier = Decimal("1.0")
        if -5.0 < daily_pnl_pct <= -self.config.daily_max_loss_pct:
            drawdown_multiplier = Decimal("0.5")  # 50% 축소
            logger.debug(
                f"[포지션축소] 일일손실 {daily_pnl_pct:.1f}% → "
                f"포지션 50% 축소"
            )

        # 연속 손실 시 포지션 축소 (손익비 개선 — 연속 손실 시 리스크 축소)
        consec_losses = self.daily_stats.consecutive_losses
        if consec_losses >= 2:
            loss_multiplier = Decimal("0.5")  # 2연패 이상이면 50% 축소
            drawdown_multiplier = min(drawdown_multiplier, loss_multiplier)
            logger.debug(
                f"[포지션축소] 연속 손실 {consec_losses}회 → 포지션 50% 축소"
            )

        # 최종 포지션 비율
        position_pct = min(
            base_pct * strength_multiplier * drawdown_multiplier,
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
        volatility: Optional[float] = None,
        symbol: Optional[str] = None,
        market_cap: Optional[float] = None
    ) -> Decimal:
        """
        손절 가격 계산 (대형주 손절 완화 포함)

        Args:
            entry_price: 진입가
            side: 매매 방향
            volatility: 변동성 (선택, ATR 기반)
            symbol: 종목 코드 (대형주 판단용)
            market_cap: 시가총액 (억원, 대형주 판단용)

        Returns:
            손절가
        """
        stop_pct = self.config.default_stop_loss_pct / 100

        # 대형주 손절 완화 (KOSPI200, 시총 1조 이상)
        is_large_cap = False
        if symbol:
            # KOSPI200 대형주 목록 (시총 상위)
            large_caps = {
                '005930',  # 삼성전자
                '000660',  # SK하이닉스
                '373220',  # LG에너지솔루션
                '207940',  # 삼성바이오로직스
                '005380',  # 현대차
                '000270',  # 기아
                '051910',  # LG화학
                '006400',  # 삼성SDI
                '035420',  # NAVER
                '035720',  # 카카오
                '068270',  # 셀트리온
                '028260',  # 삼성물산
                '105560',  # KB금융
                '055550',  # 신한지주
                '086790',  # 하나금융지주
                '316140',  # 우리금융지주
            }
            is_large_cap = symbol in large_caps

        # 시가총액 기준 (1조 이상)
        if market_cap and market_cap >= 10000:  # 1조 = 10000억
            is_large_cap = True

        # 대형주는 손절폭 확대 (2.5% → 3.5%)
        if is_large_cap:
            stop_pct = max(stop_pct, 0.035)  # 최소 3.5%
            logger.debug(f"[손절완화] {symbol} 대형주 → 손절폭 {stop_pct*100:.1f}%")

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
        portfolio: Portfolio,
        strategy_type: str = ""
    ) -> Tuple[bool, str]:
        """
        포지션 오픈 가능 여부 체크

        Args:
            strategy_type: 전략 타입 (차등 리스크 관리용)

        Returns:
            (가능 여부, 거부 사유)
        """
        # 1. 당일 재진입 금지 체크 (손절 후 쿨다운)
        if symbol in self._stop_loss_today:
            stop_time = self._stop_loss_today[symbol]
            elapsed = (datetime.now() - stop_time).total_seconds() / 60
            if elapsed < self._cooldown_minutes:
                remaining = int(self._cooldown_minutes - elapsed)
                return False, f"손절 후 재진입 금지 ({remaining}분 남음)"

        # 2. 일일 손실 한도 체크 (차등 리스크 관리)
        if self._is_daily_loss_limit_hit(portfolio, strategy_type):
            equity = portfolio.total_equity
            daily_pnl_pct = float(portfolio.daily_pnl / equity * 100) if equity > 0 else 0.0
            if daily_pnl_pct <= -5.0:
                return False, f"일일 손실 한도 초과 ({daily_pnl_pct:.1f}%) - 전면 차단"
            else:
                return False, f"일일 손실 한도 도달 ({daily_pnl_pct:.1f}%) - 방어적 전략만 허용"

        # 3. 일일 거래 횟수 체크
        if self.daily_stats.trades >= self.config.daily_max_trades:
            return False, f"일일 거래 횟수 한도 ({self.config.daily_max_trades}회)"

        # 4. 최대 포지션 수 체크 (동적 계산 + flex)
        avail_cash = float(self._get_available_cash(portfolio))
        effective_max = self.get_effective_max_positions(portfolio.total_equity, available_cash=avail_cash)
        if symbol not in portfolio.positions:
            if len(portfolio.positions) >= effective_max:
                return False, f"최대 포지션 수 도달 ({len(portfolio.positions)}/{effective_max}개)"

        # 5. 포지션 크기 체크
        position_value = price * quantity
        max_value = portfolio.total_equity * Decimal(str(self.config.max_position_pct / 100))
        if position_value > max_value:
            return False, f"포지션 크기 초과 ({position_value:,.0f} > {max_value:,.0f})"

        # 6. 현금 체크 (매수 시)
        if side == OrderSide.BUY:
            required = position_value * Decimal("1.001")  # 수수료 여유
            available = self._get_available_cash(portfolio)
            if required > available:
                return False, f"현금 부족 ({available:,.0f} < {required:,.0f})"

        # 7. 연속 손실 체크 (변경: 2회->3회, 분할익절 순손실 오탐 방지)
        if self.daily_stats.consecutive_losses >= 3:
            return False, f"연속 손실 ({self.daily_stats.consecutive_losses}회) - 거래 중단"

        return True, ""

    def _is_daily_loss_limit_hit(self, portfolio: Portfolio, strategy_type: str = "") -> bool:
        """
        일일 손실 한도 도달 여부 (차등 리스크 관리)

        변경: 실현 손익만이 아닌 미실현 손익도 포함하여 체크
        (기존: portfolio.daily_pnl, 변경: effective_daily_pnl)

        Args:
            portfolio: 포트폴리오
            strategy_type: 전략 타입 (mean_reversion, defensive 등)

        Returns:
            차단 여부
        """
        equity = portfolio.total_equity
        if equity <= 0:
            return False

        # 변경: 미실현 손익 포함 (실현+미실현 합산으로 손실 한도 정확히 체크)
        effective_pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        daily_pnl_pct = float(effective_pnl / equity * 100)

        # 1단계: -3% ~ -5% → 방어적 전략만 허용
        if -5.0 < daily_pnl_pct <= -self.config.daily_max_loss_pct:
            # 방어적 전략은 허용 (역추세, 저평가 대형주)
            defensive_strategies = {'mean_reversion', 'defensive', 'value_large_cap'}
            if strategy_type in defensive_strategies:
                logger.debug(
                    f"[차등리스크] 손실 {daily_pnl_pct:.1f}% → "
                    f"방어적 전략 '{strategy_type}' 허용"
                )
                return False  # 차단하지 않음
            else:
                logger.debug(
                    f"[차등리스크] 손실 {daily_pnl_pct:.1f}% → "
                    f"공격적 전략 '{strategy_type}' 차단"
                )
                return True  # 차단

        # 2단계: -5% 이상 → 완전 차단
        if daily_pnl_pct <= -5.0:
            logger.warning(f"[차등리스크] 손실 {daily_pnl_pct:.1f}% → 모든 전략 차단")
            return True

        # 손실 3% 미만 → 정상 거래
        return False

    # ============================================================
    # 이벤트 처리
    # ============================================================

    def on_fill(self, fill_event: FillEvent, portfolio: Portfolio) -> List[Event]:
        """체결 이벤트 처리"""
        events = []

        # 일일 통계 업데이트 (매수 체결만 카운트 — 분할 익절이 한도를 소모하지 않도록)
        if fill_event.side == OrderSide.BUY:
            self.daily_stats.trades += 1

        # 일일 손실 체크 (변경: 미실현 손익 포함, 현재 자산 기준)
        equity = portfolio.total_equity
        effective_pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        daily_pnl_pct = float(effective_pnl / equity * 100) if equity > 0 else 0.0

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
            # 당일 재진입 금지: 손절 종목 기록
            self._stop_loss_today[position.symbol] = datetime.now()
            logger.info(
                f"[재진입금지] {position.symbol} 손절 기록 "
                f"({self._cooldown_minutes}분간 재진입 차단)"
            )

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
        """일일 통계 초기화 (날짜 변경 시에만)"""
        today = date.today()

        # 같은 날이면 리셋하지 않음
        if self.daily_stats.date == today:
            logger.debug(f"일일 통계 유지 (같은 날: {today})")
            return

        # 날짜가 변경된 경우만 리셋
        logger.info(f"날짜 변경: {self.daily_stats.date} → {today}, 일일 통계 초기화")
        self.daily_stats = DailyStats(peak_equity=self.initial_capital)
        self.metrics = RiskMetrics()
        self.metrics.can_trade = True
        self._stop_loss_today.clear()  # 손절 목록 초기화

        # 리셋 후 저장
        self._save_daily_stats()

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

        # 일일 손익 저장
        self._save_daily_stats()

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

    # ============================================================
    # 일일 손익 영속화
    # ============================================================

    def _save_daily_stats(self):
        """일일 손익을 파일에 저장"""
        try:
            data = {
                "date": self.daily_stats.date.isoformat(),
                "trades": self.daily_stats.trades,
                "wins": self.daily_stats.wins,
                "losses": self.daily_stats.losses,
                "total_pnl": str(self.daily_stats.total_pnl),
                "consecutive_losses": self.daily_stats.consecutive_losses,
                "peak_equity": str(self.daily_stats.peak_equity),
            }
            with open(self._daily_stats_path, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"일일 손익 저장 완료: {data['total_pnl']}원")
        except Exception as e:
            logger.error(f"일일 손익 저장 실패: {e}")

    def _load_daily_stats(self):
        """일일 손익을 파일에서 로드"""
        try:
            if not self._daily_stats_path.exists():
                logger.debug("일일 손익 파일 없음 (신규 시작)")
                return

            with open(self._daily_stats_path, 'r') as f:
                data = json.load(f)

            saved_date = date.fromisoformat(data["date"])
            today = date.today()

            # 날짜가 다르면 리셋 (새로운 날)
            if saved_date != today:
                logger.info(f"날짜 변경 감지: {saved_date} → {today}, 일일 손익 리셋")
                return

            # 같은 날이면 복원
            self.daily_stats.trades = data["trades"]
            self.daily_stats.wins = data["wins"]
            self.daily_stats.losses = data["losses"]
            self.daily_stats.total_pnl = Decimal(data["total_pnl"])
            self.daily_stats.consecutive_losses = data["consecutive_losses"]
            self.daily_stats.peak_equity = Decimal(data["peak_equity"])

            logger.info(
                f"일일 손익 복원: {self.daily_stats.total_pnl:,.0f}원 "
                f"(거래 {self.daily_stats.trades}회, 승 {self.daily_stats.wins} / 패 {self.daily_stats.losses})"
            )
        except Exception as e:
            logger.error(f"일일 손익 로드 실패: {e}")
