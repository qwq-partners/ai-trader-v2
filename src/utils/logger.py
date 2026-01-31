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
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger


def setup_logger(
    log_level: str = "INFO",
    log_dir: Optional[str] = None,
    rotation: str = "1 day",
    retention: str = "30 days",
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


# 전역 인스턴스
trading_logger = TradingLogger()
