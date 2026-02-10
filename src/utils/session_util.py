"""
시장 세션 유틸리티

중복된 세션 체크 로직을 통합 관리합니다.
"""

from datetime import date, datetime, time
from typing import Tuple

from src.core.types import MarketSession, TradingConfig


class SessionUtil:
    """시장 세션 관리 유틸리티"""

    # 세션 시간대 (시:분 튜플)
    PRE_MARKET_START = (8, 0)
    PRE_MARKET_END = (8, 50)
    REGULAR_START = (9, 0)
    REGULAR_END = (15, 20)  # 동시호가 전까지
    NEXT_START = (15, 40)  # 10분 휴장 후
    NEXT_END = (20, 0)

    @staticmethod
    def get_current_session() -> MarketSession:
        """현재 시장 세션 반환

        Returns:
            MarketSession: 현재 세션 (PRE_MARKET, REGULAR, NEXT, CLOSED)
        """
        now = datetime.now()

        # 주말 + 공휴일 (engine.py의 정확한 휴장일 함수 사용)
        from src.core.engine import is_kr_market_holiday
        if is_kr_market_holiday(now.date()):
            return MarketSession.CLOSED

        hour = now.hour
        minute = now.minute
        time_int = hour * 100 + minute  # HHMM 형식

        # 프리장: 08:00 ~ 08:50
        if 800 <= time_int < 850:
            return MarketSession.PRE_MARKET

        # 정규장: 09:00 ~ 15:20 (장마감 동시호가 전까지)
        if 900 <= time_int < 1520:
            return MarketSession.REGULAR

        # 15:20~15:40: CLOSED (장마감 동시호가 + 휴장)

        # 넥스트장: 15:40 ~ 20:00
        if 1540 <= time_int < 2000:
            return MarketSession.NEXT

        return MarketSession.CLOSED

    @staticmethod
    def is_trading_hours(config: TradingConfig) -> bool:
        """거래 가능 시간 여부

        Args:
            config: 트레이딩 설정 (enable_pre_market, enable_next_market)

        Returns:
            bool: 거래 가능 여부
        """
        session = SessionUtil.get_current_session()

        if session == MarketSession.CLOSED:
            return False

        if session == MarketSession.PRE_MARKET and not config.enable_pre_market:
            return False

        if session == MarketSession.NEXT and not config.enable_next_market:
            return False

        return True

    @staticmethod
    def get_session_time_range(session: MarketSession) -> Tuple[time, time]:
        """세션의 시작/종료 시각 반환

        Args:
            session: 세션 타입

        Returns:
            (시작시각, 종료시각) 튜플
        """
        if session == MarketSession.PRE_MARKET:
            return (
                time(*SessionUtil.PRE_MARKET_START),
                time(*SessionUtil.PRE_MARKET_END),
            )
        elif session == MarketSession.REGULAR:
            return (
                time(*SessionUtil.REGULAR_START),
                time(*SessionUtil.REGULAR_END),
            )
        elif session == MarketSession.NEXT:
            return (
                time(*SessionUtil.NEXT_START),
                time(*SessionUtil.NEXT_END),
            )
        else:
            # CLOSED는 범위가 없음
            return (time(0, 0), time(0, 0))

    @staticmethod
    def time_to_session_end(session: MarketSession) -> int:
        """현재 세션 종료까지 남은 시간(초)

        Args:
            session: 현재 세션

        Returns:
            int: 남은 시간(초), CLOSED면 0
        """
        if session == MarketSession.CLOSED:
            return 0

        now = datetime.now()
        _, end_time = SessionUtil.get_session_time_range(session)

        end_datetime = datetime.combine(now.date(), end_time)
        if end_datetime < now:
            return 0

        return int((end_datetime - now).total_seconds())

    @staticmethod
    def format_session(session: MarketSession) -> str:
        """세션을 한글 문자열로 변환

        Args:
            session: 세션 타입

        Returns:
            str: 세션 이름 (예: "정규장", "프리장")
        """
        session_names = {
            MarketSession.PRE_MARKET: "프리장",
            MarketSession.REGULAR: "정규장",
            MarketSession.NEXT: "넥스트장",
            MarketSession.CLOSED: "휴장",
        }
        return session_names.get(session, "알 수 없음")
