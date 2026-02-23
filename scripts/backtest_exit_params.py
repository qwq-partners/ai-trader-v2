#!/usr/bin/env python3
"""
B안 효과 분석 — 핵심 질문에 직접 답하기

일봉 OHLCV만으로 장중 경로를 정확히 재현할 수 없으므로,
실제 거래 결과 + 당일 고가/저가 데이터를 결합하여 분석.

분석 항목:
1. 5일 모멘텀 필터: 어떤 거래가 걸러지고, 순 효과는?
2. 남겨둔 수익: 실제 청산 후 당일/이후 얼마나 더 올랐는가?
3. 1차 익절 확대 효과: 2.5%→4.0%로 올리면 수익 거래에서 얼마나 더 벌 수 있는가?
"""

# ─── 일봉 데이터 (당일 고가/저가가 핵심) ──────────────────────────────
DAILY = {
    # symbol: {date: {o, h, l, c}}
    "042700": {
        "0209": {"o": 202500, "h": 203000, "l": 193700, "c": 197800},
        "0212": {"o": 200500, "h": 224500, "l": 199400, "c": 209500},
        "0213": {"o": 205500, "h": 208500, "l": 200000, "c": 202500},
    },
    "010690": {
        "0209": {"o": 9030, "h": 9060, "l": 8900, "c": 9030},
        "0212": {"o": 10160, "h": 11690, "l": 10080, "c": 11420},
        "0213": {"o": 11190, "h": 11410, "l": 10570, "c": 10670},
    },
    "005690": {
        "0209": {"o": 13860, "h": 14860, "l": 13860, "c": 14710},
        "0212": {"o": 14780, "h": 17800, "l": 14470, "c": 16990},
        "0213": {"o": 18010, "h": 18260, "l": 16350, "c": 16350},
    },
    "018880": {
        "0212": {"o": 4605, "h": 4730, "l": 4350, "c": 4390},
        "0213": {"o": 4350, "h": 4820, "l": 4310, "c": 4460},
    },
    "031820": {
        "0212": {"o": 615, "h": 723, "l": 610, "c": 694},
        "0213": {"o": 710, "h": 790, "l": 699, "c": 732},
    },
    "088350": {
        "0212": {"o": 4360, "h": 4910, "l": 4305, "c": 4495},
        "0213": {"o": 4525, "h": 5200, "l": 4495, "c": 4830},
    },
    "001200": {
        "0212": {"o": 4460, "h": 4550, "l": 4395, "c": 4425},
        "0213": {"o": 4590, "h": 5000, "l": 4440, "c": 4780},
    },
    "034020": {
        "0212": {"o": 94700, "h": 95500, "l": 92300, "c": 95500},
        "0213": {"o": 94000, "h": 99900, "l": 93700, "c": 96700},
    },
    # SEPA 종목
    "055550": {
        "0210": {"o": 95700, "h": 99000, "l": 94800, "c": 97900},
        "0211": {"o": 97800, "h": 101000, "l": 97800, "c": 100900},
        "0212": {"o": 105600, "h": 107200, "l": 101100, "c": 106000},
        "0213": {"o": 105200, "h": 106200, "l": 102400, "c": 102500},
    },
    "086790": {
        "0210": {"o": 124700, "h": 126800, "l": 120500, "c": 122200},
        "0211": {"o": 121500, "h": 127800, "l": 121500, "c": 125800},
        "0212": {"o": 127700, "h": 130000, "l": 126000, "c": 130000},
        "0213": {"o": 131500, "h": 132200, "l": 126700, "c": 127600},
    },
    "024110": {
        "0210": {"o": 23900, "h": 24500, "l": 23700, "c": 24150},
        "0211": {"o": 24500, "h": 25100, "l": 24350, "c": 25000},
        "0212": {"o": 25650, "h": 26400, "l": 25150, "c": 26150},
        "0213": {"o": 26500, "h": 26800, "l": 25700, "c": 26150},
    },
    "105560": {
        "0210": {"o": 157300, "h": 160300, "l": 153300, "c": 155500},
        "0211": {"o": 155500, "h": 165000, "l": 155400, "c": 164500},
        "0212": {"o": 163800, "h": 168500, "l": 162400, "c": 168500},
        "0213": {"o": 166800, "h": 170500, "l": 164000, "c": 167900},
    },
    "316140": {
        "0210": {"o": 35700, "h": 36500, "l": 35100, "c": 35600},
        "0211": {"o": 36150, "h": 38450, "l": 35950, "c": 37850},
        "0212": {"o": 39750, "h": 39750, "l": 37700, "c": 39150},
        "0213": {"o": 40100, "h": 41500, "l": 38950, "c": 38950},
    },
    "073240": {
        "0210": {"o": 7000, "h": 7410, "l": 6940, "c": 7360},
        "0211": {"o": 7460, "h": 7650, "l": 7400, "c": 7530},
        "0212": {"o": 7460, "h": 7460, "l": 7000, "c": 7100},
        "0213": {"o": 7100, "h": 7140, "l": 6950, "c": 7010},
    },
    "005940": {
        "0211": {"o": 29050, "h": 29050, "l": 28100, "c": 29000},
        "0212": {"o": 29500, "h": 29900, "l": 29100, "c": 29150},
        "0213": {"o": 30300, "h": 32250, "l": 29600, "c": 30900},
    },
}

# 5일 종가 데이터 (5일 모멘텀 계산용)
CLOSE_5D = {
    # symbol: {entry_date: (5일전 종가, 전일 종가)}
    "042700": {"0212": (193500, 190500)},  # 0205c=193500, 0211c=190500
    "010690": {"0212": (9300, 10150)},     # 0205c=9300, 0211c=10150
    "005690": {"0212": (13790, 14630)},    # 0205c=13790, 0211c=14630
    "018880": {"0213": (4030, 4390)},      # 0205c=4030, 0212c=4390
    "031820": {"0213": (615, 694)},        # 0205c=615, 0212c=694
    "088350": {"0213": (3700, 4495)},      # 0205c=3700, 0212c=4495
    "001200": {"0213": (4150, 4425)},      # 0205c=4150, 0212c=4425
    "034020": {"0213": (90600, 95500)},    # 0205c=90600, 0212c=95500
}


FEE_RT = 0.00227  # 왕복 수수료 ~0.227%

# ─── 거래 데이터 ────────────────────────────────────────────────────
trades = [
    # SEPA
    {"sym": "055550", "name": "신한지주",     "strat": "sepa", "entry": 98800,  "exit": 102600, "qty": 4,   "date": "0210", "pnl": 15200,  "pnl_pct": 3.85},
    {"sym": "086790", "name": "하나금융지주",  "strat": "sepa", "entry": 125900, "exit": 128700, "qty": 2,   "date": "0210", "pnl": 5600,   "pnl_pct": 2.22},
    {"sym": "024110", "name": "기업은행",     "strat": "sepa", "entry": 24497,  "exit": 25000,  "qty": 52,  "date": "0210", "pnl": 26156,  "pnl_pct": 2.05},
    {"sym": "105560", "name": "KB금융",      "strat": "sepa", "entry": 159400, "exit": 164600, "qty": 8,   "date": "0210", "pnl": 41600,  "pnl_pct": 3.26},
    {"sym": "316140", "name": "우리금융지주",  "strat": "sepa", "entry": 36400,  "exit": 37400,  "qty": 35,  "date": "0210", "pnl": 35000,  "pnl_pct": 2.75},
    {"sym": "073240", "name": "금호타이어",   "strat": "sepa", "entry": 7340,   "exit": 7570,   "qty": 176, "date": "0210", "pnl": 40480,  "pnl_pct": 3.13},
    {"sym": "005940", "name": "NH투자증권",  "strat": "sepa", "entry": 28800,  "exit": 29450,  "qty": 9,   "date": "0211", "pnl": 5850,   "pnl_pct": 2.26},
    {"sym": "024110", "name": "기업은행2",   "strat": "sepa", "entry": 24900,  "exit": 25820,  "qty": 5,   "date": "0211", "pnl": 4600,   "pnl_pct": 3.69},
    # 모멘텀
    {"sym": "042700", "name": "한미반도체",   "strat": "momentum", "entry": 219000, "exit": 207500, "qty": 6,    "date": "0212", "pnl": -69000,  "pnl_pct": -5.25},
    {"sym": "010690", "name": "화신",        "strat": "momentum", "entry": 11390,  "exit": 11160,  "qty": 68,   "date": "0212", "pnl": -15640,  "pnl_pct": -2.02},
    {"sym": "005690", "name": "파미셀",      "strat": "momentum", "entry": 16723,  "exit": 17192,  "qty": 54,   "date": "0212", "pnl": 25326,   "pnl_pct": 2.80},
    {"sym": "018880", "name": "한온시스템",   "strat": "momentum", "entry": 4755,   "exit": 4655,   "qty": 275,  "date": "0213", "pnl": -27500,  "pnl_pct": -2.10},
    {"sym": "031820", "name": "아이티센",     "strat": "momentum", "entry": 759,    "exit": 742,    "qty": 1736, "date": "0213", "pnl": -29512,  "pnl_pct": -2.24},
    {"sym": "088350", "name": "한화생명",     "strat": "momentum", "entry": 4930,   "exit": 4835,   "qty": 147,  "date": "0213", "pnl": -13965,  "pnl_pct": -1.93},
    {"sym": "001200", "name": "유진투자증권",  "strat": "momentum", "entry": 4844,   "exit": 4965,   "qty": 71,   "date": "0213", "pnl": 8591,    "pnl_pct": 2.50},
    {"sym": "034020", "name": "두산에너빌리티","strat": "momentum", "entry": 99900,  "exit": 96200,  "qty": 8,    "date": "0213", "pnl": -29600,  "pnl_pct": -3.70},
]


def pct(entry, price):
    return (price - entry) / entry * 100


def main():
    print("=" * 90)
    print("  B안 효과 분석 — 실제 거래 + 당일 시장 데이터 기반")
    print("=" * 90)

    # ═══ 분석 1: 5일 모멘텀 필터 ═══
    print(f"\n{'─'*90}")
    print("  분석 1: 5일 모멘텀 필터 (>= 2.0% 필수)")
    print(f"{'─'*90}")

    momentum_trades = [t for t in trades if t["strat"] == "momentum"]
    filter_saved = 0
    filter_lost = 0

    for t in momentum_trades:
        data_5d = CLOSE_5D.get(t["sym"], {}).get(t["date"])
        if data_5d:
            close_5ago, close_prev = data_5d
            chg_5d = (close_prev - close_5ago) / close_5ago * 100
        else:
            chg_5d = None

        would_filter = chg_5d is not None and chg_5d < 2.0
        actual_result = "승" if t["pnl"] > 0 else "패"
        marker = "BLOCK" if would_filter else "PASS"

        if would_filter:
            if t["pnl"] < 0:
                filter_saved += abs(t["pnl"])
            else:
                filter_lost += t["pnl"]

        print(
            f"  [{marker:5s}] {t['sym']:6s} {t['name']:12s} | "
            f"5일 모멘텀: {chg_5d:>+6.1f}% | "
            f"실제: {actual_result} {t['pnl']:>+10,}원 ({t['pnl_pct']:>+5.2f}%)"
        )

    print(f"\n  → 필터 효과: 손실 회피 {filter_saved:>+,}원 / 수익 포기 {filter_lost:>,}원")
    print(f"  → 순 효과: {filter_saved - filter_lost:>+,}원")

    # ═══ 분석 2: 수익 기회 분석 (당일 고가 기준) ═══
    print(f"\n{'─'*90}")
    print("  분석 2: 수익 기회 — 실제 청산가 vs 당일 고가 (남겨둔 돈)")
    print(f"{'─'*90}")

    total_left = 0
    for t in trades:
        day_data = DAILY.get(t["sym"], {}).get(t["date"])
        if not day_data:
            continue

        day_high = day_data["h"]
        max_pct_possible = pct(t["entry"], day_high)
        actual_pct = t["pnl_pct"]
        left_on_table = max_pct_possible - actual_pct  # 남겨둔 수익률
        left_money = (day_high - t["exit"]) * t["qty"] if day_high > t["exit"] else 0

        if t["pnl"] > 0 and left_money > 0:
            total_left += left_money
            print(
                f"  {t['sym']:6s} {t['name']:12s} [{t['strat']:8s}] | "
                f"청산 {t['exit']:>10,} ({actual_pct:>+5.2f}%) | "
                f"당일고가 {day_high:>10,} ({max_pct_possible:>+5.2f}%) | "
                f"남긴 수익: {left_money:>+10,}원 (+{left_on_table:.1f}%p)"
            )

    print(f"\n  → 수익 거래에서 테이블에 남긴 총액: {total_left:>+,}원")

    # ═══ 분석 3: 1차 익절 임팩트 시뮬레이션 ═══
    print(f"\n{'─'*90}")
    print("  분석 3: 1차 익절 2.5%→4.0% 변경 시 수익 거래 임팩트")
    print(f"{'─'*90}")
    print(f"  {'종목':12s} | {'수량':>6s} | {'현행 1차(2.5%)':>16s} | {'B안 1차(4.0%)':>16s} | {'차이':>10s}")
    print(f"  {'─'*12} | {'─'*6} | {'─'*16} | {'─'*16} | {'─'*10}")

    total_diff = 0
    for t in trades:
        if t["pnl"] <= 0:
            continue
        if t["strat"] == "sepa":
            continue  # SEPA는 이미 5%/30%로 별도 관리

        # 현행: 2.5% 도달 시 30% 매도
        qty_current = max(1, int(t["qty"] * 0.30))
        first_exit_price_current = t["entry"] * 1.025
        pnl_current_first = (first_exit_price_current - t["entry"]) * qty_current

        # B안: 4.0% 도달 시 20% 매도 (도달 가능한 경우만)
        day_data = DAILY.get(t["sym"], {}).get(t["date"])
        if not day_data:
            continue

        day_high = day_data["h"]
        max_pct_possible = pct(t["entry"], day_high)

        if max_pct_possible >= 4.0:
            qty_b = max(1, int(t["qty"] * 0.20))
            first_exit_price_b = t["entry"] * 1.04
            pnl_b_first = (first_exit_price_b - t["entry"]) * qty_b
            diff = pnl_b_first - pnl_current_first
        else:
            qty_b = 0
            pnl_b_first = 0  # 4% 미도달
            diff = -pnl_current_first  # 현행은 2.5%에서 일부 확정, B안은 0

        total_diff += diff
        print(
            f"  {t['name']:12s} | {t['qty']:>5d}주 | "
            f"{qty_current:>3d}주×{pnl_current_first:>+8,.0f}원 | "
            f"{'미도달' if qty_b == 0 else f'{qty_b:>3d}주×{pnl_b_first:>+8,.0f}원':>16s} | "
            f"{diff:>+10,.0f}원"
        )

    print(f"\n  → 수익 거래 1차 익절 차이: {total_diff:>+,}원")

    # ═══ 분석 4: 당일 고가 도달률 분석 ═══
    print(f"\n{'─'*90}")
    print("  분석 4: 모멘텀 종목 진입 후 당일 최대 수익률 (당일 고가 기준)")
    print(f"{'─'*90}")

    for t in momentum_trades:
        day_data = DAILY.get(t["sym"], {}).get(t["date"])
        if not day_data:
            continue

        max_up = pct(t["entry"], day_data["h"])
        max_down = pct(t["entry"], day_data["l"])
        close_pct = pct(t["entry"], day_data["c"])

        # 5일 모멘텀
        data_5d = CLOSE_5D.get(t["sym"], {}).get(t["date"])
        chg_5d = (data_5d[1] - data_5d[0]) / data_5d[0] * 100 if data_5d else 0

        hit_4pct = "O" if max_up >= 4.0 else "X"
        hit_2_5pct = "O" if max_up >= 2.5 else "X"

        print(
            f"  {t['sym']:6s} {t['name']:12s} | "
            f"5d:{chg_5d:>+5.1f}% | "
            f"당일 고가:{max_up:>+6.2f}% 저가:{max_down:>+6.2f}% 종가:{close_pct:>+6.2f}% | "
            f"2.5%도달:{hit_2_5pct} 4.0%도달:{hit_4pct}"
        )

    # ═══ 종합 판정 ═══
    print(f"\n{'='*90}")
    print("  종합 판정: B안 적용 시 추정 효과")
    print(f"{'='*90}")

    actual_total = sum(t["pnl"] for t in trades)
    print(f"\n  실제 총 PnL (현행):              {actual_total:>+,}원")

    # 5d 필터 효과 (한미반도체 -69,000 회피, 수익 포기 0)
    effect_filter = filter_saved - filter_lost
    print(f"  + 5일 모멘텀 필터 효과:           {effect_filter:>+,}원")

    # 1차 익절 확대 효과는 수익 거래에서만 적용
    # 하지만 실제 효과는 "나머지 물량이 더 오래 버팀" 에서 나오므로
    # 당일 고가 기준으로 보수적 추정
    print(f"  + 1차 익절 확대 효과 (보수적):    (장중 데이터 필요 — 일봉으로 불확실)")

    print(f"\n  B안 최소 보장 개선:               {effect_filter:>+,}원 (필터만으로)")
    print(f"  5일 모멘텀 필터가 최악 거래(한미반도체 -69,000원)를 정확히 제거")
    print(f"\n  주의: 1차 익절 확대(2.5%→4.0%)의 정확한 효과 측정에는")
    print(f"  분 단위 장중 데이터가 필요합니다. 일봉만으로는 판단이 불가합니다.")


if __name__ == "__main__":
    main()
