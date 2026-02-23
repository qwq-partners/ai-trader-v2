"""
AI Trading Bot v2 - 전문가 패널 (Layer 1)

4명의 LLM 전문가가 실제 데이터 기반으로 유망 섹터/종목을 추천.
주 1회 실행 (일요일 21:00).

전문가 페르소나:
- 거시경제: 금리/환율/GDP → 섹터 영향
- 미시경제: 실적/밸류에이션/산업 트렌드
- 미국증권: 미국 시장 → 한국 수혜주
- 한국증권: KRX 수급/테마/정책 수혜
"""

import asyncio
import json
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class StockPick:
    """전문가 추천 종목"""
    symbol: str
    name: str
    horizon: str  # "1개월" | "3개월" | "6개월" | "1년"
    conviction: float  # 0~1 (전문가 합의도)
    reasons: List[str] = field(default_factory=list)
    recommended_by: List[str] = field(default_factory=list)
    target_sector: str = ""


@dataclass
class SectorView:
    """섹터별 전망"""
    name: str
    outlook: str  # "positive" | "neutral" | "negative"
    score: float  # -1 ~ +1
    reasons: List[str] = field(default_factory=list)


@dataclass
class StrategicOutlook:
    """전문가 패널 결과"""
    created_at: str = ""
    expires_at: str = ""
    market_regime: str = "neutral"  # "bullish" | "neutral" | "bearish"
    sector_outlook: Dict[str, SectorView] = field(default_factory=dict)
    recommended_stocks: List[StockPick] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        """유효 기간 내 여부"""
        if not self.expires_at:
            return False
        try:
            return datetime.now() < datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "market_regime": self.market_regime,
            "sector_outlook": {
                k: asdict(v) for k, v in self.sector_outlook.items()
            },
            "recommended_stocks": [asdict(s) for s in self.recommended_stocks],
            "risk_factors": self.risk_factors,
        }
        return result

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StrategicOutlook":
        outlook = cls(
            created_at=d.get("created_at", ""),
            expires_at=d.get("expires_at", ""),
            market_regime=d.get("market_regime", "neutral"),
            risk_factors=d.get("risk_factors", []),
        )
        for k, v in d.get("sector_outlook", {}).items():
            outlook.sector_outlook[k] = SectorView(**v)
        known_fields = {f.name for f in fields(StockPick)}
        for s in d.get("recommended_stocks", []):
            filtered = {k: v for k, v in s.items() if k in known_fields}
            outlook.recommended_stocks.append(StockPick(**filtered))
        return outlook


# 전문가 페르소나별 시스템 프롬프트
EXPERT_PROMPTS = {
    "macro": """당신은 한국 주식시장 전문 거시경제 분석가입니다.
금리, 환율, GDP, 인플레이션 등 거시 지표가 한국 주식시장 각 섹터에 미치는 영향을 분석합니다.
주어진 실제 데이터를 기반으로 향후 1~12개월 유망 섹터와 종목을 추천해주세요.
데이터에 없는 종목은 추천하지 마세요.""",

    "micro": """당신은 한국 주식시장 전문 미시경제/기업 분석가입니다.
업종별 PER/PBR, 실적 시즌 가이던스, 산업 트렌드를 분석합니다.
저평가 섹터, 실적 턴어라운드 종목, 성장주를 찾아주세요.
주어진 실제 데이터를 기반으로 추천하되, 데이터에 없는 종목은 추천하지 마세요.""",

    "us_market": """당신은 미국 증시 전문가로서 한국 수혜주를 찾는 역할입니다.
S&P500/나스닥 동향, 섹터 로테이션, AI/반도체/에너지 등 글로벌 트렌드가
한국 관련 종목에 미치는 영향을 분석합니다.
주어진 실제 데이터를 기반으로 한국 수혜주를 추천해주세요.""",

    "kr_market": """당신은 한국 증시 전문 트레이더입니다.
외국인/기관 수급 흐름, 정책 테마, 업종별 자금 흐름을 분석합니다.
스마트머니가 매집 중인 종목, 정책 수혜 종목을 찾아주세요.
주어진 실제 수급 데이터를 기반으로 추천하되, 데이터에 없는 종목은 추천하지 마세요.""",
}

USER_PROMPT_TEMPLATE = """## 현재 시장 데이터 ({date})

### 주요 지수 추이
{indices_text}

### 환율
{exchange_text}

### 외국인 순매수 상위
{foreign_text}

### 기관 순매수 상위
{inst_text}

### 최근 핫 테마
{themes_text}

### 최근 경제 뉴스
{news_text}

---

위 데이터를 분석하여 아래 JSON 형식으로 응답하세요:
```json
{{
  "market_regime": "bullish 또는 neutral 또는 bearish",
  "sector_views": [
    {{"name": "섹터명", "outlook": "positive/neutral/negative", "score": 0.5, "reasons": ["이유1"]}}
  ],
  "stock_picks": [
    {{
      "symbol": "종목코드(6자리)",
      "name": "종목명",
      "horizon": "1개월 또는 3개월 또는 6개월 또는 1년",
      "reasons": ["추천 근거1", "추천 근거2"]
    }}
  ],
  "risk_factors": ["리스크1", "리스크2"]
}}
```

중요:
- 종목코드는 반드시 6자리 숫자 (예: 005930)
- 위 데이터에 나온 종목 위주로 추천 (데이터에 없는 종목도 잘 알려진 종목이면 추천 가능)
- stock_picks는 3~10개
- 추천 근거는 구체적으로 (데이터 수치 인용)
"""


class ExpertPanel:
    """4인 전문가 LLM 패널"""

    def __init__(self, data_collector=None):
        self._data_collector = data_collector
        self._cache_dir = Path.home() / ".cache" / "ai_trader" / "strategic"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._outlook_path = self._cache_dir / "strategic_outlook.json"

    async def run_weekly_analysis(self) -> Optional[StrategicOutlook]:
        """주간 전문가 패널 실행"""
        logger.info("[전문가패널] ===== 주간 분석 시작 =====")

        try:
            # 1) 실제 데이터 수집
            if not self._data_collector:
                logger.error("[전문가패널] 데이터 수집기 없음")
                return None

            data = await self._data_collector.collect_all()

            # 2) 프롬프트 구성
            user_prompt = self._build_user_prompt(data)

            # 3) 4명 병렬 호출
            from ...utils.llm import get_llm_manager, LLMTask
            llm = get_llm_manager()

            results = await asyncio.gather(
                self._consult_expert(llm, "macro", user_prompt),
                self._consult_expert(llm, "micro", user_prompt),
                self._consult_expert(llm, "us_market", user_prompt),
                self._consult_expert(llm, "kr_market", user_prompt),
                return_exceptions=True,
            )

            # 유효한 결과만 필터
            valid_results = []
            for i, (expert_name, result) in enumerate(
                zip(["macro", "micro", "us_market", "kr_market"], results)
            ):
                if isinstance(result, Exception):
                    logger.warning(f"[전문가패널] {expert_name} 호출 실패: {result}")
                elif result and "error" not in result:
                    valid_results.append((expert_name, result))
                    picks = result.get("stock_picks", [])
                    logger.info(
                        f"[전문가패널] {expert_name}: "
                        f"레짐={result.get('market_regime', '?')}, "
                        f"추천 {len(picks)}종목"
                    )
                else:
                    logger.warning(f"[전문가패널] {expert_name} 결과 없음")

            if not valid_results:
                logger.error("[전문가패널] 유효한 전문가 응답 없음")
                return None

            # 4) 합의 도출
            consensus = self._build_consensus(valid_results)

            # 5) JSON 저장
            self._save_outlook(consensus)

            logger.info(
                f"[전문가패널] 분석 완료: "
                f"레짐={consensus.market_regime}, "
                f"추천 {len(consensus.recommended_stocks)}종목, "
                f"리스크 {len(consensus.risk_factors)}개"
            )

            return consensus

        except Exception as e:
            logger.error(f"[전문가패널] 주간 분석 오류: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    async def _consult_expert(
        self, llm, expert_name: str, user_prompt: str
    ) -> Optional[Dict[str, Any]]:
        """개별 전문가 호출"""
        from ...utils.llm import LLMTask

        system_prompt = EXPERT_PROMPTS.get(expert_name, "")
        try:
            result = await llm.complete_json(
                prompt=user_prompt,
                system=system_prompt + "\n응답은 반드시 유효한 JSON 형식으로만 해주세요.",
                task=LLMTask.STRATEGY_ANALYSIS,
                max_tokens=4096,
            )
            return result
        except Exception as e:
            logger.warning(f"[전문가패널] {expert_name} LLM 호출 실패: {e}")
            return None

    def _build_user_prompt(self, data: Dict[str, Any]) -> str:
        """데이터 → 프롬프트 변환"""
        # 지수
        indices = data.get("market_indices") or {}
        indices_lines = []
        for name, info in indices.items():
            if info:
                indices_lines.append(
                    f"- {name}: {info.get('current', '?')} "
                    f"(1개월 {info.get('change_1m_pct', 0):+.1f}%)"
                )
        indices_text = "\n".join(indices_lines) if indices_lines else "데이터 없음"

        # 환율
        exchange = data.get("exchange_rate")
        if exchange:
            exchange_text = (
                f"USD/KRW: {exchange.get('current', '?')}원 "
                f"(1개월 {exchange.get('change_1m_pct', 0):+.1f}%)"
            )
        else:
            exchange_text = "데이터 없음"

        # 외국인 순매수
        foreign = data.get("top_foreign_buys") or []
        foreign_lines = [
            f"- {f['name']}({f['symbol']}): {f.get('net_buy_qty', 0):,}주"
            for f in foreign[:10]
        ]
        foreign_text = "\n".join(foreign_lines) if foreign_lines else "데이터 없음"

        # 기관 순매수
        inst = data.get("top_inst_buys") or []
        inst_lines = [
            f"- {i['name']}({i['symbol']}): {i.get('net_buy_qty', 0):,}주"
            for i in inst[:10]
        ]
        inst_text = "\n".join(inst_lines) if inst_lines else "데이터 없음"

        # 테마
        themes = data.get("recent_themes") or []
        themes_lines = [
            f"- {t['name']} (강도: {t.get('score', 0):.0f})"
            for t in themes[:5]
        ]
        themes_text = "\n".join(themes_lines) if themes_lines else "데이터 없음"

        # 뉴스
        news_text = data.get("news_summary") or "데이터 없음"

        return USER_PROMPT_TEMPLATE.format(
            date=datetime.now().strftime("%Y-%m-%d"),
            indices_text=indices_text,
            exchange_text=exchange_text,
            foreign_text=foreign_text,
            inst_text=inst_text,
            themes_text=themes_text,
            news_text=news_text,
        )

    def _build_consensus(
        self, results: List[tuple]
    ) -> StrategicOutlook:
        """4인 전문가 결과 → 합의 도출"""
        now = datetime.now()
        outlook = StrategicOutlook(
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=7)).isoformat(),
        )

        # 마켓 레짐 투표
        regime_votes = {"bullish": 0, "neutral": 0, "bearish": 0}
        for expert_name, result in results:
            regime = result.get("market_regime", "neutral")
            regime_votes[regime] = regime_votes.get(regime, 0) + 1

        outlook.market_regime = max(regime_votes, key=regime_votes.get)

        # 섹터 전망 합산
        sector_scores: Dict[str, Dict] = {}
        for expert_name, result in results:
            for sv in result.get("sector_views", []):
                name = sv.get("name", "")
                if not name:
                    continue
                if name not in sector_scores:
                    sector_scores[name] = {"scores": [], "reasons": []}
                sector_scores[name]["scores"].append(sv.get("score", 0))
                sector_scores[name]["reasons"].extend(sv.get("reasons", []))

        for name, data in sector_scores.items():
            avg_score = sum(data["scores"]) / len(data["scores"])
            outlook_str = "positive" if avg_score > 0.2 else "negative" if avg_score < -0.2 else "neutral"
            outlook.sector_outlook[name] = SectorView(
                name=name,
                outlook=outlook_str,
                score=round(avg_score, 2),
                reasons=list(set(data["reasons"]))[:5],
            )

        # 종목 추천 합의
        stock_votes: Dict[str, Dict] = {}  # symbol → {experts, reasons, horizons, name, sector}
        for expert_name, result in results:
            for pick in result.get("stock_picks", []):
                symbol = str(pick.get("symbol", "")).strip().zfill(6)
                if not symbol.isdigit() or symbol == "000000" or len(symbol) != 6:
                    logger.debug(f"[전문가패널] 유효하지 않은 종목코드 무시: {pick.get('symbol')}")
                    continue
                if symbol not in stock_votes:
                    stock_votes[symbol] = {
                        "name": pick.get("name", ""),
                        "experts": [],
                        "reasons": [],
                        "horizons": [],
                        "sector": "",
                    }
                stock_votes[symbol]["experts"].append(expert_name)
                stock_votes[symbol]["reasons"].extend(pick.get("reasons", []))
                stock_votes[symbol]["horizons"].append(pick.get("horizon", "3개월"))
                if not stock_votes[symbol]["name"]:
                    stock_votes[symbol]["name"] = pick.get("name", "")

        for symbol, data in stock_votes.items():
            num_experts = len(data["experts"])
            # 합의도 계산
            if num_experts >= 4:
                conviction = 1.0
            elif num_experts >= 3:
                conviction = 0.75
            elif num_experts >= 2:
                conviction = 0.5
            else:
                conviction = 0.25

            # 가장 많이 선택된 horizon
            horizon_counts: Dict[str, int] = {}
            for h in data["horizons"]:
                horizon_counts[h] = horizon_counts.get(h, 0) + 1
            primary_horizon = max(horizon_counts, key=horizon_counts.get) if horizon_counts else "3개월"

            outlook.recommended_stocks.append(StockPick(
                symbol=symbol,
                name=data["name"],
                horizon=primary_horizon,
                conviction=conviction,
                reasons=list(set(data["reasons"]))[:5],
                recommended_by=data["experts"],
                target_sector=data["sector"],
            ))

        # conviction 내림차순
        outlook.recommended_stocks.sort(key=lambda x: x.conviction, reverse=True)

        # 리스크 요인 합산
        all_risks = []
        for _, result in results:
            all_risks.extend(result.get("risk_factors", []))
        outlook.risk_factors = list(set(all_risks))[:10]

        return outlook

    def _save_outlook(self, outlook: StrategicOutlook):
        """결과 JSON 저장"""
        try:
            with open(self._outlook_path, "w", encoding="utf-8") as f:
                json.dump(outlook.to_dict(), f, ensure_ascii=False, indent=2)
            logger.info(f"[전문가패널] 결과 저장: {self._outlook_path}")
        except Exception as e:
            logger.error(f"[전문가패널] 결과 저장 실패: {e}")

    def load_outlook(self) -> Optional[StrategicOutlook]:
        """캐시된 결과 로드"""
        try:
            if not self._outlook_path.exists():
                return None
            with open(self._outlook_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            outlook = StrategicOutlook.from_dict(data)
            if outlook.is_valid():
                return outlook
            logger.debug("[전문가패널] 캐시 만료됨")
            return None
        except Exception as e:
            logger.debug(f"[전문가패널] 캐시 로드 실패: {e}")
            return None
