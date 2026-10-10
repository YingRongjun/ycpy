#!/usr/bin/env bash
# 在一个**私有**的 ibus-daemon 上跑端到端自检。
#
# 为什么不用现成的会话 daemon？因为 e2e_test.py 每运行一次就会在 daemon 里留下
# 一个输入上下文（Destroy 与断开连接都回收不掉，实测对象会一直留在总线上）。
# 这些上下文会互相抢焦点，daemon 于是给引擎发额外的 FocusOut/Reset，
# 表现为「按键随机丢失」——那是测试环境自己制造的噪声，会掩盖真实结论。
#
# 起一个干净的私有 daemon，问题就消失了，而且完全不碰用户的会话。

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(dirname "$HERE")"
REAL_XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
SOCK_DIR="$(mktemp -d)"
SOCK="$SOCK_DIR/ibus.sock"
export IBUS_ADDRESS="unix:path=$SOCK"
export XDG_RUNTIME_DIR="$SOCK_DIR"
# 必须连 XDG_CONFIG_HOME 一起隔离：ibus 把自己的地址写在
# $XDG_CONFIG_HOME/ibus/bus/<machine-id>-<host>-<display> 里，不隔离的话
# 私有 daemon 会把**真实会话**的 socketfile 覆盖掉，导致会话里的输入法失联。
export XDG_CONFIG_HOME="$SOCK_DIR/config"
export XDG_CACHE_HOME="$SOCK_DIR/cache"
mkdir -p "$XDG_CONFIG_HOME/ibus/bus" "$XDG_CACHE_HOME"
# 组件目录也带上，私有 daemon 才和真实会话一样原生加载 ycpy 组件
export IBUS_COMPONENT_PATH="/usr/share/ibus/component:${REAL_XDG_DATA:-$HOME/.local/share}/ibus/component"

cleanup() {
    # 只杀本次启动的那个引擎进程。
    # 千万别用 `pkill -f "python3.*-m ycpy"`——那会把用户会话里正在用的
    # 引擎一起杀掉，输入法当场失联。
    [ -n "${ENGINE_PID:-}" ] && kill "$ENGINE_PID" 2>/dev/null || true
    [ -n "${DAEMON_PID:-}" ] && kill "$DAEMON_PID" 2>/dev/null || true
    rm -rf "$SOCK_DIR"
}
trap cleanup EXIT

echo "==> 启动私有 ibus-daemon（$SOCK）"
ibus-daemon --address="unix:path=$SOCK" --panel disable --daemonize --replace \
    >/dev/null 2>&1 || {
        echo "私有 ibus-daemon 启动失败" >&2
        exit 1
}
sleep 2

echo "==> 启动 ycpy 引擎"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$ROOT/ycpy-engine" >"$SOCK_DIR/engine.log" 2>&1 &
ENGINE_PID=$!

# XDG_CACHE_HOME 被隔离了，索引缓存是冷的，词典重建要几秒，
# 所以轮询等待就绪，而不是死等固定秒数。
for _ in $(seq 1 60); do
    grep -qE "已注册|已自注册|原生加载" "$SOCK_DIR/engine.log" 2>/dev/null && break
    sleep 0.5
done

if ! grep -qE "已注册|已自注册|原生加载" "$SOCK_DIR/engine.log" 2>/dev/null; then
    echo "引擎启动失败，日志：" >&2
    cat "$SOCK_DIR/engine.log" >&2
    exit 1
fi

echo "==> 跑端到端自检"
set +e
python3 "$HERE/e2e_test.py"
rc=$?
set -e

if [ $rc -ne 0 ]; then
    echo
    echo "==> 引擎日志："
    cat "$SOCK_DIR/engine.log"
fi

echo
echo "==> 再跑 8 遍确认稳定性（隔离环境下应当零失败）"
stable=0
for i in $(seq 1 8); do
    if python3 "$HERE/e2e_test.py" >/dev/null 2>&1; then
        stable=$((stable+1))
    else
        echo "    第 $i 遍失败"
    fi
done
echo "    通过 $stable/8"

if [ $rc -ne 0 ] || [ "$stable" -ne 8 ]; then
    echo
    echo "引擎日志："
    cat "$SOCK_DIR/engine.log"
    exit 1
fi

echo
echo "PASS: 隔离环境下端到端链路稳定"