"""
AI Trading Bot v2 - 대시보드 웹서버

aiohttp 기반 내장 웹서버로, 봇 프로세스에서 직접 실행됩니다.
"""

import asyncio
from pathlib import Path

from aiohttp import web
from loguru import logger

from .api import setup_api_routes
from .data_collector import DashboardDataCollector
from .sse import SSEManager


DASHBOARD_DIR = Path(__file__).parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"


@web.middleware
async def no_cache_middleware(request, handler):
    """정적 파일 캐시 방지 미들웨어"""
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class DashboardServer:
    """대시보드 웹서버"""

    def __init__(self, bot, host: str = "0.0.0.0", port: int = 8080):
        self.bot = bot
        self.host = host
        self.port = port

        self.data_collector = DashboardDataCollector(bot)
        self.sse_manager = SSEManager(self.data_collector)

        self._app: web.Application = None
        self._runner: web.AppRunner = None
        self._site: web.TCPSite = None

    def _create_app(self) -> web.Application:
        """aiohttp 앱 생성"""
        app = web.Application(middlewares=[no_cache_middleware])

        # REST API 라우트
        setup_api_routes(app, self.data_collector)

        # SSE 스트림
        app.router.add_get("/api/stream", self.sse_manager.handle_stream)

        # 페이지 라우트
        app.router.add_get("/", self._serve_page("index.html"))
        app.router.add_get("/equity", self._serve_page("equity.html"))
        app.router.add_get("/trades", self._serve_page("trades.html"))
        app.router.add_get("/performance", self._serve_page("performance.html"))
        app.router.add_get("/themes", self._serve_page("themes.html"))
        app.router.add_get("/settings", self._serve_page("settings.html"))
        app.router.add_get("/evolution", self._serve_page("evolution.html"))

        # 정적 파일 서빙
        app.router.add_static("/static", STATIC_DIR, name="static")

        return app

    def _serve_page(self, template_name: str):
        """HTML 페이지 서빙 핸들러 팩토리"""
        async def handler(request: web.Request) -> web.Response:
            file_path = TEMPLATES_DIR / template_name
            if not file_path.exists():
                return web.Response(text="Page not found", status=404)

            content = file_path.read_text(encoding="utf-8")
            return web.Response(text=content, content_type="text/html", charset="utf-8")

        return handler

    async def start(self):
        """서버 시작"""
        self._app = self._create_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        logger.info(f"[대시보드] http://{self.host}:{self.port} 에서 실행 중")

    async def stop(self):
        """서버 중지"""
        self.sse_manager.stop()
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        logger.info("[대시보드] 서버 종료")

    async def run(self):
        """서버 + SSE 브로드캐스트 실행 (태스크용)"""
        try:
            await self.start()

            # SSE 브로드캐스트 루프 실행
            await self.sse_manager.run_broadcast_loop()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[대시보드] 서버 오류: {e}")
        finally:
            await self.stop()
