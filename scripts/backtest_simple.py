"""간단 백테스트"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

# 설정
INITIAL_CAPITAL = 10_000_000
SYMBOLS = ["005930", "000660", "373220", "207940", "005380", "000270", "051910", "006400", "035420", "035720"]
START_DATE = "2024-01-01"
END_DATE = "2024-12-31"

# 전략 파라미터
MIN_BREAKOUT_PCT = 1.0
VOLUME_SURGE_RATIO = 3.0
STOP_LOSS_PCT = 2.5
TAKE_PROFIT_PCT = 5.0

def load_data():
    """데이터 로드"""
    import FinanceDataReader as fdr
    
    data = {}
    load_start = pd.to_datetime(START_DATE) - timedelta(days=90)
    
    for symbol in SYMBOLS:
        try:
            df = fdr.DataReader(symbol, load_start, END_DATE)
            df.columns = [c.lower() for c in df.columns]
            
            # 지표
            df['high_20d'] = df['high'].rolling(20).max().shift(1)
            df['vol_avg'] = df['volume'].rolling(20).mean()
            df['vol_ratio'] = df['volume'] / df['vol_avg']
            
            data[symbol] = df
            print(f"{symbol}: {len(df)}일 로드")
        except Exception as e:
            print(f"{symbol} 실패: {e}")
    
    return data

def backtest(data):
    """백테스트"""
    cash = INITIAL_CAPITAL
    positions = {}
    trades = []
    
    # 날짜 리스트
    all_dates = sorted(set(
        date for df in data.values() 
        for date in df.index 
        if pd.to_datetime(START_DATE) <= date <= pd.to_datetime(END_DATE)
    ))
    
    print(f"\n시뮬레이션: {len(all_dates)}일")
    
    for date in all_dates:
        # 청산 체크
        to_exit = []
        for symbol in list(positions.keys()):
            if date not in data[symbol].index:
                continue
            
            pos = positions[symbol]
            price = data[symbol].loc[date]['close']
            pnl_pct = (price - pos['entry_price']) / pos['entry_price'] * 100
            
            if pnl_pct <= -STOP_LOSS_PCT:
                reason = "stop_loss"
            elif pnl_pct >= TAKE_PROFIT_PCT:
                reason = "take_profit"
            elif (date - pos['entry_date']).days >= 20:
                reason = "timeout"
            else:
                continue
            
            # 청산
            pnl = (price - pos['entry_price']) * pos['quantity']
            trades.append({
                'symbol': symbol,
                'entry_date': pos['entry_date'],
                'exit_date': date,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'reason': reason,
                'days': (date - pos['entry_date']).days
            })
            cash += price * pos['quantity']
            to_exit.append(symbol)
        
        for symbol in to_exit:
            del positions[symbol]
        
        # 진입 체크
        if len(positions) < 5:
            for symbol, df in data.items():
                if symbol in positions or date not in df.index:
                    continue
                
                row = df.loc[date]
                if pd.isna(row['high_20d']) or pd.isna(row['vol_ratio']):
                    continue
                
                breakout = (row['close'] - row['high_20d']) / row['high_20d'] * 100
                if breakout < MIN_BREAKOUT_PCT or row['vol_ratio'] < VOLUME_SURGE_RATIO:
                    continue
                
                # 진입
                position_value = (cash + sum(data[s].loc[date]['close'] * positions[s]['quantity'] for s in positions if date in data[s].index)) * 0.10
                quantity = int(position_value / row['close'])
                
                if quantity > 0 and row['close'] * quantity <= cash:
                    positions[symbol] = {
                        'quantity': quantity,
                        'entry_price': row['close'],
                        'entry_date': date
                    }
                    cash -= row['close'] * quantity
                    print(f"{date.date()} 진입: {symbol} +{breakout:.1f}% vol={row['vol_ratio']:.1f}x")
                
                if len(positions) >= 5:
                    break
    
    # 강제 청산
    for symbol, pos in positions.items():
        price = data[symbol].loc[all_dates[-1]]['close']
        pnl = (price - pos['entry_price']) * pos['quantity']
        trades.append({
            'symbol': symbol,
            'pnl': pnl,
            'pnl_pct': (price - pos['entry_price']) / pos['entry_price'] * 100,
            'reason': 'forced'
        })
        cash += price * pos['quantity']
    
    return cash, trades

def analyze(final_cash, trades):
    """분석"""
    print("\n" + "="*80)
    print("백테스트 결과")
    print("="*80)
    
    total_return = (final_cash - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] < 0]
    
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
    
    print(f"\n수익률:")
    print(f"  초기 자본: {INITIAL_CAPITAL:,.0f}원")
    print(f"  최종 자산: {final_cash:,.0f}원")
    print(f"  총 수익률: {total_return:+.2f}%")
    
    print(f"\n거래 통계:")
    print(f"  총 거래: {len(trades)}건")
    print(f"  승리: {len(wins)}건 ({win_rate:.1f}%)")
    print(f"  패배: {len(losses)}건")
    
    print(f"\n손익 분석:")
    print(f"  평균 수익: {avg_win:+,.0f}원")
    print(f"  평균 손실: {avg_loss:+,.0f}원")
    if avg_loss != 0:
        print(f"  손익비: {abs(avg_win / avg_loss):.2f}")
    
    # 청산 이유
    reasons = {}
    for t in trades:
        r = t.get('reason', 'unknown')
        reasons[r] = reasons.get(r, 0) + 1
    
    print(f"\n청산 이유:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}건")
    
    print("\n" + "="*80)

if __name__ == "__main__":
    print("백테스트 시작...")
    data = load_data()
    final_cash, trades = backtest(data)
    analyze(final_cash, trades)
