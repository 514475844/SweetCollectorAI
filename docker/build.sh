#!/usr/bin/env bash
# 一键构建 + 冒烟（解锁 wsl.exe 之后在本机或任何 Linux x86_64 上跑）
# 用法： bash docker/build.sh            # 构建 + 起容器 + 探活
#        bash docker/build.sh buildonly  # 只构建
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 构建镜像 =="
docker compose build

if [ "${1:-}" = "buildonly" ]; then
  docker images mrm:x86_64 --format '  {{.Repository}}:{{.Tag}} {{.Size}}'
  exit 0
fi

echo "== 起容器（host 网络）=="
docker rm -f mrm >/dev/null 2>&1 || true
docker compose up -d

echo "== 等健康检查 =="
for i in $(seq 1 20); do
  st=$(docker inspect --format '{{.State.Health.Status}}' mrm 2>/dev/null || echo starting)
  echo "  [$i] $st"
  [ "$st" = "healthy" ] && break
  sleep 3
done

echo "== 探活 =="
python3 - <<'PY' 2>/dev/null || curl -fsS http://127.0.0.1:8080/healthz
import urllib.request, json
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for p in ("/healthz", "/api/session", "/v2"):
    r = op.open("http://127.0.0.1:8080" + p, timeout=8)
    print(f"  {p}: {r.status} {len(r.read())}B")
PY

echo "== 体积 =="
docker images mrm:x86_64 --format '  {{.Repository}}:{{.Tag}} {{.Size}}'
echo "完成。日志： docker logs -f mrm"
