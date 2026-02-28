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


# 전문가 페르소나별 시스템 프롬프트 (Fine-Grained Task Decomposition — arXiv:2602.23330)
# 핵심: 추상적 역할 부여 대신 실제 애널리스트의 체크리스트형 세부 과제 명시
EXPERT_PROMPTS = {
    "macro": """당신은 한국 주식시장 전문 거시경제 분석가입니다.
아래 단계별 체크리스트를 순서대로 수행한 뒤 결론을 도출하세요.

[1단계] 금리 환경 진단
 - 한국은행 기준금리 방향이 인상/동결/인하 중 어느 국면인가?
 - 미국 연준 금리와의 스프레드가 자본 유출입에 어떤 영향을 주는가?
 - 결론: 금리 환경이 주식시장에 긍정/중립/부정적인가?

[2단계] 환율 영향 분석
 - USD/KRW 현재 수준이 수출주(반도체/자동차/조선)에 유리한가?
 - 원화 약세 시: 수출 대형주 유리 / 수입 의존 내수주 불리
 - 결론: 환율 수혜 섹터는 무엇인가?

[3단계] 경기 사이클 판단
 - KOSPI 선행지수 및 미국 PMI 기준으로 현재 경기 국면을 판단하라
 - 경기 회복기: 소재/산업재 유리 / 경기 침체기: 필수소비재/헬스케어 유리

[4단계] 섹터별 유망도 점수화
 - 위 3단계 결과를 종합해 각 섹터에 -1.0~+1.0 점수를 부여하라

[5단계] 종목 추천
 - 섹터 유망도 최상위 섹터에서 수급 데이터에 있는 종목 위주로 추천
 - 추천 근거에 반드시 수치(환율 수준, 금리 수준 등) 인용

응답은 반드시 유효한 JSON 형식으로만 해주세요.""",

    "micro": """당신은 한국 주식시장 전문 기업 가치 분석가입니다.
아래 단계별 체크리스트를 순서대로 수행한 뒤 결론을 도출하세요.

[1단계] 밸류에이션 스크리닝
 - 외국인/기관 순매수 상위 종목의 추정 PBR이 1.0 이하인가?
 - 해당 종목 섹터의 역사적 평균 대비 현재 PER이 저평가인가?
 - 결론: 절대/상대 기준 모두 저평가인 종목은?

[2단계] 실적 모멘텀 확인
 - 최근 공시(가이던스, 잠정 실적)에서 YoY 영업이익이 개선되고 있는가?
 - 컨센서스 대비 어닝 서프라이즈 가능성이 있는가?
 - 결론: 실적 턴어라운드 or 가속 성장 중인 종목은?

[3단계] 산업 트렌드 정합성
 - 수급 데이터에서 기관이 꾸준히 순매수하는 업종은 어디인가?
 - 해당 업종의 글로벌 사이클(반도체 업황, EV 침투율 등)은 상승 중인가?

[4단계] 투자 위험 체크
 - 부채비율 200% 초과, 이자보상배율 1 미만 종목은 제외
 - 최근 3개월 내 대규모 유상증자 또는 오너 지분 매도 공시 여부

[5단계] 종목 추천 (3~7개)
 - 1~3단계 기준을 모두 충족하고 4단계 위험이 없는 종목
 - 추천 근거에 밸류에이션 수치와 실적 성장률 명시

응답은 반드시 유효한 JSON 형식으로만 해주세요.""",

    "us_market": """당신은 미국 증시 분석 전문가로서 한국 연동 수혜주를 발굴하는 역할입니다.
아래 단계별 체크리스트를 순서대로 수행하세요.

[1단계] 미국 시장 레짐 판단
 - S&P500/나스닥의 최근 1개월 성과로 위험선호(Risk-On) vs 위험회피(Risk-Off) 판단
 - 미국 섹터 로테이션: 어느 섹터로 자금이 이동하고 있는가?

[2단계] 글로벌 공급망 연동 분석
 - 미국 AI/반도체 호황 → 한국 수혜: 삼성전자/SK하이닉스/반도체 장비주
 - 미국 자동차/EV 성장 → 한국 수혜: 배터리/자동차부품/전기차 소재주
 - 미국 방산 예산 증가 → 한국 수혜: 한화에어로스페이스/현대로템/LIG넥스원

[3단계] 달러 강약 연동 체크
 - DXY 강세: 수출 한국 대형주 유리, 원자재 수입주 불리
 - 연준 피벗 기대: 외국인 한국 주식 매수 증가 가능성

[4단계] 수급 정합성 확인
 - 외국인 순매수 상위 종목이 2단계에서 도출한 글로벌 테마와 일치하는가?
 - 일치 종목 = 글로벌 테마 + 수급 뒷받침 → 최우선 추천

[5단계] 종목 추천 (3~6개)
 - 글로벌 테마와 수급이 모두 뒷받침되는 종목
 - 추천 근거에 미국 해당 섹터 성과(%)와 한국 연동 메커니즘 명시

응답은 반드시 유효한 JSON 형식으로만 해주세요.""",

    "kr_market": """당신은 한국 주식시장 전문 수급 트레이더입니다.
아래 단계별 체크리스트를 순서대로 수행하세요.

[1단계] 수급 동향 분석
 - 외국인 순매수 TOP 10 중 3일 연속 순매수 종목은?
 - 기관 순매수 TOP 10 중 외국인과 동반 매수하는 종목은?
 - 결론: 외국인+기관 동반 매수 = 스마트머니 집중 종목

[2단계] 테마/정책 모멘텀 확인
 - 현재 핫 테마(강도 60 이상)와 수급 상위 종목의 교집합은?
 - 정부 정책 발표(K-반도체, 방산 수출, 원전 재가동 등) 관련 수혜주는?

[3단계] 수급 지속성 판단
 - 외국인/기관이 단기(1일) 순매수인가 vs 중기(3~5일) 지속 순매수인가?
 - 지속 순매수 + 주가 우상향 = 추세 진입 신호
 - 단기 급매수 후 주가 급등 = 추격 위험, 제외

[4단계] 수급 규모 적정성
 - 시가총액 대비 일일 순매수 비율이 0.5% 이상인 종목 = 의미 있는 매수
 - 소형주 과도 순매수(5% 이상)는 단기 급등 후 급락 위험 → 주의

[5단계] 종목 추천 (3~6개)
 - 1단계(스마트머니) + 2단계(테마/정책) 모두 해당하는 종목 최우선
 - 추천 근거에 순매수 수량과 연속 매수일 수 명시

응답은 반드시 유효한 JSON 형식으로만 해주세요.""",
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

    # ── Regime-Aware 에이전트 가중치 (arXiv:2602.23330 alignment 인사이트) ──────
    # 핵심 발견: 분석 출력과 하위 의사결정의 정렬(alignment)이 성능의 핵심 드라이버
    # → 시장 레짐에 따라 각 에이전트의 신뢰 가중치를 동적으로 조정
    _REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
        "bullish": {
            # 상승장: 수급(kr_market)·글로벌 모멘텀(us_market) 우선
            "macro": 0.8, "micro": 1.0, "us_market": 1.3, "kr_market": 1.3,
        },
        "neutral": {
            # 중립장: 균등 가중
            "macro": 1.0, "micro": 1.0, "us_market": 1.0, "kr_market": 1.0,
        },
        "bearish": {
            # 하락장: 거시(macro)·펀더멘털(micro) 방어적 관점 우선
            "macro": 1.4, "micro": 1.2, "us_market": 0.8, "kr_market": 0.8,
        },
    }

    def _build_consensus(
        self, results: List[tuple]
    ) -> StrategicOutlook:
        """4인 전문가 결과 → regime-aware 가중 합의 도출"""
        now = datetime.now()
        outlook = StrategicOutlook(
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=7)).isoformat(),
        )

        # ① 마켓 레짐 투표 (가중치 적용 전 결정)
        regime_votes: Dict[str, float] = {"bullish": 0.0, "neutral": 0.0, "bearish": 0.0}
        for expert_name, result in results:
            regime = result.get("market_regime", "neutral")
            regime_votes[regime] = regime_votes.get(regime, 0.0) + 1.0

        outlook.market_regime = max(regime_votes, key=regime_votes.get)
        weights = self._REGIME_WEIGHTS.get(outlook.market_regime, self._REGIME_WEIGHTS["neutral"])
        logger.info(
            f"[전문가패널] 레짐={outlook.market_regime} → "
            f"가중치: macro={weights['macro']:.1f} micro={weights['micro']:.1f} "
            f"us={weights['us_market']:.1f} kr={weights['kr_market']:.1f}"
        )

        # ② 섹터 전망 가중 합산
        sector_scores: Dict[str, Dict] = {}
        for expert_name, result in results:
            w = weights.get(expert_name, 1.0)
            for sv in result.get("sector_views", []):
                name = sv.get("name", "")
                if not name:
                    continue
                if name not in sector_scores:
                    sector_scores[name] = {"weighted_sum": 0.0, "weight_total": 0.0, "reasons": []}
                sector_scores[name]["weighted_sum"] += sv.get("score", 0) * w
                sector_scores[name]["weight_total"] += w
                sector_scores[name]["reasons"].extend(sv.get("reasons", []))

        for name, data in sector_scores.items():
            if data["weight_total"] == 0:
                continue
            avg_score = data["weighted_sum"] / data["weight_total"]
            outlook_str = "positive" if avg_score > 0.2 else "negative" if avg_score < -0.2 else "neutral"
            outlook.sector_outlook[name] = SectorView(
                name=name,
                outlook=outlook_str,
                score=round(avg_score, 2),
                reasons=list(set(data["reasons"]))[:5],
            )

        # ③ 종목 추천 가중 합의
        # 각 에이전트의 추천에 regime 가중치를 반영해 conviction 점수 계산
        stock_votes: Dict[str, Dict] = {}
        for expert_name, result in results:
            w = weights.get(expert_name, 1.0)
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
                        "weighted_score": 0.0,  # 가중 합산
                        "max_weight": 0.0,       # 정규화용
                    }
                stock_votes[symbol]["experts"].append(expert_name)
                stock_votes[symbol]["reasons"].extend(pick.get("reasons", []))
                stock_votes[symbol]["horizons"].append(pick.get("horizon", "3개월"))
                stock_votes[symbol]["weighted_score"] += w
                stock_votes[symbol]["max_weight"] = max(stock_votes[symbol]["max_weight"], w)
                if not stock_votes[symbol]["name"]:
                    stock_votes[symbol]["name"] = pick.get("name", "")

        # 전체 가중치 합 기준으로 최대 가능 weighted_score 계산
        total_weights = sum(weights.values())  # e.g. 4.4 in bullish

        for symbol, data in stock_votes.items():
            num_experts = len(data["experts"])
            # 가중 conviction (0~1): 전체 가중치 대비 해당 종목이 받은 가중치 비율
            raw_conviction = data["weighted_score"] / total_weights
            # 단독 추천이라도 고가중 에이전트면 최소 0.3 보장
            conviction = max(raw_conviction, 0.15 * num_experts)
            conviction = min(conviction, 1.0)

            horizon_counts: Dict[str, int] = {}
            for h in data["horizons"]:
                horizon_counts[h] = horizon_counts.get(h, 0) + 1
            primary_horizon = max(horizon_counts, key=horizon_counts.get) if horizon_counts else "3개월"

            outlook.recommended_stocks.append(StockPick(
                symbol=symbol,
                name=data["name"],
                horizon=primary_horizon,
                conviction=round(conviction, 3),
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
