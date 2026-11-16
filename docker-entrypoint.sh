#!/bin/sh
# docker-entrypoint.sh —— 容器启动入口（订单 B5 权限 + A2 日志 + 启动健壮性）
set -e

# —— 卷可写性自检（任何一条不过都直接中文报错退出，不让它"假绿"）——
for d in /data /cache; do
  if [ ! -w "$d" ]; then
    echo "数据目录不可写：$d" >&2
    echo "请在宿主机执行：sudo chown -R 1000:1000 ./data ./cache（详见 docker/README.md「常见问题」）" >&2
    exit 1
  fi
done

# 日志目录（A2：文件日志落可挂卷目录）
mkdir -p /data/logs

echo "启动 Media Resource Manager（无头服务端）"
echo "  数据目录：${MRM_HOME:-未设置}"
echo "  Web 端口：${MRM_WEB_PORT:-8080}  监听：${MRM_WEB_BIND:-0.0.0.0}"

# exec 保证 PID 1 就是 python ⇒ docker stop 的 SIGTERM 能被 headless 收到（A3）
exec "$@"
