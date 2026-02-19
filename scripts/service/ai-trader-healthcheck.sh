#!/bin/bash
# AI Trader v2 헬스체크 스크립트
# systemd timer로 5분마다 실행
# 문제 감지 시 서비스 재시작 + 로그 기록

set -euo pipefail

LOG_TAG="[헬스체크]"
SERVICE_NAME="ai-trader"
DASHBOARD_URL="http://localhost:8080"
PID_FILE="/home/user/.cache/ai_trader/trader.pid"
LOG_DIR="/home/user/projects/ai-trader-v2/logs"
HEALTHCHECK_LOG="/tmp/ai-trader-healthcheck.log"
MAX_MEMORY_MB=800
MAX_RESTART_PER_HOUR=3

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_TAG $1" | tee -a "$HEALTHCHECK_LOG"
}

# --- 체크 1: 서비스 상태 ---
check_service_active() {
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        log "FAIL: 서비스 비활성 상태 — 재시작 시도"
        systemctl restart "$SERVICE_NAME"
        return 1
    fi
    return 0
}

# --- 체크 2: 프로세스 존재 ---
check_process() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if ! kill -0 "$pid" 2>/dev/null; then
            log "FAIL: PID $pid 프로세스 없음 (좀비 PID 파일)"
            rm -f "$PID_FILE"
            systemctl restart "$SERVICE_NAME"
            return 1
        fi
    else
        # PID 파일 없으면 프로세스 직접 탐색
        if ! pgrep -f "run_trader.py" > /dev/null; then
            log "FAIL: run_trader.py 프로세스 없음"
            systemctl restart "$SERVICE_NAME"
            return 1
        fi
    fi
    return 0
}

# --- 체크 3: 대시보드 응답 ---
check_dashboard() {
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$DASHBOARD_URL" 2>/dev/null || echo "000")
    if [ "$http_code" != "200" ]; then
        log "WARN: 대시보드 응답 실패 (HTTP $http_code)"
        return 1
    fi
    return 0
}

# --- 체크 4: 메모리 사용량 ---
check_memory() {
    local pid mem_mb
    pid=$(pgrep -f "run_trader.py" | head -1)
    if [ -n "$pid" ]; then
        mem_mb=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.0f", $1/1024}')
        if [ "$mem_mb" -gt "$MAX_MEMORY_MB" ]; then
            log "WARN: 메모리 과다 사용 ${mem_mb}MB (한도 ${MAX_MEMORY_MB}MB)"
            return 1
        fi
    fi
    return 0
}

# --- 체크 5: 최근 에러 로그 ---
check_recent_errors() {
    local today error_count
    today=$(date '+%Y%m%d')
    local log_dir="$LOG_DIR/$today"
    if [ -d "$log_dir" ]; then
        # 최근 10분 내 CRITICAL 로그
        error_count=$(find "$log_dir" -name "*.log" -newer <(date -d '10 minutes ago' '+%Y%m%d%H%M') 2>/dev/null | \
            xargs grep -c "CRITICAL" 2>/dev/null | \
            awk -F: '{sum+=$2} END {print sum+0}')
        if [ "$error_count" -gt 5 ]; then
            log "WARN: 최근 10분 CRITICAL 로그 ${error_count}건"
            return 1
        fi
    fi
    return 0
}

# --- 체크 6: 재시작 횟수 제한 ---
check_restart_limit() {
    local recent_restarts
    recent_restarts=$(journalctl -u "$SERVICE_NAME" --since "1 hour ago" --no-pager 2>/dev/null | \
        grep -c "Started AI Trader" 2>/dev/null || echo "0")
    if [ "$recent_restarts" -ge "$MAX_RESTART_PER_HOUR" ]; then
        log "CRITICAL: 1시간 내 ${recent_restarts}회 재시작 — 반복 장애 의심, 재시작 보류"
        return 1
    fi
    return 0
}

# === 메인 실행 ===
main() {
    local failures=0

    # 재시작 횟수 먼저 체크 (무한 재시작 방지)
    if ! check_restart_limit; then
        log "재시작 한도 초과 — 수동 확인 필요"
        exit 1
    fi

    check_service_active || ((failures++))
    check_process || ((failures++))
    check_dashboard || ((failures++))
    check_memory || ((failures++))
    check_recent_errors || ((failures++))

    if [ "$failures" -eq 0 ]; then
        log "OK: 모든 체크 통과"
    else
        log "결과: ${failures}개 체크 실패"
    fi
}

main "$@"
