"""
AI Trading Bot v2 - 전략적 데이터 수집

전문가 패널에 제공할 실제 시장 데이터를 수집합니다.
pykrx(MCP), FDR, KIS API, 네이버 뉴스를 활용.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger


class StrategicDataCollector:
    """전문가 패널에 제공할 실제 데이터 수집"""

    def __init__(self, kis_market_data=None, theme_detector=None):
        self._kis_market_data = kis_market_data
        self._theme_detector = theme_detector

    @staticmethod
    def _last_business_day() -> str:
        """가장 최근 영업일 (주말 + 공휴일 제외)"""
        try:
            from ...core.engine import is_kr_market_holiday
            d = (datetime.now() - timedelta(days=1)).date()
            for _ in range(10):
                if d.weekday() < 5 and not is_kr_market_holiday(d):
                    return d.strftime("%Y%m%d")
                d -= timedelta(days=1)
            return d.strftime("%Y%m%d")
        except ImportError:
            # fallback: 주말만 제외
            d = datetime.now() - timedelta(days=1)
            for _ in range(7):
                if d.weekday() < 5:
                    return d.strftime("%Y%m%d")
                d -= timedelta(days=1)
            return d.strftime("%Y%m%d")

    async def collect_all(self) -> Dict[str, Any]:
        """전문가 패널용 전체 데이터 수집"""
        logger.info("[전략적분석] 데이터 수집 시작...")

        tasks = [
            ("market_indices", self._collect_market_indices()),
            ("sector_flows", self._collect_sector_flows()),
            ("exchange_rate", self._collect_exchange_rate()),
            ("interest_rates", self._collect_interest_rates()),
            ("top_foreign_buys", self._collect_top_foreign_buys()),
            ("top_inst_buys", self._collect_top_inst_buys()),
            ("sector_valuations", self._collect_sector_valuations()),
            ("recent_themes", self._collect_recent_themes()),
            ("news_summary", self._collect_news_summary()),
        ]

        results = {}
        gather_results = await asyncio.gather(
            *(t[1] for t in tasks), return_exceptions=True
        )

        for (key, _), result in zip(tasks, gather_results):
            if isinstance(result, Exception):
                logger.warning(f"[전략적분석] {key} 수집 실패: {result}")
                results[key] = None
            else:
                results[key] = result

        collected = sum(1 for v in results.values() if v is not None)
        logger.info(f"[전략적분석] 데이터 수집 완료: {collected}/{len(tasks)}항목")
        return results

    async def collect_macro_context(self) -> Optional[Dict[str, Any]]:
        """환율/금리 매크로 컨텍스트만 수집 (LLM 전략가용)"""
        exchange = await self._collect_exchange_rate()
        rates = await self._collect_interest_rates()
        if exchange or rates:
            return {"exchange_rate": exchange, "interest_rates": rates}
        return None

    async def _collect_market_indices(self) -> Optional[Dict[str, Any]]:
        """KOSPI/KOSDAQ/S&P500/나스닥 30일 추이"""
        try:
            loop = asyncio.get_running_loop()
            import FinanceDataReader as fdr

            start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
            indices = {}

            for name, code in [
                ("KOSPI", "KS11"), ("KOSDAQ", "KQ11"),
                ("S&P500", "US500"), ("NASDAQ", "IXIC"),
            ]:
                try:
                    df = await loop.run_in_executor(
                        None, lambda c=code: fdr.DataReader(c, start)
                    )
                    if df is not None and len(df) >= 5:
                        recent = df.tail(30)
                        closes = [float(row["Close"]) for _, row in recent.iterrows()]
                        dates = [idx.strftime("%Y-%m-%d") for idx in recent.index]
                        change_1m = (closes[-1] / closes[0] - 1) * 100 if closes[0] else 0
                        indices[name] = {
                            "dates": dates[-10:],  # 최근 10일만 프롬프트에 전달
                            "closes": [round(c, 1) for c in closes[-10:]],
                            "current": round(closes[-1], 1),
                            "change_1m_pct": round(change_1m, 2),
                        }
                except Exception as e:
                    logger.debug(f"[전략적분석] {name} 지수 조회 실패: {e}")

            return indices if indices else None
        except Exception as e:
            logger.warning(f"[전략적분석] 지수 수집 오류: {e}")
            return None

    async def _collect_sector_flows(self) -> Optional[List[Dict[str, Any]]]:
        """업종별 외국인/기관 순매수 (pykrx MCP)"""
        try:
            from ...utils.mcp_client import get_mcp_manager
            manager = get_mcp_manager()

            if not manager.is_server_available("pykrx"):
                logger.debug("[전략적분석] pykrx MCP 미사용 가능")
                return None

            # pykrx는 당일 데이터 미제공 → 최근 영업일 기준
            target_date = self._last_business_day()

            result = await manager.call_tool(
                "pykrx",
                "get_market_trading_value_by_date",
                {"date": target_date, "market": "KOSPI"},
            )
            if result:
                return result if isinstance(result, list) else [result]
            return None
        except Exception as e:
            logger.debug(f"[전략적분석] 섹터 수급 수집 실패: {e}")
            return None

    async def _collect_exchange_rate(self) -> Optional[Dict[str, Any]]:
        """원/달러 환율 30일 추이"""
        try:
            loop = asyncio.get_running_loop()
            import FinanceDataReader as fdr

            start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
            df = await loop.run_in_executor(
                None, lambda: fdr.DataReader("USD/KRW", start)
            )
            if df is not None and len(df) >= 5:
                recent = df.tail(20)
                closes = [float(row["Close"]) for _, row in recent.iterrows()]
                return {
                    "current": round(closes[-1], 1),
                    "change_1m_pct": round((closes[-1] / closes[0] - 1) * 100, 2) if closes[0] != 0 else 0,
                    "recent_5d": [round(c, 1) for c in closes[-5:]],
                }
            return None
        except Exception as e:
            logger.debug(f"[전략적분석] 환율 수집 실패: {e}")
            return None

    async def _collect_interest_rates(self) -> Optional[Dict[str, Any]]:
        """한국/미국 금리 데이터 수집 (FinanceDataReader)"""
        try:
            loop = asyncio.get_running_loop()
            import FinanceDataReader as fdr

            start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
            rates = {}

            # 한국 국채 3년물 (기준금리 프록시) / 미국 국채 10년물 (연준 금리 프록시)
            for name, code in [("KR_3Y", "KR3YT=RR"), ("US_10Y", "US10YT=RR")]:
                try:
                    df = await loop.run_in_executor(
                        None, lambda c=code: fdr.DataReader(c, start)
                    )
                    if df is not None and len(df) >= 3:
                        closes = [float(row["Close"]) for _, row in df.tail(20).iterrows()]
                        rates[name] = {
                            "current": round(closes[-1], 3),
                            "change_1m_pct": round(closes[-1] - closes[0], 3),  # 금리는 %p 변화
                        }
                except Exception as e:
                    logger.debug(f"[전략적분석] {name} 금리 조회 실패: {e}")

            if rates:
                # 한미 스프레드 계산
                kr = rates.get("KR_3Y", {}).get("current")
                us = rates.get("US_10Y", {}).get("current")
                if kr is not None and us is not None:
                    rates["spread_kr_us"] = round(kr - us, 3)

            return rates if rates else None
        except Exception as e:
            logger.debug(f"[전략적분석] 금리 수집 실패: {e}")
            return None

    async def _collect_top_foreign_buys(self) -> Optional[List[Dict[str, Any]]]:
        """외국인 순매수 상위 20"""
        if not self._kis_market_data:
            return None
        try:
            results = await asyncio.gather(
                self._kis_market_data.fetch_foreign_institution(market="0001", investor="1"),
                self._kis_market_data.fetch_foreign_institution(market="0002", investor="1"),
                return_exceptions=True,
            )
            combined = []
            for res in results:
                if isinstance(res, list):
                    combined.extend(res)

            # symbol 기준 중복 제거 (순매수 많은 쪽 유지)
            seen = {}
            for item in combined:
                sym = item.get("symbol", "")
                if sym not in seen or item.get("net_buy_qty", 0) > seen[sym].get("net_buy_qty", 0):
                    seen[sym] = item
            combined = list(seen.values())
            combined.sort(key=lambda x: x.get("net_buy_qty", 0), reverse=True)
            return [
                {
                    "symbol": item.get("symbol", ""),
                    "name": item.get("name", item.get("hts_kor_isnm", "")),
                    "net_buy_qty": item.get("net_buy_qty", 0),
                }
                for item in combined[:20]
            ]
        except Exception as e:
            logger.debug(f"[전략적분석] 외국인 순매수 수집 실패: {e}")
            return None

    async def _collect_top_inst_buys(self) -> Optional[List[Dict[str, Any]]]:
        """기관 순매수 상위 20"""
        if not self._kis_market_data:
            return None
        try:
            results = await asyncio.gather(
                self._kis_market_data.fetch_foreign_institution(market="0001", investor="2"),
                self._kis_market_data.fetch_foreign_institution(market="0002", investor="2"),
                return_exceptions=True,
            )
            combined = []
            for res in results:
                if isinstance(res, list):
                    combined.extend(res)

            # symbol 기준 중복 제거 (순매수 많은 쪽 유지)
            seen = {}
            for item in combined:
                sym = item.get("symbol", "")
                if sym not in seen or item.get("net_buy_qty", 0) > seen[sym].get("net_buy_qty", 0):
                    seen[sym] = item
            combined = list(seen.values())
            combined.sort(key=lambda x: x.get("net_buy_qty", 0), reverse=True)
            return [
                {
                    "symbol": item.get("symbol", ""),
                    "name": item.get("name", item.get("hts_kor_isnm", "")),
                    "net_buy_qty": item.get("net_buy_qty", 0),
                }
                for item in combined[:20]
            ]
        except Exception as e:
            logger.debug(f"[전략적분석] 기관 순매수 수집 실패: {e}")
            return None

    async def _collect_sector_valuations(self) -> Optional[Dict[str, Any]]:
        """업종별 평균 PER/PBR (pykrx MCP)"""
        try:
            from ...utils.mcp_client import get_mcp_manager
            manager = get_mcp_manager()

            if not manager.is_server_available("pykrx"):
                return None

            target_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            result = await manager.call_tool(
                "pykrx",
                "get_market_fundamental_by_ticker",
                {"date": target_date, "market": "KOSPI"},
            )
            return result
        except Exception as e:
            logger.debug(f"[전략적분석] 밸류에이션 수집 실패: {e}")
            return None

    async def _collect_recent_themes(self) -> Optional[List[Dict[str, str]]]:
        """최근 탐지된 핫 테마"""
        if not self._theme_detector:
            return None
        try:
            themes = self._theme_detector.get_recent_themes()
            if not themes:
                return None
            return [
                {
                    "name": t.name,
                    "score": t.score,
                    "keywords": t.keywords[:5],
                    "symbols_count": len(t.related_stocks) if hasattr(t, 'related_stocks') else 0,
                }
                for t in themes[:10]
            ]
        except Exception as e:
            logger.debug(f"[전략적분석] 테마 수집 실패: {e}")
            return None

    async def _collect_news_summary(self) -> Optional[str]:
        """주요 경제 뉴스 요약 (LLM 사용하지 않고 최근 테마에서 추출)"""
        if not self._theme_detector:
            return None
        try:
            # theme_detector의 최근 뉴스 캐시에서 헤드라인 추출
            if hasattr(self._theme_detector, '_recent_articles'):
                articles = self._theme_detector._recent_articles
                if articles:
                    headlines = [a.title for a in articles[:15]]
                    return "\n".join(f"- {h}" for h in headlines)
            return None
        except Exception as e:
            logger.debug(f"[전략적분석] 뉴스 요약 수집 실패: {e}")
            return None
