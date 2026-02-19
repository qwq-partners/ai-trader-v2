"""
AI Trading Bot v2 - 대시보드 데이터 수집기

봇의 런타임 데이터를 JSON 변환하여 API/SSE에 제공합니다.
"""

import json
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    from pykrx import stock as pykrx_stock
    PYKRX_AVAILABLE = True
except ImportError:
    PYKRX_AVAILABLE = False
    logger.warning("pykrx not available - stock names will not be enriched")


def _serialize(data: Any) -> Any:
    """재귀적으로 Decimal을 float로 변환"""
    if isinstance(data, dict):
        return {k: _serialize(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_serialize(item) for item in data]
    if isinstance(data, Decimal):
        return float(data)
    if isinstance(data, datetime):
        return data.isoformat()
    return data


class DashboardDataCollector:
    """봇 런타임 데이터를 JSON으로 변환"""

    # 클래스 레벨 종목 마스터 캐시 (전체 종목명)
    _stock_master_cache: Dict[str, str] = {}
    _stock_master_loaded: bool = False

    def __init__(self, bot):
        self.bot = bot
        self._name_cache: Dict[str, str] = {}
        self._name_cache_updated: Optional[datetime] = None
        # 외부 계좌 캐시
        self._ext_accounts_cache: Optional[list] = None
        self._ext_accounts_cache_ts: Optional[datetime] = None
        import asyncio
        self._ext_accounts_lock = asyncio.Lock()

    # ----------------------------------------------------------
    # 시스템 상태
    # ----------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """봇 상태 정보"""
        bot = self.bot
        engine = bot.engine

        # 현재 세션
        session = bot._get_current_session().value

        # WS 피드 상태
        ws_stats = {}
        if bot.ws_feed:
            ws_stats = bot.ws_feed.get_stats()

        return _serialize({
            "running": bot.running,
            "session": session,
            "uptime_seconds": engine.stats.uptime_seconds,
            "engine": {
                "events_processed": engine.stats.events_processed,
                "signals_generated": engine.stats.signals_generated,
                "orders_submitted": engine.stats.orders_submitted,
                "orders_filled": engine.stats.orders_filled,
                "errors_count": engine.stats.errors_count,
                "paused": engine.paused,
            },
            "websocket": {
                "connected": ws_stats.get("connected", False),
                "subscribed_count": ws_stats.get("subscribed_count", 0),
                "message_count": ws_stats.get("message_count", 0),
                "last_message_time": ws_stats.get("last_message_time"),
            },
            "watch_symbols_count": len(bot._watch_symbols),
            "timestamp": datetime.now(),
        })

    # ----------------------------------------------------------
    # 포트폴리오
    # ----------------------------------------------------------

    def get_portfolio(self) -> Dict[str, Any]:
        """포트폴리오 정보 (실효 일일 손익 = 실현 + 미실현)"""
        portfolio = self.bot.engine.portfolio
        effective_pnl = portfolio.effective_daily_pnl

        return _serialize({
            "cash": portfolio.cash,
            "total_position_value": portfolio.total_position_value,
            "total_equity": portfolio.total_equity,
            "initial_capital": portfolio.initial_capital,
            "total_pnl": portfolio.total_pnl,
            "total_pnl_pct": portfolio.total_pnl_pct,
            "daily_pnl": effective_pnl,
            "realized_daily_pnl": portfolio.daily_pnl,
            "unrealized_pnl": portfolio.total_unrealized_pnl,
            "daily_pnl_pct": (
                float(effective_pnl / portfolio.initial_capital * 100)
                if portfolio.initial_capital > 0 else 0.0
            ),
            "daily_trades": portfolio.daily_trades,
            "cash_ratio": portfolio.cash_ratio,
            "position_count": len(portfolio.positions),
            "timestamp": datetime.now(),
        })

    # ----------------------------------------------------------
    # 포지션
    # ----------------------------------------------------------

    def get_positions(self) -> List[Dict[str, Any]]:
        """보유 포지션 목록"""
        portfolio = self.bot.engine.portfolio
        exit_mgr = self.bot.exit_manager
        name_cache = self._build_name_cache()
        positions = []

        for symbol, pos in portfolio.positions.items():
            exit_state = None
            if exit_mgr:
                state = exit_mgr.get_state(symbol)
                if state:
                    exit_state = {
                        "stage": state.current_stage.value,
                        "original_quantity": state.original_quantity,
                        "remaining_quantity": state.remaining_quantity,
                        "highest_price": state.highest_price,
                        "realized_pnl": state.total_realized_pnl,
                    }

            # 종목명: pos.name → name_cache → symbol 순서 폴백
            pos_name = getattr(pos, 'name', '') or ''
            if not pos_name or pos_name == symbol:
                pos_name = name_cache.get(symbol, symbol)
            positions.append(_serialize({
                "symbol": symbol,
                "name": pos_name,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": pos.current_price,
                "market_value": pos.market_value,
                "cost_basis": pos.cost_basis,
                "unrealized_pnl": pos.unrealized_pnl,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct,
                "strategy": pos.strategy,
                "entry_time": pos.entry_time,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "highest_price": pos.highest_price,
                "exit_state": exit_state,
            }))

        return positions

    # ----------------------------------------------------------
    # 리스크
    # ----------------------------------------------------------

    def get_risk(self) -> Dict[str, Any]:
        """리스크 지표 (실효 일일 손익 기준)"""
        engine = self.bot.engine
        risk_mgr = self.bot.risk_manager
        portfolio = engine.portfolio

        effective_pnl = portfolio.effective_daily_pnl
        daily_loss_pct = (
            float(effective_pnl / portfolio.initial_capital * 100)
            if portfolio.initial_capital > 0 else 0.0
        )

        config = engine.config.risk

        # 동적 max_positions 계산 (flex 포함) — 엔진의 단일 구현 사용
        effective_max = engine.get_effective_max_positions() if hasattr(engine, 'get_effective_max_positions') else config.max_positions

        result = {
            "can_trade": True,
            "daily_loss_pct": daily_loss_pct,
            "daily_loss_limit_pct": config.daily_max_loss_pct,
            "daily_trades": portfolio.daily_trades,
            "daily_max_trades": config.daily_max_trades,
            "position_count": len(portfolio.positions),
            "max_positions": effective_max,
            "config_max_positions": config.max_positions,
            "consecutive_losses": 0,
            "timestamp": datetime.now(),
        }

        if risk_mgr:
            result["can_trade"] = risk_mgr.metrics.can_trade
            result["consecutive_losses"] = risk_mgr.daily_stats.consecutive_losses

        return _serialize(result)

    # ----------------------------------------------------------
    # 거래 내역
    # ----------------------------------------------------------

    @classmethod
    def _load_stock_master(cls) -> None:
        """종목 마스터 로드 (클래스 레벨, 1회만 실행)"""
        if cls._stock_master_loaded or not PYKRX_AVAILABLE:
            return

        try:
            logger.info("Loading stock master from pykrx...")

            # KOSPI + KOSDAQ + KONEX 전체 종목 로드
            for market in ["KOSPI", "KOSDAQ", "KONEX"]:
                try:
                    tickers = pykrx_stock.get_market_ticker_list(market=market)
                    for ticker in tickers:
                        if ticker not in cls._stock_master_cache:
                            name = pykrx_stock.get_market_ticker_name(ticker)
                            if name:
                                cls._stock_master_cache[ticker] = name
                except Exception as e:
                    logger.warning(f"Failed to load {market} tickers: {e}")
                    continue

            cls._stock_master_loaded = True
            logger.info(f"Stock master loaded: {len(cls._stock_master_cache)} stocks")

        except Exception as e:
            logger.error(f"Failed to load stock master: {e}")

    def _build_name_cache(self) -> Dict[str, str]:
        """종목명 캐시 구축 (60초 TTL, 봇 캐시 + 포지션 + 스크리너 + pykrx 마스터)"""
        now = datetime.now()
        if self._name_cache_updated and (now - self._name_cache_updated).total_seconds() < 60:
            return self._name_cache

        # 종목 마스터 로드 (1회만)
        if not self._stock_master_loaded:
            self._load_stock_master()

        cache: Dict[str, str] = {}

        # 1. pykrx 종목 마스터 (가장 기본)
        cache.update(self._stock_master_cache)

        # 2. 봇 레벨 캐시 (우선순위 높음)
        bot_cache = getattr(self.bot, 'stock_name_cache', {})
        cache.update(bot_cache)

        # 3. 포지션에서 종목명
        portfolio = self.bot.engine.portfolio
        for symbol, pos in portfolio.positions.items():
            if symbol in cache:
                continue
            name = getattr(pos, 'name', '')
            if name and name != symbol:
                cache[symbol] = name

        # 4. 스크리너에서 종목명
        screener = self.bot.screener
        if screener:
            for stock in getattr(screener, '_last_screened', []):
                if stock.symbol not in cache and stock.name and stock.name != stock.symbol:
                    cache[stock.symbol] = stock.name

        self._name_cache = cache
        self._name_cache_updated = now
        return cache

    def _enrich_trades(self, trades) -> List[Dict[str, Any]]:
        """거래 데이터에 현재 포지션 정보 보강"""
        portfolio = self.bot.engine.portfolio
        name_cache = self._build_name_cache()
        now = datetime.now()
        result = []

        for t in trades:
            d = _serialize(t.to_dict())

            # 종목명 보강: 저널에 코드만 저장된 경우 캐시에서 가져오기
            if not d.get('name') or d['name'] == d['symbol']:
                cached_name = name_cache.get(d['symbol'])
                if cached_name:
                    d['name'] = cached_name

            # 전략 보강: 빈 문자열 또는 unknown이면 제거
            if d.get('entry_strategy') in ('unknown', ''):
                d['entry_strategy'] = ''

            # 미청산 거래: 현재가/손익/보유시간 실시간 계산
            if not d.get('exit_time'):
                pos = portfolio.positions.get(d['symbol'])
                if pos:
                    d['current_price'] = float(pos.current_price)
                    entry_price = d.get('entry_price', 0)
                    qty = d.get('entry_quantity', 0)
                    if entry_price and qty:
                        d['pnl'] = float(pos.current_price - pos.avg_price) * qty
                        d['pnl_pct'] = float(
                            (pos.current_price - pos.avg_price)
                            / pos.avg_price * 100
                        ) if pos.avg_price else 0

                # 보유시간 계산
                entry_time = d.get('entry_time')
                if entry_time:
                    if isinstance(entry_time, str):
                        entry_dt = datetime.fromisoformat(entry_time)
                    else:
                        entry_dt = entry_time
                    d['holding_minutes'] = int(
                        (now - entry_dt).total_seconds() / 60
                    )

            result.append(d)

        return result

    def get_today_trades(self) -> List[Dict[str, Any]]:
        """오늘 거래 목록"""
        journal = self.bot.trade_journal
        if not journal:
            return []

        trades = journal.get_today_trades()
        return self._enrich_trades(trades)

    def get_trades_by_date(self, trade_date: date) -> List[Dict[str, Any]]:
        """날짜별 거래 목록"""
        journal = self.bot.trade_journal
        if not journal:
            return []

        trades = journal.get_trades_by_date(trade_date)
        return self._enrich_trades(trades)

    def get_trade_stats(self, days: int = 30) -> Dict[str, Any]:
        """거래 통계 (미청산 거래 포함)"""
        journal = self.bot.trade_journal
        if not journal:
            return {"total_trades": 0}

        stats = _serialize(journal.get_statistics(days))

        # 미청산 거래 정보 추가
        open_trades = journal.get_open_trades()
        if open_trades:
            enriched = self._enrich_trades(open_trades)
            open_pnl = sum(t.get('pnl', 0) for t in enriched)
            open_pnl_pcts = [t.get('pnl_pct', 0) for t in enriched if t.get('pnl_pct', 0) != 0]
            stats['open_trades'] = len(open_trades)
            stats['open_pnl'] = open_pnl
            stats['open_avg_pnl_pct'] = (
                sum(open_pnl_pcts) / len(open_pnl_pcts)
                if open_pnl_pcts else 0
            )
            # 전체 거래 수 (청산 + 미청산)
            stats['all_trades'] = stats.get('total_trades', 0) + len(open_trades)

            # 미청산 거래의 전략별 통계 추가
            by_strategy = stats.get('by_strategy', {})
            for t in enriched:
                strategy = t.get('entry_strategy') or 'unknown'
                if strategy not in by_strategy:
                    by_strategy[strategy] = {
                        'trades': 0, 'wins': 0, 'total_pnl': 0, 'win_rate': 0
                    }
                by_strategy[strategy]['trades'] += 1
                by_strategy[strategy]['total_pnl'] += t.get('pnl', 0)
                # 미청산은 수익 중이면 wins로 카운트 (참고용)
                if t.get('pnl', 0) > 0:
                    by_strategy[strategy]['wins'] += 1
                # 승률 재계산
                by_strategy[strategy]['win_rate'] = (
                    by_strategy[strategy]['wins'] / by_strategy[strategy]['trades'] * 100
                )
            stats['by_strategy'] = by_strategy
        else:
            stats['open_trades'] = 0
            stats['open_pnl'] = 0
            stats['open_avg_pnl_pct'] = 0
            stats['all_trades'] = stats.get('total_trades', 0)

        return stats

    async def get_trade_events(
        self, target_date: date = None, event_type: str = "all"
    ) -> List[Dict[str, Any]]:
        """거래 이벤트 로그 (trade_events 테이블 기반, 폴백: 캐시)"""
        journal = self.bot.trade_journal
        if not journal:
            return []

        # TradeStorage인 경우 DB 쿼리
        if hasattr(journal, 'get_trade_events'):
            events = await journal.get_trade_events(target_date, event_type)
        else:
            # 기존 TradeJournal 폴백: 캐시에서 이벤트 구성
            target_date = target_date or date.today()
            events = []
            trades = journal.get_trades_by_date(target_date)
            for t in trades:
                if event_type in ("all", "buy"):
                    events.append({
                        "trade_id": t.id, "symbol": t.symbol, "name": t.name,
                        "event_type": "BUY", "event_time": t.entry_time.isoformat() if t.entry_time else "",
                        "price": float(t.entry_price), "quantity": t.entry_quantity,
                        "strategy": t.entry_strategy, "signal_score": float(t.entry_signal_score),
                        "status": t.exit_type if t.is_closed else "holding",
                        "entry_price": float(t.entry_price), "entry_quantity": t.entry_quantity,
                    })
                if t.exit_time and t.exit_time.date() == target_date and event_type in ("all", "sell"):
                    events.append({
                        "trade_id": t.id, "symbol": t.symbol, "name": t.name,
                        "event_type": "SELL", "event_time": t.exit_time.isoformat(),
                        "price": float(t.exit_price), "quantity": t.exit_quantity or 0,
                        "exit_type": t.exit_type, "exit_reason": t.exit_reason,
                        "pnl": float(t.pnl), "pnl_pct": float(t.pnl_pct),
                        "strategy": t.entry_strategy, "signal_score": float(t.entry_signal_score),
                        "status": t.exit_type or "closed",
                        "entry_price": float(t.entry_price), "entry_quantity": t.entry_quantity,
                    })
            events.sort(key=lambda e: e.get("event_time", ""), reverse=True)

        # 종목명 + 미청산 BUY 현재가 보강
        portfolio = self.bot.engine.portfolio
        name_cache = self._build_name_cache()
        for ev in events:
            # 종목명 보강: DB에 코드만 저장된 경우
            sym = ev.get("symbol", "")
            ev_name = ev.get("name", "")
            if not ev_name or ev_name == sym:
                ev["name"] = name_cache.get(sym, sym)

            # 미청산 BUY: 현재가/평가손익 보강
            if ev.get("event_type") == "BUY" and ev.get("status") == "holding":
                pos = portfolio.positions.get(sym)
                if pos:
                    ev["current_price"] = float(pos.current_price)
                    entry_p = ev.get("entry_price") or ev.get("price", 0)
                    qty = ev.get("entry_quantity") or ev.get("quantity", 0)
                    if entry_p and qty:
                        ev["pnl"] = float(pos.current_price - pos.avg_price) * qty
                        ev["pnl_pct"] = float(
                            (pos.current_price - pos.avg_price) / pos.avg_price * 100
                        ) if pos.avg_price else 0

        return events

    # ----------------------------------------------------------
    # 테마 / 스크리닝
    # ----------------------------------------------------------

    def get_themes(self) -> List[Dict[str, Any]]:
        """활성 테마 목록"""
        detector = self.bot.theme_detector
        if not detector:
            return []

        # 종목명 캐시 구축
        name_cache = self._build_name_cache()

        themes = []
        raw_themes = getattr(detector, '_themes', {})
        if isinstance(raw_themes, dict):
            raw_themes = raw_themes.values()

        for theme in raw_themes:
            # 관련종목을 종목명으로 변환 (코드 → 종목명)
            related_stocks_with_names = []
            for symbol in theme.related_stocks:
                name = name_cache.get(symbol, symbol)
                # 종목명이 있으면 종목명만, 없으면 코드만 표시
                related_stocks_with_names.append(name)

            themes.append(_serialize({
                "name": theme.name,
                "keywords": theme.keywords,
                "related_stocks": related_stocks_with_names,
                "score": theme.score,
                "news_count": theme.news_count,
                "detected_at": theme.detected_at,
                "last_updated": getattr(theme, 'last_updated', None),
            }))

        return themes

    def get_screening(self) -> List[Dict[str, Any]]:
        """스크리닝 결과"""
        screener = self.bot.screener
        if not screener:
            return []

        results = []
        seen_symbols = set()
        last_screened = getattr(screener, '_last_screened', [])
        if not last_screened:
            # 캐시에서 가져오기 (중복 제거)
            cache = getattr(screener, '_cache', {})
            for key, stocks in cache.items():
                for stock in stocks:
                    if stock.symbol in seen_symbols:
                        continue
                    seen_symbols.add(stock.symbol)
                    results.append(_serialize({
                        "symbol": stock.symbol,
                        "name": stock.name,
                        "price": stock.price,
                        "change_pct": stock.change_pct,
                        "volume": stock.volume,
                        "volume_ratio": stock.volume_ratio,
                        "score": stock.score,
                        "reasons": stock.reasons,
                        "screened_at": stock.screened_at,
                    }))
        else:
            for stock in last_screened:
                if stock.symbol in seen_symbols:
                    continue
                seen_symbols.add(stock.symbol)
                results.append(_serialize({
                    "symbol": stock.symbol,
                    "name": stock.name,
                    "price": stock.price,
                    "change_pct": stock.change_pct,
                    "volume": stock.volume,
                    "volume_ratio": stock.volume_ratio,
                    "score": stock.score,
                    "reasons": stock.reasons,
                    "screened_at": stock.screened_at,
                }))

        # 점수 내림차순 정렬
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results

    # ----------------------------------------------------------
    # 진화 (Evolution)
    # ----------------------------------------------------------

    def _load_latest_advice(self) -> Optional[Dict]:
        """최신 advice JSON 파일 로드"""
        evolution_dir = Path.home() / ".cache" / "ai_trader" / "evolution"
        if not evolution_dir.exists():
            return None

        advice_files = sorted(evolution_dir.glob("advice_*.json"), reverse=True)
        if not advice_files:
            return None

        try:
            with open(advice_files[0], "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def get_evolution(self) -> Dict[str, Any]:
        """진화 엔진 상태 + 최신 분석 결과 (AS-IS/TO-BE 포맷)"""
        evolver = getattr(self.bot, 'strategy_evolver', None)

        # 기본값
        result: Dict[str, Any] = {
            "summary": {
                "version": 0,
                "total_evolutions": 0,
                "successful_changes": 0,
                "rolled_back_changes": 0,
                "last_evolution": None,
                "assessment": "unknown",
                "confidence": 0,
            },
            "insights": [],
            "parameter_adjustments": [],  # AI 추천 (아직 적용 안 됨)
            "parameter_changes": [],  # 적용된 변경사항
            "avoid_situations": [],
            "focus_opportunities": [],
            "next_week_outlook": "",
        }

        # evolver 상태
        state = None
        if evolver:
            state = evolver.get_evolution_state()

        if state:
            result["summary"]["version"] = state.version
            result["summary"]["total_evolutions"] = state.total_applied
            result["summary"]["successful_changes"] = state.total_kept
            result["summary"]["rolled_back_changes"] = state.total_rolled_back

            # 마지막 진화 시간
            last_ts = None
            if state.active_change:
                last_ts = state.active_change.timestamp
            elif state.history:
                last_ts = state.history[-1].timestamp
            result["summary"]["last_evolution"] = last_ts

            # active_change → AS-IS/TO-BE 매핑 (단수)
            if state.active_change:
                ch = state.active_change
                result["parameter_changes"].append({
                    "strategy": ch.strategy,
                    "parameter": ch.parameter,
                    "as_is": ch.old_value,
                    "to_be": ch.new_value,
                    "reason": ch.reason,
                    "source": ch.source,
                    "confidence": None,
                    "expected_impact": None,
                    "is_effective": ch.is_effective,
                    "win_rate_before": ch.win_rate_before,
                    "win_rate_after": ch.win_rate_after,
                    "trades_before": ch.trades_before,
                    "trades_after": ch.trades_after,
                    "timestamp": ch.timestamp,
                })

        # advice JSON 보강
        advice = self._load_latest_advice()
        if advice:
            result["summary"]["assessment"] = advice.get("overall_assessment", "unknown")
            result["summary"]["confidence"] = advice.get("confidence_score", 0)
            result["insights"] = advice.get("key_insights", [])
            result["avoid_situations"] = advice.get("avoid_situations", [])
            result["focus_opportunities"] = advice.get("focus_opportunities", [])
            result["next_week_outlook"] = advice.get("next_week_outlook", "")

            # advice에 parameter_adjustments가 있으면 추천으로 추가
            if advice.get("parameter_adjustments"):
                for adj in advice["parameter_adjustments"]:
                    result["parameter_adjustments"].append({
                        "strategy": adj.get("strategy", ""),
                        "parameter": adj.get("parameter", ""),
                        "current_value": adj.get("current_value"),
                        "suggested_value": adj.get("suggested_value"),
                        "reason": adj.get("reason", ""),
                        "confidence": adj.get("confidence"),
                        "expected_impact": adj.get("expected_impact"),
                    })

        return _serialize(result)

    def get_evolution_history(self) -> List[Dict[str, Any]]:
        """진화 변경 이력 전체 (AS-IS/TO-BE 포맷)"""
        evolver = getattr(self.bot, 'strategy_evolver', None)
        if not evolver:
            return []

        state = evolver.get_evolution_state()
        if not state:
            return []

        history = []
        for ch in state.history:
            history.append(_serialize({
                "strategy": ch.strategy,
                "parameter": ch.parameter,
                "as_is": ch.old_value,
                "to_be": ch.new_value,
                "reason": ch.reason,
                "source": ch.source,
                "is_effective": ch.is_effective,
                "win_rate_before": ch.win_rate_before,
                "win_rate_after": ch.win_rate_after,
                "trades_before": ch.trades_before,
                "trades_after": ch.trades_after,
                "timestamp": ch.timestamp,
            }))

        return history

    # ----------------------------------------------------------
    # US 마켓 데이터
    # ----------------------------------------------------------

    async def get_us_market(self) -> Dict[str, Any]:
        """US 오버나이트 시그널 데이터"""
        us_market_data = getattr(self.bot, 'us_market_data', None)
        if not us_market_data:
            return {"available": False, "message": "US 마켓 데이터 비활성"}

        try:
            cached = getattr(us_market_data, '_cache', None)
            if not cached:
                return {"available": False, "message": "US 데이터 아직 없음 (08:00 이후 조회)"}

            cache_ts = getattr(us_market_data, '_cache_ts', None)

            result = {
                "available": True,
                "symbols": {},
                "sector_signals": {},
                "cache_time": cache_ts.isoformat() if cache_ts else None,
            }

            # 심볼별 데이터
            for symbol, data in cached.items():
                result["symbols"][symbol] = {
                    "price": data.get("price", 0),
                    "change": data.get("change", 0),
                    "change_pct": data.get("change_pct", 0),
                    "name": data.get("name", symbol),
                }

            # 섹터 시그널 (캐시된 데이터로 계산)
            try:
                sector_signals = await us_market_data.get_sector_signals()
                for theme, sig in sector_signals.items():
                    result["sector_signals"][theme] = {
                        "boost": sig.get("boost", 0),
                        "us_avg_pct": sig.get("us_avg_pct", 0),
                        "us_max_pct": sig.get("us_max_pct", 0),
                        "top_movers": sig.get("top_movers", []),
                    }
            except Exception as e:
                logger.debug(f"US 섹터 시그널 조회 실패: {e}")

            return _serialize(result)
        except Exception as e:
            logger.debug(f"US 데이터 조회 실패: {e}")
            return {"available": False, "message": "US 데이터 조회 실패"}

    # ----------------------------------------------------------
    # 이벤트 로그 (대시보드용)
    # ----------------------------------------------------------

    def get_events(self, since_id: int = 0) -> List[Dict[str, Any]]:
        """대시보드 이벤트 로그 (since_id 이후만 반환)"""
        engine = self.bot.engine
        events = getattr(engine, '_dashboard_events', [])
        return [e for e in events if e.get("id", 0) > since_id]

    # ----------------------------------------------------------
    # 프리마켓 (NXT) 데이터
    # ----------------------------------------------------------

    def get_premarket(self) -> Dict[str, Any]:
        """프리마켓 종목 데이터"""
        engine = self.bot.engine
        premarket = getattr(engine, 'premarket_data', {})

        if not premarket:
            return {"available": False, "stocks": []}

        name_cache = self._build_name_cache()
        stocks = []
        for symbol, data in premarket.items():
            stocks.append({
                "symbol": symbol,
                "name": name_cache.get(symbol, symbol),
                "pre_change_pct": data.get("pre_change_pct", 0),
                "pre_price": data.get("pre_price", 0),
                "prev_close": data.get("prev_close", 0),
                "pre_volume": data.get("pre_volume", 0),
                "pre_high": data.get("pre_high", 0),
                "pre_low": data.get("pre_low", 0),
                "updated_at": data.get("updated_at"),
            })

        # 등락률 내림차순
        stocks.sort(key=lambda x: abs(x["pre_change_pct"]), reverse=True)
        return {"available": True, "count": len(stocks), "stocks": stocks[:30]}

    # ----------------------------------------------------------
    # 에퀴티 커브 (일별 누적 손익)
    # ----------------------------------------------------------

    def get_equity_curve(self, days: int = 30) -> List[Dict[str, Any]]:
        """일별 손익 히스토리 (에퀴티 커브용)"""
        journal = self.bot.trade_journal
        if not journal:
            return []

        today = date.today()
        daily_data = []
        cumulative = 0.0

        for i in range(days - 1, -1, -1):  # 과거→현재 순
            d = today - timedelta(days=i)
            trades = journal.get_trades_by_date(d)
            closed = [t for t in trades if t.is_closed]

            day_pnl = sum(t.pnl for t in closed)
            day_trades = len(closed)
            day_wins = len([t for t in closed if t.is_win])
            cumulative += day_pnl

            if day_trades > 0 or cumulative != 0:
                daily_data.append({
                    "date": d.isoformat(),
                    "pnl": day_pnl,
                    "cumulative_pnl": cumulative,
                    "trades": day_trades,
                    "wins": day_wins,
                    "win_rate": (day_wins / day_trades * 100) if day_trades > 0 else 0,
                })

        return daily_data

    # ----------------------------------------------------------
    # 설정 (읽기 전용)
    # ----------------------------------------------------------

    def get_config(self) -> Dict[str, Any]:
        """현재 설정"""
        bot = self.bot
        engine = bot.engine
        config = engine.config

        # US 마켓 설정 (AppConfig.raw에서 조회)
        us_market_cfg = {}
        app_config = getattr(bot, 'config', None)
        if app_config and hasattr(app_config, 'raw'):
            us_market_cfg = app_config.raw.get("us_market", {})

        result = {
            "trading": {
                "initial_capital": float(config.initial_capital),
                "market": config.market.value,
                "enable_pre_market": config.enable_pre_market,
                "enable_next_market": config.enable_next_market,
                "buy_fee_rate": config.buy_fee_rate,
                "sell_fee_rate": config.sell_fee_rate,
            },
            "risk": {
                "daily_max_loss_pct": config.risk.daily_max_loss_pct,
                "daily_max_trades": config.risk.daily_max_trades,
                "base_position_pct": config.risk.base_position_pct,
                "max_position_pct": config.risk.max_position_pct,
                "max_positions": config.risk.max_positions,
                "min_cash_reserve_pct": config.risk.min_cash_reserve_pct,
                "default_stop_loss_pct": config.risk.default_stop_loss_pct,
                "default_take_profit_pct": config.risk.default_take_profit_pct,
                "trailing_stop_pct": config.risk.trailing_stop_pct,
            },
            "us_market": us_market_cfg,
            "strategies": {},
            "exit_manager": {},
        }

        # 전략 설정
        if bot.strategy_manager:
            for name, strategy in bot.strategy_manager.strategies.items():
                result["strategies"][name] = {
                    "enabled": name in bot.strategy_manager.enabled_strategies,
                    "type": name,
                }
                if hasattr(strategy, 'config'):
                    cfg = strategy.config
                    for attr in dir(cfg):
                        if not attr.startswith('_'):
                            val = getattr(cfg, attr)
                            if isinstance(val, (int, float, bool, str)):
                                result["strategies"][name][attr] = val

        # 분할 익절 설정
        if bot.exit_manager:
            ecfg = bot.exit_manager.config
            result["exit_manager"] = {
                "enable_partial_exit": ecfg.enable_partial_exit,
                "first_exit_pct": ecfg.first_exit_pct,
                "first_exit_ratio": ecfg.first_exit_ratio,
                "second_exit_pct": ecfg.second_exit_pct,
                "second_exit_ratio": ecfg.second_exit_ratio,
                "stop_loss_pct": ecfg.stop_loss_pct,
                "trailing_stop_pct": ecfg.trailing_stop_pct,
                "trailing_activate_pct": ecfg.trailing_activate_pct,
            }

        return result

    # ----------------------------------------------------------
    # 대기 주문 (Pending Orders)
    # ----------------------------------------------------------

    def get_pending_orders(self) -> List[Dict[str, Any]]:
        """대기 중인 주문 목록 (RiskManager 기반)"""
        engine = self.bot.engine
        rm = engine.risk_manager
        if not rm:
            return []

        name_cache = self._build_name_cache()
        now = datetime.now()
        result = []

        # 스냅샷 복사 (순회 중 수정 방어)
        pending_orders = set(getattr(rm, '_pending_orders', set()))
        pending_sides = dict(getattr(rm, '_pending_sides', {}))
        pending_timestamps = dict(getattr(rm, '_pending_timestamps', {}))
        pending_quantities = dict(getattr(rm, '_pending_quantities', {}))

        for symbol in pending_orders:
            side = pending_sides.get(symbol)
            side_str = side.value if side else "UNKNOWN"
            ts = pending_timestamps.get(symbol)
            elapsed = (now - ts).total_seconds() if ts else 0
            is_sell = side_str == "SELL"
            timeout = 90 if is_sell else 600
            remaining = max(timeout - elapsed, 0)
            progress = min(elapsed / timeout * 100, 100) if timeout > 0 else 0

            result.append({
                "symbol": symbol,
                "name": name_cache.get(symbol, symbol),
                "side": side_str,
                "quantity": pending_quantities.get(symbol, 0),
                "elapsed_seconds": round(elapsed),
                "timeout_seconds": timeout,
                "remaining_seconds": round(remaining),
                "progress_pct": round(progress, 1),
            })

        # 경과 시간 내림차순 정렬
        result.sort(key=lambda x: x["elapsed_seconds"], reverse=True)
        return result

    # ----------------------------------------------------------
    # 주문 내역 (Order History)
    # ----------------------------------------------------------

    def get_order_history(self) -> List[Dict[str, Any]]:
        """주문 관련 이벤트 히스토리 (주문/체결/취소/폴백)"""
        engine = self.bot.engine
        events = getattr(engine, '_dashboard_events', [])
        keywords = ("주문", "체결", "폴백", "취소")
        return [e for e in events if any(kw in e.get("type", "") for kw in keywords)
                or any(kw in e.get("message", "") for kw in keywords)]

    # ----------------------------------------------------------
    # 헬스체크 결과
    # ----------------------------------------------------------

    def get_health_checks(self) -> List[Dict[str, Any]]:
        """최신 헬스체크 결과"""
        hm = getattr(self.bot, 'health_monitor', None)
        if not hm or not hm._results:
            return []
        return [
            {"name": r.name, "level": r.level, "ok": r.ok,
             "message": r.message, "value": r.value,
             "timestamp": r.timestamp.isoformat()}
            for r in hm._results
        ]

    # ----------------------------------------------------------
    # 외부 계좌 (대시보드 전용)
    # ----------------------------------------------------------

    async def get_external_accounts(self) -> list:
        """외부 계좌 보유 포지션 + 요약 (30초 TTL 캐시, Lock 보호)"""
        async with self._ext_accounts_lock:
            now = datetime.now()
            if (
                self._ext_accounts_cache is not None
                and self._ext_accounts_cache_ts
                and (now - self._ext_accounts_cache_ts).total_seconds() < 30
            ):
                return self._ext_accounts_cache

            bot = self.bot
            ext_accounts = getattr(bot, '_external_accounts', [])
            broker = getattr(bot, 'broker', None)
            if not ext_accounts or not broker:
                self._ext_accounts_cache = []
                self._ext_accounts_cache_ts = now
                return []

            result = []
            for name, cano, acnt_prdt_cd in ext_accounts:
                try:
                    positions, summary = await broker.get_positions_for_account(
                        cano, acnt_prdt_cd
                    )
                    result.append({
                        "name": name,
                        "cano": cano,
                        "summary": summary,
                        "positions": positions,
                    })
                except Exception as e:
                    logger.warning(f"외부 계좌 {name}(****{cano[-4:]}) 조회 실패: {e}")
                    result.append({
                        "name": name,
                        "cano": cano,
                        "summary": {},
                        "positions": [],
                        "error": str(e),
                    })

            self._ext_accounts_cache = result
            self._ext_accounts_cache_ts = now
            return result

    # ----------------------------------------------------------
    # 자산 히스토리 (Equity History)
    # ----------------------------------------------------------

    def _build_equity_summary(self, snapshots) -> Dict[str, Any]:
        """스냅샷 리스트에서 요약 통계 계산"""
        snap_dicts = [s.to_dict() for s in snapshots]

        first_equity = snapshots[0].total_equity
        last_equity = snapshots[-1].total_equity
        period_return = last_equity - first_equity
        period_return_pct = (period_return / first_equity * 100) if first_equity > 0 else 0

        # 최대 낙폭 (MDD)
        peak = snapshots[0].total_equity
        max_drawdown = 0.0
        for s in snapshots:
            if s.total_equity > peak:
                peak = s.total_equity
            dd = (s.total_equity - peak) / peak * 100 if peak > 0 else 0
            if dd < max_drawdown:
                max_drawdown = dd

        # 평균 일일 손익
        daily_pnls = [s.daily_pnl for s in snapshots if s.daily_pnl != 0]
        avg_daily_pnl = sum(daily_pnls) / len(daily_pnls) if daily_pnls else 0

        # 가장 오래된/최신 날짜
        tracker = getattr(self.bot, 'equity_tracker', None)
        oldest_date = tracker.get_oldest_date() if tracker else None

        return {
            "snapshots": snap_dicts,
            "summary": {
                "period_return": round(period_return, 0),
                "period_return_pct": round(period_return_pct, 2),
                "max_drawdown_pct": round(max_drawdown, 2),
                "avg_daily_pnl": round(avg_daily_pnl, 0),
                "first_equity": first_equity,
                "last_equity": last_equity,
                "data_days": len(snapshots),
                "oldest_date": oldest_date,
            },
        }

    def get_equity_history(self, days: int = 30) -> Dict[str, Any]:
        """일별 자산 히스토리 + 요약 통계 (days 기반)"""
        tracker = getattr(self.bot, 'equity_tracker', None)
        if not tracker:
            return {"snapshots": [], "summary": {}}

        snapshots = tracker.load_history(days)
        if not snapshots:
            return {"snapshots": [], "summary": {"oldest_date": tracker.get_oldest_date()}}

        return self._build_equity_summary(snapshots)

    def get_equity_history_range(self, date_from: str, date_to: str) -> Dict[str, Any]:
        """일별 자산 히스토리 + 요약 통계 (from~to 범위)"""
        tracker = getattr(self.bot, 'equity_tracker', None)
        if not tracker:
            return {"snapshots": [], "summary": {}}

        snapshots = tracker.load_history_range(date_from, date_to)
        if not snapshots:
            return {"snapshots": [], "summary": {"oldest_date": tracker.get_oldest_date()}}

        return self._build_equity_summary(snapshots)

    def get_equity_history_positions(self, date_str: str) -> Dict[str, Any]:
        """특정일 자산 스냅샷 + 포지션 상세"""
        tracker = getattr(self.bot, 'equity_tracker', None)
        if not tracker:
            return {"error": "Equity tracker not available"}

        snapshot = tracker.get_snapshot(date_str)
        if not snapshot:
            return {"error": f"No data for {date_str}", "date": date_str, "positions": []}

        return snapshot.to_dict()

    # ----------------------------------------------------------
    # 일일 거래 리뷰 (Daily Review)
    # ----------------------------------------------------------

    def get_daily_review(self, date_str: str) -> Dict[str, Any]:
        """일일 거래 리뷰 (거래 복기 + LLM 평가 통합)"""
        reviewer = getattr(self.bot, 'daily_reviewer', None)
        if not reviewer:
            return {"date": date_str, "trade_report": None, "llm_review": None}

        trade_report = reviewer.load_report(date_str)
        llm_review = reviewer.load_llm_review(date_str)

        return _serialize({
            "date": date_str,
            "trade_report": trade_report,
            "llm_review": llm_review,
        })

    def get_daily_review_dates(self) -> Dict[str, Any]:
        """리뷰 가능 날짜 목록"""
        reviewer = getattr(self.bot, 'daily_reviewer', None)
        if not reviewer:
            return {"dates": []}

        return {"dates": reviewer.list_available_dates()}

    # ----------------------------------------------------------
    # 시스템 건강 메트릭
    # ----------------------------------------------------------

    def get_system_health(self) -> Dict[str, Any]:
        """시스템 건강 상태 (캐시, API, 토큰 등)"""
        bot = self.bot
        engine = bot.engine

        # 전략 캐시 크기
        cache_stats = {}
        if bot.strategy_manager:
            for name, strategy in bot.strategy_manager.strategies.items():
                cache_stats[name] = {
                    "price_history_symbols": len(getattr(strategy, '_price_history', {})),
                    "indicators_symbols": len(getattr(strategy, '_indicators', {})),
                }

        # 브로커 상태
        broker_stats = {}
        if bot.broker:
            broker_stats = {
                "connected": bot.broker.is_connected,
                "rate_limit_calls_last_sec": len(getattr(bot.broker, '_api_call_times', [])),
                "pending_orders": len(getattr(bot.broker, '_pending_orders', {})),
            }

        # 엔진 리스크 매니저
        risk_stats = {}
        if engine.risk_manager:
            rm = engine.risk_manager
            pending_sides = getattr(rm, '_pending_sides', {})
            risk_stats = {
                "pending_orders": len(getattr(rm, '_pending_orders', set())),
                "pending_quantities": len(getattr(rm, '_pending_quantities', {})),
                "pending_sells": sum(
                    1 for s in pending_sides.values()
                    if s and s.value == 'SELL'
                ),
                "cooldown_symbols": len(getattr(rm, '_order_fail_cooldown', {})),
                "reserved_cash": float(getattr(rm, '_reserved_cash', 0)),
            }

        return _serialize({
            "cache": cache_stats,
            "broker": broker_stats,
            "risk_manager": risk_stats,
            "stock_name_cache_size": len(getattr(bot, 'stock_name_cache', {})),
            "watch_symbols_count": len(getattr(bot, '_watch_symbols', [])),
            "timestamp": datetime.now(),
        })

    # ----------------------------------------------------------
    # 일일 정산 (KIS 체결 기반)
    # ----------------------------------------------------------

    # 수수료율 상수
    BUY_FEE_RATE = 0.000141   # 매수 수수료 0.0141%
    SELL_FEE_RATE = 0.000131  # 매도 수수료 0.0131%
    SELL_TAX_RATE = 0.002     # 증권거래세 0.20%

    async def get_daily_settlement(self, target_date: date = None) -> Dict[str, Any]:
        """
        KIS 체결 내역 기반 일일 정산.

        매수/매도 체결을 KIS API에서 조회하고, DB 진입가와 대조하여
        수수료·세금 포함 실현손익을 계산합니다.
        """
        target_date = target_date or date.today()
        broker = getattr(self.bot, 'broker', None)
        if not broker or not getattr(broker, 'is_connected', False):
            return {"error": "브로커 미연결", "date": target_date.isoformat()}

        try:
            fills = await broker.get_all_fills_for_date(target_date)
        except Exception as e:
            logger.error(f"[정산] KIS 체결 조회 실패: {e}")
            return {"error": f"KIS 체결 조회 실패: {e}", "date": target_date.isoformat()}

        if not fills:
            return {
                "date": target_date.isoformat(),
                "buys": [], "sells": [], "holdings": [],
                "summary": {
                    "total_buy_amount": 0, "total_sell_amount": 0,
                    "realized_pnl": 0, "unrealized_pnl": 0,
                    "total_pnl": 0, "buy_count": 0, "sell_count": 0,
                    "win_count": 0, "loss_count": 0,
                },
            }

        buys_raw = [f for f in fills if f.get('sll_buy_dvsn_cd') == '02']
        sells_raw = [f for f in fills if f.get('sll_buy_dvsn_cd') == '01']

        # DB에서 진입가 조회
        entry_prices = await self._load_entry_prices(target_date)

        # KIS 보유 종목 평균단가 (더 정확)
        positions_raw = {}
        try:
            positions_raw = await broker.get_positions()
            # dict or list 모두 처리
            pos_items = positions_raw.values() if isinstance(positions_raw, dict) else positions_raw
            for p in pos_items:
                try:
                    sym = p.symbol
                    avg = float(p.avg_price)
                    if sym and avg > 0:
                        entry_prices[sym] = avg
                except Exception:
                    continue
        except Exception:
            positions_raw = {}

        name_cache = self._build_name_cache()

        # 매수 정리
        buy_list = []
        total_buy_amount = 0
        for f in sorted(buys_raw, key=lambda x: x.get('ord_tmd', '')):
            qty = int(f.get('tot_ccld_qty', 0))
            price = float(f.get('avg_prvs', 0))
            amount = qty * price
            fee = round(amount * self.BUY_FEE_RATE)
            sym = f.get('symbol', '')
            name = f.get('name', '') or name_cache.get(sym, sym)
            total_buy_amount += amount + fee
            buy_list.append({
                "time": self._format_kis_time(f.get('ord_tmd', '')),
                "symbol": sym, "name": name,
                "quantity": qty, "price": price,
                "amount": amount, "fee": fee,
                "total": amount + fee,
                "odno": f.get('odno', ''),
            })

        # 매도 정리 + 손익 계산
        sell_list = []
        total_sell_net = 0
        total_realized_pnl = 0
        win_count = 0
        loss_count = 0
        for f in sorted(sells_raw, key=lambda x: x.get('ord_tmd', '')):
            qty = int(f.get('tot_ccld_qty', 0))
            sell_price = float(f.get('avg_prvs', 0))
            sell_amount = qty * sell_price
            sell_fee = round(sell_amount * self.SELL_FEE_RATE)
            sell_tax = round(sell_amount * self.SELL_TAX_RATE)
            sell_net = sell_amount - sell_fee - sell_tax
            total_sell_net += sell_net

            sym = f.get('symbol', '')
            name = f.get('name', '') or name_cache.get(sym, sym)

            ep = entry_prices.get(sym, 0)
            pnl = 0
            pnl_pct = 0
            if ep > 0:
                buy_cost = qty * ep
                buy_fee = round(buy_cost * self.BUY_FEE_RATE)
                pnl = sell_net - buy_cost - buy_fee
                pnl_pct = (pnl / (buy_cost + buy_fee) * 100) if (buy_cost + buy_fee) > 0 else 0
                total_realized_pnl += pnl
                if pnl >= 0:
                    win_count += 1
                else:
                    loss_count += 1

            sell_list.append({
                "time": self._format_kis_time(f.get('ord_tmd', '')),
                "symbol": sym, "name": name,
                "quantity": qty, "price": sell_price,
                "amount": sell_amount, "fee": sell_fee, "tax": sell_tax,
                "net": sell_net,
                "entry_price": ep,
                "pnl": round(pnl), "pnl_pct": round(pnl_pct, 2),
                "odno": f.get('odno', ''),
            })

        # 보유 현황 (미실현)
        holdings = []
        total_unrealized = 0
        try:
            pos_items = positions_raw.values() if isinstance(positions_raw, dict) else positions_raw
            for p in pos_items:
                try:
                    sym = p.symbol
                    avg = float(p.avg_price)
                    cur = float(p.current_price) if p.current_price else 0
                    qty = p.quantity
                    if qty <= 0 or avg <= 0:
                        continue
                    unrealized = (cur - avg) * qty
                    pct = (cur - avg) / avg * 100 if avg > 0 else 0
                    total_unrealized += unrealized
                    name = getattr(p, 'name', '') or name_cache.get(sym, sym)
                    holdings.append({
                        "symbol": sym, "name": name,
                        "quantity": qty, "avg_price": avg,
                        "current_price": cur,
                        "unrealized_pnl": round(unrealized),
                        "unrealized_pct": round(pct, 2),
                    })
                except Exception:
                    continue
        except Exception:
            pass

        # 미실현 수익률순 정렬
        holdings.sort(key=lambda h: h.get('unrealized_pnl', 0), reverse=True)

        return _serialize({
            "date": target_date.isoformat(),
            "buys": buy_list,
            "sells": sell_list,
            "holdings": holdings,
            "summary": {
                "total_buy_amount": round(total_buy_amount),
                "total_sell_amount": round(total_sell_net),
                "realized_pnl": round(total_realized_pnl),
                "unrealized_pnl": round(total_unrealized),
                "total_pnl": round(total_realized_pnl + total_unrealized),
                "buy_count": len(buy_list),
                "sell_count": len(sell_list),
                "win_count": win_count,
                "loss_count": loss_count,
                "holdings_count": len(holdings),
            },
        })

    async def _load_entry_prices(self, target_date: date) -> Dict[str, float]:
        """DB에서 매도 종목의 진입가 로딩"""
        entry_prices: Dict[str, float] = {}
        journal = self.bot.trade_journal
        if not journal:
            return entry_prices

        # TradeStorage (DB) 사용 시
        pool = getattr(journal, 'pool', None) or getattr(journal, '_pool', None)
        if pool:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch("""
                        SELECT symbol, entry_price, exit_time
                        FROM trades
                        WHERE exit_time IS NULL
                           OR exit_time::date = $1
                        ORDER BY entry_time
                    """, target_date)
                    for r in rows:
                        entry_prices[r['symbol']] = float(r['entry_price'])
            except Exception as e:
                logger.warning(f"[정산] DB 진입가 조회 실패: {e}")

        # 캐시 폴백
        if not entry_prices:
            try:
                for t in journal.get_open_trades():
                    entry_prices[t.symbol] = float(t.entry_price)
                for t in journal.get_trades_by_date(target_date):
                    entry_prices[t.symbol] = float(t.entry_price)
            except Exception as e:
                logger.warning(f"[정산] 캐시 진입가 조회 실패: {e}")

        return entry_prices

    @staticmethod
    def _format_kis_time(ord_tmd: str) -> str:
        """KIS 시각 포맷 (HHMMSS → HH:MM:SS)"""
        if len(ord_tmd) >= 6:
            return f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:6]}"
        return ord_tmd
