"""
AI Trading Bot v2 - 스윙 모멘텀 기술적 지표 모듈

FDR(FinanceDataReader) 일봉 데이터 기반 벡터 연산 지표 계산기.
배치 분석(장 마감 후) 전용으로, 실시간 틱 의존 없음.

지표 목록:
  MA: 5, 20, 50, 150, 200
  RSI: 2, 14 (Wilder's Smoothing)
  BB: upper, mid, lower (20일, 2σ)
  MACD: line, signal, histogram
  ATR: 14일 (%)
  Change: 5d, 20d, 60d
  52w: high, low
  SEPA: Minervini 트렌드 템플릿 체크
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


class TechnicalIndicators:
    """
    일봉 기반 기술적 지표 계산기

    캐시 TTL 24시간: 장 마감 후 1회 계산, 익일 장중 재사용.
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: Dict[str, datetime] = {}
        self.CACHE_TTL = 86400  # 24시간

    def calculate_all(self, symbol: str, daily_data: List[Dict]) -> Dict[str, Any]:
        """
        일봉 데이터 → 전체 지표 계산

        Args:
            symbol: 종목코드
            daily_data: [{"date","open","high","low","close","volume"}, ...]
                        날짜 오름차순 (오래된 것 먼저)

        Returns:
            지표 딕셔너리
        """
        # 캐시 체크
        now = datetime.now()
        if symbol in self._cache_ts:
            elapsed = (now - self._cache_ts[symbol]).total_seconds()
            if elapsed < self.CACHE_TTL and symbol in self._cache:
                return self._cache[symbol]

        if not daily_data or len(daily_data) < 5:
            return {}

        closes = [float(d["close"]) for d in daily_data]
        highs = [float(d["high"]) for d in daily_data]
        lows = [float(d["low"]) for d in daily_data]
        volumes = [int(d.get("volume", 0)) for d in daily_data]

        indicators: Dict[str, Any] = {}

        # 이동평균
        for period in [5, 20, 50, 150, 200]:
            key = f"ma{period}"
            indicators[key] = self._sma(closes, period)

        # RSI
        indicators["rsi_2"] = self._rsi(closes, 2)
        indicators["rsi_14"] = self._rsi(closes, 14)

        # 볼린저밴드
        bb = self._bollinger(closes, 20, 2.0)
        if bb:
            indicators["bb_upper"], indicators["bb_mid"], indicators["bb_lower"] = bb

        # MACD
        macd = self._macd(closes, 12, 26, 9)
        if macd:
            indicators["macd"], indicators["macd_signal"], indicators["macd_hist"] = macd

        # ATR (% 및 절대값)
        atr_result = self._atr(highs, lows, closes, 14)
        if atr_result:
            indicators["atr_14"] = atr_result[0]       # % 단위
            indicators["atr_14_abs"] = atr_result[1]    # 원 단위 절대값
        else:
            indicators["atr_14"] = None
            indicators["atr_14_abs"] = None

        # 변화율
        if len(closes) >= 6:
            indicators["change_5d"] = (closes[-1] - closes[-6]) / closes[-6] * 100 if closes[-6] > 0 else 0
        if len(closes) >= 21:
            indicators["change_20d"] = (closes[-1] - closes[-21]) / closes[-21] * 100 if closes[-21] > 0 else 0
        if len(closes) >= 61:
            indicators["change_60d"] = (closes[-1] - closes[-61]) / closes[-61] * 100 if closes[-61] > 0 else 0

        # 52주 고저 (약 250거래일)
        lookback_52w = min(len(highs), 250)
        indicators["high_52w"] = max(highs[-lookback_52w:])
        indicators["low_52w"] = min(lows[-lookback_52w:])

        # 현재가
        indicators["close"] = closes[-1]
        indicators["volume"] = volumes[-1] if volumes else 0

        # 거래량 평균 (20일)
        if len(volumes) >= 20:
            indicators["vol_ma20"] = sum(volumes[-20:]) / 20
            indicators["vol_ratio"] = volumes[-1] / indicators["vol_ma20"] if indicators["vol_ma20"] > 0 else 0

        # MA5 > MA20 정렬 플래그
        indicators["ma5_above_ma20"] = bool(
            indicators.get("ma5") and indicators.get("ma20")
            and indicators["ma5"] > indicators["ma20"]
        )

        # SEPA 체크
        sepa_pass, sepa_reasons = self.check_sepa(indicators)
        indicators["sepa_pass"] = sepa_pass
        indicators["sepa_reasons"] = sepa_reasons

        # 캐시 저장
        self._cache[symbol] = indicators
        self._cache_ts[symbol] = now

        return indicators

    # --- 핵심 지표 ---

    @staticmethod
    def _sma(closes: List[float], period: int) -> Optional[float]:
        """단순이동평균"""
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period

    @staticmethod
    def _rsi(closes: List[float], period: int) -> Optional[float]:
        """RSI (Wilder's Smoothing, base.py:366 참조)"""
        if len(closes) < period + 1:
            return None

        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

        # 첫 번째 평균 (SMA)
        gains = [max(c, 0) for c in changes[:period]]
        losses = [max(-c, 0) for c in changes[:period]]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        # Wilder's Smoothing
        for c in changes[period:]:
            gain = max(c, 0)
            loss = max(-c, 0)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_gain == 0 and avg_loss == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _bollinger(closes: List[float], period: int = 20, std: float = 2.0) -> Optional[Tuple[float, float, float]]:
        """볼린저밴드 → (upper, mid, lower)"""
        if len(closes) < period:
            return None

        data = closes[-period:]
        mid = sum(data) / period
        variance = sum((x - mid) ** 2 for x in data) / period
        sd = variance ** 0.5

        upper = mid + std * sd
        lower = mid - std * sd

        return (upper, mid, lower)

    @staticmethod
    def _macd(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9) -> Optional[Tuple[float, float, float]]:
        """MACD → (line, signal, histogram)"""
        if len(closes) < slow + sig:
            return None

        def ema(data: List[float], period: int) -> List[float]:
            multiplier = 2 / (period + 1)
            result = [data[0]]
            for i in range(1, len(data)):
                result.append(data[i] * multiplier + result[-1] * (1 - multiplier))
            return result

        ema_fast = ema(closes, fast)
        ema_slow = ema(closes, slow)

        # MACD line = EMA(fast) - EMA(slow)
        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]

        # Signal line = EMA(MACD, sig)
        signal_line = ema(macd_line, sig)

        line_val = macd_line[-1]
        signal_val = signal_line[-1]
        hist_val = line_val - signal_val

        return (line_val, signal_val, hist_val)

    @staticmethod
    def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[Tuple[float, float]]:
        """ATR → (%, 절대값) 튜플 반환"""
        if len(highs) < period + 1:
            return None

        true_ranges = []
        for i in range(1, len(highs)):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i - 1]

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        if len(true_ranges) < period:
            return None

        atr = sum(true_ranges[-period:]) / period
        current_price = closes[-1]

        if current_price <= 0:
            return None

        atr_pct = (atr / current_price) * 100
        return (atr_pct, atr)

    # --- 스윙 전용 ---

    @staticmethod
    def check_sepa(indicators: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        미너비니 SEPA 트렌드 템플릿 조건 체크

        조건:
        1. MA50 > MA150 > MA200
        2. 가격 > MA50
        3. MA200 상승 추세 (close > ma200이면 간접 확인)
        4. 52주 저점 대비 +30% 이상
        5. 52주 고점 대비 -25% 이내

        Returns:
            (pass: bool, reasons: List[str])
        """
        reasons = []
        close = indicators.get("close")
        ma50 = indicators.get("ma50")
        ma150 = indicators.get("ma150")
        ma200 = indicators.get("ma200")
        high_52w = indicators.get("high_52w")
        low_52w = indicators.get("low_52w")

        if not all([close, ma50, ma150, ma200, high_52w, low_52w]):
            return False, ["데이터 부족"]

        # 1. MA 정렬: MA50 > MA150 > MA200
        if ma50 > ma150 > ma200:
            reasons.append("MA정렬 OK (50>150>200)")
        else:
            return False, ["MA정렬 실패"]

        # 2. 가격 > MA50
        if close > ma50:
            reasons.append(f"가격>MA50 ({close:,.0f}>{ma50:,.0f})")
        else:
            return False, ["가격<MA50"]

        # 3. MA200 상승 (change_60d > 0 으로 간접 확인)
        change_60d = indicators.get("change_60d", 0)
        if change_60d and change_60d > 0:
            reasons.append(f"60일 상승 +{change_60d:.1f}%")

        # 4. 52주 저점 대비 +30% 이상
        if low_52w > 0:
            from_low = (close - low_52w) / low_52w * 100
            if from_low >= 30:
                reasons.append(f"52w저점 대비 +{from_low:.0f}%")
            else:
                return False, [f"52w저점 대비 +{from_low:.0f}% (<30%)"]

        # 5. 52주 고점 대비 -25% 이내
        if high_52w > 0:
            from_high = (close - high_52w) / high_52w * 100
            if from_high >= -25:
                reasons.append(f"52w고점 대비 {from_high:.0f}%")
            else:
                return False, [f"52w고점 대비 {from_high:.0f}% (<-25%)"]

        return True, reasons

    @staticmethod
    def check_rsi2_entry(indicators: Dict[str, Any]) -> Tuple[bool, float, str]:
        """
        RSI-2 역추세 진입 조건 체크

        조건:
        1. RSI(2) < 10
        2. 가격 > MA200 (장기 상승 추세)
        3. 가격 < 볼린저밴드 하단

        Returns:
            (pass: bool, rsi_value: float, reason: str)
        """
        rsi_2 = indicators.get("rsi_2")
        close = indicators.get("close")
        ma200 = indicators.get("ma200")
        bb_lower = indicators.get("bb_lower")

        if rsi_2 is None or close is None or ma200 is None:
            return False, 0.0, "데이터 부족"

        if rsi_2 >= 10:
            return False, rsi_2, f"RSI(2)={rsi_2:.1f} (>10)"

        if close <= ma200:
            return False, rsi_2, f"가격({close:,.0f})<MA200({ma200:,.0f})"

        # BB 하단 체크 (없으면 RSI+MA 조건만으로 통과)
        reason = f"RSI(2)={rsi_2:.1f}, 가격>MA200"
        if bb_lower and close < bb_lower:
            reason += f", BB하단 이탈"

        return True, rsi_2, reason

    @staticmethod
    def calculate_mrs(stock_closes: List[float], index_closes: List[float],
                      period: int = 20) -> Optional[Dict[str, float]]:
        """
        맨스필드 상대강도 (Mansfield Relative Strength)

        RS = stock / index
        MRS = ((RS / SMA(RS, period)) - 1) * 100

        Returns:
            {"mrs": float, "mrs_slope": float} or None
        """
        min_len = min(len(stock_closes), len(index_closes))
        if min_len < period + 5:
            return None

        # 길이 맞추기 (뒤에서부터)
        sc = stock_closes[-min_len:]
        ic = index_closes[-min_len:]

        # RS 계산
        rs = []
        for i in range(min_len):
            if ic[i] > 0:
                rs.append(sc[i] / ic[i])
            else:
                rs.append(0)

        if len(rs) < period:
            return None

        # SMA(RS, period)
        sma_rs = sum(rs[-period:]) / period
        if sma_rs <= 0:
            return None

        # MRS = ((RS / SMA(RS)) - 1) * 100
        mrs = (rs[-1] / sma_rs - 1) * 100

        # 5일 기울기: 5일 전 MRS와 현재 MRS의 차이
        if len(rs) >= period + 5:
            # 5일 전 시점의 RS[-6], SMA(RS, period)는 rs[-(period+5):-5] 구간
            sma_rs_5ago = sum(rs[-(period + 5):-5]) / period
            if sma_rs_5ago > 0:
                mrs_5ago = (rs[-6] / sma_rs_5ago - 1) * 100
            else:
                mrs_5ago = 0.0
            mrs_slope = mrs - mrs_5ago
        else:
            mrs_slope = 0.0

        return {"mrs": round(mrs, 3), "mrs_slope": round(mrs_slope, 3)}

    def invalidate_cache(self, symbol: Optional[str] = None):
        """캐시 무효화"""
        if symbol:
            self._cache.pop(symbol, None)
            self._cache_ts.pop(symbol, None)
        else:
            self._cache.clear()
            self._cache_ts.clear()
