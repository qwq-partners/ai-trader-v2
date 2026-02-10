"""
AI Trading Bot v2 - 진화된 설정 영속화

진화 엔진이 변경한 파라미터를 evolved_overrides.yml에 저장하여
봇 재시작 시에도 최적화된 설정이 유지되도록 합니다.

default.yml은 절대 수정하지 않습니다.

출처 추적: _meta 섹션에 source(evolution|manual|rollback), timestamp 기록
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from loguru import logger


class EvolvedConfigManager:
    """
    진화된 설정 관리자

    - 저장: config/evolved_overrides.yml (default.yml은 절대 수정 안 함)
    - save_override(component, param, value, source) - 진화 성공 시 호출
    - remove_override(component, param) - 롤백 시 호출
    - get_overrides() - 봇 시작 시 로드
    - get_meta(component, param) - 출처/시점 조회
    """

    def __init__(self, config_dir: Optional[str] = None):
        if config_dir:
            self._config_dir = Path(config_dir)
        else:
            self._config_dir = Path(__file__).parent.parent.parent.parent / "config"
        self._override_file = self._config_dir / "evolved_overrides.yml"
        self._overrides: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """evolved_overrides.yml 로드"""
        if not self._override_file.exists():
            return {}
        try:
            with open(self._override_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            # _meta 섹션 제외한 섹션 수 로그
            sections = [k for k in data if k != "_meta"]
            logger.info(f"진화 오버라이드 로드: {self._override_file} ({len(sections)}개 섹션)")
            return data
        except Exception as e:
            logger.warning(f"진화 오버라이드 로드 실패: {e}")
            return {}

    def _save(self):
        """evolved_overrides.yml 저장"""
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            with open(self._override_file, "w", encoding="utf-8") as f:
                yaml.dump(
                    self._overrides,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
            logger.debug(f"진화 오버라이드 저장: {self._override_file}")
        except Exception as e:
            logger.error(f"진화 오버라이드 저장 실패: {e}")

    def save_override(self, component: str, param: str, value: Any, source: str = "evolution"):
        """
        파라미터 오버라이드 저장 (출처 추적)

        Args:
            component: 컴포넌트명 (e.g., "momentum_breakout", "exit_manager", "risk_config")
            param: 파라미터명 (e.g., "stop_loss_pct")
            value: 값
            source: 출처 ("evolution" | "manual" | "rollback" | "dashboard")
        """
        if component not in self._overrides:
            self._overrides[component] = {}

        # YAML 직렬화 가능한 타입으로 변환
        if hasattr(value, 'item'):  # numpy scalar
            value = value.item()

        self._overrides[component][param] = value

        # 메타데이터 저장 (_meta 섹션)
        if "_meta" not in self._overrides:
            self._overrides["_meta"] = {}
        meta_key = f"{component}.{param}"
        self._overrides["_meta"][meta_key] = {
            "source": source,
            "timestamp": datetime.now().isoformat(),
        }

        self._save()
        logger.info(f"[영속화] 저장: {component}.{param} = {value} (source={source})")

    def remove_override(self, component: str, param: str):
        """
        파라미터 오버라이드 제거 (롤백 시)

        Args:
            component: 컴포넌트명
            param: 파라미터명
        """
        if component in self._overrides:
            self._overrides[component].pop(param, None)
            # 빈 섹션 제거
            if not self._overrides[component]:
                del self._overrides[component]

        # 메타데이터도 제거
        meta_key = f"{component}.{param}"
        if "_meta" in self._overrides:
            self._overrides["_meta"].pop(meta_key, None)
            if not self._overrides["_meta"]:
                del self._overrides["_meta"]

        self._save()
        logger.info(f"[영속화] 제거: {component}.{param}")

    def get_overrides(self) -> Dict[str, Dict[str, Any]]:
        """
        모든 오버라이드 반환 (_meta 제외)

        Returns:
            {"component_name": {"param": value, ...}, ...}
        """
        return {k: v for k, v in self._overrides.items() if k != "_meta"}

    def get_component_overrides(self, component: str) -> Dict[str, Any]:
        """특정 컴포넌트의 오버라이드 반환"""
        return dict(self._overrides.get(component, {}))

    def get_meta(self, component: str, param: str) -> Optional[Dict[str, str]]:
        """특정 파라미터의 메타데이터 (source, timestamp) 반환"""
        meta = self._overrides.get("_meta", {})
        return meta.get(f"{component}.{param}")

    def get_all_meta(self) -> Dict[str, Dict[str, str]]:
        """모든 메타데이터 반환"""
        return dict(self._overrides.get("_meta", {}))


# 싱글톤
_config_manager: Optional[EvolvedConfigManager] = None


def get_evolved_config_manager() -> EvolvedConfigManager:
    """EvolvedConfigManager 인스턴스 반환"""
    global _config_manager
    if _config_manager is None:
        _config_manager = EvolvedConfigManager()
    return _config_manager
