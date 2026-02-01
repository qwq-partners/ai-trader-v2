"""
AI Trading Bot v2 - 테마 추종 전략

핫 테마 관련 종목을 추적하고 적시에 진입합니다.

전략 원리:
1. 테마 탐지 시스템에서 핫 테마 수신
2. 테마 관련 종목 중 모멘텀 있는 종목 선별
3. 빠른 진입, 빠른 청산 (테마 쿨다운 전 익절)

적용 시간대:
- 정규장 초반 (09:00~10:30): 테마 확산 구간
- 점심 후 (13:00~14:00): 오후 테마 부각
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set, Any
from loguru import logger

from .base import BaseStrategy, StrategyConfig
from ..core.types import (
    Signal, Position, Theme,
    OrderSide, SignalStrength, StrategyType
)
from ..core.event import MarketDataEvent, ThemeEvent
from ..signals.sentiment.theme_detector import ThemeDetector, ThemeInfo, get_theme_detector


@dataclass
class ThemeChasingConfig(StrategyConfig):
    """테마 추종 전략 설정"""
    name: str = "ThemeChasing"
    strategy_type: StrategyType = StrategyType.THEME_CHASING

    # 테마 조건
    min_theme_score: float = 70.0     # 최소 테마 점수
    max_theme_age_minutes: int = 60   # 테마 신선도 (분)

    # 종목 조건
    min_change_pct: float = 1.0       # 최소 등락률 (%)
    max_change_pct: float = 15.0      # 최대 등락률 (%) - 과열 방지
    min_volume_ratio: float = 1.5     # 최소 거래량 비율

    # 진입 조건
    entry_window_minutes: int = 30    # 테마 발생 후 진입 가능 시간
    max_entries_per_theme: int = 2    # 테마당 최대 진입 수

    # 청산 조건
    stop_loss_pct: float = 1.5        # 손절 (테마는 빠른 손절)
    take_profit_pct: float = 3.0      # 익절
    trailing_stop_pct: float = 1.0    # 트레일링 스탑

    # 시간대 제한
    trading_start_time: str = "09:05" # 시작 시간
    trading_end_time: str = "15:00"   # 종료 시간


class ThemeChasingStrategy(BaseStrategy):
    """
    테마 추종 전략

    핫 테마 감지 시 관련 종목에 빠르게 진입하여
    테마 모멘텀을 따라가는 전략입니다.

    매수 조건:
    - 테마 점수 70 이상
    - 종목 등락률 +1% ~ +15%
    - 거래량 150% 이상
    - 테마 발생 후 30분 이내

    매도 조건:
    - 익절: +3%
    - 손절: -1.5%
    - 트레일링 스탑: 고점 대비 -1%
    - 테마 쿨다운 (점수 하락)
    """

    def __init__(self, config: Optional[ThemeChasingConfig] = None, kis_market_data=None):
        config = config or ThemeChasingConfig()
        super().__init__(config)
        self.theme_config = config

        # 테마 탐지기
        self._theme_detector: Optional[ThemeDetector] = None

        # KIS 시장 데이터 (외국인/기관 수급)
        self._kis_market_data = kis_market_data
        self._foreign_cache: Dict[str, Dict] = {}  # symbol -> {net_buy, updated}
        self._institution_cache: Dict[str, Dict] = {}

        # 테마 추적
        self._active_themes: Dict[str, ThemeInfo] = {}
        self._theme_entries: Dict[str, int] = {}  # 테마별 진입 횟수
        self._entries_date: Optional[date] = None  # 진입 횟수 기준 날짜

        # 포지션별 테마 매핑
        self._position_themes: Dict[str, str] = {}  # symbol -> theme_name

    def set_theme_detector(self, detector: ThemeDetector):
        """테마 탐지기 설정"""
        self._theme_detector = detector

    async def on_theme(self, event: ThemeEvent) -> Optional[Signal]:
        """테마 이벤트 처리"""
        if not self.enabled:
            return None

        theme_name = event.name
        theme_score = event.score

        # 테마 점수 필터
        if theme_score < self.theme_config.min_theme_score:
            return None

        # 테마 정보 업데이트
        if theme_name not in self._active_themes:
            self._active_themes[theme_name] = ThemeInfo(
                name=theme_name,
                keywords=event.keywords,
                related_stocks=event.symbols,
                score=theme_score,
            )
            self._theme_entries[theme_name] = 0
            logger.info(f"[테마 추종] 새 핫 테마 감지: {theme_name} (점수: {theme_score:.0f})")
        else:
            self._active_themes[theme_name].score = theme_score
            self._active_themes[theme_name].last_updated = datetime.now()

        return None  # 테마 이벤트 자체로는 신호 생성 안 함

    async def generate_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position] = None
    ) -> Optional[Signal]:
        """매매 신호 생성"""
        # 일일 진입 횟수 및 활성 테마 자동 리셋
        today = date.today()
        if self._entries_date != today:
            self._theme_entries.clear()
            self._active_themes.clear()
            self._entries_date = today

        indicators = self.get_indicators(symbol)

        if not indicators:
            return None

        # 포지션 있는 경우 청산 체크
        if position and position.quantity > 0:
            return await self._check_exit_signal(symbol, current_price, position, indicators)

        # 포지션 없는 경우 진입 체크
        return await self._check_entry_signal(symbol, current_price, indicators)

    async def _check_entry_signal(
        self,
        symbol: str,
        current_price: Decimal,
        indicators: Dict[str, float]
    ) -> Optional[Signal]:
        """진입 신호 체크"""
        # 시간대 체크
        if not self._is_trading_time():
            return None

        # 테마 탐지기 확인
        if not self._theme_detector:
            self._theme_detector = get_theme_detector()

        # 종목이 속한 테마 확인
        stock_themes = self._theme_detector.get_stock_themes(symbol)
        if not stock_themes:
            return None

        # 핫 테마 중 하나에 속하는지 확인
        hot_theme = None
        hot_theme_score = 0.0

        for theme_name in stock_themes:
            if theme_name in self._active_themes:
                theme = self._active_themes[theme_name]

                # 테마 신선도 체크 (last_updated 기준: on_theme()에서 갱신됨)
                age_minutes = (datetime.now() - theme.last_updated).total_seconds() / 60
                if age_minutes > self.theme_config.max_theme_age_minutes:
                    continue

                # 테마당 진입 횟수 체크
                if self._theme_entries.get(theme_name, 0) >= self.theme_config.max_entries_per_theme:
                    continue

                if theme.score > hot_theme_score:
                    hot_theme = theme
                    hot_theme_score = theme.score

        if not hot_theme:
            return None

        # 종목 조건 체크
        price = float(current_price)
        change_pct = indicators.get("change_1d", 0)
        vol_ratio = indicators.get("vol_ratio", 0)

        # 등락률 필터
        if change_pct < self.theme_config.min_change_pct:
            return None
        if change_pct > self.theme_config.max_change_pct:
            logger.debug(f"[테마 추종] {symbol} 과열 (등락률 {change_pct:.1f}%)")
            return None

        # 거래량 필터
        if vol_ratio < self.theme_config.min_volume_ratio:
            return None

        # 뉴스 센티멘트 필터/보너스
        news_bonus = 0.0
        news_info = ""
        if self._theme_detector:
            sentiment = self._theme_detector.get_stock_sentiment(symbol)
            if sentiment:
                direction = sentiment.get("direction", "")
                impact = sentiment.get("impact", 0)
                reason_text = sentiment.get("reason", "")

                if direction == "bearish":
                    logger.info(
                        f"[테마 추종] {symbol} 악재 차단: "
                        f"impact={impact}, {reason_text}"
                    )
                    return None

                if direction == "bullish":
                    news_bonus = min(impact * 0.1, 10.0)
                    news_info = f", 뉴스호재={impact}"

        # 외국인/기관 수급 체크
        supply_bonus = 0.0
        supply_info = ""
        await self._refresh_supply_demand()
        if self._foreign_cache or self._institution_cache:
            supply_bonus, supply_info, _ = self._get_supply_demand_bonus(symbol)
            if supply_info:
                news_info += f", {supply_info}"

        # 신호 강도 결정
        if hot_theme_score >= 90:
            strength = SignalStrength.VERY_STRONG
        elif hot_theme_score >= 80:
            strength = SignalStrength.STRONG
        else:
            strength = SignalStrength.NORMAL

        # 점수 계산
        score = self._calculate_entry_score(hot_theme_score, change_pct, vol_ratio)
        score = max(0.0, min(score + news_bonus + supply_bonus, 100.0))

        # 목표가 & 손절가
        target_price = Decimal(str(price * (1 + self.theme_config.take_profit_pct / 100)))
        stop_price = Decimal(str(price * (1 - self.theme_config.stop_loss_pct / 100)))

        # 진입 횟수 증가
        self._theme_entries[hot_theme.name] = self._theme_entries.get(hot_theme.name, 0) + 1

        # 포지션-테마 매핑 저장 (청산 시 테마 쿨다운 체크용)
        self._position_themes[symbol] = hot_theme.name

        reason = (
            f"테마[{hot_theme.name}] 점수={hot_theme_score:.0f}, "
            f"등락률={change_pct:+.1f}%, 거래량={vol_ratio:.1f}x{news_info}"
        )

        logger.info(f"[테마 추종] 진입 신호: {symbol} - {reason}")

        return self.create_signal(
            symbol=symbol,
            side=OrderSide.BUY,
            strength=strength,
            price=current_price,
            score=score,
            reason=reason,
            target_price=target_price,
            stop_price=stop_price,
        )

    async def _check_exit_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Position,
        indicators: Dict[str, float]
    ) -> Optional[Signal]:
        """
        청산 신호 체크

        기계적 청산(손절/익절/트레일링)은 ExitManager가 전담합니다.
        테마 전략은 테마 쿨다운만 자체 처리합니다.
        """
        # 테마 쿨다운 체크 (전략 고유 조건)
        theme_name = self._position_themes.get(symbol)
        if theme_name and theme_name in self._active_themes:
            theme = self._active_themes[theme_name]

            # 테마 점수 급락 시 청산
            if theme.score < self.theme_config.min_theme_score * 0.7:
                self._cleanup_position_theme(symbol)
                return self.create_signal(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    strength=SignalStrength.NORMAL,
                    price=current_price,
                    score=70.0,
                    reason=f"테마 쿨다운: {theme_name} 점수 {theme.score:.0f}",
                )

        return None

    def _calculate_entry_score(
        self,
        theme_score: float,
        change_pct: float,
        vol_ratio: float
    ) -> float:
        """진입 점수 계산"""
        score = 0.0

        # 테마 점수 (50점)
        score += min(theme_score * 0.5, 50)

        # 등락률 (25점)
        # 1%~5% 구간이 최적
        if 1 <= change_pct <= 5:
            score += 25
        elif change_pct <= 10:
            score += 15
        else:
            score += 5

        # 거래량 (25점)
        score += min(vol_ratio * 5, 25)

        return min(score, 100.0)

    def _is_trading_time(self) -> bool:
        """거래 가능 시간 체크"""
        now = datetime.now()
        current_time = now.strftime("%H:%M")

        return self.theme_config.trading_start_time <= current_time <= self.theme_config.trading_end_time

    def calculate_score(self, symbol: str) -> float:
        """신호 점수 계산"""
        # 테마 점수 조회
        if self._theme_detector:
            theme_score = self._theme_detector.get_theme_score(symbol)
        else:
            theme_score = 0.0

        indicators = self.get_indicators(symbol)
        if not indicators:
            return theme_score

        change_pct = indicators.get("change_1d", 0)
        vol_ratio = indicators.get("vol_ratio", 0)

        return self._calculate_entry_score(theme_score, change_pct, vol_ratio)

    def update_themes(self, themes: List[ThemeInfo]):
        """테마 정보 업데이트"""
        for theme in themes:
            if theme.score >= self.theme_config.min_theme_score:
                self._active_themes[theme.name] = theme
            elif theme.name in self._active_themes:
                # 점수 하락 시 제거
                del self._active_themes[theme.name]

    def get_active_themes(self) -> List[str]:
        """활성 테마 목록"""
        return list(self._active_themes.keys())

    def get_theme_stocks(self) -> Dict[str, List[str]]:
        """테마별 관련 종목"""
        result = {}
        for theme_name, theme in self._active_themes.items():
            result[theme_name] = theme.related_stocks
        return result

    async def _refresh_supply_demand(self):
        """외국인/기관 수급 데이터 캐시 갱신 (10분 주기)"""
        if not self._kis_market_data:
            return

        # 마지막 갱신 시간 체크
        now = datetime.now()
        if self._foreign_cache:
            first = next(iter(self._foreign_cache.values()), {})
            updated = first.get("updated")
            if updated and (now - updated).total_seconds() < 600:
                return

        try:
            # 외국인 순매수 (코스피 + 코스닥)
            foreign_kospi = await self._kis_market_data.fetch_foreign_institution(market="0001", investor="1") or []
            foreign_kosdaq = await self._kis_market_data.fetch_foreign_institution(market="0002", investor="1") or []
            self._foreign_cache.clear()
            for item in foreign_kospi + foreign_kosdaq:
                sym = item.get("symbol", "")
                net_buy = item.get("net_buy_qty", 0)
                if sym:
                    self._foreign_cache[sym] = {"net_buy": net_buy, "updated": now}

            # 기관 순매수 (코스피 + 코스닥)
            inst_kospi = await self._kis_market_data.fetch_foreign_institution(market="0001", investor="2") or []
            inst_kosdaq = await self._kis_market_data.fetch_foreign_institution(market="0002", investor="2") or []
            self._institution_cache.clear()
            for item in inst_kospi + inst_kosdaq:
                sym = item.get("symbol", "")
                net_buy = item.get("net_buy_qty", 0)
                if sym:
                    self._institution_cache[sym] = {"net_buy": net_buy, "updated": now}

            logger.debug(
                f"[테마 추종] 수급 캐시 갱신: 외국인 {len(self._foreign_cache)}종목, "
                f"기관 {len(self._institution_cache)}종목"
            )
        except Exception as e:
            logger.warning(f"[테마 추종] 수급 데이터 조회 실패 (무시): {e}")

    def _get_supply_demand_bonus(self, symbol: str) -> tuple:
        """
        외국인/기관 수급 기반 신뢰도 보너스/페널티

        Returns:
            (bonus_score, info_str, should_block)
        """
        foreign_data = self._foreign_cache.get(symbol)
        inst_data = self._institution_cache.get(symbol)

        foreign_buy = foreign_data.get("net_buy", 0) if foreign_data else 0
        inst_buy = inst_data.get("net_buy", 0) if inst_data else 0

        bonus = 0.0
        info_parts = []
        should_block = False

        # 외국인+기관 동시 순매수 → 신뢰도 보너스
        if foreign_buy > 0 and inst_buy > 0:
            bonus = 10.0
            info_parts.append(f"외국인+기관 순매수")
        elif foreign_buy > 0:
            bonus = 5.0
            info_parts.append(f"외국인 순매수")
        elif inst_buy > 0:
            bonus = 5.0
            info_parts.append(f"기관 순매수")

        # 외국인+기관 동시 순매도 → 주의 (진입 차단은 아니지만 점수 감점)
        if foreign_buy < 0 and inst_buy < 0:
            bonus = -10.0
            info_parts.append(f"외국인+기관 동시 순매도 주의")

        info = ", ".join(info_parts) if info_parts else ""
        return bonus, info, should_block

    def _cleanup_position_theme(self, symbol: str):
        """포지션 청산 시 테마 매핑 정리"""
        if symbol in self._position_themes:
            theme_name = self._position_themes.pop(symbol)
            logger.debug(f"[테마 추종] 포지션-테마 매핑 해제: {symbol} <- {theme_name}")

    def on_position_closed(self, symbol: str):
        """포지션 청산 콜백 (외부에서 호출)"""
        self._cleanup_position_theme(symbol)
