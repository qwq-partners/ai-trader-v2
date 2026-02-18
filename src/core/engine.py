"""
AI Trading Bot v2 - 이벤트 기반 트레이딩 엔진

핵심 이벤트 루프와 컴포넌트 조율
"""

import asyncio
import heapq
from collections import deque
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Callable, Coroutine, Set
from dataclasses import dataclass, field
import signal
import sys

from loguru import logger

from src.utils.logger import trading_logger
from src.utils.session_util import SessionUtil
from src.utils.fee_calculator import get_fee_calculator

from .event import (
    Event, EventType,
    MarketDataEvent, QuoteEvent, SignalEvent, OrderEvent, FillEvent,
    PositionEvent, RiskAlertEvent, StopTriggeredEvent,
    ThemeEvent, NewsEvent, SessionEvent, HeartbeatEvent, ErrorEvent
)
from .types import (
    Order, Fill, Position, Portfolio, Signal, RiskMetrics,
    OrderSide, OrderStatus, OrderType, TradingConfig, RiskConfig, MarketSession,
    StrategyType, TimeHorizon
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
    """한국 시장 휴장일 여부 (주말 + 공휴일)

    동적 데이터(API)와 Fallback(하드코딩)을 합쳐서 체크합니다.
    API는 당월/익월만 로드하므로 3개월 후 공휴일은 Fallback에서 커버합니다.
    """
    if d.weekday() >= 5:
        return True
    if _kr_market_holidays:
        return d in _kr_market_holidays or d in _FALLBACK_HOLIDAYS
    # 동적 데이터가 없으면 하드코딩 공휴일 체크 (fallback)
    return d in _FALLBACK_HOLIDAYS


# 하드코딩 공휴일 (동적 조회 실패 시 fallback) - 2026~2027년
_FALLBACK_HOLIDAYS: Set[date] = {
    # 2026년
    date(2026, 1, 1),   # 신정
    date(2026, 1, 27),  # 설날 전날
    date(2026, 1, 28),  # 설날
    date(2026, 1, 29),  # 설날 다음날
    date(2026, 3, 1),   # 삼일절 (일→3/2 대체)
    date(2026, 3, 2),   # 삼일절 대체공휴일
    date(2026, 5, 5),   # 어린이날
    date(2026, 5, 24),  # 석가탄신일 (일→5/25 대체)
    date(2026, 5, 25),  # 석가탄신일 대체공휴일
    date(2026, 6, 6),   # 현충일 (토)
    date(2026, 8, 15),  # 광복절 (토)
    date(2026, 8, 17),  # 광복절 대체공휴일
    date(2026, 9, 24),  # 추석 전날
    date(2026, 9, 25),  # 추석
    date(2026, 9, 26),  # 추석 다음날 (토)
    date(2026, 10, 3),  # 개천절 (토)
    date(2026, 10, 5),  # 개천절 대체공휴일
    date(2026, 10, 9),  # 한글날
    date(2026, 12, 25), # 크리스마스
    # 2027년
    date(2027, 1, 1),   # 신정
    date(2027, 2, 8),   # 설날 전날
    date(2027, 2, 9),   # 설날
    date(2027, 2, 10),  # 설날 다음날
    date(2027, 3, 1),   # 삼일절
    date(2027, 5, 5),   # 어린이날
    date(2027, 5, 13),  # 석가탄신일
    date(2027, 6, 6),   # 현충일 (일→6/7 대체)
    date(2027, 6, 7),   # 현충일 대체공휴일
    date(2027, 8, 15),  # 광복절 (일→8/16 대체)
    date(2027, 8, 16),  # 광복절 대체공휴일
    date(2027, 10, 3),  # 개천절 (일→10/4 대체)
    date(2027, 10, 4),  # 개천절 대체공휴일
    date(2027, 10, 9),  # 한글날 (토)
    date(2027, 10, 11), # 한글날 대체공휴일
    date(2027, 10, 13), # 추석 전날
    date(2027, 10, 14), # 추석
    date(2027, 10, 15), # 추석 다음날
    date(2027, 12, 25), # 크리스마스 (토)
    date(2027, 12, 27), # 크리스마스 대체공휴일
}


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
        self._MAX_QUEUE_SIZE = 1000  # 큐 크기 상한 (메모리 보호)

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

        # 프리마켓 데이터 (NXT)
        self.premarket_data: Dict[str, Dict] = {}

        # 대시보드 이벤트 로그 (ring buffer)
        self._dashboard_events: deque = deque(maxlen=200)
        self._dashboard_event_id: int = 0

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
    # 대시보드 이벤트 로그
    # ============================================================

    def push_dashboard_event(self, event_type: str, message: str):
        """대시보드 이벤트 로그에 항목 추가"""
        self._dashboard_event_id += 1
        self._dashboard_events.append({
            "id": self._dashboard_event_id,
            "time": datetime.now().isoformat(),
            "type": event_type,
            "message": message,
        })

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
            if len(self._event_queue) >= self._MAX_QUEUE_SIZE:
                logger.warning(f"이벤트 큐 포화 ({len(self._event_queue)}건) → 최저 우선순위 이벤트 폐기")
                # 가장 낮은 우선순위(큰 값) 이벤트 제거
                self._event_queue.sort()
                self._event_queue = self._event_queue[:self._MAX_QUEUE_SIZE - 1]
                heapq.heapify(self._event_queue)
            heapq.heappush(self._event_queue, event)

    async def emit_many(self, events: List[Event]):
        """여러 이벤트 일괄 발행"""
        async with self._queue_lock:
            # 포화 시 한 번만 정리 (루프마다 sort 반복 방지)
            needed = len(events)
            current = len(self._event_queue)
            if current + needed > self._MAX_QUEUE_SIZE:
                logger.warning(f"이벤트 큐 포화 ({current}건) → 최저 우선순위 이벤트 폐기")
                self._event_queue.sort()
                keep = max(self._MAX_QUEUE_SIZE - needed, self._MAX_QUEUE_SIZE // 2)
                self._event_queue = self._event_queue[:keep]
                heapq.heapify(self._event_queue)
            # 일괄 push
            for event in events:
                if len(self._event_queue) < self._MAX_QUEUE_SIZE:
                    heapq.heappush(self._event_queue, event)
                else:
                    logger.warning(f"[엔진] 이벤트 큐 포화 — {event.type} 폐기")

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

        # SIGNAL 이벤트 추적 + 대시보드 로그
        if event.type == EventType.SIGNAL:
            symbol = getattr(event, 'symbol', '?')
            side = getattr(event, 'side', '?')
            score = getattr(event, 'score', 0) or 0
            logger.info(f"[엔진] SignalEvent 처리 시작: {symbol} {side}")
            side_label = '매수' if side == OrderSide.BUY else '매도'
            self.push_dashboard_event("신호", f"{symbol} {side_label} 신호 (점수:{score:.0f})")
        elif event.type == EventType.FILL:
            symbol = getattr(event, 'symbol', '?')
            side = getattr(event, 'side', '?')
            price = getattr(event, 'price', 0)
            qty = getattr(event, 'quantity', 0)
            side_label = '매수' if side == OrderSide.BUY else '매도'
            self.push_dashboard_event("체결", f"{symbol} {side_label} {qty}주 @ {float(price):,.0f}원")
        elif event.type == EventType.ERROR:
            msg = getattr(event, 'message', str(event))
            self.push_dashboard_event("오류", msg[:100])
        elif event.type == EventType.RISK_ALERT:
            msg = getattr(event, 'message', str(event))
            self.push_dashboard_event("리스크", msg[:100])

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
        """현재 시장 세션 반환 (SessionUtil 사용)"""
        return SessionUtil.get_current_session()

    def is_trading_hours(self) -> bool:
        """거래 가능 시간 여부 (SessionUtil 사용)"""
        return SessionUtil.is_trading_hours(self.config)

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
                avg_price=Decimal("0"),
                strategy=fill.strategy,
                entry_time=fill.timestamp,
            )

        pos = self.portfolio.positions[symbol]

        # 기존 포지션에 메타데이터 없으면 채우기
        if not pos.strategy and fill.strategy:
            pos.strategy = fill.strategy
        if not pos.entry_time and fill.timestamp:
            pos.entry_time = fill.timestamp

        if fill.side == OrderSide.BUY:
            # 매수 - 평균단가 계산
            new_quantity = pos.quantity + fill.quantity
            if new_quantity > 0:
                total_cost = pos.avg_price * pos.quantity + fill.price * fill.quantity
                pos.avg_price = total_cost / Decimal(str(new_quantity)) if new_quantity != 0 else fill.price
            pos.quantity = new_quantity
            pos.current_price = fill.price  # 미실현 손익 -100% 방지
            self.portfolio.cash -= fill.total_cost

            # 신규 포지션 시 highest_price 초기화
            if pos.highest_price is None or pos.highest_price < fill.price:
                pos.highest_price = fill.price

        else:
            # 매도
            pos.quantity -= fill.quantity

            # 매도 비용 계산 (수수료 + 거래세 0.20%, fee_calculator 기준 통일)
            sell_fee = get_fee_calculator().calculate_sell_fee(fill.total_value)

            # 실현 손익 = (매도가 - 평균단가) × 수량 - 매도비용(수수료+거래세)
            # fill.commission은 브로커 계산 매도수수료인데 sell_fee와 중복이므로 제외
            realized_pnl = (fill.price - pos.avg_price) * fill.quantity - sell_fee

            # 현금 증가 = 매도 대금 - 매도비용
            self.portfolio.cash += fill.total_value - sell_fee
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

    def get_effective_max_positions(self, reserved_cash: Decimal = Decimal("0")) -> int:
        """자산 규모 + 여유자금 기반 실효 최대 포지션 수"""
        risk = self.config.risk
        max_pos = risk.max_positions
        if risk.dynamic_max_positions and risk.min_position_value > 0:
            equity_f = float(self.portfolio.total_equity)
            if equity_f > 0:
                investable = equity_f * (1 - risk.min_cash_reserve_pct / 100)
                per_pos = max(equity_f * risk.base_position_pct / 100, risk.min_position_value)
                calculated = int(investable / per_pos) if per_pos > 0 else 0
                max_pos = min(max(1, calculated), risk.max_positions)
        # Flex: 여유자금 시 추가 슬롯
        # 가용 현금 = get_available_cash()(min_reserve 차감) - 예약금
        # 비율과 금액 모두 동일 기준 사용
        if risk.flex_extra_positions > 0 and float(self.portfolio.total_equity) > 0:
            avail_cash = float(self.get_available_cash() - reserved_cash)
            cash_ratio = avail_cash / float(self.portfolio.total_equity) * 100
            if cash_ratio >= risk.flex_cash_threshold_pct and avail_cash >= risk.min_position_value:
                max_pos = min(max_pos + risk.flex_extra_positions,
                              risk.max_positions + risk.flex_extra_positions)
        return max_pos

    # ============================================================
    # 리스크 체크
    # ============================================================

    def can_open_position(
        self, symbol: str, side: OrderSide, quantity: int, price: Decimal,
        pending_symbols: Optional[Set[str]] = None,
        reserved_cash: Decimal = Decimal("0"),
        sector: Optional[str] = None,
    ) -> tuple[bool, str]:
        """포지션 오픈 가능 여부 체크"""
        risk = self.config.risk
        _pending = pending_symbols or set()

        # 1. 일일 손실 제한 체크 (실현 + 미실현 손익 합산)
        _equity = self.portfolio.total_equity
        effective_pnl = self.portfolio.effective_daily_pnl
        daily_loss_pct = float(effective_pnl / _equity * 100) if _equity > 0 else 0.0
        if daily_loss_pct <= -risk.daily_max_loss_pct:
            return False, f"일일 손실 한도 도달 ({daily_loss_pct:.1f}%, 실현={float(self.portfolio.daily_pnl):+,.0f}, 미실현={float(self.portfolio.total_unrealized_pnl):+,.0f})"

        # 2. (일일 거래 횟수 제한 제거 — 가용 현금이 게이트)

        # 3. 포지션 수 로깅 (하드 제한 없음 — 가용 현금이 게이트)
        if symbol not in self.portfolio.positions:
            effective_positions = len(self.portfolio.positions) + len(
                _pending - set(self.portfolio.positions.keys())
            )
            logger.debug(f"[리스크] 포지션 현황: {effective_positions}개 (보유={len(self.portfolio.positions)})")

        # 3-1. 섹터 분산 체크
        max_per_sector = risk.max_positions_per_sector
        if sector and max_per_sector > 0 and symbol not in self.portfolio.positions:
            same_sector = sum(1 for p in self.portfolio.positions.values() if p.sector == sector)
            if same_sector >= max_per_sector:
                return False, f"섹터 포지션 한도 초과 ({sector}: {same_sector}/{max_per_sector})"

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
        # 미실현 손익 기준선 기록 (전일 보유 포지션의 미실현 손익을 기준점으로)
        self.portfolio.daily_start_unrealized_pnl = self.portfolio.total_unrealized_pnl
        self.portfolio.daily_pnl = Decimal("0")
        self.portfolio.daily_trades = 0
        self.risk_metrics = RiskMetrics()
        logger.info(
            f"일일 통계 초기화 (시작 미실현손익: {self.portfolio.daily_start_unrealized_pnl:+,.0f}원)"
        )


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
                    position = self.engine.portfolio.positions.get(event.symbol)
                    signal = await strategy.on_market_data(event, position=position)
                    if signal:
                        # 현금 없으면 BUY 신호 무시 (SELL은 통과)
                        if no_cash and signal.side == OrderSide.BUY:
                            if len(signals) == 0:  # 첫 번째 차단 시에만 로그 (중복 방지)
                                cash = self.engine.portfolio.cash
                                equity = self.engine.portfolio.total_equity
                                min_reserve = equity * Decimal(str(self.engine.config.risk.min_cash_reserve_pct / 100))
                                logger.warning(
                                    f"[전략→엔진] BUY 신호 차단 중 (가용현금 부족): "
                                    f"현금={cash:,.0f}원, 최소보유={min_reserve:,.0f}원, "
                                    f"포지션={len(self.engine.portfolio.positions)}개"
                                )
                            continue
                        signals.append(SignalEvent.from_signal(signal, source=name))
                except Exception as e:
                    logger.exception(f"전략 오류 ({name}): {e}")

        if signals:
            # 동일 종목에 대해 다중 BUY 신호 → 최고 점수만 통과
            buy_signals = [s for s in signals if s.side == OrderSide.BUY]
            if len(buy_signals) > 1:
                best_buy = max(buy_signals, key=lambda s: s.score)
                sell_signals = [s for s in signals if s.side == OrderSide.SELL]
                logger.info(
                    f"[전략→엔진] {event.symbol} 다중 BUY 신호 {len(buy_signals)}개 → "
                    f"최고점수 {best_buy.score:.1f} ({best_buy.source}) 선택"
                )
                signals = [best_buy] + sell_signals

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

    def __init__(self, engine: TradingEngine, config: RiskConfig, risk_validator=None,
                 sector_lookup=None):
        self.engine = engine
        self.config = config

        # 외부 리스크 검증자 (RiskMgr 인스턴스) — daily_stats 공유용
        self._risk_validator = risk_validator

        # 섹터 조회 콜러블 (async def(symbol) -> Optional[str])
        self._sector_lookup = sector_lookup

        # 주문 실패 쿨다운 추적 (종목별)
        self._order_fail_cooldown: Dict[str, datetime] = {}
        self._COOLDOWN_SECONDS = 300  # 5분 쿨다운

        # 신호 중복 제거: 종목별 마지막 신호 시각 (30초 쿨다운)
        self._last_signal_time: Dict[str, datetime] = {}
        self._SIGNAL_COOLDOWN_SECONDS = 30  # 60→30: 빠른 신호 처리

        # 현금 부족 로그 쓰로틀링
        self._last_cash_warn_time: Optional[datetime] = None

        # 중복 주문 방지: 주문 진행 중인 종목
        self._pending_orders: Set[str] = set()

        # pending 등록 시각 (stale pending 정리용)
        self._pending_timestamps: Dict[str, datetime] = {}
        self._PENDING_TIMEOUT_SECONDS = 600  # 10분 타임아웃

        # 부분 체결 추적: 종목별 미체결 수량
        self._pending_quantities: Dict[str, int] = {}

        # 매도/매수 구분 추적 (매도 미체결 폴백용)
        self._pending_sides: Dict[str, OrderSide] = {}

        # 매도 시장가 폴백 횟수 추적 (무한 루프 방지, 최대 2회)
        self._pending_fallback_count: Dict[str, int] = {}

        # 현금 초과 주문 방지: 주문별 예약 현금 추적 (symbol → 예약 금액)
        self._reserved_by_order: Dict[str, Decimal] = {}

        # 손절 후 재진입 방지: 종목별 손절 시각 (30분간 재진입 차단)
        self._stop_loss_today: Dict[str, datetime] = {}
        self._STOP_LOSS_COOLDOWN_SECONDS = 1800  # 30분

        # 동시성 보호: pending 주문 관련 Lock
        self._pending_lock = asyncio.Lock()

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

        # 만료된 신호 쿨다운 항목 정리
        expired_signals = [s for s, t in self._last_signal_time.items()
                           if (now - t).total_seconds() >= self._SIGNAL_COOLDOWN_SECONDS]
        for s in expired_signals:
            del self._last_signal_time[s]

        # stale pending 주문 정리 (매도: 90초, 매수: 10분 타임아웃)
        _SELL_TIMEOUT = 90  # 매도 지정가 미체결 타임아웃
        stale_sells = []
        stale_buys = []
        for s, t in self._pending_timestamps.items():
            elapsed = (now - t).total_seconds()
            is_sell = self._pending_sides.get(s) == OrderSide.SELL
            if is_sell and elapsed >= _SELL_TIMEOUT:
                stale_sells.append(s)
            elif not is_sell and elapsed >= self._PENDING_TIMEOUT_SECONDS:
                stale_buys.append(s)

        # stale 매도: 지정가 취소 → 시장가 재주문 (최대 2회 폴백)
        _MAX_FALLBACK = 2
        for s in stale_sells:
            # await 중 dict 변경 가능 → 안전 접근
            ts = self._pending_timestamps.get(s)
            if not ts:
                continue  # 이미 다른 경로에서 해제됨
            elapsed = (now - ts).total_seconds()
            fallback_cnt = self._pending_fallback_count.get(s, 0)
            if fallback_cnt >= _MAX_FALLBACK:
                logger.warning(
                    f"[리스크] 매도 폴백 최대 횟수 초과: {s} ({fallback_cnt}회) → pending 해제"
                )
                await self.clear_pending(s)
                continue
            logger.warning(f"[리스크] 매도 미체결 폴백: {s} ({elapsed:.0f}초 초과, 폴백 {fallback_cnt+1}/{_MAX_FALLBACK}회) → 시장가 전환")
            if self.engine.broker and hasattr(self.engine.broker, 'cancel_all_for_symbol'):
                try:
                    await self.engine.broker.cancel_all_for_symbol(s)
                except Exception as e:
                    logger.warning(f"[리스크] 매도 취소 실패: {s} - {e}")
            # 시장가 재주문
            pos = self.engine.portfolio.positions.get(s)
            if pos and pos.quantity > 0:
                fallback_order = Order(
                    symbol=s,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=pos.quantity,
                    reason="미체결 폴백: 시장가 전환",
                )
                try:
                    await self.engine.broker.submit_order(fallback_order)
                    # await 후 dict write → lock 보호
                    async with self._pending_lock:
                        self._pending_timestamps[s] = datetime.now()  # 타이머 리셋
                        self._pending_sides[s] = OrderSide.SELL
                        self._pending_fallback_count[s] = fallback_cnt + 1
                    logger.info(f"[리스크] 시장가 폴백 주문 제출: {s} {pos.quantity}주 (폴백 {fallback_cnt+1}/{_MAX_FALLBACK}회)")
                except Exception as e:
                    logger.error(f"[리스크] 시장가 폴백 주문 실패: {s} - {e}")
                    await self.clear_pending(s)
            else:
                await self.clear_pending(s)

        # stale 매수: 기존 로직 (거래소 취소 + 내부 정리)
        for s in stale_buys:
            ts = self._pending_timestamps.get(s)
            if not ts:
                continue
            elapsed = (now - ts).total_seconds()
            cancel_ok = False
            if self.engine.broker and hasattr(self.engine.broker, 'cancel_all_for_symbol'):
                try:
                    cancelled = await self.engine.broker.cancel_all_for_symbol(s)
                    if cancelled:
                        logger.info(f"[리스크] stale 주문 거래소 취소 완료: {s}")
                        cancel_ok = True
                except Exception as e:
                    logger.warning(f"[리스크] stale 주문 거래소 취소 실패: {s} - {e}")
            else:
                cancel_ok = True  # 브로커 없으면 그냥 정리

            if cancel_ok:
                await self.clear_pending(s)
                logger.error(
                    f"[리스크] stale pending 강제 정리: {s} ({elapsed:.0f}초 초과)"
                )
            else:
                logger.warning(
                    f"[리스크] stale pending 유지: {s} ({elapsed:.0f}초) - 거래소 취소 실패, 다음 주기 재시도"
                )

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

        # 이미 주문 진행 중인 종목 차단 + 신호 쿨다운 체크 (Lock 보호로 TOCTOU 방지)
        async with self._pending_lock:
            # 1차: pending 주문 체크
            if event.symbol in self._pending_orders:
                logger.debug(f"[리스크] 주문 진행 중 차단: {event.symbol}")
                trading_logger.log_signal_blocked(
                    symbol=event.symbol, side=event.side.value,
                    reason="주문진행중",
                    price=float(event.price or 0), score=event.score,
                )
                return None

            # 2차: 신호 중복 제거 (60초 쿨다운)
            if event.symbol in self._last_signal_time:
                elapsed = (now - self._last_signal_time[event.symbol]).total_seconds()
                if elapsed < self._SIGNAL_COOLDOWN_SECONDS:
                    logger.debug(f"[리스크] 신호 쿨다운 차단: {event.symbol} (경과 {elapsed:.1f}초)")
                    trading_logger.log_signal_blocked(
                        symbol=event.symbol, side=event.side.value,
                        reason=f"신호쿨다운({elapsed:.0f}초)",
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

        # 손절 후 재진입 방지 체크 (매수만, 30분간 차단)
        if event.side == OrderSide.BUY and event.symbol in self._stop_loss_today:
            sl_time = self._stop_loss_today[event.symbol]
            elapsed = (datetime.now() - sl_time).total_seconds()
            if elapsed < self._STOP_LOSS_COOLDOWN_SECONDS:
                logger.debug(
                    f"[리스크] 손절 재진입 차단: {event.symbol} "
                    f"(경과 {elapsed/60:.0f}분/{self._STOP_LOSS_COOLDOWN_SECONDS/60:.0f}분)"
                )
                trading_logger.log_signal_blocked(
                    symbol=event.symbol, side=event.side.value,
                    reason=f"손절재진입차단({elapsed/60:.0f}분)",
                    price=float(event.price or 0), score=event.score,
                )
                return None
            else:
                del self._stop_loss_today[event.symbol]

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
            cash = self.engine.get_available_cash()
            logger.warning(
                f"[리스크] 포지션 크기 0: {event.symbol} "
                f"(자산={equity:,.0f}, 현금={cash:,.0f}, 가격={event.price})"
            )
            # 거절되더라도 신호 쿨다운 등록 (동일 종목 신호 반복 방지)
            self._last_signal_time[event.symbol] = datetime.now()
            return None

        # 주문 생성: 매도는 매수1호가 지정가, 매수는 시장가
        if event.side == OrderSide.SELL:
            sell_price = await self._get_sell_price(event.symbol, event.price)
            if sell_price:
                order = Order(
                    symbol=event.symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=position_size,
                    price=sell_price,
                    strategy=event.strategy.value,
                    reason=event.reason,
                    signal_score=event.score
                )
            else:
                # 호가+현재가 모두 실패 → 시장가 폴백
                order = Order(
                    symbol=event.symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=position_size,
                    price=event.price,
                    strategy=event.strategy.value,
                    reason=event.reason,
                    signal_score=event.score
                )
        else:
            order = Order(
                symbol=event.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=position_size,
                price=event.price,
                strategy=event.strategy.value,
                reason=event.reason,
                signal_score=event.score
            )

        # 리스크 체크 (SELL은 포지션 축소이므로 체크 스킵)
        if order.side == OrderSide.BUY:
            # 외부 리스크 검증자 체크 (daily_stats, 연속 손실 등)
            if self._risk_validator:
                can_trade, reason = self._risk_validator.can_open_position(
                    order.symbol, order.side, order.quantity,
                    order.price or Decimal("0"), self.engine.portfolio,
                    strategy_type=order.strategy,
                )
                if not can_trade:
                    logger.warning(f"주문 거부 (리스크 검증): {order.symbol} - {reason}")
                    trading_logger.log_signal_blocked(
                        symbol=order.symbol, side=order.side.value,
                        reason=f"리스크검증:{reason}",
                        price=float(order.price or 0), score=event.score,
                    )
                    return None

            # 섹터 조회 (sector_lookup 콜러블이 있으면 호출)
            _sector = event.metadata.get("sector") if event.metadata else None
            if not _sector and self._sector_lookup:
                try:
                    _sector = await self._sector_lookup(order.symbol)
                except Exception:
                    _sector = None

            can_trade, reason = self.engine.can_open_position(
                order.symbol, order.side, order.quantity, order.price or Decimal("0"),
                pending_symbols=self._pending_orders,
                reserved_cash=self._reserved_cash,
                sector=_sector,
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

        # 중복 주문 방지: pending 등록 + 미체결 수량 추적 - Lock 보호 (TOCTOU 방지)
        async with self._pending_lock:
            # 다시 한번 pending 체크 (Lock 사이에 다른 신호가 끼어들 수 있음)
            if order.symbol in self._pending_orders:
                logger.warning(f"[리스크] 경쟁 조건 감지: {order.symbol} 이미 주문 진행 중 (재검증)")
                return None

            self._pending_orders.add(order.symbol)
            self._pending_quantities[order.symbol] = order.quantity
            self._pending_timestamps[order.symbol] = datetime.now()
            self._pending_sides[order.symbol] = order.side

            # 신호 쿨다운 등록 (60초간 동일 종목 신호 차단)
            self._last_signal_time[order.symbol] = datetime.now()

            # 현금 예약 (매수 주문 금액만큼, 주문별 추적 + 시장가 슬리피지/수수료 버퍼 1.5%)
            if order.side == OrderSide.BUY and order.price and order.quantity:
                self._reserved_by_order[order.symbol] = order.price * order.quantity * Decimal("1.015")

        return [OrderEvent.from_order(order, source="risk_manager")]

    async def _get_sell_price(self, symbol: str, fallback_price: Optional[Decimal]) -> Optional[Decimal]:
        """매도용 최적가 조회: 매수1호가 → fallback_price"""
        if self.engine.broker and hasattr(self.engine.broker, 'get_best_bid'):
            try:
                bid = await self.engine.broker.get_best_bid(symbol)
                if bid and bid > 0:
                    logger.info(f"[리스크] 매도 호가: {symbol} 매수1호가={bid:,.0f}")
                    return Decimal(str(bid))
            except Exception as e:
                logger.warning(f"[리스크] 매수1호가 조회 실패: {symbol} - {e}")
        # 호가 조회 실패 시 event.price(현재가) 사용
        if fallback_price and fallback_price > 0:
            return fallback_price
        return None

    async def clear_pending(self, symbol: str, amount: Decimal = Decimal("0")):
        """주문 완료/실패 시 pending 해제 (외부에서 호출) - Lock 보호"""
        async with self._pending_lock:
            self._pending_orders.discard(symbol)
            self._pending_quantities.pop(symbol, None)
            self._pending_timestamps.pop(symbol, None)
            self._pending_sides.pop(symbol, None)
            self._reserved_by_order.pop(symbol, None)
            self._pending_fallback_count.pop(symbol, None)

    async def on_fill(self, event: FillEvent) -> Optional[List[Event]]:
        """체결 후 리스크 업데이트 (부분 체결 지원) - Lock 보호"""
        # 미체결 수량 감소 → 전량 체결 시에만 pending 해제 및 예약 현금 해제
        async with self._pending_lock:
            remaining = self._pending_quantities.get(event.symbol, 0) - (event.quantity or 0)
            if remaining <= 0:
                self._pending_orders.discard(event.symbol)
                self._pending_quantities.pop(event.symbol, None)
                self._pending_timestamps.pop(event.symbol, None)
                self._pending_sides.pop(event.symbol, None)
                # 전량 체결 시 원래 예약 금액 정확히 해제 (슬리피지 드리프트 방지)
                self._reserved_by_order.pop(event.symbol, None)
                self._pending_fallback_count.pop(event.symbol, None)
            else:
                self._pending_quantities[event.symbol] = remaining
                logger.info(f"[리스크] 부분 체결: {event.symbol} 잔여 {remaining}주")

        # 일일 손실 체크 (실현 + 미실현 손익 합산)
        _equity = self.engine.portfolio.total_equity
        effective_pnl = self.engine.portfolio.effective_daily_pnl
        daily_loss_pct = float(
            effective_pnl / _equity * 100
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

    def _get_hybrid_params(self, strategy: StrategyType, total_equity: Decimal) -> tuple[float, float, Decimal]:
        """하이브리드 모드: 전략별 타임 호라이즌에 따른 자금 풀 파라미터 반환

        Args:
            strategy: 전략 타입
            total_equity: 총 자산

        Returns:
            (base_position_pct, max_position_pct, pool_equity) 튜플
        """
        # 전략 → 타임 호라이즌 매핑
        strategy_horizon_map = {
            StrategyType.MOMENTUM_BREAKOUT: TimeHorizon.SWING,
            StrategyType.GAP_AND_GO: TimeHorizon.SWING,
            StrategyType.THEME_CHASING: TimeHorizon.SHORT_TERM,
            StrategyType.MEAN_REVERSION: TimeHorizon.DAY,
            StrategyType.SCALPING: TimeHorizon.DAY,
        }

        time_horizon = strategy_horizon_map.get(strategy, TimeHorizon.SWING)
        hybrid = self.config.hybrid

        # 타임 호라이즌별 파라미터
        if time_horizon == TimeHorizon.DAY:
            pool_pct = hybrid.day_trading_pct / 100
            base_pct = hybrid.day_base_position_pct / 100
            max_pct = hybrid.day_max_position_pct / 100
        elif time_horizon == TimeHorizon.SHORT_TERM:
            pool_pct = hybrid.short_term_pct / 100
            base_pct = hybrid.short_term_base_position_pct / 100
            max_pct = hybrid.short_term_max_position_pct / 100
        else:  # SWING
            pool_pct = hybrid.swing_pct / 100
            base_pct = hybrid.swing_base_position_pct / 100
            max_pct = hybrid.swing_max_position_pct / 100

        pool_equity = total_equity * Decimal(str(pool_pct))

        return base_pct, max_pct, pool_equity

    def _calculate_position_size(self, signal: SignalEvent) -> int:
        """포지션 크기 계산 (자본 활용률 최적화, 분할익절 최소 수량 보장)"""
        equity = self.engine.portfolio.total_equity
        price = signal.price or Decimal("0")

        if price <= 0 or equity <= 0:
            logger.warning(
                f"[리스크] price/equity 체크 실패: {signal.symbol} "
                f"(price={price}, equity={equity})"
            )
            return 0

        # 하이브리드 모드: 타임 호라이즌별 자금 풀 사용
        if self.config.hybrid.enabled:
            base_pct, max_pct, pool_equity = self._get_hybrid_params(signal.strategy, equity)
        else:
            # 기존 방식
            base_pct = self.config.base_position_pct / 100
            max_pct = self.config.max_position_pct / 100
            pool_equity = equity

        # 신호 강도에 따른 조정
        multiplier = {
            "very_strong": 2.0,
            "strong": 1.5,
            "normal": 1.0,
            "weak": 0.5
        }.get(signal.strength.value, 1.0)

        # 비율 기반 포지션 금액 (풀 내에서 계산)
        position_pct = min(base_pct * multiplier, max_pct)
        pct_value = pool_equity * Decimal(str(position_pct))

        # 가용 현금 (수수료 여유분, 예약 현금 차감)
        available = self.engine.get_available_cash() - self._reserved_cash
        if available <= 0:
            cash = self.engine.get_available_cash()
            reserved = self._reserved_cash
            reserved_orders = len(self._reserved_by_order)
            logger.warning(
                f"[리스크] 가용 현금 부족: {signal.symbol} "
                f"(cash={cash:,.0f}, reserved={reserved:,.0f}, "
                f"reserved_orders={reserved_orders}, available={available:,.0f})"
            )
            return 0

        # 일일 매수 횟수 제한 + 적응형 포지션 사이징
        max_daily_buys = self.config.max_daily_new_buys
        daily_buy_count = self.engine.portfolio.daily_trades

        if daily_buy_count >= max_daily_buys:
            logger.info(f"[리스크] 일일 매수 한도 도달: {daily_buy_count}/{max_daily_buys}")
            return 0

        # 적응형 사이징: 남은 슬롯에 가용현금을 고르게 배분
        remaining_slots = max(max_daily_buys - daily_buy_count, 1)
        adaptive_value = available * Decimal("0.9") / Decimal(str(remaining_slots))

        # 기존 계산과 적응형 중 큰 값 사용 (자본 유휴 방지)
        max_value = equity * Decimal(str(self.config.max_position_pct / 100))
        position_value = min(max(pct_value, adaptive_value), max_value, available)

        # 하락장 포지션 축소 (일일 손실 한도 50% 도달 시 포지션 50% 축소)
        effective_pnl = self.engine.portfolio.effective_daily_pnl
        if equity > 0:
            daily_pnl_pct = float(effective_pnl / equity * 100)
            half_limit = -self.config.daily_max_loss_pct / 2
            if daily_pnl_pct <= half_limit:
                position_value *= Decimal("0.5")
                logger.debug(
                    f"[포지션축소] 일일손실 {daily_pnl_pct:.1f}% "
                    f"(한도의 50% 초과) → 포지션 50% 축소"
                )

        # 전략별 포지션 배율 (역추세 등은 축소) — 최종 금액에 적용
        position_multiplier = 1.0
        if signal.signal and signal.signal.metadata:
            position_multiplier = signal.signal.metadata.get("position_multiplier", 1.0)
        if position_multiplier != 1.0:
            position_value *= Decimal(str(position_multiplier))

        # 최소 포지션 금액 체크
        min_val = Decimal(str(self.config.min_position_value))
        if position_value < min_val:
            logger.warning(
                f"[리스크] 포지션 금액 미달: {signal.symbol} "
                f"(position_value={position_value:,.0f} < min_val={min_val:,.0f}, "
                f"pct_value={pct_value:,.0f}, max_value={max_value:,.0f}, "
                f"available={available:,.0f})"
            )
            return 0

        # 수량 계산 (시장가 주문 시 상한가 +30% 증거금 고려)
        quantity = int(position_value / price)
        max_qty_for_market = int(available / (price * Decimal("1.3")))
        if max_qty_for_market < quantity:
            quantity = max_qty_for_market

        # 최소 수량 체크: 분할 익절에 최소 3주 권장
        MIN_QTY_FOR_PARTIAL_EXIT = 3
        if quantity < MIN_QTY_FOR_PARTIAL_EXIT:
            cost_for_min = price * MIN_QTY_FOR_PARTIAL_EXIT * Decimal("1.001")
            if cost_for_min <= available and cost_for_min <= max_value:
                quantity = MIN_QTY_FOR_PARTIAL_EXIT
            elif quantity >= 1:
                # 3주는 못 사지만 1~2주는 가능 → 분할익절 불가하나 거래 허용
                logger.info(
                    f"[리스크] 분할익절 불가 수량: {signal.symbol} "
                    f"(수량={quantity}, 3주 비용={cost_for_min:,.0f}, 가용={available:,.0f})"
                )
            else:
                return 0

        return max(quantity, 0)
