# AI Trader v2 모바일 앱 구현 계획

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 기존 웹 대시보드를 모바일 최적화된 Android APK로 재구성한다.

**Architecture:** Expo 54 + React Native + TypeScript. TradingDataProvider (Context+useReducer)가 SSE/폴링으로 서버 데이터를 관리하고, 5개 하단 탭이 이를 소비한다. 기존 서버 REST API 100% 재활용.

**Tech Stack:** Expo 54, React Native, TypeScript strict, NativeWind 4, Expo Router, react-native-svg, axios, expo-notifications

**Design Doc:** `docs/plans/2026-02-15-mobile-app-design.md`

**Project Path:** `/home/user/projects/ai-trader-mobile/`

**Prerequisites:** Node 20+ (`nvm use 20`), pnpm

---

## 이전 시도에서 배운 교훈 (반드시 준수)

1. **Node 20+ 필수** — Expo 54는 `Array.prototype.toReversed()` 사용. Node 18에서 실패.
2. **equity-history API 사용** — `equity-curve`는 청산된 거래만 반영. `equity-history`는 실제 스냅샷(미실현 포함).
3. **헬스체크 경로** — `/api/health-checks` (하이픈, 슬래시 아님)
4. **데모 데이터 분리** — 서버 연결 시 모든 DEMO 상수 사용 금지. `state.isDemo` 플래그 기반 분기.
5. **기본값은 실제 설정과 일치** — stopLoss 2.5%, takeProfit 2.5% (4.0%/10.0% 아님)
6. **initial_capital** — portfolio API의 `initial_capital`은 일일 리셋됨. 자산 차트에는 equity-history의 `total_equity` 사용.

---

## Task 1: 프로젝트 초기화 + 설정 파일

**Files:**
- Create: `package.json`, `app.config.ts`, `tsconfig.json`, `babel.config.js`, `metro.config.js`, `tailwind.config.js`, `global.css`, `.gitignore`, `nativewind-env.d.ts`, `expo-env.d.ts`, `.npmrc`

**Step 1: Expo 프로젝트 생성**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.0/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
cd /home/user/projects
npx create-expo-app@latest ai-trader-mobile --template blank-typescript
cd ai-trader-mobile
```

**Step 2: 의존성 설치**

```bash
npx expo install expo-router expo-linking expo-constants expo-status-bar react-native-safe-area-context react-native-screens react-native-gesture-handler react-native-reanimated @expo/vector-icons expo-haptics expo-notifications expo-background-fetch expo-task-manager
npx expo install nativewind tailwindcss react-native-svg
npx expo install @react-native-async-storage/async-storage
pnpm add axios
pnpm add -D @types/react @types/react-native
```

**Step 3: 설정 파일 작성**

- `app.config.ts`: scheme `ai-trader`, bundleId `kr.ai.qwq.trader`, plugins (expo-router, nativewind), android adaptiveIcon
- `tsconfig.json`: strict, paths `@/*` → `./*`, jsx `react-jsx`
- `babel.config.js`: `babel-preset-expo` + `nativewind/babel`
- `metro.config.js`: `withNativeWind` wrapper
- `tailwind.config.js`: content paths, darkMode `class`, custom colors (bg-base `#0b0b14`, bg-surface `#12121e`, accent-* 등)
- `global.css`: `@tailwind base; @tailwind components; @tailwind utilities;` + CSS variables
- `.gitignore`: node_modules, .expo, dist, *.apk

**Step 4: 디렉토리 구조 생성**

```
mkdir -p app/(tabs) components/ui constants hooks lib shared assets/images
```

**Step 5: TypeScript 검증 + 커밋**

```bash
npx tsc --noEmit
git init && git add -A && git commit -m "chore: Expo 프로젝트 초기화 + NativeWind 설정"
```

---

## Task 2: 핵심 라이브러리 — API 클라이언트

**Files:**
- Create: `lib/api-client.ts`

서버 API 전체를 커버하는 타입 + 클라이언트 + SSE 클라이언트.

**타입 정의 (서버 응답 1:1 매칭):**

```typescript
// 포트폴리오, 상태, 포지션, 거래, 통계, 리스크
PortfolioData, StatusData, PositionData, TradeData, TradeStats, RiskData
// 테마, 이벤트, 설정
ThemeData, EventData, ConfigData
// 진화
EvolutionData, EvolutionHistoryItem
// 자산 히스토리 (equity-history API — 실제 스냅샷)
EquitySnapshot, EquityHistoryResponse
// 에퀴티 커브 (equity-curve API — closed trades only, 성과탭용)
EquityCurvePoint
// 일일 리뷰
DailyReviewData (trades + llm_analysis + strategy_stats)
// 스크리닝, 헬스체크, 대기주문, 외부계좌
ScreeningItem, HealthCheck, PendingOrder, ExternalAccount
```

**API 메서드 (모든 엔드포인트):**

| 메서드 | 경로 | 반환 |
|--------|------|------|
| `getStatus()` | `/api/status` | StatusData |
| `getPortfolio()` | `/api/portfolio` | PortfolioData |
| `getPositions()` | `/api/positions` | PositionData[] |
| `getRisk()` | `/api/risk` | RiskData |
| `getTodayTrades()` | `/api/trades/today` | TradeData[] |
| `getTrades(date)` | `/api/trades?date=` | TradeData[] |
| `getTradeStats(days)` | `/api/trades/stats?days=` | TradeStats |
| `getThemes()` | `/api/themes` | ThemeData[] |
| `getScreening()` | `/api/screening` | ScreeningItem[] |
| `getConfig()` | `/api/config` | ConfigData |
| `getEvolution()` | `/api/evolution` | EvolutionData |
| `getEvolutionHistory()` | `/api/evolution/history` | EvolutionHistoryItem[] |
| `applyEvolution(body)` | POST `/api/evolution/apply` | {success,message} |
| `getEquityCurve(days)` | `/api/equity-curve?days=` | EquityCurvePoint[] |
| `getEquityHistory(days)` | `/api/equity-history?days=` | EquityHistoryResponse |
| `getEquityHistoryRange(from,to)` | `/api/equity-history?from=&to=` | EquityHistoryResponse |
| `getEquityPositions(date)` | `/api/equity-history/positions?date=` | PositionSnapshot[] |
| `getDailyReview(date?)` | `/api/daily-review?date=` | DailyReviewData |
| `getDailyReviewDates()` | `/api/daily-review/dates` | {dates:string[]} |
| `getHealthChecks()` | `/api/health-checks` | HealthCheck[] |
| `getPendingOrders()` | `/api/orders/pending` | PendingOrder[] |
| `getOrderHistory()` | `/api/orders/history` | OrderEvent[] |
| `getExternalAccounts()` | `/api/accounts/positions` | ExternalAccount[] |
| `getEvents(sinceId)` | `/api/events?since=` | EventData[] |
| `testConnection()` | `/api/status` | {connected,latencyMs} |

**SSE 클라이언트:** EventEmitter 패턴, web=EventSource / native=폴링 3초.

**주의사항:**
- 모든 list 반환 메서드에 try/catch → 빈 배열 폴백
- baseUrl은 AsyncStorage에서 로드, 기본값 `https://qwq.ai.kr`
- 응답 캐시 없음 (SSE가 실시간 푸시)

**Step: 커밋**
```bash
git add lib/api-client.ts && git commit -m "feat: API 클라이언트 + 전체 타입 정의"
```

---

## Task 3: 핵심 라이브러리 — 상태 관리 + 데모 데이터

**Files:**
- Create: `lib/trading-data-provider.tsx`, `lib/demo-data.ts`, `lib/notifications.ts`, `lib/utils.ts`

### trading-data-provider.tsx

- `TradingState`: connected, connecting, isDemo, serverUrl, lastError, lastUpdated + 실시간 데이터 (portfolio, status, positions, risk, todayTrades, events, pendingOrders, externalAccounts)
- `TradingAction`: SET_SERVER_URL, SET_CONNECTING, SET_CONNECTED, SET_ERROR, UPDATE_* (각 데이터), SET_DEMO_MODE, RESET
- `isDemo` 관리: `SET_CONNECTED` 시 `isDemo = !connected`로 설정. 개별 UPDATE에서는 isDemo 변경 안 함.
- `connect()`: 5개 API 병렬 호출 (status, portfolio, positions, risk, todayTrades) → SSE 시작 → SET_CONNECTED
- `refresh()`: 동일 5개 재호출
- SSE 리스너: status, portfolio, positions, risk, events, pending_orders, external_accounts
- getDemoPortfolio/Status/Positions/Events 헬퍼 함수 export

### demo-data.ts

- 초기자본 10,000,000원 기준
- DEMO_PORTFOLIO, DEMO_STATUS, DEMO_POSITIONS (3~4개 종목)
- DEMO_TRADES (5~6건), DEMO_EVENTS (6개)
- DEMO_STRATEGIES (2개: Momentum, SEPA-Trend)
- DEMO_THEMES (4개), DEMO_RISK_SETTINGS
- 포맷 함수: formatKRW, formatPct, formatPrice, formatTime
- 타입 export: StrategyPerformance, Theme, RiskSettings, Trade 등

### notifications.ts

- `NotificationManager` 클래스
- 5개 채널: trade_fill, stop_loss, risk_warning, error, daily_summary
- `handleTradingEvent(event)` → 타입별 로컬 알림 발생
- `handlePortfolioAlert({dailyPnlPct, limitPct})` → 80% 초과 시 알림
- AsyncStorage로 알림 설정 on/off 저장

### utils.ts

- `cn()` — NativeWind className merge (clsx + twMerge)

**Step: 커밋**
```bash
git add lib/ && git commit -m "feat: 상태 관리 + 데모 데이터 + 알림 매니저"
```

---

## Task 4: 테마/훅/상수/컴포넌트

**Files:**
- Create: `constants/theme.ts`, `hooks/use-colors.ts`, `hooks/use-color-scheme.ts`, `hooks/use-color-scheme.web.ts`, `shared/types.ts`, `shared/const.ts`
- Create: `components/screen-container.tsx`, `components/equity-chart.tsx`, `components/haptic-tab.tsx`, `components/ui/icon-symbol.tsx`, `components/ui/icon-symbol.ios.tsx`
- Create: `lib/_core/theme.ts`, `lib/_core/nativewind-pressable.ts`, `lib/theme-provider.tsx`

### 테마 시스템

```typescript
// constants/theme.ts — 다크 테마 색상
colors = {
  background: "#0b0b14",
  surface: "#12121e",
  elevated: "#1a1a2e",
  border: "#ffffff12",
  foreground: "#e2e8f0",
  muted: "#94a3b8",
  primary: "#6366f1",
  success: "#34d399",
  error: "#f87171",
  warning: "#fbbf24",
}
```

### equity-chart.tsx

- react-native-svg 기반 라인차트
- Props: `data: {date, equity, pnl}[]`, width, height
- 자동 Y축 스케일링 (데이터 범위 기반)
- 영역 그라데이션 (수익=녹색, 손실=빨강)
- X축 날짜 라벨, Y축 금액 라벨

### screen-container.tsx

- SafeAreaView + NativeWind bg-background 래퍼

**Step: 커밋**
```bash
git add constants/ hooks/ shared/ components/ lib/_core/ lib/theme-provider.tsx
git commit -m "feat: 테마 시스템 + 공용 컴포넌트 + 훅"
```

---

## Task 5: 앱 레이아웃

**Files:**
- Create: `app/_layout.tsx`, `app/(tabs)/_layout.tsx`

### app/_layout.tsx

```
ThemeProvider → SafeAreaProvider → GestureHandlerRootView → TradingDataProvider → Stack
```

- `global.css` import
- Stack: (tabs) + position-detail 모달

### app/(tabs)/_layout.tsx

- 하단 탭 5개:
  1. index (실시간) — MaterialIcons `dashboard`
  2. trades (거래) — MaterialIcons `receipt-long`
  3. performance (성과) — MaterialIcons `trending-up`
  4. review (리뷰) — MaterialIcons `psychology`
  5. settings (설정) — MaterialIcons `settings`

- 탭 바 스타일: bg-surface, borderTop 없음, activeTintColor=primary

**Step: 커밋**
```bash
git add app/ && git commit -m "feat: 앱 레이아웃 + 하단 탭 네비게이션"
```

---

## Task 6: 실시간 탭

**Files:**
- Create: `app/(tabs)/index.tsx`

**섹션 컴포넌트 (파일 내 분리):**

1. `PortfolioHero` — 총자산 히어로, 일일손익, 현금/포지션 비율 바
   - 데이터: `state.portfolio` (SSE) or getDemoPortfolio()
2. `RiskGauge` — 3칸 수평: 일일손실게이지 | 포지션수 | 거래가능
   - 데이터: `state.risk` (SSE)
3. `PositionCards` — FlatList, 각 카드에 종목명/손익%/청산단계
   - 탭 → `router.push('/position-detail?symbol=...')`
   - 데이터: `state.positions` (SSE) or getDemoPositions()
4. `ExternalAccounts` — 접이식 (TouchableOpacity toggle)
   - 데이터: `state.externalAccounts` (SSE)
5. `PendingOrders` — 조건부 렌더링 (pendingOrders.length > 0)
   - 진행률 바 (경과시간/타임아웃)
6. `ThemeChips` — ScrollView horizontal, 칩 리스트
   - 데이터: REST `/api/themes` (Pull-to-Refresh)
7. `ScreeningTop` — 상위 5개 종목 컴팩트
   - 데이터: REST `/api/screening`
8. `EventFeed` — 최근 10개 이벤트, 아이콘+메시지+시간
   - 데이터: `state.events` (SSE)

**Step: 커밋**
```bash
git add app/(tabs)/index.tsx && git commit -m "feat: 실시간 탭 — 포트폴리오/리스크/포지션/테마/이벤트"
```

---

## Task 7: 거래 탭

**Files:**
- Create: `app/(tabs)/trades.tsx`

**섹션:**

1. `DateNavigator` — 좌우 화살표 + 날짜 표시 + 오늘 버튼
   - state: selectedDate (Date)
2. `DailySummary` — 4칸 그리드: 거래수|승률|총손익|평균수익률
   - 데이터: REST `/api/trades/stats?days=1` 또는 거래목록에서 집계
3. `ExitTypeBar` — 수평 스택 바 (손절/익절/트레일링 비율)
   - 거래 목록의 reason 필드에서 집계
4. `TradeList` — FlatList 카드형
   - 각 카드: 좌측 컬러바(매수=파랑/매도=빨강) + 종목명+전략뱃지 + 가격 + 손익
   - 탭 → 하단 모달 (진입/청산 사유 상세)
   - 데이터: REST `/api/trades?date=YYYY-MM-DD`

**Step: 커밋**
```bash
git add app/(tabs)/trades.tsx && git commit -m "feat: 거래 탭 — 날짜 네비게이션/통계/거래 목록"
```

---

## Task 8: 성과 탭

**Files:**
- Create: `app/(tabs)/performance.tsx`

**상단 서브 탭:** `성과 분석` | `자산 히스토리` (useState toggle)

### 성과 분석 뷰:
1. `PeriodChips` — 7일|30일|90일 선택
2. `StatsSummary` — 2x3 카드: 총거래|승률|총손익|PF|최대낙폭|평균보유
   - 데이터: REST `/api/trades/stats?days=N`
3. `EquityCurveChart` — equity-history API → SVG 라인차트
   - **equity-history 사용** (실제 스냅샷, 미실현 포함)
   - 오늘 스냅샷 없으면 portfolio.total_equity 추가
4. `StrategyCards` — 전략별 성과 카드
   - 데이터: `/api/trades/stats` → `by_strategy` 필드

### 자산 히스토리 뷰:
1. `PeriodSelector` — 프리셋(7/14/30일) 또는 from~to
2. `EquitySummary` — 4칸: 기간수익률|최대낙폭|평균일일손익|데이터일수
   - 데이터: equity-history.summary
3. `EquityAreaChart` — SVG 영역차트 (총자산 추이)
4. `DailyList` — FlatList: 날짜|총자산|변동|거래수
   - 탭 → 바텀시트 (해당일 포지션 상세, `/api/equity-history/positions?date=`)

**Step: 커밋**
```bash
git add app/(tabs)/performance.tsx && git commit -m "feat: 성과 탭 — 성과분석/자산히스토리 서브뷰"
```

---

## Task 9: 리뷰 탭

**Files:**
- Create: `app/(tabs)/review.tsx`

**상단 서브 탭:** `일일 리뷰` | `진화 이력` (useState toggle)

### 일일 리뷰 뷰:
1. `ReviewDateNav` — 좌우 화살표, 리뷰 가능 날짜만 이동
   - 데이터: REST `/api/daily-review/dates` → dates[]
2. `ReviewSummary` — 4칸: 승률|손익비|총손익|LLM 평가
3. `TradeReviewCards` — 각 거래 복기
   - 종목+전략+손익, 접이식 LLM 분석 (진입논리, 청산타이밍)
   - 데이터: `/api/daily-review?date=` → trades[]
4. `LLMInsights` — LLM 종합 평가 카드
   - 인사이트 목록, 회피패턴/집중기회 2칸
5. `ParameterRecommendations` — 파라미터 추천 카드
   - 전략+파라미터+현재→제안+신뢰도
   - "적용" 버튼 → Alert 확인 → POST `/api/evolution/apply`

### 진화 이력 뷰:
1. `EvolutionSummary` — 총진화|성공|롤백|최근일
   - 데이터: REST `/api/evolution`
2. `ChangeHistoryList` — 변경 이력 FlatList
   - 날짜+전략+파라미터+AS-IS→TO-BE+효과뱃지
   - 접이식: 사유 상세
   - 데이터: REST `/api/evolution/history`

**Step: 커밋**
```bash
git add app/(tabs)/review.tsx && git commit -m "feat: 리뷰 탭 — 일일리뷰/진화이력"
```

---

## Task 10: 설정 탭

**Files:**
- Create: `app/(tabs)/settings.tsx`

**섹션:**

1. `ServerConnection` — URL 입력 + 테스트 버튼 + 연결/해제 + 상태(dot) + 레이턴시
2. `NotificationSettings` — 5개 토글 (체결|손절|리스크|에러|일일요약)
3. `CurrentConfig` — 서버 `/api/config`에서 읽기 전용 표시
   - 트레이딩: 초기자본, 시장, 수수료
   - 리스크: 일일최대손실, 포지션비율, 손절/익절
   - 분할익절: 1차/2차 설정
   - 전략별: 활성화+주요 파라미터
4. `DemoModeToggle` — 데모 모드 수동 전환
5. `AppInfo` — 버전, 빌드일자

**Step: 커밋**
```bash
git add app/(tabs)/settings.tsx && git commit -m "feat: 설정 탭 — 서버연결/알림/설정/데모모드"
```

---

## Task 11: 포지션 상세 모달

**Files:**
- Create: `app/position-detail.tsx`

- Expo Router 모달 (Stack.Screen presentation="modal")
- URL params: symbol, name 전달
- SVG 가격 레벨 차트 (평균가/현재가/손절/목표/트레일링)
- 청산 진행 상태 (exit_state 기반)
- 리스크 config에서 기본값 로드 (default_stop_loss_pct=2.5, default_take_profit_pct=2.5)

**Step: 커밋**
```bash
git add app/position-detail.tsx && git commit -m "feat: 포지션 상세 모달 — 가격 레벨 차트"
```

---

## Task 12: 빌드 검증 + 에셋

**Files:**
- Create: `assets/images/icon.png`, `assets/images/splash.png`, `assets/images/adaptive-icon.png`

**Step 1: TypeScript 전체 검증**
```bash
npx tsc --noEmit
```
Expected: 에러 0건

**Step 2: Expo 웹 프리뷰 시작**
```bash
npx expo start --web --port 8081
```
Expected: HTTP 200 + 5개 탭 렌더링

**Step 3: 서버 연결 테스트**
- 설정 탭에서 URL 입력 → 연결 → 실시간 데이터 수신 확인

**Step 4: 커밋**
```bash
git add -A && git commit -m "chore: 에셋 + 빌드 검증 완료"
```

---

## Task 13: EAS Build 설정 (APK)

**Files:**
- Create: `eas.json`

```json
{
  "build": {
    "preview": {
      "android": {
        "buildType": "apk"
      }
    },
    "production": {
      "android": {
        "buildType": "app-bundle"
      }
    }
  }
}
```

**Step:**
```bash
npx eas build --platform android --profile preview
```

**Step: 커밋**
```bash
git add eas.json && git commit -m "chore: EAS Build 설정 — Android APK"
```

---

## 실행 순서 요약

| Task | 내용 | 의존성 | 예상 파일 수 |
|------|------|--------|-------------|
| 1 | 프로젝트 초기화 + 설정 | 없음 | ~12 |
| 2 | API 클라이언트 | Task 1 | 1 |
| 3 | 상태관리 + 데모 + 알림 | Task 2 | 4 |
| 4 | 테마/훅/컴포넌트 | Task 1 | ~12 |
| 5 | 앱 레이아웃 | Task 3,4 | 2 |
| 6 | 실시간 탭 | Task 5 | 1 |
| 7 | 거래 탭 | Task 5 | 1 |
| 8 | 성과 탭 | Task 5 | 1 |
| 9 | 리뷰 탭 | Task 5 | 1 |
| 10 | 설정 탭 | Task 5 | 1 |
| 11 | 포지션 상세 | Task 5 | 1 |
| 12 | 빌드 검증 | Task 6~11 | 3 |
| 13 | EAS Build | Task 12 | 1 |

**병렬 가능:** Task 2+4 (독립), Task 6~11 (독립, Task 5 완료 후)

**총 파일 수:** ~41개
