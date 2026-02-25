"""
AI Trading Bot v2 - VCP 패턴 탐지 (Layer 3)

Mark Minervini 스타일 변동성 수축 패턴 (Volatility Contraction Pattern).
매일 15:35 실행, swing_screener의 FDR 데이터 재사용.

조건:
1. 52주 고점의 75% 이상
2. 200일 MA 위
3. 주간 변동폭 수축 (최근 4주)
4. 거래량 감소 추세
5. MA 정배열 (50 > 150 > 200)
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from loguru import logger


@dataclass
class VCPCandidate:
    """VCP 패턴 후보"""
    symbol: str
    name: str
    score: float  # 0~100
    high_proximity: float  # 52주 고점 대비 비율 (0~1)
    contraction_count: int  # 수축 횟수
    vol_declining: bool  # 거래량 감소
    ma_aligned: bool  # MA 정배열
    weekly_ranges: List[float] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VCPCandidate":
        return cls(**d)


class VCPDetector:
    """변동성 수축 패턴 (VCP) 탐지"""

    def __init__(self):
        self._cache_dir = Path.home() / ".cache" / "ai_trader" / "strategic"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def detect_all(
        self, candidates_data: List[Dict[str, Any]]
    ) -> List[VCPCandidate]:
        """전체 후보에서 VCP 패턴 탐지

        Args:
            candidates_data: swing_screener._calculate_all_indicators() 결과
                각 항목: {"symbol", "name", "indicators", "daily_data"}
        """
        logger.info(f"[VCP] 패턴 탐지 시작: {len(candidates_data)}종목")

        results = []
        for data in candidates_data:
            try:
                candidate = self._detect_single(data)
                if candidate:
                    results.append(candidate)
            except Exception as e:
                logger.debug(f"[VCP] {data.get('symbol', '?')} 분석 실패: {e}")

        results.sort(key=lambda x: x.score, reverse=True)

        # 캐시 저장
        self._save_cache(results)

        logger.info(f"[VCP] 탐지 완료: {len(results)}종목")
        for c in results[:5]:
            logger.info(
                f"  {c.symbol} {c.name}: 점수={c.score:.0f} "
                f"고점근접={c.high_proximity:.1%} 수축={c.contraction_count}회"
            )

        return results

    def _detect_single(self, data: Dict[str, Any]) -> Optional[VCPCandidate]:
        """개별 종목 VCP 탐지"""
        symbol = data["symbol"]
        name = data["name"]
        daily_data = data.get("daily_data", [])

        if len(daily_data) < 100:
            return None

        closes = np.array([d["close"] for d in daily_data], dtype=float)
        highs = np.array([d["high"] for d in daily_data], dtype=float)
        lows = np.array([d["low"] for d in daily_data], dtype=float)
        volumes = np.array([d["volume"] for d in daily_data], dtype=float)

        current_close = closes[-1]
        if current_close <= 0:
            return None

        # 1) 52주 고점 대비 위치
        high_252 = np.max(highs[-252:]) if len(highs) >= 252 else np.max(highs)
        high_proximity = float(current_close / high_252) if high_252 > 0 else 0.0  # numpy.float64 → float

        if high_proximity < 0.75:
            return None

        # 2) 200일 MA 위
        if len(closes) >= 200:
            ma200 = np.mean(closes[-200:])
            if current_close < ma200:
                return None
        else:
            # 데이터 부족 시 150일 MA 사용
            if len(closes) >= 150:
                ma150 = np.mean(closes[-150:])
                if current_close < ma150:
                    return None

        # 3) 주간 변동폭 수축 (최근 4주)
        weekly_ranges = []
        for i in range(4):
            end_idx = len(daily_data) - i * 5
            start_idx = end_idx - 5
            if start_idx < 0:
                break

            week_highs = highs[start_idx:end_idx]
            week_lows = lows[start_idx:end_idx]
            week_closes = closes[start_idx:end_idx]

            if len(week_closes) > 0:
                week_mean = np.mean(week_closes)
                if week_mean > 0:
                    range_pct = (np.max(week_highs) - np.min(week_lows)) / week_mean
                    weekly_ranges.append(float(range_pct))

        if len(weekly_ranges) < 3:
            return None

        # 주간 변동폭이 수축 중인지 (역순: 가장 오래된 → 최근)
        weekly_ranges.reverse()
        contractions = 0
        for i in range(1, len(weekly_ranges)):
            if weekly_ranges[i] < weekly_ranges[i - 1]:
                contractions += 1

        if contractions < 2:
            return None

        # 4) 거래량 감소 추세
        vol_10 = np.mean(volumes[-10:])
        vol_30 = np.mean(volumes[-30:])
        vol_declining = bool(vol_10 < vol_30 * 0.8)  # numpy.bool_ → Python bool (JSON 직렬화 안전)

        # 5) MA 정배열 (50 > 150 > 200)
        ma_aligned = False
        if len(closes) >= 200:
            ma50 = np.mean(closes[-50:])
            ma150 = np.mean(closes[-150:])
            ma200 = np.mean(closes[-200:])
            ma_aligned = bool(ma50 > ma150 > ma200)  # numpy.bool_ → Python bool

            # 200일 MA 상승 중
            ma200_20d_ago = np.mean(closes[-220:-20]) if len(closes) >= 220 else ma200
            ma200_rising = ma200 > ma200_20d_ago
        else:
            ma200_rising = False

        # 6) VCP 점수 계산
        score = self._calculate_vcp_score(
            high_proximity=high_proximity,
            contraction_count=contractions,
            vol_declining=vol_declining,
            ma_aligned=ma_aligned,
            ma200_rising=ma200_rising,
            weekly_ranges=weekly_ranges,
        )

        if score < 40:
            return None

        reasons = []
        if high_proximity >= 0.9:
            reasons.append(f"52주 고점 {high_proximity:.0%} 근접")
        elif high_proximity >= 0.8:
            reasons.append(f"52주 고점 대비 {high_proximity:.0%}")
        if contractions >= 3:
            reasons.append(f"변동성 3주 연속 수축")
        elif contractions >= 2:
            reasons.append(f"변동성 2주 수축")
        if vol_declining:
            reasons.append("거래량 감소 (매도 소진)")
        if ma_aligned:
            reasons.append("MA 정배열 (50>150>200)")

        return VCPCandidate(
            symbol=symbol,
            name=name,
            score=score,
            high_proximity=round(high_proximity, 3),
            contraction_count=contractions,
            vol_declining=vol_declining,
            ma_aligned=ma_aligned,
            weekly_ranges=[round(r, 4) for r in weekly_ranges],
            reasons=reasons,
        )

    @staticmethod
    def _calculate_vcp_score(
        high_proximity: float,
        contraction_count: int,
        vol_declining: bool,
        ma_aligned: bool,
        ma200_rising: bool,
        weekly_ranges: List[float],
    ) -> float:
        """VCP 점수 산출 (0~100)"""
        score = 0.0

        # 52주 고점 근접도
        if high_proximity >= 0.90:
            score += 25
        elif high_proximity >= 0.80:
            score += 15
        elif high_proximity >= 0.75:
            score += 5

        # 주간 변동폭 수축 횟수
        if contraction_count >= 3:
            score += 25
        elif contraction_count >= 2:
            score += 15

        # 거래량 감소
        if vol_declining:
            score += 15

        # MA 정배열
        if ma_aligned:
            score += 15

        # 200일 MA 상승 중
        if ma200_rising:
            score += 10

        # 최근 주간 변동폭이 5% 미만 (타이트한 수축)
        if weekly_ranges and weekly_ranges[-1] < 0.05:
            score += 10

        return min(score, 100)

    def _save_cache(self, candidates: List[VCPCandidate]):
        """결과 캐시 저장 + 오래된 캐시 정리"""
        today = datetime.now().strftime("%Y%m%d")
        path = self._cache_dir / f"vcp_candidates_{today}.json"
        try:
            data = [c.to_dict() for c in candidates]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._cleanup_old_cache("vcp_candidates")
        except Exception as e:
            logger.warning(f"[VCP] 캐시 저장 실패: {e}")

    @staticmethod
    def _cleanup_old_cache(prefix: str, max_age_days: int = 7):
        """오래된 캐시 파일 정리"""
        cache_dir = Path.home() / ".cache" / "ai_trader" / "strategic"
        cutoff = datetime.now() - timedelta(days=max_age_days)
        for f in cache_dir.glob(f"{prefix}_*.json"):
            try:
                date_str = f.stem.split("_")[-1]
                file_date = datetime.strptime(date_str, "%Y%m%d")
                if file_date < cutoff:
                    f.unlink()
            except (ValueError, OSError):
                pass

    def load_cache(self) -> List[VCPCandidate]:
        """오늘 캐시 로드"""
        today = datetime.now().strftime("%Y%m%d")
        path = self._cache_dir / f"vcp_candidates_{today}.json"
        try:
            if not path.exists():
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [VCPCandidate.from_dict(d) for d in data]
        except Exception as e:
            logger.debug(f"[VCP] 캐시 로드 실패: {e}")
            return []
