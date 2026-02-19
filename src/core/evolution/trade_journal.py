"""
AI Trading Bot v2 - 거래 저널 (Trade Journal)

모든 거래를 구조화하여 기록하고, 복기 및 학습에 활용합니다.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Any
from loguru import logger


@dataclass
class TradeRecord:
    """거래 기록"""
    # 기본 정보
    id: str                              # 거래 고유 ID
    symbol: str                          # 종목코드
    name: str = ""                       # 종목명

    # 진입 정보
    entry_time: datetime = None          # 진입 시간
    entry_price: float = 0               # 진입가
    entry_quantity: int = 0              # 진입 수량
    entry_reason: str = ""               # 진입 사유
    entry_strategy: str = ""             # 사용 전략
    entry_signal_score: float = 0        # 진입 신호 점수

    # 청산 정보
    exit_time: Optional[datetime] = None # 청산 시간
    exit_price: float = 0                # 청산가
    exit_quantity: int = 0               # 청산 수량
    exit_reason: str = ""                # 청산 사유
    exit_type: str = ""                  # 청산 유형 (take_profit, stop_loss, trailing, manual)

    # 결과
    pnl: float = 0                       # 손익 (원)
    pnl_pct: float = 0                   # 손익률 (%)
    holding_minutes: int = 0             # 보유 시간 (분)

    # 컨텍스트 (진화 학습용)
    market_context: Dict[str, Any] = field(default_factory=dict)  # 시장 상황
    indicators_at_entry: Dict[str, float] = field(default_factory=dict)  # 진입 시 지표
    indicators_at_exit: Dict[str, float] = field(default_factory=dict)   # 청산 시 지표
    theme_info: Dict[str, Any] = field(default_factory=dict)     # 테마 정보

    # 복기 메모 (LLM이 작성)
    review_notes: str = ""               # 복기 노트
    lesson_learned: str = ""             # 교훈
    improvement_suggestion: str = ""     # 개선 제안

    # 메타데이터
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def is_win(self) -> bool:
        """승리 여부"""
        return self.pnl > 0

    @property
    def is_closed(self) -> bool:
        """청산 완료 여부"""
        return self.exit_time is not None

    def to_dict(self) -> Dict:
        """딕셔너리로 변환 (JSON 저장용)"""
        d = asdict(self)
        # datetime 변환
        for key, value in d.items():
            if isinstance(value, datetime):
                d[key] = value.isoformat() if value else None
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "TradeRecord":
        """딕셔너리에서 생성"""
        # datetime 파싱 (개별 필드 실패 시 None 처리)
        for key in ["entry_time", "exit_time", "created_at", "updated_at"]:
            if data.get(key) and isinstance(data[key], str):
                try:
                    data[key] = datetime.fromisoformat(data[key])
                except (ValueError, TypeError):
                    data[key] = None
        return cls(**data)


class TradeJournal:
    """
    거래 저널

    모든 거래를 기록하고 조회하는 저장소입니다.
    파일 기반으로 영구 저장되며, 복기 및 학습에 활용됩니다.
    """

    def __init__(self, storage_dir: str = None):
        self.storage_dir = Path(storage_dir or os.getenv(
            "TRADE_JOURNAL_DIR",
            os.path.expanduser("~/.cache/ai_trader/journal")
        ))
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # 메모리 캐시
        self._trades: Dict[str, TradeRecord] = {}
        self._today_trades: List[str] = []

        # 로드
        self._load_recent_trades()

        logger.info(f"TradeJournal 초기화: {self.storage_dir}")

    def _get_file_path(self, trade_date: date) -> Path:
        """날짜별 저장 파일 경로"""
        return self.storage_dir / f"trades_{trade_date.strftime('%Y%m%d')}.json"

    def _load_recent_trades(self, days: int = 30):
        """최근 거래 로드"""
        today = date.today()

        for i in range(days):
            trade_date = today - timedelta(days=i)
            file_path = self._get_file_path(trade_date)

            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    for trade_dict in data.get("trades", []):
                        try:
                            trade = TradeRecord.from_dict(trade_dict)
                            self._trades[trade.id] = trade

                            if trade_date == today:
                                self._today_trades.append(trade.id)
                        except Exception as te:
                            logger.warning(f"거래 레코드 파싱 실패 (건너뜀): {te}")

                except json.JSONDecodeError as e:
                    logger.error(f"거래 파일 손상 ({file_path}): {e}")
                except Exception as e:
                    logger.warning(f"거래 로드 실패 ({file_path}): {e}")

        logger.info(f"거래 저널 로드: {len(self._trades)}건 (최근 {days}일)")

    def _save_trades(self, trade_date: date):
        """해당 날짜 거래 저장"""
        file_path = self._get_file_path(trade_date)

        # 해당 날짜 거래 필터
        trades = [
            t for t in self._trades.values()
            if t.entry_time and t.entry_time.date() == trade_date
        ]

        data = {
            "date": trade_date.isoformat(),
            "count": len(trades),
            "trades": [t.to_dict() for t in trades],
            "updated_at": datetime.now().isoformat(),
        }

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[거래저널] 저장 실패: {file_path} — {e}")

        logger.debug(f"거래 저장: {file_path} ({len(trades)}건)")

    def record_entry(
        self,
        trade_id: str,
        symbol: str,
        name: str,
        entry_price: float,
        entry_quantity: int,
        entry_reason: str,
        entry_strategy: str,
        signal_score: float = 0,
        indicators: Dict[str, float] = None,
        market_context: Dict[str, Any] = None,
        theme_info: Dict[str, Any] = None,
    ) -> TradeRecord:
        """
        진입 기록

        매수 체결 시 호출합니다.
        """
        now = datetime.now()

        trade = TradeRecord(
            id=trade_id,
            symbol=symbol,
            name=name,
            entry_time=now,
            entry_price=entry_price,
            entry_quantity=entry_quantity,
            entry_reason=entry_reason,
            entry_strategy=entry_strategy,
            entry_signal_score=signal_score,
            indicators_at_entry=indicators or {},
            market_context=market_context or {},
            theme_info=theme_info or {},
        )

        self._trades[trade_id] = trade

        if now.date() == date.today():
            self._today_trades.append(trade_id)

        # 저장
        self._save_trades(now.date())

        logger.info(
            f"[저널] 진입 기록: {symbol} {name} "
            f"{entry_quantity}주 @ {entry_price:,.0f}원 ({entry_strategy})"
        )

        return trade

    def record_exit(
        self,
        trade_id: str,
        exit_price: float,
        exit_quantity: int,
        exit_reason: str,
        exit_type: str,
        indicators: Dict[str, float] = None,
        exit_time: datetime = None,
    ) -> Optional[TradeRecord]:
        """
        청산 기록

        매도 체결 시 호출합니다.
        """
        trade = self._trades.get(trade_id)
        if not trade:
            logger.warning(f"[저널] 거래 ID 없음: {trade_id}")
            return None

        now = exit_time or datetime.now()

        # 청산 정보 업데이트
        trade.exit_time = now
        trade.exit_price = exit_price
        trade.exit_quantity = (trade.exit_quantity or 0) + exit_quantity  # 누적
        trade.exit_reason = exit_reason
        trade.exit_type = exit_type
        trade.indicators_at_exit = indicators or {}

        # 손익 계산 (수수료 포함, 누적: 부분 매도 시 += 방식)
        if trade.entry_price > 0:
            from src.utils.fee_calculator import calculate_net_pnl
            partial_pnl, _ = calculate_net_pnl(trade.entry_price, exit_price, exit_quantity)
            trade.pnl += partial_pnl  # += 누적 (수수료 포함)
            # 총 투자원금 기준 손익률
            invested = trade.entry_price * trade.entry_quantity
            trade.pnl_pct = float(trade.pnl / invested * 100) if invested > 0 else 0.0

        # 보유 시간 계산
        if trade.entry_time:
            delta = now - trade.entry_time
            trade.holding_minutes = int(delta.total_seconds() / 60)

        trade.updated_at = now

        # 오늘 청산된 거래를 _today_trades에 추가 (어제 진입→오늘 청산 케이스 포함)
        if now.date() == date.today() and trade_id not in self._today_trades:
            self._today_trades.append(trade_id)

        # 저장
        self._save_trades(trade.entry_time.date())

        emoji = "+" if trade.pnl > 0 else ""
        logger.info(
            f"[저널] 청산 기록: {trade.symbol} {trade.name} "
            f"{exit_quantity}주 @ {exit_price:,.0f}원 "
            f"({exit_type}) -> {emoji}{trade.pnl:,.0f}원 ({trade.pnl_pct:+.1f}%)"
        )

        return trade

    def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        """거래 조회"""
        return self._trades.get(trade_id)

    def get_today_trades(self) -> List[TradeRecord]:
        """오늘 거래 목록"""
        return [self._trades[tid] for tid in self._today_trades if tid in self._trades]

    def get_trades_by_date(self, trade_date: date) -> List[TradeRecord]:
        """날짜별 거래 목록"""
        return [
            t for t in self._trades.values()
            if t.entry_time and t.entry_time.date() == trade_date
        ]

    def get_trades_by_strategy(self, strategy: str, days: int = 30) -> List[TradeRecord]:
        """전략별 거래 목록"""
        cutoff = datetime.now() - timedelta(days=days)
        return [
            t for t in self._trades.values()
            if t.entry_strategy == strategy and t.entry_time and t.entry_time > cutoff
        ]

    def get_closed_trades(self, days: int = 30) -> List[TradeRecord]:
        """청산된 거래 목록"""
        cutoff = datetime.now() - timedelta(days=days)
        return [
            t for t in self._trades.values()
            if t.is_closed and t.entry_time and t.entry_time > cutoff
        ]

    def get_open_trades(self) -> List[TradeRecord]:
        """미청산 거래 목록"""
        return [t for t in self._trades.values() if not t.is_closed]

    def get_recent_trades(self, days: int = 7) -> List[TradeRecord]:
        """최근 N일 거래 목록 (청산/미청산 모두)"""
        cutoff = datetime.now() - timedelta(days=days)
        return [
            t for t in self._trades.values()
            if t.entry_time and t.entry_time > cutoff
        ]

    def update_review(
        self,
        trade_id: str,
        review_notes: str = "",
        lesson_learned: str = "",
        improvement_suggestion: str = "",
    ):
        """복기 노트 업데이트 (LLM 분석 결과)"""
        trade = self._trades.get(trade_id)
        if not trade:
            return

        trade.review_notes = review_notes
        trade.lesson_learned = lesson_learned
        trade.improvement_suggestion = improvement_suggestion
        trade.updated_at = datetime.now()

        # 저장
        if trade.entry_time:
            self._save_trades(trade.entry_time.date())

    def get_statistics(self, days: int = 30) -> Dict[str, Any]:
        """거래 통계"""
        trades = self.get_closed_trades(days)

        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "avg_pnl_pct": 0,
                "total_pnl": 0,
                "avg_holding_minutes": 0,
                "best_trade": None,
                "worst_trade": None,
            }

        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]

        total_pnl = sum(t.pnl for t in trades)
        avg_pnl_pct = sum(t.pnl_pct for t in trades) / len(trades)
        avg_holding = sum(t.holding_minutes for t in trades) / len(trades)

        best = max(trades, key=lambda t: t.pnl_pct) if trades else None
        worst = min(trades, key=lambda t: t.pnl_pct) if trades else None

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "avg_pnl_pct": avg_pnl_pct,
            "total_pnl": total_pnl,
            "avg_holding_minutes": avg_holding,
            "best_trade": best.to_dict() if best else None,
            "worst_trade": worst.to_dict() if worst else None,
            "by_strategy": self._get_stats_by_strategy(trades),
            "by_exit_type": self._get_stats_by_exit_type(trades),
        }

    def _get_stats_by_strategy(self, trades: List[TradeRecord]) -> Dict[str, Dict]:
        """전략별 통계"""
        stats = {}

        for trade in trades:
            strategy = trade.entry_strategy or "unknown"
            if strategy not in stats:
                stats[strategy] = {"trades": 0, "wins": 0, "total_pnl": 0}

            stats[strategy]["trades"] += 1
            if trade.is_win:
                stats[strategy]["wins"] += 1
            stats[strategy]["total_pnl"] += trade.pnl

        # 승률 계산
        for strategy, s in stats.items():
            s["win_rate"] = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0

        return stats

    def _get_stats_by_exit_type(self, trades: List[TradeRecord]) -> Dict[str, Dict]:
        """청산 유형별 통계"""
        stats = {}

        for trade in trades:
            exit_type = trade.exit_type or "unknown"
            if exit_type not in stats:
                stats[exit_type] = {"trades": 0, "wins": 0, "avg_pnl_pct": 0, "total_pnl_pct": 0}

            stats[exit_type]["trades"] += 1
            if trade.is_win:
                stats[exit_type]["wins"] += 1
            stats[exit_type]["total_pnl_pct"] += trade.pnl_pct

        # 평균 계산
        for exit_type, s in stats.items():
            s["avg_pnl_pct"] = s["total_pnl_pct"] / s["trades"] if s["trades"] > 0 else 0
            del s["total_pnl_pct"]

        return stats


# 싱글톤 인스턴스
_trade_journal: Optional[TradeJournal] = None


def get_trade_journal() -> TradeJournal:
    """TradeJournal 인스턴스 반환 (TradeStorage 우선, 실패 시 JSON 폴백)"""
    global _trade_journal
    if _trade_journal is None:
        try:
            from src.data.storage.trade_storage import get_trade_storage
            _trade_journal = get_trade_storage()
            logger.info("[거래저널] TradeStorage(DB+JSON) 모드 초기화")
        except Exception as e:
            logger.warning(f"[거래저널] TradeStorage 초기화 실패, JSON 폴백: {e}")
            _trade_journal = TradeJournal()
    return _trade_journal
