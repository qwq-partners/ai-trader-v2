"""
AI Trading Bot v2 - 백테스트 시스템

모멘텀 브레이크아웃 전략을 과거 데이터로 검증
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategies.momentum import MomentumBreakoutStrategy, MomentumConfig
from src.core.types import OrderSide, Position


@dataclass
class BacktestTrade:
    """백테스트 거래 기록"""
    symbol: str
    entry_date: datetime
    entry_price: float
    exit_date: datetime
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    holding_days: int
    exit_reason: str  # "stop_loss", "take_profit", "trailing", "timeout"


@dataclass
class BacktestResult:
    """백테스트 결과"""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0  # 총 수익 / 총 손실
    
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    
    sharpe_ratio: float = 0.0
    
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)


class Backtester:
    """
    백테스트 엔진
    
    과거 데이터로 전략을 시뮬레이션하고 성과 측정
    """
    
    def __init__(
        self,
        initial_capital: float = 10_000_000,
        symbols: List[str] = None,
        start_date: str = "2024-01-01",
        end_date: str = "2024-12-31",
    ):
        self.initial_capital = initial_capital
        self.symbols = symbols or self._get_default_symbols()
        self.start_date = pd.to_datetime(start_date)
        self.end_date = pd.to_datetime(end_date)
        
        # 상태
        self.equity = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[BacktestTrade] = []
        self.equity_history: List[Tuple[datetime, float]] = []
        
        # 데이터
        self.price_data: Dict[str, pd.DataFrame] = {}
        
    def _get_default_symbols(self) -> List[str]:
        """기본 테스트 종목 (KOSPI 대형주)"""
        return [
            "005930",  # 삼성전자
            "000660",  # SK하이닉스
            "373220",  # LG에너지솔루션
            "207940",  # 삼성바이오로직스
            "005380",  # 현대차
            "000270",  # 기아
            "051910",  # LG화학
            "006400",  # 삼성SDI
            "035420",  # NAVER
            "035720",  # 카카오
            "105560",  # KB금융
            "055550",  # 신한지주
            "028260",  # 삼성물산
            "012330",  # 현대모비스
            "066570",  # LG전자
            "003550",  # LG
            "096770",  # SK이노베이션
            "017670",  # SK텔레콤
            "034730",  # SK
            "032830",  # 삼성생명
        ]
    
    def load_data(self):
        """과거 데이터 로드"""
        logger.info(f"데이터 로드: {self.start_date.date()} ~ {self.end_date.date()}")
        
        try:
            import FinanceDataReader as fdr
        except ImportError:
            logger.error("FinanceDataReader 필요: pip install finance-datareader")
            return False
        
        # 60일 전부터 로드 (지표 계산용)
        load_start = self.start_date - timedelta(days=90)
        
        for symbol in self.symbols:
            try:
                df = fdr.DataReader(symbol, load_start, self.end_date)
                if df.empty:
                    logger.warning(f"{symbol}: 데이터 없음")
                    continue
                
                # 컬럼명 통일
                df.columns = [c.lower() for c in df.columns]
                if 'close' not in df.columns:
                    logger.warning(f"{symbol}: close 컬럼 없음")
                    continue
                
                # 지표 계산
                df = self._calculate_indicators(df)
                self.price_data[symbol] = df
                
                logger.info(f"{symbol}: {len(df)}일 로드")
                
            except Exception as e:
                logger.error(f"{symbol} 로드 실패: {e}")
        
        logger.info(f"총 {len(self.price_data)}개 종목 로드 완료")
        return len(self.price_data) > 0
    
    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """기술적 지표 계산"""
        # 20일 고가
        df['high_20d'] = df['high'].rolling(20).max().shift(1)
        
        # 거래량 비율 (20일 평균 대비)
        df['volume_avg_20'] = df['volume'].rolling(20).mean()
        df['vol_ratio'] = df['volume'] / df['volume_avg_20']
        
        # 가격 변화율
        df['change_1d'] = df['close'].pct_change(1) * 100
        df['change_5d'] = df['close'].pct_change(5) * 100
        df['change_20d'] = df['close'].pct_change(20) * 100
        
        # 신고가 근접도
        df['high_52w'] = df['high'].rolling(252).max()
        df['high_proximity'] = df['close'] / df['high_52w']
        
        return df
    
    def run(self, config: MomentumConfig) -> BacktestResult:
        """백테스트 실행"""
        logger.info("백테스트 시작")
        logger.info(f"전략: MomentumBreakout")
        logger.info(f"파라미터: breakout={config.min_breakout_pct}%, volume={config.volume_surge_ratio}x")
        logger.info(f"손절={config.stop_loss_pct}%, 익절={config.take_profit_pct}%")
        
        # 날짜별 시뮬레이션
        all_dates = sorted(set(
            date for df in self.price_data.values() 
            for date in df.index 
            if self.start_date <= date <= self.end_date
        ))
        
        logger.info(f"시뮬레이션 기간: {len(all_dates)}일")
        
        for current_date in all_dates:
            # 1. 기존 포지션 청산 체크
            self._check_exits(current_date, config)
            
            # 2. 새로운 진입 체크
            self._check_entries(current_date, config)
            
            # 3. 자산 가치 기록
            self._update_equity(current_date)
        
        # 4. 남은 포지션 강제 청산
        self._close_all_positions(all_dates[-1])
        
        # 5. 결과 분석
        result = self._analyze_results()
        
        logger.info("백테스트 완료")
        return result
    
    def _check_entries(self, date: datetime, config: MomentumConfig):
        """진입 신호 체크"""
        if len(self.positions) >= 5:  # 최대 5개 포지션
            return
        
        for symbol, df in self.price_data.items():
            if symbol in self.positions:
                continue
            
            if date not in df.index:
                continue
            
            row = df.loc[date]
            
            # 데이터 검증
            if pd.isna(row['high_20d']) or pd.isna(row['vol_ratio']):
                continue
            
            # 브레이크아웃 체크
            breakout_pct = (row['close'] - row['high_20d']) / row['high_20d'] * 100
            if breakout_pct < config.min_breakout_pct:
                continue
            
            # 거래량 체크
            if row['vol_ratio'] < config.volume_surge_ratio:
                continue
            
            # 진입!
            position_value = self.equity * 0.10  # 10% 포지션
            if position_value > self.cash:
                continue
            
            price = row['close']
            quantity = int(position_value / price)
            if quantity == 0:
                continue
            # 포지션 생성 (딕셔너리)
            self.positions[symbol] = {
                "quantity": quantity,
                "entry_price": price,
                "entry_time": date,
            }
            
            self.cash -= price * quantity
            
            logger.debug(f"{date.date()} 진입: {symbol} {quantity}주 @{price:,.0f} (돌파 +{breakout_pct:.1f}%, 거래량 {row['vol_ratio']:.1f}x)")
    
    def _check_exits(self, date: datetime, config: MomentumConfig):
        """청산 조건 체크"""
        to_exit = []
        
        for symbol, pos in self.positions.items():
            if date not in self.price_data[symbol].index:
                continue
            
            row = self.price_data[symbol].loc[date]
            current_price = row['close']
            entry_price = float(pos.entry_price)
            
            pnl_pct = (current_price - entry_price) / entry_price * 100
            holding_days = (date - pos.entry_time).days
            
            exit_reason = None
            
            # 손절
            if pnl_pct <= -config.stop_loss_pct:
                exit_reason = "stop_loss"
            
            # 익절
            elif pnl_pct >= config.take_profit_pct:
                exit_reason = "take_profit"
            
            # 트레일링 스탑 (간단 버전: 고점 대비)
            elif pnl_pct >= config.take_profit_pct * 0.5:
                # 고점 찾기
                future_df = self.price_data[symbol][pos.entry_time:date]
                if not future_df.empty:
                    peak_price = future_df['close'].max()
                    drawdown_from_peak = (current_price - peak_price) / peak_price * 100
                    if drawdown_from_peak <= -config.trailing_stop_pct:
                        exit_reason = "trailing"
            
            # 타임아웃 (20일)
            elif holding_days >= 20:
                exit_reason = "timeout"
            
            if exit_reason:
                to_exit.append((symbol, current_price, exit_reason))
        
        # 청산 실행
        for symbol, exit_price, exit_reason in to_exit:
            self._exit_position(symbol, exit_price, date, exit_reason)
    
    def _exit_position(self, symbol: str, exit_price: float, exit_date: datetime, reason: str):
        """포지션 청산"""
        pos = self.positions[symbol]
        entry_price = float(pos.entry_price)
        quantity = pos.quantity
        
        pnl = (exit_price - entry_price) * quantity
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        holding_days = (exit_date - pos.entry_time).days
        
        # 거래 기록
        trade = BacktestTrade(
            symbol=symbol,
            entry_date=pos.entry_time,
            entry_price=entry_price,
            exit_date=exit_date,
            exit_price=exit_price,
            quantity=quantity,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_days=holding_days,
            exit_reason=reason,
        )
        self.trades.append(trade)
        
        # 현금 회수
        self.cash += exit_price * quantity
        
        # 포지션 제거
        del self.positions[symbol]
        
        logger.debug(f"{exit_date.date()} 청산: {symbol} {reason} {pnl_pct:+.1f}% (보유 {holding_days}일)")
    
    def _update_equity(self, date: datetime):
        """자산 가치 업데이트"""
        position_value = 0.0
        for symbol, pos in self.positions.items():
            if date in self.price_data[symbol].index:
                current_price = self.price_data[symbol].loc[date]['close']
                position_value += current_price * pos.quantity
        
        self.equity = self.cash + position_value
        self.equity_history.append((date, self.equity))
    
    def _close_all_positions(self, date: datetime):
        """모든 포지션 강제 청산"""
        for symbol in list(self.positions.keys()):
            if date in self.price_data[symbol].index:
                exit_price = self.price_data[symbol].loc[date]['close']
                self._exit_position(symbol, exit_price, date, "forced")
    
    def _analyze_results(self) -> BacktestResult:
        """결과 분석"""
        result = BacktestResult()
        
        if not self.trades:
            logger.warning("거래 내역 없음")
            return result
        
        result.trades = self.trades
        result.equity_curve = self.equity_history
        result.final_equity = self.equity
        result.total_return_pct = (self.equity - self.initial_capital) / self.initial_capital * 100
        
        # 거래 통계
        result.total_trades = len(self.trades)
        result.winning_trades = sum(1 for t in self.trades if t.pnl > 0)
        result.losing_trades = sum(1 for t in self.trades if t.pnl < 0)
        result.win_rate = result.winning_trades / result.total_trades * 100 if result.total_trades > 0 else 0
        
        # 손익 통계
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [t.pnl for t in self.trades if t.pnl < 0]
        
        result.total_pnl = sum(t.pnl for t in self.trades)
        result.avg_win = sum(wins) / len(wins) if wins else 0
        result.avg_loss = sum(losses) / len(losses) if losses else 0
        
        total_win = sum(wins) if wins else 0
        total_loss = abs(sum(losses)) if losses else 0
        result.profit_factor = total_win / total_loss if total_loss > 0 else 0
        
        # 최대 낙폭 (MDD)
        if self.equity_history:
            equity_series = pd.Series([e for _, e in self.equity_history])
            peak = equity_series.expanding().max()
            drawdown = equity_series - peak
            result.max_drawdown = drawdown.min()
            result.max_drawdown_pct = (drawdown / peak).min() * 100
        
        # 샤프 비율 (일간 수익률 기반)
        if len(self.equity_history) > 1:
            equity_df = pd.DataFrame(self.equity_history, columns=['date', 'equity'])
            daily_returns = equity_df['equity'].pct_change().dropna()
            if len(daily_returns) > 0 and daily_returns.std() > 0:
                result.sharpe_ratio = (daily_returns.mean() / daily_returns.std()) * (252 ** 0.5)
        
        return result


def print_result(result: BacktestResult, config: MomentumConfig):
    """결과 출력"""
    print("\n" + "="*80)
    print("백테스트 결과")
    print("="*80)
    
    print(f"\n전략 파라미터:")
    print(f"  돌파 기준: {config.min_breakout_pct}%")
    print(f"  거래량 기준: {config.volume_surge_ratio}x")
    print(f"  손절: {config.stop_loss_pct}%")
    print(f"  익절: {config.take_profit_pct}%")
    
    print(f"\n수익률:")
    print(f"  최종 자산: {result.final_equity:,.0f}원")
    print(f"  총 수익률: {result.total_return_pct:+.2f}%")
    print(f"  최대 낙폭: {result.max_drawdown_pct:.2f}%")
    print(f"  샤프 비율: {result.sharpe_ratio:.2f}")
    
    print(f"\n거래 통계:")
    print(f"  총 거래: {result.total_trades}건")
    print(f"  승리: {result.winning_trades}건 ({result.win_rate:.1f}%)")
    print(f"  패배: {result.losing_trades}건")
    
    print(f"\n손익 분석:")
    print(f"  평균 수익: {result.avg_win:+,.0f}원")
    print(f"  평균 손실: {result.avg_loss:+,.0f}원")
    print(f"  손익비: {abs(result.avg_win / result.avg_loss) if result.avg_loss != 0 else 0:.2f}")
    print(f"  Profit Factor: {result.profit_factor:.2f}")
    
    print(f"\n청산 이유:")
    reasons = {}
    for trade in result.trades:
        reasons[trade.exit_reason] = reasons.get(trade.exit_reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}건")
    
    print("\n" + "="*80)


async def main():
    """메인 실행"""
    # 백테스터 초기화
    backtester = Backtester(
        initial_capital=10_000_000,
        start_date="2024-01-01",
        end_date="2024-12-31",
    )
    
    # 데이터 로드
    if not backtester.load_data():
        logger.error("데이터 로드 실패")
        return
    
    # 전략 설정
    config = MomentumConfig(
        min_breakout_pct=1.0,
        volume_surge_ratio=3.0,
        stop_loss_pct=2.5,
        take_profit_pct=5.0,
        trailing_stop_pct=1.5,
    )
    
    # 백테스트 실행
    result = backtester.run(config)
    
    # 결과 출력
    print_result(result, config)


if __name__ == "__main__":
    asyncio.run(main())
