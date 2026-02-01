"""
AI Trading Bot v2 - 로깅 설정

Loguru 기반 로깅 시스템

로그 파일 구조:
- trader_YYYYMMDD.log: 전체 시스템 로그
- error_YYYYMMDD.log: 에러만
- trades_YYYYMMDD.log: 거래 로그 (신호, 주문, 체결)
- screening_YYYYMMDD.log: 스크리닝/테마 탐지 결과
- daily_YYYYMMDD.json: 일일 복기용 JSON 로그
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger


def setup_logger(
    log_level: str = "INFO",
    log_dir: Optional[str] = None,
    rotation: str = "1 day",
    retention: str = "7 days",
    enable_console: bool = True,
    enable_file: bool = True,
):
    """
    로거 설정

    Args:
        log_level: 로그 레벨 (DEBUG, INFO, WARNING, ERROR)
        log_dir: 로그 디렉토리 경로
        rotation: 로그 파일 로테이션 주기
        retention: 로그 파일 보관 기간
        enable_console: 콘솔 출력 활성화
        enable_file: 파일 출력 활성화
    """
    # 기존 핸들러 제거
    logger.remove()

    # 포맷 정의
    console_format = (
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    file_format = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{name}:{function}:{line} | "
        "{message}"
    )

    # 콘솔 핸들러
    if enable_console:
        logger.add(
            sys.stdout,
            format=console_format,
            level=log_level,
            colorize=True,
            backtrace=True,
            diagnose=True,
        )

    # 파일 핸들러
    if enable_file and log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        today = datetime.now().strftime("%Y%m%d")

        # 일반 로그
        logger.add(
            log_path / f"trader_{today}.log",
            format=file_format,
            level=log_level,
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
            backtrace=True,
            diagnose=True,
        )

        # 에러 로그 (별도 파일)
        logger.add(
            log_path / f"error_{today}.log",
            format=file_format,
            level="ERROR",
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
            backtrace=True,
            diagnose=True,
        )

        # 거래 로그 (별도 파일)
        logger.add(
            log_path / f"trades_{today}.log",
            format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
            level="INFO",
            filter=lambda record: record["extra"].get("trade_log", False),
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
        )

        # 스크리닝/테마 로그 (별도 파일)
        logger.add(
            log_path / f"screening_{today}.log",
            format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
            level="INFO",
            filter=lambda record: record["extra"].get("screening_log", False),
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
        )

    logger.info(f"로거 설정 완료: level={log_level}, dir={log_dir}")


def cleanup_old_logs(log_base_dir: str, max_days: int = 7):
    """
    오래된 로그 파일/디렉터리 정리

    - YYYYMMDD 형식 디렉터리 삭제
    - 오래된 .log 파일 삭제
    """
    base = Path(log_base_dir)
    if not base.exists():
        return

    cutoff = datetime.now() - timedelta(days=max_days)
    cutoff_str = cutoff.strftime("%Y%m%d")
    removed_dirs = 0
    removed_files = 0

    # YYYYMMDD 디렉터리 정리
    for entry in base.iterdir():
        if entry.is_dir() and re.match(r"^\d{8}$", entry.name):
            if entry.name < cutoff_str:
                try:
                    import shutil
                    shutil.rmtree(entry)
                    removed_dirs += 1
                except Exception as e:
                    logger.warning(f"[cleanup] 디렉터리 삭제 실패: {entry} - {e}")

    # base 디렉터리 내 오래된 .log 파일 정리
    for entry in base.iterdir():
        if entry.is_file() and entry.suffix == ".log":
            try:
                mtime = datetime.fromtimestamp(entry.stat().st_mtime)
                if mtime < cutoff:
                    entry.unlink()
                    removed_files += 1
            except Exception as e:
                logger.warning(f"[cleanup] 파일 삭제 실패: {entry} - {e}")

    if removed_dirs or removed_files:
        logger.info(
            f"[cleanup] 로그 정리 완료: 디렉터리 {removed_dirs}개, 파일 {removed_files}개 삭제 "
            f"(기준: {max_days}일)"
        )


def cleanup_old_cache(max_days: int = 7):
    """
    오래된 캐시 JSON 파일 정리

    - ~/.cache/ai_trader/journal/trades_*.json
    - ~/.cache/ai_trader/evolution/advice_*.json
    """
    cache_base = Path.home() / ".cache" / "ai_trader"
    if not cache_base.exists():
        return

    cutoff = datetime.now() - timedelta(days=max_days)
    removed = 0

    patterns = [
        cache_base / "journal" / "trades_*.json",
        cache_base / "evolution" / "advice_*.json",
    ]

    for pattern in patterns:
        parent = pattern.parent
        if not parent.exists():
            continue
        glob_pattern = pattern.name
        for f in parent.glob(glob_pattern):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
                    removed += 1
            except Exception as e:
                logger.warning(f"[cleanup] 캐시 파일 삭제 실패: {f} - {e}")

    if removed:
        logger.info(f"[cleanup] 캐시 정리 완료: {removed}개 파일 삭제 (기준: {max_days}일)")


def get_trade_logger():
    """거래 전용 로거"""
    return logger.bind(trade_log=True)


class TradingLogger:
    """
    거래 로깅 유틸리티

    거래 이벤트를 구조화된 형식으로 기록
    복기용 JSON 로그 자동 생성
    """

    def __init__(self):
        self._trade_logger = logger.bind(trade_log=True)
        self._screening_logger = logger.bind(screening_log=True)
        self._daily_records: List[Dict[str, Any]] = []
        self._log_dir: Optional[Path] = None

    def set_log_dir(self, log_dir: str):
        """로그 디렉토리 설정 (JSON 저장용)"""
        self._log_dir = Path(log_dir)

    def _add_record(self, record_type: str, data: Dict[str, Any]):
        """일일 JSON 기록에 추가"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "type": record_type,
            **data
        }
        self._daily_records.append(record)

    def log_signal(
        self,
        symbol: str,
        side: str,
        strength: str,
        score: float,
        reason: str,
        price: float,
        strategy: str = "",
    ):
        """신호 로깅"""
        self._trade_logger.info(
            f"[SIGNAL] {symbol} {side} | 전략={strategy} 강도={strength} 점수={score:.0f} | "
            f"가격={price:,.0f} | {reason}"
        )
        self._add_record("signal", {
            "symbol": symbol,
            "side": side,
            "strategy": strategy,
            "strength": strength,
            "score": score,
            "price": price,
            "reason": reason,
        })

    def log_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        status: str = "submitted",
        order_id: str = "",
    ):
        """주문 로깅"""
        self._trade_logger.info(
            f"[ORDER] {symbol} {side} {quantity}주 @ {price:,.0f}원 | "
            f"유형={order_type} 상태={status}"
        )
        self._add_record("order", {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "order_type": order_type,
            "status": status,
            "order_id": order_id,
        })

    def log_fill(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        commission: float,
    ):
        """체결 로깅"""
        total = quantity * price
        self._trade_logger.info(
            f"[FILL] {symbol} {side} {quantity}주 @ {price:,.0f}원 | "
            f"총액={total:,.0f}원 수수료={commission:,.0f}원"
        )
        self._add_record("fill", {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "total": total,
            "commission": commission,
        })

    def log_exit(
        self,
        symbol: str,
        quantity: int,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        reason: str,
    ):
        """청산 로깅 (분할 익절/손절)"""
        self._trade_logger.info(
            f"[EXIT] {symbol} {quantity}주 | "
            f"진입={entry_price:,.0f} 청산={exit_price:,.0f} | "
            f"손익={pnl:+,.0f}원 ({pnl_pct:+.2f}%) | {reason}"
        )
        self._add_record("exit", {
            "symbol": symbol,
            "quantity": quantity,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
        })

    def log_position_update(
        self,
        symbol: str,
        action: str,
        quantity: int,
        avg_price: float,
        pnl: Optional[float] = None,
    ):
        """포지션 변경 로깅"""
        msg = f"[POSITION] {symbol} {action} | {quantity}주 @ {avg_price:,.0f}원"
        if pnl is not None:
            msg += f" | P&L={pnl:+,.0f}원"
        self._trade_logger.info(msg)

    def log_risk_alert(
        self,
        alert_type: str,
        message: str,
        action: str,
    ):
        """리스크 경고 로깅"""
        self._trade_logger.warning(
            f"[RISK] {alert_type} | {message} | 조치={action}"
        )
        self._add_record("risk_alert", {
            "alert_type": alert_type,
            "message": message,
            "action": action,
        })

    # ============================================================
    # 스크리닝/테마 로그
    # ============================================================

    def log_screening(
        self,
        source: str,
        total_stocks: int,
        top_stocks: List[Dict[str, Any]],
    ):
        """스크리닝 결과 로깅"""
        self._screening_logger.info(
            f"[SCREENING] 소스={source} | 총 {total_stocks}개 종목 발굴"
        )
        for stock in top_stocks[:10]:
            self._screening_logger.info(
                f"  - {stock.get('symbol')} {stock.get('name', '')}: "
                f"점수={stock.get('score', 0):.0f} | {stock.get('reasons', [])}"
            )
        self._add_record("screening", {
            "source": source,
            "total_stocks": total_stocks,
            "top_stocks": top_stocks[:20],
        })

    def log_theme(
        self,
        theme_name: str,
        score: float,
        keywords: List[str],
        related_stocks: List[str],
        news_count: int = 0,
    ):
        """테마 탐지 결과 로깅"""
        self._screening_logger.info(
            f"[THEME] {theme_name} | 점수={score:.0f} | "
            f"키워드={keywords[:5]} | 관련종목={related_stocks[:5]}"
        )
        self._add_record("theme", {
            "theme_name": theme_name,
            "score": score,
            "keywords": keywords,
            "related_stocks": related_stocks,
            "news_count": news_count,
        })

    def log_watchlist_update(
        self,
        added: List[str],
        removed: List[str],
        total: int,
    ):
        """감시 종목 변경 로깅"""
        self._screening_logger.info(
            f"[WATCHLIST] 추가={len(added)} 제거={len(removed)} 총={total}개"
        )
        if added:
            self._screening_logger.info(f"  추가: {added[:10]}")

    def log_evolution(
        self,
        assessment: str,
        confidence: float,
        insights: List[str],
        parameter_changes: List[Dict[str, Any]],
    ):
        """자가 진화 결과 로깅"""
        self._screening_logger.info(
            f"[EVOLUTION] 평가={assessment.upper()} | 신뢰도={confidence:.0%} | "
            f"인사이트={len(insights)}개 | 파라미터변경={len(parameter_changes)}개"
        )
        for insight in insights[:5]:
            self._screening_logger.info(f"  [인사이트] {insight}")
        for change in parameter_changes:
            self._screening_logger.info(
                f"  [파라미터] {change.get('parameter')}: "
                f"{change.get('from')} -> {change.get('to')} "
                f"(신뢰도: {change.get('confidence', 0):.0%})"
            )
        self._add_record("evolution", {
            "assessment": assessment,
            "confidence": confidence,
            "insights": insights,
            "parameter_changes": parameter_changes,
        })

    # ============================================================
    # 일일 요약
    # ============================================================

    def log_daily_summary(
        self,
        total_trades: int,
        wins: int,
        losses: int,
        total_pnl: float,
        pnl_pct: float,
        positions: List[Dict[str, Any]] = None,
    ):
        """일일 요약 로깅"""
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0

        self._trade_logger.info(
            f"[DAILY SUMMARY] "
            f"거래={total_trades}회 | 승={wins} 패={losses} | "
            f"승률={win_rate:.1f}% | "
            f"손익={total_pnl:+,.0f}원 ({pnl_pct:+.2f}%)"
        )

        summary = {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "pnl_pct": pnl_pct,
            "positions": positions or [],
        }
        self._add_record("daily_summary", summary)

        # JSON 파일로 저장
        self._save_daily_json()

    def _save_daily_json(self):
        """일일 복기용 JSON 저장"""
        if not self._log_dir or not self._daily_records:
            return

        try:
            today = datetime.now().strftime("%Y%m%d")
            json_path = self._log_dir / f"daily_{today}.json"

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({
                    "date": today,
                    "generated_at": datetime.now().isoformat(),
                    "records": self._daily_records,
                    "summary": self._generate_summary(),
                }, f, ensure_ascii=False, indent=2)

            logger.info(f"[LOG] 일일 복기 JSON 저장: {json_path}")

        except Exception as e:
            logger.error(f"JSON 로그 저장 실패: {e}")

    def _generate_summary(self) -> Dict[str, Any]:
        """일일 기록 요약 생성"""
        signals = [r for r in self._daily_records if r["type"] == "signal"]
        orders = [r for r in self._daily_records if r["type"] == "order"]
        fills = [r for r in self._daily_records if r["type"] == "fill"]
        exits = [r for r in self._daily_records if r["type"] == "exit"]

        total_pnl = sum(e.get("pnl", 0) for e in exits)
        wins = len([e for e in exits if e.get("pnl", 0) > 0])
        losses = len([e for e in exits if e.get("pnl", 0) < 0])

        return {
            "total_signals": len(signals),
            "total_orders": len(orders),
            "total_fills": len(fills),
            "total_exits": len(exits),
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
        }

    def flush(self):
        """현재까지 기록 저장 (강제)"""
        self._save_daily_json()

    # ============================================================
    # 세션/포트폴리오/신호 차단 로그 (진화 시스템 연동)
    # ============================================================

    def log_session_change(
        self,
        new_session: str,
        prev_session: str = "",
        details: str = "",
    ):
        """세션 변경 상세 로깅"""
        self._trade_logger.info(
            f"[SESSION] {prev_session} → {new_session}"
            + (f" | {details}" if details else "")
        )
        self._add_record("session_change", {
            "new_session": new_session,
            "prev_session": prev_session,
            "details": details,
        })

    def log_portfolio_sync(
        self,
        ghost_removed: int,
        new_added: int,
        total_positions: int,
        cash: float,
        total_equity: float,
    ):
        """포트폴리오 동기화 결과 로깅"""
        self._trade_logger.info(
            f"[SYNC] 유령제거={ghost_removed} 신규추가={new_added} "
            f"보유={total_positions}종목 | 현금={cash:,.0f}원 총자산={total_equity:,.0f}원"
        )
        self._add_record("portfolio_sync", {
            "ghost_removed": ghost_removed,
            "new_added": new_added,
            "total_positions": total_positions,
            "cash": cash,
            "total_equity": total_equity,
        })

    def log_signal_blocked(
        self,
        symbol: str,
        side: str,
        reason: str,
        price: float = 0,
        score: float = 0,
    ):
        """신호 차단 로깅 (진화 학습 핵심 데이터)"""
        self._trade_logger.info(
            f"[BLOCKED] {symbol} {side} | 사유={reason} | "
            f"가격={price:,.0f} 점수={score:.0f}"
        )
        self._add_record("signal_blocked", {
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "price": price,
            "score": score,
        })

    def get_evolution_context(self) -> Dict[str, Any]:
        """
        진화 시스템용 컨텍스트 데이터 반환

        차단 통계, 리스크 경고, 테마/스크리닝 요약
        """
        blocked = [r for r in self._daily_records if r["type"] == "signal_blocked"]
        risk_alerts = [r for r in self._daily_records if r["type"] == "risk_alert"]
        themes = [r for r in self._daily_records if r["type"] == "theme"]
        screenings = [r for r in self._daily_records if r["type"] == "screening"]

        # 차단 사유별 통계
        block_reasons: Dict[str, int] = {}
        for b in blocked:
            reason = b.get("reason", "unknown")
            block_reasons[reason] = block_reasons.get(reason, 0) + 1

        return {
            "blocked_signals": {
                "total": len(blocked),
                "by_reason": block_reasons,
            },
            "risk_alerts": {
                "total": len(risk_alerts),
                "details": [
                    {"type": r.get("alert_type"), "message": r.get("message")}
                    for r in risk_alerts[:10]
                ],
            },
            "themes_detected": len(themes),
            "screenings_run": len(screenings),
        }


# 전역 인스턴스
trading_logger = TradingLogger()
