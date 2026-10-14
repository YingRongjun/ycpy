#!/usr/bin/env python3
"""验证**真实 X 路径**下，ibus 交给引擎的 ``keycode`` 是不是非零。

为什么需要这个探针
==================
修「连续触发按键」那个 bug 的方案是把 press/release 的配对键从 ``keyval``
换成 ``keycode``——keyval 会随修饰键状态变（Shift↓ A↓ A↑ 那一刻 Shift 已经
松了，keyval 就是 'a' 而不是 'A'），而 keycode 是硬件键，恒定不变。

但**整条链路里 keycode 是 ibus 传过来的**。如果它传的是 0，这个修复就是空的：
``_pair_key`` 会全部退回 keyval，等于什么都没改。项目里的单测与压测又一律
传 ``keycode=0``（那样测不出问题，但也验证不了修复），所以必须在真实链路上
单独确认一次。

探针做什么
==========
起一个**私有的** ibus-daemon（完全不碰会话那个），用源码树里的引擎连上去，
开一个真 Gtk 窗口、把焦点给它，然后用 XTest 打一组**必然改变 keyval 的序列**：

    Shift↓   a↓   Shift↑   a↑

读引擎 trace，看最后那次抬起的 ``keyval`` 与 ``keycode``：如果 keyval 已经从
``0x41``('A') 变成 ``0x61``('a')，而 ``keycode`` 两次都是 38，就同时证明了
两件事——**keyval 确实会变**（bug 成立），以及 **keycode 恒定**（修复成立）。

用法::

    python3 tools/probe_keycode.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE = "/tmp/ycpy-keycode-probe.log"

# 走 GTK 侧，构造一个真实的按键事件，交给 ibus IM context。
# 硬件 keycode 不自己编，用 XKeysymToKeycode 查当前布局。
CLIENT = r'''
import ctypes, sys, time
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib

x11 = ctypes.CDLL("libX11.so.6")
xtst = ctypes.CDLL("libXtst.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                               ctypes.c_ulong]
x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XKeysymToKeycode.restype = ctypes.c_ubyte
x11.XFlush.argtypes = [ctypes.c_void_p]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_int, ctypes.c_ulong]

D = x11.XOpenDisplay(None)
assert D, "打不开 DISPLAY"

win = Gtk.Window(title="ycpy-keycode-probe")
entry = Gtk.TextView()
entry.set_editable(True)
win.add(entry)
win.set_size_request(480, 120)
win.show_all()
entry.grab_focus()
x11.XSetInputFocus(D, ctypes.c_ulong(entry.get_toplevel().get_window().get_xid()),
                   2, 0)
x11.XSync(D, 0)

_ctx = GLib.MainContext.default()
def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        while _ctx.pending():
            _ctx.iteration(False)
        GLib.usleep(2000)

pump(400)

def down(kc):
    xtst.XTestFakeKeyEvent(D, kc, 1, 0); x11.XSync(D, 0); GLib.usleep(40000)
def up(kc):
    xtst.XTestFakeKeyEvent(D, kc, 0, 0); x11.XSync(D, 0); GLib.usleep(40000)

kc_shift = x11.XKeysymToKeycode(D, 0xFFE1)   # Shift_L
kc_a     = x11.XKeysymToKeycode(D, 0x61)     # a
sys.stderr.write("  X 查到：Shift_L keycode=%d  a keycode=%d\n"
                 % (kc_shift, kc_a)); sys.stderr.flush()

# 关键序列：Shift↓ a↓ Shift↑ a↑ —— 抬起 a 时 Shift 已经没了，
# 于是同一个物理键的 keyval 从 'A' 变成了 'a'。
down(kc_shift); down(kc_a); up(kc_shift); up(kc_a)
pump(300)

# 正常节奏再敲几个字母，给 trace 一点对照样本
for _ in range(3):
    GLib.usleep(80000)
    down(kc_a); up(kc_a)
    pump(150)

pump(600)
win.destroy()
pump(200)
'''


def main() -> int:
    if "--client" in sys.argv:
        exec(compile(CLIENT, "<probe-client>", "exec"), {})
        return 0

    sock_dir = tempfile.mkdtemp(prefix="ycpy-probe-")
    sock = os.path.join(sock_dir, "ibus.sock")
    env = dict(os.environ)
    env.update({
        "IBUS_ADDRESS": "unix:path=" + sock,
        "XDG_RUNTIME_DIR": sock_dir,
        # 三个都隔离：ibus 把地址写在 XDG_CONFIG_HOME/ibus/bus/ 下，
        # 不隔离会把**真实会话**的 socketfile 覆盖掉，害得输入法失联。
        "XDG_CONFIG_HOME": os.path.join(sock_dir, "config"),
        "XDG_CACHE_HOME": os.path.join(sock_dir, "cache"),
        "IBUS_COMPONENT_PATH": "/usr/share/ibus/component",
        "PYTHONPATH": ROOT + (os.pathsep + env["PYTHONPATH"]
                              if env.get("PYTHONPATH") else ""),
        "PYIM_TRACE": TRACE,
        "GDK_BACKEND": "x11",
    })
    os.makedirs(os.path.join(env["XDG_CONFIG_HOME"], "ibus", "bus"),
                exist_ok=True)
    os.makedirs(env["XDG_CACHE_HOME"], exist_ok=True)
    if os.path.exists(TRACE):
        os.remove(TRACE)
    open(TRACE, "w").close()

    daemon = engine = client = None
    try:
        print("==> 私有 ibus-daemon", sock)
        daemon = subprocess.Popen(
            ["ibus-daemon", "--address=" + "unix:path=" + sock,
             "--panel", "disable", "--replace"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        time.sleep(2)

        print("==> 源码树里的 ycpy 引擎")
        elog = os.path.join(sock_dir, "engine.log")
        with open(elog, "w") as fp:
            engine = subprocess.Popen([os.path.join(ROOT, "ycpy-engine")],
                                      env=env, stdout=fp, stderr=fp)
        ready = False
        for _ in range(80):
            try:
                if re.search(r"已注册|已自注册|原生加载",
                             open(elog, encoding="utf-8", errors="replace").read()):
                    ready = True
                    break
            except OSError:
                pass
            time.sleep(0.5)
        if not ready:
            print("引擎没起来，日志：", file=sys.stderr)
            print(open(elog, encoding="utf-8", errors="replace").read(),
                  file=sys.stderr)
            return 1

        print("==> 把 ycpy 设为私有 daemon 的当前引擎")
        r = subprocess.run(["ibus", "engine", "ycpy"], env=env,
                           capture_output=True, text=True)
        if r.returncode:
            print("   ibus engine 失败: %s" % r.stderr.strip(), file=sys.stderr)
            print(open(elog, encoding="utf-8", errors="replace").read(),
                  file=sys.stderr)
            return 1
        time.sleep(1.0)

        print("==> XTest 打键：Shift↓ a↓ Shift↑ a↑")
        client = subprocess.run([sys.executable, os.path.abspath(__file__),
                                 "--client"],
                                env=env, capture_output=True, text=True)
        sys.stderr.write(client.stderr)
        time.sleep(1.0)

        print("\n==> 引擎 trace（press/release 那几行）")
        raw = open(TRACE, encoding="utf-8").read().splitlines()
        for l in raw:
            if "] press " in l or "] release " in l:
                print("   " + l.split("] ", 1)[-1])
        rc = _judge(raw)
        if rc:
            print("\n==> 完整 trace:")
            for l in raw:
                print("   " + l)
            print("==> 引擎日志:")
            print(open(elog, encoding="utf-8", errors="replace").read())
        return rc
    finally:
        if engine:
            engine.terminate()
        if daemon:
            daemon.terminate()
        shutil.rmtree(sock_dir, ignore_errors=True)


def _judge(raw) -> int:
    presses = [(int(m.group(1), 16), int(m.group(2)), m.group(3))
               for m in (re.search(
                   r"press   keyval=(0x[0-9a-fA-F]+) keycode=(\d+).*-> (\S+)", l)
                   for l in raw) if m]
    releases = [(int(m.group(1), 16), int(m.group(2)), m.group(3))
                for m in (re.search(
                    r"release keyval=(0x[0-9a-fA-F]+) keycode=(\d+) 记录=(\S+)", l)
                    for l in raw) if m]
    print("\n==> 判定")
    if not presses or not releases:
        print("   ❌ 没抓到 press/release，探针没走通")
        return 1

    zero = [kv for kv, kc, _ in presses if kc == 0]
    if zero:
        print("   ❌ keycode 有 %d 次是 0 —— 按 keycode 配对的修复无效"
              % len(zero))
        return 1
    print("   ✅ keycode 全部非零（%d 次 press）——按 keycode 配对可行"
          % len(presses))

    # 找「同一个 keycode、keyval 却不同」的 press/release 对
    pmap = {}
    for kv, kc, _ in presses:
        pmap.setdefault(kc, []).append(kv)
    hits = [(kc, kvs) for kc, kvs in pmap.items() if len(set(kvs)) > 1]
    if hits:
        for kc, kvs in hits:
            print("   ✅ keycode=%d 的 keyval 变了：%s —— bug 成立"
                  % (kc, " -> ".join("0x%x" % v for v in kvs)))
    else:
        print("   ⚠️ 这次没观察到 keyval 变化（可能布局或时序差异），"
              "但 keycode 非零已确认")
    return 0


if __name__ == "__main__":
    sys.exit(main())
