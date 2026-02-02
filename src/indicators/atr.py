"""
ATR (Average True Range) 계산

변동성 기반 지표로 동적 손절/익절에 사용
"""

from decimal import Decimal
from typing import List, Optional
from loguru import logger


def calculate_atr(
    highs: List[Decimal],
    lows: List[Decimal],
    closes: List[Decimal],
    period: int = 14
) -> Optional[float]:
    """
    ATR (Average True Range) 계산

    Args:
        highs: 고가 리스트 (최신 → 과거 순서)
        lows: 저가 리스트
        closes: 종가 리스트
        period: ATR 기간 (기본 14일)

    Returns:
        ATR 값 (%), None이면 계산 불가

    Example:
        >>> atr = calculate_atr(highs, lows, closes, period=14)
        >>> if atr:
        >>>     dynamic_stop = max(2.5, min(5.0, atr * 2.0))
    """
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        logger.debug(f"[ATR] 데이터 부족: {len(highs)}개 (최소 {period+1}개 필요)")
        return None

    if not (len(highs) == len(lows) == len(closes)):
        logger.warning("[ATR] 데이터 길이 불일치")
        return None

    try:
        # True Range 계산
        true_ranges = []

        for i in range(len(highs) - 1):
            high = float(highs[i])
            low = float(lows[i])
            prev_close = float(closes[i + 1])

            # TR = max(high - low, |high - prev_close|, |low - prev_close|)
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            true_ranges.append(tr)

            if len(true_ranges) >= period:
                break

        if len(true_ranges) < period:
            logger.debug(f"[ATR] True Range 부족: {len(true_ranges)}개")
            return None

        # ATR = True Range의 평균
        atr = sum(true_ranges[:period]) / period

        # 최근 종가 기준 퍼센트로 변환
        current_price = float(closes[0])
        if current_price <= 0:
            return None

        atr_pct = (atr / current_price) * 100

        logger.debug(f"[ATR] 계산 완료: {atr_pct:.2f}% (절대값: {atr:.2f})")
        return atr_pct

    except (ValueError, IndexError, ZeroDivisionError) as e:
        logger.warning(f"[ATR] 계산 실패: {e}")
        return None


def calculate_dynamic_stop_loss(
    atr_pct: float,
    min_stop: float = 2.5,
    max_stop: float = 5.0,
    multiplier: float = 2.0
) -> float:
    """
    ATR 기반 동적 손절 계산

    Args:
        atr_pct: ATR (퍼센트)
        min_stop: 최소 손절폭 (%)
        max_stop: 최대 손절폭 (%)
        multiplier: ATR 배수 (기본 2.0)

    Returns:
        손절폭 (%)

    Example:
        >>> atr = calculate_atr(highs, lows, closes)
        >>> stop_loss = calculate_dynamic_stop_loss(atr)
        >>> # 변동성 낮음(ATR 1%) → 2.5% 손절
        >>> # 변동성 보통(ATR 2%) → 4.0% 손절
        >>> # 변동성 높음(ATR 3%+) → 5.0% 손절 (상한)
    """
    if atr_pct is None or atr_pct <= 0:
        return min_stop

    dynamic_stop = atr_pct * multiplier
    clamped_stop = max(min_stop, min(max_stop, dynamic_stop))

    logger.debug(
        f"[동적손절] ATR={atr_pct:.2f}% × {multiplier} = {dynamic_stop:.2f}% "
        f"→ {clamped_stop:.2f}% (범위: {min_stop}~{max_stop}%)"
    )

    return clamped_stop
