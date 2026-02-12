"""
AI Trading Bot v2 - 이벤트 시스템

이벤트 기반 아키텍처의 핵심 이벤트 정의
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import Optional, Dict, Any, List
import uuid

from .types import (
    Order, Fill, Position, Signal, Price, Quote, Theme,
    OrderSide, OrderStatus, SignalStrength, StrategyType, MarketSession
)


class EventType(Enum):
    """이벤트 유형"""
    # 시장 데이터
    MARKET_DATA = auto()        # 시세 데이터
    QUOTE = auto()              # 호가 데이터
    TICK = auto()               # 틱 데이터

    # 거래
    SIGNAL = auto()             # 매매 신호
    ORDER = auto()              # 주문
    FILL = auto()               # 체결
    POSITION = auto()           # 포지션 변경

    # 리스크
    RISK_ALERT = auto()         # 리스크 경고
    STOP_TRIGGERED = auto()     # 손절/익절 트리거

    # 테마/뉴스
    THEME = auto()              # 테마 감지
    NEWS = auto()               # 뉴스

    # 시스템
    HEARTBEAT = auto()          # 하트비트
    SESSION = auto()            # 세션 변경
    ERROR = auto()              # 에러
    LOG = auto()                # 로그


@dataclass
class Event:
    """이벤트 기본 클래스"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    type: EventType = EventType.HEARTBEAT
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = ""            # 이벤트 소스
    priority: int = 5           # 우선순위 (1=최고, 10=최저)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __lt__(self, other):
        """우선순위 비교 (heap용)"""
        if not isinstance(other, Event):
            return NotImplemented
        # 우선순위가 같으면 시간순
        if self.priority == other.priority:
            return self.timestamp < other.timestamp
        return self.priority < other.priority


# ============================================================
# 시장 데이터 이벤트
# ============================================================

@dataclass
class MarketDataEvent(Event):
    """시장 데이터 이벤트 (OHLCV)"""
    type: EventType = EventType.MARKET_DATA

    symbol: str = ""
    open: Decimal = Decimal("0")
    high: Decimal = Decimal("0")
    low: Decimal = Decimal("0")
    close: Decimal = Decimal("0")
    volume: int = 0
    value: Decimal = Decimal("0")       # 거래대금

    prev_close: Optional[Decimal] = None  # 전일 종가
    change: Decimal = Decimal("0")        # 전일 대비
    change_pct: float = 0.0               # 전일 대비 (%)

    def to_price(self) -> Price:
        """Price 객체로 변환"""
        return Price(
            symbol=self.symbol,
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            value=self.value
        )


@dataclass
class QuoteEvent(Event):
    """호가 이벤트"""
    type: EventType = EventType.QUOTE
    priority: int = 2  # 높은 우선순위

    symbol: str = ""
    bid_price: Decimal = Decimal("0")
    bid_size: int = 0
    ask_price: Decimal = Decimal("0")
    ask_size: int = 0

    # 호가창 (선택적)
    bid_prices: List[Decimal] = field(default_factory=list)
    bid_sizes: List[int] = field(default_factory=list)
    ask_prices: List[Decimal] = field(default_factory=list)
    ask_sizes: List[int] = field(default_factory=list)

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> Decimal:
        return (self.bid_price + self.ask_price) / 2

    def to_quote(self) -> Quote:
        """Quote 객체로 변환"""
        return Quote(
            symbol=self.symbol,
            timestamp=self.timestamp,
            bid_price=self.bid_price,
            bid_size=self.bid_size,
            ask_price=self.ask_price,
            ask_size=self.ask_size
        )


@dataclass
class TickEvent(Event):
    """틱 이벤트 (체결 데이터)"""
    type: EventType = EventType.TICK
    priority: int = 2

    symbol: str = ""
    price: Decimal = Decimal("0")
    size: int = 0
    side: Optional[OrderSide] = None  # 매수/매도 체결


# ============================================================
# 거래 이벤트
# ============================================================

@dataclass
class SignalEvent(Event):
    """매매 신호 이벤트"""
    type: EventType = EventType.SIGNAL
    priority: int = 3

    signal: Optional[Signal] = None

    # 간편 접근용
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    strength: SignalStrength = SignalStrength.NORMAL
    strategy: StrategyType = StrategyType.MOMENTUM_BREAKOUT

    price: Optional[Decimal] = None
    target_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None

    score: float = 0.0
    confidence: float = 0.0
    reason: str = ""

    @classmethod
    def from_signal(cls, signal: Signal, source: str = "") -> "SignalEvent":
        """Signal 객체로부터 생성"""
        return cls(
            source=source,
            signal=signal,
            symbol=signal.symbol,
            side=signal.side,
            strength=signal.strength,
            strategy=signal.strategy,
            price=signal.price,
            target_price=signal.target_price,
            stop_price=signal.stop_price,
            score=signal.score,
            confidence=signal.confidence,
            reason=signal.reason,
            metadata=dict(signal.metadata) if signal.metadata else {}
        )


@dataclass
class OrderEvent(Event):
    """주문 이벤트"""
    type: EventType = EventType.ORDER
    priority: int = 1  # 최고 우선순위

    order: Optional[Order] = None

    # 간편 접근용
    order_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: int = 0
    price: Optional[Decimal] = None
    status: OrderStatus = OrderStatus.PENDING

    @classmethod
    def from_order(cls, order: Order, source: str = "") -> "OrderEvent":
        """Order 객체로부터 생성"""
        return cls(
            source=source,
            order=order,
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            status=order.status
        )


@dataclass
class FillEvent(Event):
    """체결 이벤트"""
    type: EventType = EventType.FILL
    priority: int = 1

    fill: Optional[Fill] = None

    # 간편 접근용
    order_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: int = 0
    price: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")

    @classmethod
    def from_fill(cls, fill: Fill, source: str = "") -> "FillEvent":
        """Fill 객체로부터 생성"""
        return cls(
            source=source,
            fill=fill,
            order_id=fill.order_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            commission=fill.commission
        )


@dataclass
class PositionEvent(Event):
    """포지션 변경 이벤트"""
    type: EventType = EventType.POSITION
    priority: int = 3

    position: Optional[Position] = None
    action: str = ""  # "opened", "increased", "decreased", "closed"

    symbol: str = ""
    quantity_change: int = 0
    pnl: Optional[Decimal] = None


# ============================================================
# 리스크 이벤트
# ============================================================

@dataclass
class RiskAlertEvent(Event):
    """리스크 경고 이벤트"""
    type: EventType = EventType.RISK_ALERT
    priority: int = 1  # 최고 우선순위

    alert_type: str = ""          # "daily_loss", "max_position", "consecutive_loss"
    message: str = ""
    current_value: float = 0.0
    threshold: float = 0.0
    action: str = ""              # "warn", "block", "liquidate"

    @property
    def is_critical(self) -> bool:
        return self.action in ("block", "liquidate")


@dataclass
class StopTriggeredEvent(Event):
    """손절/익절 트리거 이벤트"""
    type: EventType = EventType.STOP_TRIGGERED
    priority: int = 1

    symbol: str = ""
    trigger_type: str = ""        # "stop_loss", "take_profit", "trailing_stop"
    trigger_price: Decimal = Decimal("0")
    current_price: Decimal = Decimal("0")
    position_side: str = ""       # "long", "short"


# ============================================================
# 테마/뉴스 이벤트
# ============================================================

@dataclass
class ThemeEvent(Event):
    """테마 감지 이벤트"""
    type: EventType = EventType.THEME
    priority: int = 4

    theme: Optional[Theme] = None

    name: str = ""
    keywords: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    score: float = 0.0

    @classmethod
    def from_theme(cls, theme: Theme, source: str = "") -> "ThemeEvent":
        """Theme 객체로부터 생성"""
        return cls(
            source=source,
            theme=theme,
            name=theme.name,
            keywords=theme.keywords,
            symbols=theme.symbols,
            score=theme.score
        )


@dataclass
class NewsEvent(Event):
    """뉴스 이벤트"""
    type: EventType = EventType.NEWS
    priority: int = 4

    title: str = ""
    content: str = ""
    url: str = ""
    source_name: str = ""         # 뉴스 출처

    symbols: List[str] = field(default_factory=list)  # 관련 종목
    themes: List[str] = field(default_factory=list)   # 관련 테마
    sentiment: float = 0.0        # 감성 점수 (-1 ~ 1)


# ============================================================
# 시스템 이벤트
# ============================================================

@dataclass
class SessionEvent(Event):
    """세션 변경 이벤트"""
    type: EventType = EventType.SESSION
    priority: int = 2

    session: MarketSession = MarketSession.CLOSED
    prev_session: Optional[MarketSession] = None


@dataclass
class HeartbeatEvent(Event):
    """하트비트 이벤트"""
    type: EventType = EventType.HEARTBEAT
    priority: int = 10  # 최저 우선순위

    uptime_seconds: float = 0.0
    memory_mb: float = 0.0
    active_positions: int = 0
    pending_orders: int = 0


@dataclass
class ErrorEvent(Event):
    """에러 이벤트"""
    type: EventType = EventType.ERROR
    priority: int = 1

    error_type: str = ""
    message: str = ""
    traceback: str = ""
    recoverable: bool = True


@dataclass
class LogEvent(Event):
    """로그 이벤트"""
    type: EventType = EventType.LOG
    priority: int = 9

    level: str = "INFO"           # DEBUG, INFO, WARNING, ERROR
    message: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
