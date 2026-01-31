"""
AI Trading Bot v2 - 핵심 타입 정의

모든 도메인 객체와 열거형 타입을 정의합니다.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import Optional, Dict, Any, List
import uuid


# ============================================================
# 열거형 (Enums)
# ============================================================

class Market(str, Enum):
    """거래 시장"""
    KRX = "KRX"           # 한국거래소 (정규장)
    KRX_EXT = "KRX_EXT"   # 한국거래소 (시간외/넥스트)
    NASDAQ = "NASDAQ"     # 나스닥
    NYSE = "NYSE"         # 뉴욕증권거래소


class MarketSession(str, Enum):
    """시장 세션"""
    PRE_MARKET = "pre_market"      # 프리장 (08:00~08:50)
    REGULAR = "regular"            # 정규장 (09:00~15:30)
    AFTER_HOURS = "after_hours"    # 시간외 (15:40~16:00)
    NEXT = "next"                  # 넥스트장 (15:30~20:00)
    CLOSED = "closed"              # 장 마감


# TradingSession은 MarketSession의 alias (하위호환)
TradingSession = MarketSession


class OrderSide(str, Enum):
    """주문 방향"""
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """주문 유형"""
    MARKET = "market"        # 시장가
    LIMIT = "limit"          # 지정가
    STOP = "stop"            # 스탑
    STOP_LIMIT = "stop_limit"  # 스탑 리밋


class OrderStatus(str, Enum):
    """주문 상태"""
    PENDING = "pending"          # 대기
    SUBMITTED = "submitted"      # 제출됨
    PARTIAL = "partial"          # 부분 체결
    FILLED = "filled"            # 완전 체결
    CANCELLED = "cancelled"      # 취소됨
    REJECTED = "rejected"        # 거부됨
    EXPIRED = "expired"          # 만료됨


class PositionSide(str, Enum):
    """포지션 방향"""
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalStrength(str, Enum):
    """신호 강도"""
    VERY_STRONG = "very_strong"  # 매우 강함
    STRONG = "strong"            # 강함
    NORMAL = "normal"            # 보통
    WEAK = "weak"                # 약함


class StrategyType(str, Enum):
    """전략 유형"""
    MOMENTUM_BREAKOUT = "momentum_breakout"
    THEME_CHASING = "theme_chasing"
    GAP_AND_GO = "gap_and_go"
    MEAN_REVERSION = "mean_reversion"
    SCALPING = "scalping"


# ============================================================
# 데이터 클래스 (Data Classes)
# ============================================================

@dataclass
class Symbol:
    """종목 정보"""
    code: str                     # 종목코드 (예: 005930)
    name: str                     # 종목명 (예: 삼성전자)
    market: Market = Market.KRX   # 시장
    sector: Optional[str] = None  # 섹터

    @property
    def full_code(self) -> str:
        """전체 코드 (시장 포함)"""
        return f"{self.market.value}:{self.code}"


@dataclass
class Price:
    """가격 정보 (OHLCV)"""
    symbol: str                   # 종목코드
    timestamp: datetime           # 시간
    open: Decimal                 # 시가
    high: Decimal                 # 고가
    low: Decimal                  # 저가
    close: Decimal                # 종가 (현재가)
    volume: int                   # 거래량
    value: Optional[Decimal] = None  # 거래대금

    @property
    def typical_price(self) -> Decimal:
        """대표가격 (HLC 평균)"""
        return (self.high + self.low + self.close) / 3


@dataclass
class Quote:
    """호가 정보"""
    symbol: str
    timestamp: datetime
    bid_price: Decimal            # 매수 호가
    bid_size: int                 # 매수 잔량
    ask_price: Decimal            # 매도 호가
    ask_size: int                 # 매도 잔량

    @property
    def spread(self) -> Decimal:
        """스프레드"""
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> Decimal:
        """중간가"""
        return (self.bid_price + self.ask_price) / 2


@dataclass
class Order:
    """주문"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.LIMIT
    quantity: int = 0
    price: Optional[Decimal] = None        # 지정가 (시장가면 None)
    stop_price: Optional[Decimal] = None   # 스탑 가격

    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    filled_price: Optional[Decimal] = None

    strategy: Optional[str] = None         # 전략명
    reason: Optional[str] = None           # 주문 사유

    created_at: datetime = field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None
    broker_order_id: Optional[str] = None  # 브로커 주문번호

    @property
    def is_active(self) -> bool:
        """활성 주문 여부"""
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

    @property
    def remaining_quantity(self) -> int:
        """미체결 수량"""
        return self.quantity - self.filled_quantity


@dataclass
class Fill:
    """체결 정보"""
    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    commission: Decimal = Decimal("0")
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def total_value(self) -> Decimal:
        """총 체결금액"""
        return self.price * self.quantity

    @property
    def total_cost(self) -> Decimal:
        """총 비용 (수수료 포함)"""
        return self.total_value + self.commission


@dataclass
class Position:
    """포지션"""
    symbol: str
    name: str = ""
    side: PositionSide = PositionSide.FLAT
    quantity: int = 0
    avg_price: Decimal = Decimal("0")
    current_price: Decimal = Decimal("0")

    # 리스크 관리
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    trailing_stop_pct: Optional[float] = None
    highest_price: Optional[Decimal] = None   # 트레일링용

    # 메타데이터
    strategy: Optional[str] = None
    entry_time: Optional[datetime] = None

    @property
    def market_value(self) -> Decimal:
        """시장가치"""
        return self.current_price * self.quantity

    @property
    def cost_basis(self) -> Decimal:
        """취득원가"""
        return self.avg_price * self.quantity

    @property
    def unrealized_pnl(self) -> Decimal:
        """미실현 손익"""
        if self.quantity == 0:
            return Decimal("0")
        return self.market_value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> float:
        """미실현 손익률 (%)"""
        if self.cost_basis == 0:
            return 0.0
        return float(self.unrealized_pnl / self.cost_basis * 100)

    @property
    def is_profit(self) -> bool:
        """수익 상태"""
        return self.unrealized_pnl > 0


@dataclass
class Portfolio:
    """포트폴리오"""
    cash: Decimal = Decimal("0")
    positions: Dict[str, Position] = field(default_factory=dict)
    initial_capital: Decimal = Decimal("0")

    # 일일 통계
    daily_pnl: Decimal = Decimal("0")
    daily_trades: int = 0

    @property
    def total_position_value(self) -> Decimal:
        """총 포지션 가치"""
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_equity(self) -> Decimal:
        """총 자산"""
        return self.cash + self.total_position_value

    @property
    def total_pnl(self) -> Decimal:
        """총 손익"""
        return self.total_equity - self.initial_capital

    @property
    def total_pnl_pct(self) -> float:
        """총 손익률 (%)"""
        if self.initial_capital == 0:
            return 0.0
        return float(self.total_pnl / self.initial_capital * 100)

    @property
    def cash_ratio(self) -> float:
        """현금 비율"""
        if self.total_equity == 0:
            return 1.0
        return float(self.cash / self.total_equity)


@dataclass
class Signal:
    """매매 신호"""
    symbol: str
    side: OrderSide
    strength: SignalStrength
    strategy: StrategyType

    price: Optional[Decimal] = None
    target_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None

    score: float = 0.0              # 신호 점수 (0~100)
    confidence: float = 0.0         # 신뢰도 (0~1)

    reason: str = ""                # 신호 생성 사유
    metadata: Dict[str, Any] = field(default_factory=dict)

    timestamp: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None  # 신호 만료 시간

    @property
    def is_buy(self) -> bool:
        return self.side == OrderSide.BUY

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at


@dataclass
class Theme:
    """테마 정보"""
    name: str                       # 테마명 (예: AI/반도체)
    keywords: List[str]             # 관련 키워드
    symbols: List[str]              # 관련 종목
    score: float = 0.0              # 테마 강도 (0~100)

    news_count: int = 0             # 관련 뉴스 수
    price_momentum: float = 0.0     # 관련 종목 평균 모멘텀

    detected_at: datetime = field(default_factory=datetime.now)

    @property
    def is_hot(self) -> bool:
        """핫 테마 여부"""
        return self.score > 70


@dataclass
class TradeResult:
    """거래 결과"""
    symbol: str
    side: OrderSide
    entry_price: Decimal
    exit_price: Decimal
    quantity: int

    entry_time: datetime
    exit_time: datetime

    strategy: str
    reason: str = ""

    @property
    def pnl(self) -> Decimal:
        """손익"""
        if self.side == OrderSide.BUY:
            return (self.exit_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.exit_price) * self.quantity

    @property
    def pnl_pct(self) -> float:
        """손익률 (%)"""
        if not self.entry_price:
            return 0.0
        return float((self.exit_price - self.entry_price) / self.entry_price * 100)

    @property
    def holding_time(self) -> float:
        """보유 시간 (분)"""
        return (self.exit_time - self.entry_time).total_seconds() / 60

    @property
    def is_win(self) -> bool:
        """승리 여부"""
        return self.pnl > 0


@dataclass
class RiskMetrics:
    """리스크 지표"""
    # 일일 제한
    daily_loss: Decimal = Decimal("0")
    daily_loss_pct: float = 0.0
    daily_trades: int = 0

    # 포지션 제한
    max_position_value: Decimal = Decimal("0")
    total_exposure: float = 0.0

    # 상태
    is_daily_loss_limit_hit: bool = False
    is_max_trades_hit: bool = False
    can_trade: bool = True

    # 연속 손실
    consecutive_losses: int = 0


# ============================================================
# 설정 타입
# ============================================================

@dataclass
class RiskConfig:
    """리스크 설정"""
    # 일일 한도
    daily_max_loss_pct: float = 5.0
    daily_max_trades: int = 20

    # 포지션 관리
    base_position_pct: float = 10.0    # 기본 포지션 비율 (10% - 보수적)
    max_position_pct: float = 30.0     # 최대 포지션 비율 (30%로 하향)
    max_positions: int = 5             # 최대 동시 포지션 수 (분산투자)
    min_cash_reserve_pct: float = 20.0 # 최소 현금 예비 (안전마진)
    min_position_value: int = 500000   # 최소 포지션 금액 (50만원 — 이하면 매수 안 함)
    dynamic_max_positions: bool = True # 자산 규모에 따라 max_positions 동적 조정

    # 손절/익절
    default_stop_loss_pct: float = 2.0
    default_take_profit_pct: float = 3.0
    trailing_stop_pct: float = 1.5

    # 특별 상황
    hot_theme_position_pct: float = 70.0
    momentum_multiplier: float = 1.5


@dataclass
class TradingConfig:
    """트레이딩 설정"""
    # 기본 설정
    initial_capital: Decimal = Decimal("10000000")  # fallback (실제값은 KIS API에서 동기화)
    market: Market = Market.KRX

    # 수수료
    buy_fee_rate: float = 0.00015      # 0.015%
    sell_fee_rate: float = 0.00315     # 0.015% + 세금 0.30%

    # 슬리피지
    expected_slippage_ticks: int = 1
    max_slippage_ticks: int = 3

    # 시간대
    enable_pre_market: bool = True
    enable_next_market: bool = True

    # 리스크
    risk: RiskConfig = field(default_factory=RiskConfig)
