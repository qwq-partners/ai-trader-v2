#!/bin/bash
# AI Trader v2 - systemd 서비스 설치 스크립트
# 사용법: sudo bash scripts/service/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="/etc/systemd/system"

echo "=== AI Trader v2 systemd 서비스 설치 ==="
echo ""

# 1. 서비스 파일 복사
echo "[1/5] 서비스 파일 복사..."
cp "$SCRIPT_DIR/ai-trader.service" "$SERVICE_DIR/"
cp "$SCRIPT_DIR/ai-trader-healthcheck.service" "$SERVICE_DIR/"
cp "$SCRIPT_DIR/ai-trader-healthcheck.timer" "$SERVICE_DIR/"
echo "  → $SERVICE_DIR/ 에 3개 파일 복사 완료"

# 2. 헬스체크 스크립트 실행 권한
echo "[2/5] 실행 권한 설정..."
chmod +x "$SCRIPT_DIR/ai-trader-healthcheck.sh"

# 3. systemd 리로드
echo "[3/5] systemd 데몬 리로드..."
systemctl daemon-reload

# 4. 서비스 활성화 (부팅 시 자동 시작)
echo "[4/5] 서비스 활성화..."
systemctl enable ai-trader.service
systemctl enable ai-trader-healthcheck.timer

# 5. 기존 프로세스 정리 후 시작
echo "[5/5] 서비스 시작..."
# 기존 nohup 프로세스 정리
pkill -f "run_trader.py" 2>/dev/null || true
sleep 2
rm -f /home/user/.cache/ai_trader/trader.pid

systemctl start ai-trader.service
systemctl start ai-trader-healthcheck.timer

echo ""
echo "========================================="
echo " 설치 완료!"
echo "========================================="
echo ""
echo " 상태 확인:"
echo "   systemctl status ai-trader"
echo "   journalctl -u ai-trader -f          # 실시간 로그"
echo "   journalctl -u ai-trader --since today  # 오늘 로그"
echo ""
echo " 관리 명령:"
echo "   sudo systemctl restart ai-trader    # 재시작"
echo "   sudo systemctl stop ai-trader       # 중지"
echo "   sudo systemctl disable ai-trader    # 자동시작 해제"
echo ""
echo " 헬스체크:"
echo "   systemctl list-timers               # 타이머 목록"
echo "   journalctl -u ai-trader-healthcheck # 헬스체크 로그"
echo ""

# 상태 출력
systemctl status ai-trader.service --no-pager || true
echo ""
systemctl list-timers ai-trader-healthcheck.timer --no-pager || true
