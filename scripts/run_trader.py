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
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

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
from src.data.feeds.kis_websocket import KISWebSocketFeed, KISWebSocketConfig, MarketSession
from src.signals.sentiment.theme_detector import ThemeDetector, get_theme_detector
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


class TradingBot:
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
        self._watch_symbols_lock = asyncio.Lock()

        # 대시보드 서버
        self.dashboard: Optional[DashboardServer] = None

        # KIS 시장 데이터 조회 클라이언트
        self.kis_market_data: Optional[KISMarketData] = None

        # US 시장 오버나이트 데이터 클라이언트
        self.us_market_data: Optional[USMarketData] = None

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

            # 테마 탐지기 초기화 (kis_market_data + us_market_data 연동)
            theme_cfg = self.config.get("theme_detector") or {}
            self.theme_detector = ThemeDetector(
                kis_market_data=self.kis_market_data,
                us_market_data=self.us_market_data,
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
                    stop_loss_pct=momentum_cfg.get("stop_loss_pct", 2.0),
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
                        stop_loss_pct=theme_strategy_cfg.get("stop_loss_pct", 1.5),
                        take_profit_pct=theme_strategy_cfg.get("take_profit_pct", 3.0),
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
                    stop_loss_pct=gap_cfg.get("stop_loss_pct", 1.5),
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
                    "stop_loss_pct": momentum_cfg.get("stop_loss_pct", 2.0),
                    "trailing_stop_pct": momentum_cfg.get("trailing_stop_pct", 1.5),
                },
                "theme_chasing": {
                    "stop_loss_pct": theme_strategy_cfg.get("stop_loss_pct", 1.5),
                    "trailing_stop_pct": theme_strategy_cfg.get("trailing_stop_pct", 1.0),
                },
                "gap_and_go": {
                    "stop_loss_pct": gap_cfg.get("stop_loss_pct", 1.5),
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
                first_exit_ratio=exit_cfg.get("first_exit_ratio", 0.5),
                second_exit_pct=exit_cfg.get("second_exit_pct", 5.0),
                second_exit_ratio=exit_cfg.get("second_exit_ratio", 0.5),
                stop_loss_pct=exit_cfg.get("stop_loss_pct", 2.0),
                trailing_stop_pct=exit_cfg.get("trailing_stop_pct", 1.5),
                trailing_activate_pct=exit_cfg.get("trailing_activate_pct", 2.0),
                include_fees=exit_cfg.get("include_fees", True),
            ))
            logger.info("분할 익절 관리자 초기화 완료")

            # 자가 진화 엔진 초기화
            evolution_cfg = self.config.get("evolution") or {}
            if evolution_cfg.get("enabled", True):
                self.trade_journal = get_trade_journal()
                self.strategy_evolver = get_strategy_evolver()

                # 전략 등록 (파라미터 자동 조정용)
                for name, strategy in self.strategy_manager.strategies.items():
                    self.strategy_evolver.register_strategy(name, strategy)

                logger.info("자가 진화 엔진 초기화 완료")

            # 종목 스크리너 초기화
            self.screener = get_screener()
            screener_cfg = self.config.get("screener") or {}
            self.screener.min_volume_ratio = screener_cfg.get("min_volume_ratio", 2.0)
            self.screener.min_change_pct = screener_cfg.get("min_change_pct", 1.0)
            self.screener.max_change_pct = screener_cfg.get("max_change_pct", 15.0)
            self._screening_interval = screener_cfg.get("scan_interval_minutes", 10) * 60
            logger.info("종목 스크리너 초기화 완료")

            # 엔진에 컴포넌트 연결
            self.engine.strategy_manager = self.strategy_manager
            self.engine.risk_manager = self.risk_manager
            self.engine.broker = self.broker

            # 엔진 내부 RiskManager 인스턴스화 (SIGNAL/FILL 이벤트 핸들러 등록)
            self._engine_risk_manager = RiskManager(self.engine, self.config.trading.risk)
            logger.info("엔진 리스크 매니저 (SIGNAL 핸들러) 등록 완료")

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
            logger.error(f"기존 포지션 로드 오류: {e}")

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

        # 중복 제거
        self._watch_symbols = list(set(watch_cfg))
        logger.info(f"감시 종목 {len(self._watch_symbols)}개 로드")

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
                logger.debug(f"일봉 로드 실패 ({symbol}): {e}")
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

        except Exception as e:
            logger.error(f"시세 데이터 처리 오류: {e}")

    async def _check_exit_signal(self, symbol: str, current_price: Decimal):
        """분할 익절/손절 신호 확인"""
        if not self.exit_manager or not self.broker:
            return

        try:
            # 전략이 이미 SELL 주문을 pending으로 올렸으면 ExitManager 스킵
            if self._engine_risk_manager and symbol in self._engine_risk_manager._pending_orders:
                return

            # 청산 신호 확인
            exit_signal = self.exit_manager.update_price(symbol, current_price)

            if exit_signal:
                action, quantity, reason = exit_signal
                logger.info(f"[청산 신호] {symbol}: {reason} ({quantity}주)")

                # 주문 생성 및 제출
                from src.core.types import Order, OrderSide, OrderType

                order = Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,  # 시장가 청산
                    quantity=quantity,
                )

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
                    logger.info(f"[청산 주문 성공] {symbol} {quantity}주 -> 주문번호: {result}")
                    trading_logger.log_order(
                        symbol=symbol,
                        side="SELL",
                        quantity=quantity,
                        price=float(current_price),
                        order_type="MARKET",
                        status=f"submitted ({reason})"
                    )
                    # 엔진 RiskManager에 pending 등록 → 전략 SELL과 중복 방지
                    if self._engine_risk_manager:
                        self._engine_risk_manager._pending_orders.add(symbol)
                else:
                    logger.error(f"[청산 주문 실패] {symbol} - {result} (3회 시도 후)")
                    # 청산 실패는 중요하므로 알림
                    await self._send_error_alert(
                        "WARNING",
                        f"청산 주문 실패: {symbol} {quantity}주 (3회 재시도 후)",
                        f"사유: {result}\n이유: {reason}"
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
                if self._engine_risk_manager:
                    self._engine_risk_manager.block_symbol(order.symbol)
                    # pending 해제 + 예약 현금 환원
                    order_amount = (order.price * order.quantity) if order.price and order.quantity else Decimal("0")
                    self._engine_risk_manager.clear_pending(order.symbol, order_amount)
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
        logger.info(f"세션 변경: {event.session.value}")

        # 장 마감 시 일일 요약
        if event.session.value == "closed" and event.prev_session:
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

            # 포트폴리오 업데이트
            self.engine.update_position(fill)

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
                                positions = await self.broker.get_positions()
                                if fill.symbol in positions:
                                    real_name = getattr(positions[fill.symbol], 'name', '')
                                    if real_name and real_name != fill.symbol:
                                        position.name = real_name
                                        self.stock_name_cache[fill.symbol] = real_name
                            except Exception:
                                pass
                    elif pos_name and pos_name != fill.symbol:
                        self.stock_name_cache[fill.symbol] = pos_name

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

        trading_logger.log_daily_summary(
            total_trades=portfolio.daily_trades,
            wins=0,  # TODO: 승/패 추적
            losses=0,
            total_pnl=float(portfolio.daily_pnl),
            pnl_pct=pnl_pct
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

            # 8. 대시보드 서버 실행
            dashboard_cfg = self.config.get("dashboard") or {}
            if dashboard_cfg.get("enabled", True):
                self.dashboard = DashboardServer(
                    self,
                    host=dashboard_cfg.get("host", "0.0.0.0"),
                    port=dashboard_cfg.get("port", 8080),
                )
                tasks.append(asyncio.create_task(self.dashboard.run(), name="dashboard"))

            # 모든 태스크 실행
            await asyncio.gather(*tasks, return_exceptions=True)

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
        # 정규장: 09:00 ~ 15:30
        # 넥스트장: 15:30 ~ 20:00

        if 800 <= time_val < 850:
            return MarketSession.PRE_MARKET
        elif 900 <= time_val < 1530:
            return MarketSession.REGULAR
        elif 1530 <= time_val < 2000:
            return MarketSession.NEXT_MARKET
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
                        MarketSession.NEXT_MARKET: TradingSession.AFTER_HOURS,
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
                    logger.info(
                        f"[엔진 상태] 이벤트처리={engine_stats.events_processed}건, "
                        f"신호생성={engine_stats.signals_generated}건, "
                        f"오류={engine_stats.errors_count}건"
                    )
                    if self.ws_feed:
                        stats = self.ws_feed.get_stats()
                        logger.info(
                            f"[WS 상태] 연결={stats['connected']}, "
                            f"구독={stats['subscribed_count']}개, "
                            f"수신={stats['message_count']}건, "
                            f"마지막={stats['last_message_time'] or 'N/A'}"
                        )

                # 매일 06:00에 NXT 종목 갱신
                if now.hour == 6 and (last_nxt_update is None or last_nxt_update.date() != now.date()):
                    logger.info("[NXT] 매일 06:00 NXT 종목 갱신 시작")
                    await self._refresh_nxt_symbols()
                    last_nxt_update = now

                await asyncio.sleep(60)  # 1분마다 체크

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"세션 체크 오류: {e}")

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

    async def _run_pre_market_us_signal(self):
        """US 시장 오버나이트 시그널 사전 조회 (08:00 아침 레포트 전)"""
        if not self.us_market_data:
            return

        try:
            signal = await self.us_market_data.get_overnight_signal()
            if not signal:
                return

            sentiment = signal.get("sentiment", "neutral")
            indices = signal.get("indices", {})
            sector_signals = signal.get("sector_signals", {})

            logger.info(f"[US 시그널] 시장 심리: {sentiment}")
            for name, info in indices.items():
                logger.info(f"[US 시그널]   {name}: {info['change_pct']:+.1f}%")

            if sector_signals:
                boosted = [
                    f"{t}({s['boost']:+d})" for t, s in sector_signals.items()
                ]
                logger.info(f"[US 시그널] 한국 테마 영향: {', '.join(boosted)}")
            else:
                logger.info("[US 시그널] 한국 테마 영향 없음 (임계값 미달)")

        except Exception as e:
            logger.warning(f"[US 시그널] 오버나이트 시그널 조회 실패: {e}")

    async def _run_daily_report_scheduler(self):
        """
        일일 레포트 스케줄러

        - 00:00: 일일 통계 초기화
        - 08:00: 오늘의 추천 종목 레포트
        - 17:00: 추천 종목 결과 레포트
        """
        if not self.report_generator:
            self.report_generator = get_report_generator()

        last_morning_report: Optional[date] = None
        last_evening_report: Optional[date] = None
        last_holiday_refresh_month: Optional[str] = None
        last_daily_reset: Optional[date] = None

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                # 매월 25일 이후: 익월 휴장일 자동 갱신
                if now.day >= 25 and self.kis_market_data:
                    next_month = (now.replace(day=1) + timedelta(days=32)).strftime("%Y%m")
                    if last_holiday_refresh_month != next_month:
                        try:
                            h = await self.kis_market_data.fetch_holidays(next_month)
                            if h:
                                from src.core.engine import _kr_market_holidays
                                _kr_market_holidays.update(h)
                                logger.info(f"[휴장일] 익월({next_month}) 휴장일 {len(h)}일 추가 로드")
                            last_holiday_refresh_month = next_month
                        except Exception as e:
                            logger.warning(f"[휴장일] 익월 휴장일 갱신 실패: {e}")

                # 자정: 일일 통계 + 전략 상태 초기화 (공휴일 포함 매일 실행)
                if last_daily_reset != today:
                    try:
                        self.engine.reset_daily_stats()
                        if self.risk_manager:
                            self.risk_manager.reset_daily_stats()

                        # 전략별 일일 상태 초기화
                        for name, strat in self.strategy_manager.strategies.items():
                            if hasattr(strat, 'clear_gap_stocks'):
                                strat.clear_gap_stocks()
                            if hasattr(strat, 'clear_oversold_stocks'):
                                strat.clear_oversold_stocks()
                            if hasattr(strat, '_theme_entries'):
                                strat._theme_entries.clear()
                            if hasattr(strat, '_active_themes'):
                                strat._active_themes.clear()

                        last_daily_reset = today
                        logger.info("[스케줄러] 일일 통계 + 전략 상태 초기화 완료")
                    except Exception as e:
                        logger.error(f"[스케줄러] 일일 초기화 실패: {e}")

                # 공휴일(주말 포함)이면 레포트 스킵
                if is_kr_market_holiday(today):
                    await asyncio.sleep(60)
                    continue

                # 아침 8시 레포트 (08:00~08:05)
                if now.hour == 8 and now.minute < 5:
                    if last_morning_report != today:
                        # US 시장 오버나이트 시그널 선행 조회
                        await self._run_pre_market_us_signal()

                        logger.info("[레포트] 아침 추천 종목 레포트 발송 시작")
                        try:
                            await self.report_generator.generate_morning_report(
                                max_stocks=10,
                                send_telegram=True,
                            )
                            last_morning_report = today
                        except Exception as e:
                            logger.error(f"[레포트] 아침 레포트 발송 실패: {e}")

                # 오후 5시 결과 레포트 (17:00~17:05)
                if now.hour == 17 and now.minute < 5:
                    if last_evening_report != today:
                        logger.info("[레포트] 오후 결과 레포트 발송 시작")
                        try:
                            await self.report_generator.generate_evening_report(
                                send_telegram=True,
                            )
                            last_evening_report = today
                        except Exception as e:
                            logger.error(f"[레포트] 오후 레포트 발송 실패: {e}")

                # 1분마다 체크
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"레포트 스케줄러 오류: {e}")

    async def _run_evolution_scheduler(self):
        """
        자가 진화 스케줄러

        - 20:30: 일일 진화 실행 (장 마감 후)
          1. 거래 저널에서 데이터 분석
          2. LLM으로 전략 개선안 도출
          3. 파라미터 자동 조정
          4. 효과 평가 및 롤백
        """
        last_evolution_date: Optional[date] = None

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                # 넥스트장 마감 후 20:30~20:35에 일일 진화 실행
                if now.hour == 20 and 30 <= now.minute < 35:
                    if last_evolution_date != today:
                        logger.info("[진화] 일일 자가 진화 시작...")

                        try:
                            # 1. 복기 및 진화 실행
                            evolution_cfg = self.config.get("evolution") or {}
                            analysis_days = evolution_cfg.get("analysis_days", 7)
                            min_trades = evolution_cfg.get("min_trades_for_evolution", 5)

                            # 최소 거래 수 체크
                            recent_trades = self.trade_journal.get_recent_trades(days=analysis_days)

                            if len(recent_trades) >= min_trades:
                                # 진화 실행
                                result = await self.strategy_evolver.evolve(days=analysis_days)

                                if result:
                                    # 진화 결과 로깅
                                    logger.info(
                                        f"[진화] 완료 - 평가={result.overall_assessment}, "
                                        f"인사이트 {len(result.key_insights)}개, "
                                        f"파라미터 조정 {len(result.parameter_adjustments)}개"
                                    )

                                    # 핵심 인사이트 로그
                                    for insight in result.key_insights[:3]:
                                        logger.info(f"  [인사이트] {insight}")

                                    # 파라미터 변경 로그
                                    for adj in result.parameter_adjustments:
                                        logger.info(
                                            f"  [파라미터] {adj.parameter}: "
                                            f"{adj.current_value} -> {adj.suggested_value} "
                                            f"(신뢰도: {adj.confidence:.0%})"
                                        )

                                    # 텔레그램 알림 (선택적)
                                    if evolution_cfg.get("send_telegram", True):
                                        await self._send_evolution_report(result)

                                    # 거래 로그에 기록 (복기용)
                                    trading_logger.log_evolution(
                                        assessment=result.overall_assessment,
                                        confidence=result.confidence_score,
                                        insights=result.key_insights,
                                        parameter_changes=[
                                            {
                                                "parameter": p.parameter,
                                                "from": p.current_value,
                                                "to": p.suggested_value,
                                                "confidence": p.confidence,
                                            }
                                            for p in result.parameter_adjustments
                                        ],
                                    )
                                else:
                                    logger.info("[진화] 진화 결과 없음 (변경 불필요)")
                            else:
                                logger.info(
                                    f"[진화] 거래 부족으로 스킵 "
                                    f"({len(recent_trades)}/{min_trades}건)"
                                )

                            last_evolution_date = today

                        except Exception as e:
                            logger.error(f"[진화] 실행 오류: {e}")
                            import traceback
                            await self._send_error_alert(
                                "ERROR",
                                "자가 진화 실행 오류",
                                traceback.format_exc()
                            )

                # 매 시간 정각에 진화 효과 평가 (적용된 변경이 있는 경우)
                if now.minute < 5 and 9 <= now.hour <= 15:
                    try:
                        # 진화 상태 확인 및 효과 평가
                        state = self.strategy_evolver.get_evolution_state()

                        if state and state.active_changes:
                            evaluation = await self.strategy_evolver.evaluate_changes()

                            if evaluation:
                                logger.info(
                                    f"[진화 평가] 활성 변경 {len(state.active_changes)}개, "
                                    f"효과: {evaluation.get('effectiveness', 'unknown')}"
                                )

                                # 효과 없으면 롤백 고려
                                if evaluation.get('should_rollback', False):
                                    logger.warning("[진화] 효과 없음 - 롤백 실행")
                                    await self.strategy_evolver.rollback_last_change()

                    except Exception as e:
                        logger.error(f"[진화 평가] 오류: {e}")

                # 1분마다 체크
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"진화 스케줄러 오류: {e}")

    async def _send_evolution_report(self, result):
        """진화 결과 텔레그램 알림"""
        try:
            emoji_map = {"good": "✅", "fair": "⚠️", "poor": "❌", "no_data": "📊"}
            emoji = emoji_map.get(result.overall_assessment, "📊")

            text = f"""
{emoji} <b>AI Trader v2 - 일일 진화 리포트</b>

<b>분석 기간:</b> 최근 {result.period_days}일
<b>전체 평가:</b> {result.overall_assessment.upper()}
<b>신뢰도:</b> {result.confidence_score:.0%}

<b>핵심 인사이트:</b>
"""
            for i, insight in enumerate(result.key_insights[:5], 1):
                text += f"{i}. {insight}\n"

            if result.parameter_adjustments:
                text += "\n<b>파라미터 조정:</b>\n"
                for adj in result.parameter_adjustments[:3]:
                    text += (
                        f"- {adj.parameter}: {adj.current_value} -> {adj.suggested_value} "
                        f"({adj.confidence:.0%})\n"
                    )

            if result.next_week_outlook:
                text += f"\n<b>전망:</b> {result.next_week_outlook[:200]}"

            await send_alert(text)

        except Exception as e:
            logger.error(f"진화 리포트 전송 실패: {e}")

    async def _run_theme_detection(self):
        """테마 탐지 루프"""
        try:
            scan_interval = self.theme_detector.detection_interval_minutes * 60

            while self.running:
                try:
                    # 테마 스캔
                    themes = await self.theme_detector.detect_themes(force=True)

                    if themes:
                        logger.info(f"[테마 탐지] {len(themes)}개 테마 감지")

                        # 테마 이벤트 발행
                        for theme in themes:
                            event = ThemeEvent(
                                source="theme_detector",
                                name=theme.name,
                                score=theme.score,
                                keywords=theme.keywords,
                                symbols=theme.related_stocks,
                            )
                            await self.engine.emit(event)

                            # 테마 관련 종목 WebSocket 구독 추가
                            if self.ws_feed and theme.related_stocks:
                                async with self._watch_symbols_lock:
                                    new_symbols = [s for s in theme.related_stocks
                                                 if s not in self._watch_symbols]
                                    if new_symbols:
                                        await self.ws_feed.subscribe(new_symbols[:10])
                                        self._watch_symbols.extend(new_symbols[:10])
                                        logger.info(f"[테마 탐지] 신규 종목 구독: {new_symbols[:10]}")

                        # 종목별 뉴스 임팩트 → NewsEvent 발행 + WS 구독
                        sentiments = self.theme_detector.get_all_stock_sentiments()
                        for symbol, data in sentiments.items():
                            impact = data.get("impact", 0)
                            direction = data.get("direction", "bullish")
                            reason = data.get("reason", "")

                            # 임팩트 70+ 종목은 NewsEvent 발행
                            if impact >= 70:
                                news_event = NewsEvent(
                                    source="theme_detector",
                                    title=reason,
                                    symbols=[symbol],
                                    sentiment=1.0 if direction == "bullish" else -1.0,
                                )
                                await self.engine.emit(news_event)

                                # WebSocket 구독에 자동 추가
                                if self.ws_feed:
                                    async with self._watch_symbols_lock:
                                        if symbol not in self._watch_symbols:
                                            await self.ws_feed.subscribe([symbol])
                                            self._watch_symbols.append(symbol)
                                            logger.info(
                                                f"[뉴스 임팩트] {symbol} 구독 추가 "
                                                f"(impact={impact}, {direction})"
                                            )

                except Exception as e:
                    logger.error(f"테마 스캔 오류: {e}")

                # 다음 스캔까지 대기
                await asyncio.sleep(scan_interval)

        except asyncio.CancelledError:
            pass

    async def _run_screening(self):
        """주기적 종목 스크리닝 루프"""
        try:
            # 초기 대기 (다른 컴포넌트 초기화 후)
            await asyncio.sleep(60)

            while self.running:
                try:
                    # 세션 확인 - 마감 시간에는 스크리닝 스킵
                    current_session = self._get_current_session()
                    if current_session == MarketSession.CLOSED:
                        await asyncio.sleep(self._screening_interval)
                        continue

                    logger.info(f"[스크리닝] 동적 종목 스캔 시작... (세션: {current_session.value})")

                    # 통합 스크리닝 실행 (theme_detector 연동)
                    screened = await self.screener.screen_all(
                        theme_detector=self.theme_detector,
                    )

                    # 점수 맵 생성 (WebSocket 우선순위용)
                    scores = {s.symbol: s.score for s in screened}

                    new_symbols = []
                    async with self._watch_symbols_lock:
                        for stock in screened:
                            # 높은 점수 종목만 감시 목록에 추가
                            if stock.score >= 70 and stock.symbol not in self._watch_symbols:
                                new_symbols.append(stock.symbol)
                                self._watch_symbols.append(stock.symbol)
                                logger.info(
                                    f"  [NEW] {stock.symbol} {stock.name}: "
                                    f"점수={stock.score:.0f}, {', '.join(stock.reasons[:2])}"
                                )

                    # 신규 종목 WebSocket 구독 (점수와 함께)
                    if self.ws_feed:
                        # 전체 점수 업데이트
                        self.ws_feed.set_symbol_scores(scores)

                        if new_symbols:
                            # 신규 종목 구독 (롤링 방식으로 자동 관리)
                            await self.ws_feed.subscribe(new_symbols, scores)
                            stats = self.ws_feed.get_subscription_stats()
                            logger.info(
                                f"[스크리닝] 신규 {len(new_symbols)}개 추가 → "
                                f"총 감시={stats['total_watch']}, 구독={stats['subscribed_count']}, "
                                f"롤링대기={stats['rolling_queue_size']}"
                            )

                            # 감시 종목 변경 로그
                            trading_logger.log_watchlist_update(
                                added=new_symbols,
                                removed=[],
                                total=stats['total_watch'],
                            )

                    # 스크리닝 결과 로그 기록 (복기용)
                    if screened:
                        trading_logger.log_screening(
                            source=f"periodic_{current_session.value}",
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

                    logger.info(f"[스크리닝] 완료 - 총 {len(screened)}개 후보, 신규 {len(new_symbols)}개")

                except Exception as e:
                    logger.error(f"스크리닝 오류: {e}")

                # 다음 스캔까지 대기
                await asyncio.sleep(self._screening_interval)

        except asyncio.CancelledError:
            pass

    async def _run_fill_check(self):
        """체결 확인 루프 (5초마다)"""
        check_interval = 5  # 초

        try:
            while self.running:
                try:
                    # 미체결 주문이 있는 경우에만 확인
                    open_orders = await self.broker.get_open_orders()

                    if open_orders:
                        fills = await self.broker.check_fills()

                        for fill in fills:
                            logger.info(
                                f"[체결] {fill.symbol} {fill.side.value} "
                                f"{fill.quantity}주 @ {fill.price:,.0f}원"
                            )

                            # 체결 이벤트 발행 → _on_fill() 핸들러에서 일괄 처리
                            # (update_position, exit_manager, trading_logger, trade_journal)
                            from src.core.event import FillEvent
                            event = FillEvent.from_fill(fill, source="kis_broker")
                            await self.engine.emit(event)

                except Exception as e:
                    logger.error(f"체결 확인 오류: {e}")
                    # 연속 오류 시에만 알림 (3회 이상)
                    if not hasattr(self, '_fill_check_errors'):
                        self._fill_check_errors = 0
                    self._fill_check_errors += 1
                    if self._fill_check_errors >= 3:
                        await self._send_error_alert(
                            "ERROR",
                            f"체결 확인 연속 오류 ({self._fill_check_errors}회)",
                            str(e)
                        )
                        self._fill_check_errors = 0

                await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            pass

    async def _sync_portfolio(self):
        """KIS API와 포트폴리오 동기화"""
        if not self.broker:
            return

        try:
            # 1. KIS API에서 실제 잔고/포지션 조회
            balance = await self.broker.get_account_balance()
            kis_positions = await self.broker.get_positions()

            if not balance:
                logger.warning("포트폴리오 동기화: 잔고 조회 실패")
                return

            portfolio = self.engine.portfolio
            kis_symbols = set(kis_positions.keys()) if kis_positions else set()
            bot_symbols = set(portfolio.positions.keys())

            # 2. 유령 포지션 제거 (봇에만 있고 KIS에 없는 종목)
            ghost_symbols = bot_symbols - kis_symbols
            for symbol in ghost_symbols:
                pos = portfolio.positions[symbol]
                logger.warning(
                    f"[동기화] 유령 포지션 제거: {symbol} {pos.name} "
                    f"({pos.quantity}주 @ {pos.avg_price:,.0f}원)"
                )
                del portfolio.positions[symbol]
                if self.exit_manager and hasattr(self.exit_manager, '_states'):
                    self.exit_manager._states.pop(symbol, None)

            # 3. 누락 포지션 추가 (KIS에 있고 봇에 없는 종목)
            new_symbols = kis_symbols - bot_symbols
            for symbol in new_symbols:
                pos = kis_positions[symbol]
                portfolio.positions[symbol] = pos
                logger.info(
                    f"[동기화] 포지션 추가: {symbol} {pos.name} "
                    f"({pos.quantity}주 @ {pos.avg_price:,.0f}원)"
                )
                if self.exit_manager:
                    self.exit_manager.register_position(pos)
                if symbol not in self._watch_symbols:
                    self._watch_symbols.append(symbol)

            # 4. 기존 포지션 수량/가격 업데이트
            common_symbols = bot_symbols & kis_symbols
            for symbol in common_symbols:
                bot_pos = portfolio.positions[symbol]
                kis_pos = kis_positions[symbol]
                if bot_pos.quantity != kis_pos.quantity:
                    logger.warning(
                        f"[동기화] 수량 수정: {symbol} "
                        f"{bot_pos.quantity}주 → {kis_pos.quantity}주"
                    )
                    bot_pos.quantity = kis_pos.quantity
                if kis_pos.avg_price > 0 and bot_pos.avg_price != kis_pos.avg_price:
                    logger.info(
                        f"[동기화] 평단가 수정: {symbol} "
                        f"{bot_pos.avg_price:,.0f}원 → {kis_pos.avg_price:,.0f}원"
                    )
                    bot_pos.avg_price = kis_pos.avg_price
                if kis_pos.current_price > 0:
                    bot_pos.current_price = kis_pos.current_price

            # 5. 현금 동기화
            available_cash = Decimal(str(balance.get('available_cash', 0)))
            if available_cash > 0:
                old_cash = portfolio.cash
                portfolio.cash = available_cash
                if abs(old_cash - available_cash) > 1000:
                    logger.info(
                        f"[동기화] 현금 수정: {old_cash:,.0f}원 → {available_cash:,.0f}원"
                    )

            # 6. initial_capital은 당일 시작 자본 → 동기화에서 갱신하지 않음
            #    (봇 시작 시 또는 daily_reset에서만 설정)
            #    포지션 사이징은 total_equity를 사용하므로 영향 없음

            changes = len(ghost_symbols) + len(new_symbols)
            if changes > 0:
                logger.info(
                    f"[동기화] 완료: 제거={len(ghost_symbols)}, "
                    f"추가={len(new_symbols)}, "
                    f"보유={len(portfolio.positions)}종목"
                )
            else:
                logger.debug(
                    f"[동기화] 확인 완료: 보유={len(portfolio.positions)}종목, 변경 없음"
                )

        except Exception as e:
            logger.error(f"포트폴리오 동기화 오류: {e}")

    async def _run_portfolio_sync(self):
        """주기적 포트폴리오 동기화 루프"""
        await asyncio.sleep(30)  # 시작 후 30초 대기
        while self.running:
            try:
                await self._sync_portfolio()
            except Exception as e:
                logger.error(f"동기화 루프 오류: {e}")
            await asyncio.sleep(300)  # 5분마다 동기화

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
