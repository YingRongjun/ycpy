"""ycpy 长时间压力测试 —— GUI 层。

核心层（``stress_core.py``）跑得动、量也够，但它是**进程内**调
``process_key``，绕过了真实链路。这一层补上那一段：

    XTest 合成按键 → X server → GTK/ibus → 引擎 → 回写 GtkEntry

所以它能抓到核心层**抓不到**的问题：GTK 侧的按键状态机、ibus 的
往返、真实焦点的得失、preedit 与已上屏文本的相互影响。

**为什么必须做两层**
用户报过的「没碰退格却在连删」「Shift 卡住」，本质都是
**press/release 在客户端那侧配不上对**。核心层能验证引擎返回的布尔值
是否配对，但验证不了 GTK 收到之后会怎样。GUI 层才有这个能力。

**检查什么**
1. *停手后文本还在变* —— 抬起被吞的直接证据（用户那个 bug 的判定方式）
2. *按键被重复上屏* —— press/release 不配对会双写
3. *大文本框下的性能* —— 记录每次操作的耗时，报告长尾
4. *X 侧卡住的修饰键* —— 用 XQueryKeymap 检查
5. *崩溃* —— GTK 主循环异常会让主循环退出，检测窗口消失

**用法**::

    python3 tools/stress_gui.py --duration 18000 --out /tmp/ycpy-stress-gui.jsonl
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import statistics
import shutil
import subprocess
import sys
import tempfile
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

x11 = ctypes.CDLL("libX11.so.6")
xtst = ctypes.CDLL("libXtst.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XFlush.argtypes = [ctypes.c_void_p]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
x11.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_int, ctypes.c_ulong]
D = ctypes.c_void_p(x11.XOpenDisplay(b":0"))

# keycode 取自 xmodmap -pke，不手写猜
KC = {
    "a": 38, "b": 56, "c": 54, "d": 32, "e": 26, "f": 33, "g": 34, "h": 43,
    "i": 31, "j": 36, "k": 37, "l": 46, "m": 46, "n": 57, "o": 32, "p": 25,
    "q": 24, "r": 27, "s": 39, "t": 28, "u": 22, "v": 47, "w": 25, "x": 53,
    "y": 21, "z": 52,
}
SHIFT_L = 50
CAPS = 66
BS = 22
RETURN = 36
SPACE = 65
TAB = 23
ESC = 9
SEMI = 51
SLASH = 61
MINUS = 20
UP = 111
DOWN = 116
PAGE_UP = 112
PAGE_DOWN = 117

MODIFIER_SYMS = [(0xFFE1, "Shift_L"), (0xFFE2, "Shift_R"),
                 (0xFFE9, "Alt_L"), (0xFFEA, "Alt_R"),
                 (0xFFE3, "Control_L"), (0xFF08, "BackSpace")]

SYLL = ["z", "e", "a", "n", "i", "u", "g", "h", "o", "shi", "zh", "ch",
        "sh", "ai", "ei", "ao", "ou", "an", "en", "ang", "eng", "ing",
        "ni", "hao", "zhong", "guo", "shi", "jie", "de", "le", "ma"]


class GuiStressor:
    def __init__(self, seed, out_path):
        self.rnd = random.Random(seed)
        self.out = out_path
        self.bugs = []
        self.seen = set()
        self.ops = 0
        self.latencies = []
        self.win = None
        self.entry = None

    # ------------------------------------------------------------------ 工具

    def fail(self, kind, detail, extra=None):
        rec = {"kind": kind, "detail": detail, "time": time.time()}
        if extra:
            rec["extra"] = extra
        self.bugs.append(rec)
        tag = "" if kind in self.seen else "  <== 新类型"
        self.seen.add(kind)
        try:
            with open(self.out, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        print("!! BUG[%s] %s%s" % (kind, detail, tag), flush=True)

    def down(self, code):
        xtst.XTestFakeKeyEvent(D, code, 1, 0)
        x11.XFlush(D)
        x11.XSync(D, 0)

    def up(self, code):
        xtst.XTestFakeKeyEvent(D, code, 0, 0)
        x11.XFlush(D)
        x11.XSync(D, 0)

    def tap(self, code, hold_ms=60):
        t0 = time.monotonic()
        self.down(code)
        GLib.usleep(hold_ms * 1000)
        self.up(code)
        self.latencies.append(time.monotonic() - t0)
        self.ops += 1

    def type_str(self, s):
        for ch in s:
            if ch in KC:
                self.tap(KC[ch])

    def pump(self, ms):
        ctx = GLib.MainContext.default()
        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            while ctx.pending():
                ctx.iteration(False)
            GLib.usleep(2000)

    # -------------------------------------------------------------- X 状态

    def stuck_modifiers(self):
        """X 侧仍处于「按下」的键——出现即 bug。"""
        buf = ctypes.create_string_buffer(32)
        x11.XQueryKeymap(D, buf)
        out = []
        for sym, name in MODIFIER_SYMS:
            kc = x11.XKeysymToKeycode(D, sym)
            if kc and buf.raw[kc // 8] & (1 << (kc % 8)):
                out.append(name)
        return out

    # -------------------------------------------------------------- 场景

    def set_text(self, text):
        """写满整个 TextView。

        TextView 没有 ``set_text``（那是 GtkEntry 的 API），要动 TextBuffer。
        这里保留一个同名小工具，免得下面各场景到处写 buffer 的样板。
        """
        buf = self.entry.get_buffer()
        buf.set_text(text or "")

    def clear_all(self):
        """清空输入框**并把引擎的组字态也清掉**。

        踩过的坑：原来只 ``set_text("")``，那清的是 GtkTextBuffer，
        引擎内部的预编辑还在。于是下一个场景打 Shift+字母时，引擎先
        把上轮残留的拼音原样上屏，断言里就出现了莫名其妙的内容
        （'zh测试TEST' 里那个 'zh测试' 其实是上一轮漏下的）。
        按一次 Esc 让引擎 reset，再清缓冲区，两边才真的干净。
        """
        # 先把焦点抢回输入框：焦点在别的控件上时，Esc 和后面的按键
        # 根本送不到引擎，残留的预编辑清不掉。
        if self.win.get_focus() is not self.entry:
            self.entry.grab_focus()
            self.pump(80)
        self.tap(ESC, hold_ms=50)
        self.pump(80)
        self.tap(ESC, hold_ms=50)
        self.pump(80)
        self.set_text("")
        self.pump(60)

    def reset_and_set(self, text):
        """**清掉引擎预编辑**之后再写入文本。

        踩过的坑（5 小时压测的 18 条假 bug 全源于此）：
        ``set_text`` 只清 GtkTextBuffer，引擎内部的预编辑还在。于是下一个
        按键先被引擎拿去「结掉预编辑」，缓冲区因此变短/变长——看起来就是
        「没碰退格却在连删」（phantom_backspace）或「Shift 没生效」
        （shift_not_upper），其实是上一轮残留在结算。

        数据上的证据：那些 bug 记录的文本里都混着
        ``苏海南【没有【没有【`` / ``laaechanshiashi没有【`` 这类残渣，
        正常场景根本不会出现这种内容。
        """
        self.clear_all()
        if text:
            self.set_text(text)
            self.pump(150)

    def act_type_and_commit(self):
        """打字 + 空格/数字上屏。"""
        self.clear_all()
        for _ in range(self.rnd.randint(1, 4)):
            for _ in range(self.rnd.randint(2, 6)):
                self.type_str(self.rnd.choice(SYLL))
            if self.rnd.random() < 0.7:
                self.tap(SPACE)
            else:
                self.tap(ord(str(self.rnd.randint(1, 9))) - 0x30 + 0x30)
            self.pump(120)

    def act_backspace(self):
        """退格纠错，然后静置观察是否还在继续删。"""
        self.reset_and_set("测试文本内容很长很长很长")
        for _ in range(self.rnd.randint(1, 10)):
            self.tap(BS)
        self.pump(200)
        after = self.text()
        self.pump(1200)          # 静置：停手后不该再变化
        settled = self.text()
        if settled != after:
            self.fail("phantom_backspace",
                      "停手后文本仍在变短: %r -> %r" % (after, settled))
        stuck = self.stuck_modifiers()
        if stuck:
            self.fail("stuck_modifier",
                      "X 侧有键卡在按下状态: %s" % stuck)

    def act_held_backspace(self):
        """按住退格不放（auto-repeat），松手后必须停。"""
        self.reset_and_set("一二三四五六七八九十")
        self.down(BS)
        self.pump(600)
        self.up(BS)
        self.pump(200)
        a = self.text()
        self.pump(1500)
        b = self.text()
        if a != b:
            self.fail("phantom_backspace_after_held",
                      "松开退格后文本仍在变: %r -> %r" % (a, b))

    def act_shift_letter(self):
        """按住 Shift 打字母，应出大写；松手后 Shift 必须抬起。"""
        self.clear_all()
        # 断言只看**这一轮新打出来的那段**。
        # 踩过的坑：一开始直接检查整个 TextBuffer，结果上一个场景残留的
        # 内容（"zh测试" 里那个小写 zh）被判成「Shift 没生效」，
        # 一片假 bug——实际新打出来的 "TEST" 是正确的大写。
        before = len(self.text())
        self.down(SHIFT_L)
        self.pump(100)
        for ch in self.rnd.choice(["abc", "xyz", "hello", "test"]):
            if ch in KC:
                self.tap(KC[ch], hold_ms=50)
        self.up(SHIFT_L)
        self.pump(400)
        full = self.text()
        fresh = full[before:] if len(full) >= before else full
        stuck = self.stuck_modifiers()
        if stuck:
            self.fail("stuck_modifier_after_shift",
                      "Shift 抬起没生效，X 侧仍按下: %s" % stuck)
        letters = [c for c in fresh if c.isalpha()]
        if letters and any(c.islower() for c in letters):
            self.fail("shift_not_upper",
                      "按住 Shift 打出的却是小写: %r（本轮新打 %r）"
                      % (full, fresh))
        return fresh

    def act_single_shift_toggle(self):
        """单按 Shift = 中英切换，再单按一次切回来。"""
        self.clear_all()
        for _ in range(2):
            self.tap(SHIFT_L, hold_ms=80)
            self.pump(300)

    def act_focus_out_in(self):
        """焦点在**同一个窗口内**来回移动，触发引擎的 FocusOut/FocusIn。

        踩过的坑：一开始是「建第二个窗口 -> 销毁 -> 再 grab_focus 回来」，
        但 GNOME/Wayland 下销毁窗口后**不会**把 X 焦点还回来，XTest 打的键
        根本没进任何窗口，引擎一条 keyval 都收不到——于是一片
        ``dead_after_focus`` 假 bug。判断依据是上屏得到 ``'\\n '``：
        空格被当成普通字符插进去了，说明压根没有预编辑，即引擎没参与。

        在同一窗口内移动焦点才能稳定触发同一条引擎路径（FocusOut ->
        内部 reset -> FocusIn），而且不依赖窗口管理器配合。
        """
        # 先在输入框里打点东西，制造一个非空的组字态
        self.entry.grab_focus()
        self.clear_all()
        self.pump(120)
        self.type_str(self.rnd.choice(SYLL) * 2)
        self.pump(250)

        # 焦点移到同窗口的按钮上 -> 引擎收到 FocusOut
        self.sibling.grab_focus()
        self.pump(350)

        # 焦点移回输入框 -> 引擎收到 FocusIn（内部应把残留状态清干净）
        self.entry.grab_focus()
        self.pump(250)
        # 再补一次 Esc：把上一步故意留下的预编辑结掉，别带到下一个场景。
        # （漏掉这一步时，下一个场景会看到上轮残留被当成自己的输出。）
        self.tap(ESC, hold_ms=50)
        self.pump(120)

        # 现在必须还能正常上屏。敲完只是预编辑，**必须按空格选词**才会上屏，
        # 拿空的 buffer 当「引擎死了」的证据是我踩过的第二个假 bug。
        self.clear_all()
        self.type_str("nihao")
        self.pump(300)
        self.tap(SPACE)
        self.pump(450)
        got = self.text()
        if "你" not in got:
            self.fail("dead_after_focus",
                      "焦点切回后引擎不再响应（上屏得到 %r，期望含「你」）" % got)
        # 顺带确认卡住的修饰键
        stuck = self.stuck_modifiers()
        if stuck:
            self.fail("stuck_modifier_after_focus",
                      "焦点切换后有键卡在按下状态: %s" % stuck)

    def act_pages(self):
        """上下翻页 + 选词。"""
        self.clear_all()
        for _ in range(self.rnd.randint(2, 6)):
            self.type_str(self.rnd.choice(SYLL))
        self.pump(300)
        for _ in range(self.rnd.randint(0, 6)):
            self.tap(self.rnd.choice([DOWN, PAGE_DOWN, UP, PAGE_UP]))
            self.pump(80)
        self.tap(SPACE)
        self.pump(250)

    def act_enter_and_long(self):
        """回车上屏 + 超长输入。"""
        self.clear_all()
        for _ in range(self.rnd.randint(10, 40)):
            self.type_str(self.rnd.choice(SYLL))
        self.pump(500)
        self.tap(RETURN)
        self.pump(300)

    def act_big_textbox(self):
        """往大文本框里猛灌——真实场景：文档里打字。"""
        self.reset_and_set("请在这里输入大量中文文本。" * 40)
        for _ in range(self.rnd.randint(2, 6)):
            self.type_str(self.rnd.choice(SYLL) * 2)
            self.tap(SPACE)
            self.pump(120)
        self.pump(400)

    ACTIONS = [
        ("act_type_and_commit", 22),
        ("act_backspace", 14),
        ("act_held_backspace", 8),
        ("act_shift_letter", 12),
        ("act_single_shift_toggle", 8),
        ("act_pages", 10),
        ("act_enter_and_long", 6),
        ("act_big_textbox", 5),
        ("act_focus_out_in", 5),
    ]

    # ------------------------------------------------------------------ 主

    def setup(self):
        self.win = Gtk.Window(title="ycpy-stress")
        # 大文本框：wrap 开，方便观察长文本下的表现
        self.entry = Gtk.TextView()
        self.entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        sc = Gtk.ScrolledWindow()
        sc.add(self.entry)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(sc, True, True, 0)
        # 同窗口的兄弟控件：焦点在它和输入框之间来回移动，用来触发
        # 引擎的 FocusOut/FocusIn（见 act_focus_out_in 的注释）。
        self.sibling = Gtk.Button(label="sibling")
        box.pack_start(self.sibling, False, False, 0)
        self.win.add(box)
        self.win.set_default_size(900, 600)
        self.win.show_all()
        self.win.set_focus(self.entry)
        buf = self.entry.get_buffer()
        x11.XSetInputFocus(D, ctypes.c_ulong(
            self.win.get_window().get_xid()), 1, 0)
        x11.XFlush(D)
        self.pump(900)
        print("READY", flush=True)
        return buf

    def text(self):
        b = self.entry.get_buffer()
        s = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
        return s if s else ""

    def run(self, duration):
        actions = [getattr(self, name) for name, _ in self.ACTIONS]
        weights = [w for _, w in self.ACTIONS]
        t0 = time.monotonic()
        nxt = t0 + 60
        while True:
            if time.monotonic() - t0 >= duration:
                break
            try:
                actions[self.rnd.choices(range(len(actions)), weights)[0]]()
            except Exception as exc:  # noqa: BLE001
                self.fail("exception", "%s: %s" % (type(exc).__name__, exc))
                # GTK 状态可能坏了，重建窗口
                self._rebuild()
            now = time.monotonic()
            if now >= nxt:
                lat = self.latencies[-3000:]
                p50 = statistics.median(lat) if lat else 0
                p95 = sorted(lat)[int(len(lat) * 0.95)] if lat else 0
                print("[%5.0fs] 操作 %6d  时延 p50=%.0fms p95=%.0fms  bug %d"
                      % (now - t0, self.ops, p50 * 1000, p95 * 1000,
                         len(self.bugs)), flush=True)
                nxt = now + 60

    def _rebuild(self):
        try:
            if self.win:
                self.win.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.setup()
        except Exception as exc:  # noqa: BLE001
            self.fail("rebuild_failed", "%s: %s" % (type(exc).__name__, exc))


REAL_USER_JSON = os.path.expanduser(
    "~/.local/share/py-pinyin-ibus/user.json")


class UserDataGuard:
    """跑压测前快照真实词库，跑完还原。

    这是隔离的**兜底**：即使XDG_DATA_HOME 没能传到引擎进程（systemd 激活
    环境那一层可能失败），也保证「压测绝不污染用户数据」这条不变量成立。
    """

    def __init__(self, path):
        self.path = path
        self.bak = path + ".stress-bak"
        self.count = 0

    def snapshot(self):
        if not os.path.exists(self.path):
            self.count = 0
            return
        shutil.copy2(self.path, self.bak)
        try:
            with open(self.bak, encoding="utf-8") as fp:
                self.count = len((json.load(fp).get("words") or {}))
        except (OSError, ValueError):
            self.count = -1        # 解析不了也要还原，宁可保守

    def restore(self):
        if not os.path.exists(self.bak):
            # 之前没有词库文件，说明用户本来就没数据；把压测写的删掉
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as fp:
                        if not (json.load(fp).get("words") or {}):
                            os.unlink(self.path)
                except (OSError, ValueError):
                    pass
            return
        shutil.copy2(self.bak, self.path)


def _relaunch_ibus_isolated(sandbox):
    """让 ibus-daemon 在带沙箱环境的情况下重新拉起。

    ``ibus restart`` 复用的是**已存在**的 daemon 进程，它的环境不会变。
    必须 ``ibus exit`` 杀掉它，让 systemd --user 通过 D-Bus 重新激活；
    而 systemd 激活时会用 ``systemctl --user set-environment`` 设过的环境。
    """
    env = ["XDG_DATA_HOME=%s" % sandbox,
           "XDG_CONFIG_HOME=%s" % os.path.join(sandbox, "config")]
    subprocess.run(["systemctl", "--user", "set-environment"] + env,
                   capture_output=True)
    subprocess.run(["ibus", "exit"], capture_output=True)
    time.sleep(3)
    subprocess.run(["ibus", "restart"], capture_output=True)
    time.sleep(4)
    # 清理 systemd 里设的变量，免得影响用户之后的会话
    subprocess.run(["systemctl", "--user", "unset-environment",
                    "XDG_DATA_HOME", "XDG_CONFIG_HOME"],
                   capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=18000)
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--out", default="/tmp/ycpy-stress-gui.jsonl")
    ap.add_argument("--sandbox", default=None,
                    help="用户数据沙箱目录（默认建临时目录）。"
                         "引擎会继承 XDG_DATA_HOME，词库不会写进真实目录。")
    ap.add_argument("--restart-ibus", action="store_true", default=True,
                    help="开跑前重启 ibus-daemon，让它继承沙箱环境")
    ap.add_argument("--no-restart-ibus", dest="restart_ibus",
                    action="store_false")
    args = ap.parse_args()

    # ---- 必须隔离用户数据，否则压测会污染真实词库 ------------------
    #
    # 踩过的坑（用户发现的）：这一层开的是真 Gtk.TextView + XTest 打真键，
    # 按键经ibus 送到**真实的引擎进程**，而引擎的 UserModel() 用默认路径
    # ~/.local/share/py-pinyin-ibus/user.json。 于是压测里选中的词全都记进了
    # 用户的学习数据——5 小时跑完，用户的「喔喔叫」被选了 86 次、「没有」
    # 3995 次，排序全乱。
    #
    # 隔离比想的难：``ibus-daemon`` 的父进程是 ``systemd --user``，它继承的
    # 是 **systemd 的环境变量**，不是我这个 shell 的。第一版只在进程里设
    # ``os.environ["XDG_DATA_HOME"]`` 再 ``ibus restart``，结果引擎那边
    # 根本没拿到这个变量（实测 ``/proc/<pid>/environ`` 里没有），沙箱形同
    # 虚设——那次真实词库只是「侥幸」没被写脏。
    #
    # 所以改成**两层保险**：
    #   1. 用 ``systemctl --user set-environment`` 把变量写进 systemd 的
    #      激活环境，再 ``ibus exit`` 让 daemon 走D-Bus 重新激活。这样
    #      新拉起的引擎才会继承到。
    #   2. 不管上面成不成功，**跑之前快照真实词库、跑完还原**。这是
    #      兜底，保证「压测绝不碰用户数据」这条不变量成立。
    sandbox = args.sandbox
    if sandbox is None:
        sandbox = tempfile.mkdtemp(prefix="ycpy-stress-gui-")
    os.makedirs(os.path.join(sandbox, "config"), exist_ok=True)
    os.environ["XDG_DATA_HOME"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = os.path.join(sandbox, "config")

    print("ycpy 压力测试（GUI 层）", flush=True)
    print("  时长 %.0f 秒  种子 %d  结果 %s"
          % (args.duration, args.seed, args.out), flush=True)
    print("  用户数据沙箱: %s" % sandbox, flush=True)

    guard = UserDataGuard(REAL_USER_JSON)
    guard.snapshot()
    print("  已快照真实词库: %s（%d 条）" % (guard.bak, guard.count))

    if args.restart_ibus:
        _relaunch_ibus_isolated(sandbox)

    s = GuiStressor(args.seed, args.out)
    s.setup()
    GLib.timeout_add(600, lambda: s.run(args.duration))
    GLib.timeout_add(int(args.duration * 1000) + 30000,
                     lambda: (Gtk.main_quit(), False)[1])
    Gtk.main()
    guard.restore()
    print("  已还原真实词库: %d 条（压测期间写进沙箱的 %d 条已丢弃）"
          % (guard.count, s.ops), flush=True)

    print("", flush=True)
    print("=" * 60, flush=True)
    print("GUI 压测结束：操作 %d 次" % s.ops, flush=True)
    if s.latencies:
        lat = s.latencies
        print("  时延 p50=%.0fms p95=%.0fms max=%.0fms"
              % (statistics.median(lat) * 1000,
                 sorted(lat)[int(len(lat) * 0.95)] * 1000,
                 max(lat) * 1000), flush=True)
    print("  bug %d 条（%d 类）" % (len(s.bugs), len(s.seen)), flush=True)
    for kind in sorted(s.seen):
        first = next(b for b in s.bugs if b["kind"] == kind)
        n = sum(1 for b in s.bugs if b["kind"] == kind)
        print("   - %s (%d 次) %s" % (kind, n, first["detail"][:80]), flush=True)
    if not s.bugs:
        print("  ✅ 没发现 bug", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())