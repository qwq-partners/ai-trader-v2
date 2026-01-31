"""
AI Trading Bot v2 - 일일 투자 레포트 시스템

매일 아침 8시: 오늘의 추천 종목 레포트
매일 오후 5시: 추천 종목 결과 레포트
"""

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
from loguru import logger

# 프로젝트 내 모듈
from ..utils.telegram import get_telegram_notifier, TelegramNotifier
from ..signals.screener import get_screener, ScreenedStock
from ..signals.sentiment.theme_detector import get_theme_detector, NewsCollector


@dataclass
class RecommendedStock:
    """추천 종목"""
    rank: int
    symbol: str
    name: str

    # 투자 포인트
    investment_thesis: str        # 왜 이 종목인가? (1줄 요약)
    catalyst: str                 # 촉매 (상승 이유)

    # 가격 정보
    prev_close: float = 0        # 전일 종가
    target_entry: float = 0      # 목표 진입가
    target_exit: float = 0       # 목표 청산가 (익절)
    stop_loss: float = 0         # 손절가

    # 점수
    news_score: float = 0        # 뉴스 기반 점수 (0~100)
    tech_score: float = 0        # 기술적 점수 (0~100)
    theme_score: float = 0       # 테마 점수 (0~100)
    total_score: float = 0       # 종합 점수

    # 리스크
    risk_level: str = "중"       # 낮음/중/높음
    risk_factors: List[str] = field(default_factory=list)

    # 관련 정보
    related_theme: str = ""      # 관련 테마
    key_news: str = ""           # 핵심 뉴스 요약

    # 결과 (오후 리포트용)
    result_price: Optional[float] = None
    result_pct: Optional[float] = None


class DailyReportGenerator:
    """
    일일 투자 레포트 생성기

    투자자 관점에서 실제로 도움이 되는 레포트를 생성합니다.

    핵심 원칙:
    1. 간결하고 명확하게 - 한눈에 파악 가능
    2. 액션 가이드 제공 - 무엇을 언제 얼마에 살지
    3. 리스크 경고 - 어떤 위험이 있는지
    4. 근거 제시 - 왜 이 종목인지
    """

    def __init__(self, kis_market_data=None):
        self.telegram = get_telegram_notifier()
        self.screener = get_screener()
        self.theme_detector = get_theme_detector()
        self.news_collector = NewsCollector()
        self._kis_market_data = kis_market_data
        self._us_market_data = None

        # 오늘의 추천 종목 저장 (오후 결과 리포트용)
        self._today_recommendations: List[RecommendedStock] = []
        self._recommendation_date: Optional[date] = None
        self._today_news: List[Dict] = []  # 당일 핵심 뉴스

    async def generate_morning_report(
        self,
        llm_manager=None,
        max_stocks: int = 10,
        send_telegram: bool = True,
    ) -> str:
        """
        아침 8시 추천 종목 레포트 생성

        Args:
            llm_manager: LLM 매니저 (뉴스 분석용)
            max_stocks: 추천 종목 수 (최소 10개)
            send_telegram: 텔레그램 발송 여부

        Returns:
            레포트 메시지
        """
        logger.info("[레포트] 아침 추천 종목 레포트 생성 시작")

        today = date.today()
        max_stocks = max(max_stocks, 10)  # 최소 10개 보장

        # LLM 매니저 자동 연결 (미전달 시)
        if llm_manager is None:
            try:
                from ..utils.llm import get_llm_manager
                llm_manager = get_llm_manager()
            except Exception as e:
                logger.warning(f"LLM 매니저 자동 연결 실패: {e}")

        # 1. 종목 스크리닝 (5,000원 미만 소형주 제외, theme_detector 연동)
        screened = await self.screener.screen_all(
            llm_manager=llm_manager,
            min_price=5000,
            theme_detector=self.theme_detector,
        )

        # 2. 테마 탐지
        hot_themes = []
        if self.theme_detector:
            try:
                themes = await self.theme_detector.detect_themes()
                hot_themes = [t for t in themes if t.score >= 60][:5]
            except Exception as e:
                logger.warning(f"테마 탐지 실패: {e}")

        # 3. 종목 점수 재계산 및 순위 결정
        recommendations = await self._rank_stocks(screened, hot_themes, max_stocks)

        # 4. 종목별 대표뉴스 수집
        await self._collect_per_stock_news(recommendations)

        # 5. 추천 종목 저장 (오후 리포트용)
        self._today_recommendations = recommendations
        self._recommendation_date = today

        # 5-1. 업종 동향 데이터 조회
        sector_lines = await self._fetch_sector_summary()

        # 5-2. US 시장 오버나이트 데이터 조회
        us_lines = await self._fetch_us_market_summary()

        # 6. 레포트 생성
        report = self._format_morning_report(recommendations, hot_themes, today, sector_lines, us_lines)

        # 7. 텔레그램 레포트 채널로 발송
        if send_telegram:
            success = await self.telegram.send_report(report)
            if success:
                logger.info(f"[레포트] 아침 레포트 발송 완료 ({len(recommendations)}종목)")
            else:
                logger.error("[레포트] 아침 레포트 발송 실패")

        return report

    async def generate_evening_report(
        self,
        send_telegram: bool = True,
    ) -> str:
        """
        오후 5시 결과 레포트 생성

        아침에 추천한 종목들의 당일 성과 + 실제 거래 결과를 보고합니다.
        """
        logger.info("[레포트] 오후 결과 레포트 생성 시작")

        today = date.today()

        # 오늘 추천 종목이 없으면 스킵
        if not self._today_recommendations or self._recommendation_date != today:
            logger.warning("[레포트] 오늘 추천 종목이 없습니다")
            return ""

        # 현재가 조회 및 결과 계산
        await self._update_results()

        # 실거래 결과 조회 (TradeJournal)
        trade_summary = self._get_trade_summary()

        # 레포트 생성
        report = self._format_evening_report(self._today_recommendations, today)

        # 실거래 섹션 추가
        if trade_summary:
            report += "\n\n" + trade_summary

        # 텔레그램 레포트 채널로 발송
        if send_telegram:
            success = await self.telegram.send_report(report)
            if success:
                logger.info("[레포트] 오후 결과 레포트 발송 완료")
            else:
                logger.error("[레포트] 오후 결과 레포트 발송 실패")

        return report

    def _get_trade_summary(self) -> str:
        """TradeJournal에서 당일 실거래 결과 조회"""
        try:
            from ..core.evolution.trade_journal import get_trade_journal
            journal = get_trade_journal()
            today_trades = journal.get_today_trades()

            if not today_trades:
                return ""

            lines = [
                "─" * 20,
                "<b>당일 실거래 결과</b>",
                "",
            ]

            total_pnl = 0
            closed_count = 0
            open_count = 0

            for trade in today_trades:
                symbol = trade.get("symbol", "")
                name = trade.get("name", symbol)
                entry_price = trade.get("entry_price", 0)
                exit_price = trade.get("exit_price")
                pnl = trade.get("pnl", 0)
                pnl_pct = trade.get("pnl_pct", 0)
                exit_reason = trade.get("exit_reason", "")

                if exit_price:
                    # 청산 완료
                    closed_count += 1
                    total_pnl += pnl
                    emoji = "📈" if pnl >= 0 else "📉"
                    lines.append(
                        f"{emoji} {name}: {pnl_pct:+.1f}% ({pnl:+,.0f}원) - {exit_reason}"
                    )
                else:
                    # 보유 중
                    open_count += 1
                    lines.append(f"🔄 {name}: 보유 중 (진입가 {entry_price:,.0f}원)")

            if closed_count > 0:
                lines.extend([
                    "",
                    f"• 청산: {closed_count}건, 보유: {open_count}건",
                    f"• 실현 손익: {total_pnl:+,.0f}원",
                ])

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"실거래 결과 조회 실패: {e}")
            return ""

    async def _rank_stocks(
        self,
        screened: List[ScreenedStock],
        hot_themes: List,
        max_stocks: int,
    ) -> List[RecommendedStock]:
        """종목 순위 결정 및 추천 종목 생성"""

        # 테마 관련 종목 맵
        theme_map = {}
        for theme in hot_themes:
            for symbol in getattr(theme, 'related_stocks', []):
                theme_map[symbol] = theme.name

        recommendations = []

        for i, stock in enumerate(screened[:max_stocks * 3]):  # 후보군 넉넉하게
            # ETF/ETN 방어적 필터 (스크리너 미경유 시 대비)
            if self.screener._is_etf_etn(stock.name):
                continue

            # 기본 점수
            news_score = min(stock.score, 100)
            tech_score = self._calculate_tech_score(stock)
            theme_score = 80 if stock.symbol in theme_map else 0

            # 종합 점수
            total = (news_score * 0.4) + (tech_score * 0.3) + (theme_score * 0.3)

            # 가격 계산
            entry = stock.price
            target = entry * 1.03  # +3% 익절
            stop = entry * 0.98   # -2% 손절

            # 리스크 평가
            risk_level, risk_factors = self._assess_risk(stock)

            # 상세 투자 포인트 생성
            thesis = self._generate_detailed_thesis(stock, theme_map.get(stock.symbol, ""))
            catalyst = self._generate_catalyst(stock, theme_map.get(stock.symbol, ""))

            rec = RecommendedStock(
                rank=len(recommendations) + 1,
                symbol=stock.symbol,
                name=stock.name,
                investment_thesis=thesis,
                catalyst=catalyst,
                prev_close=stock.price,
                target_entry=entry,
                target_exit=target,
                stop_loss=stop,
                news_score=news_score,
                tech_score=tech_score,
                theme_score=theme_score,
                total_score=total,
                risk_level=risk_level,
                risk_factors=risk_factors,
                related_theme=theme_map.get(stock.symbol, ""),
                key_news="",  # 이후 종목별 뉴스에서 채움
            )
            recommendations.append(rec)

            if len(recommendations) >= max_stocks:
                break

        # 최소 10개가 안 되면 점수 낮은 것도 포함
        if len(recommendations) < 10 and len(screened) > len(recommendations):
            for stock in screened[len(recommendations):]:
                if len(recommendations) >= max_stocks:
                    break
                if stock.symbol in [r.symbol for r in recommendations]:
                    continue
                if self.screener._is_etf_etn(stock.name):
                    continue

                entry = stock.price
                thesis = self._generate_detailed_thesis(stock, theme_map.get(stock.symbol, ""))
                catalyst = self._generate_catalyst(stock, theme_map.get(stock.symbol, ""))
                risk_level, risk_factors = self._assess_risk(stock)

                rec = RecommendedStock(
                    rank=len(recommendations) + 1,
                    symbol=stock.symbol,
                    name=stock.name,
                    investment_thesis=thesis,
                    catalyst=catalyst,
                    prev_close=entry,
                    target_entry=entry,
                    target_exit=entry * 1.03,
                    stop_loss=entry * 0.98,
                    news_score=min(stock.score, 100),
                    tech_score=self._calculate_tech_score(stock),
                    theme_score=80 if stock.symbol in theme_map else 0,
                    total_score=stock.score,
                    risk_level=risk_level,
                    risk_factors=risk_factors,
                    related_theme=theme_map.get(stock.symbol, ""),
                    key_news="",
                )
                recommendations.append(rec)

        return recommendations

    def _calculate_tech_score(self, stock: ScreenedStock) -> float:
        """기술적 점수 계산"""
        score = 50  # 기본점수

        # 등락률 기반
        if stock.change_pct > 5:
            score += 30
        elif stock.change_pct > 2:
            score += 20
        elif stock.change_pct > 0:
            score += 10

        # 거래량 급증 여부
        if "거래량" in " ".join(stock.reasons):
            score += 20

        return min(score, 100)

    def _assess_risk(self, stock: ScreenedStock) -> Tuple[str, List[str]]:
        """리스크 평가"""
        factors = []

        # 과열 체크
        if stock.change_pct > 10:
            factors.append("과열 주의 (10%+ 급등)")

        # 저가주 체크
        if stock.price < 2000:
            factors.append("저가주 변동성")

        # 레버리지 ETF 체크
        if "레버리지" in stock.name or "인버스" in stock.name:
            factors.append("레버리지/인버스 상품")

        # 리스크 레벨
        if len(factors) >= 2:
            level = "높음"
        elif len(factors) >= 1:
            level = "중"
        else:
            level = "낮음"

        return level, factors

    def _generate_detailed_thesis(self, stock: ScreenedStock, theme: str) -> str:
        """상세 투자 포인트 생성"""
        parts = []

        # 테마 관련
        if theme:
            parts.append(f"{theme} 테마 핵심 수혜주")

        # 등락률 기반
        if stock.change_pct > 10:
            parts.append(f"전일 {stock.change_pct:+.1f}% 급등, 강한 상승 모멘텀")
        elif stock.change_pct > 5:
            parts.append(f"전일 {stock.change_pct:+.1f}% 상승, 추세 진행 중")
        elif stock.change_pct > 2:
            parts.append(f"전일 {stock.change_pct:+.1f}% 상승, 매수세 유입")
        elif stock.change_pct > 0:
            parts.append(f"전일 소폭 상승({stock.change_pct:+.1f}%), 저점 매수 기회")

        # 거래량 기반
        reasons_str = " ".join(stock.reasons)
        if "거래량" in reasons_str:
            parts.append("거래량 급증으로 세력/기관 매수 포착")

        # 신고가 기반
        if "신고가" in reasons_str:
            parts.append("52주 신고가 근접, 돌파 시 추가 상승 기대")

        # 스크리너 이유 활용
        for reason in stock.reasons:
            if reason not in parts and "순위" not in reason:
                parts.append(reason)

        if not parts:
            parts.append("기술적 돌파 신호 감지")

        return " / ".join(parts[:3])

    def _generate_catalyst(self, stock: ScreenedStock, theme: str) -> str:
        """상승 촉매 생성"""
        catalysts = []

        if theme:
            catalysts.append(f"{theme} 테마 강세")

        reasons_str = " ".join(stock.reasons)
        if "거래량" in reasons_str:
            catalysts.append("거래량 폭발")
        if "신고가" in reasons_str:
            catalysts.append("신고가 돌파")
        if "상승률" in reasons_str:
            catalysts.append("강한 상승 모멘텀")

        if stock.change_pct > 5:
            catalysts.append(f"전일 {stock.change_pct:+.1f}% 급등")

        if not catalysts:
            catalysts.append("기술적 반등 신호")

        return ", ".join(catalysts[:2])

    async def _collect_per_stock_news(self, recommendations: List[RecommendedStock]):
        """종목별 대표뉴스 수집"""
        for rec in recommendations:
            try:
                # 종목명으로 뉴스 검색
                articles = await self.news_collector.search_news(
                    query=f"{rec.name} 주식",
                    display=3,
                    sort="date"
                )

                if articles:
                    # HTML 태그 제거 후 첫 번째 뉴스 제목 사용
                    title = articles[0].title
                    title = re.sub(r'<[^>]+>', '', title)
                    rec.key_news = title
                else:
                    rec.key_news = ""

            except Exception as e:
                logger.debug(f"종목 뉴스 검색 실패 ({rec.name}): {e}")
                rec.key_news = ""

            # 네이버 API rate limit 방지
            await asyncio.sleep(0.2)

    async def _update_results(self):
        """추천 종목 결과 업데이트 (네이버 금융에서 현재가 조회)"""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            for rec in self._today_recommendations:
                try:
                    # 네이버 금융 시세 조회
                    url = f"https://finance.naver.com/item/main.nhn?code={rec.symbol}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text()

                        # 현재가 파싱 (정규식)
                        # <dd><span class="blind">현재가</span>XX,XXX</dd> 패턴
                        price_match = re.search(r'<span class="blind">현재가</span>([0-9,]+)', html)
                        if price_match:
                            price_str = price_match.group(1).replace(",", "")
                            rec.result_price = float(price_str)

                            # 수익률 계산 (전일 종가 대비)
                            if rec.prev_close > 0:
                                rec.result_pct = (rec.result_price - rec.prev_close) / rec.prev_close * 100
                            else:
                                rec.result_pct = 0.0

                            logger.debug(f"[결과] {rec.symbol}: {rec.result_price:,.0f}원 ({rec.result_pct:+.1f}%)")

                except Exception as e:
                    logger.warning(f"현재가 조회 실패 ({rec.symbol}): {e}")
                    rec.result_price = None
                    rec.result_pct = None

    async def _fetch_sector_summary(self) -> List[str]:
        """업종지수 상승/하락 TOP 5 요약"""
        kmd = self._kis_market_data
        if not kmd:
            try:
                from ..data.providers.kis_market_data import get_kis_market_data
                kmd = get_kis_market_data()
            except Exception:
                return []

        try:
            sectors = await kmd.fetch_sector_indices()
            if not sectors:
                return []

            # 등락률 파싱
            parsed = []
            for s in sectors:
                name = s.get("name", "")
                change_pct = s.get("change_pct", 0.0)
                if name:
                    parsed.append((name, change_pct))

            if not parsed:
                return []

            parsed.sort(key=lambda x: x[1], reverse=True)

            lines = ["📈 <b>업종 동향 (전일 기준)</b>"]

            # 상승 TOP 5
            top = [f"{n}({p:+.1f}%)" for n, p in parsed[:5] if p > 0]
            if top:
                lines.append(f"  ▲ 상승: {' / '.join(top)}")

            # 하락 TOP 5
            bottom = [f"{n}({p:+.1f}%)" for n, p in parsed[-5:] if p < 0]
            if bottom:
                bottom.reverse()
                lines.append(f"  ▼ 하락: {' / '.join(bottom)}")

            lines.append("")
            return lines

        except Exception as e:
            logger.warning(f"[레포트] 업종 동향 조회 실패: {e}")
            return []

    async def _fetch_us_market_summary(self) -> List[str]:
        """US 시장 오버나이트 요약 (텔레그램 HTML)"""
        umd = self._us_market_data
        if not umd:
            try:
                from ..data.providers.us_market_data import get_us_market_data
                umd = get_us_market_data()
            except Exception:
                return []

        try:
            signal = await umd.get_overnight_signal()
            if not signal or not signal.get("indices"):
                return []

            sentiment = signal.get("sentiment", "neutral")
            indices = signal.get("indices", {})
            sector_signals = signal.get("sector_signals", {})

            # 심리 이모지
            sentiment_emoji = {
                "bullish": "📈", "bearish": "📉", "neutral": "➡️"
            }.get(sentiment, "➡️")
            sentiment_kr = {
                "bullish": "강세", "bearish": "약세", "neutral": "보합"
            }.get(sentiment, "보합")

            lines = [f"{sentiment_emoji} <b>US 시장 마감 ({sentiment_kr})</b>"]

            # 지수 등락률
            idx_parts = []
            for name, info in indices.items():
                pct = info["change_pct"]
                arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "─")
                idx_parts.append(f"{name} {arrow}{abs(pct):.1f}%")
            if idx_parts:
                lines.append(f"  {' / '.join(idx_parts)}")

            # 한국 테마 연동 (부스트가 있는 테마만)
            if sector_signals:
                boost_parts = []
                for theme, sig in sector_signals.items():
                    boost = sig["boost"]
                    if boost > 0:
                        boost_parts.append(f"{theme}(+{boost})")
                    elif boost < 0:
                        boost_parts.append(f"{theme}({boost})")
                if boost_parts:
                    lines.append(f"  → 한국 테마 영향: {', '.join(boost_parts)}")

            lines.append("")
            return lines

        except Exception as e:
            logger.warning(f"[레포트] US 시장 요약 조회 실패: {e}")
            return []

    def _format_morning_report(
        self,
        recommendations: List[RecommendedStock],
        hot_themes: List,
        report_date: date,
        sector_lines: Optional[List[str]] = None,
        us_lines: Optional[List[str]] = None,
    ) -> str:
        """아침 레포트 포맷팅"""

        date_str = report_date.strftime("%Y년 %m월 %d일")

        lines = [
            f"📊 <b>오늘의 추천 종목 ({len(recommendations)}개)</b>",
            f"<i>{date_str} 08:00 기준</i>",
            "",
        ]

        # 핫 테마
        if hot_themes:
            theme_strs = [f"{t.name}({t.score:.0f})" for t in hot_themes[:5]]
            lines.append(f"🔥 <b>핫 테마:</b> {' / '.join(theme_strs)}")
            lines.append("")

        # US 시장 오버나이트
        if us_lines:
            lines.extend(us_lines)

        # 업종 동향
        if sector_lines:
            lines.extend(sector_lines)

        # 추천 종목
        for rec in recommendations:
            risk_emoji = {"낮음": "🟢", "중": "🟡", "높음": "🔴"}.get(rec.risk_level, "⚪")

            lines.append(f"<b>{rec.rank}. {rec.name}</b> <code>{rec.symbol}</code> {risk_emoji}{rec.total_score:.0f}점")
            lines.append(f"   📌 <b>추천이유:</b> {rec.investment_thesis}")
            lines.append(f"   ⚡ <b>촉매:</b> {rec.catalyst}")

            if rec.key_news:
                news_title = rec.key_news
                if len(news_title) > 50:
                    news_title = news_title[:50] + "..."
                lines.append(f"   📰 <b>뉴스:</b> {news_title}")

            lines.append(
                f"   💰 진입: {rec.target_entry:,.0f}원 → "
                f"목표: {rec.target_exit:,.0f}원(+3%) / "
                f"손절: {rec.stop_loss:,.0f}원(-2%)"
            )

            if rec.risk_factors:
                lines.append(f"   ⚠️ {', '.join(rec.risk_factors)}")

            lines.append("")

        # 투자 주의사항
        lines.extend([
            "─" * 20,
            "<i>본 정보는 투자 참고용이며, 투자 판단과 책임은 본인에게 있습니다.</i>",
        ])

        return "\n".join(lines)

    def _format_evening_report(
        self,
        recommendations: List[RecommendedStock],
        report_date: date,
    ) -> str:
        """오후 결과 레포트 포맷팅"""

        date_str = report_date.strftime("%Y년 %m월 %d일")

        lines = [
            f"<b>오늘의 추천 종목 결과</b>",
            f"<i>{date_str} 17:00 기준</i>",
            "",
        ]

        wins = 0
        total_pct = 0.0

        for rec in recommendations:
            if rec.result_pct is not None:
                # 결과 이모지
                if rec.result_pct >= 3:
                    emoji = "🎯"  # 목표 달성
                    wins += 1
                elif rec.result_pct >= 0:
                    emoji = "✅"  # 수익
                    wins += 1
                elif rec.result_pct >= -2:
                    emoji = "➖"  # 손절 이내
                else:
                    emoji = "❌"  # 손실

                total_pct += rec.result_pct

                lines.append(
                    f"{emoji} <b>{rec.name}</b> <code>{rec.symbol}</code>: "
                    f"{rec.result_pct:+.1f}%"
                )
            else:
                lines.append(
                    f"⏳ <b>{rec.name}</b> <code>{rec.symbol}</code>: 데이터 없음"
                )

        # 요약
        lines.extend([
            "",
            "─" * 20,
            f"<b>성과 요약</b>",
            f"• 적중률: {wins}/{len(recommendations)} ({wins/len(recommendations)*100:.0f}%)",
            f"• 평균 수익률: {total_pct/len(recommendations):+.1f}%",
        ])

        return "\n".join(lines)


# 싱글톤 인스턴스
_report_generator: Optional[DailyReportGenerator] = None


def get_report_generator() -> DailyReportGenerator:
    """레포트 생성기 인스턴스 반환"""
    global _report_generator
    if _report_generator is None:
        _report_generator = DailyReportGenerator()
    return _report_generator
