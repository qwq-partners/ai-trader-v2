"""
AI Trading Bot v2 - SSE (Server-Sent Events) 스트림 관리

실시간 데이터를 브라우저에 푸시합니다.
"""

import asyncio
import json
import time
from typing import Any, Dict, Set

from aiohttp import web
from loguru import logger


class SSEManager:
    """SSE 클라이언트 관리 및 브로드캐스트"""

    def __init__(self, data_collector):
        self.data_collector = data_collector
        self._clients: Set[web.StreamResponse] = set()
        self._running = False

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        """SSE 스트림 핸들러"""
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        self._clients.add(response)
        logger.info(f"[SSE] 클라이언트 연결 (총 {len(self._clients)}명)")

        try:
            # 연결 유지 (클라이언트 끊길 때까지)
            while True:
                await asyncio.sleep(15)  # 15초마다 heartbeat
                try:
                    await response.write(b": heartbeat\n\n")
                except (ConnectionResetError, ConnectionError,
                        BrokenPipeError, OSError):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._clients.discard(response)
            logger.info(f"[SSE] 클라이언트 연결 해제 (남은 {len(self._clients)}명)")

        return response

    async def broadcast(self, event_type: str, data: Any):
        """모든 연결된 클라이언트에 이벤트 전송"""
        if not self._clients:
            return

        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        payload_bytes = payload.encode("utf-8")

        disconnected = set()
        for client in self._clients:
            try:
                await client.write(payload_bytes)
            except (ConnectionResetError, ConnectionError,
                    BrokenPipeError, OSError):
                disconnected.add(client)
            except Exception:
                disconnected.add(client)

        # 끊긴 클라이언트 제거
        if disconnected:
            self._clients -= disconnected
            logger.debug(f"[SSE] 끊긴 클라이언트 {len(disconnected)}명 정리 (남은 {len(self._clients)}명)")

    async def run_broadcast_loop(self):
        """주기적 데이터 브로드캐스트"""
        self._running = True
        dc = self.data_collector

        # 각 이벤트별 마지막 전송 시간
        last_sent: Dict[str, float] = {}

        # 이벤트별 주기 (초)
        intervals = {
            "status": 5,
            "portfolio": 5,
            "positions": 2,
            "risk": 10,
            "events": 2,
        }

        # 이벤트 로그 커서
        last_event_id = 0

        logger.info("[SSE] 브로드캐스트 루프 시작")

        try:
            while self._running:
                now = time.time()

                for event_type, interval in intervals.items():
                    if now - last_sent.get(event_type, 0) >= interval:
                        try:
                            if event_type == "status":
                                data = dc.get_status()
                            elif event_type == "portfolio":
                                data = dc.get_portfolio()
                            elif event_type == "positions":
                                data = dc.get_positions()
                            elif event_type == "risk":
                                data = dc.get_risk()
                            elif event_type == "events":
                                new_events = dc.get_events(last_event_id)
                                if not new_events:
                                    last_sent[event_type] = now
                                    continue
                                data = new_events
                                last_event_id = new_events[-1].get("id", last_event_id)
                            else:
                                continue

                            await self.broadcast(event_type, data)
                            last_sent[event_type] = now

                        except Exception as e:
                            logger.error(f"[SSE] {event_type} 브로드캐스트 오류: {type(e).__name__}: {e}")

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("[SSE] 브로드캐스트 루프 종료")

    def stop(self):
        """브로드캐스트 중지"""
        self._running = False
