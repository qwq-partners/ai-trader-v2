"""
AI Trading Bot v2 - 설정 관리

YAML 설정 파일 로드 및 환경변수 처리
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional
from decimal import Decimal
from dataclasses import dataclass

import yaml
from loguru import logger

from ..core.types import TradingConfig, RiskConfig


def load_yaml_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """YAML 설정 파일 로드"""
    if config_path is None:
        # 기본 경로
        project_root = Path(__file__).parent.parent.parent
        config_path = project_root / "config" / "default.yml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        logger.warning(f"설정 파일 없음: {config_path}")
        return {}

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        logger.info(f"설정 로드: {config_path}")
        return config
    except Exception as e:
        logger.error(f"설정 로드 실패: {e}")
        return {}


def get_env_or_config(key: str, config: Dict[str, Any], default: Any = None) -> Any:
    """환경변수 우선, 없으면 설정 파일 값 사용"""
    env_value = os.getenv(key)
    if env_value is not None:
        return env_value

    # 설정에서 키 찾기 (점 표기법 지원)
    keys = key.lower().split('_')
    value = config
    for k in keys:
        if isinstance(value, dict):
            value = value.get(k, value.get(k.upper()))
        else:
            break

    return value if value is not None else default


def create_trading_config(config: Optional[Dict[str, Any]] = None) -> TradingConfig:
    """TradingConfig 객체 생성"""
    if config is None:
        config = load_yaml_config()

    trading = config.get("trading", {})
    risk_cfg = config.get("risk", {})

    # RiskConfig 생성
    risk = RiskConfig(
        daily_max_loss_pct=float(risk_cfg.get("daily_max_loss_pct", 5.0)),
        daily_max_trades=int(risk_cfg.get("daily_max_trades", 20)),
        base_position_pct=float(risk_cfg.get("base_position_pct", 25.0)),
        max_position_pct=float(risk_cfg.get("max_position_pct", 50.0)),
        max_positions=int(risk_cfg.get("max_positions", 5)),
        min_cash_reserve_pct=float(risk_cfg.get("min_cash_reserve_pct", 10.0)),
        min_position_value=int(risk_cfg.get("min_position_value", 500000)),
        dynamic_max_positions=bool(risk_cfg.get("dynamic_max_positions", True)),
        default_stop_loss_pct=float(risk_cfg.get("default_stop_loss_pct", 2.0)),
        default_take_profit_pct=float(risk_cfg.get("default_take_profit_pct", 3.0)),
        trailing_stop_pct=float(risk_cfg.get("trailing_stop_pct", 1.5)),
        hot_theme_position_pct=float(risk_cfg.get("hot_theme_position_pct", 70.0)),
        momentum_multiplier=float(risk_cfg.get("momentum_multiplier", 1.5)),
    )

    # TradingConfig 생성
    initial_capital = os.getenv("INITIAL_CAPITAL") or trading.get("initial_capital", 500000)

    fees = trading.get("fees", {})

    return TradingConfig(
        initial_capital=Decimal(str(initial_capital)),
        buy_fee_rate=float(fees.get("buy_rate", 0.00015)),
        sell_fee_rate=float(fees.get("sell_rate", 0.00315)),
        enable_pre_market=trading.get("enable_pre_market", True),
        enable_next_market=trading.get("enable_next_market", True),
        risk=risk,
    )


def load_dotenv(dotenv_path: Optional[str] = None):
    """환경변수 로드 (.env 파일)"""
    if dotenv_path is None:
        project_root = Path(__file__).parent.parent.parent
        dotenv_path = project_root / ".env"
    else:
        dotenv_path = Path(dotenv_path)

    if not dotenv_path.exists():
        return

    try:
        with open(dotenv_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        logger.debug(f".env 로드: {dotenv_path}")
    except Exception as e:
        logger.debug(f".env 로드 실패: {e}")


@dataclass
class AppConfig:
    """애플리케이션 전체 설정"""
    trading: TradingConfig
    raw: Dict[str, Any]  # 원본 설정

    @classmethod
    def load(cls, config_path: Optional[str] = None, dotenv_path: Optional[str] = None) -> "AppConfig":
        """설정 로드"""
        # .env 로드
        load_dotenv(dotenv_path)

        # YAML 로드
        raw = load_yaml_config(config_path)

        # TradingConfig 생성
        trading = create_trading_config(raw)

        return cls(trading=trading, raw=raw)

    def get(self, *keys: str, default: Any = None) -> Any:
        """중첩 키로 설정 값 조회"""
        value = self.raw
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return default
            if value is None:
                return default
        return value
