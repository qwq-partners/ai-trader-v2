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
│   └── exit_manager.py        # 3단계 익절 (2%→25%, 4%→35%, 6%→20%) + ATR 동적 손절
├── indicators/
│   └── atr.py                 # ATR 계산 (변동성 기반 손절)
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

## 매매 전략 상세

### 공통 사항
- 모든 전략은 `BaseStrategy` 상속, `generate_signal()` + `calculate_score()` 구현
- 공통 지표: MA5/20/60, vol_ratio(시간보정), RSI(14일), VWAP(20봉), 변동성(20일), 고가근접도
- 최소 점수 60점, 최소 신뢰도 0.5, 최소 거래량 1.5배, 최소 주가 1,000원
- 지표 재계산: 캔들 변경 또는 30초 경과 시만 (캐시: 200봉/종목, 500종목)

### 1. 모멘텀 브레이크아웃 (`momentum_breakout`)
- **원리**: 20일 고가 돌파 + 거래량 급증 → 추세 진입
- **진입 조건**:
  - 시간: 09:05~15:20
  - 현재가 > 20일 고가 (+0.5% 이상, `min_breakout_pct`)
  - 거래량 >= 2.0배 (`volume_surge_ratio`)
- **점수(0~100)**: 가격 모멘텀(40) + 거래량(30) + 신고가 근접도(20) + 테마(10)
- **신호 강도**: >=85 VERY_STRONG, >=70 STRONG, >=60 NORMAL
- **청산**: 익절 +5%, 손절 -2%, 트레일링 -1.5%
- **주요 파라미터**: `breakout_period=20`, `min_breakout_pct=0.5`, `volume_surge_ratio=2.0`

### 2. 테마 추종 (`theme_chasing`)
- **원리**: 핫 테마 감지 → 관련 종목 중 모멘텀 종목 빠른 진입/청산
- **진입 조건**:
  - 시간: 09:05~15:00
  - 테마 점수 >= 70, 테마 발생 60분 이내
  - 등락률 +1%~+15%, 거래량 >= 1.5배
  - 테마당 최대 2회 진입
  - 뉴스 센티멘트(bearish 차단), 외국인/기관 수급(동시 순매수 +10점)
- **점수(0~100)**: 테마 점수(50) + 등락률(25) + 거래량(25) + 뉴스/수급 보너스(+20)
- **신호 강도**: 테마 >=90 VERY_STRONG, >=80 STRONG, 그 외 NORMAL
- **청산**: 익절 +3%, 손절 -1.5%, 트레일링 -1%, 테마 쿨다운(점수 70%이하) 즉시 청산
- **주요 파라미터**: `min_theme_score=70`, `max_theme_age_minutes=60`, `max_entries_per_theme=2`

### 3. 갭상승 추종 (`gap_and_go`)
- **원리**: 장 시작 갭상승 → 30분 고가 형성 후 눌림목 진입, VWAP 지지 확인
- **진입 조건**:
  - 시간: 09:30~11:00 (장 초반만)
  - 갭 상승률 +2%~+10%
  - 30분 대기 후 눌림목 1%~3% (깊이 3배 초과 시 갭 실패 간주)
  - 거래량 >= 2.0배, VWAP 지지(허용 오차 0.5%)
- **점수(0~100)**: 갭 크기(35) + 눌림목 깊이(35) + 거래량(30) + VWAP 보너스(+10)
- **신호 강도**: 갭 >=5% VERY_STRONG, >=3% STRONG, >=2% NORMAL
- **청산**: 익절 +4%, 손절 -1.5%, 트레일링 -1.5%, 갭 시작점 -1% 이탈 시 즉시 청산
- **손절가 결정**: max(고정 손절, 갭 시작점 -0.5%, VWAP -0.5%)
- **주요 파라미터**: `min_gap_pct=2.0`, `max_gap_pct=10.0`, `pullback_pct=1.0`, `entry_delay_minutes=30`

### 4. 평균 회귀 (`mean_reversion`)
- **원리**: 과매도 + 급락 후 반등 시 역추세 매수 (고위험, 포지션 축소)
- **진입 조건**:
  - 시간: 09:30~15:00
  - RSI <= 30, 3일 낙폭 <= -10%, 고점 대비 -30% 이내
  - 양봉 필수 (1일 변화량 > 0)
  - 거래량 >= 1.5배
- **점수(0~100)**: RSI 깊이(35) + 낙폭(25) + 거래량(20) + 반등 강도(20)
- **신호 강도**: RSI <20 STRONG, <25 NORMAL, <30 WEAK
- **청산**: 익절 +5%, 손절 -3%, 트레일링 -2%, RSI >70(과매수)+수익 시 즉시 청산
- **포지션 크기**: 50% 축소 (`position_size_multiplier=0.5`)
- **주요 파라미터**: `max_rsi=30`, `min_decline_pct=-10`, `max_drawdown_from_high=30`

### 청산 관리 (ExitManager - 3단계 익절 + ATR 동적 손절)
- **1차 익절**: 수익률 >= +2% → 25% 매도 (`first_exit_pct=2.0`, `first_exit_ratio=0.25`)
- **2차 익절**: 수익률 >= +4% → 35% 추가 매도 (`second_exit_pct=4.0`, `second_exit_ratio=0.47`)
- **3차 익절**: 수익률 >= +6% → 20% 추가 매도 (`third_exit_pct=6.0`, `third_exit_ratio=0.5`)
- **트레일링**: 나머지 20% → 고점 대비 -1.5% 이탈 시 청산 (`trailing_stop_pct=1.5`)
- **ATR 기반 동적 손절**:
  - 변동성 낮음(ATR 1%) → 2.5% 손절
  - 변동성 보통(ATR 2%) → 4.0% 손절
  - 변동성 높음(ATR 3%+) → 5.0% 손절 (상한)
  - 범위: 2.5% ~ 5.0% (`min_stop_pct=2.5`, `max_stop_pct=5.0`, `atr_multiplier=2.0`)
- **손익비(R:R)**: 2:1 이상 목표
- 수수료 포함 순손익 기준, 최소 익절 수량: `max(1, int(...))`

### 리스크 관리
- **일일 최대 손실**: -3% → 거래 중단 (5% → 3% 강화)
- **일일 최대 거래**: 15회
- **최대 동시 포지션**: 10개 (자산 규모 동적 조정, 2 → 10 확대)
- **기본 포지션 비율**: 10%, 최대 20% (15% → 10%, 25% → 20% 축소)
- **최소 현금 예비**: 5% (15% → 5% 완화)
- **최소 포지션 금액**: 50만원
- **백테스트 최적화 파라미터** (2024년 검증):
  - `min_breakout_pct`: 0.5% (1.0% → 0.5%)
  - `volume_surge_ratio`: 2.5x (3.0x → 2.5x)
  - 결과: +3.49% 수익률, 63.6% 승률 (기존 대비 235% 개선)
- 연속 손실 5회 시 거래 중단
- 분할 익절 최소 3주 보장 (3주 미만이면 매수 스킵)

### 전략 비교표
| 항목 | 모멘텀 | 테마 | 갭상승 | 평균회귀 |
|------|-------|------|-------|---------|
| 시장 상황 | 상승장 | 테마 확산 | 오프닝 | 급락장 |
| 시간대 | 09:05~15:20 | 09:05~15:00 | 09:30~11:00 | 09:30~15:00 |
| 포지션 크기 | 100% | 100% | 100% | 50% |
| 익절 | +5% | +3% | +4% | +5% |
| 손절 | -2% | -1.5% | -1.5% | -3% |
| 고유 청산 | 없음 | 테마 쿨다운 | 갭 시작점 이탈 | RSI 과매수 |

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
- 데이터: pandas, numpy, FinanceDataReader, pykrx
- LLM: openai, google-generativeai (aiohttp 직접 호출)
- 스크래핑: beautifulsoup4, requests
- 알림: python-telegram-bot

## 최신 업데이트 (2026-02-02)

### Phase 1 완료: 백테스트 최적화 + 3단계 익절 + ATR 기반 동적 손절

#### 1. 백테스트 시스템 구축
- `scripts/backtest_simple.py`: 2024년 전체 백테스트
- `scripts/optimize_params.py`: 파라미터 최적화
- **결과**: 0.5% breakout, 2.5x volume → +3.49% 수익률, 63.6% 승률 (235% 개선)

#### 2. 파라미터 최적화 적용
- `min_breakout_pct`: 1.0% → **0.5%**
- `volume_surge_ratio`: 3.0x → **2.5x**
- 백테스트 검증: 수익률 235% 개선, 승률 19.2%p 향상

#### 3. 3단계 익절 시스템 (Phase 1-3)
```
기존: 3% → 5% (2단계)
개선: 2% → 4% → 6% (3단계)

1차: +2% → 25% 매도 (빠른 수익 확보)
2차: +4% → 35% 추가 매도 (안정적 수익)
3차: +6% → 20% 추가 매도 (큰 수익 추구)
나머지: 20% → 트레일링 (수익 극대화)
```

#### 4. ATR 기반 동적 손절
- `src/indicators/atr.py` 추가
- 변동성 대응 손절: 2.5% ~ 5.0% 자동 조정
- 변동성 낮음 → 좁은 손절, 높음 → 넓은 손절
- ATR 계산: 14일 True Range 평균

#### 5. 손익비(R:R) 개선
- 기존: 1.2:1 ~ 2:1 → 목표: **2:1 이상**
- 조기 익절(2%) + 트레일링 조합으로 손익비 최적화

#### 백테스트 성과 (2024년 전체)
| 지표 | 기존 | 최적화 | 개선율 |
|------|------|--------|--------|
| 수익률 | +1.04% | **+3.49%** | **+235%** |
| 승률 | 44.4% | **63.6%** | **+19.2%p** |
| 거래 횟수 | 9회 | 11회 | +22% |
| 손익비 | 1.92:1 | 1.59:1 | -17% |

## 트러블슈팅 가이드

### 1. 포트폴리오 동기화 이슈

#### 근본 원인: KIS API 응답 지연
- **문제**: 실제 청산 체결 후 몇 분간 API가 이전 상태(보유 중) 반환
- **증상**: 청산 완료 후에도 포트폴리오에 종목이 남아있는 "유령 포지션" 발생
- **예시**: 10:00~10:01 청산 체결 → 10:00:45 동기화 시 여전히 "보유 중"으로 응답

#### 해결 방법
1. **청산 실패 시 실제 보유 수량 재확인** (`run_trader.py` lines 753-771)
   ```python
   # 청산 실패 시 실제 보유 수량 재확인 (유령 포지션 제거)
   actual_positions = await self.broker.get_positions()
   if symbol not in actual_positions or actual_positions[symbol].quantity == 0:
       # 유령 포지션 제거
       del self.engine.portfolio.positions[symbol]
       self.exit_manager._states.pop(symbol, None)
   ```

2. **동기화 주기 단축** (`bot_schedulers.py`)
   - 기존: 5분(300초) → 개선: 2분(120초)
   - KIS API 응답 지연에 더 빠르게 대응

3. **엔진 일시정지 회피**
   - 유령 포지션 감지 시 엔진 일시정지 스킵
   - 실제 보유 종목만 재시도 대상으로 제한

#### 예방 체크리스트
- [ ] 청산 후 2~3분 이내 포트폴리오 동기화 확인
- [ ] 유령 포지션 발생 시 자동 제거 로직 정상 작동 확인
- [ ] 엔진 일시정지 무한 루프 방지 로직 확인
- [ ] DEBUG 로그로 API 응답 시간 모니터링

---

### 2. WebSocket 중복 프로세스 문제

#### 증상
- 에러: `"ALREADY IN USE appkey"` (msg_cd=OPSP8996)
- 원인: 여러 개의 `run_trader.py` 프로세스가 동시 실행되어 동일한 appkey로 WebSocket 연결 시도

#### 해결 방법
```bash
# 1. 모든 run_trader.py 프로세스 확인
ps aux | grep run_trader.py

# 2. 중복 프로세스 전체 종료
pkill -9 -f "run_trader.py"

# 3. 단일 인스턴스만 재시작
python scripts/run_trader.py
```

#### 예방
- 봇 재시작 전 항상 기존 프로세스 확인
- systemd나 supervisor 사용 시 중복 실행 방지 설정
- `.pid` 파일로 단일 인스턴스 보장 (TODO)

---

### 3. 전략 타입 안전성 이슈

#### 증상
- 에러: `"argument of type 'datetime.datetime' is not iterable"`
- 원인: 딕셔너리 속성이 datetime 객체로 덮어씌워져 `in` 연산자 사용 시 타입 에러 발생

#### 해결 방법 (`momentum.py` lines 127-140)
```python
# 방어적 타입 체크 추가
if not isinstance(self._last_signal_time, dict):
    self._last_signal_time = {}
if symbol in self._last_signal_time:
    elapsed = (datetime.now() - self._last_signal_time[symbol]).total_seconds()
    if elapsed < self._signal_cooldown:
        return None

if not isinstance(self._recently_stopped, dict):
    self._recently_stopped = {}
if symbol in self._recently_stopped:
    elapsed = (datetime.now() - self._recently_stopped[symbol]).total_seconds()
    if elapsed < self._stop_penalty:
        return None
```

#### 예방
- 클래스 속성 초기화 시 타입 명시 (`Dict[str, datetime]`)
- 속성 재할당 전 타입 검증
- 유닛 테스트로 타입 안전성 보장

---

### 4. DEBUG 로그 활성화

#### 언제 사용하나?
- 익절/손절 로직 디버깅
- 포트폴리오 동기화 이슈 추적
- 전략 신호 생성 과정 분석
- API 응답 지연 측정

#### 활성화 방법
```bash
# 실행 시 로그 레벨 지정
python scripts/run_trader.py --log-level DEBUG

# 또는 config/default.yml 수정
logging:
  level: DEBUG  # INFO → DEBUG
```

#### 주요 DEBUG 로그 포인트
- `[ExitManager]`: 익절/손절 조건 체크
- `[Engine]`: 전략 실행 및 신호 처리
- `[KIS Broker]`: API 요청/응답 상세
- `[Portfolio]`: 포지션 변경 추적
- `[청산 실패]`: 청산 재시도 과정

---

### 5. 일반적인 문제 해결

#### 매수가 실행되지 않을 때
1. **거래 가능 시간 확인**: 09:05~15:20 범위 내인가?
2. **최소 점수 확인**: 종목 점수 >= 60점?
3. **리스크 한도 확인**:
   - 일일 손실 -3% 초과 시 거래 중단
   - 최대 포지션 10개 도달
   - 현금 부족 (최소 5% 예비금)
4. **신호 쿨다운**: 5분 쿨다운 중인가? (momentum 전략)
5. **손절 페널티**: 30분 재진입 금지 기간인가? (momentum 전략)

#### 체결 확인 안 될 때
- `check_fills` 스케줄러 정상 동작 확인 (5초 주기)
- KIS API 접근성 확인 (`broker.get_orders()` 호출)
- 주문 ID 일치 여부 확인 (로그에서 `order_id` 추적)

#### 익절/손절이 작동하지 않을 때
1. **ExitManager 활성화 확인**: `config/default.yml`에서 `enabled: true`
2. **포지션 동기화**: 실제 보유 수량과 봇 포지션 일치 여부
3. **DEBUG 로그**: `[ExitManager]` 태그로 조건 체크 과정 확인
4. **ATR 계산**: 충분한 캔들 데이터 수집되었는가? (최소 14봉)

#### 로그 확인 명령어
```bash
# 실시간 로그 모니터링
tail -f logs/$(date +%Y%m%d)/trader_$(date +%Y%m%d).log

# 특정 패턴 검색
grep "ExitManager" logs/$(date +%Y%m%d)/trader_$(date +%Y%m%d).log
grep "청산" logs/$(date +%Y%m%d)/trader_$(date +%Y%m%d).log
grep "ERROR" logs/$(date +%Y%m%d)/trader_$(date +%Y%m%d).log
```

---

### 6. 긴급 상황 대응

#### 봇이 멈췄을 때
1. 로그 확인: `tail -100 logs/$(date +%Y%m%d)/trader_$(date +%Y%m%d).log`
2. 프로세스 상태: `ps aux | grep run_trader.py`
3. WebSocket 연결: 로그에서 "WebSocket 연결 성공" 확인
4. 재시작: `pkill -9 -f "run_trader.py" && python scripts/run_trader.py`

#### 손실이 급증할 때
- 수동으로 엔진 일시정지: 대시보드 또는 직접 프로세스 종료
- 포지션 수동 청산: KIS HTS 또는 MTS 사용
- 리스크 한도 재확인: `config/default.yml`의 `max_daily_loss_pct` 설정

#### API 오류가 반복될 때
- KIS 토큰 갱신: `~/.cache/ai_trader/kis_token_*.json` 삭제 후 재시작
- API 키 검증: `.env` 파일의 `KIS_APPKEY`, `KIS_APPSECRET` 확인
- 서비스 점검: KIS Open API 공지사항 확인

---

## 재발 방지 원칙

### 코드 수정 시
1. **방어적 프로그래밍**: 타입 체크, None 체크, 예외 처리
2. **DEBUG 로그 먼저**: 문제 재현 → 로그 분석 → 수정
3. **근본 원인 파악**: 증상이 아닌 원인 해결
4. **문서화**: CLAUDE.md 업데이트로 지식 축적

### 배포 전
1. **Dry-run 테스트**: `--dry-run` 모드로 충분히 검증
2. **로그 레벨 DEBUG**: 첫 실행 시 상세 로그로 모니터링
3. **단계적 배포**: 파라미터 변경 시 소량으로 시작

### 운영 중
1. **정기 모니터링**: 포트폴리오 동기화, API 응답 시간
2. **이상 징후 즉시 대응**: 유령 포지션, 중복 프로세스
3. **일일 복기**: 로그 검토, 진화 시스템 피드백 확인
