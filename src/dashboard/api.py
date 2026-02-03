"""
AI Trading Bot v2 - REST API 핸들러
"""

import os
import asyncio
from datetime import date, datetime

from aiohttp import web
from loguru import logger


def setup_api_routes(app: web.Application, data_collector):
    """API 라우트 등록"""
    handler = APIHandler(data_collector)

    app.router.add_get("/api/status", handler.get_status)
    app.router.add_get("/api/portfolio", handler.get_portfolio)
    app.router.add_get("/api/positions", handler.get_positions)
    app.router.add_get("/api/risk", handler.get_risk)
    app.router.add_get("/api/trades/today", handler.get_today_trades)
    app.router.add_get("/api/trades", handler.get_trades)
    app.router.add_get("/api/trades/stats", handler.get_trade_stats)
    app.router.add_get("/api/themes", handler.get_themes)
    app.router.add_get("/api/screening", handler.get_screening)
    app.router.add_get("/api/config", handler.get_config)
    app.router.add_get("/api/us-market", handler.get_us_market)
    app.router.add_get("/api/evolution", handler.get_evolution)
    app.router.add_get("/api/evolution/history", handler.get_evolution_history)
    app.router.add_get("/api/code-evolution", handler.get_code_evolution)
    app.router.add_get("/api/health", handler.get_system_health)
    app.router.add_post("/api/evolution/apply", handler.apply_evolution_parameter)


class APIHandler:
    """REST API 핸들러"""

    def __init__(self, data_collector):
        self.dc = data_collector

    async def get_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_status())

    async def get_portfolio(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_portfolio())

    async def get_positions(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_positions())

    async def get_risk(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_risk())

    async def get_today_trades(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_today_trades())

    async def get_trades(self, request: web.Request) -> web.Response:
        date_str = request.query.get("date")
        if date_str:
            try:
                trade_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return web.json_response(
                    {"error": "Invalid date format. Use YYYY-MM-DD"},
                    status=400,
                )
        else:
            trade_date = date.today()

        return web.json_response(self.dc.get_trades_by_date(trade_date))

    async def get_trade_stats(self, request: web.Request) -> web.Response:
        days = int(request.query.get("days", "30"))
        days = max(1, min(days, 365))
        return web.json_response(self.dc.get_trade_stats(days))

    async def get_themes(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_themes())

    async def get_screening(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_screening())

    async def get_config(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_config())

    async def get_us_market(self, request: web.Request) -> web.Response:
        return web.json_response(await self.dc.get_us_market())

    async def get_evolution(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_evolution())

    async def get_evolution_history(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_evolution_history())

    async def get_code_evolution(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_code_evolution())

    async def get_system_health(self, request: web.Request) -> web.Response:
        return web.json_response(self.dc.get_system_health())

    async def apply_evolution_parameter(self, request: web.Request) -> web.Response:
        """
        파라미터 진화 추천 반영 + 봇 재시작

        POST /api/evolution/apply
        Body: {
            "strategy": "momentum_breakout",
            "parameter": "min_breakout_pct",
            "new_value": 0.8,
            "reason": "승률 향상을 위한 필터 강화"
        }
        """
        try:
            data = await request.json()
        except Exception as e:
            return web.json_response(
                {"success": False, "message": f"Invalid JSON: {e}"},
                status=400,
            )

        strategy = data.get("strategy")
        parameter = data.get("parameter")
        new_value = data.get("new_value")
        reason = data.get("reason", "대시보드 수동 반영")

        if not all([strategy, parameter, new_value is not None]):
            return web.json_response(
                {"success": False, "message": "strategy, parameter, new_value 필수"},
                status=400,
            )

        try:
            # 1. Config 업데이트 (evolved_config_manager 사용)
            from src.core.evolution.config_persistence import get_evolved_config_manager

            config_mgr = get_evolved_config_manager()
            success = await config_mgr.apply_parameter_change(
                strategy=strategy,
                parameter=parameter,
                new_value=new_value,
                reason=reason,
                source="dashboard",
            )

            if not success:
                return web.json_response(
                    {"success": False, "message": "파라미터 적용 실패"},
                    status=500,
                )

            logger.info(
                f"[대시보드] 파라미터 반영: {strategy}.{parameter} = {new_value} "
                f"(사유: {reason})"
            )

            # 2. 봇 재시작 예약 (3초 후)
            asyncio.create_task(self._restart_bot_delayed(3))

            return web.json_response({
                "success": True,
                "message": "파라미터가 적용되었습니다. 3초 후 봇이 재시작됩니다.",
            })

        except Exception as e:
            logger.error(f"[대시보드] 파라미터 반영 오류: {e}")
            return web.json_response(
                {"success": False, "message": str(e)},
                status=500,
            )

    async def _restart_bot_delayed(self, delay_seconds: int):
        """봇 재시작 (지연 실행)"""
        await asyncio.sleep(delay_seconds)
        logger.warning("[대시보드] 파라미터 적용 완료 → 봇 재시작")
        os._exit(0)  # systemd/supervisor가 재시작
