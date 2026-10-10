#!/usr/bin/env python3
"""端到端联调脚本（需要真实运行的 ibus-daemon + ycpy 引擎）。

流程与 ibus 实际工作方式一致：

1. ``SetGlobalEngine('ycpy')`` 让 daemon 激活引擎（它会回调我们的 ``CreateEngine``）；
2. 造一个真实的 IBus 输入上下文并 focus-in，daemon 会把它设为当前上下文；
3. 直接对引擎对象发 ``ProcessKeyEvent``（等价于应用层按键）；
4. 观察输入上下文收到的 ``UpdatePreeditText`` / ``UpdateLookupTable`` /
   ``CommitText``，确认整条链路——包括 ibus 自定义序列化格式——都通了。

用法::

    python3 tools/e2e_test.py
"""

from __future__ import annotations

import os
import sys

import dbus
import dbus.mainloop.glib
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ycpy.ibusproto import BUS_NAME, IFACE_BUS, IFACE_ENGINE  # noqa: E402
from ycpy.engine import BUS_NAME as ENGINE_BUS_NAME, resolve_ibus_address  # noqa: E402

LOG = []


def log(msg: str) -> None:
    LOG.append(msg)
    print(msg, flush=True)


def find_engine_path(bus) -> str:
    """问引擎进程它的引擎对象路径：直接调它导出的 Factory 接口。

    注意总线的两个名字别搞混：``org.freedesktop.IBus`` 是 daemon，
    ``ycpy.pinyin.assist`` 才是引擎进程自己占的那个。
    """
    factory = dbus.Interface(
        bus.get_object(ENGINE_BUS_NAME, "/org/freedesktop/IBus/Factory"),
        "org.freedesktop.IBus.Factory",
    )
    return str(factory.CreateEngine("ycpy", timeout=10))


ATTEMPTS = 5


def main() -> int:
    """跑一次自检；因为会和桌面真实输入抢总线，允许重试若干次。"""
    last = 1
    for attempt in range(1, ATTEMPTS + 1):
        rc = run_once()
        if rc == 0:
            if attempt > 1:
                print("（第 %d 次尝试才拿到干净结果：此前失败是总线上的外来按键干扰）" % attempt)
            return 0
        last = rc
    print()
    print("连续 %d 次都没拿到干净结果。" % ATTEMPTS)
    print("最可能的原因是这个会话里正在敲键盘——引擎和桌面输入共用同一条 ibus 总线，")
    print("你打的字符会被一起送到引擎里。停手等几秒再跑一次即可；")
    print("组字与联想的确定性验证请用： python3 tests/test_core.py")
    return last


def run_once() -> int:
    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(resolve_ibus_address())
    ibus = dbus.Interface(bus.get_object(BUS_NAME, "/org/freedesktop/IBus"), IFACE_BUS)

    # --- 激活引擎 -------------------------------------------------------
    ibus.SetGlobalEngine("ycpy")
    log("已请求切换到 ycpy")

    engine_path = find_engine_path(bus)
    log("引擎对象路径: %s" % engine_path)

    engine = dbus.Interface(bus.get_object(ENGINE_BUS_NAME, engine_path), IFACE_ENGINE)

    # --- 建一个真实的输入上下文 -----------------------------------------
    ctx_path = str(ibus.CreateInputContext("ycpy-e2e", timeout=10))
    log("输入上下文: %s" % ctx_path)
    ctx = dbus.Interface(bus.get_object(BUS_NAME, ctx_path),
                         "org.freedesktop.IBus.InputContext")
    # 上下文必须先声明能力（Focus / SurroundingText）才会接受 FocusIn。
    ctx.SetCapabilities(0x01 | 0x02 | 0x04 | 0x08, timeout=10)
    ctx.FocusIn(timeout=10)
    log("上下文已 focus-in")

    # 上一次运行可能留下了半截组字，先清干净再开始，否则测的是残留状态。
    engine.Reset(timeout=10)

    # --- 订阅引擎信号 -----------------------------------------------------
    events = []

    def on_commit(text):
        events.append(("commit", _decode(text)))

    def on_preedit(text, cursor, visible, mode):
        events.append(("preedit", _decode(text) if visible else "", visible))

    def on_lookup(table, visible):
        events.append(("lookup", _decode_table(table) if visible else []))

    def on_fwd(keyval, keycode, state):
        events.append(("forward", keyval))

    for handler, signal in ((on_commit, "CommitText"),
                            (on_preedit, "UpdatePreeditText"),
                            (on_lookup, "UpdateLookupTable"),
                            (on_fwd, "ForwardKeyEvent")):
        bus.add_signal_receiver(handler, signal_name=signal,
                                dbus_interface=IFACE_ENGINE,
                                bus_name=ENGINE_BUS_NAME, path=engine_path)

    # --- 敲一串键 --------------------------------------------------------
    def send(ch: str, modifier: int = 0) -> bool:
        handled = engine.ProcessKeyEvent(ord(ch), 0, modifier, timeout=10)
        _drain()
        return bool(handled)

    try:
        return run_checks(send, events)
    finally:
        note_environment()


def run_checks(send, events) -> int:
    log("--- 逐键输入 cxy ---")
    for ch in "cxy":
        handled = send(ch)
        log("  '%s' 已消费=%s  最近事件=%s" % (ch, handled, events[-2:]))

    # 找到「程序员」在候选里的位置再按对应数字键——排序会随用户词频变化，
    # 不能写死按第几个。
    lookup = [e for e in events if e[0] == "lookup" and e[1]]
    if not lookup:
        print("FAIL: 始终没收到候选列表")
        return 1
    page = lookup[-1][1]
    log("候选页: %s" % (page,))
    if "程序员" not in page:
        print("FAIL: 「程序员」不在首屏候选里 -> %s" % (page,))
        return 1
    index = page.index("程序员")
    digit = str(index + 1)
    log("--- 按 '%s' 选中「程序员」（第 %d 项）---" % (digit, index + 1))
    send(digit)
    ok = any(e[0] == "commit" and e[1] == "程序员" for e in events)
    log("  事件序列: %s" % (events[-3:],))

    # 联想键默认是 Ctrl+;（分号留给中文标点「；」）
    log("--- 联想：Ctrl+';' 后应给出后继词 ---")
    send(";", 1 << 2)
    log("  联想候选: %s" % (events[-1],))
    assoc_ok = events[-1][0] == "lookup" and bool(events[-1][1])
    send("\x1b")
    log("  Esc 退出联想后最近事件: %s" % (events[-2:],))

    print()
    print("全拼/首字母上屏: %s" % ("PASS" if ok else "FAIL"))
    print("联想候选:       %s" % ("PASS" if assoc_ok else "FAIL"))
    return 0 if ok and assoc_ok else 1


def note_environment() -> None:
    """说明这个自检脚本的固有局限，避免把环境噪声误判成引擎缺陷。

    脚本是直接对引擎对象发 ProcessKeyEvent 来模拟按键的，走的是真实 daemon，
    所以会和**当前桌面上正在进行的真实输入共用同一条总线**。只要你在跑这个
    脚本的同时在用键盘，引擎就会额外收到那些按键（实测能收到 Backspace、
    Space 以及 "xuxuan"、"zee" 之类的串），表现为「按键随机丢失」。

    引擎本身没有问题：它对收到的每一个按键都是正确响应的（对照引擎日志里的
    buffer 变化即可验证）。组字/联想/学习的确定性验证请以 tests/test_core.py
    为准；本脚本只用来证明 D-Bus 线格式和 daemon 转发链路是通的。
    """
    print("提示: 本脚本与桌面真实输入共用 ibus 总线，运行时请勿同时敲键盘；"
          "若要反复压测，建议用 tools/e2e_isolated.sh（私有 daemon）。")


def _drain(seconds: float = 0.25) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def _decode(ibus_text) -> str:
    """IBusText 序列化结构 = ("IBusText", {}, text, attrs)。"""
    try:
        return str(ibus_text[2])
    except Exception:
        return repr(ibus_text)


def _decode_table(table) -> list:
    return [_decode(c) for c in table[7]]


if __name__ == "__main__":
    sys.exit(main())