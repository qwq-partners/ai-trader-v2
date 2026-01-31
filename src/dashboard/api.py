"""
AI Trading Bot v2 - REST API 핸들러
"""

from datetime import date, datetime

from aiohttp import web


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
