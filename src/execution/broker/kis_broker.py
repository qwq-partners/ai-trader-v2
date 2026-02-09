"""
AI Trading Bot v2 - KIS (한국투자증권) 브로커

실제 KIS Open API를 사용하여 주문을 실행합니다.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import aiohttp
from loguru import logger

from .base import BaseBroker
from ...core.types import (
    Order, Fill, Position, OrderSide, OrderStatus, OrderType, MarketSession
)
from ...utils.kis_token_manager import get_token_manager


@dataclass
class KISConfig:
    """KIS API 설정"""
    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""           # 계좌번호 (CANO)
    account_product_cd: str = "01"  # 계좌상품코드
    env: str = "prod"              # prod / dev(모의투자)

    # API 기본 URL
    base_url: str = field(default="")

    # 타임아웃
    timeout_seconds: int = 15

    def __post_init__(self):
        if not self.base_url:
            if self.env == "prod":
                self.base_url = "https://openapi.koreainvestment.com:9443"
            else:
                self.base_url = "https://openapivts.koreainvestment.com:29443"

    @classmethod
    def from_env(cls) -> "KISConfig":
        """환경변수에서 설정 로드"""
        return cls(
            app_key=os.getenv("KIS_APPKEY", "") or os.getenv("KIS_APP_KEY", ""),
            app_secret=os.getenv("KIS_APPSECRET", "") or os.getenv("KIS_SECRET_KEY", ""),
            account_no=os.getenv("KIS_CANO", ""),
            account_product_cd=os.getenv("KIS_ACNT_PRDT_CD", "01"),
            env=os.getenv("KIS_ENV", "prod"),
            timeout_seconds=int(os.getenv("KIS_API_TIMEOUT_SECONDS", "15")),
        )


class KISBroker(BaseBroker):
    """
    KIS (한국투자증권) 브로커

    실제 KIS Open API를 사용하여 주문을 실행합니다.

    지원 거래:
    - 정규장 (09:00~15:30): 일반 매매
    - 프리장 (08:00~08:50): 시간외 단일가 (NXT)
    - 넥스트장 (15:30~20:00): 시간외 단일가 (NXT)
    """

    def __init__(self, config: Optional[KISConfig] = None):
        self.config = config or KISConfig.from_env()
        self._session: Optional[aiohttp.ClientSession] = None
        self._token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

        # 주문 추적
        self._pending_orders: Dict[str, Order] = {}
        self._order_id_to_kis_no: Dict[str, str] = {}
        self._order_id_to_orgno: Dict[str, str] = {}

        # NXT 거래 가능 종목 캐시
        self._nxt_symbols_cache: List[str] = []
        self._nxt_cache_updated: Optional[datetime] = None

        # 토큰 매니저
        self._token_manager = get_token_manager()

        # API 레이트 리미터 (초당 max_rps 호출 제한)
        self._rate_limit_lock = asyncio.Lock()
        self._api_call_times: collections.deque = collections.deque(maxlen=20)
        self._max_rps = 18  # KIS API 초당 최대 호출 수 (안전 마진 포함)

        # 검증
        if not self.config.app_key or not self.config.app_secret:
            raise ValueError("KIS_APPKEY와 KIS_APPSECRET이 설정되지 않았습니다.")
        if not self.config.account_no:
            raise ValueError("KIS_CANO(계좌번호)가 설정되지 않았습니다.")

        logger.info(
            f"KISBroker 초기화: env={self.config.env}, "
            f"account=****{self.config.account_no[-4:]}"
        )

    # ============================================================
    # API 레이트 리미팅
    # ============================================================

    async def _rate_limit(self):
        """API 호출 전 레이트 리미트 대기 (슬라이딩 윈도우)"""
        async with self._rate_limit_lock:
            now = time.monotonic()
            # 1초 이내 호출 기록만 유지
            while self._api_call_times and now - self._api_call_times[0] > 1.0:
                self._api_call_times.popleft()
            # 초당 호출 한도 도달 시 대기
            if len(self._api_call_times) >= self._max_rps:
                wait_time = 1.0 - (now - self._api_call_times[0])
                if wait_time > 0:
                    logger.debug(f"[레이트 리밋] {wait_time:.3f}초 대기 (초당 {self._max_rps}건 제한)")
                    await asyncio.sleep(wait_time)
            self._api_call_times.append(time.monotonic())

    # ============================================================
    # 연결 관리
    # ============================================================

    async def connect(self) -> bool:
        """KIS API 연결 및 토큰 발급"""
        try:
            # HTTP 세션 생성
            if not self._session or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
                self._session = aiohttp.ClientSession(timeout=timeout)

            # 토큰 발급
            if not await self._ensure_token():
                logger.error("KIS 토큰 발급 실패")
                return False

            logger.info("KIS API 연결 완료")
            return True

        except asyncio.TimeoutError:
            logger.error("KIS 연결 타임아웃")
            return False
        except aiohttp.ClientError as e:
            logger.error(f"KIS HTTP 클라이언트 오류: {e}")
            return False
        except (ValueError, KeyError) as e:
            logger.error(f"KIS 설정 오류: {e}")
            return False
        except Exception as e:
            # 예상치 못한 오류만 여기서 처리
            logger.exception(f"KIS 연결 실패 (예상치 못한 오류): {e}")
            return False

    async def disconnect(self) -> None:
        """연결 해제"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("KIS API 연결 해제")

    @property
    def is_connected(self) -> bool:
        """연결 상태 (토큰 유효성 포함)"""
        if self._session is None or self._session.closed:
            return False
        if self._token is None:
            return False
        # 토큰 매니저의 유효성 체크 활용 (만료 5분 전이면 갱신 필요)
        return self._token_manager._is_token_valid()

    # ============================================================
    # 토큰 관리 (토큰 매니저 사용)
    # ============================================================

    async def _ensure_token(self) -> bool:
        """토큰 유효성 확인 및 갱신 (지수 백오프 재시도)"""
        for attempt in range(3):
            self._token = await self._token_manager.get_access_token()
            if self._token is not None:
                return True
            # 지수 백오프: 1초, 2초, 4초
            delay = 2 ** attempt
            logger.warning(f"[토큰] 발급 실패 (시도 {attempt + 1}/3), {delay}초 후 재시도")
            await asyncio.sleep(delay)
        logger.error("[토큰] 3회 재시도 후에도 토큰 발급 실패")
        return False

    # ============================================================
    # HTTP 헬퍼
    # ============================================================

    def _get_headers(self, tr_id: str) -> Dict[str, str]:
        """API 호출 헤더 생성"""
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
        }

    def _is_token_error(self, data: dict) -> bool:
        """토큰 관련 오류 여부 확인"""
        msg_cd = str(data.get("msg_cd", ""))
        # EGW00123: Access Token 만료, EGW00121: 유효하지 않은 Access Token
        return msg_cd in ("EGW00123", "EGW00121")

    async def _api_get(self, url: str, tr_id: str, params: dict) -> dict:
        """API GET 요청 (토큰 만료 시 자동 갱신 + 재시도, 일시적 오류 재시도)"""
        if not self._session or self._session.closed:
            logger.warning("[API] 세션 없음, 재연결 시도")
            if not await self.connect():
                return {"rt_cd": "-1", "msg1": "세션 연결 실패"}
        if self._token is None:
            logger.warning("[API] 토큰 없음, 갱신 시도")
            if not await self._ensure_token():
                return {"rt_cd": "-1", "msg1": "토큰 발급 실패"}
        for attempt in range(3):
            try:
                await self._rate_limit()
                headers = self._get_headers(tr_id)
                async with self._session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 401 and attempt < 2:
                        logger.warning("[토큰] 401 응답, 토큰 갱신 시도")
                        await self._ensure_token()
                        continue
                    if resp.status in (429, 500, 502, 503) and attempt < 2:
                        wait = 2 ** attempt  # 지수 백오프: 1초, 2초, 4초
                        logger.warning(f"[API] HTTP {resp.status}, {attempt+1}회 재시도 ({wait}초 대기)")
                        await asyncio.sleep(wait)
                        continue
                    try:
                        data = await resp.json()
                    except Exception:
                        logger.warning(f"[API] JSON 파싱 실패 (status={resp.status})")
                        return {"rt_cd": "-1", "msg1": f"JSON 파싱 실패 (HTTP {resp.status})"}
                    if self._is_token_error(data) and attempt < 2:
                        logger.warning(f"[토큰] 토큰 오류 감지 ({data.get('msg_cd')}), 갱신 시도")
                        await self._ensure_token()
                        continue
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    wait = 2 ** attempt
                    logger.warning(f"[API] 네트워크 오류, {attempt+1}회 재시도 ({wait}초 대기): {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"[API] GET 실패 (3회 시도): {e}")
                return {"rt_cd": "-1", "msg1": f"네트워크 오류: {e}"}
        return {"rt_cd": "-1", "msg1": "API 호출 실패 (최대 재시도 초과)"}

    async def _api_post(self, url: str, tr_id: str, json_data: dict,
                        extra_headers: Optional[dict] = None) -> dict:
        """API POST 요청 (토큰 만료 시 자동 갱신 + 재시도, 일시적 오류 재시도)"""
        if not self._session or self._session.closed:
            logger.warning("[API] 세션 없음, 재연결 시도")
            if not await self.connect():
                return {"rt_cd": "-1", "msg1": "세션 연결 실패"}
        if self._token is None:
            logger.warning("[API] 토큰 없음, 갱신 시도")
            if not await self._ensure_token():
                return {"rt_cd": "-1", "msg1": "토큰 발급 실패"}
        for attempt in range(3):
            try:
                await self._rate_limit()
                headers = self._get_headers(tr_id)
                if extra_headers:
                    headers.update(extra_headers)
                async with self._session.post(url, headers=headers, json=json_data) as resp:
                    if resp.status == 401 and attempt < 2:
                        logger.warning("[토큰] 401 응답, 토큰 갱신 시도")
                        await self._ensure_token()
                        continue
                    if resp.status in (429, 500, 502, 503) and attempt < 2:
                        wait = 2 ** attempt  # 지수 백오프: 1초, 2초, 4초
                        logger.warning(f"[API] HTTP {resp.status}, {attempt+1}회 재시도 ({wait}초 대기)")
                        await asyncio.sleep(wait)
                        continue
                    try:
                        data = await resp.json()
                    except Exception:
                        logger.warning(f"[API] JSON 파싱 실패 (status={resp.status})")
                        return {"rt_cd": "-1", "msg1": f"JSON 파싱 실패 (HTTP {resp.status})"}
                    if self._is_token_error(data) and attempt < 2:
                        logger.warning(f"[토큰] 토큰 오류 감지 ({data.get('msg_cd')}), 갱신 시도")
                        await self._ensure_token()
                        continue
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    wait = 2 ** attempt
                    logger.warning(f"[API] 네트워크 오류, {attempt+1}회 재시도 ({wait}초 대기): {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"[API] POST 실패 (3회 시도): {e}")
                return {"rt_cd": "-1", "msg1": f"네트워크 오류: {e}"}
        return {"rt_cd": "-1", "msg1": "API 호출 실패 (최대 재시도 초과)"}

    async def _get_hashkey(self, params: Dict[str, Any]) -> Optional[str]:
        """주문 API용 hashkey 발급 (최대 3회 재시도)"""
        url = f"{self.config.base_url}/uapi/hashkey"
        headers = {
            "Content-Type": "application/json",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        for attempt in range(3):
            try:
                await self._rate_limit()
                async with self._session.post(url, headers=headers, json=params) as resp:
                    if resp.status != 200:
                        if attempt < 2:
                            logger.warning(f"Hashkey 발급 실패 (HTTP {resp.status}), 재시도 {attempt + 1}/3")
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                        return None
                    data = await resp.json()
                    return data.get("HASH")
            except Exception as e:
                if attempt < 2:
                    logger.warning(f"Hashkey 발급 오류 ({e}), 재시도 {attempt + 1}/3")
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    logger.error(f"Hashkey 발급 실패 (3회 재시도 소진): {e}")
        return None

    # ============================================================
    # 주문 실행
    # ============================================================

    async def submit_order(self, order: Order) -> Tuple[bool, str]:
        """주문 제출"""
        if not self.is_connected:
            if not await self.connect():
                return False, "연결 실패"

        try:
            # 현재 세션 확인
            session = self._get_current_market_session()

            # 세션별 거래 가능 여부 체크
            if session == "closed":
                return False, "장 마감 시간입니다 (거래 불가)"
            elif session == "break":
                return False, "휴장 시간입니다 (15:30~15:40)"

            # 동시호가 세션 처리
            if session in ("pre_close", "closing"):
                # 동시호가는 지정가만 가능
                if order.order_type == OrderType.MARKET:
                    return False, f"동시호가 시간에는 시장가 주문 불가 ({session})"

            # NXT 세션(프리장/넥스트장)에서 NXT 거래 불가 종목 체크
            if session in ("pre_market", "next_market"):
                nxt_symbols = await self.get_nxt_symbols()
                if order.symbol.zfill(6) not in [s.zfill(6) for s in nxt_symbols]:
                    logger.warning(f"NXT 거래 불가 종목: {order.symbol} (세션: {session})")
                    return False, f"{order.symbol}은(는) NXT 거래 불가 종목입니다"

            # TR ID 결정 (세션별)
            tr_id = self._get_tr_id_for_session(order.side)

            # 주문 구분 결정
            ord_dvsn = self._get_order_division(order)

            # 주문 가격
            if order.order_type == OrderType.MARKET:
                ord_unpr = "0"
            elif order.price:
                ord_unpr = str(self.round_to_tick(float(order.price)))
            else:
                return False, "지정가 주문에 가격이 필요합니다"

            # 파라미터
            params = {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_cd,
                "PDNO": order.symbol.zfill(6),
                "ORD_DVSN": ord_dvsn,
                "ORD_QTY": str(order.quantity),
                "ORD_UNPR": ord_unpr,
                "CTAC_TLNO": "",
                "SLL_TYPE": "01" if order.side == OrderSide.SELL else "",
                "ALGO_NO": "",
            }

            # 시간외 단일가 설정 (프리장/넥스트장)
            session = self._get_current_market_session()
            if session in ("pre_market", "next_market"):
                params["AFHR_FLPR_YN"] = "Y"  # 시간외단일가여부
                logger.debug(f"시간외 단일가 주문 (세션: {session})")

            # Hashkey
            hashkey = await self._get_hashkey(params)
            if not hashkey:
                return False, "Hashkey 발급 실패"

            # API 호출
            url = f"{self.config.base_url}/uapi/domestic-stock/v1/trading/order-cash"
            data = await self._api_post(url, tr_id, params, extra_headers={"hashkey": hashkey})

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                msg = data.get("msg1", "알 수 없는 오류")
                msg_cd = data.get("msg_cd", "")
                logger.error(f"주문 실패: [{msg_cd}] {msg}")
                return False, f"[{msg_cd}] {msg}"

            # 주문번호 추출
            output = data.get("output", {})
            if isinstance(output, list):
                output = output[0] if output else {}

            kis_ord_no = output.get("ODNO") or output.get("odno", "")
            orgno = output.get("KRX_FWDG_ORD_ORGNO") or output.get("ORGNO", "")

            if not kis_ord_no:
                kis_ord_no = f"TEMP_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

            # 주문 추적
            order.status = OrderStatus.SUBMITTED
            order.broker_order_id = kis_ord_no
            order.updated_at = datetime.now()

            self._pending_orders[order.id] = order
            self._order_id_to_kis_no[order.id] = kis_ord_no
            if orgno:
                self._order_id_to_orgno[order.id] = orgno

            logger.info(
                f"주문 제출 성공: {order.symbol} {order.side.value} "
                f"{order.quantity}주 @ {ord_unpr}원 -> KIS#{kis_ord_no}"
            )
            return True, kis_ord_no

        except Exception as e:
            logger.exception(f"주문 제출 오류: {e}")
            return False, str(e)

    def _get_order_division(self, order: Order) -> str:
        """
        주문 구분 코드 결정

        ORD_DVSN 코드:
        - 00: 지정가
        - 01: 시장가
        - 02: 조건부지정가
        - 05: 장전 시간외 (프리장/넥스트장에서 시간외 단일가)
        - 06: 장후 시간외 (사용 안함 - 05로 통일)
        """
        session = self._get_current_market_session()

        # 프리장/넥스트장은 시간외 단일가 (05)
        if session in ("pre_market", "next_market"):
            return "05"

        # 정규장
        if order.order_type == OrderType.MARKET:
            return "01"

        if order.order_type == OrderType.LIMIT:
            return "00"

        return "01"

    def _get_current_market_session(self) -> str:
        """현재 장 세션 판단 (정규장/프리장/넥스트장)"""
        now = datetime.now()
        hour, minute = now.hour, now.minute
        time_val = hour * 100 + minute

        # 세션 시간대 (KRX 기준)
        # 프리장: 08:00 ~ 08:50
        # 동시호가: 08:50 ~ 09:00 (정규장 전 동시호가)
        # 정규장: 09:00 ~ 15:20
        # 장마감 동시호가: 15:20 ~ 15:30
        # 넥스트장: 15:40 ~ 20:00 (10분 휴장 후)

        if 800 <= time_val < 850:
            return "pre_market"   # 프리장
        elif 850 <= time_val < 900:
            return "pre_close"    # 동시호가 (프리장 → 정규장 전환)
        elif 900 <= time_val < 1520:
            return "regular"      # 정규장
        elif 1520 <= time_val < 1530:
            return "closing"      # 장마감 동시호가
        elif 1530 <= time_val < 1540:
            return "break"        # 휴장 (정규장 → 넥스트장 전환)
        elif 1540 <= time_val < 2000:
            return "next_market"  # 넥스트장
        else:
            return "closed"

    def _get_tr_id_for_session(self, side: OrderSide) -> str:
        """
        주문 TR ID 반환

        국내주식 현금주문:
        - 매수: TTTC0802U
        - 매도: TTTC0801U

        시간외 단일가(NXT)도 동일한 TR ID 사용
        ORD_DVSN="05"와 AFHR_FLPR_YN="Y"로 시간외 주문 구분
        """
        if side == OrderSide.BUY:
            return "TTTC0802U"
        else:
            return "TTTC0801U"

    async def get_nxt_symbols(self) -> List[str]:
        """
        NXT(시간외 단일가) 거래 가능 종목 조회

        데이터 소스 우선순위:
        1. nextrade.co.kr 크롤링 (공식 NXT 종목)
        2. KIS API 거래량 상위 종목
        3. 기본 하드코딩 목록

        캐시된 데이터를 사용하며, 하루에 한 번 갱신합니다.
        """
        # 캐시 확인 (하루 1회 갱신)
        if self._nxt_symbols_cache and self._nxt_cache_updated:
            if (datetime.now() - self._nxt_cache_updated).days < 1:
                return self._nxt_symbols_cache

        # 1차: nextrade.co.kr에서 크롤링
        try:
            nxt_symbols = await self._fetch_nxt_from_nextrade()
            if nxt_symbols and len(nxt_symbols) > 50:
                self._nxt_symbols_cache = nxt_symbols
                self._nxt_cache_updated = datetime.now()
                logger.info(f"NXT 종목 {len(nxt_symbols)}개 로드 (nextrade.co.kr)")
                return self._nxt_symbols_cache
        except Exception as e:
            logger.warning(f"nextrade.co.kr 크롤링 실패: {e}")

        # 2차: KIS API에서 대형주 조회
        try:
            kospi200 = await self._fetch_kospi200_constituents()
            if kospi200:
                self._nxt_symbols_cache = kospi200
                self._nxt_cache_updated = datetime.now()
                return self._nxt_symbols_cache

        except Exception as e:
            logger.warning(f"NXT 종목 조회 실패: {e}")

        # 3차: 기본 목록 반환
        if not self._nxt_symbols_cache:
            self._nxt_symbols_cache = self._get_default_nxt_symbols()
            self._nxt_cache_updated = datetime.now()

        return self._nxt_symbols_cache

    async def _fetch_nxt_from_nextrade(self) -> List[str]:
        """
        nextrade.co.kr에서 NXT 거래 가능 종목 크롤링

        공식 NXT 종목 데이터를 API 엔드포인트에서 가져옵니다.
        """
        url = "https://www.nextrade.co.kr/brdinfoTime/brdinfoTimeList.do"

        # 세션 생성 (필요시)
        session = self._session
        if not session or session.closed:
            session = aiohttp.ClientSession()
            close_session = True
        else:
            close_session = False

        try:
            symbols = []
            page = 1
            page_size = 100

            while True:
                payload = {
                    "pageIndex": str(page),
                    "pageUnit": str(page_size),
                    "searchKeyword": "",
                }

                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }

                async with session.post(url, data=payload, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        logger.warning(f"nextrade API 오류: {resp.status}")
                        break

                    data = await resp.json(content_type=None)
                    items = data.get("brdinfoTimeList", [])

                    if not items:
                        break

                    for item in items:
                        code = item.get("isuSrdCd", "")
                        if code:
                            # 종목코드 정리 (A005930 -> 005930)
                            if code.startswith("A"):
                                code = code[1:]
                            symbols.append(code.zfill(6))

                    # 다음 페이지가 있는지 확인
                    if len(items) < page_size:
                        break

                    page += 1

                    # 최대 10페이지 (1000개 종목)
                    if page > 10:
                        break

            return symbols

        except asyncio.TimeoutError:
            logger.warning("nextrade.co.kr 타임아웃")
            return []
        except Exception as e:
            logger.warning(f"nextrade 크롤링 오류: {e}")
            return []
        finally:
            if close_session and session:
                await session.close()

    async def _fetch_kospi200_constituents(self) -> List[str]:
        """코스피200 구성 종목 조회 (NXT 거래 가능 대상)"""
        if not self.is_connected:
            return []

        try:
            # 코스피200 ETF(069500)의 구성 종목을 통해 간접 조회
            # 또는 상위 종목 목록 API 사용
            # 여기서는 거래량 상위 종목을 대형주로 간주

            tr_id = "FHKST01010200"  # 거래량 상위
            url = f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"

            params = {
                "FID_COND_MRKT_DIV_CODE": "J",  # 전체
                "FID_COND_SCR_DIV_CODE": "20101",
                "FID_INPUT_ISCD": "0001",  # KOSPI
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": "",
            }

            data = await self._api_get(url, tr_id, params)

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                return []

            output = data.get("output", [])

            symbols = []
            for item in output[:100]:  # 상위 100개
                code = item.get("mksc_shrn_iscd", "")
                if code:
                    symbols.append(code.zfill(6))

            if symbols:
                logger.info(f"대형주 {len(symbols)}개 조회 완료 (NXT 가능)")
                return symbols

        except Exception as e:
            logger.debug(f"코스피200 조회 실패: {e}")

        return []

    def _get_default_nxt_symbols(self) -> List[str]:
        """기본 NXT 거래 가능 종목 (대형주 + ETF)"""
        # 코스피200 상위 + 주요 ETF
        return [
            # 대형주 (시가총액 상위)
            "005930", "000660", "005380", "035420", "000270",  # 삼성, SK하이닉스, 현대차, 네이버, 기아
            "005490", "035720", "051910", "006400", "028260",  # POSCO, 카카오, LG화학, 삼성SDI, 삼성물산
            "207940", "068270", "096770", "003670", "034730",  # 삼바, 셀트리온, SK이노, 포스코퓨처엠, SK
            "066570", "055550", "012330", "105560", "032830",  # LG전자, 신한지주, 현대모비스, KB금융, 삼성생명
            "018260", "316140", "323410", "011200", "017670",  # 삼성SDS, 우리금융, 카카오뱅크, HMM, SK텔레콤
            "009150", "015760", "010950", "010130", "086790",  # 삼성전기, 한국전력, S-Oil, 고려아연, 하나금융
            "033780", "024110", "034220", "011070", "352820",  # KT&G, 기업은행, LG디스플레이, LG이노텍, 하이브
            "047050", "003490", "000810", "030200", "036570",  # POSCO인터내셔널, 대한항공, 삼성화재, KT, 엔씨소프트
            "022100", "009540", "329180", "003550", "402340",  # 포스코DX, 한국조선해양, 현대건설, LG, SK스퀘어
            "373220", "003410", "090430", "004020", "241560",  # LG에너지솔루션, 쌍용씨앤이, 아모레퍼시픽, 현대제철, 두산밥캣
            # 주요 ETF
            "069500", "102110", "233740", "114800", "122630",  # KODEX200, TIGER200, 코스닥150레버리지, 인버스, KODEX레버리지
            "252670", "091160", "091170", "229200", "305540",  # KODEX200선물인버스2X, KODEX반도체, 코덱스은행, KODEX코스닥150, TIGER 2차전지테마
            "364970", "371460", "395160", "261240", "278530",  # KODEX 2차전지, TIGER 2차전지TOP10, 삼성퓨처모빌리티, KODEX 코스피, KODEX 미국S&P500
        ]

    async def cancel_order(self, order_id: str) -> bool:
        """주문 취소"""
        if order_id not in self._pending_orders:
            logger.warning(f"취소할 주문을 찾을 수 없음: {order_id}")
            return False

        if not self.is_connected:
            if not await self.connect():
                return False

        try:
            order = self._pending_orders[order_id]
            kis_ord_no = self._order_id_to_kis_no.get(order_id, "")
            orgno = self._order_id_to_orgno.get(order_id, "")

            if not kis_ord_no:
                logger.error(f"KIS 주문번호 없음: {order_id}")
                return False

            tr_id = "TTTC0803U"  # 정정취소

            params = {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_cd,
                "KRX_FWDG_ORD_ORGNO": orgno,
                "ORGN_ODNO": kis_ord_no,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",  # 취소
                "ORD_QTY": str(order.quantity),
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",
            }

            hashkey = await self._get_hashkey(params)
            if not hashkey:
                return False

            url = f"{self.config.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
            data = await self._api_post(url, tr_id, params, extra_headers={"hashkey": hashkey})

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                msg = data.get("msg1", "")
                logger.error(f"주문 취소 실패: {msg}")
                return False

            # 추적에서 제거
            self._pending_orders.pop(order_id, None)
            self._order_id_to_kis_no.pop(order_id, None)
            self._order_id_to_orgno.pop(order_id, None)

            logger.info(f"주문 취소 성공: {order_id} (KIS#{kis_ord_no})")
            return True

        except Exception as e:
            logger.exception(f"주문 취소 오류: {e}")
            return False

    async def cancel_all_for_symbol(self, symbol: str) -> int:
        """특정 종목의 모든 미체결 주문 취소

        Returns:
            취소된 주문 수
        """
        cancelled = 0
        orders_to_cancel = [
            (oid, order) for oid, order in self._pending_orders.items()
            if order.symbol == symbol and order.is_active
        ]
        for order_id, order in orders_to_cancel:
            try:
                if await self.cancel_order(order_id):
                    cancelled += 1
            except Exception as e:
                logger.warning(f"[KIS] 종목 {symbol} 주문 {order_id} 취소 실패: {e}")
        return cancelled

    async def modify_order(self, order_id: str, new_quantity: Optional[int] = None,
                           new_price: Optional[Decimal] = None) -> bool:
        """주문 수정"""
        if order_id not in self._pending_orders:
            logger.warning(f"수정할 주문을 찾을 수 없음: {order_id}")
            return False

        if not self.is_connected:
            if not await self.connect():
                return False

        try:
            order = self._pending_orders[order_id]
            kis_ord_no = self._order_id_to_kis_no.get(order_id, "")
            orgno = self._order_id_to_orgno.get(order_id, "")

            if not kis_ord_no:
                return False

            tr_id = "TTTC0803U"  # 정정취소

            params = {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_cd,
                "KRX_FWDG_ORD_ORGNO": orgno,
                "ORGN_ODNO": kis_ord_no,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "01",  # 정정
                "ORD_QTY": str(new_quantity or order.quantity),
                "ORD_UNPR": str(self.round_to_tick(float(new_price))) if new_price else "0",
                "QTY_ALL_ORD_YN": "N",
            }

            hashkey = await self._get_hashkey(params)
            if not hashkey:
                return False

            url = f"{self.config.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
            data = await self._api_post(url, tr_id, params, extra_headers={"hashkey": hashkey})

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                msg = data.get("msg1", "")
                logger.error(f"주문 수정 실패: {msg}")
                return False

            # 주문 정보 업데이트
            if new_quantity:
                order.quantity = new_quantity
            if new_price:
                order.price = new_price
            order.updated_at = datetime.now()

            logger.info(f"주문 수정 성공: {order_id}")
            return True

        except Exception as e:
            logger.exception(f"주문 수정 오류: {e}")
            return False

    # ============================================================
    # 조회
    # ============================================================

    async def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """주문 상태 조회"""
        if order_id in self._pending_orders:
            return self._pending_orders[order_id].status
        return None

    async def get_open_orders(self) -> List[Order]:
        """미체결 주문 목록"""
        return list(self._pending_orders.values())

    async def get_positions(self) -> Dict[str, Position]:
        """보유 포지션 조회"""
        if not self.is_connected:
            if not await self.connect():
                return {}

        positions = {}

        try:
            tr_id = "TTTC8434R"
            url = f"{self.config.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"

            params = {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_cd,
                "AFHR_FLPR_YN": "N",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "INQR_DVSN": "01",
                "OFL_YN": "N",
                "PRCS_DVSN": "00",
                "UNPR_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }

            data = await self._api_get(url, tr_id, params)

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                logger.warning(f"포지션 조회 실패: {data.get('msg1', '')}")
                return positions

            output1 = data.get("output1", []) or []

            for item in output1:
                symbol = str(item.get("pdno", "")).zfill(6)
                qty = int(item.get("hldg_qty", "0") or "0")

                if qty > 0:
                    avg_price = Decimal(str(item.get("pchs_avg_pric", "0") or "0"))
                    current_price = Decimal(str(item.get("prpr", "0") or "0"))
                    name = str(item.get("prdt_name", "") or "").strip()

                    positions[symbol] = Position(
                        symbol=symbol,
                        name=name,
                        quantity=qty,
                        avg_price=avg_price,
                        current_price=current_price if current_price > 0 else avg_price,
                    )

            logger.debug(f"포지션 조회 완료: {len(positions)}개")
            return positions

        except Exception as e:
            logger.exception(f"포지션 조회 오류: {e}")
            return positions

    async def get_account_balance(self) -> Dict[str, Any]:
        """
        계좌 잔고 조회

        반환값:
        - total_equity: 실제 총자산 (주문가능금액 + 주식평가액)
        - available_cash: 매수 가능 금액 (미수 없는)
        - deposit: 예수금 총액
        - stock_value: 주식 평가액
        - unrealized_pnl: 평가손익
        - tot_evlu_amt: KIS API 총평가금액 (D+2 정산 등 포함, 참고용)
        """
        if not self.is_connected:
            if not await self.connect():
                return {}

        try:
            # 1. 잔고 조회 API (주식 평가액, 예수금 등)
            tr_id = "TTTC8434R"
            url = f"{self.config.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"

            params = {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_cd,
                "AFHR_FLPR_YN": "N",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "INQR_DVSN": "01",
                "OFL_YN": "N",
                "PRCS_DVSN": "00",
                "UNPR_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }

            data = await self._api_get(url, tr_id, params)

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                logger.warning(f"잔고 조회 실패: {data.get('msg1', '')}")
                return {}

            output2 = data.get("output2", [])
            if not output2:
                return {}

            account_info = output2[0] if isinstance(output2, list) else output2

            # 핵심 금액
            deposit = float(account_info.get("dnca_tot_amt", "0") or "0")  # 예수금 총액
            stock_value = float(account_info.get("scts_evlu_amt", "0") or "0")  # 주식 평가액

            # 평가 손익
            unrealized_pnl = float(account_info.get("evlu_pfls_smtl_amt", "0") or "0")

            # 매입 금액 합계
            purchase_amt = float(account_info.get("pchs_amt_smtl_amt", "0") or "0")

            # KIS API 총평가금액 (D+2 정산 등 포함, 참고용)
            tot_evlu_amt = float(account_info.get("tot_evlu_amt", "0") or "0")

            # 2. 매수가능조회 API (실제 주문 가능 금액)
            available_cash = 0.0
            try:
                tr_id2 = "TTTC8908R"
                url2 = f"{self.config.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"

                params2 = {
                    "CANO": self.config.account_no,
                    "ACNT_PRDT_CD": self.config.account_product_cd,
                    "PDNO": "005930",  # 삼성전자 기준
                    "ORD_UNPR": "0",
                    "ORD_DVSN": "00",  # 지정가 기준 (시장가=01이면 상한가 기준으로 과소계산됨)
                    "CMA_EVLU_AMT_ICLD_YN": "N",
                    "OVRS_ICLD_YN": "N",
                }

                data2 = await self._api_get(url2, tr_id2, params2)
                if str(data2.get("rt_cd", "")) == "0":
                    output = data2.get("output", {})
                    # 미수 없는 매수가능금액 (실제 주문 가능 금액)
                    available_cash = float(output.get("nrcvb_buy_amt", "0") or "0")
            except Exception as e:
                logger.debug(f"매수가능조회 실패: {e}")
                # 실패시 예수금 사용
                available_cash = deposit

            # 실제 총자산 = 주문가능금액 + 주식평가액
            total_equity = available_cash + stock_value

            return {
                "total_equity": total_equity,  # 실제 총자산 (주문가능 + 주식)
                "available_cash": available_cash,  # 매수 가능 금액 (실제 주문 가능)
                "deposit": deposit,  # 예수금 (D+2 정산 전)
                "stock_value": stock_value,  # 주식 평가액
                "purchase_amount": purchase_amt,  # 매입 금액
                "unrealized_pnl": unrealized_pnl,  # 평가 손익
                "tot_evlu_amt": tot_evlu_amt,  # KIS 총평가금액 (참고용)
            }

        except Exception as e:
            logger.exception(f"잔고 조회 오류: {e}")
            return {}

    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        """현재가 조회"""
        if not self.is_connected:
            if not await self.connect():
                return {}

        try:
            tr_id = "FHKST01010100"
            url = f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"

            params = {
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": symbol.zfill(6),
            }

            data = await self._api_get(url, tr_id, params)

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                return {}

            output = data.get("output", {})
            return {
                "symbol": symbol,
                "name": str(output.get("hts_kor_isnm", "") or "").strip(),
                "price": float(output.get("stck_prpr", "0") or "0"),
                "open": float(output.get("stck_oprc", "0") or "0"),
                "high": float(output.get("stck_hgpr", "0") or "0"),
                "low": float(output.get("stck_lwpr", "0") or "0"),
                "prev_close": float(output.get("stck_sdpr", "0") or "0"),
                "volume": int(output.get("acml_vol", "0") or "0"),
                "change": float(output.get("prdy_vrss", "0") or "0"),
                "change_pct": float(output.get("prdy_ctrt", "0") or "0"),
            }

        except Exception as e:
            logger.exception(f"현재가 조회 오류: {e}")
            return {}

    async def get_orderbook(self, symbol: str) -> Dict[str, Any]:
        """호가 조회"""
        if not self.is_connected:
            if not await self.connect():
                return {}

        try:
            tr_id = "FHKST01010200"
            url = f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"

            params = {
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": symbol.zfill(6),
            }

            data = await self._api_get(url, tr_id, params)

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                return {}

            output1 = data.get("output1", {})
            output2 = data.get("output2", {})

            # 호가 추출
            bids = []
            asks = []
            for i in range(1, 11):
                bid_price = float(output1.get(f"bidp{i}", "0") or "0")
                bid_size = int(output1.get(f"bidp_rsqn{i}", "0") or "0")
                ask_price = float(output1.get(f"askp{i}", "0") or "0")
                ask_size = int(output1.get(f"askp_rsqn{i}", "0") or "0")

                if bid_price > 0:
                    bids.append({"price": bid_price, "size": bid_size})
                if ask_price > 0:
                    asks.append({"price": ask_price, "size": ask_size})

            return {
                "symbol": symbol,
                "bids": bids,
                "asks": asks,
                "total_bid_volume": int(output2.get("total_bidp_rsqn", "0") or "0"),
                "total_ask_volume": int(output2.get("total_askp_rsqn", "0") or "0"),
            }

        except Exception as e:
            logger.exception(f"호가 조회 오류: {e}")
            return {}

    async def get_best_bid(self, symbol: str) -> Optional[float]:
        """매수1호가 조회 (매도 시 사용)"""
        orderbook = await self.get_orderbook(symbol)
        bids = orderbook.get("bids", [])
        if bids and bids[0]["price"] > 0:
            return bids[0]["price"]
        return None

    # ============================================================
    # 과거 일봉 데이터 조회
    # ============================================================

    async def get_daily_prices(self, symbol: str, days: int = 60) -> List[Dict[str, Any]]:
        """
        과거 일봉 OHLCV 데이터 조회

        Args:
            symbol: 종목코드
            days: 조회 일수 (기본 60일)

        Returns:
            일봉 데이터 리스트 (오래된 순서)
        """
        if not self.is_connected:
            if not await self.connect():
                return []

        try:
            tr_id = "FHKST03010100"
            url = f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"

            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")

            params = {
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": symbol.zfill(6),
                "fid_input_date_1": start_date,
                "fid_input_date_2": end_date,
                "fid_period_div_code": "D",  # 일봉
                "fid_org_adj_prc": "0",  # 수정주가 미반영
            }

            data = await self._api_get(url, tr_id, params)

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                msg = data.get("msg1", "")
                logger.warning(f"일봉 조회 실패 ({symbol}): {msg}")
                return []

            output2 = data.get("output2", [])
            if not output2:
                return []

            result = []
            for item in output2:
                try:
                    close_p = float(item.get("stck_clpr", "0") or "0")
                    if close_p <= 0:
                        continue
                    result.append({
                        "date": item.get("stck_bsop_date", ""),
                        "open": float(item.get("stck_oprc", "0") or "0"),
                        "high": float(item.get("stck_hgpr", "0") or "0"),
                        "low": float(item.get("stck_lwpr", "0") or "0"),
                        "close": close_p,
                        "volume": int(item.get("acml_vol", "0") or "0"),
                        "value": int(item.get("acml_tr_pbmn", "0") or "0"),
                    })
                except (ValueError, TypeError):
                    continue

            # 오래된 순서로 정렬
            result.sort(key=lambda x: x["date"])

            # 요청 일수만큼 자르기
            return result[-days:]

        except Exception as e:
            logger.error(f"일봉 조회 오류 ({symbol}): {e}")
            return []

    # ============================================================
    # 체결 확인
    # ============================================================

    async def check_fills(self) -> List[Fill]:
        """체결 확인"""
        if not self.is_connected:
            return []

        fills = []

        try:
            tr_id = "TTTC8001R"
            url = f"{self.config.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"

            today = datetime.now().strftime("%Y%m%d")

            params = {
                "CANO": self.config.account_no,
                "ACNT_PRDT_CD": self.config.account_product_cd,
                "INQR_STRT_DT": today,
                "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",
                "ORD_GNO_BRNO": "",
                "CCLD_DVSN": "01",  # 체결
                "INQR_DVSN": "00",
                "INQR_DVSN_1": "",
                "INQR_DVSN_3": "00",
                "EXCG_ID_DVSN_CD": "ALL",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "PDNO": "",
                "ODNO": "",
            }

            data = await self._api_get(url, tr_id, params)

            rt_cd = data.get("rt_cd", "")
            if str(rt_cd) != "0":
                return fills

            output1 = data.get("output1", []) or []

            # KIS 주문번호 -> 내부 주문 ID 매핑
            kis_to_order_id = {v: k for k, v in self._order_id_to_kis_no.items()}

            for item in output1:
                odno = str(item.get("ODNO") or item.get("odno", "")).strip()
                ccld_qty = int(item.get("TOT_CCLD_QTY") or item.get("tot_ccld_qty", "0") or "0")
                ccld_price = float(item.get("AVG_PRVS") or item.get("avg_prvs", "0") or "0")

                if odno in kis_to_order_id and ccld_qty > 0:
                    order_id = kis_to_order_id[odno]
                    order = self._pending_orders.get(order_id)

                    if order:
                        # TOT_CCLD_QTY는 누적 체결수량 → 이전 체결분 차감하여 증분만 처리
                        prev_filled = order.filled_quantity or 0
                        new_qty = ccld_qty - prev_filled

                        if new_qty <= 0:
                            continue  # 이미 처리된 체결, 스킵

                        # 증분 체결가 역산: AVG_PRVS는 누적 평균가이므로
                        # incremental_price = (cum_avg * cum_qty - prev_avg * prev_qty) / new_qty
                        if prev_filled > 0 and order.filled_price:
                            prev_cost = float(order.filled_price) * prev_filled
                            total_cost = ccld_price * ccld_qty
                            incremental_price = (total_cost - prev_cost) / new_qty if new_qty > 0 else ccld_price
                        else:
                            incremental_price = ccld_price  # 첫 체결은 그대로

                        fill_price = Decimal(str(round(incremental_price, 2)))

                        fill = Fill(
                            order_id=order_id,
                            symbol=order.symbol,
                            side=order.side,
                            quantity=new_qty,
                            price=fill_price,
                            commission=self.calculate_commission(
                                order.side, new_qty, fill_price
                            ),
                            strategy=order.strategy,
                            reason=order.reason,
                            signal_score=order.signal_score,
                        )
                        fills.append(fill)

                        # 체결 상태 업데이트 (누적값으로 설정)
                        order.filled_quantity = ccld_qty
                        order.filled_price = Decimal(str(ccld_price))

                        if order.filled_quantity >= order.quantity:
                            order.status = OrderStatus.FILLED
                            # 완전 체결: 추적에서 제거
                            self._pending_orders.pop(order_id, None)
                            self._order_id_to_kis_no.pop(order_id, None)
                            self._order_id_to_orgno.pop(order_id, None)
                        else:
                            order.status = OrderStatus.PARTIAL
                            logger.info(
                                f"[부분체결] {order.symbol} "
                                f"{order.filled_quantity}/{order.quantity}주"
                            )

            return fills

        except Exception as e:
            logger.exception(f"체결 확인 오류: {e}")
            return fills
