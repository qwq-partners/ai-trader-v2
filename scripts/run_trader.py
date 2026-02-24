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
import os
import psutil
import aiohttp
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Set, List

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
from src.utils.session_util import SessionUtil
from src.analytics.daily_report import get_report_generator
from src.utils.telegram import send_alert
from src.core.evolution import (
    get_trade_journal, get_trade_reviewer, get_strategy_evolver
)
from src.dashboard.server import DashboardServer
from bot_schedulers import SchedulerMixin


# ============================================================
# PID 파일 + flock 기반 프로세스 중복 방지
# ============================================================

import fcntl

PID_FILE = Path.home() / ".cache" / "ai_trader" / "trader.pid"
LOCK_FILE = Path.home() / ".cache" / "ai_trader" / "trader.lock"
_lock_fd = None  # 전역 파일 디스크립터 (프로세스 수명 동안 유지)


def _find_other_trader_processes() -> list:
    """현재 프로세스를 제외한 run_trader.py 프로세스 목록 반환"""
    my_pid = os.getpid()
    others = []
    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            pid = proc.info['pid']
            if pid == my_pid:
                continue
            cmdline = ' '.join(proc.info.get('cmdline') or [])
            if 'run_trader.py' in cmdline and 'grep' not in cmdline:
                others.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return others


def acquire_singleton_lock() -> bool:
    """
    flock 기반 싱글톤 락 획득 + 기존 프로세스 강제 종료

    1단계: 실행 중인 다른 run_trader.py 프로세스를 SIGTERM → SIGKILL
    2단계: flock 파일 락으로 race condition 완전 차단
    3단계: PID 파일 기록

    Returns:
        True: 락 획득 성공 (유일한 프로세스)
        False: 락 획득 실패
    """
    global _lock_fd
    import time

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 1단계: 기존 프로세스 종료
    others = _find_other_trader_processes()
    if others:
        logger.warning(f"기존 트레이더 프로세스 발견: {others} — 종료 시도")
        for pid in others:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        # SIGTERM 대기 (최대 3초)
        time.sleep(3)

        # 아직 살아있으면 SIGKILL
        for pid in others:
            try:
                if psutil.pid_exists(pid):
                    os.kill(pid, signal.SIGKILL)
                    logger.warning(f"PID {pid} SIGKILL 전송")
            except ProcessLookupError:
                pass

        time.sleep(1)

    # 2단계: flock 획득 (non-blocking)
    try:
        _lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
    except (IOError, OSError):
        logger.error("flock 획득 실패 — 다른 프로세스가 이미 락을 보유 중")
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False

    # 3단계: PID 파일 기록
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    logger.info(f"싱글톤 락 획득 완료 (PID: {os.getpid()})")
    return True


def release_singleton_lock():
    """락 해제 + PID 파일 제거"""
    global _lock_fd
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception as e:
        logger.warning(f"PID 파일 제거 실패: {e}")

    try:
        if _lock_fd:
            fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
            _lock_fd.close()
            _lock_fd = None
    except Exception as e:
        logger.warning(f"flock 해제 실패: {e}")

    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


# 하위 호환용 — 기존 코드에서 호출하는 함수
def write_pid_file():
    """PID 파일 갱신 (acquire_singleton_lock에서 이미 기록됨)"""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))


def remove_pid_file():
    """PID 파일 제거"""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception as e:
        logger.warning(f"PID 파일 제거 실패: {e}")


# ============================================================


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
        self._screening_signal_cooldown: dict = {}  # 장중 스크리닝 시그널 쿨다운
        self._daily_entry_count: Dict[str, int] = {}  # 종목별 당일 진입 횟수

        # 일일 레포트 생성기
        self.report_generator = None

        # MCP 서버 클라이언트 (pykrx, naver-search)
        self._mcp_manager = None

        # 종목 뉴스/공시 검증기
        self._stock_validator: Optional['StockValidator'] = None

        # 자가 진화 엔진
        self.trade_journal = None
        self.strategy_evolver = None

        # 자산 히스토리 추적기
        self.equity_tracker = None

        # 일일 거래 리뷰어
        self.daily_reviewer = None

        # 감시 종목
        self._watch_symbols: List[str] = []

        # 전략별 청산 파라미터 (ExitManager에 전달용)
        self._strategy_exit_params: Dict[str, Dict[str, float]] = {}
        # 종목별 전략 매핑 (ExitManager 등록 시 사용)
        self._symbol_strategy: Dict[str, str] = {}
        # 종목별 신호 정보 (TradeJournal 기록용)
        # TODO: Signal 타입으로 교체 필요
        self._symbol_signals: Dict[str, Any] = {}
        self._exit_pending_symbols: Set[str] = set()  # ExitManager 매도 중복 방지
        self._exit_pending_timestamps: Dict[str, datetime] = {}  # 매도 pending 타임스탬프
        self._exit_reasons: Dict[str, str] = {}  # 청산 사유 저장 (symbol → reason)
        self._sell_blocked_symbols: Dict[str, datetime] = {}  # 청산 실패 종목 일시 차단 (NXT 불가 등)
        self._pause_resume_at: Optional[datetime] = None  # 자동 재개 타이머
        self._watch_symbols_lock = asyncio.Lock()
        self._portfolio_lock = asyncio.Lock()

        # 섹터 분산
        self._sector_cache: dict = {}

        # 외부 계좌 조회 (대시보드 전용)
        self._external_accounts: list = []  # [(name, cano, acnt_prdt_cd), ...]

        # REST 피드용 스크리닝 캐시
        self._last_screened: list = []

        # 배치 분석기 (스윙 모멘텀)
        self.batch_analyzer = None

        # 대시보드 서버
        self.dashboard: Optional[DashboardServer] = None

        # 헬스 모니터
        self.health_monitor = None

        # KIS 시장 데이터 조회 클라이언트
        self.kis_market_data: Optional[KISMarketData] = None

        # US 시장 오버나이트 데이터 클라이언트
        self.us_market_data: Optional[USMarketData] = None

        # 종목 마스터
        self.stock_master: Optional[StockMaster] = None

        # 종목명 캐시 (symbol → name)
        self.stock_name_cache: Dict[str, str] = {}
        # 엔진에 종목명 캐시 참조 연결 (대시보드 이벤트 로그용)
        self.engine._stock_name_cache = self.stock_name_cache

        # 시그널 핸들러
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """종료 시그널 핸들러"""
        def handle_shutdown(signum, frame):
            logger.warning(f"종료 신호 수신 ({signum})")
            self.stop()

        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)

    async def _get_sector(self, symbol: str) -> Optional[str]:
        """종목 섹터 조회 (StockMaster DB corp_cls 기반, 캐시 적용)"""
        if symbol in self._sector_cache:
            return self._sector_cache[symbol]
        if self.stock_master and hasattr(self.stock_master, 'pool') and self.stock_master.pool:
            try:
                async with self.stock_master.pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT corp_cls FROM kr_stock_master WHERE ticker = $1", symbol)
                    if row and row["corp_cls"]:
                        if len(self._sector_cache) > 2000:
                            # 절반 제거 (간단 LRU 근사)
                            keys_to_del = list(self._sector_cache.keys())[:1000]
                            for k in keys_to_del:
                                del self._sector_cache[k]
                        self._sector_cache[symbol] = row["corp_cls"]
                        return row["corp_cls"]
            except Exception as e:
                logger.debug(f"[섹터] {symbol} 조회 실패: {e}")
        return None

    # CRITICAL 에러 유형 (즉시 텔레그램 발송 대상)
    _CRITICAL_ERROR_TYPES = {
        "daily_loss_limit", "api_failure", "broker_disconnect",
        "position_sync_error", "order_reject_critical", "system_crash",
    }

    async def _send_error_alert(self, error_type: str, message: str, details: str = "",
                                critical: bool = False):
        """
        에러 알림

        CRITICAL 에러는 즉시 텔레그램 발송, 일반 에러는 로그만 기록합니다.
        """
        log_msg = f"[알림] {error_type}: {message}" + (f" | {details[:200]}" if details else "")
        is_critical = critical or error_type in self._CRITICAL_ERROR_TYPES
        if is_critical:
            logger.error(log_msg)
            try:
                alert_text = f"🚨 <b>[{error_type}]</b> {message}"
                if details:
                    alert_text += f"\n<pre>{details[:300]}</pre>"
                await send_alert(alert_text)
            except Exception as e:
                logger.error(f"CRITICAL 알림 텔레그램 발송 실패: {e}")
        else:
            logger.warning(log_msg)

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
                        self.config.trading.initial_capital = Decimal(str(actual_capital))

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

            # MCP 서버 클라이언트 초기화 (pykrx, naver-search)
            try:
                from src.utils.mcp_client import get_mcp_manager
                self._mcp_manager = get_mcp_manager()
                await self._mcp_manager.initialize()
                logger.info("MCP 서버 클라이언트 초기화 완료")
            except Exception as e:
                logger.warning(f"MCP 클라이언트 초기화 실패 (무시): {e}")
                self._mcp_manager = None

            # 종목 뉴스/공시 검증기 초기화
            try:
                from src.signals.fundamentals import get_stock_validator
                self._stock_validator = get_stock_validator()
                await self._stock_validator.initialize()
                logger.info("종목 뉴스/공시 검증기 초기화 완료")
            except Exception as e:
                logger.warning(f"종목 검증기 초기화 실패 (무시): {e}")
                self._stock_validator = None

            # 전략 매니저 초기화
            self.strategy_manager = StrategyManager(self.engine)

            # 모멘텀 전략 등록
            momentum_cfg = self.config.get("strategies", "momentum_breakout") or {}
            if momentum_cfg.get("enabled", True):
                momentum_strategy = MomentumBreakoutStrategy(MomentumConfig(
                    min_breakout_pct=momentum_cfg.get("min_breakout_pct", 1.0),
                    volume_surge_ratio=momentum_cfg.get("volume_surge_ratio", 3.0),
                    stop_loss_pct=momentum_cfg.get("stop_loss_pct", 2.5),
                    take_profit_pct=momentum_cfg.get("take_profit_pct", 5.0),
                    trailing_stop_pct=momentum_cfg.get("trailing_stop_pct", 1.5),
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

            # 스윙 전략 설정 (배치 분석기 및 청산 파라미터용)
            strategies_cfg = self.config.get("strategies") or {}
            rsi2_cfg = strategies_cfg.get("rsi2_reversal") or {}
            sepa_cfg = strategies_cfg.get("sepa_trend") or {}

            # 전략별 청산 파라미터 기록 (ExitManager 전달용: 손절/트레일링 + 익절 목표)
            self._strategy_exit_params = {
                "momentum_breakout": {
                    "stop_loss_pct": momentum_cfg.get("stop_loss_pct", 2.5),
                    "trailing_stop_pct": momentum_cfg.get("trailing_stop_pct", 1.5),
                    "first_exit_pct": 3.0,    # 수정 B안: 3.0% (기존 2.5%)
                    "second_exit_pct": 5.0,   # 수정 B안: 5.0% (기존 6.0%)
                    "third_exit_pct": momentum_cfg.get("take_profit_pct", 10.0),  # 10.0%
                },
                "theme_chasing": {
                    "stop_loss_pct": theme_strategy_cfg.get("stop_loss_pct", 2.0),
                    "trailing_stop_pct": theme_strategy_cfg.get("trailing_stop_pct", 1.0),
                    "first_exit_pct": theme_strategy_cfg.get("take_profit_pct", 8.0) * 0.3,   # 2.4%
                    "second_exit_pct": theme_strategy_cfg.get("take_profit_pct", 8.0) * 0.6,  # 4.8%
                    "third_exit_pct": theme_strategy_cfg.get("take_profit_pct", 8.0),          # 8.0%
                },
                "gap_and_go": {
                    "stop_loss_pct": gap_cfg.get("stop_loss_pct", 2.0),
                    "trailing_stop_pct": gap_cfg.get("trailing_stop_pct", 1.5),
                    "first_exit_pct": gap_cfg.get("take_profit_pct", 8.0) * 0.3,   # 2.4%
                    "second_exit_pct": gap_cfg.get("take_profit_pct", 8.0) * 0.6,  # 4.8%
                    "third_exit_pct": gap_cfg.get("take_profit_pct", 8.0),          # 8.0%
                },
                "mean_reversion": {
                    "stop_loss_pct": mr_cfg.get("stop_loss_pct", 3.0),
                    "trailing_stop_pct": mr_cfg.get("trailing_stop_pct", 2.0),
                    "first_exit_pct": mr_cfg.get("take_profit_pct", 5.0) * 0.3,   # 1.5%
                    "second_exit_pct": mr_cfg.get("take_profit_pct", 5.0) * 0.6,  # 3.0%
                    "third_exit_pct": mr_cfg.get("take_profit_pct", 5.0),          # 5.0%
                },
                "rsi2_reversal": {
                    "stop_loss_pct": rsi2_cfg.get("stop_loss_pct", 5.0),
                    "trailing_stop_pct": 3.0,
                    "first_exit_pct": 5.0,
                    "second_exit_pct": 10.0,
                    "third_exit_pct": 15.0,
                },
                "sepa_trend": {
                    "stop_loss_pct": sepa_cfg.get("stop_loss_pct", 5.0),
                    "trailing_stop_pct": 3.0,
                    "first_exit_pct": 5.0,
                    "second_exit_pct": 10.0,
                    "third_exit_pct": 15.0,
                },
                "strategic_swing": {
                    "stop_loss_pct": 5.0,
                    "trailing_stop_pct": 3.0,
                    "first_exit_pct": 5.0,
                    "second_exit_pct": 10.0,
                    "third_exit_pct": 15.0,
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
                first_exit_ratio=exit_cfg.get("first_exit_ratio", 0.25),
                second_exit_pct=exit_cfg.get("second_exit_pct", 5.0),
                second_exit_ratio=exit_cfg.get("second_exit_ratio", 0.3),
                stop_loss_pct=exit_cfg.get("stop_loss_pct", 2.5),
                trailing_stop_pct=exit_cfg.get("trailing_stop_pct", 1.5),
                trailing_activate_pct=exit_cfg.get("trailing_activate_pct", 3.0),
                min_stop_pct=exit_cfg.get("min_stop_pct", 2.0),
                max_stop_pct=exit_cfg.get("max_stop_pct", 4.0),
                atr_multiplier=exit_cfg.get("atr_multiplier", 1.5),
                include_fees=exit_cfg.get("include_fees", True),
            ))
            logger.info("분할 익절 관리자 초기화 완료")

            # 기존 포지션을 ExitManager에 등록 (초기화 순서: 포지션 로드 → ExitManager 생성 후 보완)
            if self.engine.portfolio.positions:
                for symbol, position in self.engine.portfolio.positions.items():
                    price_history = self._get_price_history_for_atr(symbol)
                    # 전략별 청산 파라미터 조회 (재시작 시 기존 포지션에 전략 ExitConfig 반영)
                    exit_params = self._strategy_exit_params.get(position.strategy, {}) if position.strategy else {}
                    self.exit_manager.register_position(
                        position,
                        price_history=price_history,
                        stop_loss_pct=exit_params.get("stop_loss_pct"),
                        trailing_stop_pct=exit_params.get("trailing_stop_pct"),
                        first_exit_pct=exit_params.get("first_exit_pct"),
                        second_exit_pct=exit_params.get("second_exit_pct"),
                        third_exit_pct=exit_params.get("third_exit_pct"),
                    )
                logger.info(
                    f"기존 포지션 {len(self.engine.portfolio.positions)}개 ExitManager 등록 완료"
                )

            # 자가 진화 엔진 초기화
            evolution_cfg = self.config.get("evolution") or {}
            if evolution_cfg.get("enabled", True):
                self.trade_journal = get_trade_journal()

                # TradeStorage DB 연결 (DB+JSON 듀얼 모드)
                if hasattr(self.trade_journal, 'connect'):
                    await self.trade_journal.connect()
                    # KIS 당일 체결 동기화
                    if self.broker and hasattr(self.trade_journal, 'sync_from_kis'):
                        await self.trade_journal.sync_from_kis(self.broker, engine=self.engine)
                    # DB 연결 후 포지션 전략/진입시간 복원
                    if self.engine.portfolio.positions:
                        await self._restore_position_metadata(self.engine.portfolio.positions)

                # 일일 통계 복원 (재시작 시 daily_pnl / daily_start_unrealized_pnl 유지)
                self.engine.restore_daily_stats()

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

                # 기존 포지션에 전략 정보 보강 (TradeJournal에서)
                if self.engine.portfolio.positions and self.trade_journal:
                    open_trades = self.trade_journal.get_open_trades()
                    trade_by_symbol = {t.symbol: t for t in open_trades}
                    for symbol, pos in self.engine.portfolio.positions.items():
                        if not pos.strategy and symbol in trade_by_symbol:
                            trade = trade_by_symbol[symbol]
                            if trade.entry_strategy:  # 빈 문자열 제외
                                pos.strategy = trade.entry_strategy
                                if not pos.entry_time and trade.entry_time:
                                    pos.entry_time = trade.entry_time
                                logger.info(f"  포지션 전략 보강: {symbol} → {trade.entry_strategy}")

                logger.info("자가 진화 엔진 초기화 완료")

            # 자산 히스토리 추적기 초기화
            from src.analytics.equity_tracker import EquityTracker
            self.equity_tracker = EquityTracker()
            # 기존 거래 저널에서 과거 데이터 백필
            if self.trade_journal:
                self.equity_tracker.backfill_from_journal(
                    initial_capital=float(self.engine.portfolio.initial_capital)
                )

            # 일일 거래 리뷰어 초기화
            from src.core.evolution.daily_reviewer import DailyReviewer
            self.daily_reviewer = DailyReviewer()
            logger.info("일일 거래 리뷰어 초기화 완료")

            # 종목 스크리너 초기화
            self.screener = get_screener()
            screener_cfg = self.config.get("screener") or {}
            self.screener.min_volume_ratio = screener_cfg.get("min_volume_ratio", 2.0)
            self.screener.min_change_pct = screener_cfg.get("min_change_pct", 1.0)
            self.screener.max_change_pct = screener_cfg.get("max_change_pct", 15.0)
            self.screener.min_trading_value = screener_cfg.get("min_trading_value", 100000000)  # 기본 1억원
            self._screening_interval = screener_cfg.get("scan_interval_minutes", 10) * 60
            # stock_master → screener 연동 (종목 DB 활용)
            if self.stock_master:
                self.screener.set_stock_master(self.stock_master)
            # broker → screener 연동 (모멘텀/변동성 필터용)
            if self.broker:
                self.screener.set_broker(self.broker)
            logger.info("종목 스크리너 초기화 완료")

            # 엔진에 컴포넌트 연결
            self.engine.strategy_manager = self.strategy_manager
            self.engine.broker = self.broker

            # 엔진 이벤트 핸들링용 RiskManager (SIGNAL→ORDER, FILL 추적)
            # RiskMgr(대시보드/daily_stats)를 risk_validator로 주입하여 상태 공유
            engine_risk_manager = RiskManager(
                self.engine, self.config.trading.risk,
                risk_validator=self.risk_manager,
                sector_lookup=self._get_sector,
            )
            self.engine.risk_manager = engine_risk_manager
            logger.info("엔진 리스크 매니저 (SIGNAL 핸들러 + 리스크 검증 위임) 등록 완료")

            # 데이터 소스 확인
            data_cfg = self.config.get("data") or {}
            realtime_source = data_cfg.get("realtime_source", "kis_websocket")

            # WebSocket 피드 초기화 (항상 — 보유종목 실시간 시세 + NXT 지원)
            # REST 폴링은 스크리닝 종목용으로 병행 유지
            if not self.dry_run:
                self.ws_feed = KISWebSocketFeed(KISWebSocketConfig.from_env())
                self.ws_feed.on_market_data(self._on_market_data)
                if realtime_source == "rest_polling":
                    logger.info("REST+WS 병행 모드: WS=보유종목 실시간, REST=스크리닝 종목")
                else:
                    logger.info("WebSocket 피드 초기화 완료")

            # 배치 분석기 초기화 (스윙 모멘텀 모드)
            if rsi2_cfg.get("enabled") or sepa_cfg.get("enabled"):
                from src.core.batch_analyzer import BatchAnalyzer
                self.batch_analyzer = BatchAnalyzer(
                    engine=self.engine,
                    broker=self.broker,
                    kis_market_data=self.kis_market_data,
                    stock_master=self.stock_master,
                    exit_manager=self.exit_manager,
                    config={
                        "rsi2_reversal": rsi2_cfg,
                        "sepa_trend": sepa_cfg,
                        "batch": self.config.get("batch") or {},
                    },
                )
                # ExitManager 보유기간 설정
                batch_cfg = self.config.get("batch") or {}
                if self.exit_manager:
                    self.exit_manager._max_holding_days = batch_cfg.get("max_holding_days", 10)
                logger.info("배치 분석기 초기화 완료 (스윙 모멘텀 모드)")

            # 헬스 모니터 초기화
            from src.monitoring.health_monitor import HealthMonitor
            self.health_monitor = HealthMonitor(self)
            logger.info("헬스 모니터 초기화 완료")

            # 외부 계좌 설정 파싱 (대시보드 조회 전용)
            ext_accounts_str = os.getenv("KIS_EXT_ACCOUNTS", "")
            if ext_accounts_str:
                for entry in ext_accounts_str.split(","):
                    parts = entry.strip().split(":")
                    if len(parts) != 3:
                        logger.warning(f"외부 계좌 형식 오류 (무시): {entry.strip()} - '이름:CANO:ACNT_PRDT_CD' 형식")
                        continue
                    name, cano, acnt_prdt_cd = parts
                    if len(cano) != 8 or not cano.isdigit():
                        logger.warning(f"외부 계좌 CANO 오류 (무시): {name} - 8자리 숫자 필요 (입력: {cano})")
                        continue
                    if len(acnt_prdt_cd) != 2 or not acnt_prdt_cd.isdigit():
                        logger.warning(f"외부 계좌 ACNT_PRDT_CD 오류 (무시): {name} - 2자리 숫자 필요 (입력: {acnt_prdt_cd})")
                        continue
                    self._external_accounts.append((name, cano, acnt_prdt_cd))
                if self._external_accounts:
                    masked = [f"{a[0]}({a[1][:2]}****{a[1][-2:]})" for a in self._external_accounts]
                    logger.info(f"외부 계좌 {len(self._external_accounts)}개 설정: {', '.join(masked)}")

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
            # 에러 알림 발송 (이벤트 루프 존재 시에만)
            try:
                import traceback
                loop = asyncio.get_running_loop()
                loop.create_task(self._send_error_alert(
                    "CRITICAL",
                    "봇 초기화 실패",
                    traceback.format_exc()
                ))
            except RuntimeError:
                pass  # 이벤트 루프 없음 — 무시
            return False

    async def _restore_position_metadata(self, positions: dict):
        """DB에서 포지션 전략/진입시간 복원 (KIS API에는 없는 정보)"""
        if not self.trade_journal or not hasattr(self.trade_journal, 'pool') or not self.trade_journal.pool:
            return
        try:
            rows = await self.trade_journal.pool.fetch(
                "SELECT symbol, entry_strategy, entry_time FROM trades "
                "WHERE exit_time IS NULL OR exit_quantity < entry_quantity "
                "ORDER BY entry_time DESC"
            )
            meta = {}
            for r in rows:
                sym = r['symbol']
                if sym not in meta:  # 최신 거래 우선
                    meta[sym] = (r['entry_strategy'], r['entry_time'])
            restored = 0
            for sym, pos in positions.items():
                if sym in meta:
                    strategy, entry_time = meta[sym]
                    if not pos.strategy and strategy:
                        pos.strategy = strategy
                    if not pos.entry_time and entry_time:
                        pos.entry_time = entry_time
                    restored += 1
            if restored:
                logger.info(f"[메타복원] {restored}개 포지션 전략/진입시간 DB 복원")
        except Exception as e:
            logger.warning(f"[메타복원] DB 조회 실패: {e}")

    async def _load_existing_positions(self):
        """기존 보유 종목 로드 (KIS API에서)"""
        if not self.broker:
            return

        try:
            positions = await self.broker.get_positions()

            if positions:
                logger.info(f"기존 보유 종목 {len(positions)}개 로드")

                # DB에서 전략/진입시간 복원
                await self._restore_position_metadata(positions)

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

                    # 섹터 세팅
                    position.sector = await self._get_sector(symbol)

                    # 수익률 계산
                    if position.avg_price > 0:
                        pnl_pct = float((position.current_price - position.avg_price) / position.avg_price * 100)
                        logger.info(
                            f"  - {symbol}: {position.quantity}주 @ {position.avg_price:,.0f}원 "
                            f"(현재가: {position.current_price:,.0f}원, 수익률: {pnl_pct:+.2f}%)"
                        )

                    # 분할 익절 관리자에 등록 (ATR 기반 동적 손절)
                    if self.exit_manager:
                        price_history = self._get_price_history_for_atr(symbol)
                        self.exit_manager.register_position(position, price_history=price_history)

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
            # 프리마켓(NXT) 데이터 수집 (08:00~08:50)
            now = datetime.now()
            time_val = now.hour * 100 + now.minute
            if 800 <= time_val < 850:
                prev = float(event.prev_close) if event.prev_close and event.prev_close > 0 else 0
                cur = float(event.close) if event.close and event.close > 0 else 0
                if prev > 0 and cur > 0:
                    change_pct = (cur - prev) / prev * 100
                    existing = self.engine.premarket_data.get(event.symbol, {})
                    self.engine.premarket_data[event.symbol] = {
                        "prev_close": prev,
                        "pre_price": cur,
                        "pre_change_pct": change_pct,
                        "pre_volume": getattr(event, 'volume', 0) or 0,
                        "pre_high": max(cur, existing.get("pre_high", 0)),
                        "pre_low": min(cur, existing.get("pre_low", cur)) if existing.get("pre_low", 0) > 0 else cur,
                        "updated_at": now.isoformat(),
                    }

            # 엔진 이벤트 큐에 전달
            await self.engine.emit(event)

            # 분할 익절 체크 (보유 종목에 대해)
            if self.exit_manager and event.symbol in self.engine.portfolio.positions:
                await self._check_exit_signal(event.symbol, event.close)
            elif self.exit_manager and not hasattr(self, '_exit_check_logged'):
                # 첫 시세 수신 시 한 번만 로그 (디버깅용)
                self._exit_check_logged = True
                logger.debug(
                    f"[청산 체크] 보유 종목: {list(self.engine.portfolio.positions.keys())}, "
                    f"시세 수신 종목 예시: {event.symbol}"
                )

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
            # stale pending 클린업 (장중 3분 / 장전 30분 이상 체결 미확인 시 양쪽 pending 모두 해제)
            if self._exit_pending_timestamps:
                now_time = datetime.now()
                # 장전(~09:00)에는 체결 자체가 불가능 → 장 시작까지 대기 (30분)
                stale_minutes = 30 if now_time.hour < 9 else 3
                stale_cutoff = now_time - timedelta(minutes=stale_minutes)
                stale = [s for s, t in self._exit_pending_timestamps.items() if t < stale_cutoff]
                for s in stale:
                    # KIS 미체결 주문 먼저 취소 (살아있는 주문 → 장 시작 시 중복 체결 방지)
                    if self.broker and hasattr(self.broker, 'cancel_all_for_symbol'):
                        try:
                            cancelled = await self.broker.cancel_all_for_symbol(s)
                            if cancelled:
                                logger.info(f"[청산 pending] {s} KIS 주문 {cancelled}건 취소 완료")
                        except Exception as e:
                            logger.warning(f"[청산 pending] {s} KIS 주문 취소 실패: {e}")
                    self._exit_pending_symbols.discard(s)
                    self._exit_pending_timestamps.pop(s, None)
                    # RiskManager pending도 동기화 해제 (매도 영구 차단 방지)
                    if self.engine.risk_manager:
                        await self.engine.risk_manager.clear_pending(s)
                    # ExitManager stage 롤백 (stale timeout = 주문 실패로 간주)
                    if self.exit_manager:
                        self.exit_manager.rollback_stage(s)
                    logger.warning(f"[청산 pending] {s} 타임아웃 해제 ({stale_minutes}분 초과, RiskManager+ExitManager 동기화)")

            # 엔진 RiskManager에서 이미 해제된 종목은 _exit_pending_symbols에서도 동기화 해제
            if self._exit_pending_symbols and self.engine.risk_manager:
                orphaned = [s for s in self._exit_pending_symbols
                            if s not in self.engine.risk_manager._pending_orders]
                for s in orphaned:
                    # KIS 미체결 주문 취소 (고아 pending = KIS에 살아있는 주문 가능)
                    if self.broker and hasattr(self.broker, 'cancel_all_for_symbol'):
                        try:
                            cancelled = await self.broker.cancel_all_for_symbol(s)
                            if cancelled:
                                logger.info(f"[청산 pending] {s} 고아 KIS 주문 {cancelled}건 취소 완료")
                        except Exception:
                            pass
                    self._exit_pending_symbols.discard(s)
                    self._exit_pending_timestamps.pop(s, None)
                    if self.exit_manager:
                        self.exit_manager.rollback_stage(s)
                    logger.warning(f"[청산 pending] {s} 동기화 해제 (RiskManager에 없음 → 고아 pending 정리)")

            # 이미 매도 주문이 진행 중이면 중복 방지 (ExitManager + 전략 SELL 양방향)
            if symbol in self._exit_pending_symbols:
                return
            if self.engine.risk_manager and symbol in self.engine.risk_manager._pending_orders:
                return

            # 청산 실패 블랙리스트 체크 (NXT 거래불가/장운영시간 에러 → 정규장 시작 시 자동 해제)
            if symbol in self._sell_blocked_symbols:
                blocked_at = self._sell_blocked_symbols[symbol]
                now_bl = datetime.now()
                # 정규장(09:00~16:00)이고 블랙리스트 등록이 장 전이면 해제 (정규장에서 재시도)
                if 9 <= now_bl.hour < 16 and blocked_at.hour < 9:
                    del self._sell_blocked_symbols[symbol]
                    logger.info(f"[청산 차단 해제] {symbol} 정규장 시작으로 블랙리스트 해제")
                # 다음 날이면 해제 (날짜가 바뀜)
                elif now_bl.date() > blocked_at.date():
                    del self._sell_blocked_symbols[symbol]
                    logger.info(f"[청산 차단 해제] {symbol} 일자 변경으로 블랙리스트 해제")
                # 장중 5분 경과 시 자동 해제 (수량 초과 등 일시적 차단)
                elif (now_bl - blocked_at).total_seconds() >= 300:
                    del self._sell_blocked_symbols[symbol]
                    logger.info(f"[청산 차단 해제] {symbol} 5분 경과로 블랙리스트 해제")
                else:
                    return  # 블랙리스트 유지, 청산 시도 차단

            # 동시호가 시간대 체크 (15:20~15:30)
            now = datetime.now()
            time_val = now.hour * 100 + now.minute
            is_auction = 1520 <= time_val < 1530

            # 정규장 종료(15:20) 이후 모든 청산 차단
            # 넥스트장 개별 종목 거래가능 여부 판별이 불완전하므로, 안전하게 정규장만 허용
            # 동시호가(15:20~15:30)는 is_auction=True로 LIMIT 주문 허용
            if time_val >= 1520 and not is_auction:
                return

            # 청산 신호 확인
            exit_signal = self.exit_manager.update_price(symbol, current_price)

            # 디버깅: 주기적으로 상태 로그 (5분마다)
            if not hasattr(self, '_last_exit_status_log'):
                self._last_exit_status_log = {}
            last_log = self._last_exit_status_log.get(symbol, datetime.min)
            if (datetime.now() - last_log).total_seconds() >= 300:  # 5분
                state = self.exit_manager.get_state(symbol)
                if state:
                    pos = self.engine.portfolio.positions.get(symbol)
                    if pos:
                        pnl_pct = float((current_price - pos.avg_price) / pos.avg_price * 100) if pos.avg_price > 0 else 0
                        logger.debug(
                            f"[청산 상태] {symbol}: 수익률={pnl_pct:+.2f}%, "
                            f"손절={state.dynamic_stop_pct or state.stop_loss_pct or 3.0:.2f}%, "
                            f"단계={state.current_stage.value}"
                        )
                self._last_exit_status_log[symbol] = datetime.now()

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

                # 청산 사유 저장 (체결 시 journal에 전달하기 위해)
                self._exit_reasons[symbol] = reason

                # ExitManager 전용 pending 선등록 (중복 매도 방지, submit await 중 race condition 차단)
                self._exit_pending_symbols.add(symbol)
                self._exit_pending_timestamps[symbol] = datetime.now()
                # 엔진 RiskManager에도 pending 등록 (전략 SELL 신호 중복 차단용)
                if self.engine.risk_manager:
                    async with self.engine.risk_manager._pending_lock:
                        self.engine.risk_manager._pending_orders.add(symbol)
                        self.engine.risk_manager._pending_timestamps[symbol] = datetime.now()
                        self.engine.risk_manager._pending_sides[symbol] = OrderSide.SELL
                        self.engine.risk_manager._pending_quantities[symbol] = quantity

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

                    # 대시보드 이벤트 로그에 청산 주문 추가
                    name = self.engine._get_stock_name(symbol)
                    self.engine.push_dashboard_event("주문", f"{name} 매도 {quantity}주 ({reason})")

                    # 청산(손절/본전이탈/트레일링) 시 RiskManager에 기록 (재진입 방지)
                    is_loss_exit = ("손절" in reason or "본전 이탈" in reason or "트레일링" in reason)
                    if is_loss_exit and self.engine.risk_manager:
                        if hasattr(self.engine.risk_manager, '_stop_loss_today'):
                            self.engine.risk_manager._stop_loss_today.add(symbol)
                            logger.info(f"[재진입금지] {symbol} 당일 재진입 차단 (사유: {reason})")

                    trading_logger.log_order(
                        symbol=symbol,
                        side="SELL",
                        quantity=quantity,
                        price=float(current_price),
                        order_type=order_type_str,
                        status=f"submitted ({reason})"
                    )
                else:
                    # 주문 실패 시 양쪽 pending 해제
                    self._exit_pending_symbols.discard(symbol)
                    self._exit_pending_timestamps.pop(symbol, None)
                    if self.engine.risk_manager:
                        await self.engine.risk_manager.clear_pending(symbol)
                    # ExitManager stage 롤백 (stage만 올라가고 매도 안 된 상태 방지)
                    if self.exit_manager:
                        self.exit_manager.rollback_stage(symbol)

                    # 비재시도 에러 판별: NXT 거래불가 / 장운영시간 아님
                    result_str = str(result)
                    non_retryable = (
                        "NXT" in result_str
                        or "거래 불가" in result_str
                        or "장운영시간" in result_str
                        or "운영시간" in result_str
                    )

                    if non_retryable:
                        # 블랙리스트 등록 (정규장까지 재시도 차단)
                        self._sell_blocked_symbols[symbol] = datetime.now()
                        logger.warning(
                            f"[청산 차단] {symbol} 블랙리스트 등록: {result} "
                            f"(정규장 시작 시 자동 해제)"
                        )
                        return  # 엔진 일시정지 하지 않음

                    # ---- 수량 초과 에러: 실제 보유수량 보정 후 재시도 ----
                    if "수량을 초과" in result_str or "APBK0400" in result_str:
                        logger.warning(
                            f"[수량 보정] {symbol} 주문수량({quantity}주) 초과 → 실제 보유수량 확인 중..."
                        )
                        try:
                            actual_positions = await self.broker.get_positions()
                            actual_pos = actual_positions.get(symbol)
                            actual_qty = actual_pos.quantity if actual_pos else 0

                            if actual_qty == 0:
                                # 유령 포지션 제거
                                logger.warning(
                                    f"[유령 포지션 제거] {symbol} - 실제 보유 0주 (수량 초과 후 확인)"
                                )
                                if symbol in self.engine.portfolio.positions:
                                    del self.engine.portfolio.positions[symbol]
                                if self.exit_manager:
                                    self.exit_manager.remove_position(symbol)
                                logger.info(f"[포지션 정리 완료] {symbol} 유령 포지션 제거됨")
                                return

                            elif actual_qty < quantity:
                                # 수량 보정: 포트폴리오 + ExitManager 동기화
                                logger.warning(
                                    f"[수량 보정] {symbol} 봇={quantity}주 → 실제={actual_qty}주, "
                                    f"포트폴리오/ExitManager 동기화"
                                )
                                if symbol in self.engine.portfolio.positions:
                                    self.engine.portfolio.positions[symbol].quantity = actual_qty
                                if self.exit_manager:
                                    em_state = self.exit_manager.get_state(symbol)
                                    if em_state:
                                        em_state.remaining_quantity = actual_qty

                                # 보정된 수량으로 매도 재시도
                                corrected_order = Order(
                                    symbol=symbol,
                                    side=OrderSide.SELL,
                                    order_type=OrderType.LIMIT if is_auction else OrderType.MARKET,
                                    quantity=actual_qty,
                                    price=current_price if is_auction else None,
                                )
                                self._exit_pending_symbols.add(symbol)
                                self._exit_pending_timestamps[symbol] = datetime.now()
                                if self.engine.risk_manager:
                                    async with self.engine.risk_manager._pending_lock:
                                        self.engine.risk_manager._pending_orders.add(symbol)
                                        self.engine.risk_manager._pending_timestamps[symbol] = datetime.now()
                                        self.engine.risk_manager._pending_sides[symbol] = OrderSide.SELL
                                        self.engine.risk_manager._pending_quantities[symbol] = actual_qty

                                retry_ok, retry_result = await self.broker.submit_order(corrected_order)
                                if retry_ok:
                                    logger.info(
                                        f"[수량 보정 성공] {symbol} {actual_qty}주 매도 주문 제출 "
                                        f"(원래 {quantity}주 → {actual_qty}주)"
                                    )
                                    return
                                else:
                                    logger.error(
                                        f"[수량 보정 후 재시도 실패] {symbol} {actual_qty}주: {retry_result}"
                                    )
                                    self._exit_pending_symbols.discard(symbol)
                                    self._exit_pending_timestamps.pop(symbol, None)
                                    if self.engine.risk_manager:
                                        await self.engine.risk_manager.clear_pending(symbol)

                            else:
                                # actual_qty >= quantity인데 수량 초과 → 미체결 주문 존재 가능
                                logger.warning(
                                    f"[미체결 주문 의심] {symbol} 실제={actual_qty}주, "
                                    f"주문시도={quantity}주 → 기존 미체결 매도 주문 취소 시도"
                                )
                                try:
                                    cancelled = await self.broker.cancel_all_for_symbol(symbol)
                                    if cancelled > 0:
                                        logger.info(f"[미체결 취소] {symbol} {cancelled}건 취소 완료")
                                except Exception as ce:
                                    logger.debug(f"[미체결 취소 실패] {symbol}: {ce}")

                        except Exception as e:
                            logger.error(f"[수량 보정 실패] {symbol} 실제 수량 조회 오류: {e}")

                        # 수량 보정 후에도 실패하면 블랙리스트 등록 (무한 루프 방지)
                        self._sell_blocked_symbols[symbol] = datetime.now()
                        logger.warning(
                            f"[청산 차단] {symbol} 수량 초과 반복 → 블랙리스트 등록 "
                            f"(다음 포트폴리오 동기화 시 재시도)"
                        )
                        return  # 엔진 일시정지 하지 않음 (무한 루프 방지)

                    # ---- 기존 로직: 재시도 가능한 에러 → 유령 포지션 확인 + 엔진 일시정지 ----
                    logger.error(f"[청산 주문 실패] {symbol} - {result} (3회 시도 후)")

                    # 청산 실패 시 실제 보유 수량 재확인 (유령 포지션 제거)
                    logger.info(f"[포지션 재확인] {symbol} 실제 보유 수량 조회 중...")
                    try:
                        actual_positions = await self.broker.get_positions()
                        if symbol not in actual_positions or actual_positions[symbol].quantity == 0:
                            logger.warning(
                                f"[유령 포지션 제거] {symbol} - 실제 계좌에 없음 (API 응답 지연 의심)"
                            )
                            if symbol in self.engine.portfolio.positions:
                                del self.engine.portfolio.positions[symbol]
                            if self.exit_manager:
                                self.exit_manager.remove_position(symbol)
                            logger.info(f"[포지션 정리 완료] {symbol} 유령 포지션 제거됨, 엔진 계속 동작")
                            return  # 유령 포지션이므로 엔진 일시정지 스킵
                    except Exception as e:
                        logger.error(f"[포지션 재확인 실패] {symbol}: {e}")

                    # 실제 보유 중인데 청산 실패한 경우만 엔진 일시정지
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
            logger.error(f"청산 신호 처리 오류: {e}", exc_info=True)
            # pending 누수 방지
            self._exit_pending_symbols.discard(symbol)
            self._exit_pending_timestamps.pop(symbol, None)
            if self.engine.risk_manager:
                await self.engine.risk_manager.clear_pending(symbol)

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
                name = self.engine._get_stock_name(order.symbol)
                side_label = '매수' if order.side.value.upper() == 'BUY' else '매도'
                price_str = f" @ {float(order.price):,.0f}원" if order.price else ""
                self.engine.push_dashboard_event("주문", f"{name} {side_label} {order.quantity}주{price_str}")
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
                name = self.engine._get_stock_name(order.symbol)
                self.engine.push_dashboard_event("오류", f"{name} 주문 실패: {result}")
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
                    await self.engine.risk_manager.clear_pending(order.symbol, order_amount)
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
                await self.engine.risk_manager.clear_pending(order.symbol, order_amount)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"주문 제출 오류: {e}")
            if self.engine.risk_manager and order:
                order_amount = (order.price * order.quantity) if order.price and order.quantity else Decimal("0")
                await self.engine.risk_manager.clear_pending(order.symbol, order_amount)

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

        # 리스크 알림 즉시 텔레그램 발송 (CRITICAL)
        await self._send_error_alert(
            "daily_loss_limit",
            f"리스크 경고: {event.alert_type}",
            f"메시지: {event.message}\n조치: {event.action}",
            critical=True,
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

            # 매도 시 update_position 전에 entry_price 캡처 (전량 매도 시 포지션 삭제 대비)
            _pre_sell_entry_price = Decimal("0")
            if fill.side.value.upper() == "SELL":
                pos = self.engine.portfolio.positions.get(fill.symbol)
                if pos:
                    _pre_sell_entry_price = pos.avg_price
                # 인스턴스 변수에도 저장 (_record_trade_to_journal에서 폴백 사용)
                if not hasattr(self, '_last_sell_entry_price'):
                    self._last_sell_entry_price = {}
                self._last_sell_entry_price[fill.symbol] = _pre_sell_entry_price

            # 포트폴리오 업데이트 (동기화 lock으로 보호)
            async with self._portfolio_lock:
                self.engine.update_position(fill)

            # 리스크 통계 업데이트 (대시보드용: can_trade, daily_loss 등)
            if self.risk_manager:
                self.risk_manager.on_fill(event, self.engine.portfolio)

            # 매도 체결 시 연속 손실 추적 (record_trade_result 호출)
            if fill.side.value.upper() == "SELL" and self.risk_manager:
                try:
                    # 체결 시점 PnL 계산 (진입가 기준)
                    entry_price = Decimal("0")
                    if self.trade_journal:
                        open_trades = self.trade_journal.get_open_trades()
                        matching = [t for t in open_trades if t.symbol == fill.symbol]
                        if matching:
                            entry_price = Decimal(str(matching[0].entry_price))
                    if entry_price <= 0:
                        # update_position 전에 캡처한 평균단가 사용
                        entry_price = _pre_sell_entry_price
                    if entry_price > 0:
                        from src.utils.fee_calculator import calculate_net_pnl as _calc_net_pnl
                        net_pnl, _ = _calc_net_pnl(float(entry_price), float(fill.price), fill.quantity)
                        trade_pnl = Decimal(str(net_pnl))
                        self.risk_manager.record_trade_result(trade_pnl)
                except Exception as e:
                    logger.debug(f"거래 결과 기록 실패: {e}")

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

                    # 섹터 세팅
                    if not position.sector:
                        position.sector = await self._get_sector(fill.symbol)

                # 매수 체결 즉시 WS 보유종목 구독 추가 (NXT 시세 포함)
                if self.ws_feed:
                    self.ws_feed.set_priority_symbols(list(self.engine.portfolio.positions.keys()))
                    logger.debug(f"[WS] 매수 체결 → 보유종목 구독 갱신: {fill.symbol} 추가")

            # ExitManager 매도 pending 해제 (체결 확인) + 엔진 RiskManager pending 해제
            if fill.side.value.upper() == "SELL":
                # 매도 체결 시 항상 pending 해제 (부분/전량 무관)
                # → ExitManager의 stage 진행이 동일 단계 중복 익절을 방지하므로 안전
                self._exit_pending_symbols.discard(fill.symbol)
                self._exit_pending_timestamps.pop(fill.symbol, None)
                if self.engine.risk_manager:
                    await self.engine.risk_manager.clear_pending(fill.symbol)

                if fill.symbol not in self.engine.portfolio.positions:
                    # 포지션 완전 종료: WS 구독 해제
                    if self.ws_feed and hasattr(self.ws_feed, '_priority_symbols'):
                        self.ws_feed._priority_symbols.discard(fill.symbol)

                # EXIT 레코드 기록 (진입가/손익/사유)
                # _pre_sell_entry_price: update_position 전에 캡처한 원래 매수 평균가
                try:
                    entry_price = float(_pre_sell_entry_price) if _pre_sell_entry_price else None

                    # 폴백: 저널에서 진입가 조회
                    if not entry_price and self.trade_journal:
                        open_trades = self.trade_journal.get_open_trades()
                        matching = [t for t in open_trades if t.symbol == fill.symbol]
                        if matching:
                            entry_price = matching[0].entry_price
                        else:
                            closed = self.trade_journal.get_closed_trades(days=1)
                            sym_trades = [t for t in closed if t.symbol == fill.symbol]
                            entry_price = sym_trades[-1].entry_price if sym_trades else None

                    # 최종 폴백
                    if not entry_price:
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

                    # TradeJournal 청산 기록은 _record_trade_to_journal()에서 처리
                except Exception as e:
                    logger.warning(f"EXIT 로그 기록 실패: {e}")

            # 분할 익절 관리자 업데이트
            if self.exit_manager:
                if fill.side.value.upper() == "SELL":
                    # 매도 체결 시 상태 업데이트
                    self.exit_manager.on_fill(fill.symbol, fill.quantity, fill.price)
                elif fill.side.value.upper() == "BUY":
                    # 매수 체결 시 새 포지션 등록 (전략별 청산 파라미터 + ATR 전달)
                    position = self.engine.portfolio.positions.get(fill.symbol)
                    if position:
                        strategy_name = self._symbol_strategy.get(fill.symbol, "")
                        exit_params = self._strategy_exit_params.get(strategy_name, {})
                        price_history = self._get_price_history_for_atr(fill.symbol)
                        self.exit_manager.register_position(
                            position,
                            stop_loss_pct=exit_params.get("stop_loss_pct"),
                            trailing_stop_pct=exit_params.get("trailing_stop_pct"),
                            price_history=price_history,
                            first_exit_pct=exit_params.get("first_exit_pct"),
                            second_exit_pct=exit_params.get("second_exit_pct"),
                            third_exit_pct=exit_params.get("third_exit_pct"),
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
                # 캐시에 저장 + 포지션 name 보강
                if stock_name and stock_name != fill.symbol:
                    self.stock_name_cache[fill.symbol] = stock_name
                    pos = self.engine.portfolio.positions.get(fill.symbol)
                    if pos and (not pos.name or pos.name == fill.symbol):
                        pos.name = stock_name

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
                matching.sort(key=lambda t: t.entry_time)  # FIFO: 가장 오래된 거래 우선

                if matching:
                    trade = matching[0]

                    indicators = {}
                    if hasattr(self.strategy_manager, '_indicators'):
                        indicators = self.strategy_manager._indicators.get(fill.symbol, {})

                    # 청산 타입 결정: ExitManager에서 저장한 사유 우선, 없으면 fill에서 추론
                    reason = getattr(fill, 'reason', '') or self._exit_reasons.pop(fill.symbol, '') or ''
                    exit_type = self._infer_exit_type(reason)

                    # exit_reason에 상세 사유 포함
                    if not reason:
                        reason = exit_type

                    # KIS와 일치하도록 포트폴리오 평균단가 사용
                    avg_price = None
                    pos = self.engine.portfolio.positions.get(fill.symbol)
                    if pos and pos.avg_price > 0:
                        avg_price = float(pos.avg_price)
                    elif not pos:
                        # 전량 매도 후 포지션 삭제됨 → _on_fill에서 캡처한 진입가 사용
                        cached = getattr(self, '_last_sell_entry_price', {})
                        pre_price = cached.get(fill.symbol, Decimal("0"))
                        if pre_price > 0:
                            avg_price = float(pre_price)
                            logger.debug(
                                f"[저널] {fill.symbol} 포지션 삭제됨 → 캡처된 평균단가 {avg_price:,.0f}원 사용"
                            )
                        else:
                            logger.debug(
                                f"[저널] {fill.symbol} 포지션 없음 (전량 매도 후 삭제?) "
                                f"→ 개별 진입가로 PnL 계산"
                            )

                    self.trade_journal.record_exit(
                        trade_id=trade.id,
                        exit_price=float(fill.price),
                        exit_quantity=fill.quantity,
                        exit_reason=reason,
                        exit_type=exit_type,
                        indicators=indicators,
                        avg_entry_price=avg_price,
                    )
                else:
                    logger.warning(
                        f"[저널] 매도 체결 매칭 실패: {fill.symbol} {fill.quantity}주 @ {fill.price:,.0f}원 "
                        f"— 미청산 거래에서 해당 종목 못 찾음 (open_trades={len(open_trades)}건)"
                    )

        except Exception as e:
            logger.warning(f"거래 저널 기록 실패: {e}")

    @staticmethod
    def _infer_exit_type(reason: str) -> str:
        """청산 사유 문자열에서 exit_type 추론"""
        if not reason:
            return "unknown"
        r = reason.lower()
        if "손절" in r:
            return "stop_loss"
        if "1차 익절" in r or "1차익절" in r:
            return "first_take_profit"
        if "2차 익절" in r or "2차익절" in r:
            return "second_take_profit"
        if "3차 익절" in r or "3차익절" in r:
            return "third_take_profit"
        if "익절" in r:
            return "take_profit"
        if "트레일링" in r:
            return "trailing"
        if "본전" in r:
            return "breakeven"
        if "시간" in r or "보유기간" in r or "종료" in r:
            return "time_exit"
        if "동기화" in r:
            return "kis_sync"
        return "manual"

    async def _daily_summary(self):
        """일일 요약"""
        portfolio = self.engine.portfolio
        stats = self.engine.stats

        daily_pnl_val = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        pnl_pct = float(daily_pnl_val / portfolio.total_equity * 100) if portfolio.total_equity > 0 else 0.0

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

        # PID 파일 생성 (프로세스 중복 방지)
        write_pid_file()

        logger.info("=== 트레이딩 봇 시작 ===")

        try:
            # 태스크 생성
            tasks = []

            # 1. 메인 엔진 실행
            tasks.append(asyncio.create_task(self.engine.run(), name="engine"))

            # 2. WebSocket 피드 실행 (보유종목 실시간 시세 — NXT 포함)
            if self.ws_feed:
                # 보유 종목을 우선순위로 설정 (항상 구독)
                if self.engine.portfolio.positions:
                    self.ws_feed.set_priority_symbols(list(self.engine.portfolio.positions.keys()))
                    logger.info(f"[WS] 보유 종목 {len(self.engine.portfolio.positions)}개 우선 구독 설정")
                tasks.append(asyncio.create_task(self._run_ws_feed(), name="ws_feed"))

            # 2-1. REST 시세 피드 (WS와 병행 — 스크리닝 종목 시세용)
            if self.broker:
                tasks.append(asyncio.create_task(self._run_rest_price_feed(), name="rest_price_feed"))

            # 3. 테마 탐지 루프 실행
            if self.theme_detector:
                tasks.append(asyncio.create_task(self._run_theme_detection(), name="theme_detector"))

            # 4. 체결 확인 루프 실행 (실시간 모드)
            if self.broker:
                tasks.append(asyncio.create_task(self._run_fill_check(), name="fill_checker"))

            # 4-1. 포트폴리오 동기화 루프 (2분마다 KIS API와 동기화)
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

            # 7. 주간 전략 예산 리밸런싱 (자가진화 파라미터 자동변경은 비활성화)
            if self.strategy_evolver:
                # evolution_scheduler (LLM 자동 파라미터 튜닝) 제거 — 수동 운영
                tasks.append(asyncio.create_task(
                    self._run_weekly_rebalance_scheduler(), name="weekly_rebalance"
                ))

            # 8. 로그/캐시 정리 스케줄러
            tasks.append(asyncio.create_task(self._run_log_cleanup(), name="log_cleanup"))

            # 9. 종목 마스터 갱신 스케줄러
            if self.stock_master:
                tasks.append(asyncio.create_task(
                    self._run_stock_master_refresh(), name="stock_master_refresh"
                ))

            # 10. 일봉 데이터 갱신 스케줄러
            if self.broker:
                tasks.append(asyncio.create_task(
                    self._run_daily_candle_refresh(), name="daily_candle_refresh"
                ))

            # 10-3. 배치 분석 스케줄러 (스윙 모멘텀)
            if self.batch_analyzer:
                tasks.append(asyncio.create_task(
                    self._run_batch_scheduler(), name="batch_scheduler"
                ))

            # 11. 헬스 모니터
            if self.health_monitor:
                tasks.append(asyncio.create_task(
                    self._run_health_monitor(), name="health_monitor"
                ))

            # 12. 대시보드 서버 실행
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
        """WebSocket 피드 실행 (보유종목 실시간 시세 — NXT 세션 포함)"""
        try:
            # 연결
            if await self.ws_feed.connect():
                # NXT 거래 가능 종목 로드 (프리장/넥스트장용)
                await self._load_nxt_symbols()

                # 현재 세션 설정
                current_session = self._get_current_session()
                self.ws_feed.set_session(current_session)

                # 보유종목만 구독 (REST 폴링이 스크리닝 종목 담당)
                position_symbols = list(self.engine.portfolio.positions.keys())
                if position_symbols:
                    await self.ws_feed.subscribe(position_symbols)
                    stats = self.ws_feed.get_subscription_stats()
                    logger.info(
                        f"[WS] 보유종목 구독: {stats['subscribed_count']}개, "
                        f"세션={current_session.value}"
                    )
                else:
                    logger.info("[WS] 보유종목 없음 — 대기 (매수 체결 시 자동 구독)")

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

            # 없으면 설정 파일의 기본 목록 사용
            if not nxt_symbols:
                nxt_symbols = self.config.get("nxt_default_symbols") or []
                if nxt_symbols:
                    logger.info(f"NXT 기본 종목 목록 사용 (설정 파일): {len(nxt_symbols)}개")
                else:
                    logger.warning("NXT 기본 종목 목록 없음")

            if self.ws_feed:
                self.ws_feed.set_nxt_symbols(nxt_symbols)

        except Exception as e:
            logger.warning(f"NXT 종목 로드 실패: {e}")

    def _get_price_history_for_atr(self, symbol: str) -> Optional[Dict[str, List[Decimal]]]:
        """
        ATR 계산용 히스토리 데이터 가져오기 (전략에 로드된 일봉 활용)

        Returns:
            {"high": [...], "low": [...], "close": [...]} 또는 None
        """
        try:
            sm = self.strategy_manager or getattr(self.engine, 'strategy_manager', None)
            if not sm:
                return None

            # 첫 번째 전략의 히스토리에서 데이터 조회
            for strategy in sm.strategies.values():
                history = strategy._price_history.get(symbol)
                if history and len(history) >= 15:
                    # 최신 → 과거 순서 (ATR 계산 함수 요구사항)
                    bars_sorted = sorted(history[-20:], key=lambda x: x.timestamp, reverse=True)
                    return {
                        "high": [bar.high for bar in bars_sorted],
                        "low": [bar.low for bar in bars_sorted],
                        "close": [bar.close for bar in bars_sorted],
                    }

            return None
        except Exception as e:
            logger.debug(f"[ATR] {symbol} 히스토리 데이터 로드 실패: {e}")
            return None

    def _get_current_session(self) -> MarketSession:
        """현재 시간 기반 세션 판단 (SessionUtil 사용)"""
        return SessionUtil.get_current_session()

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
        if hasattr(self, 'ws_feed') and self.ws_feed:
            self.ws_feed._running = False

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
            if self.trade_journal and hasattr(self.trade_journal, 'disconnect'):
                await self.trade_journal.disconnect()
                logger.info("TradeStorage DB 종료")
        except Exception as e:
            logger.error(f"TradeStorage 종료 실패: {e}")

        try:
            if self.batch_analyzer:
                logger.info("배치 분석기 종료")
        except Exception as e:
            logger.error(f"배치 분석기 종료 실패: {e}")

        try:
            if self._mcp_manager:
                await self._mcp_manager.shutdown()
                logger.info("MCP 서버 클라이언트 종료")
        except Exception as e:
            logger.error(f"MCP 클라이언트 종료 실패: {e}")

        # 미체결 주문 전량 취소 (RiskManager pending + ExitManager pending + 보유 종목 합집합)
        try:
            if self.broker and hasattr(self.broker, 'cancel_all_for_symbol'):
                cancel_targets = set(self.engine.portfolio.positions.keys())
                if self.engine.risk_manager:
                    cancel_targets |= set(self.engine.risk_manager._pending_orders)
                cancel_targets |= self._exit_pending_symbols
                cancelled_total = 0
                for sym in cancel_targets:
                    try:
                        cnt = await self.broker.cancel_all_for_symbol(sym)
                        cancelled_total += cnt
                    except Exception:
                        pass
                if cancelled_total > 0:
                    logger.info(f"미체결 주문 {cancelled_total}건 취소 완료")
        except Exception as e:
            logger.error(f"미체결 주문 취소 실패: {e}")

        try:
            if self.broker:
                await self.broker.disconnect()
        except Exception as e:
            logger.error(f"브로커 연결 해제 실패: {e}")

        # PID 파일 제거
        remove_pid_file()

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

    # 프로세스 중복 체크 (flock + 기존 프로세스 자동 종료)
    if not acquire_singleton_lock():
        logger.error("싱글톤 락 획득 실패. 종료합니다.")
        sys.exit(1)

    # 봇 실행
    bot = TradingBot(config, dry_run=args.dry_run)
    try:
        await bot.run()
    finally:
        # 비정상 종료 시에도 락 + PID 파일 정리
        release_singleton_lock()


if __name__ == "__main__":
    asyncio.run(main())
