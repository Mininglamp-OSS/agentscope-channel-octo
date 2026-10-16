#!/usr/bin/env bash
# 观察文章里的心跳键：agentscope:channel:liveness:{cid}（Hash：node_id -> 心跳 JSON，整键 TTL 30s，worker 每 10s 续租）
# 用法：./watch.sh [channel_id] [user]
CID="${1:-$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("channel_id",""))' "$(dirname "$0")/state.json" 2>/dev/null)}"
USER_ID="${2:-demo}"
REDIS="docker exec agentscope-redis redis-cli"
while true; do
  clear
  echo "== $(date '+%H:%M:%S')  channel=${CID:-?} =="
  for key in $($REDIS --scan --pattern 'agentscope:channel:liveness:*'); do
    echo "$key   TTL=$($REDIS TTL "$key")s"
    $REDIS HGETALL "$key" | paste - - | sed 's/^/    /'
  done
  [[ -z "$(${REDIS} --scan --pattern 'agentscope:channel:liveness:*')" ]] && echo "(no liveness keys — 没有 worker 在持有连接)"
  echo
  if [[ -n "$CID" ]]; then
    for port in 8000 8001 8002; do
      out="$(curl -s -m 1 -H "X-User-ID: $USER_ID" "http://localhost:$port/channels/$CID/status")"
      [[ -n "$out" ]] && echo ":$port  $out"
    done
  fi
  echo
  echo "outbound queue keys (文章说已删除，应为空): $($REDIS --scan --pattern '*channel*outbound*' | wc -l | tr -d ' ')"
  sleep 2
done
