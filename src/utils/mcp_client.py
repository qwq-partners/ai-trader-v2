"""
MCP 서버 클라이언트 매니저

pykrx-mcp, naver-search-mcp 서버의 수명주기를 관리하는 싱글톤.
서버 연결 실패 시 graceful degradation (None 반환).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

# MCP SDK lazy import (미설치 시 모듈 import 자체는 성공하도록)
ClientSession = None
StdioServerParameters = None
stdio_client = None


def _ensure_mcp_imports():
    """MCP SDK를 lazy import (미설치 시 ImportError 발생)"""
    global ClientSession, StdioServerParameters, stdio_client
    if ClientSession is not None:
        return
    from mcp import ClientSession as _CS, StdioServerParameters as _SSP
    from mcp.client.stdio import stdio_client as _sc
    ClientSession = _CS
    StdioServerParameters = _SSP
    stdio_client = _sc


@dataclass
class MCPServerConfig:
    """MCP 서버 연결 설정"""
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Optional[Dict[str, str]] = None
    startup_timeout: float = 20.0
    call_timeout: float = 10.0
    max_retries: int = 3


class MCPServerConnection:
    """개별 MCP 서버 연결 관리"""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._session: Optional[ClientSession] = None
        self._cm = None  # stdio_client context manager
        self._connected = False
        self._tools: List[str] = []
        self._last_reconnect_attempt: float = 0
        self._reconnect_count: int = 0
        self._backoff_intervals = [30, 60, 120, 300]

    @property
    def connected(self) -> bool:
        return self._connected and self._session is not None

    @property
    def tools(self) -> List[str]:
        return self._tools

    async def connect(self) -> bool:
        """MCP 서버에 연결"""
        try:
            _ensure_mcp_imports()
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env,
            )

            # stdio_client는 async context manager → 수동 진입
            self._cm = stdio_client(server_params)
            read_stream, write_stream = await asyncio.wait_for(
                self._cm.__aenter__(),
                timeout=self.config.startup_timeout,
            )

            self._session = ClientSession(read_stream, write_stream)
            await asyncio.wait_for(
                self._session.__aenter__(),
                timeout=self.config.startup_timeout,
            )

            # 초기화 + 도구 목록 가져오기
            await asyncio.wait_for(
                self._session.initialize(),
                timeout=self.config.startup_timeout,
            )

            tools_result = await asyncio.wait_for(
                self._session.list_tools(),
                timeout=self.config.startup_timeout,
            )
            self._tools = [t.name for t in tools_result.tools]
            self._connected = True
            self._reconnect_count = 0

            logger.info(
                f"[MCP] {self.config.name} 서버 연결 성공 "
                f"(도구 {len(self._tools)}개: {', '.join(self._tools[:5])}...)"
            )
            return True

        except asyncio.TimeoutError:
            logger.warning(f"[MCP] {self.config.name} 서버 연결 타임아웃 ({self.config.startup_timeout}초)")
            await self.disconnect()
            return False
        except Exception as e:
            logger.warning(f"[MCP] {self.config.name} 서버 연결 실패: {e}")
            await self.disconnect()
            return False

    async def disconnect(self):
        """MCP 서버 연결 해제"""
        self._connected = False
        self._tools = []

        try:
            if self._session is not None:
                await self._session.__aexit__(None, None, None)
        except Exception:
            pass
        finally:
            self._session = None

        try:
            if self._cm is not None:
                await self._cm.__aexit__(None, None, None)
        except Exception:
            pass
        finally:
            self._cm = None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Any]:
        """MCP 도구 호출 (실패 시 None)"""
        if not self.connected:
            if not await self._try_reconnect():
                return None

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=self.config.call_timeout,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[MCP] {self.config.name}.{tool_name} 호출 타임아웃 ({self.config.call_timeout}초)")
            return None
        except Exception as e:
            logger.warning(f"[MCP] {self.config.name}.{tool_name} 호출 실패: {e}")
            self._connected = False
            return None

    async def _try_reconnect(self) -> bool:
        """지수 백오프로 재연결 시도"""
        if self._reconnect_count >= self.config.max_retries:
            return False

        now = time.monotonic()
        backoff_idx = min(self._reconnect_count, len(self._backoff_intervals) - 1)
        backoff = self._backoff_intervals[backoff_idx]

        if now - self._last_reconnect_attempt < backoff:
            return False

        self._last_reconnect_attempt = now
        self._reconnect_count += 1

        logger.info(f"[MCP] {self.config.name} 재연결 시도 ({self._reconnect_count}/{self.config.max_retries})")
        await self.disconnect()
        return await self.connect()


class MCPClientManager:
    """MCP 서버 클라이언트 통합 매니저 (싱글톤)"""

    def __init__(self):
        self._servers: Dict[str, MCPServerConnection] = {}
        self._initialized = False

    async def initialize(self):
        """모든 MCP 서버 병렬 연결"""
        if self._initialized:
            return

        # venv 내 pykrx-mcp 경로
        venv_bin = os.path.join(os.path.dirname(sys.executable), "pykrx-mcp")

        configs = [
            MCPServerConfig(
                name="pykrx",
                command=venv_bin,
                args=[],
                env=None,
                startup_timeout=20.0,
                call_timeout=10.0,
                max_retries=3,
            ),
            MCPServerConfig(
                name="naver_search",
                command="npx",
                args=["-y", "@isnow890/naver-search-mcp"],
                env=dict(os.environ),  # dotenv 로드된 환경 명시적 전달 (env=None은 MCP SDK에서 미전달)
                startup_timeout=30.0,
                call_timeout=15.0,
                max_retries=3,
            ),
        ]

        # 병렬 연결 (하나 실패해도 나머지 진행)
        connections = []
        for cfg in configs:
            conn = MCPServerConnection(cfg)
            self._servers[cfg.name] = conn
            connections.append(conn.connect())

        results = await asyncio.gather(*connections, return_exceptions=True)

        for cfg, result in zip(configs, results):
            if isinstance(result, Exception):
                logger.warning(f"[MCP] {cfg.name} 초기화 예외: {result}")
            elif not result:
                logger.warning(f"[MCP] {cfg.name} 초기화 실패 (무시)")

        self._initialized = True
        logger.info("[MCP] MCPClientManager 초기화 완료")

    async def shutdown(self):
        """모든 MCP 서버 연결 해제"""
        for name, conn in self._servers.items():
            try:
                await conn.disconnect()
                logger.debug(f"[MCP] {name} 서버 연결 해제")
            except Exception as e:
                logger.error(f"[MCP] {name} 종료 실패: {e}")

        self._servers.clear()
        self._initialized = False
        logger.info("[MCP] MCPClientManager 종료 완료")

    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Optional[Any]:
        """MCP 도구 호출 (실패 시 None 반환, graceful)"""
        conn = self._servers.get(server_name)
        if conn is None:
            logger.debug(f"[MCP] 서버 '{server_name}' 미등록")
            return None

        return await conn.call_tool(tool_name, arguments)

    def is_server_available(self, name: str) -> bool:
        """서버 연결 상태 확인"""
        conn = self._servers.get(name)
        return conn is not None and conn.connected


# 전역 싱글톤
_mcp_manager: Optional[MCPClientManager] = None


def get_mcp_manager() -> MCPClientManager:
    """전역 MCPClientManager 싱글톤 반환"""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPClientManager()
    return _mcp_manager
