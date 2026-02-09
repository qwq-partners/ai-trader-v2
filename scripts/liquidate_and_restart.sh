#!/bin/bash
# ============================================================
# 전략전환: 청산 → 봇 재기동 (1회성 스크립트)
# 2026-02-10(화) 09:01 cron으로 실행
# ============================================================
set -euo pipefail

PROJ="/home/user/projects/ai-trader-v2"
PY="${PROJ}/venv/bin/python"
LOG="/tmp/liquidate_20260210.log"
TRADER_LOG="/tmp/trader_restart.log"

echo "========================================" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 전략전환 시작" >> "$LOG"

# 1. 기존 run_trader.py 종료
echo "[$(date '+%H:%M:%S')] 기존 봇 종료 중..." >> "$LOG"
pkill -TERM -f "run_trader.py" 2>/dev/null || true
sleep 5
# 아직 살아있으면 강제 종료
pkill -KILL -f "run_trader.py" 2>/dev/null || true
sleep 2
echo "[$(date '+%H:%M:%S')] 기존 봇 종료 완료" >> "$LOG"

# 2. 전량 청산 실행
echo "[$(date '+%H:%M:%S')] 전량 청산 시작..." >> "$LOG"
cd "$PROJ"
"$PY" scripts/liquidate_all.py --force >> "$LOG" 2>&1
LIQUIDATE_EXIT=$?
echo "[$(date '+%H:%M:%S')] 전량 청산 완료 (exit=$LIQUIDATE_EXIT)" >> "$LOG"

# 3. 봇 재기동 (새 로직 반영)
echo "[$(date '+%H:%M:%S')] 봇 재기동 중..." >> "$LOG"
cd "$PROJ"
nohup "$PY" scripts/run_trader.py --config config/default.yml > "$TRADER_LOG" 2>&1 &
NEW_PID=$!
echo "[$(date '+%H:%M:%S')] 봇 재기동 완료 (PID=$NEW_PID)" >> "$LOG"

# 4. crontab에서 자기 자신 제거
echo "[$(date '+%H:%M:%S')] cron 항목 자동 제거 중..." >> "$LOG"
crontab -l 2>/dev/null | grep -v "liquidate_and_restart" | grep -v "전략전환 전량청산" | crontab -
echo "[$(date '+%H:%M:%S')] 완료" >> "$LOG"
echo "========================================" >> "$LOG"
