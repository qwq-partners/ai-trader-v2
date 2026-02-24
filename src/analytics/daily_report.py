"""
AI Trading Bot v2 - 일일 투자 레포트 시스템

매일 아침 8시: 오늘의 추천 종목 레포트
매일 오후 5시: 추천 종목 결과 레포트
"""

import asyncio
import dataclasses
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from loguru import logger

# 추천 종목 캐시 경로
_REC_CACHE_DIR = Path.home() / ".cache" / "ai_trader"

# 프로젝트 내 모듈
from ..utils.telegram import get_telegram_notifier, TelegramNotifier
from ..signals.screener import get_screener, ScreenedStock
from ..signals.sentiment.theme_detector import get_theme_detector, NewsCollector
from ..data.storage.news_storage import get_news_storage


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

        # NewsStorage 추가 (종목별 뉴스 조회용)
        self._news_storage = None

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

        # 5. 추천 종목 저장 (오후 리포트용) + 파일 영속화
        self._today_recommendations = recommendations
        self._recommendation_date = today
        self._save_recommendations(today)

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

        # 메모리에 없으면 파일에서 복원 시도 (봇 재시작 대응)
        if not self._today_recommendations or self._recommendation_date != today:
            loaded = self._load_recommendations(today)
            if loaded:
                self._today_recommendations = loaded
                self._recommendation_date = today
                logger.info(f"[레포트] 추천 종목 파일 복원: {len(loaded)}종목")
            else:
                logger.warning("[레포트] 오늘 추천 종목이 없습니다 (메모리 + 파일 모두 없음)")
                return ""

        # 현재가 조회 및 결과 계산
        await self._update_results()

        # 레포트 생성 (추천 종목 결과만 — 봇 실거래 결과는 별도 채널)
        report = self._format_evening_report(self._today_recommendations, today)

        # 텔레그램 레포트 채널로 발송
        if send_telegram:
            success = await self.telegram.send_report(report)
            if success:
                logger.info("[레포트] 오후 결과 레포트 발송 완료")
            else:
                logger.error("[레포트] 오후 결과 레포트 발송 실패")

        return report

    async def _get_trade_summary(self) -> str:
        """DB에서 당일 실거래 결과 조회 (봇 재시작 대응)"""
        try:
            from ..data.storage.trade_storage import get_trade_storage
            storage = get_trade_storage()
            if storage is None:
                return ""

            # DB에서 오늘 청산된 거래 조회
            stats = await storage.get_statistics_from_db(days=1)
            # 오늘 오픈 포지션은 엔진에서 직접 가져옴
            open_positions = []
            try:
                from ..core.evolution.trade_journal import get_trade_journal
                journal = get_trade_journal()
                for t in journal.get_today_trades():
                    if not t.get("exit_price"):
                        open_positions.append(t)
            except Exception:
                pass

            if not stats and not open_positions:
                return ""

            total_trades = stats.get("total_trades", 0)
            wins = stats.get("wins", 0)
            losses = stats.get("losses", 0)
            total_pnl = stats.get("total_pnl", 0.0)
            win_rate = stats.get("win_rate", 0.0)
            avg_pnl_pct = stats.get("avg_pnl_pct", 0.0)
            best = stats.get("best_trade")
            worst = stats.get("worst_trade")

            lines = [
                "─" * 20,
                "<b>📊 봇 실거래 결과</b>",
                "",
            ]

            if total_trades > 0:
                lines.extend([
                    f"• 총 거래: {total_trades}건 (승 {wins} / 패 {losses})",
                    f"• 승률: {win_rate:.1f}% / 평균 손익률: {avg_pnl_pct:+.2f}%",
                    f"• 실현 손익: <b>{total_pnl:+,.0f}원</b>",
                ])
                if best and worst and total_trades >= 2:
                    lines.append(
                        f"• 최고: {best['name']} {best['pnl_pct']:+.1f}% / "
                        f"최저: {worst['name']} {worst['pnl_pct']:+.1f}%"
                    )

            if open_positions:
                lines.append(f"• 보유 중: {len(open_positions)}종목")

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
        """기술적 점수 계산 (수급 중심, 모멘텀 편향 제거)"""
        score = 40  # 기본점수
        reasons_str = " ".join(stock.reasons)

        # 수급 신호 (최우선, 신뢰도 높음)
        if stock.has_foreign_buying:
            score += 25
        if stock.has_inst_buying:
            score += 20

        # 기술적 지표
        if "SPDI↑" in reasons_str:      score += 10  # 수급선행 지표
        if "지속상승" in reasons_str:    score += 8
        if "MA20+" in reasons_str:       score += 5
        if "RSI" in reasons_str:         score += 5
        if "저PER" in reasons_str:       score += 5
        if "저PBR" in reasons_str:       score += 3
        if "고ROE" in reasons_str:       score += 4

        # 거래량 (있으면 가산, 단독으론 약한 신호)
        if stock.volume_ratio >= 3.0:    score += 10
        elif stock.volume_ratio >= 2.0:  score += 6
        elif "거래량" in reasons_str:    score += 3

        # 등락률 — 보조 신호만 (최대 8점)
        if stock.change_pct > 5:         score += 8
        elif stock.change_pct > 2:       score += 5
        elif stock.change_pct > 0:       score += 2

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
        """스크리너 reasons 기반 상세 투자 포인트 생성 (수급 → 테마 → 기술 순)"""
        parts = []
        reasons_str = " ".join(stock.reasons)

        # 1. 수급 신호 (가장 신뢰도 높음 — 최우선)
        for r in stock.reasons:
            if "외국인 순매수" in r and r not in parts:
                parts.append(r)
                break
        for r in stock.reasons:
            if "기관 순매수" in r and r not in parts:
                parts.append(r)
                break

        # 2. 테마 멤버십
        if theme:
            parts.append(f"{theme} 테마")

        # 3. 기술적 신호 — 스크리너 reasons 직접 사용
        TECH_KEYWORDS = ["SPDI↑", "지속상승", "MA20+", "RSI", "저PER", "저PBR", "고ROE", "흑자"]
        for kw in TECH_KEYWORDS:
            for r in stock.reasons:
                if kw in r and r not in parts:
                    parts.append(r)
                    break
            if len(parts) >= 4:
                break

        # 4. 거래량 (있으면 추가)
        for r in stock.reasons:
            if "거래량" in r and r not in parts:
                parts.append(r)
                break

        # 5. 아무것도 없으면 등락률로 보완
        if not parts:
            if abs(stock.change_pct) > 0.5:
                parts.append(f"전일 {stock.change_pct:+.1f}%")
            else:
                parts.append("기술적 돌파 신호")

        return " / ".join(parts[:4])

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
        """
        종목별 대표뉴스 수집

        DB에 저장된 뉴스 중 해당 종목이 언급된 뉴스를 조회합니다.
        (네이버 API가 아닌 자체 DB 사용)
        """
        # NewsStorage 초기화
        if self._news_storage is None:
            self._news_storage = await get_news_storage()

        for rec in recommendations:
            try:
                # DB에서 종목명으로 뉴스 검색 (최근 3일, 최대 5건)
                articles = await self._news_storage.search_news(
                    keyword=rec.name,
                    days=3,
                    limit=5
                )

                if articles:
                    # 가장 최근 뉴스의 제목 사용
                    rec.key_news = articles[0].title
                    logger.debug(f"[레포트] {rec.name} 대표뉴스: {rec.key_news[:30]}...")
                else:
                    rec.key_news = ""
                    logger.debug(f"[레포트] {rec.name} 관련 뉴스 없음")

            except Exception as e:
                logger.warning(f"종목 뉴스 검색 실패 ({rec.name}): {e}")
                rec.key_news = ""

    def _save_recommendations(self, report_date: date) -> None:
        """추천 종목을 파일에 영속화 (봇 재시작 대응)"""
        try:
            _REC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = _REC_CACHE_DIR / f"morning_recs_{report_date.isoformat()}.json"
            data = {
                "date": report_date.isoformat(),
                "stocks": [dataclasses.asdict(r) for r in self._today_recommendations],
            }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.debug(f"[레포트] 추천 종목 저장: {path} ({len(self._today_recommendations)}종목)")
        except Exception as e:
            logger.warning(f"[레포트] 추천 종목 저장 실패: {e}")

    def _load_recommendations(self, report_date: date) -> List["RecommendedStock"]:
        """파일에서 추천 종목 복원"""
        try:
            path = _REC_CACHE_DIR / f"morning_recs_{report_date.isoformat()}.json"
            if not path.exists():
                return []
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("date") != report_date.isoformat():
                return []
            stocks = []
            for d in raw.get("stocks", []):
                # risk_factors 필드 타입 보정
                d["risk_factors"] = d.get("risk_factors") or []
                stocks.append(RecommendedStock(**d))
            return stocks
        except Exception as e:
            logger.warning(f"[레포트] 추천 종목 로드 실패: {e}")
            return []

    async def _update_results(self):
        """추천 종목 결과 업데이트 (KIS API → pykrx 종가 → 네이버 금융 순)"""
        import aiohttp

        today_str = date.today().strftime("%Y%m%d")

        for rec in self._today_recommendations:
            try:
                price = None

                # 1차: KIS API (실시간, 정확)
                if hasattr(self, '_bot') and hasattr(self._bot, 'broker') and self._bot.broker:
                    try:
                        quote = await self._bot.broker.get_quote(rec.symbol)
                        if quote and quote.get("price", 0) > 0:
                            price = quote["price"]
                    except Exception:
                        pass

                # 2차: pykrx 종가 (장 마감 후 안정적)
                if price is None:
                    try:
                        def _fetch_pykrx(sym: str, date_str: str) -> Optional[float]:
                            from pykrx import stock as pykrx_stock
                            df = pykrx_stock.get_market_ohlcv(date_str, date_str, sym)
                            if df is not None and not df.empty:
                                return float(df["종가"].iloc[-1])
                            return None

                        price = await asyncio.to_thread(_fetch_pykrx, rec.symbol, today_str)
                        if price:
                            logger.debug(f"[결과] {rec.symbol} pykrx 종가: {price:,.0f}원")
                    except Exception as e:
                        logger.debug(f"[결과] {rec.symbol} pykrx 조회 실패: {e}")

                # 3차: 네이버 금융 HTML 파싱 (최후 폴백)
                if price is None:
                    try:
                        async with aiohttp.ClientSession() as session:
                            url = f"https://finance.naver.com/item/main.nhn?code={rec.symbol}"
                            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                if resp.status == 200:
                                    html = await resp.text()
                                    price_match = re.search(r'<span class="blind">현재가</span>([0-9,]+)', html)
                                    if not price_match:
                                        price_match = re.search(r'class="no_today"[^>]*>.*?<span[^>]*>([0-9,]+)', html, re.DOTALL)
                                    if price_match:
                                        price = float(price_match.group(1).replace(",", ""))
                    except Exception as e:
                        logger.debug(f"[결과] {rec.symbol} 네이버 조회 실패: {e}")

                if price and price > 0:
                    rec.result_price = price
                    if rec.prev_close > 0:
                        rec.result_pct = (rec.result_price - rec.prev_close) / rec.prev_close * 100
                    else:
                        rec.result_pct = 0.0
                    logger.debug(f"[결과] {rec.symbol}: {rec.result_price:,.0f}원 ({rec.result_pct:+.1f}%)")
                else:
                    logger.warning(f"[결과] {rec.symbol}: 종가 조회 실패 (3개 소스 모두 실패)")

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

    async def generate_us_market_report(self, send_telegram: bool = True) -> str:
        """
        미국증시 마감 레포트 생성 (매일 07:00)

        Yahoo Finance 데이터 기반으로 지수, 섹터 ETF, 개별 종목 등락을
        한눈에 보기 좋게 정리하여 텔레그램 발송.
        """
        from ..data.providers.us_market_data import (
            get_us_market_data, US_KOREA_SECTOR_MAP, INDEX_SYMBOLS, INDEX_NAMES,
        )

        umd = self._us_market_data or get_us_market_data()
        quotes = await umd.fetch_us_market_summary()

        if not quotes:
            msg = "⚠️ 미국증시 데이터 조회 실패"
            if send_telegram:
                await self.telegram.send_report(msg)
            return msg

        now = datetime.now()
        date_str = now.strftime("%Y.%m.%d")

        # ── 지수 ──
        idx_lines = []
        idx_pcts = []
        for sym in INDEX_SYMBOLS:
            q = quotes.get(sym)
            if not q:
                continue
            name = INDEX_NAMES.get(sym, sym)
            pct = q["change_pct"]
            price = q["price"]
            idx_pcts.append(pct)
            if pct > 0:
                idx_lines.append(f"  🔼 {name}  <b>+{pct:.2f}%</b>  ({price:,.1f})")
            elif pct < 0:
                idx_lines.append(f"  🔽 {name}  <b>{pct:.2f}%</b>  ({price:,.1f})")
            else:
                idx_lines.append(f"  ▪️ {name}  0.00%  ({price:,.1f})")

        avg_pct = sum(idx_pcts) / len(idx_pcts) if idx_pcts else 0
        if avg_pct >= 1.0:
            mood = "📈 강세 마감"
        elif avg_pct <= -1.0:
            mood = "📉 약세 마감"
        else:
            mood = "➡️ 보합 마감"

        lines = [
            f"🇺🇸 <b>미국증시 마감 리포트</b>",
            f"<i>{date_str} 07:00 기준 (전일 NY 마감)</i>",
            "",
            f"<b>■ 주요 지수  {mood}</b>",
        ]
        lines.extend(idx_lines)
        lines.append("")

        # ── 빅테크 ──
        bigtech = ["NVDA", "AAPL", "MSFT", "GOOG", "META", "AMZN", "TSLA"]
        bt_lines = []
        for sym in bigtech:
            q = quotes.get(sym)
            if not q:
                continue
            pct = q["change_pct"]
            name = q.get("name", sym)
            # 이름이 너무 길면 축약
            if len(name) > 12:
                name = name[:12]
            if pct > 0:
                bt_lines.append(f"{sym} <b>+{pct:.1f}%</b>")
            elif pct < 0:
                bt_lines.append(f"{sym} {pct:.1f}%")
            else:
                bt_lines.append(f"{sym} 0.0%")

        if bt_lines:
            lines.append(f"<b>■ 빅테크</b>")
            # 4개씩 줄바꿈
            for i in range(0, len(bt_lines), 4):
                lines.append("  " + "  ".join(bt_lines[i:i + 4]))
            lines.append("")

        # ── 섹터 ETF + 개별종목 (테마 매핑) ──
        sector_signals = await umd.get_sector_signals()
        if sector_signals:
            lines.append(f"<b>■ 한국 시장 영향</b>")
            for theme, sig in sorted(
                sector_signals.items(),
                key=lambda x: abs(x[1]["boost"]),
                reverse=True,
            ):
                boost = sig["boost"]
                avg = sig["us_avg_pct"]
                movers = sig.get("top_movers", [])
                icon = "🔺" if boost > 0 else "🔻"
                movers_str = ", ".join(movers[:3])
                lines.append(
                    f"  {icon} <b>{theme}</b> (부스트 {boost:+d}점)"
                )
                lines.append(f"      평균 {avg:+.1f}%  {movers_str}")
            lines.append("")

        # ── 공포/탐욕 지표 대용: VIX ──
        # VIX는 US_SYMBOLS에 없으므로 별도 조회 불필요, 지수 평균으로 대체
        if avg_pct >= 1.5:
            market_msg = "💡 강한 상승 — 한국 관련 테마주 갭업 가능성"
        elif avg_pct >= 0.5:
            market_msg = "💡 소폭 상승 — 반도체·IT 섹터 긍정적"
        elif avg_pct <= -1.5:
            market_msg = "⚠️ 강한 하락 — 한국 시장 하방 압력 주의"
        elif avg_pct <= -0.5:
            market_msg = "⚠️ 소폭 하락 — 보수적 접근 권장"
        else:
            market_msg = "💡 변동 미미 — 국내 자체 재료에 주목"

        lines.append(f"<b>■ 오늘의 포인트</b>")
        lines.append(f"  {market_msg}")

        report = "\n".join(lines)

        if send_telegram:
            # ── 1) 차트 이미지 생성 후 전송 (caption = 지수 요약) ──────────
            chart_sent = False
            try:
                from .us_market_chart import generate_us_market_chart, generate_sp500_map
                from ..utils.telegram import send_photo as tg_send_photo

                # 1a) 지수 카드 + 섹터 ETF 히트맵
                chart_buf = generate_us_market_chart(
                    quotes=quotes,
                    date_str=date_str,
                    avg_pct=avg_pct,
                )
                if chart_buf:
                    caption_lines = [f"🇺🇸 <b>미국증시 마감</b>  {date_str}  {mood}", ""]
                    caption_lines.extend(idx_lines)
                    caption = "\n".join(caption_lines)[:1024]

                    chart_sent = await tg_send_photo(
                        chart_buf, caption=caption, parse_mode="HTML"
                    )
                    if chart_sent:
                        logger.info("[레포트] 미국증시 차트 이미지 발송 완료")
                    else:
                        logger.warning("[레포트] 차트 이미지 발송 실패")

                # 1b) S&P500 개별 종목 맵 (별도 전송)
                try:
                    stock_quotes = await umd.fetch_sp500_stocks()
                    sp500_buf = generate_sp500_map(stock_quotes, date_str=date_str)
                    if sp500_buf:
                        await tg_send_photo(sp500_buf, caption="📊 <b>S&P 500 Map</b>",
                                            parse_mode="HTML")
                        logger.info("[레포트] S&P500 맵 발송 완료")
                except Exception as sp_err:
                    logger.warning(f"[레포트] S&P500 맵 생성/전송 실패: {sp_err}")

            except Exception as chart_err:
                logger.error(f"[레포트] 차트 생성/전송 오류: {chart_err}")

            # ── 2) 텍스트 리포트 전송 ─────────────────────────────────────
            # 차트 전송 성공 시 → 섹터 영향 + 포인트만 (중복 지수 생략)
            # 차트 전송 실패 시 → 전체 report 텍스트 전송
            if chart_sent:
                detail_lines = []
                if bt_lines:
                    detail_lines.append("<b>■ 빅테크</b>")
                    for i in range(0, len(bt_lines), 4):
                        detail_lines.append("  " + "  ".join(bt_lines[i:i + 4]))
                    detail_lines.append("")
                if sector_signals:
                    detail_lines.append("<b>■ 한국 시장 영향</b>")
                    for theme, sig in sorted(
                        sector_signals.items(),
                        key=lambda x: abs(x[1]["boost"]),
                        reverse=True,
                    ):
                        boost = sig["boost"]
                        avg_s = sig["us_avg_pct"]
                        movers = sig.get("top_movers", [])
                        icon = "🔺" if boost > 0 else "🔻"
                        movers_str = ", ".join(movers[:3])
                        detail_lines.append(
                            f"  {icon} <b>{theme}</b> (부스트 {boost:+d}점)"
                        )
                        detail_lines.append(
                            f"      평균 {avg_s:+.1f}%  {movers_str}"
                        )
                    detail_lines.append("")
                detail_lines.append("<b>■ 오늘의 포인트</b>")
                detail_lines.append(f"  {market_msg}")
                if detail_lines:
                    await self.telegram.send_report("\n".join(detail_lines))
            else:
                # 차트 없이 텍스트 전체 전송 (fallback)
                success = await self.telegram.send_report(report)
                if success:
                    logger.info("[레포트] 미국증시 텍스트 레포트 발송 완료")
                else:
                    logger.error("[레포트] 미국증시 레포트 발송 실패")

        return report

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
            lines.append(f"   📌 {rec.investment_thesis}")

            if rec.key_news:
                news_title = rec.key_news[:55] + "..." if len(rec.key_news) > 55 else rec.key_news
                lines.append(f"   📰 {news_title}")

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
        """오후 결과 레포트 포맷팅 (HTML, Telegram)"""

        date_str = report_date.strftime("%Y.%m.%d")
        SEP = "─" * 18

        lines = [
            f"📋 <b>추천종목 결과</b>  <i>{date_str} 장마감</i>",
            SEP,
            "",
        ]

        wins = 0
        total_pct = 0.0
        evaluated = []

        for rec in recommendations:
            if rec.result_pct is not None:
                # 등급 판정
                if rec.result_pct >= 3:
                    grade = "🎯"
                    wins += 1
                elif rec.result_pct >= 0:
                    grade = "✅"
                    wins += 1
                elif rec.result_pct >= -2:
                    grade = "➖"
                else:
                    grade = "❌"

                # 목표/손절 도달 태그
                tag = ""
                if rec.target_exit > 0 and rec.result_price and rec.result_price >= rec.target_exit:
                    tag = "  <b>🏆목표</b>"
                elif rec.stop_loss > 0 and rec.result_price and rec.result_price <= rec.stop_loss:
                    tag = "  <b>🛑손절</b>"

                total_pct += rec.result_pct
                evaluated.append(rec)

                # 종목 헤더: 순위 + 이름 + 심볼
                lines.append(
                    f"{grade} <b>{rec.rank}. {rec.name}</b> "
                    f"<code>{rec.symbol}</code>"
                )
                # 결과 수치: 종가 + 등락률 + 태그
                lines.append(
                    f"   <code>{rec.result_price:>10,.0f}원</code>  "
                    f"<b>{rec.result_pct:+.1f}%</b>{tag}"
                )
                lines.append("")
            else:
                lines.append(
                    f"⏳ <b>{rec.rank}. {rec.name}</b> "
                    f"<code>{rec.symbol}</code>"
                )
                lines.append("   <i>종가 데이터 없음</i>")
                lines.append("")

        # ── 성과 요약 ──
        n = len(evaluated)
        lines.append(SEP)
        lines.append("<b>📊 성과 요약</b>")
        lines.append("")

        if n > 0:
            avg_pct = total_pct / n
            hit_rate = wins / n * 100

            sorted_recs = sorted(evaluated, key=lambda r: r.result_pct or 0, reverse=True)
            best  = sorted_recs[0]
            worst = sorted_recs[-1]

            lines.extend([
                f"적중률  <b>{wins} / {n}</b>  ({hit_rate:.0f}%)",
                f"평균    <b>{avg_pct:+.2f}%</b>",
                "",
                f"🥇  {best.name}  <b>{best.result_pct:+.1f}%</b>",
                f"🔻  {worst.name}  <b>{worst.result_pct:+.1f}%</b>",
            ])
        else:
            lines.append("<i>결과 데이터 없음</i>")

        return "\n".join(lines)


# 싱글톤 인스턴스
_report_generator: Optional[DailyReportGenerator] = None


def get_report_generator() -> DailyReportGenerator:
    """레포트 생성기 인스턴스 반환"""
    global _report_generator
    if _report_generator is None:
        _report_generator = DailyReportGenerator()
    return _report_generator
