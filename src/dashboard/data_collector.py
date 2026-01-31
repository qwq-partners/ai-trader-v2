"""
AI Trading Bot v2 - 대시보드 데이터 수집기

봇의 런타임 데이터를 JSON 변환하여 API/SSE에 제공합니다.
"""

import asyncio
import glob
import json
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


def _decimal_to_float(obj):
    """Decimal → float 변환 (JSON 직렬화용)"""
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


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

    def __init__(self, bot):
        self.bot = bot

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
        """포트폴리오 정보"""
        portfolio = self.bot.engine.portfolio

        return _serialize({
            "cash": portfolio.cash,
            "total_position_value": portfolio.total_position_value,
            "total_equity": portfolio.total_equity,
            "initial_capital": portfolio.initial_capital,
            "total_pnl": portfolio.total_pnl,
            "total_pnl_pct": portfolio.total_pnl_pct,
            "daily_pnl": portfolio.daily_pnl,
            "daily_pnl_pct": (
                float(portfolio.daily_pnl / portfolio.initial_capital * 100)
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

            positions.append(_serialize({
                "symbol": symbol,
                "name": getattr(pos, 'name', '') or symbol,
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
        """리스크 지표"""
        engine = self.bot.engine
        risk_mgr = self.bot.risk_manager
        portfolio = engine.portfolio

        daily_loss_pct = (
            float(portfolio.daily_pnl / portfolio.initial_capital * 100)
            if portfolio.initial_capital > 0 else 0.0
        )

        config = engine.config.risk

        # 동적 max_positions 계산
        effective_max = config.max_positions
        if risk_mgr and hasattr(risk_mgr, 'get_effective_max_positions'):
            effective_max = risk_mgr.get_effective_max_positions(portfolio.total_equity)

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

    def _build_name_cache(self) -> Dict[str, str]:
        """종목명 캐시 구축 (봇 캐시 + 포지션 + 스크리너)"""
        cache: Dict[str, str] = {}

        # 1. 봇 레벨 캐시 (가장 우선)
        bot_cache = getattr(self.bot, 'stock_name_cache', {})
        cache.update(bot_cache)

        # 2. 포지션에서 종목명
        portfolio = self.bot.engine.portfolio
        for symbol, pos in portfolio.positions.items():
            if symbol in cache:
                continue
            name = getattr(pos, 'name', '')
            if name and name != symbol:
                cache[symbol] = name

        # 3. 스크리너에서 종목명
        screener = self.bot.screener
        if screener:
            for stock in getattr(screener, '_last_screened', []):
                if stock.symbol not in cache and stock.name and stock.name != stock.symbol:
                    cache[stock.symbol] = stock.name

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
        else:
            stats['open_trades'] = 0
            stats['open_pnl'] = 0
            stats['open_avg_pnl_pct'] = 0
            stats['all_trades'] = stats.get('total_trades', 0)

        return stats

    # ----------------------------------------------------------
    # 테마 / 스크리닝
    # ----------------------------------------------------------

    def get_themes(self) -> List[Dict[str, Any]]:
        """활성 테마 목록"""
        detector = self.bot.theme_detector
        if not detector:
            return []

        themes = []
        raw_themes = getattr(detector, '_themes', {})
        if isinstance(raw_themes, dict):
            raw_themes = raw_themes.values()

        for theme in raw_themes:
            themes.append(_serialize({
                "name": theme.name,
                "keywords": theme.keywords,
                "related_stocks": theme.related_stocks,
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
            "parameter_changes": [],
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
            result["summary"]["total_evolutions"] = state.total_evolutions
            result["summary"]["successful_changes"] = state.successful_changes
            result["summary"]["rolled_back_changes"] = state.rolled_back_changes
            result["summary"]["last_evolution"] = (
                state.last_evolution.isoformat() if state.last_evolution else None
            )

            # active_changes → AS-IS/TO-BE 매핑
            for ch in state.active_changes:
                result["parameter_changes"].append({
                    "strategy": ch.strategy,
                    "parameter": ch.parameter,
                    "as_is": ch.old_value,
                    "to_be": ch.new_value,
                    "reason": ch.reason,
                    "source": ch.source,
                    "confidence": getattr(ch, 'confidence', None),
                    "expected_impact": getattr(ch, 'expected_impact', None),
                    "is_effective": ch.is_effective,
                    "win_rate_before": ch.win_rate_before,
                    "win_rate_after": ch.win_rate_after,
                    "trades_before": ch.trades_before,
                    "trades_after": ch.trades_after,
                    "timestamp": (
                        ch.timestamp.isoformat()
                        if isinstance(ch.timestamp, datetime) else ch.timestamp
                    ),
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

            # advice에 parameter_adjustments가 있고 evolver에서 못 가져온 경우 보충
            if not result["parameter_changes"] and advice.get("parameter_adjustments"):
                for adj in advice["parameter_adjustments"]:
                    result["parameter_changes"].append({
                        "strategy": adj.get("strategy", ""),
                        "parameter": adj.get("parameter", ""),
                        "as_is": adj.get("current_value"),
                        "to_be": adj.get("suggested_value"),
                        "reason": adj.get("reason", ""),
                        "source": "llm",
                        "confidence": adj.get("confidence"),
                        "expected_impact": adj.get("expected_impact"),
                        "is_effective": None,
                        "win_rate_before": None,
                        "win_rate_after": None,
                        "trades_before": None,
                        "trades_after": None,
                        "timestamp": advice.get("analysis_date"),
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
        for ch in state.change_history:
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
                "timestamp": (
                    ch.timestamp.isoformat()
                    if isinstance(ch.timestamp, datetime) else ch.timestamp
                ),
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
