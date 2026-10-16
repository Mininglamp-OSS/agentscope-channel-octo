#!/usr/bin/env bash
# 启停脚本：API 副本 / channel worker（日志在 deploy/logs，pid 在 deploy/run）
#   ./run.sh api 8000            启动一个 API 副本（不持有连接，CHANNEL_MODE=worker）
#   ./run.sh api 8001 embedded   启动一个“旧架构”副本（自己持有连接）
#   ./run.sh worker [name]       启动 channel worker（可多开：worker w2）
#   ./run.sh stop [name|all]     停止
#   ./run.sh status              查看进程
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${VENV:-$HERE/../.venv}/bin/activate"   # 指向安装了 agentscope + agentscope-channel-octo 的虚拟环境
mkdir -p "$HERE/logs" "$HERE/run"
# shellcheck disable=SC1090
source "$VENV"
cd "$HERE"

start() {  # name, logfile, env..., cmd
  local name="$1"; shift
  local pidf="$HERE/run/$name.pid"
  if [[ -f "$pidf" ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidf"))"; return
  fi
  nohup env "$@" > "$HERE/logs/$name.log" 2>&1 &
  echo $! > "$pidf"
  echo "started $name (pid $!) -> logs/$name.log"
}

case "${1:-}" in
  api)
    port="${2:-8000}"; mode="${3:-worker}"
    start "api-$port" PORT="$port" CHANNEL_MODE="$mode" python api.py ;;
  worker)
    name="${2:-worker}"
    start "$name" python channel_worker.py ;;
  stop)
    target="${2:-all}"
    for pidf in "$HERE"/run/*.pid; do
      [[ -e "$pidf" ]] || continue
      n="$(basename "$pidf" .pid)"
      if [[ "$target" == "all" || "$target" == "$n" ]]; then
        pid="$(cat "$pidf")"
        if kill -0 "$pid" 2>/dev/null; then kill "$pid" && echo "stopped $n (pid $pid)"; fi
        rm -f "$pidf"
      fi
    done ;;
  status)
    for pidf in "$HERE"/run/*.pid; do
      [[ -e "$pidf" ]] || { echo "nothing running"; break; }
      n="$(basename "$pidf" .pid)"; pid="$(cat "$pidf")"
      if kill -0 "$pid" 2>/dev/null; then echo "$n: running (pid $pid)"; else echo "$n: dead"; fi
    done ;;
  *)
    sed -n '2,8p' "$0" ;;
esac
