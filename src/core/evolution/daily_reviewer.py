"""
AI Trading Bot v2 - 일일 거래 리뷰어 (Daily Reviewer)

대시보드용 일일 거래 리포트와 LLM 종합 평가를 생성합니다.
- review_YYYYMMDD.json: 거래 통계 리포트 (17:00)
- llm_review_YYYYMMDD.json: LLM 종합 평가 (20:30)
"""

import json
import os
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Any

from loguru import logger

from .trade_journal import TradeJournal, TradeRecord, get_trade_journal
from ...utils.llm import LLMManager, LLMTask, get_llm_manager
from ...utils.telegram import send_alert


# LLM 시스템 프롬프트
_REVIEW_SYSTEM_PROMPT = """당신은 경험 많은 퀀트 트레이더이자 전략 분석가입니다.
오늘 하루의 거래를 상세히 복기하고, 각 거래의 성공/실패 원인을 분석해주세요.

## 분석 원칙
1. 각 거래별로 진입 판단의 적절성을 평가
2. 청산 타이밍과 방법의 효율성 분석
3. 지표 활용의 적합성 검토
4. 반복되는 실수 패턴 식별
5. 성공 패턴 강화 방안 제시

## 목표
- 일평균 수익률 1% 달성
- 승률 55% 이상 + 손익비 1.5 이상

## 응답 형식
반드시 유효한 JSON 형식으로만 응답하세요. 마크다운 코드 블록이나 설명 없이 JSON만 출력하세요."""

# LLM 응답 JSON 스키마 (프롬프트에 포함)
_RESPONSE_SCHEMA = """{
  "assessment": "good 또는 fair 또는 poor",
  "confidence": 0.0~1.0,
  "daily_return_pct": -0.52,
  "trade_reviews": [
    {
      "symbol": "005930",
      "name": "삼성전자",
      "pnl_pct": 2.1,
      "review": "상세 복기 코멘트",
      "lesson": "교훈"
    }
  ],
  "insights": ["인사이트1", "인사이트2"],
  "avoid_patterns": ["패턴1", "패턴2"],
  "focus_opportunities": ["기회1", "기회2"],
  "parameter_suggestions": [
    {
      "strategy": "momentum_breakout",
      "parameter": "min_score",
      "current_value": 65,
      "suggested_value": 70,
      "reason": "이유",
      "confidence": 0.8
    }
  ],
  "telegram_summary": "📊 2/14 거래 리뷰\\n승률 40% (2/5) | 손익 -45,230원\\n..."
}"""


def _parse_date_str(date_str: Optional[str]) -> date:
    """날짜 문자열(YYYY-MM-DD)을 date 객체로 변환. None이면 오늘."""
    if date_str is None:
        return date.today()
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _date_to_file_suffix(d: date) -> str:
    """date 객체를 파일명용 YYYYMMDD 문자열로 변환."""
    return d.strftime("%Y%m%d")


def _format_trade_for_prompt(trade: TradeRecord) -> Dict[str, Any]:
    """TradeRecord를 LLM 프롬프트용 딕셔너리로 변환."""
    return {
        "symbol": trade.symbol,
        "name": trade.name,
        "strategy": trade.entry_strategy,
        "entry_time": trade.entry_time.strftime("%H:%M") if trade.entry_time else "",
        "exit_time": trade.exit_time.strftime("%H:%M") if trade.exit_time else "",
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "quantity": trade.entry_quantity,
        "pnl": trade.pnl,
        "pnl_pct": round(trade.pnl_pct, 2),
        "holding_minutes": trade.holding_minutes,
        "entry_reason": trade.entry_reason,
        "exit_reason": trade.exit_reason,
        "exit_type": trade.exit_type,
        "indicators_at_entry": trade.indicators_at_entry,
    }


class DailyReviewer:
    """
    일일 거래 리뷰어

    두 종류의 리포트를 생성합니다:
    1. 거래 통계 리포트 (review_YYYYMMDD.json) — 17:00
    2. LLM 종합 평가 (llm_review_YYYYMMDD.json) — 20:30
    """

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        llm_manager: Optional[LLMManager] = None,
    ):
        self.storage_dir = Path(storage_dir or os.getenv(
            "TRADE_JOURNAL_DIR",
            os.path.expanduser("~/.cache/ai_trader/journal")
        ))
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.llm = llm_manager or get_llm_manager()

        logger.info(f"[거래리뷰] DailyReviewer 초기화: {self.storage_dir}")

    # ─── 파일 경로 ──────────────────────────────────────────

    def _review_path(self, d: date) -> Path:
        """거래 리포트 파일 경로."""
        return self.storage_dir / f"review_{_date_to_file_suffix(d)}.json"

    def _llm_review_path(self, d: date) -> Path:
        """LLM 종합 평가 파일 경로."""
        return self.storage_dir / f"llm_review_{_date_to_file_suffix(d)}.json"

    # ─── 거래 리포트 생성 ───────────────────────────────────

    def generate_trade_report(
        self,
        trade_journal: TradeJournal,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        일일 거래 통계 리포트를 생성하고 JSON 파일로 저장한다.

        Args:
            trade_journal: 거래 저널 인스턴스
            date_str: 대상 날짜 (YYYY-MM-DD). None이면 오늘.

        Returns:
            리포트 딕셔너리
        """
        target_date = _parse_date_str(date_str)
        trades = trade_journal.get_trades_by_date(target_date)
        closed_trades = [t for t in trades if t.is_closed]

        logger.info(
            f"[거래리뷰] 거래 리포트 생성: {target_date} "
            f"(전체 {len(trades)}건, 청산 {len(closed_trades)}건)"
        )

        # 개별 거래 정보
        trade_details = []
        for t in closed_trades:
            trade_details.append({
                "symbol": t.symbol,
                "name": t.name,
                "strategy": t.entry_strategy,
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.entry_quantity,
                "pnl": t.pnl,
                "pnl_pct": round(t.pnl_pct, 2),
                "holding_minutes": t.holding_minutes,
                "entry_reason": t.entry_reason,
                "exit_reason": t.exit_reason,
                "exit_type": t.exit_type,
                "indicators_at_entry": t.indicators_at_entry,
            })

        # 요약 통계
        summary = self._calculate_summary(closed_trades)

        # 전략별 성과
        strategy_performance = self._calculate_strategy_performance(closed_trades)

        report = {
            "date": target_date.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "trades": trade_details,
            "summary": summary,
            "strategy_performance": strategy_performance,
        }

        # 파일 저장
        try:
            file_path = self._review_path(target_date)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            logger.info(f"[거래리뷰] 거래 리포트 저장: {file_path}")
        except Exception as e:
            logger.error(f"[거래리뷰] 거래 리포트 저장 실패: {e}")

        return report

    def _calculate_summary(self, trades: List[TradeRecord]) -> Dict[str, Any]:
        """청산 거래 목록에서 요약 통계를 계산한다."""
        if not trades:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "total_pnl_pct": 0.0,
                "profit_factor": 0.0,
                "best_trade": None,
                "worst_trade": None,
            }

        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]

        total_profit = sum(t.pnl for t in wins)
        total_loss = abs(sum(t.pnl for t in losses))

        # 손실 0원 시 profit_factor 상한 99.9 (왜곡 방지)
        if total_loss > 0:
            profit_factor = min(total_profit / total_loss, 99.9)
        elif total_profit > 0:
            profit_factor = 99.9
        else:
            profit_factor = 0.0

        total_pnl = sum(t.pnl for t in trades)
        total_pnl_pct = sum(t.pnl_pct for t in trades)

        best = max(trades, key=lambda t: t.pnl_pct)
        worst = min(trades, key=lambda t: t.pnl_pct)

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl": round(total_pnl, 0),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "profit_factor": round(profit_factor, 2),
            "best_trade": {
                "symbol": best.symbol,
                "name": best.name,
                "pnl_pct": round(best.pnl_pct, 2),
                "pnl": round(best.pnl, 0),
            },
            "worst_trade": {
                "symbol": worst.symbol,
                "name": worst.name,
                "pnl_pct": round(worst.pnl_pct, 2),
                "pnl": round(worst.pnl, 0),
            },
        }

    def _calculate_strategy_performance(
        self,
        trades: List[TradeRecord],
    ) -> Dict[str, Dict[str, Any]]:
        """전략별 성과를 계산한다."""
        stats: Dict[str, Dict[str, Any]] = {}

        for trade in trades:
            strategy = trade.entry_strategy or "unknown"
            if strategy not in stats:
                stats[strategy] = {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0.0,
                    "total_pnl_pct": 0.0,
                }

            stats[strategy]["trades"] += 1
            if trade.is_win:
                stats[strategy]["wins"] += 1
            else:
                stats[strategy]["losses"] += 1
            stats[strategy]["total_pnl"] += trade.pnl
            stats[strategy]["total_pnl_pct"] += trade.pnl_pct

        # 평균/승률 계산
        for s in stats.values():
            count = s["trades"]
            s["avg_pnl_pct"] = round(s["total_pnl_pct"] / count, 2) if count > 0 else 0.0
            s["win_rate"] = round(s["wins"] / count * 100, 1) if count > 0 else 0.0
            s["total_pnl"] = round(s["total_pnl"], 0)
            s["total_pnl_pct"] = round(s["total_pnl_pct"], 2)

        return stats

    # ─── LLM 종합 평가 ─────────────────────────────────────

    async def generate_llm_review(
        self,
        trade_journal: TradeJournal,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        LLM을 사용한 종합 거래 평가를 생성하고 저장한다.

        기존 거래 리포트가 없으면 먼저 생성한 후 LLM에 전달한다.

        Args:
            trade_journal: 거래 저널 인스턴스
            date_str: 대상 날짜 (YYYY-MM-DD). None이면 오늘.

        Returns:
            LLM 평가 딕셔너리
        """
        target_date = _parse_date_str(date_str)
        date_str_formatted = target_date.isoformat()

        logger.info(f"[거래리뷰] LLM 종합 평가 시작: {date_str_formatted}")

        # 거래 리포트 로드 (없으면 생성)
        report = self.load_report(date_str_formatted)
        if report is None:
            report = self.generate_trade_report(trade_journal, date_str_formatted)

        trades = report.get("trades", [])
        summary = report.get("summary", {})
        strategy_performance = report.get("strategy_performance", {})

        # 거래가 없으면 기본 리뷰 반환
        if summary.get("total_trades", 0) == 0:
            llm_review = self._create_empty_review(target_date)
            self._save_llm_review(target_date, llm_review)
            logger.info("[거래리뷰] 거래 없음 — 빈 리뷰 저장")
            return llm_review

        # LLM 프롬프트 구성
        prompt = self._build_llm_prompt(target_date, trades, summary, strategy_performance)

        # LLM 호출
        try:
            llm_response = await self.llm.complete(
                prompt,
                task=LLMTask.TRADE_REVIEW,
                system=_REVIEW_SYSTEM_PROMPT,
            )

            if not llm_response.success or not llm_response.content:
                raise ValueError(llm_response.error or "LLM 응답 없음")

            # JSON 파싱
            llm_review = self._parse_llm_response(llm_response.content, target_date)
            logger.info(
                f"[거래리뷰] LLM 평가 완료: "
                f"assessment={llm_review.get('assessment')}, "
                f"trade_reviews={len(llm_review.get('trade_reviews', []))}건"
            )

        except Exception as e:
            logger.error(f"[거래리뷰] LLM 평가 실패, 폴백 생성: {e}")
            llm_review = self._create_fallback_review(target_date, summary, trades)

        # 파일 저장
        self._save_llm_review(target_date, llm_review)

        # 텔레그램 알림
        telegram_summary = llm_review.get("telegram_summary", "")
        if telegram_summary:
            try:
                await send_alert(telegram_summary)
                logger.info("[거래리뷰] 텔레그램 리뷰 알림 발송 완료")
            except Exception as e:
                logger.warning(f"[거래리뷰] 텔레그램 발송 실패: {e}")

        return llm_review

    def _build_llm_prompt(
        self,
        target_date: date,
        trades: List[Dict[str, Any]],
        summary: Dict[str, Any],
        strategy_performance: Dict[str, Dict[str, Any]],
    ) -> str:
        """LLM에 전달할 분석 프롬프트를 구성한다."""
        date_display = target_date.strftime("%Y-%m-%d (%a)")

        lines = [
            f"# {date_display} 거래 리뷰 요청",
            "",
            "## 요약 통계",
            f"- 총 거래: {summary.get('total_trades', 0)}건",
            f"- 승리: {summary.get('wins', 0)}건 / 패배: {summary.get('losses', 0)}건",
            f"- 승률: {summary.get('win_rate', 0):.1f}%",
            f"- 총 손익: {summary.get('total_pnl', 0):+,.0f}원 ({summary.get('total_pnl_pct', 0):+.2f}%)",
            f"- Profit Factor: {summary.get('profit_factor', 0):.2f}",
        ]

        # 최고/최악 거래
        best = summary.get("best_trade")
        worst = summary.get("worst_trade")
        if best:
            lines.append(f"- 최고 거래: {best['name']}({best['symbol']}) {best['pnl_pct']:+.2f}%")
        if worst:
            lines.append(f"- 최악 거래: {worst['name']}({worst['symbol']}) {worst['pnl_pct']:+.2f}%")

        # 전략별 성과
        if strategy_performance:
            lines.extend(["", "## 전략별 성과"])
            for strategy, perf in strategy_performance.items():
                lines.append(
                    f"- {strategy}: {perf['trades']}건, "
                    f"승률 {perf['win_rate']:.1f}%, "
                    f"평균 {perf['avg_pnl_pct']:+.2f}%, "
                    f"총손익 {perf['total_pnl']:+,.0f}원"
                )

        # 개별 거래 상세
        lines.extend(["", "## 개별 거래 상세"])
        for i, t in enumerate(trades, 1):
            lines.extend([
                f"",
                f"### 거래 {i}: {t.get('name', '')} ({t.get('symbol', '')})",
                f"- 전략: {t.get('strategy', '')}",
                f"- 진입: {t.get('entry_time', '')} @ {t.get('entry_price', 0):,.0f}원",
                f"- 청산: {t.get('exit_time', '')} @ {t.get('exit_price', 0):,.0f}원",
                f"- 수량: {t.get('quantity', 0)}주",
                f"- 손익: {t.get('pnl', 0):+,.0f}원 ({t.get('pnl_pct', 0):+.2f}%)",
                f"- 보유시간: {t.get('holding_minutes', 0)}분",
                f"- 진입사유: {t.get('entry_reason', '')}",
                f"- 청산사유: {t.get('exit_reason', '')}",
                f"- 청산유형: {t.get('exit_type', '')}",
            ])

            indicators = t.get("indicators_at_entry", {})
            if indicators:
                indicator_parts = [f"{k}={v}" for k, v in indicators.items() if isinstance(v, (int, float))]
                if indicator_parts:
                    lines.append(f"- 진입지표: {', '.join(indicator_parts[:8])}")

        # 응답 형식 안내
        lines.extend([
            "",
            "## 응답 형식",
            "다음 JSON 형식으로 응답해주세요:",
            _RESPONSE_SCHEMA,
        ])

        return "\n".join(lines)

    def _parse_llm_response(
        self,
        response_text: str,
        target_date: date,
    ) -> Dict[str, Any]:
        """LLM 응답에서 JSON을 추출하고 파싱한다."""
        # JSON 블록 추출
        json_start = response_text.find("{")
        json_end = response_text.rfind("}") + 1

        if json_start == -1 or json_end <= 0:
            raise ValueError("LLM 응답에서 JSON을 찾을 수 없음")

        json_str = response_text[json_start:json_end]
        data = json.loads(json_str)

        # 메타데이터 추가
        data["date"] = target_date.isoformat()
        data["generated_at"] = datetime.now().isoformat()
        data["source"] = "llm"

        return data

    def _create_empty_review(self, target_date: date) -> Dict[str, Any]:
        """거래가 없는 날의 빈 리뷰를 생성한다."""
        return {
            "date": target_date.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "source": "empty",
            "assessment": "no_data",
            "confidence": 0.0,
            "daily_return_pct": 0.0,
            "trade_reviews": [],
            "insights": ["오늘은 거래가 없습니다."],
            "avoid_patterns": [],
            "focus_opportunities": [],
            "parameter_suggestions": [],
            "telegram_summary": "",
        }

    def _create_fallback_review(
        self,
        target_date: date,
        summary: Dict[str, Any],
        trades: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """LLM 실패 시 기본 통계 기반 폴백 리뷰를 생성한다."""
        total_trades = summary.get("total_trades", 0)
        win_rate = summary.get("win_rate", 0)
        total_pnl = summary.get("total_pnl", 0)
        total_pnl_pct = summary.get("total_pnl_pct", 0)
        profit_factor = summary.get("profit_factor", 0)

        # 평가 결정
        if total_pnl_pct >= 1.0 and win_rate >= 50:
            assessment = "good"
        elif total_pnl_pct < 0 or win_rate < 40 or profit_factor < 1.0:
            assessment = "poor"
        else:
            assessment = "fair"

        # 인사이트 생성
        insights = []
        if win_rate < 40:
            insights.append(f"승률 {win_rate:.1f}%로 낮음 — 진입 조건 강화 필요")
        if profit_factor < 1.0:
            insights.append(f"Profit Factor {profit_factor:.2f}로 1 미만 — 손절 관리 필요")
        if total_pnl_pct < 0:
            insights.append(f"일 수익률 {total_pnl_pct:+.2f}% 손실 — 원인 분석 필요")
        if not insights:
            insights.append(f"일 수익률 {total_pnl_pct:+.2f}%, 승률 {win_rate:.1f}%")

        # 간단한 거래별 리뷰
        trade_reviews = []
        for t in trades:
            pnl_pct = t.get("pnl_pct", 0)
            if pnl_pct > 0:
                review_comment = "수익 거래"
            else:
                review_comment = f"손실 거래 ({t.get('exit_type', '')})"

            trade_reviews.append({
                "symbol": t.get("symbol", ""),
                "name": t.get("name", ""),
                "pnl_pct": pnl_pct,
                "review": review_comment,
                "lesson": "LLM 분석 실패로 상세 복기 불가",
            })

        date_display = target_date.strftime("%-m/%-d")
        telegram_summary = (
            f"<b>{date_display} 거래 리뷰 (자동)</b>\n"
            f"승률 {win_rate:.0f}% ({summary.get('wins', 0)}/{total_trades}) | "
            f"손익 {total_pnl:+,.0f}원 ({total_pnl_pct:+.2f}%)\n"
            f"PF {profit_factor:.2f}"
        )

        return {
            "date": target_date.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "source": "fallback",
            "assessment": assessment,
            "confidence": 0.3,
            "daily_return_pct": total_pnl_pct,
            "trade_reviews": trade_reviews,
            "insights": insights,
            "avoid_patterns": [],
            "focus_opportunities": [],
            "parameter_suggestions": [],
            "telegram_summary": telegram_summary,
        }

    def _save_llm_review(self, target_date: date, review: Dict[str, Any]) -> None:
        """LLM 리뷰를 파일에 저장한다."""
        try:
            file_path = self._llm_review_path(target_date)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(review, f, ensure_ascii=False, indent=2)
            logger.info(f"[거래리뷰] LLM 리뷰 저장: {file_path}")
        except Exception as e:
            logger.error(f"[거래리뷰] LLM 리뷰 저장 실패: {e}")

    # ─── 리포트 조회 ───────────────────────────────────────

    def load_report(self, date_str: str) -> Optional[Dict[str, Any]]:
        """
        거래 리포트(review_YYYYMMDD.json)를 로드한다.

        Args:
            date_str: 날짜 (YYYY-MM-DD)

        Returns:
            리포트 딕셔너리 또는 None
        """
        target_date = _parse_date_str(date_str)
        file_path = self._review_path(target_date)

        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[거래리뷰] 리포트 로드 실패 ({file_path}): {e}")
            return None

    def load_llm_review(self, date_str: str) -> Optional[Dict[str, Any]]:
        """
        LLM 종합 평가(llm_review_YYYYMMDD.json)를 로드한다.

        Args:
            date_str: 날짜 (YYYY-MM-DD)

        Returns:
            LLM 평가 딕셔너리 또는 None
        """
        target_date = _parse_date_str(date_str)
        file_path = self._llm_review_path(target_date)

        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[거래리뷰] LLM 리뷰 로드 실패 ({file_path}): {e}")
            return None

    def list_available_dates(self) -> List[str]:
        """
        리뷰 파일이 존재하는 날짜 목록을 반환한다.

        review_YYYYMMDD.json 또는 llm_review_YYYYMMDD.json이 존재하는
        날짜를 YYYY-MM-DD 형식으로 정렬하여 반환한다.

        Returns:
            날짜 문자열 리스트 (오름차순)
        """
        dates = set()

        try:
            for file_path in self.storage_dir.iterdir():
                name = file_path.name

                # review_YYYYMMDD.json
                if name.startswith("review_") and name.endswith(".json"):
                    date_part = name[7:15]  # "review_" = 7글자
                    if len(date_part) == 8 and date_part.isdigit():
                        formatted = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                        dates.add(formatted)

                # llm_review_YYYYMMDD.json
                if name.startswith("llm_review_") and name.endswith(".json"):
                    date_part = name[11:19]  # "llm_review_" = 11글자
                    if len(date_part) == 8 and date_part.isdigit():
                        formatted = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                        dates.add(formatted)

        except Exception as e:
            logger.error(f"[거래리뷰] 날짜 목록 조회 실패: {e}")

        return sorted(dates)


# 싱글톤 인스턴스
_daily_reviewer: Optional[DailyReviewer] = None


def get_daily_reviewer() -> DailyReviewer:
    """DailyReviewer 싱글톤 인스턴스를 반환한다."""
    global _daily_reviewer
    if _daily_reviewer is None:
        _daily_reviewer = DailyReviewer()
    return _daily_reviewer
