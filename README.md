# AI Trading Bot v2

AI 기반 자동 트레이딩 봇 시스템

## 프로젝트 개요

- **목표**: 일별 1% 이상 수익률
- **시장**: 한국 주식 시장 (KRX)
- **자본 규모**: 1천만원 미만
- **거래 방식**: 현금 거래 (레버리지 없음)

## 주요 기능

### 전략 시스템
- **모멘텀 브레이크아웃**: 20일 고가 돌파 시 매수
- **테마 추종**: 핫 테마 관련 종목 추적
- **갭상승 추종**: 갭 상승 후 눌림목 매수
- **평균 회귀**: 과매도 종목 반등 매매

### 데이터 소스
- **KIS API**: 실시간 시세, 업종지수, 등락률 순위
- **네이버 금융**: 뉴스 크롤링, 종목 시세
- **Yahoo Finance**: US 시장 오버나이트 데이터 (지수, ETF, 개별주)

### 시그널 시스템
- **뉴스 테마 탐지**: LLM 기반 뉴스 분석 → 테마 + 종목 임팩트 추출
- **업종지수 보정**: KIS 업종지수 등락률 → 테마 점수 동적 보정
- **US 오버나이트 시그널**: US 시장 마감 데이터 → 한국 테마 선행 부스트
- **뉴스 임팩트 스코어링**: 종목별 뉴스 센티멘트 점수화

### US 오버나이트 시그널
미국 시장 마감 데이터(한국시간 06:00)를 활용하여 한국 장 개장 전에 테마 점수를 사전 부스트합니다.

**타임라인**:
```
06:00 KST - US 시장 마감 → Yahoo Finance 데이터 확정
08:00 KST - 아침 레포트에 US 섹션 포함 (Yahoo API 1회 호출 → 24h 캐시)
09:00 KST - ThemeDetector에서 캐시된 US 데이터로 테마 부스트
```

**US → 한국 테마 매핑** (~40개 심볼 추적):

| US 지표 그룹 | 한국 테마 | 부스트 예시 |
|-------------|----------|-----------|
| SOX, SMH, NVDA, AMD, TSM 등 | AI/반도체 | +3% → +25점 |
| URNM, SMR, OKLO, CEG, VST | 원자력 | +3% → +20점 |
| TSLA, LIT, RIVN, LCID | 2차전지 | +3% → +15점 |
| XLV, IBB, XBI | 바이오 | +2.5% → +15점 |
| ICLN, TAN, ENPH, FSLR | 탄소중립 | +3% → +15점 |
| ITA, LMT, RTX, NOC, GD | 방산 | +3% → +15점 |

### 리스크 관리
- 일일 손실 한도: -5%
- 최대 포지션 수: 5개
- 개별 포지션 한도: 50%
- 자동 손절/익절 + 분할 익절
- 트레일링 스탑

### 일일 레포트
- **08:00 아침 레포트**: US 시장 마감 요약, 업종 동향, 핫 테마, 추천 종목 10선
- **17:00 결과 레포트**: 추천 종목 당일 성과, 실거래 결과

### 시간대별 운영
- 프리장 (08:00~08:50): US 오버나이트 시그널 + 뉴스 선반영
- 정규장 (09:00~15:30): 메인 전략 실행
- 넥스트장 (15:30~20:00): 테마 연장 플레이

### 자가 진화 엔진
- 매일 20:30 거래 결과 분석 → 전략 파라미터 자동 조정
- LLM 기반 전략 개선안 도출

## 설치

```bash
# 가상환경 생성
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일 편집하여 API 키 입력
```

## 사용법

### 계좌 잔고 확인
```bash
python scripts/check_balance.py
```

### 트레이딩 봇 실행
```bash
# 실제 거래
python scripts/run_trader.py

# Dry Run (거래 없이 테스트)
python scripts/run_trader.py --dry-run

# 디버그 모드
python scripts/run_trader.py --log-level DEBUG
```

## 프로젝트 구조

```
ai-trader-v2/
├── src/
│   ├── core/                    # 핵심 엔진
│   │   ├── engine.py            # 이벤트 기반 엔진
│   │   ├── event.py             # 이벤트 정의
│   │   ├── types.py             # 타입 정의
│   │   └── evolution/           # 자가 진화 엔진
│   │
│   ├── data/                    # 데이터 계층
│   │   ├── providers/           # 데이터 프로바이더
│   │   │   ├── kis_market_data.py   # KIS 시장 데이터 (업종, 등락률)
│   │   │   └── us_market_data.py    # US 시장 오버나이트 (Yahoo Finance)
│   │   └── feeds/               # 실시간 피드
│   │       └── kis_websocket.py
│   │
│   ├── signals/                 # 시그널 생성
│   │   ├── screener.py          # 종목 스크리너
│   │   └── sentiment/           # 센티멘트 분석
│   │       └── theme_detector.py    # 테마 탐지 + US 부스트
│   │
│   ├── strategies/              # 전략 모듈
│   │   ├── momentum.py          # 모멘텀 브레이크아웃
│   │   ├── theme_chasing.py     # 테마 추종
│   │   ├── gap_and_go.py        # 갭상승 추종
│   │   ├── mean_reversion.py    # 평균 회귀
│   │   └── exit_manager.py      # 분할 익절 관리
│   │
│   ├── execution/               # 주문 실행
│   │   └── broker/
│   │       └── kis_broker.py
│   │
│   ├── risk/                    # 리스크 관리
│   │   └── manager.py
│   │
│   ├── analytics/               # 분석/레포트
│   │   └── daily_report.py      # 일일 투자 레포트
│   │
│   ├── dashboard/               # 웹 대시보드
│   │   └── server.py
│   │
│   └── utils/                   # 유틸리티
│       ├── config.py
│       ├── logger.py
│       ├── llm.py               # LLM 매니저
│       └── telegram.py          # 텔레그램 알림
│
├── config/
│   └── default.yml              # 기본 설정
│
├── scripts/
│   ├── run_trader.py            # 메인 실행
│   └── check_balance.py         # 잔고 확인
│
└── logs/                        # 로그 파일
```

## 설정

### config/default.yml
```yaml
trading:
  initial_capital: 10000000

risk:
  daily_max_loss_pct: 5.0
  max_positions: 5
  default_stop_loss_pct: 2.0
  default_take_profit_pct: 3.0

# US 시장 오버나이트 시그널
us_market:
  enabled: true
  fetch_time: "07:30"
  cache_ttl_hours: 24

strategies:
  momentum_breakout:
    enabled: true
    volume_surge_ratio: 2.0
  theme_chasing:
    enabled: true
    min_theme_score: 70
  gap_and_go:
    enabled: true
  mean_reversion:
    enabled: true
```

### 환경변수 (.env)
```bash
KIS_APPKEY=your_key
KIS_APPSECRET=your_secret
KIS_CANO=12345678
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

## 개발 로드맵

### Phase 1: 핵심 인프라 ✅
- [x] 프로젝트 구조
- [x] 핵심 타입 정의
- [x] KIS API 브로커
- [x] 이벤트 기반 엔진

### Phase 2: 전략 구현 ✅
- [x] 모멘텀 브레이크아웃
- [x] 테마 추종
- [x] 갭상승 추종
- [x] 평균 회귀

### Phase 3: 실시간 데이터 ✅
- [x] WebSocket 피드
- [x] 실시간 시세
- [x] 업종지수 연동

### Phase 4: 고도화 ✅
- [x] 뉴스/테마 분석 (LLM 기반)
- [x] 종목 스크리너
- [x] 웹 대시보드
- [x] 텔레그램 레포트 (아침/오후)
- [x] 분할 익절 + 트레일링 스탑
- [x] 자가 진화 엔진
- [x] US 오버나이트 시그널 (Yahoo Finance → 한국 테마 부스트)
- [x] 뉴스 임팩트 스코어링

## 주의사항

⚠️ **투자 경고**
- 이 시스템은 교육 및 연구 목적으로 제작되었습니다.
- 실제 투자에 사용할 경우 원금 손실 위험이 있습니다.
- 소액으로 충분히 테스트한 후 사용하세요.

## 라이선스

Private - 개인 사용 목적
