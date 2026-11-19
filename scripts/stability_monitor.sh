#!/usr/bin/env bash
# 24-hour stability record. Every 15 min appends one block to ~/stability/YYYYMMDD.log:
# health (exit code = failed checks), service restarts, memory, CPU, last cycle line, errors in the window.
# Stops by itself after DURATION_H hours.   nohup bash scripts/stability_monitor.sh >/dev/null 2>&1 &
set -u
APP_DIR="${APP_DIR:-$HOME/bot}"; DURATION_H="${DURATION_H:-24}"; STEP_S="${STEP_S:-900}"
OUT="$HOME/stability"; mkdir -p "$OUT"
LOG="$OUT/$(date -u +%Y%m%dT%H%M%SZ).log"
END=$(( $(date +%s) + DURATION_H * 3600 ))
cd "$APP_DIR"
while [ "$(date +%s)" -lt "$END" ]; do
  {
    echo "=== $(date -u +%FT%TZ) (ALA $(TZ=Asia/Almaty date +%H:%M))"
    .venv/bin/python -m app.cli health 2>/dev/null; echo "health_failures=$?"
    systemctl show arb-bot -p NRestarts -p MemoryCurrent -p ActiveEnterTimestamp | tr '\n' ' '; echo
    PID=$(systemctl show arb-bot -p MainPID --value); ps -o rss=,pcpu= -p "$PID" 2>/dev/null | awk '{print "bot_rss_mb="int($1/1024)" bot_cpu="$2}'
    sudo journalctl -u arb-bot --since "-${STEP_S}s" --no-pager -o cat | grep "cycle done" | tail -1
    echo "errors_in_window=$(sudo journalctl -u arb-bot --since "-${STEP_S}s" --no-pager -o cat | grep -cE 'ERROR|Traceback')"
    echo "cycles_in_window=$(sudo journalctl -u arb-bot --since "-${STEP_S}s" --no-pager -o cat | grep -c 'cycle done')"
  } >> "$LOG" 2>&1
  sleep "$STEP_S"
done
echo "=== finished $(date -u +%FT%TZ)" >> "$LOG"
