"""
AI Trading Bot v2 - 테마 탐지 시스템

뉴스와 시장 데이터를 분석하여 현재 핫한 테마를 실시간 탐지합니다.

핵심 기능:
1. 뉴스 수집 (네이버 금융)
2. LLM 기반 테마 추출
3. 테마-종목 매핑
4. 테마 강도 스코어링
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Any
import aiohttp
from loguru import logger

from ...core.types import Theme
from ...core.event import ThemeEvent
from ...utils.llm import LLMManager, LLMTask, get_llm_manager
from ...data.storage.news_storage import (
    NewsStorage, NewsArticle as StoredNewsArticle, ThemeRecord, get_news_storage
)


@dataclass
class NewsArticle:
    """뉴스 기사"""
    title: str
    content: str = ""
    url: str = ""
    source: str = ""
    published_at: datetime = field(default_factory=datetime.now)

    @property
    def text(self) -> str:
        """제목 + 본문"""
        return f"{self.title}\n{self.content}" if self.content else self.title


@dataclass
class ThemeInfo:
    """테마 정보"""
    name: str
    keywords: List[str] = field(default_factory=list)
    related_stocks: List[str] = field(default_factory=list)  # 종목코드
    news_count: int = 0
    mention_count: int = 0
    score: float = 0.0
    detected_at: datetime = field(default_factory=datetime.now)
    last_updated: datetime = field(default_factory=datetime.now)

    def to_theme(self) -> Theme:
        """Theme 객체로 변환"""
        return Theme(
            name=self.name,
            keywords=self.keywords,
            symbols=self.related_stocks,
            score=self.score,
            news_count=self.news_count,
            detected_at=self.detected_at,
        )


# ============================================================
# 한국 시장 테마-종목 매핑 (기본 데이터)
# ============================================================

DEFAULT_THEME_STOCKS = {
    "AI/반도체": ["005930", "000660", "042700", "403870", "357780"],  # 삼전, 하이닉스, 한미반도체, HPSP, 하이브
    "2차전지": ["373220", "006400", "247540", "086520", "003670"],   # LG에너지, 삼성SDI, 에코프로BM, 에코프로, 포스코퓨처엠
    "바이오": ["207940", "068270", "326030", "196170", "091990"],    # 삼바, 셀트리온, SK바이오팜, 알테오젠, 셀트리온헬스
    "로봇": ["277810", "454910", "090460", "049950", "012510"],      # 레인보우로보, 두산로보, 비에이치아이, 엘앤에프, 두산
    "방산": ["012450", "079550", "047810", "006260", "012750"],      # 한화에어로, LIG넥스원, 한국항공우주, 한화, 에스원
    "조선": ["010140", "009540", "042660", "042670", "267250"],      # 삼성중공업, 한국조선해양, 대우조선해양, HD현대인프라코어, HD현대
    "금융/은행": ["105560", "086790", "055550", "024110", "316140"], # KB금융, 하나금융, 신한지주, 기업은행, 우리금융
    "자동차": ["005380", "000270", "012330", "161390", "204320"],    # 현대차, 기아, 현대모비스, 한국타이어, 만도
    "엔터": ["352820", "041510", "122870", "035900", "293480"],      # 하이브, SM, YG, JYP, 카카오게임즈
    "게임": ["263750", "112040", "036570", "251270", "194480"],      # 펄어비스, 위메이드, 엔씨소프트, 넷마블, 데브시스터즈
    "화장품": ["090430", "051900", "069960", "003850", "214370"],    # 아모레퍼시픽, LG생활건강, 현대백화점, 콜마HD, 케어젠
    "인터넷/플랫폼": ["035720", "035420", "263750", "036570", "251270"], # 카카오, 네이버
    "건설": ["000720", "028260", "047040", "034730", "000210"],      # 현대건설, 삼성물산, 대우건설, SK에코플랜트, DL
    "원자력": ["009830", "034020", "092870", "042600", "331910"],    # 한화솔루션, 두산에너빌리티, 마이크로, 새로닉스, 코스텍시스템
    "탄소중립": ["009830", "117580", "003580", "267260", "293490"],  # 한화솔루션, 대성에너지, 넥스틸, 현대일렉트릭, 카카오페이
}

# 테마 키워드 매핑
THEME_KEYWORDS = {
    "AI/반도체": ["반도체", "AI", "인공지능", "HBM", "GPU", "엔비디아", "메모리", "파운드리", "TSMC", "삼성전자", "SK하이닉스"],
    "2차전지": ["2차전지", "배터리", "전기차", "EV", "리튬", "양극재", "음극재", "분리막", "전해질", "에코프로", "LG에너지"],
    "바이오": ["바이오", "신약", "임상", "FDA", "의약품", "제약", "셀트리온", "삼성바이오", "항암제", "항체"],
    "로봇": ["로봇", "휴머노이드", "자동화", "협동로봇", "산업용로봇", "테슬라봇", "보스턴다이나믹스"],
    "방산": ["방산", "무기", "미사일", "K방산", "수출", "국방", "한화에어로스페이스", "LIG넥스원"],
    "조선": ["조선", "LNG선", "컨테이너선", "수주", "HD현대", "삼성중공업", "한국조선해양"],
    "원자력": ["원전", "원자력", "SMR", "소형모듈원자로", "두산에너빌리티", "핵발전"],
    "탄소중립": ["탄소중립", "신재생", "태양광", "풍력", "ESG", "그린뉴딜", "수소"],
}


# ============================================================
# 주요 종목 이름→코드 매핑 (LLM 프롬프트 힌트용)
# ============================================================
KNOWN_STOCKS = {
    "삼성전자": "005930", "SK하이닉스": "000660", "현대차": "005380",
    "LG에너지솔루션": "373220", "삼성바이오로직스": "207940", "삼성바이오": "207940",
    "셀트리온": "068270", "네이버": "035420", "카카오": "035720",
    "기아": "000270", "포스코홀딩스": "005490", "삼성SDI": "006400",
    "KB금융": "105560", "한화에어로스페이스": "012450", "한화에어로": "012450",
    "HD현대중공업": "329180", "에코프로BM": "247540", "에코프로비엠": "247540",
    "에코프로": "086520", "LG화학": "051910", "현대모비스": "012330",
    "POSCO홀딩스": "005490", "SK이노베이션": "096770", "LG전자": "066570",
    "삼성물산": "028260", "한국전력": "015760", "하나금융지주": "086790",
    "신한지주": "055550", "SK텔레콤": "017670", "KT": "030200",
    "한미반도체": "042700", "두산에너빌리티": "034020", "포스코퓨처엠": "003670",
    "알테오젠": "196170", "HD현대": "267250", "LIG넥스원": "079550",
    "한국항공우주": "047810", "삼성중공업": "010140", "하이브": "352820",
    "크래프톤": "259960", "SK바이오팜": "326030", "카카오뱅크": "323410",
    "SK": "034730", "LG": "003550", "한화솔루션": "009830",
    "현대건설": "000720", "만도": "204320", "한국타이어앤테크놀로지": "161390",
    "HPSP": "403870", "레인보우로보틱스": "277810", "두산로보틱스": "454910",
    "한화": "006260", "우리금융": "316140", "기업은행": "024110",
    "SM": "041510", "YG": "122870", "JYP": "035900",
    "펄어비스": "263750", "위메이드": "112040", "엔씨소프트": "036570",
    "넷마블": "251270", "아모레퍼시픽": "090430", "LG생활건강": "051900",
    "현대일렉트릭": "267260", "대우조선해양": "042660",
    "한국조선해양": "009540", "HD현대인프라코어": "042670",
}


class NewsCollector:
    """뉴스 수집기 (네이버 금융)"""

    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self._session: Optional[aiohttp.ClientSession] = None

        # 네이버 API 키가 없으면 환경변수에서 로드
        if not self.client_id:
            import os
            self.client_id = os.getenv("NAVER_CLIENT_ID", "")
            self.client_secret = os.getenv("NAVER_CLIENT_SECRET", "")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def search_news(
        self,
        query: str,
        display: int = 20,
        sort: str = "date"
    ) -> List[NewsArticle]:
        """네이버 뉴스 검색"""
        if not self.client_id:
            logger.warning("네이버 API 키 없음 - 뉴스 수집 건너뜀")
            return []

        try:
            session = await self._get_session()
            url = "https://openapi.naver.com/v1/search/news.json"

            async with session.get(
                url,
                headers={
                    "X-Naver-Client-Id": self.client_id,
                    "X-Naver-Client-Secret": self.client_secret,
                },
                params={
                    "query": query,
                    "display": display,
                    "sort": sort,
                }
            ) as resp:
                if resp.status != 200:
                    logger.error(f"뉴스 검색 실패: {resp.status}")
                    return []

                data = await resp.json()
                articles = []

                for item in data.get("items", []):
                    # HTML 태그 제거
                    title = re.sub(r'<[^>]+>', '', item.get("title", ""))
                    description = re.sub(r'<[^>]+>', '', item.get("description", ""))

                    articles.append(NewsArticle(
                        title=title,
                        content=description,
                        url=item.get("link", ""),
                        source=item.get("originallink", ""),
                    ))

                return articles

        except Exception as e:
            logger.error(f"뉴스 수집 오류: {e}")
            return []

    async def get_market_news(self, limit: int = 30) -> List[NewsArticle]:
        """시장 전반 뉴스 수집"""
        queries = ["증시", "코스피", "코스닥", "주식시장"]
        all_articles = []

        for query in queries:
            articles = await self.search_news(query, display=limit // len(queries))
            all_articles.extend(articles)

        return all_articles

    async def get_theme_news(self, theme: str, limit: int = 10) -> List[NewsArticle]:
        """특정 테마 뉴스 수집"""
        keywords = THEME_KEYWORDS.get(theme, [theme])
        query = " ".join(keywords[:3])  # 상위 3개 키워드 사용
        return await self.search_news(query, display=limit)


class ThemeDetector:
    """
    테마 탐지기

    뉴스를 분석하여 현재 핫한 테마를 탐지합니다.
    수집된 뉴스와 테마 히스토리는 PostgreSQL에 저장됩니다.
    """

    # 테마 → 업종 키워드 매핑 (KIS 업종지수 sec_kw in sec_name 매칭)
    # KIS 표준 업종명: 전기전자, 화학, 의약품, 기계, 운수장비, 서비스업,
    #   건설업, 전기가스업, 금융업, 은행, 증권, 유통업, 통신업, 철강금속 등
    THEME_SECTOR_MAP: Dict[str, List[str]] = {
        "AI/반도체": ["전기전자", "통신업"],
        "2차전지": ["전기전자", "화학"],
        "바이오": ["의약품"],
        "로봇": ["기계", "전기전자"],
        "방산": ["기계", "운수장비"],
        "조선": ["운수장비"],
        "금융/은행": ["은행", "금융업", "증권"],
        "자동차": ["운수장비"],
        "엔터": ["서비스업"],
        "게임": ["서비스업", "통신업"],
        "화장품": ["화학", "유통업"],
        "인터넷/플랫폼": ["서비스업", "통신업"],
        "건설": ["건설업"],
        "원자력": ["전기가스", "전기전자"],
        "탄소중립": ["전기가스", "화학"],
    }

    def __init__(self, llm_manager: Optional[LLMManager] = None, kis_market_data=None, us_market_data=None):
        self.llm = llm_manager or get_llm_manager()
        self.news_collector = NewsCollector()
        self._kis_market_data = kis_market_data
        self._us_market_data = us_market_data

        # 뉴스/테마 저장소 (PostgreSQL)
        self._storage: Optional[NewsStorage] = None
        self._storage_initialized = False

        # 테마 추적
        self._themes: Dict[str, ThemeInfo] = {}
        self._last_detection: Optional[datetime] = None

        # 종목별 뉴스 센티멘트 (LLM 결과 저장)
        # {symbol: {sentiment, impact, direction, theme, reason, updated_at}}
        self._stock_sentiments: Dict[str, Dict] = {}

        # 설정
        self.detection_interval_minutes = 30  # 탐지 주기
        self.min_news_count = 3  # 최소 뉴스 수
        self.hot_theme_threshold = 70  # 핫 테마 기준 점수

        # 키워드→테마 역매핑 (정규화용)
        self._keyword_to_theme: Dict[str, str] = {}
        for theme_name, keywords in THEME_KEYWORDS.items():
            for kw in keywords:
                self._keyword_to_theme[kw.lower()] = theme_name
            # 테마명 자체도 키워드로 등록
            self._keyword_to_theme[theme_name.lower()] = theme_name

    def _normalize_theme_name(self, raw_name: str) -> str:
        """LLM 반환 테마명을 DEFAULT_THEME_STOCKS 키로 정규화"""
        # 1. 정확히 일치
        if raw_name in DEFAULT_THEME_STOCKS:
            return raw_name

        # 2. 소문자 비교
        raw_lower = raw_name.lower().strip()
        if raw_lower in self._keyword_to_theme:
            return self._keyword_to_theme[raw_lower]

        # 3. 부분 문자열 매칭 (가장 긴 매칭 우선 → 모호성 방지)
        candidates = []
        for key in DEFAULT_THEME_STOCKS:
            key_lower = key.lower()
            if raw_lower in key_lower or key_lower in raw_lower:
                candidates.append(key)
        if candidates:
            # 가장 긴 키를 우선 (예: "AI/반도체" > "AI")
            candidates.sort(key=len, reverse=True)
            return candidates[0]

        # 4. 키워드 포함 매칭 (가장 긴 키워드 우선)
        kw_candidates = []
        for kw, theme in self._keyword_to_theme.items():
            if kw in raw_lower:
                kw_candidates.append((len(kw), theme))
        if kw_candidates:
            kw_candidates.sort(reverse=True)
            return kw_candidates[0][1]

        logger.debug(f"[ThemeDetector] 매핑 불가 테마명: {raw_name}")
        return raw_name

    async def _ensure_storage(self):
        """저장소 연결 보장"""
        if not self._storage_initialized:
            try:
                self._storage = await get_news_storage()
                self._storage_initialized = True
                logger.info("[ThemeDetector] 뉴스 저장소 연결 완료")
            except Exception as e:
                logger.warning(f"[ThemeDetector] 저장소 연결 실패 (메모리 모드): {e}")
                self._storage = None

    async def detect_themes(self, force: bool = False) -> List[ThemeInfo]:
        """
        테마 탐지 실행

        Args:
            force: 강제 실행 여부
        """
        # 탐지 주기 체크
        if not force and self._last_detection:
            elapsed = (datetime.now() - self._last_detection).total_seconds() / 60
            if elapsed < self.detection_interval_minutes:
                return list(self._themes.values())

        logger.info("테마 탐지 시작...")
        self._last_detection = datetime.now()

        # 저장소 연결 보장
        await self._ensure_storage()

        try:
            # 1. 시장 뉴스 수집
            news_articles = await self.news_collector.get_market_news(limit=30)

            if not news_articles:
                logger.warning("수집된 뉴스 없음")
                return list(self._themes.values())

            logger.info(f"뉴스 {len(news_articles)}건 수집 완료")

            # 2. LLM으로 테마 + 종목 임팩트 추출 (DB 저장보다 먼저 실행)
            llm_result = await self._extract_themes_from_news(news_articles)
            detected_themes = llm_result.get("themes", [])
            stock_impacts = llm_result.get("stock_impacts", [])

            # 2-1. 뉴스를 DB에 저장 (LLM 결과 후 → sentiment_score 채울 수 있음)
            if self._storage:
                stored_articles = [
                    StoredNewsArticle(
                        title=a.title,
                        content=a.content,
                        url=a.url,
                        source=a.source,
                        published_at=a.published_at,
                        sentiment_score=self._estimate_article_sentiment(a.title),
                    )
                    for a in news_articles
                ]
                saved = await self._storage.save_news_batch(stored_articles)
                logger.debug(f"[ThemeDetector] 뉴스 {saved}건 DB 저장")

            if not detected_themes:
                logger.info("감지된 테마 없음 - 기존 테마 유지")
                return list(self._themes.values())

            # 2-2. 종목별 센티멘트 파싱 (themes[].stocks + stock_impacts)
            now = datetime.now()

            # stock_impacts에서 파싱 (기존 엔트리보다 impact가 높으면 덮어쓰기)
            for item in stock_impacts:
                symbol = self._resolve_stock_symbol(
                    item.get("symbol", ""), item.get("name", "")
                )
                if not symbol:
                    continue
                impact = item.get("impact", 0)
                direction = item.get("direction", "bullish")
                existing = self._stock_sentiments.get(symbol)
                if not existing or impact > existing.get("impact", 0):
                    self._stock_sentiments[symbol] = {
                        "sentiment": 1.0 if direction == "bullish" else -1.0,
                        "impact": impact,
                        "direction": direction,
                        "theme": "",
                        "reason": item.get("reason", ""),
                        "updated_at": now,
                    }

            # 3. 테마 정보 업데이트
            for theme_data in detected_themes:
                # themes[].stocks에서 종목 센티멘트 파싱
                for stock_item in theme_data.get("stocks", []):
                    symbol = self._resolve_stock_symbol(
                        stock_item.get("symbol", ""), stock_item.get("name", "")
                    )
                    if not symbol:
                        continue
                    impact = stock_item.get("impact", 0)
                    direction = stock_item.get("direction", "bullish")
                    theme_name_raw = theme_data.get("theme", "")
                    # 기존 엔트리보다 impact가 높으면 덮어쓰기
                    existing = self._stock_sentiments.get(symbol)
                    if not existing or impact > existing.get("impact", 0):
                        self._stock_sentiments[symbol] = {
                            "sentiment": 1.0 if direction == "bullish" else -1.0,
                            "impact": impact,
                            "direction": direction,
                            "theme": theme_name_raw,
                            "reason": f"테마[{theme_name_raw}] 관련",
                            "updated_at": now,
                        }

                # 테마명 추출 및 정규화
                raw_name = theme_data.get("theme", "")
                if not raw_name:
                    continue

                theme_name = self._normalize_theme_name(raw_name)
                if theme_name != raw_name:
                    logger.info(f"[ThemeDetector] 테마명 정규화: '{raw_name}' → '{theme_name}'")

                if theme_name in self._themes:
                    # 기존 테마 업데이트
                    theme = self._themes[theme_name]
                    theme.news_count = theme_data.get("news_count", 0)
                    theme.score = theme_data.get("score", 0)
                    theme.last_updated = datetime.now()
                else:
                    # 새 테마 추가
                    related_stocks = DEFAULT_THEME_STOCKS.get(theme_name, [])

                    self._themes[theme_name] = ThemeInfo(
                        name=theme_name,
                        keywords=THEME_KEYWORDS.get(theme_name, [theme_name]),
                        related_stocks=related_stocks,
                        news_count=theme_data.get("news_count", 0),
                        score=theme_data.get("score", 0),
                    )

            if self._stock_sentiments:
                logger.info(
                    f"[ThemeDetector] 종목 센티멘트 {len(self._stock_sentiments)}개 갱신"
                )

            # 3-1. 업종지수 데이터로 테마 점수 보정
            await self._adjust_scores_by_sector()

            # 3-2. US 시장 오버나이트 데이터로 테마 점수 보정
            await self._adjust_scores_by_us_market()

            # 4. 오래된 테마 제거 (1시간 이상 업데이트 없음)
            cutoff = datetime.now() - timedelta(hours=1)
            self._themes = {
                name: theme
                for name, theme in self._themes.items()
                if theme.last_updated > cutoff
            }

            # 4-1. 오래된 종목 센티멘트 제거 (1시간 이상)
            stale_symbols = [
                sym for sym, data in self._stock_sentiments.items()
                if data.get("updated_at", datetime.min) < cutoff
            ]
            for sym in stale_symbols:
                del self._stock_sentiments[sym]
            if stale_symbols:
                logger.debug(f"[ThemeDetector] 스테일 센티멘트 {len(stale_symbols)}개 제거")

            # 5. 테마 히스토리를 DB에 저장
            if self._storage and self._themes:
                theme_records = [
                    ThemeRecord(
                        theme_name=theme.name,
                        score=theme.score,
                        news_count=theme.news_count,
                        keywords=theme.keywords,
                    )
                    for theme in self._themes.values()
                ]
                await self._storage.save_themes_batch(theme_records)
                logger.debug(f"[ThemeDetector] 테마 {len(theme_records)}개 DB 저장")

            logger.info(f"테마 탐지 완료: {len(self._themes)}개 테마 활성")
            return list(self._themes.values())

        except Exception as e:
            logger.exception(f"테마 탐지 오류: {e}")
            return list(self._themes.values())

    async def _extract_themes_from_news(
        self,
        articles: List[NewsArticle]
    ) -> Dict[str, Any]:
        """
        LLM을 사용하여 뉴스에서 테마 + 종목별 임팩트 동시 추출

        Returns:
            {"themes": [...], "stock_impacts": [...]}
        """
        # 뉴스 제목들 준비
        titles = "\n".join([f"- {a.title}" for a in articles[:20]])  # 최대 20개

        theme_list = list(DEFAULT_THEME_STOCKS.keys())
        numbered_themes = "\n".join([f"  {i+1}. {t}" for i, t in enumerate(theme_list)])

        # KNOWN_STOCKS 힌트 (상위 40개)
        known_stocks_hint = "\n".join(
            [f"  {name}={code}" for name, code in list(KNOWN_STOCKS.items())[:40]]
        )

        prompt = f"""다음은 오늘의 한국 주식시장 뉴스 제목들입니다:

{titles}

위 뉴스들을 분석하여 (1) 투자 테마와 (2) 개별 종목 임팩트를 동시에 추출해주세요.

**허용 테마 목록 (반드시 이 중에서만 선택):**
{numbered_themes}

**참고 - 주요 종목코드:**
{known_stocks_hint}

다음 JSON 형식으로만 응답하세요:
{{
  "themes": [
    {{
      "theme": "위 목록의 테마명 그대로",
      "news_count": 관련 뉴스 수,
      "score": 0-100 사이의 테마 강도 점수,
      "reason": "테마 선정 이유 (20자 이내)",
      "stocks": [
        {{
          "symbol": "6자리 종목코드",
          "name": "종목명",
          "impact": 0-100 영향도 점수,
          "direction": "bullish 또는 bearish"
        }}
      ]
    }}
  ],
  "stock_impacts": [
    {{
      "symbol": "6자리 종목코드",
      "name": "종목명",
      "impact": 0-100 영향도 점수,
      "direction": "bullish 또는 bearish",
      "reason": "영향 이유 (30자 이내)"
    }}
  ]
}}

규칙:
1. themes: 최대 5개 테마, 뉴스 2개 이상인 테마만, 각 테마별 stocks는 최대 5개
2. stock_impacts: 테마 무관하게 뉴스에 직접 언급된 개별 종목 (최대 10개)
3. impact: 0=무관, 50=보통, 80+=강한 영향
4. direction: 호재=bullish, 악재=bearish
5. theme 필드는 반드시 위 허용 목록의 테마명을 정확히 사용
6. symbol은 위 종목코드 힌트 참고, 모르면 빈 문자열"""

        result = await self.llm.complete_json(prompt, task=LLMTask.THEME_DETECTION)

        if "error" in result:
            logger.error(f"테마 추출 LLM 오류: {result.get('error')}")
            return {"themes": [], "stock_impacts": []}

        return {
            "themes": result.get("themes", []),
            "stock_impacts": result.get("stock_impacts", []),
        }

    async def get_theme_stocks(self, theme_name: str) -> List[str]:
        """테마 관련 종목 조회"""
        theme = self._themes.get(theme_name)
        if theme:
            return theme.related_stocks

        # 기본 매핑에서 찾기
        return DEFAULT_THEME_STOCKS.get(theme_name, [])

    def get_hot_themes(self, min_score: float = 70) -> List[ThemeInfo]:
        """핫 테마 목록 (점수 기준)"""
        return [
            theme for theme in self._themes.values()
            if theme.score >= min_score
        ]

    def get_stock_themes(self, symbol: str) -> List[str]:
        """특정 종목이 속한 테마들"""
        themes = []
        for theme_name, stocks in DEFAULT_THEME_STOCKS.items():
            if symbol in stocks:
                themes.append(theme_name)
        return themes

    def get_all_theme_stocks(self) -> Dict[str, List[str]]:
        """모든 테마와 관련 종목 반환"""
        return DEFAULT_THEME_STOCKS.copy()

    def get_theme_score(self, symbol: str) -> float:
        """종목의 테마 점수 (해당 종목이 속한 테마들의 최고 점수)"""
        themes = self.get_stock_themes(symbol)
        if not themes:
            return 0.0

        scores = []
        for theme_name in themes:
            if theme_name in self._themes:
                scores.append(self._themes[theme_name].score)

        return max(scores) if scores else 0.0

    def to_events(self) -> List[ThemeEvent]:
        """현재 테마들을 이벤트로 변환"""
        events = []
        for theme in self._themes.values():
            events.append(ThemeEvent.from_theme(theme.to_theme(), source="theme_detector"))
        return events

    # ============================================================
    # 종목 센티멘트 접근자
    # ============================================================

    def get_stock_sentiment(self, symbol: str) -> Optional[Dict]:
        """
        종목 센티멘트 조회 (1시간 이내 데이터만 반환)

        Returns:
            {sentiment, impact, direction, theme, reason, updated_at} 또는 None
        """
        data = self._stock_sentiments.get(symbol)
        if not data:
            return None
        # 1시간 이상 경과 시 무효
        elapsed = (datetime.now() - data["updated_at"]).total_seconds()
        if elapsed > 3600:
            return None
        return data

    def get_all_stock_sentiments(self) -> Dict[str, Dict]:
        """전체 유효 센티멘트 (1시간 이내)"""
        now = datetime.now()
        return {
            symbol: data
            for symbol, data in self._stock_sentiments.items()
            if (now - data["updated_at"]).total_seconds() <= 3600
        }

    def _resolve_stock_symbol(self, symbol: str, name: str) -> str:
        """종목코드 보정: 코드가 유효하면 그대로, 아니면 이름으로 KNOWN_STOCKS 매핑"""
        symbol = symbol.strip()
        if symbol and symbol.isdigit() and len(symbol) == 6:
            return symbol
        # 이름으로 코드 조회
        name = name.strip()
        resolved = KNOWN_STOCKS.get(name, "")
        if resolved:
            return resolved
        return ""

    async def _adjust_scores_by_sector(self):
        """업종지수 등락률 기반 테마 점수 보정"""
        kmd = self._kis_market_data
        if not kmd:
            try:
                from ...data.providers.kis_market_data import get_kis_market_data
                kmd = get_kis_market_data()
            except Exception:
                return

        try:
            sectors = await kmd.fetch_sector_indices()
            if not sectors:
                return

            # 업종명 → 등락률 맵
            sector_map: Dict[str, float] = {}
            for s in sectors:
                name = s.get("name", "")
                change_pct = s.get("change_pct", 0.0)
                if name:
                    sector_map[name] = change_pct

            adjusted_cnt = 0
            for theme_name, theme_info in self._themes.items():
                related_sectors = self.THEME_SECTOR_MAP.get(theme_name, [])
                if not related_sectors:
                    continue

                # 관련 업종 등락률 평균
                pcts = []
                for sec_kw in related_sectors:
                    for sec_name, pct in sector_map.items():
                        if sec_kw in sec_name:
                            pcts.append(pct)
                            break

                if not pcts:
                    continue

                avg_pct = sum(pcts) / len(pcts)

                # 상승 업종: +10~+20 보너스 / 하락 업종: -10~-20 페널티
                if avg_pct >= 1.0:
                    bonus = min(avg_pct * 10, 20)
                    theme_info.score = min(theme_info.score + bonus, 100)
                    adjusted_cnt += 1
                elif avg_pct <= -1.0:
                    penalty = min(abs(avg_pct) * 10, 20)
                    theme_info.score = max(theme_info.score - penalty, 0)
                    adjusted_cnt += 1

            if adjusted_cnt:
                logger.info(f"[ThemeDetector] 업종지수 기반 테마 점수 보정: {adjusted_cnt}개 테마")

        except Exception as e:
            logger.warning(f"[ThemeDetector] 업종지수 보정 오류 (무시): {e}")

    async def _adjust_scores_by_us_market(self):
        """US 시장 오버나이트 데이터 기반 테마 점수 보정"""
        umd = self._us_market_data
        if not umd:
            try:
                from ...data.providers.us_market_data import get_us_market_data
                umd = get_us_market_data()
            except Exception:
                return

        try:
            sector_signals = await umd.get_sector_signals()
            if not sector_signals:
                return

            adjusted_cnt = 0
            for theme_name, theme_info in self._themes.items():
                signal = sector_signals.get(theme_name)
                if not signal:
                    continue

                boost = signal["boost"]
                if boost == 0:
                    continue

                old_score = theme_info.score
                theme_info.score = max(0, min(theme_info.score + boost, 100))
                adjusted_cnt += 1

                if abs(boost) >= 15:
                    movers = ", ".join(signal.get("top_movers", []))
                    logger.info(
                        f"[ThemeDetector] US 오버나이트 부스트: "
                        f"{theme_name} {old_score:.0f}→{theme_info.score:.0f} "
                        f"(boost={boost:+d}, US avg={signal['us_avg_pct']:+.1f}%, "
                        f"top: {movers})"
                    )

            if adjusted_cnt:
                logger.info(
                    f"[ThemeDetector] US 오버나이트 기반 테마 점수 보정: {adjusted_cnt}개 테마"
                )

        except Exception as e:
            logger.warning(f"[ThemeDetector] US 오버나이트 보정 오류 (무시): {e}")

    @staticmethod
    def _estimate_article_sentiment(title: str) -> Optional[float]:
        """키워드 기반 간이 센티멘트 추정 (LLM 미사용)"""
        if not title:
            return None

        bullish_keywords = [
            "급등", "상승", "호재", "수혜", "최고", "신고가", "돌파", "강세",
            "매출증가", "실적개선", "호실적", "수주", "계약", "상한가",
            "외국인매수", "기관매수", "순매수",
        ]
        bearish_keywords = [
            "급락", "하락", "악재", "폭락", "최저", "신저가", "약세",
            "적자", "실적악화", "하한가", "외국인매도", "기관매도",
            "순매도", "리콜", "부실", "소송",
        ]

        score = 0.0
        title_lower = title.lower()
        for kw in bullish_keywords:
            if kw in title_lower:
                score += 0.3
        for kw in bearish_keywords:
            if kw in title_lower:
                score -= 0.3

        # 범위 제한 -1.0 ~ 1.0
        return max(min(score, 1.0), -1.0) if score != 0 else None

    # ============================================================
    # DB 조회 메서드 (히스토리/통계)
    # ============================================================

    async def get_news_history(self, hours: int = 24, limit: int = 100) -> List[Dict]:
        """최근 뉴스 히스토리 조회"""
        await self._ensure_storage()
        if not self._storage:
            return []

        articles = await self._storage.get_recent_news(hours=hours, limit=limit)
        return [
            {
                "title": a.title,
                "content": a.content,
                "url": a.url,
                "source": a.source,
                "collected_at": a.collected_at.isoformat() if a.collected_at else None,
            }
            for a in articles
        ]

    async def get_theme_history(self, theme_name: str, days: int = 7) -> List[Dict]:
        """테마 히스토리 조회"""
        await self._ensure_storage()
        if not self._storage:
            return []

        records = await self._storage.get_theme_history(theme_name, days=days)
        return [
            {
                "theme_name": r.theme_name,
                "score": r.score,
                "news_count": r.news_count,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            }
            for r in records
        ]

    async def get_theme_trend(self, theme_name: str, days: int = 7) -> List[Dict]:
        """테마 트렌드 (일별 점수 변화)"""
        await self._ensure_storage()
        if not self._storage:
            return []

        return await self._storage.get_theme_trend(theme_name, days=days)

    async def get_hot_themes_from_db(
        self,
        hours: int = 24,
        min_score: float = 70
    ) -> List[Dict]:
        """DB에서 핫 테마 조회 (평균 점수 기준)"""
        await self._ensure_storage()
        if not self._storage:
            return []

        return await self._storage.get_hot_themes(hours=hours, min_score=min_score)

    async def get_storage_stats(self) -> Dict:
        """저장소 통계 조회"""
        await self._ensure_storage()
        if not self._storage:
            return {"status": "disconnected"}

        stats = await self._storage.get_stats()
        stats["status"] = "connected"
        return stats


# 전역 인스턴스
_theme_detector: Optional[ThemeDetector] = None

def get_theme_detector() -> ThemeDetector:
    """전역 테마 탐지기 인스턴스"""
    global _theme_detector
    if _theme_detector is None:
        _theme_detector = ThemeDetector()
    return _theme_detector
