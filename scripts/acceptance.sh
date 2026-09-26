#!/usr/bin/env sh
# 同板两次观测的复核编号隔离验收（需在装有 Docker Compose 的环境运行）：
#
#   1) 构建并启动 app 服务（Compose），等待健康检查通过；
#   2) 在运行中的服务上依次提交同板两次合法观测（仅 A 失效 / 仅 C 失效），
#      保留首条编号，查询两条记录，核对输入、选择向量与逐校验复算各自独立；
#   3) 重启 app 服务，再次核对首条记录（及第二条），结论仍须分别定位
#      A / C 且证据不被后续提交或重启改变。
#
# 任一步失败即以非零退出码退出。
set -eu

# 验收脚本在 app 容器内执行（镜像已内置 tests/），经回环访问本服务。
BASE="http://127.0.0.1:8080"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

wait_healthy() {
  echo "等待 app 健康..."
  i=0
  while [ "$i" -lt 60 ]; do
    if docker compose exec -T app python -c \
        'import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=3).status==200 else 1)' \
        >/dev/null 2>&1; then
      echo "app 已健康"
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  echo "app 未在限定时间内变为健康" >&2
  docker compose logs app >&2 || true
  return 1
}

echo "== [1/3] 构建并启动 app 服务 =="
docker compose up -d --build app
wait_healthy

echo "== [2/3] 提交两次观测并核对两条记录的隔离性 =="
if ! docker compose exec -T app \
      python tests/acceptance_api.py phase1 "$BASE" >"$TMP" 2>&1; then
  cat "$TMP" >&2
  exit 1
fi
cat "$TMP"
RIDS="$(sed -n 's/^RID=//p' "$TMP" | tail -n1)"
RID_A="$(printf '%s' "$RIDS" | awk '{print $1}')"
RID_C="$(printf '%s' "$RIDS" | awk '{print $2}')"
[ -n "$RID_A" ] || { echo "未取得首条复核编号" >&2; exit 1; }
echo "首条编号（观测 1，仅 A）: $RID_A"
echo "第二条编号（观测 2，仅 C）: $RID_C"

echo "== [3/3] 重启 app 服务后再次核对 =="
docker compose restart app
wait_healthy

if ! docker compose exec -T app \
      python tests/acceptance_api.py phase2 "$BASE" "$RID_A" "$RID_C" >"$TMP" 2>&1; then
  cat "$TMP" >&2
  exit 1
fi
cat "$TMP"

echo "== 验收全部通过：同板两次观测的编号永久隔离，重启后首条仍定位 A =="
