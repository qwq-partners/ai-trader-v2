"""
AI Trading Bot v2 - 브로커 베이스 클래스

모든 브로커 구현의 추상 인터페이스
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

from ...core.types import Order, Fill, Position, OrderSide, OrderStatus


class BaseBroker(ABC):
    """브로커 추상 베이스 클래스"""

    @abstractmethod
    async def connect(self) -> bool:
        """브로커 연결"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """브로커 연결 해제"""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """연결 상태"""
        pass

    # ============================================================
    # 주문 실행
    # ============================================================

    @abstractmethod
    async def submit_order(self, order: Order) -> Tuple[bool, str]:
        """
        주문 제출

        Args:
            order: 주문 객체

        Returns:
            (성공 여부, 브로커 주문번호 또는 에러 메시지)
        """
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        주문 취소

        Args:
            order_id: 주문 ID

        Returns:
            취소 성공 여부
        """
        pass

    @abstractmethod
    async def modify_order(self, order_id: str, new_quantity: Optional[int] = None,
                           new_price: Optional[Decimal] = None) -> bool:
        """
        주문 수정

        Args:
            order_id: 주문 ID
            new_quantity: 새 수량
            new_price: 새 가격

        Returns:
            수정 성공 여부
        """
        pass

    # ============================================================
    # 조회
    # ============================================================

    @abstractmethod
    async def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """주문 상태 조회"""
        pass

    @abstractmethod
    async def get_open_orders(self) -> List[Order]:
        """미체결 주문 목록"""
        pass

    @abstractmethod
    async def get_positions(self) -> Dict[str, Position]:
        """보유 포지션 조회"""
        pass

    @abstractmethod
    async def get_account_balance(self) -> Dict[str, Any]:
        """계좌 잔고 조회"""
        pass

    # ============================================================
    # 시세
    # ============================================================

    @abstractmethod
    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        """현재가 조회"""
        pass

    @abstractmethod
    async def get_orderbook(self, symbol: str) -> Dict[str, Any]:
        """호가 조회"""
        pass

    # ============================================================
    # 유틸리티
    # ============================================================

    def calculate_commission(self, side: OrderSide, quantity: int, price: Decimal) -> Decimal:
        """수수료 계산 (기본 구현, 오버라이드 가능)"""
        value = price * quantity

        if side == OrderSide.BUY:
            # 매수: 수수료만
            return value * Decimal("0.00015")  # 0.015%
        else:
            # 매도: 수수료 + 세금
            commission = value * Decimal("0.00015")  # 0.015%
            tax = value * Decimal("0.002")  # 0.20% (2026년 기준)
            return commission + tax

    @staticmethod
    def round_to_tick(price: float) -> int:
        """
        호가 단위로 반올림 (한국 주식)

        Args:
            price: 원래 가격

        Returns:
            호가 단위로 반올림된 가격
        """
        if price < 1000:
            tick = 1
        elif price < 5000:
            tick = 5
        elif price < 10000:
            tick = 10
        elif price < 50000:
            tick = 50
        elif price < 100000:
            tick = 100
        elif price < 500000:
            tick = 500
        else:
            tick = 1000

        return int(round(price / tick) * tick)
