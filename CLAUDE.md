# AI Trading Bot v2 - CLAUDE.md
> 최종 업데이트: 2026-02-11 | 상세 변경 이력은 MEMORY.md 참조

## Project Structure
The main project directory is `/home/user/projects/ai-trader-v2/` (NOT `~/ai-trader/` or other paths). Always verify the working directory before reading or editing files.

## Language & Communication
- 모든 대화는 반드시 한국어(한글)로 진행할 것
- '커밋해줘' = commit AND push. '푸시' = push. 애매하면 commit + push 기본.
- 'new' 또는 'fresh'로 요청하면 이전 실패한 패턴 참조 금지.

## Git & GitHub
- Use SSH for git push (not HTTPS). PAT-based auth if SSH unavailable.
- Always commit and push together unless explicitly told otherwise.
- `gh auth login` interactive mode does NOT work in this environment.

## 검증 프로토콜 (절대 규칙)
코드 수정 후 반드시 아래 순서 수행:
1. `python -m py_compile <수정파일>` — 문법 검증
2. 기존 프로세스 종료: `ps aux | grep run_trader` → `kill <PID>`
3. 봇 재시작: `nohup python scripts/run_trader.py --config config/default.yml > /tmp/trader_restart.log 2>&1 &`
4. 로그 확인: `sleep 5 && grep "ERROR" /tmp/trader_restart.log`
5. 에러 없으면 완료 보고, 있으면 즉시 수정

## 코드 리뷰 프로토콜
사용자가 "리뷰해봐" 요청 시:
1. 변경된 모든 파일 재읽기 (캐시 의존 금지)
2. P0(치명적), P1(중요), P2(경미) 우선순위로 이슈 분류
3. 각 이슈: 파일명 + 라인번호 + 구체적 문제 + 수정방안
4. P0부터 수정 → py_compile → 재시작 → 로그 확인

---

## 프로젝트 개요
- 한국 주식(KRX) 자동 매매 봇
- 목표: 주당 5% 수익률, 현금 거래(레버리지 없음), 자본 1천만원 미만
- 비동기(asyncio) 이벤트 기반 아키텍처
- REST 폴링 모드 (WebSocket은 조건부 활성화)

## 프로젝트 경로
- 소스: `/home/user/projects/ai-trader-v2`
- 가상환경: `venv/` (.venv 아님)
- 설정: `config/default.yml` (base) + `config/evolved_overrides.yml` (override)
- 환경변수: `.env`
- 로그: `logs/YYYYMMDD/` 또는 `/tmp/trader_restart.log`
- 캐시/상태: `~/.cache/ai_trader/`
  - `journal/` - 거래 기록 JSON
  - `evolution/` - 진화 상태, 조언 기록

## 설정 주의사항
> **`evolved_overrides.yml`이 `default.yml` 위에 머지됨**
>
> 설정 변경 시 양쪽 모두 확인 필요. evolved_overrides가 default를 덮어쓰므로,
> default.yml만 바꿔도 evolved_overrides에 같은 키가 있으면 적용 안 됨.

## 실행 방법
```bash
source venv/bin/activate
python scripts/run_trader.py --config config/default.yml    # 실거래
python scripts/run_trader.py --dry-run                       # 테스트
python scripts/run_trader.py --log-level DEBUG               # 상세 로그
```

---

## 디렉토리 구조
```
src/
├── core/
│   ├── engine.py              # 이벤트 기반 트레이딩 엔진 (TradingEngine, StrategyManager, RiskManager)
│   ├── event.py               # 이벤트 타입 (MARKET_DATA, SIGNAL, ORDER, FILL 등)
│   ├── types.py               # 도메인 타입 (Position, Portfolio, Signal, Order 등)
│   ├── batch_analyzer.py      # 배치 분석 (15:40 스캔 → 09:01 실행)
│   └── evolution/
│       ├── trade_journal.py   # 거래 기록 저장소
│       ├── trade_reviewer.py  # 거래 복기 분석
│       ├── llm_strategist.py  # LLM 전략 조언 생성
│       ├── strategy_evolver.py # 파라미터 자동 조정, 효과 추적, 롤백
│       └── config_persistence.py # evolved_overrides.yml 영속화
├── strategies/
│   ├── base.py                # BaseStrategy 추상 클래스
│   ├── momentum.py            # 20일 고가 돌파 + 거래량 급증
│   ├── theme_chasing.py       # 핫 테마 종목 추종
│   ├── gap_and_go.py          # 갭상승 후 눌림목 매수
│   ├── sepa_trend.py          # SEPA 추세 전략 (스윙)
│   └── exit_manager.py        # 분할 익절 + ATR 동적 손절
├── indicators/
│   ├── atr.py                 # ATR 계산 (변동성 기반 손절)
│   └── technical.py           # 기술 지표 (MA, RSI, MRS 등)
├── execution/broker/
│   ├── base.py                # 브로커 추상 클래스
│   └── kis_broker.py          # 한국투자증권(KIS) API 통합
├── data/
│   ├── feeds/kis_websocket.py # KIS WebSocket (조건부 활성화)
│   └── storage/stock_master.py # 종목마스터 DB (3700+ 종목)
├── signals/
│   ├── screener/
│   │   ├── stock_screener.py  # 통합 스크리너 (거래량, 등락률, 신고가, 외국인)
│   │   └── swing_screener.py  # SEPA 후보 스캔
│   └── sentiment/
│       └── theme_detector.py  # 테마 탐지기 (네이버 뉴스 + LLM)
├── risk/
│   └── manager.py             # 리스크 관리 (일일 손실 한도, 현금 체크)
├── monitoring/
│   └── health_monitor.py      # 운영 모니터링 (8개 체크, 텔레그램 알림)
├── dashboard/
│   ├── server.py              # aiohttp 웹서버 (포트 8080)
│   ├── api.py                 # REST API 엔드포인트
│   ├── sse.py                 # Server-Sent Events 실시간 스트림
│   └── data_collector.py      # 대시보드 데이터 수집기
├── analytics/
│   └── daily_report.py        # 일일 레포트
└── utils/
    ├── config.py              # YAML 설정 로더
    ├── logger.py              # Loguru 로깅 (일별 로테이션)
    ├── llm.py                 # LLM 통합 (OpenAI + Gemini)
    ├── telegram.py            # 텔레그램 알림 (CRITICAL 3회 재시도)
    ├── fee_calculator.py      # 수수료 계산 (매수 0.014%, 매도 0.013%+세금 0.20%)
    └── kis_token_manager.py   # KIS API 토큰 자동 갱신

scripts/
├── run_trader.py              # 메인 실행 스크립트 (이벤트 핸들러, 라이프사이클)
├── bot_schedulers.py          # 스케줄러 (스크리닝, 체결확인, 동기화, 배치, 헬스모니터)
└── liquidate_all.py           # 긴급 전량 매도 (지정가→시장가 폴백)
```

---

## 핵심 아키텍처

### 실행 흐름 (REST 폴링 모드)
```
engine.run()               → 이벤트 메인 루프 (우선순위 힙 큐)
_run_screening (5분)        → 종목 스크리닝 → 자동 시그널 발행 (09:15~15:00)
_run_fill_check (2~5초)     → 적응형 체결 확인 (미체결 시 2초, 없으면 5초)
_run_rest_price_feed (45초) → WS 비활성 시 보유+스크리닝 상위 20종목 시세 폴링
_run_sync_portfolio (2분)   → KIS 잔고 동기화
_run_health_monitor         → 운영 모니터링 (15초/60초/5분 주기)
theme_detector (15분)       → 테마 탐지 (네이버 뉴스 + LLM)
batch: 15:40 daily_scan     → 전략별 일일 스캔 → pending_signals.json 저장
batch: 09:01 execute        → 전일 시그널 실행 (T+1)
evolve (20:30)              → 자가 진화 (복기 → 파라미터 조정)
dashboard (8080)            → 웹 대시보드 + 외부 계좌 뷰어
```

### 거래 흐름
```
스크리닝(5분) → 고점수 후보 필터(≥75) → REST API 실시간 가격 검증
→ Signal 생성 → SignalEvent → 엔진 리스크 체크(현금, 손실한도)
→ 주문 제출(KIS API, LIMIT) → 체결 확인(2~5초)
→ ExitManager 분할 익절/손절 모니터링
→ 매도: 매수1호가 지정가 → 90초 미체결 시 시장가 폴백
```

### 장중 스크리닝 자동진입 (`bot_schedulers.py:_run_screening()`)
- **트리거**: 5분 주기 스크리닝, `MarketSession.REGULAR`, 09:15~15:00
- **필터**: score ≥ 75, 기보유/pending 제외, 가용 현금 ≥ min_position_value
- **가격 검증**: REST API 실시간 시세 재조회
  - 등락률 1~15% (10:00 전 12%, 13:30 이후 10% — 시간대별 과열 방지)
  - 현재가 > 시가, 거래량 > 0
- **제한**: 쿨다운 30분/종목, 1회 최대 5개 시그널, 최대 8개 검증
- **엔진 안전장치가 최종 게이트**: 현금 부족/일일 손실 한도 시 자동 거부

---

## 매매 전략

### 공통 사항
- 모든 전략은 `BaseStrategy` 상속, `generate_signal()` + `calculate_score()` 구현
- 공통 지표: MA5/20/60, vol_ratio, RSI(14일), VWAP, 변동성, 고가근접도
- 최소 주가 1,000원, Decimal 정밀 계산

### 전략별 현재 파라미터 (evolved_overrides 반영)
| 전략 | 시작시간 | 최소 거래량 | 손절 | 최소 점수 |
|------|---------|-----------|------|---------|
| 모멘텀 | 09:15 | 2.5배 | 3.5% | 65 |
| 테마 | 09:10 | 2.3배 | 2.5% | - |
| 갭상승 | 09:30 | 3.0배 | 2.5% | - |
| SEPA | 배치 | - | 5.0% | 60 |

### 청산 관리 (ExitManager)
- **1차 익절**: +2.5% → 30% 매도
- **1R 본전 이동**: +2.5%(=손절폭) 도달 시 활성화, 수익 0% 이하 시 전량 청산
- **트레일링**: 고점 대비 ATR 기반 하락폭 감시 (-2.5% 기본)
- **ATR 동적 손절**: 범위 2~4% (ExitConfig 기본값, evolved_overrides에서 조정 가능)
- **손절**: 2.5% (evolved_overrides) / 전략별 override 가능
- **SEPA 전략별 익절**: 5/10/15% (run_trader.py에서 하드코딩)

### 매도 주문 처리
- **1단계**: 매수1호가(best bid) 지정가 주문
- **2단계**: 90초 미체결 시 기존 주문 취소 → 시장가 폴백
- **추적**: `_pending_sides`, `_pending_quantities` 양쪽 동기화
- **동시호가(15:20~15:30)**: LIMIT만 허용 (시장가 거부됨)

---

## 리스크 관리 (현재 적용값)

| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -2.0% | evolved_overrides (effective_daily_pnl 기준) |
| 일일 거래 횟수 | **제한 없음** | 2026-02-11 제거 |
| 최대 포지션 수 | **제한 없음** | 가용 현금이 유일한 게이트 |
| 연속 손실 중단 | **제한 없음** | 2026-02-11 제거 |
| 동일 섹터 최대 | 3개 | max_positions_per_sector (0=제한없음) |
| 기본 포지션 비율 | 25% (default) / 10% (override) | equity 대비 |
| 최대 포지션 비율 | 35% | 개별 포지션 상한 |
| 최소 현금 보유 | 5% | total_equity 대비 |
| 최소 포지션 금액 | 20만원 | 미달 시 매수 거부 |

### 수수료 (한국투자증권 BanKIS, 2026년~)
- 매수: 0.0141% (유관기관 제비용 포함)
- 매도: 0.0131% + 증권거래세 0.20% = 약 0.213%
- 왕복: 약 0.227%

---

## 대시보드 개발 패턴

새 기능 추가 시 아래 순서를 따름:
1. `data_collector.py` — 데이터 수집 메서드 추가
2. `api.py` — REST 엔드포인트 추가
3. `sse.py` — 실시간 이벤트 추가 (필요 시)
4. HTML 템플릿 — 카드/페이지 추가
5. JS — 렌더링 함수 + SSE 핸들러

**네비게이션**: 실시간 | 거래 | 성과 | 자산 | 테마 | 진화 | 설정
**차트**: `Plotly.react()` 사용 (메모리 릭 방지, `Plotly.newPlot` 금지)

### 외부 계좌 뷰어
- `.env`의 `KIS_EXT_ACCOUNTS` 환경변수로 계좌 설정 (형식: `이름:CANO:ACNT_PRDT_CD`)
- `GET /api/accounts/positions` + SSE `external_accounts` (30초 주기)
- 대시보드 실시간 탭에 계좌별 카드 표시 (요약 + 포지션 테이블)
- 30초 TTL 캐시 + asyncio.Lock (동시 API 호출 방지)
- **주의**: KIS API는 55번(DC 가입자계좌) 미지원, HTS ID 연결 실물계좌만 가능

---

## 운영 모니터링 (HealthMonitor)
| 체크 | 주기 | 조건 |
|------|------|------|
| 이벤트 루프 스톨 | 15초 (장중) | 처리 카운트 변화 없음 |
| WS 피드 단절 | 15초 (장중) | WS 모드 시 |
| 일일 손실 근접 | 15초 (장중) | 한도의 80% 도달 |
| Pending 교착 | 15초 (장중) | 5분 이상 해소 안 됨 |
| 메모리 사용량 | 60초 | > 500MB |
| 이벤트 큐 포화 | 60초 | > 80% |
| 브로커 연결 | 60초 | API 응답 실패 |
| 롤링 성과 | 5분 | 승률 < 20% 또는 연속 5패 |

---

## 코딩 규칙

### 패턴
- **비동기**: 모든 I/O는 `async/await` (aiohttp, asyncio)
- **데이터클래스**: 도메인 모델은 `@dataclass`
- **정밀 계산**: 금액/가격은 `Decimal` 사용
- **한국어**: 주석, 로그 메시지 모두 한국어
- **로그 태그**: `[리스크]`, `[스크리닝]`, `[진화]` 등

### 주의사항
- `.env`에 API 키 저장 (커밋 금지)
- KIS API 토큰은 `~/.cache/ai_trader/`에 캐시
- **Position.current_price 반드시 체결가로 초기화** — 미초기화 시 unrealized_pnl -100% → 일일손실 즉시 트리거
- **pending 상태 관리**: 예외 핸들러에서 반드시 `clear_pending()` 호출 (누수 방지)
- **시간 기반 dict**: 만료항목 자동 정리 로직 필수 (메모리 누수 방지)
- **파일 수정 시 연관 체크**: types.py ↔ engine.py, exit_manager.py ↔ run_trader.py, config.py ↔ YAML

## 환경변수 (.env)
```
KIS_APPKEY, KIS_APPSECRET, KIS_CANO, KIS_ENV (prod/dev)
KIS_EXT_ACCOUNTS (외부 계좌 조회, 형식: 이름:CANO:ACNT_PRDT_CD 쉼표 구분)
OPENAI_API_KEY, GEMINI_API_KEY
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
INITIAL_CAPITAL (기본 10000000)
```

## 의존성
- Python 3.11+
- 핵심: aiohttp, websockets, loguru, pyyaml, pydantic
- 데이터: pandas, numpy, FinanceDataReader, pykrx
- LLM: openai, google-generativeai
- 모니터링: psutil (venv에 별도 설치 필요)
- 알림: python-telegram-bot

---

## 트러블슈팅

### PID 파일 충돌로 시작 안 될 때
```bash
ps aux | grep run_trader | grep -v grep   # 실제 프로세스 확인
kill <PID>                                 # 기존 종료
rm -f *.pid /tmp/trader_*.pid             # PID 파일 정리
```

### 포트폴리오 동기화 이슈
- KIS API 응답 지연(수 분) → 유령 포지션 발생 가능
- 청산 실패 시 `broker.get_positions()`로 실제 보유 확인 후 정리
- 동기화 주기: 2분

### WebSocket 중복 프로세스
- "ALREADY IN USE appkey" → `pkill -9 -f "run_trader.py"` 후 단일 재시작

### 매수가 실행되지 않을 때
1. 가용 현금 확인 (`get_available_cash()`)
2. 일일 손실 한도(-2%) 도달 여부
3. 스크리닝 쿨다운(30분) 확인
4. 로그에서 `[스크리닝] 자동진입 체크` 항목 확인

### 긴급 전량 매도
```bash
cd /home/user/projects/ai-trader-v2
source venv/bin/activate
python scripts/liquidate_all.py   # 지정가 → 15초 대기 → 시장가 폴백
```

---

## LLM 모델 선택
| 작업 | Primary | Fallback |
|------|---------|----------|
| 테마 탐지, 뉴스 요약 | Gemini Flash Lite | OpenAI gpt-5-mini |
| 거래 복기, 전략 진화 | OpenAI gpt-5.2 | Gemini 2.5 Pro |

- Thinking 모델: `max_completion_tokens` 사용, `temperature` 미지원
- 타임아웃: 120초, 일일 예산: $5

## 진화 시스템
- 매일 20:30 자동 실행
- `TradeReviewer` → `LLMStrategist` → `StrategyEvolver`
- 신뢰도 >= 0.6인 파라미터만 자동 적용
- 7일 후 효과 평가, 승률 -5% 이하면 자동 롤백
- 결과는 `evolved_overrides.yml`에 영속화
