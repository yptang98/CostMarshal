#!/usr/bin/env sh
set -u

action="${1:-sleep}"
shift 2>/dev/null || true
duration=""
seconds=""
path=""
text=""
command=""
every="30s"
timeout="1h"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --duration) duration="${2:-}"; shift 2 ;;
    --seconds) seconds="${2:-}"; shift 2 ;;
    --path) path="${2:-}"; shift 2 ;;
    --text) text="${2:-}"; shift 2 ;;
    --command) command="${2:-}"; shift 2 ;;
    --every) every="${2:-}"; shift 2 ;;
    --timeout) timeout="${2:-}"; shift 2 ;;
    *) echo "wakewait: unknown argument: $1" >&2; exit 2 ;;
  esac
done

to_seconds() {
  value="$1"
  [ -n "$value" ] || { echo 0; return; }
  awk '
    BEGIN {
      value = ARGV[1]
      ARGV[1] = ""
      if (value !~ /^[0-9]+([.][0-9]+)?(ms|s|m|h|d)?$/) exit 2
      unit = value
      sub(/^[0-9]+([.][0-9]+)?/, "", unit)
      amount = value
      sub(/(ms|s|m|h|d)$/, "", amount)
      scale = 1
      if (unit == "ms") scale = 0.001
      else if (unit == "m") scale = 60
      else if (unit == "h") scale = 3600
      else if (unit == "d") scale = 86400
      seconds = amount * scale
      if (seconds < 1) seconds = 1
      print int(seconds == int(seconds) ? seconds : int(seconds) + 1)
    }
  ' "$value"
}

now_text() {
  date '+%Y-%m-%d %H:%M:%S %z'
}

sleep_quiet() {
  delay="$1"
  [ "$delay" -gt 0 ] && sleep "$delay"
}

wait_until() {
  every_s="$1"
  timeout_s="$2"
  start=$(date +%s)
  deadline=$((start + timeout_s))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if check_ready; then return 0; fi
    now=$(date +%s)
    remaining=$((deadline - now))
    [ "$remaining" -lt 0 ] && remaining=0
    delay="$every_s"
    [ "$delay" -gt "$remaining" ] && delay="$remaining"
    sleep_quiet "$delay"
  done
  check_ready
}

case "$action" in
  sleep)
    if [ -n "$seconds" ]; then delay="$seconds"; else delay="$(to_seconds "$duration")"; fi
    [ "$delay" -gt 0 ] || { echo "wakewait: sleep requires --duration or --seconds" >&2; exit 2; }
    sleep_quiet "$delay"
    now_text
    exit 0
    ;;
  wait-file|wait-contains|wait-command)
    every_s="$(to_seconds "$every")" || { echo "wakewait: invalid --every" >&2; exit 2; }
    timeout_s="$(to_seconds "$timeout")" || { echo "wakewait: invalid --timeout" >&2; exit 2; }
    echo "wakewait $action start $(now_text) every=${every_s}s timeout=${timeout_s}s"
    ;;
  *)
    echo "wakewait: action must be sleep, wait-file, wait-contains, or wait-command" >&2
    exit 2
    ;;
esac

check_ready() {
  case "$action" in
    wait-file)
      [ -n "$path" ] || return 1
      [ -e "$path" ]
      ;;
    wait-contains)
      [ -n "$path" ] && [ -n "$text" ] || return 1
      [ -f "$path" ] && grep -F -q -- "$text" "$path"
      ;;
    wait-command)
      [ -n "$command" ] || return 1
      sh -c "$command" >/dev/null 2>&1
      ;;
  esac
}

if wait_until "$every_s" "$timeout_s"; then
  echo "wakewait $action ready $(now_text)"
  exit 0
fi

echo "wakewait $action timeout $(now_text)"
exit 124
