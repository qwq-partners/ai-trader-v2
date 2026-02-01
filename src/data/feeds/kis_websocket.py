"""
AI Trading Bot v2 - KIS WebSocket 실시간 데이터 피드

한국투자증권 WebSocket API를 통해 실시간 시세를 수신합니다.

기능:
- 실시간 체결가 (호가)
- 실시간 체결 (틱)
- 호가 잔량
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Callable, Coroutine, Dict, List, Optional, Set, Any
import aiohttp
from loguru import logger

from ...core.types import OrderSide
from ...core.event import MarketDataEvent, QuoteEvent, TickEvent
from ...utils.kis_token_manager import get_token_manager


class KISWebSocketType(str, Enum):
    """WebSocket 구독 타입"""
    PRICE = "H0STCNT0"      # 실시간 체결가
    ORDERBOOK = "H0STASP0"  # 실시간 호가
    NOTICE = "H0STCNI0"     # 체결 통보


@dataclass
class KISWebSocketConfig:
    """WebSocket 설정"""
    app_key: str = ""
    app_secret: str = ""
    env: str = "prod"

    # WebSocket URL
    ws_url: str = field(default="")

    # 재연결 설정
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 10

    # 하트비트
    ping_interval: float = 30.0

    def __post_init__(self):
        if not self.ws_url:
            if self.env == "prod":
                self.ws_url = "ws://ops.koreainvestment.com:21000"
            else:
                self.ws_url = "ws://ops.koreainvestment.com:31000"

    @classmethod
    def from_env(cls) -> "KISWebSocketConfig":
        return cls(
            app_key=os.getenv("KIS_APPKEY", "") or os.getenv("KIS_APP_KEY", ""),
            app_secret=os.getenv("KIS_APPSECRET", "") or os.getenv("KIS_SECRET_KEY", ""),
            env=os.getenv("KIS_ENV", "prod"),
        )


# 콜백 타입
DataCallback = Callable[[MarketDataEvent], Coroutine[Any, Any, None]]
QuoteCallback = Callable[[QuoteEvent], Coroutine[Any, Any, None]]
TickCallback = Callable[[TickEvent], Coroutine[Any, Any, None]]


class MarketSession(str, Enum):
    """장 세션"""
    PRE_MARKET = "pre_market"      # 프리장 (08:00-08:50)
    REGULAR = "regular"            # 정규장 (09:00-15:30)
    NEXT_MARKET = "next_market"    # 넥스트장 (15:30-20:00)
    CLOSED = "closed"              # 장 마감


class KISWebSocketFeed:
    """
    KIS WebSocket 실시간 데이터 피드

    실시간 시세 데이터를 WebSocket으로 수신하여 이벤트로 변환합니다.

    주요 기능:
    - 롤링 구독: 40개 제한 내에서 감시 종목을 순환하며 구독
    - 우선순위: 보유종목(항상) > 점수 높은 종목
    - 세션 인식: 프리장/넥스트장은 NXT 거래 가능 종목만 구독
    """

    # KIS WebSocket 구독 제한
    MAX_SUBSCRIPTIONS = 40  # 최대 동시 구독 종목 수
    ROLLING_INTERVAL = 30   # 롤링 주기 (초)

    def __init__(self, config: Optional[KISWebSocketConfig] = None):
        self.config = config or KISWebSocketConfig.from_env()

        # WebSocket 상태
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._connected = False

        # 구독 종목 관리
        self._subscribed_symbols: Set[str] = set()   # 현재 구독 중
        self._pending_subscriptions: Set[str] = set()

        # 우선순위 구독 관리
        self._priority_symbols: Set[str] = set()     # 보유 종목 (최우선, 항상 구독)
        self._watch_symbols: Set[str] = set()        # 전체 감시 종목
        self._symbol_scores: Dict[str, float] = {}   # 종목별 점수

        # 롤링 구독 관리
        self._rolling_queue: List[str] = []          # 롤링 대기 종목
        self._rolling_index: int = 0                 # 현재 롤링 위치
        self._rolling_task: Optional[asyncio.Task] = None

        # 세션별 종목 관리
        self._current_session: MarketSession = MarketSession.CLOSED
        self._nxt_symbols: Set[str] = set()          # NXT 거래 가능 종목
        self._regular_only_symbols: Set[str] = set()  # 정규장만 거래 가능

        # 콜백
        self._data_callbacks: List[DataCallback] = []
        self._quote_callbacks: List[QuoteCallback] = []
        self._tick_callbacks: List[TickCallback] = []

        # 인증 (토큰 매니저 사용)
        self._token_manager = get_token_manager()
        self._approval_key: Optional[str] = None

        # 통계
        self._message_count = 0
        self._price_data_count = 0
        self._last_message_time: Optional[datetime] = None
        self._reconnect_count = 0
        self._logged_first_price = False

        logger.info(f"KISWebSocketFeed 초기화: env={self.config.env}, 롤링주기={self.ROLLING_INTERVAL}초")

    # ============================================================
    # 콜백 등록
    # ============================================================

    def on_market_data(self, callback: DataCallback):
        """시세 데이터 콜백 등록"""
        self._data_callbacks.append(callback)

    def on_quote(self, callback: QuoteCallback):
        """호가 콜백 등록"""
        self._quote_callbacks.append(callback)

    def on_tick(self, callback: TickCallback):
        """틱 콜백 등록"""
        self._tick_callbacks.append(callback)

    # ============================================================
    # 연결 관리
    # ============================================================

    async def connect(self) -> bool:
        """WebSocket 연결"""
        if self._connected:
            return True

        try:
            # 세션 생성
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()

            # 인증 키 발급 (토큰 매니저 사용)
            self._approval_key = await self._token_manager.get_approval_key()
            if not self._approval_key:
                logger.error("WebSocket 인증 키 발급 실패")
                return False

            # WebSocket 연결
            self._ws = await self._session.ws_connect(
                self.config.ws_url,
                heartbeat=self.config.ping_interval,
                timeout=aiohttp.ClientTimeout(total=15),
            )

            self._connected = True
            self._running = True

            logger.info(f"WebSocket 연결 완료: {self.config.ws_url}")

            # 대기 중인 구독 처리
            for symbol in self._pending_subscriptions:
                await self._subscribe_symbol(symbol)
            self._pending_subscriptions.clear()

            return True

        except Exception as e:
            logger.exception(f"WebSocket 연결 실패: {e}")
            return False

    async def disconnect(self):
        """WebSocket 연결 해제"""
        self._running = False
        self._connected = False

        # 롤링 태스크 중지
        if self._rolling_task:
            self._rolling_task.cancel()
            self._rolling_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()

        if self._session and not self._session.closed:
            await self._session.close()

        logger.info("WebSocket 연결 해제")

    async def _get_approval_key(self) -> Optional[str]:
        """WebSocket 인증 키 발급 (토큰 매니저 사용)"""
        return await self._token_manager.get_approval_key()

    # ============================================================
    # 세션 관리
    # ============================================================

    def set_session(self, session: MarketSession):
        """
        현재 장 세션 설정

        프리장/넥스트장에서는 NXT 거래 가능 종목만 구독합니다.
        """
        if session != self._current_session:
            self._current_session = session
            logger.info(f"[WS] 세션 변경: {session.value}")
            # 세션 변경 시 구독 재구성 필요
            asyncio.create_task(self._rebuild_subscriptions())

    def set_nxt_symbols(self, symbols: List[str]):
        """
        NXT 거래 가능 종목 설정

        프리장/넥스트장에서 거래 가능한 종목 목록입니다.
        (대형주, ETF 등 약 400여 종목)
        """
        self._nxt_symbols = set(s.zfill(6) for s in symbols)
        logger.info(f"[WS] NXT 거래 가능 종목 {len(self._nxt_symbols)}개 설정")

    def is_nxt_tradable(self, symbol: str) -> bool:
        """해당 종목이 NXT 거래 가능한지 확인"""
        return symbol.zfill(6) in self._nxt_symbols

    def get_session_symbols(self) -> Set[str]:
        """현재 세션에서 거래 가능한 감시 종목 반환"""
        if self._current_session in (MarketSession.PRE_MARKET, MarketSession.NEXT_MARKET):
            # 프리장/넥스트장: NXT 종목만
            return self._watch_symbols & self._nxt_symbols
        else:
            # 정규장: 전체
            return self._watch_symbols.copy()

    # ============================================================
    # 구독 관리 (롤링 방식)
    # ============================================================

    def set_priority_symbols(self, symbols: List[str]):
        """
        보유 종목 설정 (최우선 구독)

        보유 종목은 항상 구독 상태를 유지하며, 롤링에서 제외됩니다.
        """
        self._priority_symbols = set(s.zfill(6) for s in symbols)
        logger.info(f"[WS] 보유 종목 {len(self._priority_symbols)}개 설정")

    def set_symbol_score(self, symbol: str, score: float):
        """종목 점수 설정 (구독 우선순위용)"""
        self._symbol_scores[symbol.zfill(6)] = score

    def set_symbol_scores(self, scores: Dict[str, float]):
        """종목 점수 일괄 설정"""
        for symbol, score in scores.items():
            self._symbol_scores[symbol.zfill(6)] = score

    async def subscribe(self, symbols: List[str], scores: Dict[str, float] = None):
        """
        종목 구독 (롤링 방식)

        감시 종목 목록에 추가하고, 제한(40개)을 초과하면 롤링 방식으로
        순환 구독합니다.

        롤링 방식:
        - 보유 종목(priority)은 항상 구독 유지
        - 나머지 종목은 점수 순으로 정렬하여 순환
        - ROLLING_INTERVAL마다 일부 종목 교체

        Args:
            symbols: 구독할 종목 코드 목록
            scores: 종목별 점수 (우선순위 결정용)
        """
        # 점수 업데이트
        if scores:
            self.set_symbol_scores(scores)

        # 감시 종목에 추가
        for symbol in symbols:
            symbol = symbol.zfill(6)
            self._watch_symbols.add(symbol)

        # 롤링 큐 갱신
        self._update_rolling_queue()

        # 초기 구독 실행
        await self._apply_subscriptions()

        # 롤링 태스크 시작 (필요시)
        total_watch = len(self.get_session_symbols())
        if total_watch > self.MAX_SUBSCRIPTIONS and not self._rolling_task:
            self._rolling_task = asyncio.create_task(self._rolling_subscription_loop())
            logger.info(f"[WS] 롤링 구독 시작: 총 {total_watch}개 종목 → {self.ROLLING_INTERVAL}초 주기")

    def _update_rolling_queue(self):
        """롤링 대기 큐 갱신 (점수순 정렬)"""
        # 현재 세션에서 거래 가능한 종목
        session_symbols = self.get_session_symbols()

        # 보유 종목 제외
        rollable = session_symbols - self._priority_symbols

        # 점수순 정렬
        self._rolling_queue = sorted(
            rollable,
            key=lambda s: self._symbol_scores.get(s, 0),
            reverse=True
        )

    async def _apply_subscriptions(self):
        """현재 상태에 맞게 구독 적용"""
        # 세션에서 거래 가능한 종목
        session_symbols = self.get_session_symbols()

        # 보유 종목 (항상 구독, 세션 무관하게 - 청산 대비)
        priority = self._priority_symbols.copy()

        # 나머지 슬롯
        available_slots = max(0, self.MAX_SUBSCRIPTIONS - len(priority))

        # 롤링 큐에서 현재 윈도우 선택
        if self._rolling_queue:
            start = self._rolling_index % len(self._rolling_queue)
            end = min(start + available_slots, len(self._rolling_queue))
            window = self._rolling_queue[start:end]

            # wrap around
            if len(window) < available_slots and start > 0:
                remaining = available_slots - len(window)
                window += self._rolling_queue[:remaining]
        else:
            window = []

        # 목표 구독 목록
        target = priority | set(window)

        # 해제할 종목
        to_unsubscribe = self._subscribed_symbols - target
        # 신규 구독할 종목
        to_subscribe = target - self._subscribed_symbols

        # 구독 해제
        for symbol in to_unsubscribe:
            if self._connected:
                await self._unsubscribe_symbol(symbol)
            self._subscribed_symbols.discard(symbol)

        # 신규 구독
        for symbol in to_subscribe:
            if self._connected:
                await self._subscribe_symbol(symbol)
            else:
                self._pending_subscriptions.add(symbol)

        if to_unsubscribe or to_subscribe:
            logger.debug(
                f"[WS] 구독 변경: 해제={len(to_unsubscribe)}, 추가={len(to_subscribe)}, "
                f"현재={len(self._subscribed_symbols)}/{self.MAX_SUBSCRIPTIONS}"
            )

    async def _rolling_subscription_loop(self):
        """롤링 구독 루프"""
        try:
            while self._running:
                await asyncio.sleep(self.ROLLING_INTERVAL)

                # 롤링이 필요한 경우에만 실행
                if len(self._rolling_queue) <= self.MAX_SUBSCRIPTIONS - len(self._priority_symbols):
                    continue

                # 인덱스 이동
                slots = self.MAX_SUBSCRIPTIONS - len(self._priority_symbols)
                self._rolling_index = (self._rolling_index + slots // 2) % max(1, len(self._rolling_queue))

                # 구독 적용
                await self._apply_subscriptions()

                logger.debug(
                    f"[WS] 롤링 교체: idx={self._rolling_index}, "
                    f"대기={len(self._rolling_queue)}, 구독={len(self._subscribed_symbols)}"
                )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[WS] 롤링 루프 오류: {e}")

    async def _rebuild_subscriptions(self):
        """구독 재구성 (세션 변경 시)"""
        self._update_rolling_queue()
        await self._apply_subscriptions()

        # 롤링 태스크 관리
        total_watch = len(self.get_session_symbols())
        need_rolling = total_watch > self.MAX_SUBSCRIPTIONS

        if need_rolling and not self._rolling_task:
            self._rolling_task = asyncio.create_task(self._rolling_subscription_loop())
        elif not need_rolling and self._rolling_task:
            self._rolling_task.cancel()
            self._rolling_task = None

    async def unsubscribe(self, symbols: List[str]):
        """종목 구독 해제 (감시 목록에서 제거)"""
        for symbol in symbols:
            symbol = symbol.zfill(6)

            # 보유 종목은 해제 불가
            if symbol in self._priority_symbols:
                logger.warning(f"[WS] 보유 종목은 구독 해제 불가: {symbol}")
                continue

            # 감시 목록에서 제거
            self._watch_symbols.discard(symbol)
            self._symbol_scores.pop(symbol, None)

            # 현재 구독 중이면 해제
            if symbol in self._subscribed_symbols:
                if self._connected:
                    await self._unsubscribe_symbol(symbol)
                self._subscribed_symbols.discard(symbol)

        # 롤링 큐 갱신
        self._update_rolling_queue()

    def get_subscription_stats(self) -> Dict[str, Any]:
        """구독 통계 반환"""
        session_symbols = self.get_session_symbols()
        return {
            "session": self._current_session.value,
            "total_watch": len(self._watch_symbols),
            "session_tradable": len(session_symbols),
            "priority_count": len(self._priority_symbols),
            "subscribed_count": len(self._subscribed_symbols),
            "rolling_queue_size": len(self._rolling_queue),
            "rolling_index": self._rolling_index,
            "is_rolling": self._rolling_task is not None,
        }

    async def _subscribe_symbol(self, symbol: str):
        """단일 종목 구독"""
        if not self._ws or self._ws.closed:
            return

        try:
            # 실시간 체결가 구독
            message = {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "1",  # 1: 등록
                    "content-type": "utf-8",
                },
                "body": {
                    "input": {
                        "tr_id": KISWebSocketType.PRICE.value,
                        "tr_key": symbol,
                    }
                }
            }

            await self._ws.send_json(message)
            self._subscribed_symbols.add(symbol)

            logger.debug(f"종목 구독: {symbol}")

        except Exception as e:
            logger.error(f"구독 실패 ({symbol}): {e}")

    async def _unsubscribe_symbol(self, symbol: str):
        """단일 종목 구독 해제"""
        if not self._ws or self._ws.closed:
            logger.debug(f"[WS] 구독 해제 스킵 ({symbol}): 연결 없음")
            self._subscribed_symbols.discard(symbol)
            return

        try:
            message = {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "2",  # 2: 해제
                    "content-type": "utf-8",
                },
                "body": {
                    "input": {
                        "tr_id": KISWebSocketType.PRICE.value,
                        "tr_key": symbol,
                    }
                }
            }

            await self._ws.send_json(message)

        except Exception as e:
            logger.error(f"구독 해제 실패 ({symbol}): {e}")

    # ============================================================
    # 메시지 수신
    # ============================================================

    async def run(self):
        """메시지 수신 루프"""
        while self._running:
            try:
                if not self._connected:
                    success = await self.connect()
                    if not success:
                        await asyncio.sleep(self.config.reconnect_delay)
                        continue
                    # 재연결 성공 시 카운터 리셋
                    if self._reconnect_count > 0:
                        logger.info(f"WebSocket 재연결 성공 (시도 {self._reconnect_count}회 후)")
                        self._reconnect_count = 0

                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(msg.data)

                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        logger.warning(f"WebSocket 연결 종료 (close_code={getattr(self._ws, 'close_code', '?')})")
                        break

                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"WebSocket 오류: {msg.data}")
                        break

                    elif msg.type == aiohttp.WSMsgType.CLOSING:
                        logger.warning("WebSocket 연결 종료 중...")

                # async for 정상 종료 (서버가 연결 닫음)
                logger.warning(f"WebSocket 수신 루프 종료 (close_code={getattr(self._ws, 'close_code', '?')}, closed={getattr(self._ws, 'closed', '?')})")

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.exception(f"WebSocket 수신 오류: {e}")

            # 재연결 (지수 백오프, 무제한 재시도)
            if self._running:
                self._connected = False
                self._reconnect_count += 1

                # 지수 백오프: 5s, 10s, 20s, 40s, 최대 120s
                delay = min(
                    self.config.reconnect_delay * (2 ** min(self._reconnect_count - 1, 5)),
                    120.0
                )

                # 10회 초과 시 경고만 (중단하지 않음)
                if self._reconnect_count > self.config.max_reconnect_attempts:
                    if self._reconnect_count % 10 == 0:
                        logger.warning(
                            f"WebSocket 재연결 {self._reconnect_count}회 시도 중 "
                            f"(다음 대기: {delay:.0f}초)"
                        )
                else:
                    logger.info(f"재연결 시도 ({self._reconnect_count})... (대기: {delay:.0f}초)")

                await asyncio.sleep(delay)

        await self.disconnect()

    async def _handle_message(self, data: str):
        """메시지 처리"""
        self._message_count += 1
        self._last_message_time = datetime.now()

        try:
            # JSON 형식 체크
            if data.startswith("{"):
                msg = json.loads(data)
                # 시스템 메시지 (연결 확인 등)
                if "header" in msg:
                    tr_id = msg.get("header", {}).get("tr_id", "")
                    tr_key = msg.get("header", {}).get("tr_key", "")
                    if tr_id == "PINGPONG":
                        return
                # 구독 응답 로깅
                body = msg.get("body", {})
                rt_cd = body.get("rt_cd", "")
                msg_cd = body.get("msg_cd", "")
                msg1 = body.get("msg1", "")
                if rt_cd or msg_cd:
                    logger.debug(f"[WS 응답] rt_cd={rt_cd}, msg_cd={msg_cd}, msg={msg1}")
                # 승인키 만료/오류 감지 → 재연결 트리거
                if msg_cd in ("EGW00123", "EGW00121", "EGW00201"):
                    logger.warning(f"[WS] 승인키 오류 감지 ({msg_cd}: {msg1}), 재연결 시도")
                    self._connected = False
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                return

            # 파이프 구분 데이터 (실시간 시세)
            parts = data.split("|")
            if len(parts) < 4:
                logger.debug(f"[WS] 파이프 구분 데이터 부족: {len(parts)}개 파트")
                return

            # 암호화 여부, TR ID, 데이터 건수, 데이터
            encrypted = parts[0]
            tr_id = parts[1]
            try:
                count = int(parts[2])
            except ValueError:
                logger.warning(f"[WS] 데이터 건수 파싱 실패 (숫자 아님): parts[2]='{parts[2]}'")
                return
            raw_data = parts[3]

            # 수신 통계 로깅 (5000건마다)
            if self._message_count % 5000 == 0:
                logger.info(f"[WS] 메시지 수신 통계: 총 {self._message_count}건, TR={tr_id}")

            # TR ID별 처리
            if tr_id == KISWebSocketType.PRICE.value:
                await self._handle_price_data(raw_data)

            elif tr_id == KISWebSocketType.ORDERBOOK.value:
                await self._handle_orderbook_data(raw_data)

        except Exception as e:
            logger.error(f"메시지 처리 오류: {e}")

    async def _handle_price_data(self, data: str):
        """실시간 체결가 처리"""
        try:
            fields = data.split("^")

            if len(fields) < 20:
                logger.warning(f"[WS] 체결가 필드 부족: {len(fields)}개 (최소 20 필요)")
                return

            # 필드 매핑 (KIS 실시간 체결가 스펙)
            symbol = fields[0].zfill(6)
            time_str = fields[1]          # HHMMSS
            price = int(fields[2])        # 현재가

            # 0원/음수 체결가 필터 (데이터 이상)
            if price <= 0:
                return
            change_sign = fields[3]       # 전일대비부호
            change = int(fields[4])       # 전일대비
            change_pct = float(fields[5]) # 전일대비율
            open_price = int(fields[7])   # 시가
            high_price = int(fields[8])   # 고가
            low_price = int(fields[9])    # 저가
            volume = int(fields[13])      # 누적거래량
            value = int(fields[14])       # 누적거래대금

            self._price_data_count += 1

            # 첫 수신 로그
            if not self._logged_first_price:
                logger.info(f"[WS] 첫 체결가 수신: {symbol} {price:,}원 ({change_pct:+.2f}%) vol={volume:,}")
                self._logged_first_price = True

            # 주기적 로그 (5000건마다)
            if self._price_data_count % 5000 == 0:
                logger.info(f"[WS] 체결가 수신 {self._price_data_count}건째: {symbol} {price:,}원")

            # 이벤트 생성
            event = MarketDataEvent(
                source="kis_websocket",
                symbol=symbol,
                open=Decimal(str(open_price)),
                high=Decimal(str(high_price)),
                low=Decimal(str(low_price)),
                close=Decimal(str(price)),
                volume=volume,
                value=Decimal(str(value)),
                change=Decimal(str(change if change_sign != "5" else -change)),
                change_pct=change_pct if change_sign != "5" else -change_pct,
            )

            # 콜백 호출
            for callback in self._data_callbacks:
                try:
                    await callback(event)
                except Exception as e:
                    logger.error(f"데이터 콜백 오류: {e}")

        except Exception as e:
            logger.error(f"체결가 처리 오류: {e}")

    async def _handle_orderbook_data(self, data: str):
        """실시간 호가 처리"""
        try:
            fields = data.split("^")

            if len(fields) < 40:
                return

            symbol = fields[0].zfill(6)

            # 최우선 호가
            ask_price = int(fields[3])   # 매도1호가
            ask_size = int(fields[4])    # 매도1잔량
            bid_price = int(fields[13])  # 매수1호가
            bid_size = int(fields[14])   # 매수1잔량

            event = QuoteEvent(
                source="kis_websocket",
                symbol=symbol,
                bid_price=Decimal(str(bid_price)),
                bid_size=bid_size,
                ask_price=Decimal(str(ask_price)),
                ask_size=ask_size,
            )

            # 콜백 호출
            for callback in self._quote_callbacks:
                try:
                    await callback(event)
                except Exception as e:
                    logger.error(f"호가 콜백 오류: {e}")

        except Exception as e:
            logger.error(f"호가 처리 오류: {e}")

    # ============================================================
    # 유틸리티
    # ============================================================

    @property
    def is_connected(self) -> bool:
        """연결 상태"""
        return self._connected and self._ws is not None and not self._ws.closed

    def get_stats(self) -> Dict[str, Any]:
        """통계 정보"""
        return {
            "connected": self.is_connected,
            "subscribed_count": len(self._subscribed_symbols),
            "message_count": self._message_count,
            "last_message_time": self._last_message_time.isoformat() if self._last_message_time else None,
            "reconnect_count": self._reconnect_count,
        }
