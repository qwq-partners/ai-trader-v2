"""
AI Trading Bot v2 - 이벤트 기반 트레이딩 엔진

핵심 이벤트 루프와 컴포넌트 조율
"""

import asyncio
import heapq
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Callable, Coroutine, Set
from dataclasses import dataclass, field
import signal
import sys

from loguru import logger

from src.utils.logger import trading_logger

from .event import (
    Event, EventType,
    MarketDataEvent, QuoteEvent, SignalEvent, OrderEvent, FillEvent,
    PositionEvent, RiskAlertEvent, StopTriggeredEvent,
    ThemeEvent, NewsEvent, SessionEvent, HeartbeatEvent, ErrorEvent
)
from .types import (
    Order, Fill, Position, Portfolio, Signal, RiskMetrics,
    OrderSide, OrderStatus, OrderType, TradingConfig, RiskConfig, MarketSession
)


# ============================================================
# 한국 시장 휴장일 (동적 조회 + fallback)
# ============================================================
# KISMarketData.fetch_holidays()로 채워지는 동적 캐시
_kr_market_holidays: Set[date] = set()


def set_kr_market_holidays(holidays: Set[date]):
    """외부에서 조회한 휴장일을 주입 (봇 시작 시 호출)"""
    global _kr_market_holidays
    _kr_market_holidays = holidays
    logger.info(f"한국 시장 휴장일 {len(holidays)}일 로드 완료")


def is_kr_market_holiday(d: date) -> bool:
    """한국 시장 휴장일 여부 (주말 + 공휴일)"""
    if d.weekday() >= 5:
        return True
    if _kr_market_holidays:
        return d in _kr_market_holidays
    # 동적 데이터가 없으면 주말만 체크 (fallback)
    return False


# 이벤트 핸들러 타입
EventHandler = Callable[[Event], Coroutine[Any, Any, Optional[List[Event]]]]


@dataclass
class EngineStats:
    """엔진 통계"""
    start_time: datetime = field(default_factory=datetime.now)
    events_processed: int = 0
    signals_generated: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    errors_count: int = 0

    @property
    def uptime_seconds(self) -> float:
        return (datetime.now() - self.start_time).total_seconds()


class TradingEngine:
    """
    이벤트 기반 트레이딩 엔진

    모든 컴포넌트를 조율하고 이벤트를 라우팅합니다.
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.running = False
        self.paused = False

        # 이벤트 큐 (우선순위 힙)
        self._event_queue: List[Event] = []
        self._queue_lock = asyncio.Lock()

        # 이벤트 핸들러 레지스트리
        self._handlers: Dict[EventType, List[EventHandler]] = {
            event_type: [] for event_type in EventType
        }

        # 포트폴리오
        self.portfolio = Portfolio(
            cash=config.initial_capital,
            initial_capital=config.initial_capital
        )

        # 리스크 메트릭스
        self.risk_metrics = RiskMetrics()

        # 통계
        self.stats = EngineStats()

        # 컴포넌트 참조 (초기화 후 설정)
        self.strategy_manager = None
        self.risk_manager = None
        self.broker = None
        self.data_feed = None

        # 시그널 핸들러
        self._setup_signal_handlers()

        logger.info("TradingEngine 초기화 완료")

    def _setup_signal_handlers(self):
        """시스템 시그널 핸들러 설정"""
        def handle_shutdown(signum, frame):
            logger.warning(f"종료 신호 수신 ({signum}). 안전하게 종료합니다...")
            self.running = False

        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)

    # ============================================================
    # 이벤트 핸들러 관리
    # ============================================================

    def register_handler(self, event_type: EventType, handler: EventHandler):
        """이벤트 핸들러 등록"""
        self._handlers[event_type].append(handler)
        logger.debug(f"핸들러 등록: {event_type.name} -> {handler.__name__}")

    def unregister_handler(self, event_type: EventType, handler: EventHandler):
        """이벤트 핸들러 해제"""
        if handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)

    async def emit(self, event: Event):
        """이벤트 발행 (큐에 추가)"""
        async with self._queue_lock:
            heapq.heappush(self._event_queue, event)

    async def emit_many(self, events: List[Event]):
        """여러 이벤트 일괄 발행"""
        async with self._queue_lock:
            for event in events:
                heapq.heappush(self._event_queue, event)

    # ============================================================
    # 메인 이벤트 루프
    # ============================================================

    async def run(self):
        """메인 이벤트 루프 실행"""
        self.running = True
        logger.info("트레이딩 엔진 시작")

        # 초기화 이벤트
        await self._emit_startup_events()

        # 하트비트 태스크
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            while self.running:
                # 일시 정지 체크
                if self.paused:
                    await asyncio.sleep(0.1)
                    continue

                # 이벤트 처리
                event = await self._get_next_event()
                if event:
                    await self._process_event(event)
                else:
                    # 이벤트 없으면 잠시 대기
                    await asyncio.sleep(0.001)

        except Exception as e:
            logger.exception(f"엔진 오류: {e}")
            await self.emit(ErrorEvent(
                error_type=type(e).__name__,
                message=str(e),
                recoverable=False
            ))
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            await self._shutdown()

    async def _get_next_event(self) -> Optional[Event]:
        """다음 이벤트 가져오기"""
        async with self._queue_lock:
            if self._event_queue:
                return heapq.heappop(self._event_queue)
        return None

    async def _process_event(self, event: Event):
        """이벤트 처리"""
        self.stats.events_processed += 1

        # SIGNAL 이벤트 추적
        if event.type == EventType.SIGNAL:
            logger.info(f"[엔진] SignalEvent 처리 시작: {getattr(event, 'symbol', '?')} {getattr(event, 'side', '?')}")

        handlers = self._handlers.get(event.type, [])
        if not handlers:
            if event.type == EventType.SIGNAL:
                logger.warning(f"[엔진] SIGNAL 핸들러 없음!")
            return

        for handler in handlers:
            try:
                # 핸들러 실행
                result = await handler(event)

                # 새 이벤트가 반환되면 큐에 추가
                if result:
                    await self.emit_many(result)

            except Exception as e:
                self.stats.errors_count += 1
                logger.exception(f"핸들러 오류 ({handler.__name__}): {e}")

                await self.emit(ErrorEvent(
                    source=handler.__name__,
                    error_type=type(e).__name__,
                    message=str(e),
                    recoverable=True
                ))

    async def _emit_startup_events(self):
        """시작 이벤트 발행"""
        # 세션 이벤트
        current_session = self._get_current_session()
        await self.emit(SessionEvent(
            source="engine",
            session=current_session
        ))

        logger.info(f"현재 세션: {current_session.value}")

    async def _heartbeat_loop(self):
        """하트비트 루프"""
        while self.running:
            try:
                # 실제 대기 주문 수 조회
                pending = 0
                if self.risk_manager and hasattr(self.risk_manager, '_pending_orders'):
                    pending = len(self.risk_manager._pending_orders)
                await self.emit(HeartbeatEvent(
                    source="engine",
                    uptime_seconds=self.stats.uptime_seconds,
                    active_positions=len(self.portfolio.positions),
                    pending_orders=pending,
                ))
                await asyncio.sleep(10)  # 10초마다
            except asyncio.CancelledError:
                break

    async def _shutdown(self):
        """종료 처리"""
        logger.info("트레이딩 엔진 종료 중...")

        # 열린 포지션 경고
        if self.portfolio.positions:
            logger.warning(f"열린 포지션 {len(self.portfolio.positions)}개:")
            for symbol, pos in self.portfolio.positions.items():
                logger.warning(f"  {symbol}: {pos.quantity}주, P&L: {pos.unrealized_pnl:+,.0f}원")

        # 통계 출력
        logger.info(f"=== 엔진 통계 ===")
        logger.info(f"실행 시간: {self.stats.uptime_seconds:.0f}초")
        logger.info(f"처리 이벤트: {self.stats.events_processed:,}개")
        logger.info(f"생성 신호: {self.stats.signals_generated:,}개")
        logger.info(f"체결 주문: {self.stats.orders_filled:,}개")
        logger.info(f"오류: {self.stats.errors_count:,}개")

        self.running = False
        logger.info("트레이딩 엔진 종료 완료")

    # ============================================================
    # 시장 세션 관리
    # ============================================================

    def _get_current_session(self) -> MarketSession:
        """현재 시장 세션 반환"""
        now = datetime.now()

        # 주말 + 공휴일
        if is_kr_market_holiday(now.date()):
            return MarketSession.CLOSED

        hour = now.hour
        minute = now.minute
        time_int = hour * 100 + minute  # HHMM 형식

        # 프리장: 08:00 ~ 08:50
        if 800 <= time_int < 850:
            return MarketSession.PRE_MARKET

        # 정규장: 09:00 ~ 15:20 (장마감 동시호가 전까지)
        if 900 <= time_int < 1520:
            return MarketSession.REGULAR

        # 15:20~15:40: CLOSED (장마감 동시호가 + 휴장)
        # → is_trading_hours()=False → 신규 매수/매도 신호 차단

        # 넥스트장: 15:40 ~ 20:00 (10분 휴장 반영)
        if 1540 <= time_int < 2000:
            return MarketSession.NEXT

        return MarketSession.CLOSED

    def is_trading_hours(self) -> bool:
        """거래 가능 시간 여부"""
        session = self._get_current_session()
        if session == MarketSession.CLOSED:
            return False
        if session == MarketSession.PRE_MARKET and not self.config.enable_pre_market:
            return False
        if session == MarketSession.NEXT and not self.config.enable_next_market:
            return False
        return True

    # ============================================================
    # 포트폴리오 관리
    # ============================================================

    def update_position(self, fill: Fill):
        """체결로 포지션 업데이트"""
        symbol = fill.symbol

        if symbol not in self.portfolio.positions:
            # 새 포지션
            self.portfolio.positions[symbol] = Position(
                symbol=symbol,
                quantity=0,
                avg_price=Decimal("0")
            )

        pos = self.portfolio.positions[symbol]

        if fill.side == OrderSide.BUY:
            # 매수 - 평균단가 계산
            new_quantity = pos.quantity + fill.quantity
            if new_quantity > 0:
                total_cost = pos.avg_price * pos.quantity + fill.price * fill.quantity
                pos.avg_price = total_cost / new_quantity
            pos.quantity = new_quantity
            self.portfolio.cash -= fill.total_cost

            # 신규 포지션 시 highest_price 초기화
            if pos.highest_price is None or pos.highest_price < fill.price:
                pos.highest_price = fill.price

        else:
            # 매도
            pos.quantity -= fill.quantity
            realized_pnl = (fill.price - pos.avg_price) * fill.quantity - fill.commission
            self.portfolio.cash += fill.total_value - fill.commission
            self.portfolio.daily_pnl += realized_pnl

            # 포지션 종료 시 제거
            if pos.quantity <= 0:
                del self.portfolio.positions[symbol]

        # 매수 체결만 일일 거래 횟수로 카운트 (분할 익절 매도가 한도를 소모하지 않도록)
        if fill.side == OrderSide.BUY:
            self.portfolio.daily_trades += 1

    def update_position_price(self, symbol: str, current_price: Decimal):
        """
        시세 업데이트로 포지션 현재가/최고가 갱신

        트레일링 스탑 계산에 필요합니다.
        """
        pos = self.portfolio.positions.get(symbol)
        if not pos:
            return

        # 0 이하 가격은 데이터 오류 → 무시
        if current_price <= 0:
            return

        # 현재가 업데이트
        pos.current_price = current_price

        # 최고가 업데이트 (트레일링 스탑용)
        if pos.highest_price is None or current_price > pos.highest_price:
            pos.highest_price = current_price

    def get_position(self, symbol: str) -> Optional[Position]:
        """포지션 조회"""
        return self.portfolio.positions.get(symbol)

    def get_available_cash(self) -> Decimal:
        """가용 현금 (최소 현금 보유량 제외)"""
        min_reserve = self.portfolio.total_equity * Decimal(str(self.config.risk.min_cash_reserve_pct / 100))
        return max(self.portfolio.cash - min_reserve, Decimal("0"))

    # ============================================================
    # 리스크 체크
    # ============================================================

    def can_open_position(
        self, symbol: str, side: OrderSide, quantity: int, price: Decimal,
        pending_symbols: Optional[Set[str]] = None,
        reserved_cash: Decimal = Decimal("0"),
    ) -> tuple[bool, str]:
        """포지션 오픈 가능 여부 체크"""
        risk = self.config.risk
        _pending = pending_symbols or set()

        # 1. 일일 손실 제한 체크 (현재 자산 기준)
        _equity = self.portfolio.total_equity
        daily_loss_pct = float(self.portfolio.daily_pnl / _equity * 100) if _equity > 0 else 0.0
        if daily_loss_pct <= -risk.daily_max_loss_pct:
            return False, f"일일 손실 한도 도달 ({daily_loss_pct:.1f}%)"

        # 2. 일일 거래 횟수 제한
        if self.portfolio.daily_trades >= risk.daily_max_trades:
            return False, f"일일 거래 횟수 한도 도달 ({self.portfolio.daily_trades}회)"

        # 3. 최대 포지션 수 제한 (pending 주문 포함, 동적 계산)
        if symbol not in self.portfolio.positions:
            effective_positions = len(self.portfolio.positions) + len(
                _pending - set(self.portfolio.positions.keys())
            )
            # 동적 max_positions 계산
            max_pos = risk.max_positions
            if risk.dynamic_max_positions and risk.min_position_value > 0:
                equity_f = float(self.portfolio.total_equity)
                investable = equity_f * (1 - risk.min_cash_reserve_pct / 100)
                per_pos = max(equity_f * risk.base_position_pct / 100, risk.min_position_value)
                calculated = int(investable / per_pos) if per_pos > 0 else 0
                max_pos = min(max(1, calculated), risk.max_positions)
            if effective_positions >= max_pos:
                return False, f"최대 포지션 수 도달 ({effective_positions}/{max_pos}개, pending 포함)"

        # 4. 포지션 크기 제한
        position_value = price * quantity
        max_position_value = self.portfolio.total_equity * Decimal(str(risk.max_position_pct / 100))
        if position_value > max_position_value:
            return False, f"포지션 크기 초과 ({position_value:,.0f} > {max_position_value:,.0f})"

        # 5. 현금 체크 (예약된 현금 차감)
        if side == OrderSide.BUY:
            required_cash = position_value * Decimal("1.001")  # 수수료 여유
            available = self.get_available_cash() - reserved_cash
            if required_cash > available:
                return False, f"현금 부족 ({available:,.0f} < {required_cash:,.0f})"

        return True, ""

    # ============================================================
    # 편의 메서드
    # ============================================================

    def pause(self):
        """엔진 일시 정지"""
        self.paused = True
        logger.info("엔진 일시 정지")

    def resume(self):
        """엔진 재개"""
        self.paused = False
        logger.info("엔진 재개")

    def stop(self):
        """엔진 종료"""
        self.running = False
        logger.info("엔진 종료 요청")

    def reset_daily_stats(self):
        """일일 통계 초기화"""
        self.portfolio.daily_pnl = Decimal("0")
        self.portfolio.daily_trades = 0
        self.risk_metrics = RiskMetrics()
        logger.info("일일 통계 초기화")


class StrategyManager:
    """
    전략 관리자

    여러 전략을 관리하고 신호를 통합합니다.
    """

    def __init__(self, engine: TradingEngine):
        self.engine = engine
        self.strategies: Dict[str, Any] = {}  # 전략 객체들
        self.enabled_strategies: List[str] = []

        # 엔진에 핸들러 등록
        engine.register_handler(EventType.MARKET_DATA, self.on_market_data)
        engine.register_handler(EventType.THEME, self.on_theme)

    def register_strategy(self, name: str, strategy):
        """전략 등록"""
        self.strategies[name] = strategy
        self.enabled_strategies.append(name)
        logger.info(f"전략 등록: {name}")

    def enable_strategy(self, name: str):
        """전략 활성화"""
        if name in self.strategies and name not in self.enabled_strategies:
            self.enabled_strategies.append(name)

    def disable_strategy(self, name: str):
        """전략 비활성화"""
        if name in self.enabled_strategies:
            self.enabled_strategies.remove(name)

    async def on_market_data(self, event: MarketDataEvent) -> Optional[List[Event]]:
        """시장 데이터 수신 시 전략 실행"""
        # 포지션 가격 업데이트 (트레일링 스탑용)
        self.engine.update_position_price(event.symbol, event.close)

        # 가용 현금 없으면 매수 진입 건너뜀 (매도 신호는 계속 생성)
        no_cash = self.engine.get_available_cash() <= 0

        signals = []

        for name in self.enabled_strategies:
            strategy = self.strategies.get(name)
            if strategy and hasattr(strategy, 'on_market_data'):
                try:
                    signal = await strategy.on_market_data(event)
                    if signal:
                        # 현금 없으면 BUY 신호 무시 (SELL은 통과)
                        if no_cash and signal.side == OrderSide.BUY:
                            continue
                        signals.append(SignalEvent.from_signal(signal, source=name))
                except Exception as e:
                    logger.error(f"전략 오류 ({name}): {e}")

        if signals:
            self.engine.stats.signals_generated += len(signals)
            for sig in signals:
                logger.info(f"[전략→엔진] 신호 큐 추가: {sig.symbol} {sig.side.value} 가격={sig.price} 점수={sig.score:.1f}")

        return signals if signals else None

    async def on_theme(self, event: ThemeEvent) -> Optional[List[Event]]:
        """테마 감지 시 전략 실행"""
        signals = []

        for name in self.enabled_strategies:
            strategy = self.strategies.get(name)
            if strategy and hasattr(strategy, 'on_theme'):
                try:
                    signal = await strategy.on_theme(event)
                    if signal:
                        signals.append(SignalEvent.from_signal(signal, source=name))
                except Exception as e:
                    logger.error(f"전략 오류 ({name}): {e}")

        return signals if signals else None


class RiskManager:
    """
    리스크 관리자

    신호를 검증하고 포지션 크기를 계산합니다.
    """

    def __init__(self, engine: TradingEngine, config: RiskConfig, risk_validator=None):
        self.engine = engine
        self.config = config

        # 외부 리스크 검증자 (RiskMgr 인스턴스) — daily_stats 공유용
        self._risk_validator = risk_validator

        # 주문 실패 쿨다운 추적 (종목별)
        self._order_fail_cooldown: Dict[str, datetime] = {}
        self._COOLDOWN_SECONDS = 300  # 5분 쿨다운

        # 현금 부족 로그 쓰로틀링
        self._last_cash_warn_time: Optional[datetime] = None

        # 중복 주문 방지: 주문 진행 중인 종목
        self._pending_orders: Set[str] = set()

        # pending 등록 시각 (stale pending 정리용)
        self._pending_timestamps: Dict[str, datetime] = {}
        self._PENDING_TIMEOUT_SECONDS = 600  # 10분 타임아웃

        # 부분 체결 추적: 종목별 미체결 수량
        self._pending_quantities: Dict[str, int] = {}

        # 현금 초과 주문 방지: 주문별 예약 현금 추적 (symbol → 예약 금액)
        self._reserved_by_order: Dict[str, Decimal] = {}

        # 엔진에 핸들러 등록
        engine.register_handler(EventType.SIGNAL, self.on_signal)
        engine.register_handler(EventType.FILL, self.on_fill)

    @property
    def _reserved_cash(self) -> Decimal:
        """예약 현금 합계 (주문별 추적 기반)"""
        return sum(self._reserved_by_order.values()) if self._reserved_by_order else Decimal("0")

    def block_symbol(self, symbol: str):
        """종목 주문 쿨다운 등록 (외부에서 호출)"""
        self._order_fail_cooldown[symbol] = datetime.now()

    async def on_signal(self, event: SignalEvent) -> Optional[List[Event]]:
        """신호 검증 및 주문 생성"""
        logger.info(f"[리스크] 신호 수신: {event.symbol} {event.side.value} 가격={event.price} 점수={event.score:.1f}")

        # 만료된 쿨다운 항목 정리
        now = datetime.now()
        expired = [s for s, t in self._order_fail_cooldown.items()
                   if (now - t).total_seconds() >= self._COOLDOWN_SECONDS]
        for s in expired:
            del self._order_fail_cooldown[s]

        # stale pending 주문 정리 (10분 이상 미체결 → 거래소 거부 등)
        stale_pending = [s for s, t in self._pending_timestamps.items()
                         if (now - t).total_seconds() >= self._PENDING_TIMEOUT_SECONDS]
        for s in stale_pending:
            self.clear_pending(s)
            logger.warning(f"[리스크] stale pending 정리: {s} ({self._PENDING_TIMEOUT_SECONDS}초 초과)")

        # 거래 가능 여부 체크
        if not self.engine.is_trading_hours():
            session = self.engine._get_current_session()
            logger.info(f"[리스크] 거래 시간 외 차단: {event.symbol} (세션={session.value})")
            trading_logger.log_signal_blocked(
                symbol=event.symbol, side=event.side.value,
                reason=f"거래시간외({session.value})",
                price=float(event.price or 0), score=event.score,
            )
            return None

        # 이미 주문 진행 중인 종목 차단 (중복 신호 방지)
        if event.symbol in self._pending_orders:
            logger.debug(f"[리스크] 주문 진행 중 차단: {event.symbol}")
            trading_logger.log_signal_blocked(
                symbol=event.symbol, side=event.side.value,
                reason="주문진행중",
                price=float(event.price or 0), score=event.score,
            )
            return None

        # 이미 포지션이 있는 종목 매수 차단
        if event.side == OrderSide.BUY and event.symbol in self.engine.portfolio.positions:
            logger.debug(f"[리스크] 기존 포지션 보유 차단: {event.symbol}")
            trading_logger.log_signal_blocked(
                symbol=event.symbol, side=event.side.value,
                reason="기존포지션보유",
                price=float(event.price or 0), score=event.score,
            )
            return None

        # 매수 신호인 경우: 가용 현금 사전 체크 (로그 폭주 방지)
        if event.side == OrderSide.BUY:
            available = self.engine.get_available_cash() - self._reserved_cash
            if available <= 0:
                now = datetime.now()
                if (self._last_cash_warn_time is None or
                        (now - self._last_cash_warn_time).total_seconds() > 60):
                    logger.warning(f"[리스크] 가용 현금 없음 - 매수 신호 무시 ({event.symbol})")
                    self._last_cash_warn_time = now
                trading_logger.log_signal_blocked(
                    symbol=event.symbol, side=event.side.value,
                    reason="현금부족",
                    price=float(event.price or 0), score=event.score,
                )
                return None

        # 주문 실패 쿨다운 체크
        if event.symbol in self._order_fail_cooldown:
            cooldown_start = self._order_fail_cooldown[event.symbol]
            elapsed = (datetime.now() - cooldown_start).total_seconds()
            if elapsed < self._COOLDOWN_SECONDS:
                return None  # 쿨다운 중 - 조용히 무시
            else:
                del self._order_fail_cooldown[event.symbol]

        # 포지션 크기 계산
        if event.side == OrderSide.SELL:
            # 매도: 보유 수량 전체
            pos = self.engine.portfolio.positions.get(event.symbol)
            position_size = pos.quantity if pos else 0
        else:
            # 매수: 자본 비율 기반
            position_size = self._calculate_position_size(event)

        if position_size <= 0:
            equity = self.engine.portfolio.total_equity
            logger.info(f"[리스크] 포지션 크기 0: {event.symbol} (자산={equity:,.0f}, 가격={event.price})")
            return None

        # 주문 생성 (시장가 주문)
        order = Order(
            symbol=event.symbol,
            side=event.side,
            order_type=OrderType.MARKET,
            quantity=position_size,
            price=event.price,
            strategy=event.strategy.value,
            reason=event.reason
        )

        # 리스크 체크 (SELL은 포지션 축소이므로 체크 스킵)
        if order.side == OrderSide.BUY:
            # 외부 리스크 검증자 체크 (daily_stats, 연속 손실 등)
            if self._risk_validator:
                can_trade, reason = self._risk_validator.can_open_position(
                    order.symbol, order.side, order.quantity,
                    order.price or Decimal("0"), self.engine.portfolio,
                    strategy_type=order.strategy  # 차등 리스크 관리용
                )
                if not can_trade:
                    logger.warning(f"주문 거부 (리스크 검증): {order.symbol} - {reason}")
                    trading_logger.log_signal_blocked(
                        symbol=order.symbol, side=order.side.value,
                        reason=f"리스크검증:{reason}",
                        price=float(order.price or 0), score=event.score,
                    )
                    return None

            can_trade, reason = self.engine.can_open_position(
                order.symbol, order.side, order.quantity, order.price or Decimal("0"),
                pending_symbols=self._pending_orders,
                reserved_cash=self._reserved_cash,
            )
            if not can_trade:
                logger.warning(f"주문 거부: {order.symbol} - {reason}")
                trading_logger.log_signal_blocked(
                    symbol=order.symbol, side=order.side.value,
                    reason=f"엔진리스크:{reason}",
                    price=float(order.price or 0), score=event.score,
                )
                return None

        logger.info(f"주문 생성: {order.side.value} {order.symbol} {order.quantity}주 @ {order.price}")

        # 중복 주문 방지: pending 등록 + 미체결 수량 추적
        self._pending_orders.add(order.symbol)
        self._pending_quantities[order.symbol] = order.quantity
        self._pending_timestamps[order.symbol] = datetime.now()

        # 현금 예약 (매수 주문 금액만큼, 주문별 추적 + 시장가 슬리피지/수수료 버퍼 1.5%)
        if order.side == OrderSide.BUY and order.price and order.quantity:
            self._reserved_by_order[order.symbol] = order.price * order.quantity * Decimal("1.015")

        return [OrderEvent.from_order(order, source="risk_manager")]

    def clear_pending(self, symbol: str, amount: Decimal = Decimal("0")):
        """주문 완료/실패 시 pending 해제 (외부에서 호출)"""
        self._pending_orders.discard(symbol)
        self._pending_quantities.pop(symbol, None)
        self._pending_timestamps.pop(symbol, None)
        self._reserved_by_order.pop(symbol, None)

    async def on_fill(self, event: FillEvent) -> Optional[List[Event]]:
        """체결 후 리스크 업데이트 (부분 체결 지원)"""
        # 미체결 수량 감소 → 전량 체결 시에만 pending 해제 및 예약 현금 해제
        remaining = self._pending_quantities.get(event.symbol, 0) - (event.quantity or 0)
        if remaining <= 0:
            self._pending_orders.discard(event.symbol)
            self._pending_quantities.pop(event.symbol, None)
            self._pending_timestamps.pop(event.symbol, None)
            # 전량 체결 시 원래 예약 금액 정확히 해제 (슬리피지 드리프트 방지)
            self._reserved_by_order.pop(event.symbol, None)
        else:
            self._pending_quantities[event.symbol] = remaining
            logger.info(f"[리스크] 부분 체결: {event.symbol} 잔여 {remaining}주")

        # 일일 손실 체크 (현재 자산 기준)
        _equity = self.engine.portfolio.total_equity
        daily_loss_pct = float(
            self.engine.portfolio.daily_pnl / _equity * 100
        ) if _equity > 0 else 0.0

        if daily_loss_pct <= -self.config.daily_max_loss_pct:
            return [RiskAlertEvent(
                source="risk_manager",
                alert_type="daily_loss",
                message=f"일일 손실 한도 도달: {daily_loss_pct:.1f}%",
                current_value=daily_loss_pct,
                threshold=-self.config.daily_max_loss_pct,
                action="block"
            )]

        return None

    def _calculate_position_size(self, signal: SignalEvent) -> int:
        """포지션 크기 계산 (자본 활용률 최적화, 분할익절 최소 수량 보장)"""
        equity = self.engine.portfolio.total_equity
        price = signal.price or Decimal("0")

        if price <= 0 or equity <= 0:
            return 0

        # 설정 기반 기본 비율 (RiskConfig.base_position_pct)
        base_pct = self.config.base_position_pct / 100

        # 신호 강도에 따른 조정
        multiplier = {
            "very_strong": 2.0,
            "strong": 1.5,
            "normal": 1.0,
            "weak": 0.5
        }.get(signal.strength.value, 1.0)

        # 비율 기반 포지션 금액
        position_pct = min(base_pct * multiplier, self.config.max_position_pct / 100)
        pct_value = equity * Decimal(str(position_pct))

        # 가용 현금 (수수료 여유분, 예약 현금 차감)
        available = self.engine.get_available_cash() - self._reserved_cash
        if available <= 0:
            return 0

        # 동적 max_positions 계산
        max_pos = self.config.max_positions
        if self.config.dynamic_max_positions and self.config.min_position_value > 0:
            equity_f = float(equity)
            investable = equity_f * (1 - self.config.min_cash_reserve_pct / 100)
            per_pos = max(equity_f * self.config.base_position_pct / 100, self.config.min_position_value)
            calculated = int(investable / per_pos) if per_pos > 0 else 0
            max_pos = min(max(1, calculated), self.config.max_positions)

        # 남은 슬롯 기반 균등 배분 (유휴 자본 방지)
        # 기존 포지션 매도 주문(pending)은 이중 카운트 방지
        new_pending = self._pending_orders - set(self.engine.portfolio.positions.keys())
        current_count = len(self.engine.portfolio.positions) + len(new_pending)
        remaining_slots = max(max_pos - current_count, 1)
        slot_value = available / Decimal(str(remaining_slots))

        # 비율 기반 vs 슬롯 기반 중 큰 값 → 자본 활용률 향상
        max_value = equity * Decimal(str(self.config.max_position_pct / 100))
        position_value = min(max(pct_value, slot_value), max_value, available)

        # 전략별 포지션 배율 (역추세 등은 축소) — 최종 금액에 적용
        position_multiplier = 1.0
        if signal.signal and signal.signal.metadata:
            position_multiplier = signal.signal.metadata.get("position_multiplier", 1.0)
        if position_multiplier != 1.0:
            position_value *= Decimal(str(position_multiplier))

        # 최소 포지션 금액 체크
        min_val = Decimal(str(self.config.min_position_value))
        if position_value < min_val:
            logger.debug(f"[리스크] 포지션 금액 미달: {signal.symbol} ({position_value:,.0f} < {min_val:,.0f})")
            return 0

        # 수량 계산
        quantity = int(position_value / price)

        # 최소 수량 체크: 분할 익절에 최소 3주 필요
        MIN_QTY_FOR_PARTIAL_EXIT = 3
        if quantity < MIN_QTY_FOR_PARTIAL_EXIT:
            cost_for_min = price * MIN_QTY_FOR_PARTIAL_EXIT * Decimal("1.001")
            if cost_for_min <= available and cost_for_min <= max_value:
                quantity = MIN_QTY_FOR_PARTIAL_EXIT
            else:
                logger.info(
                    f"[리스크] 최소 수량 미달 스킵: {signal.symbol} "
                    f"(가격={price:,.0f}, 3주 비용={cost_for_min:,.0f}, 가용={available:,.0f})"
                )
                return 0

        return max(quantity, 0)
