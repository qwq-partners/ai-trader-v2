#!/usr/bin/env python3
"""
AI Trading Bot v2 - 메인 트레이더 실행 스크립트

사용법:
    python scripts/run_trader.py [--config CONFIG_PATH] [--dry-run]
"""

import argparse
import asyncio
import signal
import sys
import aiohttp
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional, Set

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from loguru import logger

from src.core.engine import TradingEngine, StrategyManager, RiskManager, is_kr_market_holiday, set_kr_market_holidays
from src.data.providers.kis_market_data import KISMarketData, get_kis_market_data
from src.data.providers.us_market_data import USMarketData, get_us_market_data
from src.core.types import TradingConfig
from src.core.event import EventType, MarketDataEvent, SessionEvent, ThemeEvent, NewsEvent
from src.core.types import TradingSession
from src.execution.broker.kis_broker import KISBroker, KISConfig
from src.strategies.momentum import MomentumBreakoutStrategy, MomentumConfig
from src.strategies.theme_chasing import ThemeChasingStrategy, ThemeChasingConfig
from src.strategies.gap_and_go import GapAndGoStrategy, GapAndGoConfig
from src.strategies.mean_reversion import MeanReversionStrategy, MeanReversionConfig
from src.risk.manager import RiskManager as RiskMgr
from src.data.feeds.kis_websocket import KISWebSocketFeed, KISWebSocketConfig
from src.core.types import MarketSession
from src.signals.sentiment.theme_detector import ThemeDetector, get_theme_detector
from src.data.storage.stock_master import StockMaster, get_stock_master
from src.signals.screener import StockScreener, get_screener
from src.strategies.exit_manager import ExitManager, ExitConfig, get_exit_manager
from src.utils.config import AppConfig
from src.utils.logger import setup_logger, trading_logger
from src.analytics.daily_report import get_report_generator
from src.utils.telegram import send_alert
from src.core.evolution import (
    get_trade_journal, get_trade_reviewer, get_strategy_evolver
)
from src.dashboard.server import DashboardServer
from bot_schedulers import SchedulerMixin


class TradingBot(SchedulerMixin):
    """AI 트레이딩 봇"""

    def __init__(self, config: AppConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.running = False

        # 컴포넌트 초기화
        self.engine = TradingEngine(config.trading)
        self.broker: Optional[KISBroker] = None
        self.strategy_manager: Optional[StrategyManager] = None
        self.risk_manager: Optional[RiskMgr] = None

        # 실시간 데이터 피드
        self.ws_feed: Optional[KISWebSocketFeed] = None

        # 테마 탐지기
        self.theme_detector: Optional[ThemeDetector] = None

        # 분할 익절/청산 관리자
        self.exit_manager: Optional[ExitManager] = None

        # 종목 스크리너
        self.screener: Optional[StockScreener] = None
        self._screening_interval: int = 600  # 기본 10분

        # 일일 레포트 생성기
        self.report_generator = None

        # 자가 진화 엔진
        self.trade_journal = None
        self.strategy_evolver = None

        # 감시 종목
        self._watch_symbols: list = []

        # 전략별 청산 파라미터 (ExitManager에 전달용)
        self._strategy_exit_params: Dict[str, Dict[str, float]] = {}
        # 종목별 전략 매핑 (ExitManager 등록 시 사용)
        self._symbol_strategy: Dict[str, str] = {}
        # 종목별 신호 정보 (TradeJournal 기록용)
        self._symbol_signals: Dict[str, Any] = {}
        self._exit_pending_symbols: Set[str] = set()  # ExitManager 매도 중복 방지
        self._exit_pending_timestamps: Dict[str, datetime] = {}  # 매도 pending 타임스탬프
        self._pause_resume_at: Optional[datetime] = None  # 자동 재개 타이머
        self._watch_symbols_lock = asyncio.Lock()
        self._portfolio_lock = asyncio.Lock()

        # 대시보드 서버
        self.dashboard: Optional[DashboardServer] = None

        # KIS 시장 데이터 조회 클라이언트
        self.kis_market_data: Optional[KISMarketData] = None

        # US 시장 오버나이트 데이터 클라이언트
        self.us_market_data: Optional[USMarketData] = None

        # 종목 마스터
        self.stock_master: Optional[StockMaster] = None

        # 종목명 캐시 (symbol → name)
        self.stock_name_cache: Dict[str, str] = {}

        # 시그널 핸들러
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """종료 시그널 핸들러"""
        def handle_shutdown(signum, frame):
            logger.warning(f"종료 신호 수신 ({signum})")
            self.stop()

        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)

    async def _send_error_alert(self, error_type: str, message: str, details: str = ""):
        """
        에러 알림 (로그만 기록, 텔레그램 미발송)

        텔레그램은 8시/17시 레포트만 발송합니다.
        """
        logger.warning(f"[알림] {error_type}: {message}" + (f" | {details[:200]}" if details else ""))

    async def initialize(self) -> bool:
        """컴포넌트 초기화"""
        try:
            logger.info("=== AI Trading Bot v2 초기화 ===")
            logger.info(f"Dry Run: {self.dry_run}")

            # 브로커 초기화 및 실제 계좌 잔고 로드
            if not self.dry_run:
                self.broker = KISBroker(KISConfig.from_env())
                if not await self.broker.connect():
                    logger.error("브로커 연결 실패")
                    return False

                # 계좌 잔고에서 실제 자본 가져오기
                balance = await self.broker.get_account_balance()
                if balance:
                    actual_capital = balance.get('total_equity', 0)
                    available_cash = balance.get('available_cash', 0)
                    stock_value = balance.get('stock_value', 0)

                    # 실제 자본으로 엔진 업데이트
                    if actual_capital > 0:
                        self.engine.portfolio.initial_capital = Decimal(str(actual_capital))
                        self.engine.portfolio.cash = Decimal(str(available_cash))
                        self.config.trading.initial_capital = actual_capital

                        logger.info(f"=== 실제 계좌 잔고 ===")
                        logger.info(f"  초기자본(총자산): {actual_capital:,.0f}원")
                        logger.info(f"  주문가능금액:     {available_cash:,.0f}원")
                        logger.info(f"  주식평가금액:     {stock_value:,.0f}원")

                        # 기존 보유 종목 로드
                        await self._load_existing_positions()
                    else:
                        logger.warning(f"계좌 잔고 조회 실패, 설정값 사용: {self.config.trading.initial_capital:,}원")
                        self.engine.portfolio.initial_capital = Decimal(str(self.config.trading.initial_capital))
                        self.engine.portfolio.cash = Decimal(str(self.config.trading.initial_capital))
                else:
                    logger.warning(f"계좌 잔고 조회 실패, 설정값 사용: {self.config.trading.initial_capital:,}원")
                    self.engine.portfolio.initial_capital = Decimal(str(self.config.trading.initial_capital))
                    self.engine.portfolio.cash = Decimal(str(self.config.trading.initial_capital))
            else:
                logger.info(f"Dry Run 모드: 설정 자본 사용 ({self.config.trading.initial_capital:,}원)")
                self.engine.portfolio.initial_capital = Decimal(str(self.config.trading.initial_capital))
                self.engine.portfolio.cash = Decimal(str(self.config.trading.initial_capital))

            # KIS 시장 데이터 클라이언트 초기화 + 동적 휴장일 로드
            self.kis_market_data = get_kis_market_data()
            try:
                now = datetime.now()
                cur_month = now.strftime("%Y%m")
                next_month = (now.replace(day=1) + timedelta(days=32)).strftime("%Y%m")
                h1 = await self.kis_market_data.fetch_holidays(cur_month)
                h2 = await self.kis_market_data.fetch_holidays(next_month)
                all_holidays = h1 | h2
                if all_holidays:
                    set_kr_market_holidays(all_holidays)
            except Exception as e:
                logger.warning(f"동적 휴장일 로드 실패 (주말만 체크): {e}")
            logger.info("KIS 시장 데이터 클라이언트 초기화 완료")

            # US 시장 오버나이트 데이터 클라이언트 초기화
            us_market_cfg = self.config.get("us_market") or {}
            if us_market_cfg.get("enabled", True):
                self.us_market_data = get_us_market_data()
                logger.info("US 시장 오버나이트 데이터 클라이언트 초기화 완료")

            # 종목 마스터 초기화
            self._stock_master_config = self.config.get("stock_master") or {}
            sm_cfg = self._stock_master_config
            if sm_cfg.get("enabled", True):
                self.stock_master = get_stock_master()
                if await self.stock_master.connect():
                    # 테이블이 비어있으면 초기 갱신 실행
                    if await self.stock_master.is_empty():
                        logger.info("[종목마스터] 빈 테이블 감지 → 초기 갱신 실행")
                        try:
                            await self.stock_master.refresh_master()
                        except Exception as e:
                            logger.warning(f"[종목마스터] 초기 갱신 실패 (무시): {e}")
                    else:
                        # 캐시만 재구축
                        await self.stock_master.rebuild_cache()
                    logger.info("종목 마스터 초기화 완료")
                else:
                    logger.warning("종목 마스터 DB 연결 실패 (무시)")
                    self.stock_master = None

            # 테마 탐지기 초기화 (kis_market_data + us_market_data 연동)
            theme_cfg = self.config.get("theme_detector") or {}
            self.theme_detector = ThemeDetector(
                kis_market_data=self.kis_market_data,
                us_market_data=self.us_market_data,
                stock_master=self.stock_master,
            )
            self.theme_detector.detection_interval_minutes = theme_cfg.get("scan_interval_minutes", 15)
            self.theme_detector.min_news_count = theme_cfg.get("min_news_count", 3)
            self.theme_detector.hot_theme_threshold = theme_cfg.get("min_theme_score", 70.0)
            # 전역 싱글톤 등록 (get_theme_detector() 호출 시 동일 인스턴스 반환)
            import src.signals.sentiment.theme_detector as _td_mod
            _td_mod._theme_detector = self.theme_detector
            logger.info("테마 탐지기 초기화 완료")

            # 전략 매니저 초기화
            self.strategy_manager = StrategyManager(self.engine)

            # 모멘텀 전략 등록
            momentum_cfg = self.config.get("strategies", "momentum_breakout") or {}
            if momentum_cfg.get("enabled", True):
                momentum_strategy = MomentumBreakoutStrategy(MomentumConfig(
                    stop_loss_pct=momentum_cfg.get("stop_loss_pct", 2.5),
                    take_profit_pct=momentum_cfg.get("take_profit_pct", 5.0),
                    trailing_stop_pct=momentum_cfg.get("trailing_stop_pct", 1.5),
                    volume_surge_ratio=momentum_cfg.get("volume_surge_ratio", 2.0),
                ))
                self.strategy_manager.register_strategy("momentum_breakout", momentum_strategy)
                logger.info("모멘텀 브레이크아웃 전략 등록")

            # 테마 추종 전략 등록
            theme_strategy_cfg = self.config.get("strategies", "theme_chasing") or {}
            if theme_strategy_cfg.get("enabled", True):
                theme_strategy = ThemeChasingStrategy(
                    config=ThemeChasingConfig(
                        min_theme_score=theme_strategy_cfg.get("min_theme_score", 50.0),
                        stop_loss_pct=theme_strategy_cfg.get("stop_loss_pct", 2.0),
                        take_profit_pct=theme_strategy_cfg.get("take_profit_pct", 4.0),
                        trailing_stop_pct=theme_strategy_cfg.get("trailing_stop_pct", 1.0),
                    ),
                    kis_market_data=self.kis_market_data,
                )
                theme_strategy.set_theme_detector(self.theme_detector)
                self.strategy_manager.register_strategy("theme_chasing", theme_strategy)
                logger.info("테마 추종 전략 등록")

            # 갭상승 추종 전략 등록
            gap_cfg = self.config.get("strategies", "gap_and_go") or {}
            if gap_cfg.get("enabled", True):
                gap_strategy = GapAndGoStrategy(GapAndGoConfig(
                    min_gap_pct=gap_cfg.get("min_gap_pct", 2.0),
                    max_gap_pct=gap_cfg.get("max_gap_pct", 10.0),
                    entry_delay_minutes=gap_cfg.get("entry_delay_minutes", 30),
                    pullback_pct=gap_cfg.get("pullback_pct", 1.0),
                    min_volume_ratio=gap_cfg.get("min_volume_ratio", 2.0),
                    stop_loss_pct=gap_cfg.get("stop_loss_pct", 2.0),
                    take_profit_pct=gap_cfg.get("take_profit_pct", 4.0),
                    trailing_stop_pct=gap_cfg.get("trailing_stop_pct", 1.5),
                ))
                self.strategy_manager.register_strategy("gap_and_go", gap_strategy)
                logger.info("갭상승 추종 전략 등록")

            # 평균 회귀 전략 등록
            mr_cfg = self.config.get("strategies", "mean_reversion") or {}
            if mr_cfg.get("enabled", True):
                mr_strategy = MeanReversionStrategy(MeanReversionConfig(
                    max_rsi=mr_cfg.get("max_rsi", 30.0),
                    min_decline_pct=mr_cfg.get("min_decline_pct", -10.0),
                    min_volume_ratio=mr_cfg.get("min_volume_ratio", 1.5),
                    stop_loss_pct=mr_cfg.get("stop_loss_pct", 3.0),
                    take_profit_pct=mr_cfg.get("take_profit_pct", 5.0),
                    trailing_stop_pct=mr_cfg.get("trailing_stop_pct", 2.0),
                ))
                self.strategy_manager.register_strategy("mean_reversion", mr_strategy)
                logger.info("평균 회귀 전략 등록")

            # 전략별 청산 파라미터 기록 (ExitManager 전달용)
            self._strategy_exit_params = {
                "momentum_breakout": {
                    "stop_loss_pct": momentum_cfg.get("stop_loss_pct", 2.5),
                    "trailing_stop_pct": momentum_cfg.get("trailing_stop_pct", 1.5),
                },
                "theme_chasing": {
                    "stop_loss_pct": theme_strategy_cfg.get("stop_loss_pct", 2.0),
                    "trailing_stop_pct": theme_strategy_cfg.get("trailing_stop_pct", 1.0),
                },
                "gap_and_go": {
                    "stop_loss_pct": gap_cfg.get("stop_loss_pct", 2.0),
                    "trailing_stop_pct": gap_cfg.get("trailing_stop_pct", 1.5),
                },
                "mean_reversion": {
                    "stop_loss_pct": mr_cfg.get("stop_loss_pct", 3.0),
                    "trailing_stop_pct": mr_cfg.get("trailing_stop_pct", 2.0),
                },
            }

            # 리스크 매니저 초기화
            self.risk_manager = RiskMgr(
                self.config.trading.risk,
                self.config.trading.initial_capital
            )

            # 분할 익절/청산 관리자 초기화
            exit_cfg = self.config.get("exit_manager") or {}
            self.exit_manager = ExitManager(ExitConfig(
                enable_partial_exit=exit_cfg.get("enable_partial_exit", True),
                first_exit_pct=exit_cfg.get("first_exit_pct", 3.0),
                first_exit_ratio=exit_cfg.get("first_exit_ratio", 0.3),
                second_exit_pct=exit_cfg.get("second_exit_pct", 5.0),
                second_exit_ratio=exit_cfg.get("second_exit_ratio", 0.5),
                stop_loss_pct=exit_cfg.get("stop_loss_pct", 2.5),
                trailing_stop_pct=exit_cfg.get("trailing_stop_pct", 1.5),
                trailing_activate_pct=exit_cfg.get("trailing_activate_pct", 3.0),
                include_fees=exit_cfg.get("include_fees", True),
            ))
            logger.info("분할 익절 관리자 초기화 완료")

            # 기존 포지션을 ExitManager에 등록 (초기화 순서: 포지션 로드 → ExitManager 생성 후 보완)
            if self.engine.portfolio.positions:
                for symbol, position in self.engine.portfolio.positions.items():
                    self.exit_manager.register_position(position)
                logger.info(
                    f"기존 포지션 {len(self.engine.portfolio.positions)}개 ExitManager 등록 완료"
                )

            # 자가 진화 엔진 초기화
            evolution_cfg = self.config.get("evolution") or {}
            if evolution_cfg.get("enabled", True):
                self.trade_journal = get_trade_journal()
                self.strategy_evolver = get_strategy_evolver()

                # 전략 등록 (파라미터 자동 조정용)
                for name, strategy in self.strategy_manager.strategies.items():
                    self.strategy_evolver.register_strategy(name, strategy)

                # 컴포넌트 등록 (ExitManager, RiskConfig)
                if self.exit_manager:
                    self.strategy_evolver.register_component(
                        "exit_manager", self.exit_manager, "config"
                    )
                if self.config.trading.risk:
                    self.strategy_evolver.register_component(
                        "risk_config", self.config.trading.risk
                    )

                logger.info("자가 진화 엔진 초기화 완료")

            # 종목 스크리너 초기화
            self.screener = get_screener()
            screener_cfg = self.config.get("screener") or {}
            self.screener.min_volume_ratio = screener_cfg.get("min_volume_ratio", 2.0)
            self.screener.min_change_pct = screener_cfg.get("min_change_pct", 1.0)
            self.screener.max_change_pct = screener_cfg.get("max_change_pct", 15.0)
            self._screening_interval = screener_cfg.get("scan_interval_minutes", 10) * 60
            # stock_master → screener 연동 (종목 DB 활용)
            if self.stock_master:
                self.screener.set_stock_master(self.stock_master)
            logger.info("종목 스크리너 초기화 완료")

            # 엔진에 컴포넌트 연결
            self.engine.strategy_manager = self.strategy_manager
            self.engine.broker = self.broker

            # 엔진 이벤트 핸들링용 RiskManager (SIGNAL→ORDER, FILL 추적)
            # RiskMgr(대시보드/daily_stats)를 risk_validator로 주입하여 상태 공유
            engine_risk_manager = RiskManager(
                self.engine, self.config.trading.risk,
                risk_validator=self.risk_manager,
            )
            self.engine.risk_manager = engine_risk_manager
            logger.info("엔진 리스크 매니저 (SIGNAL 핸들러 + 리스크 검증 위임) 등록 완료")

            # WebSocket 피드 초기화 (실시간 모드)
            if not self.dry_run:
                self.ws_feed = KISWebSocketFeed(KISWebSocketConfig.from_env())
                self.ws_feed.on_market_data(self._on_market_data)
                logger.info("WebSocket 피드 초기화 완료")

            # 이벤트 핸들러 등록
            self._register_event_handlers()

            # 감시 종목 로드
            await self._load_watch_symbols()

            # 과거 일봉 데이터 로드 (전략 지표 계산용)
            await self._preload_price_history()

            # 거래 저널의 종목명 보강 (캐시에 없는 종목 API 조회)
            await self._fill_name_cache_from_journal()

            logger.info("초기화 완료")
            return True

        except Exception as e:
            logger.exception(f"초기화 실패: {e}")
            # 에러 알림 발송 (동기로 실행)
            try:
                import traceback
                asyncio.create_task(self._send_error_alert(
                    "CRITICAL",
                    "봇 초기화 실패",
                    traceback.format_exc()
                ))
            except Exception:
                pass
            return False

    async def _load_existing_positions(self):
        """기존 보유 종목 로드 (KIS API에서)"""
        if not self.broker:
            return

        try:
            positions = await self.broker.get_positions()

            if positions:
                logger.info(f"기존 보유 종목 {len(positions)}개 로드")

                for symbol, position in positions.items():
                    # 엔진 포트폴리오에 추가
                    self.engine.portfolio.positions[symbol] = position

                    # 종목명 캐시에 저장
                    pos_name = getattr(position, 'name', '')
                    if pos_name and pos_name != symbol:
                        self.stock_name_cache[symbol] = pos_name

                    # 현재가 조회하여 업데이트
                    quote = await self.broker.get_quote(symbol)
                    if quote and quote.get('price', 0) > 0:
                        position.current_price = Decimal(str(quote['price']))
                        position.highest_price = position.current_price  # 트레일링용
                    # 현재가 API에서 종목명 보강
                    if not pos_name or pos_name == symbol:
                        q_name = quote.get('name', '') if quote else ''
                        if q_name:
                            position.name = q_name
                            self.stock_name_cache[symbol] = q_name

                    # 수익률 계산
                    if position.avg_price > 0:
                        pnl_pct = float((position.current_price - position.avg_price) / position.avg_price * 100)
                        logger.info(
                            f"  - {symbol}: {position.quantity}주 @ {position.avg_price:,.0f}원 "
                            f"(현재가: {position.current_price:,.0f}원, 수익률: {pnl_pct:+.2f}%)"
                        )

                    # 분할 익절 관리자에 등록
                    if self.exit_manager:
                        self.exit_manager.register_position(position)

                    # 감시 종목에 추가
                    if symbol not in self._watch_symbols:
                        self._watch_symbols.append(symbol)

                # WebSocket에 보유 종목 우선순위 설정
                if self.ws_feed and positions:
                    self.ws_feed.set_priority_symbols(list(positions.keys()))

        except Exception as e:
            logger.warning(f"기존 포지션 로드 오류: {e}")

    async def _load_watch_symbols(self):
        """감시 종목 로드 (스크리너 활용)"""
        # 설정에서 기본 감시 종목 로드
        watch_cfg = self.config.get("watch_symbols") or []

        # 테마 탐지기에서 핫 테마 종목 추가
        if self.theme_detector:
            theme_stocks = self.theme_detector.get_all_theme_stocks()
            for stocks in theme_stocks.values():
                watch_cfg.extend(stocks)

        # 스크리너를 통한 동적 종목 발굴
        if self.screener:
            try:
                screened = await self.screener.screen_all()
                for stock in screened[:50]:  # 상위 50개
                    watch_cfg.append(stock.symbol)
                    if stock.score >= 80:
                        logger.info(f"  [스크리너] {stock.symbol} {stock.name}: 점수={stock.score:.0f}, {stock.reasons}")

                # 스크리닝 결과 로그 기록 (복기용)
                trading_logger.log_screening(
                    source="initial",
                    total_stocks=len(screened),
                    top_stocks=[{
                        "symbol": s.symbol,
                        "name": s.name,
                        "score": s.score,
                        "price": s.price,
                        "change_pct": s.change_pct,
                        "reasons": s.reasons,
                    } for s in screened[:20]]
                )
            except Exception as e:
                logger.warning(f"스크리너 초기 실행 실패: {e}")

        # 중복 제거 (기존 보유 종목 보존!)
        existing = self._watch_symbols or []
        self._watch_symbols = list(set(existing + watch_cfg))
        logger.info(f"감시 종목 {len(self._watch_symbols)}개 로드 (보유종목 {len(existing)}개 포함)")

    async def _preload_price_history(self):
        """전략용 과거 일봉 데이터 사전 로드"""
        if not self.broker or not self._watch_symbols:
            return

        logger.info(f"[히스토리] 과거 일봉 데이터 로드 시작 ({len(self._watch_symbols)}개 종목)...")
        loaded = 0
        failed = 0

        for symbol in self._watch_symbols:
            try:
                daily_data = await self.broker.get_daily_prices(symbol, days=60)
                if not daily_data:
                    failed += 1
                    continue

                # Price 객체로 변환
                from src.core.types import Price
                prices = []
                for d in daily_data:
                    try:
                        prices.append(Price(
                            symbol=symbol.zfill(6),
                            timestamp=datetime.strptime(d["date"], "%Y%m%d"),
                            open=Decimal(str(d["open"])),
                            high=Decimal(str(d["high"])),
                            low=Decimal(str(d["low"])),
                            close=Decimal(str(d["close"])),
                            volume=d["volume"],
                            value=Decimal(str(d.get("value", 0))),
                        ))
                    except (ValueError, KeyError):
                        continue

                if prices:
                    # 모든 등록된 전략에 주입
                    sm = self.strategy_manager or getattr(self.engine, 'strategy_manager', None)
                    if sm:
                        for strategy in sm.strategies.values():
                            strategy.preload_history(symbol, prices)
                    loaded += 1

                # API Rate limit 방지
                await asyncio.sleep(0.15)

            except Exception as e:
                logger.warning(f"일봉 로드 실패 ({symbol}): {e}")
                failed += 1

        logger.info(f"[히스토리] 일봉 데이터 로드 완료: 성공 {loaded}개, 실패 {failed}개")

    async def _fill_name_cache_from_journal(self):
        """거래 저널에서 종목명이 없는 종목을 API로 조회하여 캐시 채우기"""
        if not self.trade_journal or not self.broker:
            return

        try:
            trades = self.trade_journal.get_today_trades()
            missing = set()
            for t in trades:
                symbol = t.symbol
                name = t.name
                if not name or name == symbol:
                    if symbol not in self.stock_name_cache:
                        missing.add(symbol)

            if not missing:
                return

            logger.info(f"[종목명] 종목명 캐시 보강: {len(missing)}개 조회")
            for symbol in missing:
                try:
                    name_found = ''
                    # 1차: KIS get_quote API
                    quote = await self.broker.get_quote(symbol)
                    name_found = quote.get('name', '') if quote else ''

                    # 2차: 네이버 금융에서 종목명 조회
                    if not name_found or name_found == symbol:
                        try:
                            import aiohttp
                            url = f"https://finance.naver.com/item/main.naver?code={symbol.zfill(6)}"
                            async with aiohttp.ClientSession() as session:
                                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                    if resp.status == 200:
                                        html = await resp.text()
                                        # <title>네이버 금융 : 종목명</title>
                                        import re
                                        match = re.search(r'<title>(.+?)\s*:\s*N(?:pay|aver)', html)
                                        if match:
                                            name_found = match.group(1).strip()
                        except Exception:
                            pass

                    logger.info(f"[종목명] {symbol} -> name='{name_found}'")
                    if name_found and name_found != symbol:
                        self.stock_name_cache[symbol] = name_found
                    else:
                        logger.warning(f"[종목명] {symbol}: 종목명 조회 실패")
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.warning(f"[종목명] {symbol} 조회 실패: {e}")

            logger.info(f"[종목명] 캐시 보강 완료: {len(self.stock_name_cache)}개 종목")
        except Exception as e:
            logger.warning(f"[종목명] 캐시 보강 오류: {e}")

    async def _on_market_data(self, event: MarketDataEvent):
        """실시간 시세 데이터 처리"""
        try:
            # 엔진 이벤트 큐에 전달
            await self.engine.emit(event)

            # 분할 익절 체크 (보유 종목에 대해)
            if self.exit_manager and event.symbol in self.engine.portfolio.positions:
                await self._check_exit_signal(event.symbol, event.close)

        except (ValueError, KeyError, TypeError) as e:
            logger.debug(f"시세 데이터 형식 오류 ({event.symbol}): {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"시세 데이터 처리 오류 ({event.symbol}): {e}")

    async def _check_exit_signal(self, symbol: str, current_price: Decimal):
        """분할 익절/손절 신호 확인"""
        if not self.exit_manager or not self.broker:
            return

        # 자동 재개 체크 (청산 실패 후 일시정지 → 타이머 만료 시 재개)
        if self._pause_resume_at and datetime.now() >= self._pause_resume_at:
            self._pause_resume_at = None
            self.engine.resume()
            logger.info("[엔진] 자동 재개: 일시정지 타이머 만료")

        try:
            # stale pending 클린업 (3분 이상 체결 미확인 시 해제)
            if self._exit_pending_timestamps:
                stale_cutoff = datetime.now() - timedelta(minutes=3)
                stale = [s for s, t in self._exit_pending_timestamps.items() if t < stale_cutoff]
                for s in stale:
                    self._exit_pending_symbols.discard(s)
                    self._exit_pending_timestamps.pop(s, None)
                    logger.warning(f"[청산 pending] {s} 타임아웃 해제 (3분 초과)")

            # 이미 ExitManager 매도 주문이 진행 중이면 중복 방지
            if symbol in self._exit_pending_symbols:
                return

            # 동시호가 시간대 체크 (15:20~15:30)
            now = datetime.now()
            time_val = now.hour * 100 + now.minute
            is_auction = 1520 <= time_val < 1530

            # 청산 신호 확인
            exit_signal = self.exit_manager.update_price(symbol, current_price)

            if exit_signal:
                action, quantity, reason = exit_signal
                logger.info(f"[청산 신호] {symbol}: {reason} ({quantity}주)")
                trading_logger.log_position_update(
                    symbol=symbol,
                    action=f"EXIT_SIGNAL:{action}",
                    quantity=quantity,
                    avg_price=float(current_price),
                )

                # 주문 생성 및 제출
                from src.core.types import Order, OrderSide, OrderType

                order = Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.LIMIT if is_auction else OrderType.MARKET,
                    quantity=quantity,
                    price=current_price if is_auction else None,
                )

                # ExitManager 전용 pending 선등록 (중복 매도 방지, submit await 중 race condition 차단)
                self._exit_pending_symbols.add(symbol)
                self._exit_pending_timestamps[symbol] = datetime.now()
                # 엔진 RiskManager에도 pending 등록 (전략 SELL 신호 중복 차단용)
                if self.engine.risk_manager:
                    self.engine.risk_manager._pending_orders.add(symbol)
                    self.engine.risk_manager._pending_timestamps[symbol] = datetime.now()

                # 브로커에 주문 제출 (실패 시 최대 2회 재시도)
                success = False
                result = None
                for attempt in range(3):
                    success, result = await self.broker.submit_order(order)
                    if success:
                        break
                    if attempt < 2:
                        logger.warning(f"[청산 재시도] {symbol} attempt={attempt+1}, 사유: {result}")
                        await asyncio.sleep(0.5)

                if success:
                    order_type_str = "LIMIT" if is_auction else "MARKET"
                    logger.info(f"[청산 주문 성공] {symbol} {quantity}주 ({order_type_str}) -> 주문번호: {result}")

                    # 손절인 경우 RiskManager에 기록 (재진입 방지)
                    if "손절" in reason and self.engine.risk_manager:
                        self.engine.risk_manager._stop_loss_today[symbol] = datetime.now()
                        logger.info(f"[재진입금지] {symbol} 손절 기록 (60분간 재진입 차단)")

                    trading_logger.log_order(
                        symbol=symbol,
                        side="SELL",
                        quantity=quantity,
                        price=float(current_price),
                        order_type=order_type_str,
                        status=f"submitted ({reason})"
                    )
                else:
                    # 주문 실패 시 양쪽 pending 해제 (다음 시세에서 재시도 가능)
                    self._exit_pending_symbols.discard(symbol)
                    self._exit_pending_timestamps.pop(symbol, None)
                    if self.engine.risk_manager:
                        self.engine.risk_manager.clear_pending(symbol)
                    logger.error(f"[청산 주문 실패] {symbol} - {result} (3회 시도 후)")
                    # 청산 실패는 리스크 급증 → 신규 매수 차단 (5분 후 자동 재개)
                    self.engine.pause()
                    self._pause_resume_at = datetime.now() + timedelta(minutes=5)
                    logger.critical(
                        f"[엔진 일시정지] 청산 실패로 신규 매수 차단: {symbol} "
                        f"(5분 후 자동 재개 또는 수동 재개)"
                    )
                    await self._send_error_alert(
                        "CRITICAL",
                        f"청산 주문 실패 → 엔진 일시정지(5분): {symbol} {quantity}주",
                        f"사유: {result}\n이유: {reason}\n"
                        f"5분 후 자동 재개 (수동 재개도 가능)"
                    )

        except Exception as e:
            logger.error(f"청산 신호 처리 오류: {e}")

    def _register_event_handlers(self):
        """이벤트 핸들러 등록"""
        # 세션 변경 핸들러
        self.engine.register_handler(EventType.SESSION, self._on_session)

        # 리스크 경고 핸들러
        self.engine.register_handler(EventType.RISK_ALERT, self._on_risk_alert)

        # 체결 핸들러
        self.engine.register_handler(EventType.FILL, self._on_fill)

        # 테마 이벤트 핸들러
        self.engine.register_handler(EventType.THEME, self._on_theme)

        # 주문 실행 핸들러 (신호 → 주문 제출)
        self.engine.register_handler(EventType.ORDER, self._on_order)

    async def _on_order(self, event):
        """주문 이벤트 처리 - 실제 브로커에 주문 제출"""
        if not self.broker:
            logger.warning("브로커 연결 없음 - 주문 무시 (Dry Run 모드)")
            return None

        order = event.order
        if not order:
            return None

        # BUY 주문 시 종목→전략 매핑 기록 (ExitManager 전략별 파라미터용)
        if order.side.value.upper() == "BUY" and order.strategy:
            self._symbol_strategy[order.symbol] = order.strategy

        try:
            logger.info(f"[주문 제출] {order.side.value} {order.symbol} {order.quantity}주 @ {order.price}")

            # 브로커에 주문 제출
            success, result = await self.broker.submit_order(order)

            if success:
                logger.info(f"[주문 성공] {order.symbol} -> 주문번호: {result}")
                trading_logger.log_order(
                    symbol=order.symbol,
                    side=order.side.value,
                    quantity=order.quantity,
                    price=float(order.price) if order.price else 0,
                    order_type=order.order_type.value,
                    status="submitted"
                )
            else:
                logger.error(f"[주문 실패] {order.symbol} - {result}")
                trading_logger.log_order(
                    symbol=order.symbol,
                    side=order.side.value,
                    quantity=order.quantity,
                    price=float(order.price) if order.price else 0,
                    order_type=order.order_type.value,
                    status=f"failed: {result}"
                )

                # 주문 실패 시 해당 종목 쿨다운 등록 (반복 주문 방지)
                if self.engine.risk_manager:
                    self.engine.risk_manager.block_symbol(order.symbol)
                    # pending 해제 + 예약 현금 환원
                    order_amount = (order.price * order.quantity) if order.price and order.quantity else Decimal("0")
                    self.engine.risk_manager.clear_pending(order.symbol, order_amount)
                    logger.info(f"[주문 쿨다운] {order.symbol} - 5분간 주문 차단 (pending 해제)")

                # 주문 실패 알림 (종목당 1회만)
                if not hasattr(self, '_order_fail_alerted'):
                    self._order_fail_alerted = set()
                if order.symbol not in self._order_fail_alerted:
                    self._order_fail_alerted.add(order.symbol)
                    await self._send_error_alert(
                        "WARNING",
                        f"주문 실패: {order.side.value} {order.symbol} {order.quantity}주",
                        f"사유: {result}"
                    )

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"주문 제출 네트워크 오류: {e}")
            # 네트워크 오류 시 pending 해제
            if self.engine.risk_manager and order:
                order_amount = (order.price * order.quantity) if order.price and order.quantity else Decimal("0")
                self.engine.risk_manager.clear_pending(order.symbol, order_amount)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"주문 제출 오류: {e}")

        return None

    async def _on_theme(self, event: ThemeEvent):
        """테마 이벤트 처리"""
        logger.info(f"[테마] {event.name} (점수: {event.score:.0f}) - 관련종목: {event.symbols[:5]}")

        # 테마 로그 기록 (복기용)
        trading_logger.log_theme(
            theme_name=event.name,
            score=event.score,
            keywords=event.keywords,
            related_stocks=event.symbols,
        )

        # 테마 추종 전략에 전달
        theme_strategy = self.strategy_manager.strategies.get("theme_chasing")
        if theme_strategy and hasattr(theme_strategy, "on_theme"):
            await theme_strategy.on_theme(event)

    async def _on_session(self, event: SessionEvent):
        """세션 변경 처리"""
        prev = getattr(event, 'prev_session', None)
        prev_val = prev.value if prev else ""
        new_val = event.session.value

        # 포지션/현금/손익 상세 정보
        portfolio = self.engine.portfolio
        pos_count = len(portfolio.positions)
        cash = float(portfolio.cash)
        daily_pnl = float(portfolio.daily_pnl)

        details = (
            f"포지션={pos_count}종목 현금={cash:,.0f}원 "
            f"일일손익={daily_pnl:+,.0f}원"
        )
        logger.info(f"세션 변경: {prev_val} → {new_val} | {details}")

        trading_logger.log_session_change(
            new_session=new_val,
            prev_session=prev_val,
            details=details,
        )

        # 장 마감 시 일일 요약
        if new_val == "closed" and prev_val:
            await self._daily_summary()

    async def _on_risk_alert(self, event):
        """리스크 경고 처리"""
        trading_logger.log_risk_alert(
            alert_type=event.alert_type,
            message=event.message,
            action=event.action
        )

        # 리스크 알림 발송
        await self._send_error_alert(
            "WARNING",
            f"리스크 경고: {event.alert_type}",
            f"메시지: {event.message}\n조치: {event.action}"
        )

        if event.action == "block":
            logger.warning("리스크 한도 도달 - 거래 중단")
            self.engine.pause()

    async def _on_fill(self, event):
        """체결 처리"""
        fill = event.fill
        if fill:
            trading_logger.log_fill(
                symbol=fill.symbol,
                side=fill.side.value,
                quantity=fill.quantity,
                price=float(fill.price),
                commission=float(fill.commission)
            )

            # 포트폴리오 업데이트 (동기화 lock으로 보호)
            async with self._portfolio_lock:
                self.engine.update_position(fill)

            # 리스크 통계 업데이트 (대시보드용: can_trade, daily_loss 등)
            if self.risk_manager:
                self.risk_manager.on_fill(event, self.engine.portfolio)

            # 매수 체결 시 종목명 보강 및 캐시
            if fill.side.value.upper() == "BUY":
                position = self.engine.portfolio.positions.get(fill.symbol)
                if position:
                    pos_name = getattr(position, 'name', '')
                    if not pos_name or pos_name == fill.symbol:
                        # 캐시에서 먼저 확인
                        cached = self.stock_name_cache.get(fill.symbol)
                        if cached:
                            position.name = cached
                        elif self.broker:
                            try:
                                quote = await self.broker.get_quote(fill.symbol)
                                real_name = quote.get('name', '') if quote else ''
                                if real_name and real_name != fill.symbol:
                                    position.name = real_name
                                    self.stock_name_cache[fill.symbol] = real_name
                            except Exception:
                                pass
                    elif pos_name and pos_name != fill.symbol:
                        self.stock_name_cache[fill.symbol] = pos_name

            # ExitManager 매도 pending 해제 (체결 확인) + 엔진 RiskManager pending 해제
            if fill.side.value.upper() == "SELL":
                self._exit_pending_symbols.discard(fill.symbol)
                self._exit_pending_timestamps.pop(fill.symbol, None)
                if self.engine.risk_manager:
                    self.engine.risk_manager.clear_pending(fill.symbol)

                # EXIT 레코드 기록 (진입가/손익/사유)
                # 포지션은 update_position에서 이미 갱신되었으므로 저널에서 조회
                try:
                    if self.trade_journal:
                        open_trades = self.trade_journal.get_open_trades()
                        matching = [t for t in open_trades if t.symbol == fill.symbol]
                        if matching:
                            trade = matching[0]
                            entry_price = trade.entry_price
                        else:
                            # 이미 청산된 직전 거래에서 진입가 추출
                            closed = self.trade_journal.get_closed_trades(days=1)
                            sym_trades = [t for t in closed if t.symbol == fill.symbol]
                            entry_price = sym_trades[-1].entry_price if sym_trades else float(fill.price)
                    else:
                        entry_price = float(fill.price)

                    exit_price = float(fill.price)
                    pnl = (exit_price - entry_price) * fill.quantity
                    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
                    reason = getattr(fill, 'reason', '') or "매도체결"

                    trading_logger.log_exit(
                        symbol=fill.symbol,
                        quantity=fill.quantity,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        reason=reason,
                    )

                    # TradeJournal 청산 기록 (진화 기능용)
                    if self.trade_journal:
                        try:
                            # 청산 타입 결정
                            exit_type = "unknown"
                            if "손절" in reason:
                                exit_type = "stop_loss"
                            elif "익절" in reason or "트레일링" in reason:
                                exit_type = "take_profit"
                            elif "시간" in reason or "종료" in reason:
                                exit_type = "time_exit"

                            self.trade_journal.record_exit(
                                symbol=fill.symbol,
                                exit_time=fill.timestamp,
                                exit_price=float(fill.price),
                                quantity=fill.quantity,
                                exit_type=exit_type,
                                pnl=float(pnl)
                            )
                            logger.debug(f"[TradeJournal] 청산 기록: {fill.symbol} {exit_type} {pnl:+,.0f}원")
                        except Exception as je:
                            logger.warning(f"[TradeJournal] 청산 기록 실패: {je}")
                except Exception as e:
                    logger.warning(f"EXIT 로그 기록 실패: {e}")

            # 분할 익절 관리자 업데이트
            if self.exit_manager:
                if fill.side.value.upper() == "SELL":
                    # 매도 체결 시 상태 업데이트
                    self.exit_manager.on_fill(fill.symbol, fill.quantity, fill.price)
                elif fill.side.value.upper() == "BUY":
                    # 매수 체결 시 새 포지션 등록 (전략별 청산 파라미터 전달)
                    position = self.engine.portfolio.positions.get(fill.symbol)
                    if position:
                        strategy_name = self._symbol_strategy.get(fill.symbol, "")
                        exit_params = self._strategy_exit_params.get(strategy_name, {})
                        self.exit_manager.register_position(
                            position,
                            stop_loss_pct=exit_params.get("stop_loss_pct"),
                            trailing_stop_pct=exit_params.get("trailing_stop_pct"),
                        )

                        # TradeJournal 진입 기록 (진화 기능용)
                        if self.trade_journal:
                            try:
                                self.trade_journal.record_entry(
                                    symbol=fill.symbol,
                                    entry_time=fill.timestamp,
                                    entry_price=float(fill.price),
                                    quantity=fill.quantity,
                                    strategy=strategy_name or "unknown",
                                    reason=getattr(fill, 'reason', '') or "매수체결",
                                    signal_score=0  # TODO: Order에 score 필드 추가 필요
                                )
                                logger.debug(f"[TradeJournal] 진입 기록: {fill.symbol} {strategy_name}")
                            except Exception as e:
                                logger.warning(f"[TradeJournal] 진입 기록 실패: {e}")

            # 거래 저널 기록 (자가 진화용)
            if self.trade_journal:
                await self._record_trade_to_journal(fill)

    async def _record_trade_to_journal(self, fill):
        """거래 저널에 체결 기록 (자가 진화용)"""
        try:
            from src.core.evolution import TradeRecord

            trade_id = f"{fill.symbol}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

            if fill.side.value.upper() == "BUY":
                # 매수 진입 기록
                indicators = {}
                if hasattr(self.strategy_manager, '_indicators'):
                    indicators = self.strategy_manager._indicators.get(fill.symbol, {})

                # 종목명: 캐시 → 포지션 → 브로커 API 순서로 조회
                stock_name = self.stock_name_cache.get(fill.symbol, '')
                if not stock_name:
                    pos = self.engine.portfolio.positions.get(fill.symbol)
                    stock_name = getattr(pos, 'name', '') if pos else ''
                if not stock_name or stock_name == fill.symbol:
                    if self.broker:
                        try:
                            quote = await self.broker.get_quote(fill.symbol)
                            stock_name = quote.get('name', '') or fill.symbol
                        except Exception:
                            stock_name = fill.symbol
                    else:
                        stock_name = fill.symbol
                # 캐시에 저장
                if stock_name and stock_name != fill.symbol:
                    self.stock_name_cache[fill.symbol] = stock_name

                self.trade_journal.record_entry(
                    trade_id=trade_id,
                    symbol=fill.symbol,
                    name=stock_name,
                    entry_price=float(fill.price),
                    entry_quantity=fill.quantity,
                    entry_reason=getattr(fill, 'reason', ''),
                    entry_strategy=getattr(fill, 'strategy', ''),
                    signal_score=getattr(fill, 'signal_score', 0),
                    indicators=indicators,
                )
            else:
                # 매도 청산 기록 - 가장 최근 미청산 거래 찾기
                open_trades = self.trade_journal.get_open_trades()
                matching = [t for t in open_trades if t.symbol == fill.symbol]

                if matching:
                    trade = matching[0]

                    indicators = {}
                    if hasattr(self.strategy_manager, '_indicators'):
                        indicators = self.strategy_manager._indicators.get(fill.symbol, {})

                    self.trade_journal.record_exit(
                        trade_id=trade.id,
                        exit_price=float(fill.price),
                        exit_quantity=fill.quantity,
                        exit_reason=getattr(fill, 'reason', ''),
                        exit_type=getattr(fill, 'exit_type', 'unknown'),
                        indicators=indicators,
                    )

        except Exception as e:
            logger.warning(f"거래 저널 기록 실패: {e}")

    async def _daily_summary(self):
        """일일 요약"""
        portfolio = self.engine.portfolio
        stats = self.engine.stats

        pnl_pct = float(portfolio.daily_pnl / portfolio.initial_capital * 100) if portfolio.initial_capital > 0 else 0

        # trade_journal 기반 wins/losses 실제 계산
        wins = 0
        losses = 0
        if self.trade_journal:
            try:
                today_trades = self.trade_journal.get_closed_trades(days=1)
                for t in today_trades:
                    if t.pnl > 0:
                        wins += 1
                    elif t.pnl < 0:
                        losses += 1
            except Exception as e:
                logger.warning(f"일일 요약 승패 계산 실패: {e}")

        # 현재 보유 포지션 정보
        positions_info = []
        for symbol, pos in portfolio.positions.items():
            pos_pnl_pct = 0
            if pos.avg_price > 0 and pos.current_price > 0:
                pos_pnl_pct = float((pos.current_price - pos.avg_price) / pos.avg_price * 100)
            positions_info.append({
                "symbol": symbol,
                "name": getattr(pos, 'name', symbol),
                "quantity": pos.quantity,
                "avg_price": float(pos.avg_price),
                "current_price": float(pos.current_price) if pos.current_price else 0,
                "pnl_pct": pos_pnl_pct,
            })

        trading_logger.log_daily_summary(
            total_trades=portfolio.daily_trades,
            wins=wins,
            losses=losses,
            total_pnl=float(portfolio.daily_pnl),
            pnl_pct=pnl_pct,
            positions=positions_info,
        )

    async def run(self):
        """봇 실행"""
        if not await self.initialize():
            return

        self.running = True
        logger.info("=== 트레이딩 봇 시작 ===")

        try:
            # 태스크 생성
            tasks = []

            # 1. 메인 엔진 실행
            tasks.append(asyncio.create_task(self.engine.run(), name="engine"))

            # 2. WebSocket 피드 실행 (실시간 모드)
            if self.ws_feed:
                # 보유 종목을 우선순위로 설정 (항상 구독)
                if self.engine.portfolio.positions:
                    self.ws_feed.set_priority_symbols(list(self.engine.portfolio.positions.keys()))
                    logger.info(f"[WS] 보유 종목 {len(self.engine.portfolio.positions)}개 우선 구독 설정")
                tasks.append(asyncio.create_task(self._run_ws_feed(), name="ws_feed"))

            # 3. 테마 탐지 루프 실행
            if self.theme_detector:
                tasks.append(asyncio.create_task(self._run_theme_detection(), name="theme_detector"))

            # 4. 체결 확인 루프 실행 (실시간 모드)
            if self.broker:
                tasks.append(asyncio.create_task(self._run_fill_check(), name="fill_checker"))

            # 4-1. 포트폴리오 동기화 루프 (5분마다 KIS API와 동기화)
            if self.broker:
                tasks.append(asyncio.create_task(self._run_portfolio_sync(), name="portfolio_sync"))

            # 5. 종목 스크리닝 루프 실행
            if self.screener:
                tasks.append(asyncio.create_task(self._run_screening(), name="screener"))

            # 6. 일일 레포트 스케줄러 실행
            self.report_generator = get_report_generator()
            self.report_generator._kis_market_data = self.kis_market_data
            self.report_generator._us_market_data = self.us_market_data
            if self.theme_detector:
                self.report_generator.theme_detector = self.theme_detector
            tasks.append(asyncio.create_task(self._run_daily_report_scheduler(), name="report_scheduler"))

            # 7. 자가 진화 스케줄러 실행
            if self.strategy_evolver:
                tasks.append(asyncio.create_task(self._run_evolution_scheduler(), name="evolution_scheduler"))

            # 8. 코드 자동 진화 스케줄러 실행
            code_evo_cfg = self.config.get("code_evolution") or {}
            if code_evo_cfg.get("enabled", False):
                tasks.append(asyncio.create_task(
                    self._run_code_evolution_scheduler(), name="code_evolution"
                ))

            # 9. 로그/캐시 정리 스케줄러
            tasks.append(asyncio.create_task(self._run_log_cleanup(), name="log_cleanup"))

            # 10-1. 종목 마스터 갱신 스케줄러
            if self.stock_master:
                tasks.append(asyncio.create_task(
                    self._run_stock_master_refresh(), name="stock_master_refresh"
                ))

            # 10. 대시보드 서버 실행
            dashboard_cfg = self.config.get("dashboard") or {}
            if dashboard_cfg.get("enabled", True):
                self.dashboard = DashboardServer(
                    self,
                    host=dashboard_cfg.get("host", "0.0.0.0"),
                    port=dashboard_cfg.get("port", 8080),
                )
                tasks.append(asyncio.create_task(self.dashboard.run(), name="dashboard"))

            # 모든 태스크 실행
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 핵심 태스크 예외 검사 (좀비 상태 방지)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    task_name = tasks[i].get_name() if hasattr(tasks[i], 'get_name') else f"task-{i}"
                    logger.error(f"[태스크 종료] {task_name} 예외 발생: {result}")
                    await self._send_error_alert(
                        "CRITICAL",
                        f"핵심 태스크 비정상 종료: {task_name}",
                        str(result)
                    )

        except Exception as e:
            logger.exception(f"실행 오류: {e}")
            # 에러 알림 발송
            import traceback
            await self._send_error_alert(
                "CRITICAL",
                "봇 실행 중 오류 발생 - 봇 종료",
                traceback.format_exc()
            )
        finally:
            await self.shutdown()

    async def _run_ws_feed(self):
        """WebSocket 피드 실행"""
        try:
            # 연결
            if await self.ws_feed.connect():
                # NXT 거래 가능 종목 로드 (프리장/넥스트장용)
                await self._load_nxt_symbols()

                # 현재 세션 설정
                current_session = self._get_current_session()
                self.ws_feed.set_session(current_session)

                # 종목별 점수 설정 (스크리너에서)
                scores = {}
                if self.screener and hasattr(self.screener, '_last_screened'):
                    for stock in getattr(self.screener, '_last_screened', []):
                        scores[stock.symbol] = stock.score

                # 감시 종목 구독 (롤링 방식으로 전체 구독)
                if self._watch_symbols:
                    await self.ws_feed.subscribe(self._watch_symbols, scores)
                    stats = self.ws_feed.get_subscription_stats()
                    logger.info(
                        f"WebSocket 구독: 감시={stats['total_watch']}개, "
                        f"세션종목={stats['session_tradable']}개, "
                        f"구독={stats['subscribed_count']}개, "
                        f"롤링={'ON' if stats['is_rolling'] else 'OFF'}"
                    )

                # 세션 체크 태스크 시작
                asyncio.create_task(self._session_check_loop())

                # 메시지 수신 루프
                await self.ws_feed.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"WebSocket 피드 오류: {e}")
            import traceback
            await self._send_error_alert(
                "ERROR",
                "WebSocket 피드 오류",
                traceback.format_exc()
            )

    async def _load_nxt_symbols(self):
        """NXT 거래 가능 종목 로드"""
        # NXT 거래 가능 종목 목록 (대형주, ETF 등 약 400종목)
        # 실제로는 KIS API에서 조회하거나 캐시된 목록 사용
        nxt_symbols = []

        try:
            # 브로커에서 NXT 종목 조회 시도
            if self.broker and hasattr(self.broker, 'get_nxt_symbols'):
                nxt_symbols = await self.broker.get_nxt_symbols()

            # 없으면 기본 대형주/ETF 목록 사용
            if not nxt_symbols:
                # 코스피200 + 주요 ETF (예시)
                nxt_symbols = [
                    # 대형주
                    "005930", "000660", "005380", "035420", "000270",
                    "005490", "035720", "051910", "006400", "028260",
                    "207940", "068270", "096770", "003670", "034730",
                    # 주요 ETF
                    "069500", "102110", "233740", "114800", "122630",
                    "252670", "091160", "091170", "229200", "305540",
                ]
                logger.info(f"NXT 기본 종목 목록 사용: {len(nxt_symbols)}개")

            if self.ws_feed:
                self.ws_feed.set_nxt_symbols(nxt_symbols)

        except Exception as e:
            logger.warning(f"NXT 종목 로드 실패: {e}")

    def _get_current_session(self) -> MarketSession:
        """현재 시간 기반 세션 판단"""
        now = datetime.now()
        hour, minute = now.hour, now.minute
        time_val = hour * 100 + minute

        # 세션 시간대
        # 프리장: 08:00 ~ 08:50
        # 정규장: 09:00 ~ 15:20 (장마감 동시호가 전까지)
        # 15:20~15:40: CLOSED (동시호가 + 휴장)
        # 넥스트장: 15:40 ~ 20:00

        if 800 <= time_val < 850:
            return MarketSession.PRE_MARKET
        elif 900 <= time_val < 1520:
            return MarketSession.REGULAR
        elif 1540 <= time_val < 2000:
            return MarketSession.NEXT
        else:
            return MarketSession.CLOSED

    async def _session_check_loop(self):
        """세션 변경 체크 루프 (1분마다)"""
        last_session = None
        last_nxt_update = None

        try:
            while self.running:
                now = datetime.now()
                current = self._get_current_session()

                # 세션 변경 감지
                if current != last_session:
                    last_session = current
                    logger.info(f"[세션] {current.value} 감지")

                    # WebSocket 세션 설정
                    if self.ws_feed:
                        self.ws_feed.set_session(current)

                    # 엔진에 세션 이벤트 발행
                    session_map = {
                        MarketSession.PRE_MARKET: TradingSession.PRE_MARKET,
                        MarketSession.REGULAR: TradingSession.REGULAR,
                        MarketSession.NEXT: TradingSession.NEXT,
                        MarketSession.CLOSED: TradingSession.CLOSED,
                    }

                    event = SessionEvent(
                        source="session_checker",
                        session=session_map.get(current, TradingSession.CLOSED),
                    )
                    await self.engine.emit(event)

                # 엔진/WS 상태 로깅 (5분마다)
                if now.minute % 5 == 0 and now.second < 60:
                    engine_stats = self.engine.stats
                    logger.debug(
                        f"[엔진 상태] 이벤트처리={engine_stats.events_processed}건, "
                        f"신호생성={engine_stats.signals_generated}건, "
                        f"오류={engine_stats.errors_count}건"
                    )
                    if self.ws_feed:
                        stats = self.ws_feed.get_stats()
                        logger.debug(
                            f"[WS 상태] 연결={stats['connected']}, "
                            f"구독={stats['subscribed_count']}개, "
                            f"수신={stats['message_count']}건, "
                            f"마지막={stats['last_message_time'] or 'N/A'}"
                        )

                # 매일 NXT 종목 갱신 (설정 시간)
                nxt_hour = (self.config.get("scheduler") or {}).get("nxt_refresh_hour", 6)
                if now.hour == nxt_hour and (last_nxt_update is None or last_nxt_update.date() != now.date()):
                    logger.info("[NXT] 매일 06:00 NXT 종목 갱신 시작")
                    await self._refresh_nxt_symbols()
                    last_nxt_update = now

                await asyncio.sleep(60)  # 1분마다 체크

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"세션 체크 오류: {e}")

    async def _refresh_nxt_symbols(self):
        """NXT 거래 가능 종목 갱신 (매일 06시 실행)"""
        try:
            if self.broker and hasattr(self.broker, 'get_nxt_symbols'):
                # 캐시 무효화 후 새로 로드
                self.broker._nxt_cache_updated = None
                nxt_symbols = await self.broker.get_nxt_symbols()

                # WebSocket에 전달
                if self.ws_feed and nxt_symbols:
                    self.ws_feed.set_nxt_symbols(nxt_symbols)
                    logger.info(f"[NXT] {len(nxt_symbols)}개 종목 갱신 완료")

        except Exception as e:
            logger.error(f"NXT 종목 갱신 오류: {e}")

    def stop(self):
        """봇 중지"""
        self.running = False
        self.engine.stop()

    async def shutdown(self):
        """종료 처리"""
        logger.info("=== 트레이딩 봇 종료 ===")

        self.running = False

        # 각 단계를 개별 try-except로 감싸서 하나 실패해도 나머지 진행
        try:
            await self._daily_summary()
        except Exception as e:
            logger.error(f"일일 요약 생성 실패: {e}")

        try:
            trading_logger.flush()
        except Exception as e:
            logger.error(f"로그 저장 실패: {e}")

        try:
            if self.dashboard:
                await self.dashboard.stop()
                logger.info("대시보드 서버 종료")
        except Exception as e:
            logger.error(f"대시보드 종료 실패: {e}")

        try:
            if self.ws_feed:
                await self.ws_feed.disconnect()
                logger.info("WebSocket 연결 해제")
        except Exception as e:
            logger.error(f"WebSocket 해제 실패: {e}")

        try:
            if self.screener:
                await self.screener.close()
                logger.info("종목 스크리너 종료")
        except Exception as e:
            logger.error(f"스크리너 종료 실패: {e}")

        try:
            if self.kis_market_data:
                await self.kis_market_data.close()
                logger.info("KIS 시장 데이터 클라이언트 종료")
        except Exception as e:
            logger.error(f"KIS 시장 데이터 종료 실패: {e}")

        try:
            if self.us_market_data:
                await self.us_market_data.close()
                logger.info("US 시장 데이터 클라이언트 종료")
        except Exception as e:
            logger.error(f"US 시장 데이터 종료 실패: {e}")

        try:
            if self.theme_detector and self.theme_detector.news_collector:
                await self.theme_detector.news_collector.close()
                logger.info("뉴스 수집기 세션 종료")
        except Exception as e:
            logger.error(f"뉴스 수집기 종료 실패: {e}")

        try:
            if self.stock_master:
                await self.stock_master.disconnect()
                logger.info("종목 마스터 종료")
        except Exception as e:
            logger.error(f"종목 마스터 종료 실패: {e}")

        try:
            if self.broker:
                await self.broker.disconnect()
        except Exception as e:
            logger.error(f"브로커 연결 해제 실패: {e}")

        logger.info("종료 완료")


def parse_args():
    """명령줄 인자 파싱"""
    parser = argparse.ArgumentParser(description="AI Trading Bot v2")
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="설정 파일 경로"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run 모드 (실제 거래 없음)"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="로그 레벨"
    )
    return parser.parse_args()


async def main():
    """메인 함수"""
    args = parse_args()

    # 로거 설정
    log_dir = project_root / "logs" / datetime.now().strftime("%Y%m%d")
    setup_logger(
        log_level=args.log_level,
        log_dir=str(log_dir),
        enable_console=True,
        enable_file=True,
    )

    # 거래 로거에 로그 디렉토리 설정 (JSON 저장용)
    trading_logger.set_log_dir(str(log_dir))

    # 설정 로드
    config = AppConfig.load(
        config_path=args.config,
        dotenv_path=str(project_root / ".env")
    )

    # 봇 실행
    bot = TradingBot(config, dry_run=args.dry_run)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
