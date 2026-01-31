# AI Trading Bot v2 - CLAUDE.md

## 언어 설정
- 모든 대화는 반드시 한국어(한글)로 진행할 것

## 프로젝트 개요
- 한국 주식(KRX) 자동 매매 봇
- 목표: 일별 1%+ 수익률, 현금 거래(레버리지 없음), 자본 1천만원 미만
- 비동기(asyncio) 이벤트 기반 아키텍처
- LLM 기반 자가 진화 시스템 탑재

## 프로젝트 경로
- 소스: `/home/user/projects/ai-trader-v2`
- 설정: `config/default.yml`
- 환경변수: `.env`
- 로그: `logs/YYYYMMDD/`
- 캐시/상태: `~/.cache/ai_trader/`
  - `journal/` - 거래 기록 JSON
  - `evolution/` - 진화 상태, 조언 기록

## 실행 방법
```bash
# 가상환경
source venv/bin/activate

# 실행
python scripts/run_trader.py              # 실거래
python scripts/run_trader.py --dry-run    # 테스트
python scripts/run_trader.py --log-level DEBUG
```

## 디렉토리 구조
```
src/
├── core/
│   ├── engine.py              # 이벤트 기반 트레이딩 엔진 (우선순위 힙 큐)
│   ├── event.py               # 이벤트 타입 정의 (MARKET_DATA, SIGNAL, ORDER, FILL 등)
│   ├── types.py               # 도메인 타입 (Symbol, Price, Order, Position, Portfolio, Signal 등)
│   └── evolution/             # 자가 진화 시스템
│       ├── trade_journal.py   # 거래 기록 저장소 (JSON)
│       ├── trade_reviewer.py  # 거래 복기 분석 (패턴 인식, 승률/손익비 계산)
│       ├── llm_strategist.py  # LLM에 분석 요청 → 전략 조언(StrategyAdvice) 생성
│       └── strategy_evolver.py # 조언 → 파라미터 자동 적용, 효과 추적, 롤백
├── strategies/
│   ├── base.py                # BaseStrategy 추상 클래스 (generate_signal, calculate_score)
│   ├── momentum.py            # 20일 고가 돌파 + 거래량 급증
│   ├── theme_chasing.py       # 핫 테마 종목 추종
│   ├── gap_and_go.py          # 갭상승 후 눌림목 매수
│   ├── mean_reversion.py      # RSI 과매도 반등
│   └── exit_manager.py        # 분할 익절 (3%→50%, 5%→25%, 나머지 트레일링)
├── execution/broker/
│   ├── base.py                # 브로커 추상 클래스
│   └── kis_broker.py          # 한국투자증권(KIS) API 통합
├── data/feeds/
│   └── kis_websocket.py       # KIS WebSocket 실시간 데이터 (롤링 구독, 최대 100종목)
├── signals/
│   ├── screener/              # 종목 스크리너 (거래량 급증, 등락률 상위, 신고가)
│   ├── sentiment/             # 테마 탐지기 (네이버 뉴스 + LLM 분석)
│   └── technical/             # 기술적 분석
├── risk/
│   └── manager.py             # 포지션 크기, 손절/익절, 일일 한도(-5%), 최대 포지션(10)
├── analytics/
│   └── daily_report.py        # 일일 레포트 (08:00 추천, 17:00 결과)
├── dashboard/
│   ├── server.py              # aiohttp 웹서버 (포트 8080)
│   ├── api.py                 # REST API
│   └── sse.py                 # Server-Sent Events 실시간 스트림
└── utils/
    ├── config.py              # YAML 설정 로더
    ├── logger.py              # Loguru 로깅 (일별 로테이션, 30일 보관)
    ├── llm.py                 # LLM 통합 (OpenAI + Gemini, 용도별 자동 선택)
    ├── telegram.py            # 텔레그램 알림
    ├── fee_calculator.py      # 수수료 계산 (매수 0.015%, 매도 0.015%+세금 0.30%)
    └── kis_token_manager.py   # KIS API 토큰 자동 갱신
```

## 핵심 아키텍처

### 이벤트 루프 (run_trader.py)
```
engine.run()           → 이벤트 메인 루프
ws_feed.run()          → WebSocket 실시간 데이터
theme_detector (15분)  → 테마 탐지
screener (10분)        → 종목 스크리닝
check_fills (5초)      → 체결 확인
sync_portfolio (5분)   → KIS 잔고 동기화
evolve (20:30)         → 자가 진화
report (08:00, 17:00)  → 일일 레포트
dashboard (8080)       → 웹 대시보드
```

### 거래 흐름
```
시장 데이터(WebSocket) → 전략 4개 병렬 신호 생성 → 점수 필터(>=60)
→ 리스크 검증(한도, 포지션 수) → 주문 제출(KIS API)
→ 체결 확인(5초) → 분할 익절/손절 모니터링(실시간)
→ 거래 기록(journal) → 20:30 LLM 복기 → 파라미터 자동 조정
```

### LLM 모델 선택
| 작업 | Primary | Fallback |
|------|---------|----------|
| 테마 탐지, 종목 매핑, 뉴스 요약, 빠른 분류 | Gemini Flash Lite (light) | OpenAI gpt-5-mini |
| 시장 분석, 거래 복기, 전략 분석/진화 | OpenAI gpt-5.2 (heavy) | Gemini 2.5 Pro |

- Thinking 모델(gpt-5.2): `max_completion_tokens` 사용, `temperature` 미지원
- 타임아웃: 120초, max_output_tokens: 16000, 일일 예산: $5

### 진화 시스템
- 매일 20:30 자동 실행
- `TradeReviewer` → `LLMStrategist` → `StrategyEvolver`
- 신뢰도 >= 0.6인 파라미터만 자동 적용
- 7일 후 효과 평가, 승률 -5% 이하면 자동 롤백

## 코딩 규칙

### 패턴
- **비동기**: 모든 I/O는 `async/await` (aiohttp, asyncio)
- **데이터클래스**: 도메인 모델은 `@dataclass`로 정의
- **싱글톤**: 주요 컴포넌트는 `get_xxx()` 팩토리 함수로 싱글톤 관리
- **열거형**: 상수는 `str, Enum` 또는 `Enum` 사용
- **설정 로드**: `@classmethod from_env()` 패턴
- **정밀 계산**: 금액/가격은 `Decimal` 사용 (`types.py`)

### 컨벤션
- 한국어 주석, 한국어 로그 메시지 사용
- 로그 태그: `[진화]`, `[LLM 전략가]`, `[리스크]` 등
- 타입 힌트 필수 (`Dict`, `List`, `Optional`, `Tuple`)
- 이벤트 핸들러는 `on_xxx` 네이밍

### 주의사항
- `.env`에 API 키 저장 (커밋 금지)
- KIS API 토큰은 `~/.cache/ai_trader/`에 캐시
- 거래 관련 변경은 `--dry-run`으로 먼저 테스트
- LLM 비용 일일 $5 예산 초과 시 요청 거부됨

## 환경변수 (.env)
```
KIS_APPKEY, KIS_APPSECRET, KIS_CANO, KIS_ENV (prod/dev)
OPENAI_API_KEY, GEMINI_API_KEY
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
INITIAL_CAPITAL (기본 500000)
```

## 의존성
- Python 3.11+
- 핵심: aiohttp, websockets, loguru, pyyaml, pydantic
- 데이터: pandas, numpy
- LLM: openai, google-generativeai (aiohttp 직접 호출)
- 스크래핑: beautifulsoup4, requests
- 알림: python-telegram-bot
