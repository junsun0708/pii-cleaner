#!/usr/bin/env bash
# 매일 오후 5시(17:00) 슬랙 본인 메시지 자동 삭제 (cron)
# - 토큰 2개로 3건 순차 실행 (워크스페이스1: 2건, 워크스페이스2: 1건)
# - 로그 저장 비활성화: 필요 시 아래 LOG 관련 라인 주석 해제

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 로그 저장 비활성화 — 필요 시 복원
# LOG="$DIR/logs/cron_daily_cleanup_$(date +%Y%m%d).log"
# mkdir -p "$DIR/logs"

PY="$DIR/.venv/bin/python"
SCRIPT="$DIR/slack_self_delete.py"

# secrets 로드 (SLACK_TOKEN_WS1, SLACK_TOKEN_WS2)
set -a
. "$DIR/secrets.env"
set +a

# 로그 저장 비활성화
# echo "=== $(date '+%F %T %Z') 슬랙 정리 시작 ===" >> "$LOG"

run() {
    local ws_name="$1"
    local token="$2"
    local user="$3"
    # 로그 저장 비활성화 — 필요 시 ">> \"$LOG\" 2>&1" 형태로 복원
    # echo "" >> "$LOG"
    # echo "--- [$ws_name] user=$user ---" >> "$LOG"
    SLACK_USER_TOKEN="$token" "$PY" "$SCRIPT" --user "$user" --execute >/dev/null 2>&1
    # local rc=$?
    # echo "--- rc=$rc ---" >> "$LOG"
}

# 워크스페이스 1 (토큰 d902) — 2건
run "WS1" "$SLACK_TOKEN_WS1" "U05LK9CKASE"
run "WS1" "$SLACK_TOKEN_WS1" "U04KE2VRP8R"

# 워크스페이스 2 (토큰 d13a0) — 1건
run "WS2" "$SLACK_TOKEN_WS2" "U03E2P0G5KK"

# 로그 저장 비활성화
# echo "" >> "$LOG"
# echo "=== $(date '+%F %T %Z') 슬랙 정리 종료 ===" >> "$LOG"
