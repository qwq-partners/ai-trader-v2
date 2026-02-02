"""파라미터 최적화"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest_simple import load_data, backtest

# 테스트 조합
param_sets = [
    # (breakout%, volume_surge, stop_loss%, take_profit%)
    (1.0, 3.0, 2.5, 5.0),  # 현재 (기준)
    (0.5, 3.0, 2.5, 5.0),  # 돌파 완화
    (1.0, 2.5, 2.5, 5.0),  # 거래량 완화
    (0.5, 2.5, 2.5, 5.0),  # 둘 다 완화
    (1.0, 3.0, 2.0, 4.0),  # 손익 타이트
    (1.0, 3.0, 3.0, 6.0),  # 손익 여유
]

print("파라미터 최적화 시작...\n")
data = load_data()

results = []
for i, (breakout, volume, stop, profit) in enumerate(param_sets, 1):
    # 전역 변수 수정
    import backtest_simple
    backtest_simple.MIN_BREAKOUT_PCT = breakout
    backtest_simple.VOLUME_SURGE_RATIO = volume
    backtest_simple.STOP_LOSS_PCT = stop
    backtest_simple.TAKE_PROFIT_PCT = profit
    
    final_cash, trades = backtest(data)
    
    if trades:
        wins = [t for t in trades if t['pnl'] > 0]
        win_rate = len(wins) / len(trades) * 100
        total_return = (final_cash - 10_000_000) / 10_000_000 * 100
    else:
        win_rate = 0
        total_return = 0
    
    results.append({
        'params': (breakout, volume, stop, profit),
        'return': total_return,
        'trades': len(trades),
        'win_rate': win_rate,
        'final': final_cash
    })
    
    print(f"[{i}/6] 돌파={breakout}%, 거래량={volume}x, 손절={stop}%, 익절={profit}%")
    print(f"      수익률={total_return:+.2f}%, 거래={len(trades)}건, 승률={win_rate:.1f}%\n")

# 최적 조합
print("="*80)
print("최적 조합 (수익률 기준)")
print("="*80)
best = sorted(results, key=lambda x: x['return'], reverse=True)[:3]
for i, r in enumerate(best, 1):
    b, v, s, p = r['params']
    print(f"{i}. 돌파={b}%, 거래량={v}x, 손절={s}%, 익절={p}%")
    print(f"   수익률={r['return']:+.2f}%, 거래={r['trades']}건, 승률={r['win_rate']:.1f}%")

print("\n" + "="*80)
print("최적 조합 (거래 횟수 기준)")
print("="*80)
best_trades = sorted(results, key=lambda x: abs(x['trades'] - 30))[:3]  # 월 2.5회 목표
for i, r in enumerate(best_trades, 1):
    b, v, s, p = r['params']
    print(f"{i}. 돌파={b}%, 거래량={v}x, 손절={s}%, 익절={p}%")
    print(f"   수익률={r['return']:+.2f}%, 거래={r['trades']}건, 승률={r['win_rate']:.1f}%")
