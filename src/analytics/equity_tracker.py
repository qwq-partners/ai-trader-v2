"""
AI Trading Bot v2 - 자산 히스토리 추적기 (Equity Tracker)

매일 장마감 후 총자산+포지션 스냅샷을 JSON 파일로 누적 저장합니다.
대시보드 '자산' 탭에서 일별 총자산 추이를 조회하는 데 사용됩니다.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class EquitySnapshot:
    """일일 자산 스냅샷"""
    date: str                          # YYYY-MM-DD
    total_equity: float                # 총자산
    cash: float                        # 현금
    positions_value: float             # 주식 평가액
    daily_pnl: float                   # 일일 손익 (실현+미실현)
    daily_pnl_pct: float               # 일일 손익률 (%)
    position_count: int                # 보유 종목 수
    trades_count: int                  # 당일 거래 횟수
    win_rate: float                    # 당일 승률 (%)
    positions: List[Dict[str, Any]] = field(default_factory=list)  # 보유 종목 상세
    timestamp: str = ""                # ISO 타임스탬프

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EquitySnapshot":
        # positions 필드가 없을 수 있음 (백필 데이터)
        if "positions" not in data:
            data["positions"] = []
        if "timestamp" not in data:
            data["timestamp"] = ""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class EquityTracker:
    """일일 자산 스냅샷 저장/조회"""

    STORAGE_DIR = Path(os.getenv(
        "EQUITY_TRACKER_DIR",
        os.path.expanduser("~/.cache/ai_trader/journal")
    ))

    def __init__(self):
        self.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, EquitySnapshot] = {}  # date_str → snapshot
        logger.info(f"[자산추적] 초기화: {self.STORAGE_DIR}")

    def _get_file_path(self, date_str: str) -> Path:
        """날짜별 스냅샷 파일 경로 (equity_YYYYMMDD.json)"""
        compact = date_str.replace("-", "")
        return self.STORAGE_DIR / f"equity_{compact}.json"

    def save_snapshot(self, portfolio, trade_journal, name_cache: Dict[str, str] = None):
        """포트폴리오에서 현재 스냅샷 저장"""
        if name_cache is None:
            name_cache = {}

        today = date.today().isoformat()
        now = datetime.now()

        # 포트폴리오 데이터 수집
        cash = float(portfolio.cash)
        positions_value = float(portfolio.total_position_value)
        total_equity = float(portfolio.total_equity)
        effective_pnl = float(portfolio.effective_daily_pnl)
        initial_capital = float(portfolio.initial_capital)
        pnl_pct = (effective_pnl / initial_capital * 100) if initial_capital > 0 else 0.0

        # 보유 포지션 상세
        positions_list = []
        for symbol, pos in portfolio.positions.items():
            positions_list.append({
                "symbol": symbol,
                "name": name_cache.get(symbol, getattr(pos, 'name', '') or symbol),
                "quantity": pos.quantity,
                "avg_price": float(pos.avg_price),
                "current_price": float(pos.current_price),
                "market_value": float(pos.market_value),
                "pnl": float(pos.unrealized_pnl),
                "pnl_pct": float(pos.unrealized_pnl_pct),
            })

        # 당일 거래 통계
        trades_count = 0
        win_rate = 0.0
        if trade_journal:
            today_trades = trade_journal.get_today_trades()
            closed_today = [t for t in today_trades if t.is_closed]
            trades_count = len(closed_today)
            if trades_count > 0:
                wins = sum(1 for t in closed_today if t.is_win)
                win_rate = wins / trades_count * 100

        snapshot = EquitySnapshot(
            date=today,
            total_equity=total_equity,
            cash=cash,
            positions_value=positions_value,
            daily_pnl=effective_pnl,
            daily_pnl_pct=round(pnl_pct, 2),
            position_count=len(portfolio.positions),
            trades_count=trades_count,
            win_rate=round(win_rate, 1),
            positions=positions_list,
            timestamp=now.isoformat(),
        )

        # JSON 파일 저장
        file_path = self._get_file_path(today)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)
            self._cache[today] = snapshot
            logger.info(
                f"[자산추적] 스냅샷 저장: {today} "
                f"총자산={total_equity:,.0f}원 "
                f"일일손익={effective_pnl:+,.0f}원 ({pnl_pct:+.2f}%)"
            )
        except Exception as e:
            logger.error(f"[자산추적] 스냅샷 저장 실패: {e}")

    def load_history(self, days: int = 90) -> List[EquitySnapshot]:
        """최근 N일 히스토리 로드 (캐시 우선)"""
        today = date.today()
        date_from = today - timedelta(days=days - 1)
        return self.load_history_range(date_from.isoformat(), today.isoformat())

    def load_history_range(self, date_from: str, date_to: str) -> List[EquitySnapshot]:
        """날짜 범위 히스토리 로드 (from~to, 양쪽 포함)"""
        try:
            d_from = date.fromisoformat(date_from)
            d_to = date.fromisoformat(date_to)
        except ValueError:
            return []

        snapshots = []
        current = d_from
        while current <= d_to:
            date_str = current.isoformat()

            # 캐시 체크
            if date_str in self._cache:
                snapshots.append(self._cache[date_str])
            else:
                # 파일에서 로드
                file_path = self._get_file_path(date_str)
                if file_path.exists():
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        snapshot = EquitySnapshot.from_dict(data)
                        self._cache[date_str] = snapshot
                        snapshots.append(snapshot)
                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        logger.debug(f"[자산추적] {date_str} 로드 실패: {e}")

            current += timedelta(days=1)

        return snapshots

    def get_oldest_date(self) -> Optional[str]:
        """가장 오래된 스냅샷 날짜 반환 (YYYY-MM-DD)"""
        equity_files = sorted(self.STORAGE_DIR.glob("equity_*.json"))
        if not equity_files:
            return None
        # equity_YYYYMMDD.json → YYYY-MM-DD
        fname = equity_files[0].stem  # equity_20260210
        date_part = fname.replace("equity_", "")
        if len(date_part) == 8:
            return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
        return None

    def get_snapshot(self, date_str: str) -> Optional[EquitySnapshot]:
        """특정 날짜 스냅샷"""
        # 캐시 체크
        if date_str in self._cache:
            return self._cache[date_str]

        # 파일에서 로드
        file_path = self._get_file_path(date_str)
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                snapshot = EquitySnapshot.from_dict(data)
                self._cache[date_str] = snapshot
                return snapshot
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.debug(f"[자산추적] {date_str} 로드 실패: {e}")

        return None

    def backfill_from_journal(self, journal_dir: Path = None, initial_capital: float = 10_000_000):
        """
        기존 trades_YYYYMMDD.json에서 과거 스냅샷 역산 (최초 1회)

        INITIAL_CAPITAL부터 일별 실현손익을 누적하여 총자산을 추정합니다.
        포지션 정보는 없으므로 positions: []로 저장됩니다.
        """
        if journal_dir is None:
            journal_dir = self.STORAGE_DIR

        # trades_ 파일 목록
        trade_files = sorted(journal_dir.glob("trades_*.json"))
        if not trade_files:
            logger.info("[자산추적] 백필: 거래 저널 파일 없음")
            return

        cumulative_equity = initial_capital
        backfilled = 0

        for trade_file in trade_files:
            # 날짜 추출 (trades_YYYYMMDD.json → YYYY-MM-DD)
            fname = trade_file.stem  # trades_20260213
            date_part = fname.replace("trades_", "")
            if len(date_part) != 8:
                continue
            date_str = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"

            # 이미 equity 스냅샷이 있으면 스킵
            equity_file = self._get_file_path(date_str)
            if equity_file.exists():
                # 기존 스냅샷에서 총자산 읽어서 누적값 갱신
                try:
                    with open(equity_file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    cumulative_equity = existing.get("total_equity", cumulative_equity)
                except Exception:
                    pass
                continue

            # 거래 데이터 로드
            try:
                with open(trade_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            trades = data.get("trades", [])
            closed_trades = [t for t in trades if t.get("exit_time")]

            # 일일 손익
            day_pnl = sum(t.get("pnl", 0) for t in closed_trades)
            trades_count = len(closed_trades)
            wins = sum(1 for t in closed_trades if t.get("pnl", 0) > 0)
            win_rate = (wins / trades_count * 100) if trades_count > 0 else 0.0

            cumulative_equity += day_pnl
            pnl_pct = (day_pnl / (cumulative_equity - day_pnl) * 100) if (cumulative_equity - day_pnl) > 0 else 0.0

            snapshot = EquitySnapshot(
                date=date_str,
                total_equity=round(cumulative_equity, 0),
                cash=0,  # 역산 불가
                positions_value=0,  # 역산 불가
                daily_pnl=round(day_pnl, 0),
                daily_pnl_pct=round(pnl_pct, 2),
                position_count=0,
                trades_count=trades_count,
                win_rate=round(win_rate, 1),
                positions=[],
                timestamp=f"{date_str}T17:00:00",
            )

            try:
                with open(equity_file, "w", encoding="utf-8") as f:
                    json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)
                self._cache[date_str] = snapshot
                backfilled += 1
            except Exception as e:
                logger.debug(f"[자산추적] 백필 저장 실패 ({date_str}): {e}")

        if backfilled > 0:
            logger.info(f"[자산추적] 백필 완료: {backfilled}일분 데이터 생성")
