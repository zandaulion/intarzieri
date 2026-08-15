#!/usr/bin/env bash
# Is CFR reachable from here? Appends one CSV line per run.
#
# The point is comparison: run it on two machines on different connections
# and see whether outages line up. If they do, CFR was down. If only one
# machine loses it, that machine's network is being blocked.
#
#   ./probe.sh              one check, append to the log
#   ./probe.sh summary      what the log says so far
#
# Env: OUT=path  LABEL=name  NTFY_TOPIC=xxx (alert on change)  TIMEOUT=10
set -uo pipefail

HOST="${HOST:-mersultrenurilor.infofer.ro}"
OUT="${OUT:-$HOME/cfr-probe.csv}"
LABEL="${LABEL:-$(hostname -s 2>/dev/null || echo host)}"
TIMEOUT="${TIMEOUT:-10}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
NTFY_SERVER="${NTFY_SERVER:-https://ntfy.sh}"

summary() {
  [ -s "$OUT" ] || { echo "no data yet: $OUT"; exit 0; }
  awk -F, 'NR>1 {
      n++; if ($5=="1") ok++; else { bad++; if (!f) f=$1; l=$1 }
      if ($4=="0") tcpbad++
      if ($7!="" && $7!="0") { t+=$7; tn++ }
    }
    END {
      printf "  checks      %d\n", n
      printf "  reachable   %d (%.1f%%)\n", ok, n? ok*100/n : 0
      printf "  failed      %d\n", bad
      if (bad) printf "  first fail  %s\n  last fail   %s\n", f, l
      if (tcpbad) printf "  no TCP      %d  (blocked below HTTP)\n", tcpbad
      if (tn)  printf "  mean time   %.2fs\n", t/tn
    }' "$OUT"
  echo "  --- last 10 ---"
  tail -n +2 "$OUT" | tail -10 | awk -F, '{printf "  %s  %-12s tcp=%s http=%-3s %ss\n", $1, $2, $4, $6, $7}'
}

[ "${1:-}" = "summary" ] && { summary; exit 0; }

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ip=$(getent hosts "$HOST" 2>/dev/null | awk '{print $1; exit}')
[ -z "$ip" ] && ip="dns-fail"

# TCP first: a block usually shows here, before TLS or HTTP can say anything.
tcp=0
if [ "$ip" != "dns-fail" ] && timeout "$TIMEOUT" bash -c "echo > /dev/tcp/$ip/443" 2>/dev/null; then
  tcp=1
fi

read -r code total < <(curl -s -o /dev/null -w '%{http_code} %{time_total}' \
  --max-time "$TIMEOUT" "https://$HOST/" 2>/dev/null || echo "000 0")
ok=0; [ "$code" != "000" ] && ok=1

[ -f "$OUT" ] || echo "time,label,ip,tcp,ok,http_code,seconds" > "$OUT"
prev=$(tail -1 "$OUT" 2>/dev/null | awk -F, '$1!="time"{print $5}')
echo "$ts,$LABEL,$ip,$tcp,$ok,$code,$total" >> "$OUT"

echo "$ts $LABEL tcp=$tcp http=$code ${total}s"

# Alert only when the answer changes, same rule the app uses.
if [ -n "$NTFY_TOPIC" ] && [ -n "$prev" ] && [ "$prev" != "$ok" ]; then
  if [ "$ok" = "1" ]; then
    msg="CFR reachable again from $LABEL"; prio=3; tag=white_check_mark
  else
    msg="CFR unreachable from $LABEL (http=$code)"; prio=4; tag=rotating_light
  fi
  curl -s -o /dev/null --max-time 10 -H "Content-Type: application/json" \
    -d "{\"topic\":\"$NTFY_TOPIC\",\"title\":\"Probe: $LABEL\",\"message\":\"$msg\",\"priority\":$prio,\"tags\":[\"$tag\"]}" \
    "$NTFY_SERVER" || true
fi
