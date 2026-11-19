#!/usr/bin/env bash
# Network-level diagnosis of bookmaker sites from this machine: DNS, TCP, TLS, first byte,
# HTTP status and redirect chain. One plain request per URL; nothing is retried via other IPs.
#   bash scripts/diagnose_network.sh
set -u
URLS=(
  "https://1xbet.kz/" "https://1xbet.kz/ru/line/football" "https://1xbet.kz/robots.txt"
  "https://parimatch.kz/" "https://parimatch.kz/ru/" "https://parimatch.kz/robots.txt"
  "https://oinabet.kz/" "https://oinabet.kz/robots.txt"
  "https://tennisi.kz/" "https://tennisi.kz/robots.txt" "https://tennisi.bet/"
  "https://pin-up.kz/" "https://pin-up.kz/robots.txt"
)
UA="Mozilla/5.0 (X11; Linux x86_64) diagnose-network/1.0 (+read-only availability check)"
for u in "${URLS[@]}"; do
  host=$(echo "$u" | awk -F/ '{print $3}')
  ips=$(getent ahostsv4 "$host" | awk '{print $1}' | sort -u | tr '\n' ' ')
  out=$(curl -s -o /dev/null -A "$UA" --max-time 30 -L --max-redirs 5 \
    -w 'http=%{http_code} dns=%{time_namelookup}s tcp=%{time_connect}s tls=%{time_appconnect}s ttfb=%{time_starttransfer}s total=%{time_total}s redirects=%{num_redirects} final=%{url_effective} ip=%{remote_ip}' \
    "$u" 2>&1); rc=$?
  echo "$u | dns_ips=[${ips:-none}] | rc=$rc | $out"
done
