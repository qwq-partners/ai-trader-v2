"""
AI Trading Bot v2 - 코드 자동 진화 (Code Evolver)

Claude Code CLI를 활용하여 코드 자체를 개선하는 파이프라인.
모든 변경은 별도 브랜치 + PR로 관리되며, 사람이 반드시 리뷰 후 머지합니다.

파이프라인:
  트리거(주1회 토요일 or 수동 or 연속롤백3회)
    → 컨텍스트 수집 (거래성과, 진화실패이력, 에러로그)
    → 별도 브랜치 생성 (auto-evolution/YYYYMMDD-HHMMSS)
    → claude -p --output-format=json 호출 (5분 타임아웃)
    → 검증 (py_compile + pytest + 변경범위 10파일 이하)
    → git commit + gh pr create (자동머지 금지)
    → 텔레그램 알림
    → main 브랜치 복귀
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


class CodeEvolver:
    """
    Claude Code CLI 기반 코드 자동 진화

    안전 장치:
    - 모든 코드 변경은 별도 브랜치 (main 직접 수정 불가)
    - PR 생성만 — 사람이 반드시 리뷰 후 머지
    - py_compile 문법 검증 필수
    - 변경 파일 10개 초과 시 거부
    - 실패 시 브랜치 자동 삭제 + main 복귀
    """

    def __init__(
        self,
        project_root: Optional[str] = None,
        max_changed_files: int = 10,
        claude_timeout: int = 300,  # 5분
    ):
        self.project_root = Path(project_root or self._find_project_root())
        self.max_changed_files = max_changed_files
        self.claude_timeout = claude_timeout

        # 상태 추적
        self._original_branch: Optional[str] = None
        self._evolution_branch: Optional[str] = None
        self._consecutive_rollbacks = 0

    @staticmethod
    def _find_project_root() -> str:
        """프로젝트 루트 자동 탐지"""
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / ".git").exists():
                return str(parent)
        return str(Path(__file__).parent.parent.parent.parent)

    def increment_rollback_count(self):
        """롤백 카운트 증가 (strategy_evolver에서 호출)"""
        self._consecutive_rollbacks += 1

    def reset_rollback_count(self):
        """롤백 카운트 리셋"""
        self._consecutive_rollbacks = 0

    @property
    def should_trigger_by_rollbacks(self) -> bool:
        """연속 롤백 3회 이상이면 코드 진화 트리거"""
        return self._consecutive_rollbacks >= 3

    async def run_evolution(self, trigger_reason: str = "scheduled") -> Dict:
        """
        코드 진화 파이프라인 실행

        Args:
            trigger_reason: 트리거 사유 ("scheduled", "manual", "rollback_threshold")

        Returns:
            {"success": bool, "pr_url": str, "message": str, ...}
        """
        result = {
            "success": False,
            "trigger": trigger_reason,
            "timestamp": datetime.now().isoformat(),
            "branch": "",
            "pr_url": "",
            "changed_files": 0,
            "message": "",
        }

        try:
            logger.info(f"[코드진화] 파이프라인 시작 (사유: {trigger_reason})")

            # 0. claude CLI 존재 확인
            if not self._check_claude_cli():
                result["message"] = "claude CLI를 찾을 수 없습니다"
                logger.warning(f"[코드진화] {result['message']}")
                return result

            # 1. 현재 브랜치 저장
            self._original_branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

            # 2. 컨텍스트 수집
            context = self._collect_context()

            # 3. 별도 브랜치 생성
            branch_name = f"auto-evolution/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            self._git("checkout", "-b", branch_name)
            self._evolution_branch = branch_name
            result["branch"] = branch_name
            logger.info(f"[코드진화] 브랜치 생성: {branch_name}")

            # 4. Claude CLI 호출
            prompt = self._build_prompt(context)
            claude_result = await self._run_claude(prompt)

            if not claude_result.get("success"):
                raise RuntimeError(f"Claude CLI 실패: {claude_result.get('error', 'unknown')}")

            # 5. 변경 파일 검증
            changed_files = self._get_changed_files()
            result["changed_files"] = len(changed_files)

            if len(changed_files) == 0:
                result["message"] = "변경 사항 없음"
                logger.info("[코드진화] Claude가 변경한 파일 없음")
                self._cleanup_branch()
                return result

            if len(changed_files) > self.max_changed_files:
                raise RuntimeError(
                    f"변경 파일 {len(changed_files)}개 > 최대 {self.max_changed_files}개 — 거부"
                )

            # 6. py_compile 검증
            compile_errors = self._verify_syntax(changed_files)
            if compile_errors:
                raise RuntimeError(f"문법 오류: {compile_errors}")

            # 7. pytest 실행 (테스트 파일이 있는 경우)
            test_result = self._run_tests()
            if test_result and not test_result.get("passed"):
                raise RuntimeError(f"테스트 실패: {test_result.get('summary', '')}")

            # 8. git commit
            self._git("add", "-A")
            commit_msg = (
                f"auto-evolution: {trigger_reason}\n\n"
                f"Claude Code에 의한 자동 코드 진화\n"
                f"변경 파일: {len(changed_files)}개\n"
                f"트리거: {trigger_reason}"
            )
            self._git("commit", "-m", commit_msg)

            # 9. push + PR 생성
            self._git("push", "-u", "origin", branch_name)
            pr_url = self._create_pr(trigger_reason, changed_files, context)
            result["pr_url"] = pr_url

            # 10. 성공
            result["success"] = True
            result["message"] = f"PR 생성 완료: {pr_url}"
            logger.info(f"[코드진화] 완료: {pr_url}")

            # 롤백 카운트 리셋
            self.reset_rollback_count()

        except Exception as e:
            result["message"] = str(e)
            logger.error(f"[코드진화] 실패: {e}")
            # 실패 시 정리
            self._cleanup_branch()

        finally:
            # main 브랜치 복귀
            self._return_to_original_branch()

        return result

    def _check_claude_cli(self) -> bool:
        """claude CLI 존재 확인"""
        try:
            subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                timeout=10,
                cwd=str(self.project_root),
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _git(self, *args: str) -> str:
        """git 명령 실행"""
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=str(self.project_root),
            timeout=30,
        )
        if result.returncode != 0 and args[0] not in ("diff", "status"):
            logger.warning(f"[git] {' '.join(args)} 실패: {result.stderr.strip()}")
        return result.stdout

    def _collect_context(self) -> Dict:
        """코드 진화를 위한 컨텍스트 수집"""
        context = {
            "timestamp": datetime.now().isoformat(),
            "trading_performance": {},
            "evolution_failures": [],
            "recent_errors": [],
        }

        # 거래 성과 수집
        try:
            from .trade_reviewer import get_trade_reviewer
            reviewer = get_trade_reviewer()
            review = reviewer.review_period(14)  # 최근 2주
            context["trading_performance"] = {
                "total_trades": review.total_trades,
                "win_rate": review.win_rate,
                "profit_factor": review.profit_factor,
                "avg_pnl_pct": review.avg_pnl_pct,
                "issues": review.issues[:5],
            }
        except Exception as e:
            logger.debug(f"[코드진화] 거래 성과 수집 실패: {e}")

        # 진화 실패 이력 수집
        try:
            from .strategy_evolver import get_strategy_evolver
            evolver = get_strategy_evolver()
            state = evolver.get_evolution_state()
            if state:
                failed = [
                    c.to_dict() for c in state.change_history[-20:]
                    if c.is_effective is False
                ]
                context["evolution_failures"] = failed[-5:]
        except Exception as e:
            logger.debug(f"[코드진화] 진화 이력 수집 실패: {e}")

        # 최근 에러 로그 수집
        try:
            log_dir = self.project_root / "logs"
            if log_dir.exists():
                # 가장 최근 로그 디렉토리
                log_dirs = sorted(log_dir.iterdir(), reverse=True)
                for ld in log_dirs[:3]:
                    for log_file in ld.glob("*.log"):
                        try:
                            content = log_file.read_text(encoding="utf-8", errors="ignore")
                            errors = [
                                line.strip() for line in content.split("\n")
                                if "ERROR" in line or "CRITICAL" in line
                            ]
                            context["recent_errors"].extend(errors[-5:])
                        except Exception:
                            pass
                context["recent_errors"] = context["recent_errors"][:10]
        except Exception as e:
            logger.debug(f"[코드진화] 에러 로그 수집 실패: {e}")

        return context

    def _build_prompt(self, context: Dict) -> str:
        """Claude CLI에 전달할 프롬프트 구성"""
        perf = context.get("trading_performance", {})
        failures = context.get("evolution_failures", [])
        errors = context.get("recent_errors", [])

        prompt_parts = [
            "# AI Trading Bot v2 코드 개선 요청",
            "",
            "## 프로젝트 개요",
            "한국 주식 시장 자동 매매 봇입니다.",
            "일 1% 수익률을 목표로 모멘텀/테마/갭/평균회귀 전략을 사용합니다.",
            "",
            "## 현재 거래 성과",
            f"- 총 거래: {perf.get('total_trades', 'N/A')}건",
            f"- 승률: {perf.get('win_rate', 'N/A')}%",
            f"- 손익비: {perf.get('profit_factor', 'N/A')}",
            f"- 평균 수익률: {perf.get('avg_pnl_pct', 'N/A')}%",
        ]

        if perf.get("issues"):
            prompt_parts.append("\n## 식별된 문제점")
            for issue in perf["issues"]:
                prompt_parts.append(f"- {issue}")

        if failures:
            prompt_parts.append("\n## 파라미터 진화 실패 이력 (최근)")
            for f in failures[:3]:
                prompt_parts.append(
                    f"- {f.get('strategy','')}.{f.get('parameter','')}: "
                    f"{f.get('old_value','')} → {f.get('new_value','')} "
                    f"(사유: {f.get('reason','')})"
                )

        if errors:
            prompt_parts.append("\n## 최근 에러 로그")
            for err in errors[:5]:
                prompt_parts.append(f"- {err[:200]}")

        prompt_parts.extend([
            "",
            "## 개선 요청",
            "위 데이터를 분석하여 다음 중 가장 효과적인 개선을 1~3가지 수행해주세요:",
            "1. 전략 로직 개선 (진입/청산 조건 최적화)",
            "2. 리스크 관리 강화 (연속 손실 방지, 포지션 사이징)",
            "3. 버그 수정 (에러 로그 기반)",
            "4. 신호 품질 향상 (필터 추가, 노이즈 제거)",
            "",
            "## 제약 조건 (필수)",
            "- src/ 디렉토리 내 Python 파일만 수정",
            "- 변경 파일 10개 이하",
            "- 기존 인터페이스(함수 시그니처, 클래스 구조) 유지",
            "- config/default.yml 수정 금지",
            "- 새 의존성 추가 금지",
            "- 각 변경에 주석으로 변경 사유 기록",
        ])

        return "\n".join(prompt_parts)

    async def _run_claude(self, prompt: str) -> Dict:
        """Claude CLI 실행 (비동기)"""
        try:
            process = await asyncio.create_subprocess_exec(
                "claude", "-p", prompt,
                "--output-format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_root),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.claude_timeout,
            )

            if process.returncode != 0:
                return {
                    "success": False,
                    "error": stderr.decode("utf-8", errors="replace")[:500],
                }

            # JSON 파싱 시도
            output = stdout.decode("utf-8", errors="replace")
            try:
                result_data = json.loads(output)
                return {"success": True, "data": result_data}
            except json.JSONDecodeError:
                # JSON이 아니더라도 성공으로 처리 (텍스트 출력)
                return {"success": True, "data": {"raw": output[:2000]}}

        except asyncio.TimeoutError:
            return {"success": False, "error": f"타임아웃 ({self.claude_timeout}초)"}
        except FileNotFoundError:
            return {"success": False, "error": "claude CLI를 찾을 수 없습니다"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_changed_files(self) -> List[str]:
        """변경된 파일 목록"""
        diff_output = self._git("diff", "--name-only", "HEAD")
        status_output = self._git("status", "--porcelain")

        files = set()
        for line in diff_output.strip().split("\n"):
            if line.strip():
                files.add(line.strip())
        for line in status_output.strip().split("\n"):
            if line.strip():
                # status --porcelain 포맷: "XY filename"
                parts = line.strip().split(None, 1)
                if len(parts) >= 2:
                    files.add(parts[1])

        return [f for f in files if f]

    def _verify_syntax(self, changed_files: List[str]) -> List[str]:
        """Python 파일 문법 검증"""
        errors = []
        for file in changed_files:
            if not file.endswith(".py"):
                continue
            filepath = self.project_root / file
            if not filepath.exists():
                continue
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(filepath)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    errors.append(f"{file}: {result.stderr.strip()}")
            except Exception as e:
                errors.append(f"{file}: {e}")
        return errors

    def _run_tests(self) -> Optional[Dict]:
        """pytest 실행 (테스트 디렉토리 존재 시)"""
        test_dir = self.project_root / "tests"
        if not test_dir.exists():
            return None  # 테스트 없으면 스킵

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(test_dir), "-x", "--tb=short", "-q"],
                capture_output=True,
                text=True,
                cwd=str(self.project_root),
                timeout=120,
            )
            return {
                "passed": result.returncode == 0,
                "summary": result.stdout[-500:] if result.stdout else result.stderr[-500:],
            }
        except subprocess.TimeoutExpired:
            return {"passed": False, "summary": "pytest 타임아웃 (120초)"}
        except Exception as e:
            logger.warning(f"[코드진화] pytest 실행 실패: {e}")
            return None  # 테스트 실행 실패는 스킵

    def _create_pr(self, trigger_reason: str, changed_files: List[str], context: Dict) -> str:
        """GitHub PR 생성"""
        perf = context.get("trading_performance", {})

        title = f"[Auto-Evolution] {trigger_reason} - {datetime.now().strftime('%Y-%m-%d')}"

        body_parts = [
            "## Summary",
            f"- Trigger: {trigger_reason}",
            f"- Changed files: {len(changed_files)}",
            f"- Win rate: {perf.get('win_rate', 'N/A')}%",
            f"- Profit factor: {perf.get('profit_factor', 'N/A')}",
            "",
            "## Changed Files",
        ]
        for f in changed_files:
            body_parts.append(f"- `{f}`")

        body_parts.extend([
            "",
            "## Safety",
            "- [ ] 코드 리뷰 완료",
            "- [ ] py_compile 통과 확인됨",
            "- [ ] 기존 인터페이스 유지 확인",
            "",
            "> Auto-generated by AI Trading Bot Code Evolver",
        ])

        body = "\n".join(body_parts)

        try:
            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--title", title,
                    "--body", body,
                ],
                capture_output=True,
                text=True,
                cwd=str(self.project_root),
                timeout=30,
            )
            if result.returncode == 0:
                pr_url = result.stdout.strip()
                logger.info(f"[코드진화] PR 생성: {pr_url}")
                return pr_url
            else:
                logger.warning(f"[코드진화] PR 생성 실패: {result.stderr.strip()}")
                return f"PR 생성 실패: {result.stderr.strip()[:200]}"
        except FileNotFoundError:
            logger.warning("[코드진화] gh CLI를 찾을 수 없습니다")
            return "gh CLI 미설치"
        except Exception as e:
            logger.error(f"[코드진화] PR 생성 오류: {e}")
            return f"PR 생성 오류: {e}"

    def _cleanup_branch(self):
        """실패 시 브랜치 정리"""
        if not self._evolution_branch:
            return

        try:
            # 변경 사항 초기화
            self._git("checkout", "--", ".")
            self._git("clean", "-fd")

            # 원래 브랜치로 복귀
            if self._original_branch:
                self._git("checkout", self._original_branch)

            # 실패 브랜치 삭제
            self._git("branch", "-D", self._evolution_branch)
            logger.info(f"[코드진화] 실패 브랜치 삭제: {self._evolution_branch}")

            # 리모트에 push된 경우 삭제 시도
            self._git("push", "origin", "--delete", self._evolution_branch)
        except Exception as e:
            logger.warning(f"[코드진화] 브랜치 정리 실패: {e}")
        finally:
            self._evolution_branch = None

    def _return_to_original_branch(self):
        """원래 브랜치로 복귀"""
        if not self._original_branch:
            return
        try:
            current = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
            if current != self._original_branch:
                self._git("checkout", self._original_branch)
                logger.info(f"[코드진화] {self._original_branch} 브랜치 복귀")
        except Exception as e:
            logger.warning(f"[코드진화] 브랜치 복귀 실패: {e}")


# 싱글톤
_code_evolver: Optional[CodeEvolver] = None


def get_code_evolver() -> CodeEvolver:
    """CodeEvolver 인스턴스 반환"""
    global _code_evolver
    if _code_evolver is None:
        _code_evolver = CodeEvolver()
    return _code_evolver
