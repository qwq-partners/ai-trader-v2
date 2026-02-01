"""
AI Trading Bot v2 - 수수료 계산기

실제 손익 계산 시 수수료(위탁수수료 + 제세금)를 포함합니다.

한국투자증권 BanKIS 온라인 기준 (2026년 1월~):
- 매수 수수료: 0.0140527% (유관기관 제비용 포함)
- 매도 수수료: 0.0130527% (유관기관 제비용 포함)
- 증권거래세: 0.20% (2026.1.1~ 코스피/코스닥 통일)
  - 코스피: 증권거래세 0.05% + 농어촌특별세 0.15%
  - 코스닥: 증권거래세 0.20%

총 매도 비용: 약 0.213%
왕복 거래 비용: 약 0.227%
"""

from decimal import Decimal
from typing import Tuple
from dataclasses import dataclass


@dataclass
class FeeConfig:
    """수수료 설정"""
    buy_commission_rate: Decimal = Decimal("0.000140527")   # 매수 수수료 0.0140527% (한투 BanKIS)
    sell_commission_rate: Decimal = Decimal("0.000130527")  # 매도 수수료 0.0130527% (한투 BanKIS)
    sell_tax_rate: Decimal = Decimal("0.002")               # 증권거래세 0.20% (2026.1.1~)

    @property
    def total_sell_rate(self) -> Decimal:
        """총 매도 비용률"""
        return self.sell_commission_rate + self.sell_tax_rate

    @property
    def round_trip_rate(self) -> Decimal:
        """왕복 거래 비용률"""
        return self.buy_commission_rate + self.total_sell_rate


class FeeCalculator:
    """수수료 계산기"""

    def __init__(self, config: FeeConfig = None):
        self.config = config or FeeConfig()

    def calculate_buy_fee(self, amount: Decimal) -> Decimal:
        """매수 수수료 계산"""
        return amount * self.config.buy_commission_rate

    def calculate_sell_fee(self, amount: Decimal) -> Decimal:
        """매도 수수료 + 세금 계산"""
        commission = amount * self.config.sell_commission_rate
        tax = amount * self.config.sell_tax_rate
        return commission + tax

    def calculate_net_pnl(
        self,
        buy_price: Decimal,
        sell_price: Decimal,
        quantity: int
    ) -> Tuple[Decimal, Decimal]:
        """
        순손익 계산 (수수료 포함)

        Returns:
            (순손익 금액, 순손익률 %)
        """
        buy_amount = buy_price * quantity
        sell_amount = sell_price * quantity

        buy_fee = self.calculate_buy_fee(buy_amount)
        sell_fee = self.calculate_sell_fee(sell_amount)

        total_cost = buy_amount + buy_fee
        net_proceeds = sell_amount - sell_fee

        net_pnl = net_proceeds - total_cost
        net_pnl_pct = (net_pnl / total_cost) * 100 if total_cost > 0 else Decimal("0")

        return net_pnl, net_pnl_pct

    def calculate_target_price_for_net_profit(
        self,
        entry_price: Decimal,
        target_net_pct: float
    ) -> Decimal:
        """
        목표 순수익률을 달성하기 위한 목표가 계산

        Args:
            entry_price: 매수가
            target_net_pct: 목표 순수익률 (%)

        Returns:
            수수료 포함 목표가
        """
        # 총 비용 = 매수가 * (1 + 매수수수료율)
        buy_rate = 1 + float(self.config.buy_commission_rate)

        # 목표 매도가 계산
        # 순수익 = 매도가 * (1 - 매도비용률) - 매수가 * (1 + 매수수수료율)
        # target_net_pct = (순수익 / 총비용) * 100

        sell_rate = 1 - float(self.config.total_sell_rate)
        target_multiplier = (1 + target_net_pct / 100) * buy_rate / sell_rate

        return entry_price * Decimal(str(target_multiplier))

    def calculate_stop_price_for_max_loss(
        self,
        entry_price: Decimal,
        max_loss_pct: float
    ) -> Decimal:
        """
        최대 손실률을 기준으로 손절가 계산

        Args:
            entry_price: 매수가
            max_loss_pct: 최대 손실률 (%, 양수로 입력)

        Returns:
            수수료 포함 손절가
        """
        buy_rate = 1 + float(self.config.buy_commission_rate)
        sell_rate = 1 - float(self.config.total_sell_rate)

        # 손실률 = (순손익 / 총비용) * 100
        # 손절가 = 매수가 * (1 - max_loss_pct/100) * buy_rate / sell_rate
        target_multiplier = (1 - max_loss_pct / 100) * buy_rate / sell_rate

        return entry_price * Decimal(str(target_multiplier))


# 전역 인스턴스
_fee_calculator = FeeCalculator()


def get_fee_calculator() -> FeeCalculator:
    """전역 수수료 계산기"""
    return _fee_calculator


def calculate_net_pnl(
    buy_price: float,
    sell_price: float,
    quantity: int
) -> Tuple[float, float]:
    """순손익 계산 (편의 함수)"""
    pnl, pnl_pct = _fee_calculator.calculate_net_pnl(
        Decimal(str(buy_price)),
        Decimal(str(sell_price)),
        quantity
    )
    return float(pnl), float(pnl_pct)


def get_target_price(entry_price: float, target_net_pct: float) -> float:
    """목표가 계산 (편의 함수)"""
    return float(_fee_calculator.calculate_target_price_for_net_profit(
        Decimal(str(entry_price)),
        target_net_pct
    ))


def get_stop_price(entry_price: float, max_loss_pct: float) -> float:
    """손절가 계산 (편의 함수)"""
    return float(_fee_calculator.calculate_stop_price_for_max_loss(
        Decimal(str(entry_price)),
        max_loss_pct
    ))
