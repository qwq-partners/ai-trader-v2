# AI Trader v2 모바일 앱 디자인

> 승인일: 2026-02-15

## 개요

기존 웹 대시보드(https://qwq.ai.kr/)를 모바일 최적화된 Android APK로 재구성한다.
서버 API는 기존 aiohttp REST + SSE를 100% 재활용하며, 신규 API 없이 구현한다.

## 기술 스택

| 항목 | 선택 |
|------|------|
| 프레임워크 | Expo 54 + React Native |
| 언어 | TypeScript strict |
| 스타일링 | NativeWind 4 (Tailwind for RN) |
| 라우팅 | Expo Router (Bottom Tabs 5개) |
| 상태관리 | React Context + useReducer |
| 차트 | react-native-svg 직접 구현 |
| HTTP | axios |
| 실시간 | SSE (web) / 폴링 3초 (native) |
| 알림 | expo-notifications (로컬) |
| 스토리지 | @react-native-async-storage |
| 빌드 | eas build → APK |

## 탭 구조

하단 탭 바 5개:

| 탭 | 아이콘 | 웹 대응 |
|----|--------|---------|
| 실시간 | house.fill | 실시간 + 테마 통합 |
| 거래 | list.bullet | 거래 |
| 성과 | chart.line.uptrend | 성과 + 자산 통합 |
| 리뷰 | brain | 진화 |
| 설정 | gearshape | 설정 |

웹 7탭 → 모바일 5탭 축소:
- "자산" → 성과 탭 내 서브뷰 (에퀴티 커브와 자산 히스토리는 같은 데이터의 다른 뷰)
- "테마" → 실시간 탭 하단 섹션 (스크리닝과 함께 시장 상황 한눈에)

## 화면 설계

### 1. 실시간 탭

스크롤 순서:
1. **포트폴리오 히어로** — 총자산(큰 폰트), 일일손익(색상+화살표), 현금/포지션 비율 바
2. **리스크 게이지** — 수평 3칸: 일일손실% | 포지션수 | 거래가능 상태
3. **보유 포지션 카드** — 각 종목별 카드(종목명, 손익%, 청산단계 뱃지, 탭→상세 모달)
4. **외부 계좌** — 접이식 섹션(계좌별 요약 + 탭→포지션 목록)
5. **대기 주문** — 조건부 표시(주문 있을 때만), 진행률 바
6. **활성 테마** — 가로 스크롤 칩(테마명+점수), 탭→관련종목
7. **스크리닝 상위** — 상위 5개 종목 컴팩트 리스트(종목, 점수, 등락률)
8. **이벤트 피드** — 최근 10개, 아이콘+메시지+시간

데이터 소스: SSE 실시간 (portfolio, positions, risk, events, pending_orders, external_accounts) + REST (themes, screening)

### 2. 거래 탭

스크롤 순서:
1. **날짜 선택 바** — 좌우 화살표 + 날짜 + "오늘" 버튼
2. **일일 요약 카드** — 가로 4칸: 거래수 | 승률 | 총손익 | 평균수익률
3. **청산유형 분포** — 수평 스택 바 (손절/1차익절/트레일링/시장가 비율)
4. **거래 목록** — FlatList 카드형
   - 종목명 + 전략뱃지 | 진입가→청산가 | 손익(색상) | 보유시간
   - 매수(파랑)/매도(빨강) 좌측 컬러바
   - 탭 → 상세 모달(진입사유, 청산사유)

데이터 소스: REST `/api/trades?date=` + `/api/trades/stats`

### 3. 성과 탭

상단 서브 탭: `성과 분석` | `자산 히스토리`

**성과 분석:**
1. 기간 선택 칩: 1주 | 1개월 | 3개월
2. 요약 카드 2x3: 총거래 | 승률 | 총손익 | PF | 최대낙폭 | 평균보유
3. 에퀴티 커브 — SVG 라인차트 (equity-history API, 실제 자산)
4. 전략별 성과 카드: 전략명 | 거래수 | 승률바 | 총손익

**자산 히스토리:**
1. 기간 선택: from~to 또는 프리셋(7/14/30일)
2. 요약 카드: 기간수익률 | 최대낙폭 | 평균일일손익 | 데이터일수
3. 자산 추이 차트 — SVG 영역차트
4. 일자별 리스트: 날짜 | 총자산 | 변동(색상) | 거래수 → 탭→해당일 포지션(바텀시트)

데이터 소스: REST `/api/equity-history`, `/api/equity-curve`, `/api/trades/stats`

### 4. 리뷰 탭

상단 서브 탭: `일일 리뷰` | `진화 이력`

**일일 리뷰:**
1. 날짜 네비게이션 — 좌우 화살표 (리뷰 가능 날짜만)
2. 일일 요약 4칸: 승률 | 손익비 | 총손익 | LLM 평가(A~F)
3. 거래별 복기 카드: 종목+전략+손익, 접이식 LLM 분석
4. LLM 종합 평가: 인사이트, 회피패턴/집중기회
5. 파라미터 추천: 전략+파라미터+현재→제안+신뢰도, "적용" 버튼 (POST)

**진화 이력:**
1. 요약 카드: 총진화 | 성공 | 롤백 | 최근일
2. 변경 이력 리스트: 날짜+전략+파라미터+AS-IS→TO-BE+효과뱃지, 접이식 사유

데이터 소스: REST `/api/daily-review`, `/api/evolution`, `/api/evolution/history`, POST `/api/evolution/apply`

### 5. 설정 탭

1. **서버 연결** — URL 입력 + 연결 테스트 + 상태/레이턴시
2. **알림 설정** — 토글: 체결 | 손절 | 에러 | 일일요약
3. **현재 설정** (읽기 전용) — 트레이딩/리스크/분할익절/전략별
4. **앱 정보** — 버전, 빌드일자
5. **데모 모드** — 토글

데이터 소스: REST `/api/config`, AsyncStorage

## 데이터 흐름

| 데이터 | 소스 | 갱신 |
|--------|------|------|
| 포트폴리오/리스크/포지션 | SSE | 2~10초 |
| 이벤트/대기주문 | SSE | 2초 |
| 외부 계좌 | SSE | 30초 |
| 거래/통계/자산 | REST | 온디맨드 |
| 테마/스크리닝 | REST | Pull-to-Refresh |
| 리뷰/진화 | REST | 온디맨드 |
| 설정 | REST | 온디맨드 |

## 푸시 알림

expo-notifications 로컬 알림 (FCM 서버 불필요):

| 채널 | 트리거 | 우선순위 |
|------|--------|----------|
| 체결 | events 타입 fill | 높음 |
| 손절/익절 | events fill + 매도 | 높음 |
| 리스크 경고 | daily_loss > 80% limit | 높음 |
| 에러 | events 타입 error | 중간 |
| 일일 요약 | 15:30 스케줄 | 낮음 |

## 디자인 원칙

- **다크 테마 기본** (웹과 동일 #0b0b14 계열)
- **테이블 → 카드** (모바일에서 가로 스크롤 테이블 지양)
- **Pull-to-Refresh** 모든 탭
- **서버 미연결 시 데모 폴백** (isDemo 플래그 기반)
- **데모 뱃지** 모든 데모 데이터 섹션에 일관 표시

## 서버 API

기존 엔드포인트 100% 활용. 신규 API 불필요.

주요 엔드포인트:
- `/api/stream` (SSE), `/api/status`, `/api/portfolio`, `/api/positions`, `/api/risk`
- `/api/trades/today`, `/api/trades?date=`, `/api/trades/stats?days=`
- `/api/equity-history?days=`, `/api/equity-history/positions?date=`
- `/api/themes`, `/api/screening`, `/api/events?since=`
- `/api/daily-review?date=`, `/api/daily-review/dates`
- `/api/evolution`, `/api/evolution/history`, POST `/api/evolution/apply`
- `/api/config`, `/api/health-checks`, `/api/accounts/positions`
- `/api/pending-orders`, `/api/order-history`

## 프로젝트 경로

`/home/user/projects/ai-trader-mobile/`
