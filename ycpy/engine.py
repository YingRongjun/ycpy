"""IBus 引擎主体：组字状态机 + D-Bus 服务对象。

ibus 侧的引擎协议（``org.freedesktop.IBus.Engine``）分两半：

* **daemon → 引擎**：普通方法调用，``ProcessKeyEvent`` 等；
* **引擎 → daemon**：D-Bus 信号，``CommitText`` / ``UpdatePreeditText`` /
  ``UpdateLookupTable``，载荷是 ibus 自定义序列化格式（见 :mod:`ycpy.ibusproto`）。

引擎进程本身不碰候选窗口：daemon 拿到信号后转发给 ``ibus-ui-gtk3``。
"""

from __future__ import annotations

import os
import signal
import sys
import time
import traceback
from array import array

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from . import ibusproto as proto
from . import punctuation
from .composer import Composer
from .config import LOCK_MASK, SHIFT_MASK, Config, matches_hotkey
from .dictionary import Dictionary
from .usermodel import UserModel

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRACE_T0 = time.monotonic()

#: 调试用的 trace。ibus 拉起引擎时不会带环境变量过来，所以用「文件开关」：
#: 往这个路径 touch 一个文件就开始记录，删掉即停。这样不用重启 daemon 就能开。
TRACE_PATH = os.environ.get("PYIM_TRACE") or "/tmp/ycpy-trace.log"


def _trace(msg: str) -> None:
    try:
        if not os.path.exists(TRACE_PATH):
            return
        with open(TRACE_PATH, "a", encoding="utf-8") as fp:
            fp.write("[%.3f %d] %s\n" % (_now(), os.getpid(), msg))
    except OSError:
        pass


def _now() -> float:
    return time.monotonic() - _TRACE_T0

# ---- X11 keysym 常量（只列用得到的） ----
KEY_ESCAPE = 0xFF1B
KEY_BACKSPACE = 0xFF08
KEY_RETURN = 0xFF0D
KEY_KP_ENTER = 0xFF8D
KEY_SPACE = 0x20
KEY_TAB = 0xFF09
KEY_PAGE_UP = 0xFF55
KEY_PAGE_DOWN = 0xFF56
KEY_UP = 0xFF52
KEY_DOWN = 0xFF54
KEY_SEMICOLON = 0x3B
KEY_COMMA = 0x2C
KEY_MINUS = 0x2D
KEY_EQUAL = 0x3D
KEY_SHIFT_L = 0xFFE1
KEY_SHIFT_R = 0xFFE2

#: ``_press_consumed`` 的容量上限。正常情况下每条都会被对应的抬起 pop 掉，
#: 不会涨到这儿；这只是防止 release 丢失时无界增长的兜底。
#: **超限时不能 ``clear()``**——那会把透传给应用的记录一起抹掉，它们的抬起
#: 于是回放成 True，客户端只收到 press、收不到 release，开始自动重复。
#: 与 ``on_focus_out`` 同一个约束：只清 ``True``（引擎吃掉的），
#: ``False``（透传的）必须留到抬起把它 pop 掉。收缩逻辑见
#: ``_trim_press_records``。
_PRESS_CONSUMED_MAX = 512

CONTROL_MASK = 1 << 2
MOD1_MASK = 1 << 3
#: ibus 用这一位标记「这是按键松开事件，不是按下」（src/ibustypes.h）。
#: GTK 等客户端会把松开也送进 ProcessKeyEvent；不区分就会一个键打两个字符。
IBUS_RELEASE_MASK = 1 << 30

#: ``Tab`` 选当前页的第几项。约定是 Tab = 「选下一个候选」，
#: 也就是空格选中的那一项（页内下标 0）之后的第 1 项。
TAB_SELECT_INDEX = 1

#: 挂在 GNOME 输入法菜单里的「首选项」入口所用的 property key
PROP_PREFERENCES = "preferences"


def _script_path(name: str) -> str:
    """启动脚本（``ycpy-engine`` / ``ycpy-preferences``）的绝对路径。

    **两种安装布局的脚本位置不一样**，所以这里要挨个找，而不是猜一个：

    源码树::

        /opt/src/ycpy/ycpy-preferences     ← _ROOT 下

    deb 包（``tools/build_deb.sh`` 装成）::

        /usr/bin/ycpy-preferences                            ← 不在 _ROOT 下
        /usr/lib/ycpy/                                        ← 只有代码

    踩过两次坑，两个方向都错过：

    1. 写成 ``os.path.join(os.path.dirname(_ROOT), name)``——源码树下
       ``_ROOT`` 被剥成 ``/opt/src``，拼出来的路径不存在。
    2. 「修成」``os.path.join(_ROOT, name)``——源码树对了，但 deb 下
       ``_ROOT`` 是 ``/usr/lib/ycpy``，那里**只有 ycpy/ 和 data/**，
       启动脚本在 ``/usr/bin``。于是用户点「首选项」毫无反应
       （用户第二次报的就是这个）。

    真正的原因是这个函数**从来没有同时兼顾过两种布局**。所以现在按顺序
    探测：先 ``$PYIM_EXEC`` 覆盖、``_ROOT`` 下、再 ``/usr/bin``，全都找不到
    才返回 ``_ROOT`` 下那个不存在的路径——让调用方的报错信息能显示出
    它到底期望在哪。

    ``os.environ.get("PYIM_EXEC")`` 那条是 ``_exec_path`` 单独处理的，
    不在这里混用：它的语义是「整个引擎换个可执行文件」，不是「换个脚本」。
    """
    candidates = [
        os.path.join(_ROOT, name),
        os.path.join("/usr/bin", name),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


def launch_preferences() -> None:
    """把首选项对话框拉起来。

    引擎进程常驻且没有终端，不能就地跑 GTK（会把输入法干掉），
    所以另起一个脱离的子进程。
    """
    import subprocess

    script = _script_path("ycpy-preferences")
    _trace("首选项：尝试启动 %s" % script)
    if not os.path.exists(script):
        print("[ycpy] 首选项脚本不存在：%s" % script,
              file=sys.stderr, flush=True)
        _trace("首选项：脚本不存在 %s" % script)
        return
    try:
        subprocess.Popen(
            [script],
            stdout=subprocess.DEVNULL,
            # 不能丢 stderr：GTK 起不来时唯一的线索就在这里。
            #
            # 原先写的是 DEVNULL，而且**注释还写着「不能丢 stderr」**——
            # 注释与代码正好相反，于是「点了首选项没反应」这个 bug 连续
            # 查了两轮都无从查起：子进程的 traceback 全进了 /dev/null。
            # 代价是首选项每次启动会在 journal 里留几行 GTK 警告，可以接受。
            stderr=None,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        _trace("首选项：已启动 %s" % script)
    except OSError as exc:
        # 兜底再报一次，免得又是「点了没反应」且无从查起
        print("[ycpy] 拉起首选项失败：%s" % exc, file=sys.stderr, flush=True)
        _trace("首选项：启动失败 %r" % exc)


PAGE_SIZE = 9   # 默认值；实际以 Config.page_size 为准

ST_IDLE = 0        # 空闲，字母直接透传
ST_COMPOSING = 1   # 正在组字
ST_ASSOCIATE = 2   # 联想模式：展示上文的后续词
ST_ENGLISH = 3     # 英文直通：按中英切换键进入，期间所有按键都不组字


# --------------------------------------------------------------------------
# D-Bus 对象
# --------------------------------------------------------------------------

class EngineObject(dbus.service.Object):
    """``org.freedesktop.IBus.Engine`` 接口，只负责收发消息。"""

    def __init__(self, bus, path, engine):
        dbus.service.Object.__init__(self, bus, path)
        self._engine = engine
        self._path = path

    # ---- 发给 daemon 的信号 ----

    @dbus.service.signal(proto.IFACE_ENGINE, signature="v")
    def CommitText(self, text):  # noqa: N802
        pass

    @dbus.service.signal(proto.IFACE_ENGINE, signature="vubu")
    def UpdatePreeditText(self, text, cursor_pos, visible, mode):  # noqa: N802
        pass

    @dbus.service.signal(proto.IFACE_ENGINE, signature="vb")
    def UpdateLookupTable(self, table, visible):  # noqa: N802
        pass

    @dbus.service.signal(proto.IFACE_ENGINE, signature="uuu")
    def ForwardKeyEvent(self, keyval, keycode, state):  # noqa: N802
        pass

    @dbus.service.signal(proto.IFACE_ENGINE, signature="v")
    def RegisterProperties(self, props):  # noqa: N802
        pass

    @dbus.service.signal(proto.IFACE_ENGINE, signature="v")
    def UpdateProperty(self, prop):  # noqa: N802
        pass

    # ---- daemon 调进来的方法 ----

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="uuu", out_signature="b",
                     sender_keyword="_sender")
    def ProcessKeyEvent(self, keyval, keycode, modifier, _sender=None):  # noqa: N802
        handled = self._engine.process_key(int(keyval), int(keycode), int(modifier))
        _trace("keyval=0x%-4x mod=%-12d sender=%s handled=%-5s state=%d buffer=%r"
               % (int(keyval), int(modifier), _sender, handled, self._engine.state,
                  self._engine.buffer))
        return handled

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="iii", out_signature="")
    def SetCursorLocation(self, x, y, w):  # noqa: N802
        self._engine.on_cursor_location(int(x), int(y), int(w))

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def FocusIn(self):  # noqa: N802
        _trace("FocusIn")
        self._engine.on_focus_in()
        self._register_menu_properties()

    def _register_menu_properties(self) -> None:
        """把「首选项」挂到 GNOME 的输入法菜单里。

        GNOME Shell 那个输入法菜单是写死的：只有「显示键盘布局」和「键盘设置」，
        并没有 per-engine 的首选项项。但它会把引擎上报的 property 渲染成
        可点击的菜单项（拼音的全/半角开关就是这么来的）。
        所以这里借这条通道挂一个「首选项」，点击时 daemon 回送 PropertyActivate。
        """
        try:
            self.RegisterProperties(proto.make_prop_list([
                proto.make_property(PROP_PREFERENCES, "首选项…", 0,
                                    proto.PROP_TYPE_NORMAL),
            ]))
        except Exception:  # 属性通道出问题也不该影响输入法本身
            _trace("RegisterProperties 失败: %s" % traceback.format_exc())

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="ss", out_signature="")
    def FocusInId(self, object_path, client):  # noqa: N802
        self._engine.on_focus_in()

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def FocusOut(self):  # noqa: N802
        _trace("FocusOut (buffer was %r)" % self._engine.buffer)
        self._engine.on_focus_out()

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="s", out_signature="")
    def FocusOutId(self, object_path):  # noqa: N802
        self._engine.on_focus_out()

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def Reset(self):  # noqa: N802
        _trace("Reset (buffer was %r)" % self._engine.buffer)
        self._engine.reset()

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def Enable(self):  # noqa: N802
        _trace("Enable")
        self._engine.has_focus = True

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def Disable(self):  # noqa: N802
        _trace("Disable")
        self._engine.has_focus = False
        self._engine.reset()

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def PageUp(self):  # noqa: N802
        self._engine.turn_page(-1)

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def PageDown(self):  # noqa: N802
        self._engine.turn_page(1)

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def CursorUp(self):  # noqa: N802
        self._engine.turn_page(-1)

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="", out_signature="")
    def CursorDown(self):  # noqa: N802
        self._engine.turn_page(1)

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="uuu", out_signature="")
    def CandidateClicked(self, index, button, state):  # noqa: N802
        self._engine.select(int(index))

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="vuu", out_signature="")
    def SetSurroundingText(self, text, cursor_pos, anchor_pos):  # noqa: N802
        pass

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="u", out_signature="")
    def SetCapabilities(self, caps):  # noqa: N802
        pass

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="uu", out_signature="")
    def SetContentType(self, purpose, hints):  # noqa: N802
        self._engine.content_purpose = int(purpose)

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="ad", out_signature="")
    def ProcessHandWritingEvent(self, coordinates):  # noqa: N802
        pass

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="u", out_signature="")
    def CancelHandWriting(self, n_strokes):  # noqa: N802
        pass

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="su", out_signature="")
    def PropertyActivate(self, name, state):  # noqa: N802
        if str(name) == PROP_PREFERENCES:
            launch_preferences()

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="s", out_signature="")
    def PropertyShow(self, name):  # noqa: N802
        pass

    @dbus.service.method(proto.IFACE_ENGINE, in_signature="s", out_signature="")
    def PropertyHide(self, name):  # noqa: N802
        pass

    # ---- org.freedesktop.DBus.Properties（python-dbus 不会自动生成） ----

    @dbus.service.method(proto.IFACE_PROPERTIES, in_signature="ss", out_signature="v")
    def Get(self, interface_name, prop_name):  # noqa: N802
        if prop_name == "FocusId":
            return dbus.Struct(
                [dbus.Boolean(self._engine.has_focus)], signature="b", variant_level=1
            )
        if prop_name == "ActiveSurroundingText":
            return dbus.Struct([dbus.Boolean(False)], signature="b", variant_level=1)
        if prop_name == "ContentType":
            return dbus.Struct(
                [
                    dbus.UInt32(self._engine.content_purpose),
                    dbus.UInt32(0),
                ],
                signature="uu",
                variant_level=1,
            )
        raise dbus.DBusException("no such property: %s" % prop_name)

    @dbus.service.method(proto.IFACE_PROPERTIES, in_signature="ssv", out_signature="")
    def Set(self, interface_name, prop_name, value):  # noqa: N802
        if prop_name == "ContentType":
            purpose, hints = value
            self._engine.content_purpose = int(purpose)

    @dbus.service.method(proto.IFACE_PROPERTIES, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):  # noqa: N802
        return dbus.Dictionary({}, signature="sv")


class FactoryObject(dbus.service.Object):
    """``org.freedesktop.IBus.Factory``：daemon 靠它问出引擎对象路径。

    **每次 CreateEngine 都要新建一个引擎对象**，和 ibus 官方
    ``ibus_factory_real_create_engine()`` 的行为一致。

    之前这里是「返回一个固定路径 + 全进程共用一份组字状态」，于是当 daemon
    同时持有两个 proxy（全局引擎一个、context 引擎一个）时，同一次按键会被
    投递两遍，两下都加到同一个 buffer 上——表现就是按 c 打出 cc。
    每个 proxy 拥有独立对象和独立组字状态之后，daemon 投给谁就是谁，不会串。
    """

    def __init__(self, bus, engine_path, make_engine):
        dbus.service.Object.__init__(self, bus, proto.PATH_FACTORY)
        self._engine_path = engine_path
        self._make_engine = make_engine
        self._counter = 0
        # 必须自己持有引用：dbus.service.Object 只在 C 侧注册了路径，
        # Python 对象被回收后消息就没人处理了。
        self._instances: list = []

    @dbus.service.method(proto.IFACE_FACTORY, in_signature="s", out_signature="o")
    def CreateEngine(self, name):  # noqa: N802
        self._counter += 1
        path = "%s%d" % (self._engine_path, self._counter)
        logic = self._make_engine()
        logic.ui = EngineObject(self._connection, path, logic)
        self._instances.append(logic)
        print("[ycpy] CreateEngine(%s) -> %s" % (name, path),
              file=sys.stderr, flush=True)
        return dbus.ObjectPath(path)


# --------------------------------------------------------------------------
# 组字逻辑
# --------------------------------------------------------------------------

class PinyinEngine:
    """输入法状态机。完全不碰 D-Bus，方便单测。"""

    def __init__(self, dictionary: Dictionary, user: UserModel, composer: Composer,
                 ui=None, config: "Config | None" = None):
        self.dictionary = dictionary
        self.user = user
        self.composer = composer
        self.ui = ui  # EngineObject 或测试替身
        self.config = config or Config()
        self.composer.user_weight = self.config.user_freq_weight
        # 二元组加权不走单词频那条路（引擎那边只清 words），得单独同步，
        # 否则「关闭学习」时二元组仍在生效。
        self.composer.learning_enabled = self.config.enable_learning
        self.composer.fuzzy_min_exact = self.config.fuzzy_min_exact

        self.state = ST_IDLE
        self.buffer = ""      # 规范化后的字母（可能含 ' 分词符）
        self.raw_keys = ""    # 原始按键，用于拼音回显
        self.candidates: list = []

        # 记住「最近一次按下」是被引擎消费还是透传给应用，松开时必须给出一致
        # 的答案。松开事件本身不做任何动作，只回放按下时的结论。
        #
        # 为什么必须一致：客户端（GTK 等）按 ibus 返回的布尔值决定这个键要不要
        # 交给自己的按键处理。如果按下透传（False）、松开却被引擎吃掉（True），
        # 客户端就只收到了按下、永远等不到松开，它内部的按键状态机因此错乱，
        # 表现就是「按一次却连续输出同一个字符」「退格一直删」。
        #
        # **必须按 keyval 分别记录，不能只留一个「上一次」的槽位。**
        # 用户报「Shift 抬起不稳定，之后点鼠标变成选中文字」，根因就在这：
        # 单槽位会被**任何**中间键的按下覆盖。真实的按键序列是
        #     Shift↓  空格↓  空格↑  Shift↑
        # 空格被引擎消费（选词）-> 槽位变成 True -> 松开 Shift 时回放的是
        # **空格**的结论 True，Shift 的抬起就这样被吃掉了。应用内部于是认为
        # Shift 还按着，之后任何鼠标点击都成了 Shift+点击 -> 选中一片文字。
        # 修饰键（左右 Shift）通常比被修饰的键**晚**抬起，最容易中招。
        self._press_consumed: dict[int, bool] = {}
        #: 按下 Shift 后是否还在等「单按切换中英」的判定（见 _process_press）
        self._pending_shift_toggle = False
        self.page = 0
        self.page_candidates: list = []

        self.assoc_context = ""
        self.assoc_candidates: list = []

        self.has_focus = False
        self.content_purpose = proto.INPUT_PURPOSE_UNKNOWN

        self._context = ""    # 最近一次上屏的词，联想的上下文

    # -------------------------------------------------------------- 配置热重载

    @property
    def page_size(self) -> int:
        return self.config.page_size

    def reload_config_if_changed(self) -> None:
        """首选项对话框改完配置后，下一次按键就生效，不用重启输入法。"""
        if self.config.reload_if_changed():
            self.composer.user_weight = self.config.user_freq_weight
            # 见 __init__ 里的同名赋值：二元组要单独跟着开关走。
            self.composer.learning_enabled = self.config.enable_learning
            self.composer.fuzzy_min_exact = self.config.fuzzy_min_exact
            # 领域词库跟着配置即时生效。独立索引架构的好处就在这里：
            # 载入/丢弃只是加减一个索引对象（每个领域 ~25 ms），
            # **不需要重建主词典，也不需要动它的 marshal 缓存**。
            self.composer.domains.load(self.config.domains)
            if not self.config.enable_learning:
                self.dictionary.user_freq = array("q", bytes(8 * len(self.dictionary)))
            else:
                self.dictionary.apply_user_counts(self.user.words)
            self.page = 0
            if self.state in (ST_COMPOSING, ST_ASSOCIATE):
                self.refresh()

    # ---------------------------------------------------------------- UI

    def _emit_commit(self, text: str) -> None:
        if self.ui:
            self.ui.CommitText(proto.make_text(text))

    def _emit_preedit(self, text: str, visible: bool) -> None:
        if not self.ui:
            return
        if visible and text:
            self.ui.UpdatePreeditText(
                proto.make_text(text),
                dbus.UInt32(len(text)),
                True,
                proto.PREEDIT_FOCUS_MODE_LOCATION,
            )
        else:
            self.ui.UpdatePreeditText(proto.make_text(""), 0, False, proto.PREEDIT_FOCUS_MODE_NONE)

    def _emit_table(self, texts, visible: bool | None = None) -> None:
        """发候选表。

        ``visible=None`` 表示「有候选就显示，没有就隐藏」（默认行为）。
        显式传 True/False 可以强制候选窗的开合——组字期间即使一个候选都没有，
        也要把窗子留着（见 ``_show``）。
        """
        if not self.ui:
            return
        texts = list(texts)
        show = bool(texts) if visible is None else bool(visible)
        self.ui.UpdateLookupTable(
            proto.make_lookup_table(texts, 0, self.page_size), show
        )

    def _clear_ui(self) -> None:
        self._emit_preedit("", False)
        self._emit_table([])

    # -------------------------------------------------------------- 状态

    def reset(self) -> None:
        self.state = ST_IDLE
        self.buffer = ""
        self.raw_keys = ""
        self.candidates = []
        self.page_candidates = []
        self.assoc_context = ""
        self.assoc_candidates = []
        self.page = 0
        self._clear_ui()

    def on_focus_in(self) -> None:
        self.has_focus = True

    def on_focus_out(self) -> None:
        self.has_focus = False
        # 焦点离开时清理「按下结论」记录——但**只清引擎自己吃掉的那些**。
        #
        # 回归 bug（用户报「偶尔莫名其妙输出很多回车键或者退格键」）：
        # 原先这里是 ``self._press_consumed.clear()``，把**透传给应用的键**
        # 的记录也一起抹了。于是这个顺序会出事：
        #
        #     Backspace 按下   -> 返回 False -> 应用收到 press，开始删字
        #     FocusOut 到达    -> clear() 把记录抹掉
        #     Backspace 抬起   -> pop 不到 -> 兜底返回 True -> **应用收不到 release**
        #
        # 应用（GTK 一类）于是认为这个键**还按着**，开始自动重复——用户看到
        # 的就是「没碰退格却在连续删除」「凭空冒出好几个换行」。
        #
        # X 会在同一时刻送出 KeyRelease 和 FocusOut，两者进 D-Bus 队列的先后
        # 不保证，所以这个窗口是**真实存在**的，而且很窄——这正是它「偶尔」
        # 才出现、也让快节奏压测抓不到的原因（真人打字 100~300 ms 的间隔
        # 才撞得上）。
        #
        # 只清 True（引擎吃掉）的记录是安全的：那些键应用从头到尾没看见过，
        # 抬起时无论回放什么应用都感知不到。False 的必须留着，抬起才能
        # 回放 False、和 press 配成一对。
        #
        # **不能挪进 reset()**：reset() 会在 _process_press 内部被调用
        # （选词、退格清空、英文切换都会），那时用户可能正按住 Shift，清掉
        # 就会变成 3.11 那个「Shift 抬起被吞」的老 bug。
        self._press_consumed = {k: v for k, v in self._press_consumed.items()
                                if not v}
        self.reset()

    def on_cursor_location(self, x, y, w) -> None:
        pass

    # ------------------------------------------------------------ 候选刷新

    def _pool(self) -> list:
        return self.assoc_candidates if self.state == ST_ASSOCIATE else self.candidates

    def refresh(self) -> None:
        if self.state == ST_ASSOCIATE:
            self.assoc_candidates = self.composer.next_words(self.assoc_context)
            self.page = 0
            self._show(show_preedit=False)
            return

        self.candidates = self.composer.candidates(self.buffer)
        self.composer.score_with_context(self.candidates, self._context)
        self.page = 0
        # 即使一个候选都没有，也要把用户敲的字母显示出来。
        # 早先这里直接 _clear_ui()，于是敲了不匹配的拼音（比如 "xyz"）
        # 字母就从屏幕上消失了，用户既看不到自己敲了什么，也没法确认状态。
        self._show(show_preedit=True)

    def _show(self, show_preedit: bool) -> None:
        pool = self._pool()
        start = self.page * self.page_size
        window = pool[start : start + self.page_size]
        self.page_candidates = window
        if show_preedit:
            # 回显**用户实际敲进去的字母**，不是规范化之后加过分隔符的串。
            # 用户要看着自己敲的东西，输入法不该擅自把它变成 "cheng xu"。
            self._emit_preedit(self.raw_keys or self.buffer, True)
        else:
            self._emit_preedit("", False)
        # 组字期间候选窗必须一直开着，哪怕一个候选都没匹配上——用户要知道
        # 「输入法还活着、我敲的东西去哪了」。只有输入被退格删光才关窗。
        #
        # 联想模式下没有 raw_keys 可依据，就按「有没有候选」正常开关。
        if show_preedit:
            keep_open = bool(self.raw_keys or self.buffer)
        else:
            keep_open = bool(window)
        self._emit_table((c.text for c in window), visible=keep_open)

    def turn_page(self, delta: int) -> None:
        pool = self._pool()
        pages = max(1, (len(pool) + self.page_size - 1) // self.page_size)
        self.page = (self.page + delta) % pages
        self._show(show_preedit=(self.state == ST_COMPOSING))

    # ---------------------------------------------------------------- 选词

    def _select_at(self, index_in_page: int) -> bool:
        """按页内下标选词，越界时返回 False（按键透传给应用）。

        空格和 Tab 共用这一个入口，因为它们**该有同一个口径**。

        之前是各自 ``self.select(...)`` + 无脑 ``return True``，于是：
        候选只有 1 项时，按 Tab 选中那 1 项（正常），但按空格时
        ``select(0)`` 越界被静默挡掉、按键却被吞掉——**两个键对同一种情况
        给出不同答案**，Tab 修好了、空格还是死键。

        越界就返回 False：用户在这类键上得不到任何结果，放给应用是唯一说得
        通的处理（空格尤其重要，用户按空格常常就是想打一个空格）。

        ``select()`` 保留原样——它对外承诺「越界静默忽略」，改签名会牵动
        所有调用点。
        """
        if index_in_page < 0 or index_in_page >= len(self.page_candidates):
            return False
        self.select(index_in_page)
        return True

    def select(self, index_in_page: int) -> None:
        if index_in_page < 0 or index_in_page >= len(self.page_candidates):
            return
        cand = self.page_candidates[index_in_page]
        if self.config.enable_learning:
            self.user.learn(cand.text, self._context or None)
        self._context = cand.text
        self._emit_commit(cand.text)

        if self.state == ST_ASSOCIATE:
            self.assoc_context = cand.text
            self.refresh()
        else:
            self.reset()

    def _select_best(self) -> bool:
        if not self.candidates:
            return False
        self.select(0)
        return True

    def _commit_raw(self) -> None:
        self._emit_commit(self.raw_keys or self.buffer)
        self.reset()

    # ------------------------------------------------------------ 联想模式

    def enter_associate(self) -> bool:
        if not self._context:
            return False
        self.state = ST_ASSOCIATE
        self.buffer = ""
        self.raw_keys = ""
        self.assoc_context = self._context
        self.refresh()
        return True

    def exit_associate(self) -> bool:
        if self.state != ST_ASSOCIATE:
            return False
        self.state = ST_IDLE
        self.assoc_context = ""
        self.assoc_candidates = []
        self.page_candidates = []
        self._clear_ui()
        return True

    def _commit_and_associate(self) -> bool:
        """上屏当前组字，然后进入联想模式（分号键）。"""
        if self.state == ST_COMPOSING:
            if not self._select_best():
                self._commit_raw()
        self.state = ST_ASSOCIATE
        self.assoc_context = self._context
        self.refresh()
        return True

    # ------------------------------------------------------------ 按键分派

    @staticmethod
    def _is_digit(keyval: int) -> bool:
        """是不是数字键（与「能否选中」无关）。

        **只认主键盘**：``0x30..0x39``。小键盘是 ``0xFFB0..0xFFB9``
        （KP_0..KP_9），这里不认——中文态下小键盘的数字不参与选词，
        与其静默被吃掉不如原样透传给应用。
        """
        return 0x30 <= keyval <= 0x39

    def _digit_index(self, keyval: int) -> int:
        """数字键 → **页内**下标，非数字返回 -1。

        映射必须跟着当页的**实际候选数**走，不能写死「1..9 → 0..8、0 → 9」：
        每页候选数可配5 / 7 / 9（``PAGE_SIZE_CHOICES``），写死的话
        ``page_size=5`` 时按 6/7/8/9/0 全都越界，而 ``select()`` 的边界检查
        是**静默 return** ——于是那 5 个键变成「按了没反应」的死键。

        实测：``page_size=5`` 时按 1~5 正常，按 6~0 全部无效。

        所以这里按 ``len(page_candidates)`` 折算，越界就返回 -1（调用方据此
        区分「不是数字键」和「数字键越界」）。
        """
        count = len(self.page_candidates)
        if 0x31 <= keyval <= 0x39:            # 1..9
            idx = keyval - 0x31
        elif keyval == 0x30:                  # 0 收尾
            idx = count - 1
        else:
            return -1
        return idx if idx < count else -1

    def _handle_punctuation(self, keyval: int, modifier: int = 0) -> bool:
        """英文标点转中文标点。返回 True 表示这个键已被当作标点吃掉。

        组字中打标点会**先把拼音原样上屏**，再上屏标点——和其他输入法一致，
        用户想打「你好，」时不需要先按回车。
        """
        if not punctuation.is_punctuation(keyval, modifier):
            return False
        shifted = bool(modifier & (1 << 0))
        text = punctuation.convert(chr(keyval), shifted)
        if not text:
            return False
        if self.state == ST_COMPOSING and self.raw_keys:
            # 组字中打标点：先上屏**第一个候选词**，而不是把拼音原样落上去。
            # 用户打「nihao,」想要的是「你好，」，不是「nihao，」。
            if not self._select_best():
                self._emit_commit(self.raw_keys)
        elif self.state == ST_ASSOCIATE:
            self.exit_associate()
        self.state = ST_IDLE
        self.buffer = ""
        self.raw_keys = ""
        self.candidates = []
        self.page_candidates = []
        self.page = 0
        self._emit_commit(text)
        self._clear_ui()
        return True

    def _handle_composing(self, keyval: int, modifier: int = 0) -> bool:
        if 0x41 <= keyval <= 0x5A:  # A-Z
            keyval += 0x20
        if 0x61 <= keyval <= 0x7A:  # a-z：继续组字
            self.buffer += chr(keyval)
            self.raw_keys += chr(keyval)
            self.refresh()
            return True
        if self._is_digit(keyval):
            idx = self._digit_index(keyval)
            if idx >= 0:
                self.select(idx)
            # 越界也吞掉：中文态下数字键是输入法自己的键，放给应用会在
            # 中文文本里蹦出一个阿拉伯数字，比「按了没反应」更糟。
            return True
        if keyval == KEY_SPACE:
            # 越界时放给应用：用户按空格就是想打一个空格。
            #
            # 候选池为空是真实存在的（打分会剔掉繁体单字、联想末字查不到搭配
            # 时 next_words 返回空），只是被「打不出拼音给高频词兜底」挡着、
            # 极少出现——所以之前一直没被发现。原来这里一律 return True，
            # 按键被吞掉、什么也没发生。
            #
            # 走 ``_select_at`` 而不是自己判空，是为了和 Tab 保持同一口径
            # （那两处曾对同一种情况给出不同答案）。
            return self._select_at(0)
        if keyval in (KEY_RETURN, KEY_KP_ENTER):
            # 回车 = 原样上屏用户敲的字母，而不是选第一个候选词。
            # 想上屏候选词请用空格（第一个）、数字键或 `;`（上屏并进入联想）。
            # 这样「打不出来的拼音」有了确定的出路：按回车直接落英文。
            self._commit_raw()
            return True
        if keyval == KEY_BACKSPACE:
            if self.buffer:
                self.buffer = self.buffer[:-1]
                self.raw_keys = self.raw_keys[:-1]
                if self.buffer:
                    self.refresh()
                    return True
                # 退到空了：**必须立刻回到空闲态**，预编辑要收掉、候选窗要关。
                #
                # 压测抓到的 bug（tools/stress_core.py）：这里原来无条件
                # ``refresh(); return True``，于是 buffer 变空后 state 仍停在
                # ST_COMPOSING。下一次退格就会掉进下面「预编辑已空 → 交给应用」
                # 的分支，把这次退格**放给应用**——可用户明明只想删掉刚打的
                # 那一个拼音字，不想删应用里已上屏的文字。
                #
                # 表现：敲 `n` 再退格，会连带把光标前面一个已上屏的字也删掉。
                self.reset()
                return True
            # 预编辑已经空了：这次退格应该交给应用去删已上屏的文字。
            # 早先这里一律返回 True，事件被吞掉，用户表现为「退格键不工作」。
            self.reset()
            return False
        if keyval == KEY_ESCAPE:
            self.reset()
            return True
        if keyval in (KEY_PAGE_DOWN, KEY_DOWN, KEY_MINUS, KEY_COMMA):
            self.turn_page(1)
            return True
        if keyval in (KEY_PAGE_UP, KEY_UP, KEY_EQUAL):
            self.turn_page(-1)
            return True
        if keyval == KEY_TAB:
            # Tab = 「选下一个候选」，即当前页的第 2 项（页内下标 1）。
            #
            # 这里原来写的是 ``self.page * self.page_size + 1``——那是**全局**
            # 下标，而 ``select`` 收的是**页内**下标。第 0 页时全局 1 恰好
            # 等于页内 1，**掩盖了错位**；翻到第 1 页后 nxt=10 超出
            # page_candidates（长度 9），被 select 开头的边界检查静默挡掉，
            # Tab 就成了一个什么都不做的死键。实测第 1、2 页都是。
            return self._select_at(TAB_SELECT_INDEX)
        return False

    def _handle_associate(self, keyval: int) -> bool:
        if self._is_digit(keyval):
            idx = self._digit_index(keyval)
            if idx >= 0:
                self.select(idx)
            # 越界也吞掉：中文态下数字键是输入法自己的键，放给应用会在
            # 中文文本里蹦出一个阿拉伯数字，比「按了没反应」更糟。
            return True
        if keyval == KEY_SPACE:
            # 越界时放给应用。与 composing 态同一个口径（见那里）。
            #
            # 注意返回 False 时本函数末尾的 ``exit_associate()`` 仍会执行，
            # 所以联想窗会正常关闭、state 回到 ST_IDLE——实测确认过。
            return self._select_at(0)
        if keyval == KEY_TAB:
              # 与 composing 态同一处修正（见那里）：页内下标，不是全局下标。
              #
              # 越界返回 False 时，本函数末尾的 ``exit_associate()`` 不会被
              # 走到——所以这里显式退出，保持「越界 -> 联想窗关闭」和其它键
              # 一致，否则会残留一个空联想窗。
              if not self._select_at(TAB_SELECT_INDEX):
                  self.exit_associate()
              return True
        if keyval in (KEY_PAGE_DOWN, KEY_DOWN, KEY_MINUS, KEY_COMMA):
            self.turn_page(1)
            return True
        if keyval in (KEY_PAGE_UP, KEY_UP, KEY_EQUAL):
            self.turn_page(-1)
            return True
        if keyval in (KEY_ESCAPE, KEY_BACKSPACE, KEY_RETURN, KEY_KP_ENTER):
            self.exit_associate()
            return True
        self.exit_associate()
        return False

    def process_key(self, keyval: int, keycode: int = 0, modifier: int = 0) -> bool:
        """返回 True 表示本引擎消费了该按键。"""
        # 松开事件：ibus 用 modifier 的第 30 位标记它。松开本身不产生任何动作，
        # 但必须**回放按下时的结论**，否则客户端会只收到按下、收不到松开。
        if modifier & IBUS_RELEASE_MASK:
            _trace("release keyval=%#x 记录=%s"
                   % (keyval, self._press_consumed.get(keyval, "<无>")))
            # 回放**这个键自己**按下时的结论，不是「最后一个被按下的键」的。
            # pop 掉是因为一次 press/release 只配对一次；没有配对的抬起
            # （比如 ibus 送来的杂散 release）拿到 None，按老规矩消费掉。
            consumed = self._press_consumed.pop(keyval, None)
            # 只有 **Shift 自己的抬起** 才结算待办。早先这里不看 keyval，
            # 于是「按 Shift → 按任意键 → 那个键抬起」会触发切换；此时若正在
            # 组字，_toggle_english() 会把整串拼音上屏、预编辑清空——用户看到
            # 的就是「敲了一长串，字突然自己动了」。
            if self._pending_shift_toggle and keyval in (KEY_SHIFT_L, KEY_SHIFT_R):
                self._pending_shift_toggle = False
                self._toggle_english()
                # 切换完了，但**抬起仍然要放给应用**。
                #
                # 回归 bug（用户报「切到英文后按 a 出大写 A」）：按下时为了让
                # 应用能正常处理大写，返回了 False（透传），抬起却返回 True
                # 把它吃掉了。客户端于是只收到 Shift 按下、永远等不到抬起，
                # 内部认为 Shift 还按着 —— 之后每个字母都是大写。
                # 更迷惑的是：打开 CapsLock 时 Shift+CapsLock 恰好等于小写，
                # 于是「锁了 CapsLock 反而正常」，很难联想到是 Shift 抬起被吞。
                #
                # 所以这里必须回放按下的结论（False），让应用看到完整的
                # press + release 一对。
                return consumed if consumed is not None else False
            return True if consumed is None else consumed

        handled = self._process_press(keyval, modifier)
        _trace("press   keyval=%#x mod=%#x -> %s  (state=%s)"
               % (keyval, modifier, "吃掉" if handled else "透传", self.state))
        # 按下 Shift 之后又按了**任何**别的键（不管引擎处不处理），
        # 都说明 Shift 只是当修饰键用（大写、?、!），取消待切换。
        # 早先只在 handled=True 时才清除，透传的键清不掉，待办会一直留着。
        if self._pending_shift_toggle and keyval not in (KEY_SHIFT_L, KEY_SHIFT_R):
            self._pending_shift_toggle = False
        # 记下**这个键自己**的结论，供它的抬起回放。
        #
        # 关于 auto-repeat：按住不放时 X 会连发同一个 keyval 的 press（中间没有
        # release），这里只是反复覆盖同一条，抬起时 pop 一次即可，语义不变。
        #
        # 关于容量：正常情况下每条都会被对应的抬起 pop 掉，不会涨。但如果某次
        # release 没送达（切窗口时 ibus 会丢），那条会一直留着。keyval 的取值
        # 空间有限，所以最多也就几百条，这里顺手做个上限兜底，避免异常情况下
        # 无界增长。
        if len(self._press_consumed) > _PRESS_CONSUMED_MAX:
            self._trim_press_records()
        self._press_consumed[keyval] = handled
        return handled

    def _trim_press_records(self) -> None:
        """记录表超限时的收缩。**丢错的代价远大于泄漏**，所以有优先级。

        这里的取舍很容易搞反：

        * 丢掉 ``False``（透传）的记录 -> 它的抬起 pop 不到 -> 兜底返回
          ``True`` -> 客户端只收到 press、收不到 release -> **开始自动重复**。
          这是用户能看见的 bug。
        * 保留 ``False`` 的记录 -> 万一 release 真丢了，就多留一个条目，
          只是内存。keyval 取值空间有限，实际到不了多少。

        所以**先丢 ``True``**（引擎吃掉的，应用从头到尾没看见，抬起回放什么
        它都感知不到，丢掉完全无害）；不够再丢最旧的 ``False``。

        注意这里原来写的是 ``clear()``——和 ``on_focus_out`` 犯的是同一个
        错误，只是触发条件不同（那边频繁、这边要攒到 512 条才出现），
        所以修好前一个之后它一直躲在后面。
        """
        rec = self._press_consumed
        # dict 保持插入序，从头开始就是最旧的。
        while len(rec) > _PRESS_CONSUMED_MAX:
            victim = next((k for k, v in rec.items() if v), None)
            if victim is None:
                # 全是透传记录：只能丢最旧的那个。
                victim = next(iter(rec))
            rec.pop(victim, None)

    def _process_press(self, keyval: int, modifier: int = 0) -> bool:
        """处理一次真正的按下，返回是否由引擎消费。"""
        self.reload_config_if_changed()

        # 中英切换在任何状态下都优先响应。
        if matches_hotkey(keyval, modifier, self.config.zh_en_switch):
            # 裸 Shift 当切换键有个绕不开的冲突：`?` 和 `!` 本身就要按 Shift。
            # 一按 Shift 就切换，用户根本打不出这两个标点。
            # 所以这里**不在按下时切换**，只记一个待办，等松开时再看：
            # 期间没有按过别的键 -> 确实是「单按 Shift」，切换；
            # 期间按了别的键 -> Shift 只是当修饰键用（大写、?、!），不切换。
            hotkey = self.config.zh_en_switch
            bare = (int(hotkey.get("keyval", 0)) in (KEY_SHIFT_L, KEY_SHIFT_R)
                    and not int(hotkey.get("modifiers", 0)))
            if bare:
                self._pending_shift_toggle = True
                return False        # Shift 本身交给应用，大写要靠它
            return self._toggle_english()

        # 联想热键同理，必须在下面的「Ctrl/Alt 一律透传」之前判断，
        # 否则 Ctrl+; 会被当成系统快捷键放走，联想永远触发不了。
        if (self.config.enable_associate
                and matches_hotkey(keyval, modifier, self.config.associate_key)):
            if self.state == ST_COMPOSING:
                return self._commit_and_associate()
            if self.state == ST_ASSOCIATE:
                self.select(0)
                return True
            return self.enter_associate()

        if modifier & CONTROL_MASK or modifier & MOD1_MASK:
            # 剩下的 Ctrl/Alt 组合留给输入法框架（切换输入法、快捷键等）
            return False

        # 英文直通模式：除了切换键，什么都不管，全部交给应用
        if self.state == ST_ENGLISH:
            return False

        # CapsLock 锁定、或按住 Shift 时，字母应该**直接出英文字母**，
        # 不能拿去组字。
        #
        # 用户报的两个 bug 都是这一条漏了：
        # * CapsLock 锁住后按 x，引擎把大写的 X 当成拼音字母组字，弹出候选框，
        #   大写字母永远打不出来；
        # * 中文态按住 Shift 按 x，引擎把 Shift 忽略掉、把 x 塞进拼音串里，
        #   结果什么都没输出。
        #
        # 这也符合各输入法通行做法：CapsLock 相当于一个临时的英文开关。
        if 0x41 <= keyval <= 0x7A and (modifier & (LOCK_MASK | SHIFT_MASK)):
            if self.state == ST_COMPOSING and self.buffer:
                # 先把正在组的拼音结掉，再让这个字母原样出去
                if not self._select_best():
                    self._emit_commit(self.raw_keys)
                self.reset()
            elif self.state == ST_ASSOCIATE:
                self.exit_associate()
            return False

        # 中文态下的英文标点 -> 中文标点（, -> ，. -> 。 等）。
        # 放在状态分派之前，这样标点在任何中文子状态下都一致生效。
        if self.config.enable_punctuation and self._handle_punctuation(keyval, modifier):
            return True

        if self.state == ST_COMPOSING:
            if self._handle_composing(keyval, modifier):
                return True
            # 组字中收到非拼音键：先把拼音原样上屏，再让该键透传给应用
            if self.buffer:
                self._commit_raw()
            return False

        if self.state == ST_ASSOCIATE:
            if self._handle_associate(keyval):
                return True
            # 这里**必须 return**，不能靠下面的 `if self.state == ST_IDLE`。
            #
            # `_handle_associate` 对「既不是候选键也不是翻页键」的输入会先
            # `exit_associate()` 再返回 False——那个字母是**要透传给应用**的
            # （联想窗只是临时的，用户接着打的字理应正常进应用）。
            #
            # 但它顺手把 state 改成了 ST_IDLE，于是下面那个
            # `if self.state == ST_IDLE:` 分支接着把这个字母**又塞进
            # buffer 开始组字**，并返回 True ——应用永远收不到这个字母，
            # 而且输入法莫名其妙进入了组字态。
            #
            # 症状：联想窗里直接打字，第一个字母「消失」，随后候选窗弹出。
            return False

        if self.state == ST_IDLE:
            if 0x41 <= keyval <= 0x7A:  # A-Z / a-z
                if self.content_purpose in (
                    proto.INPUT_PURPOSE_DIGITS,
                    proto.INPUT_PURPOSE_NUMBER,
                    proto.INPUT_PURPOSE_PHONE,
                ):
                    return False
                if keyval < 0x61:  # 大写转小写
                    keyval += 0x20
                self.state = ST_COMPOSING
                self.buffer = chr(keyval)
                self.raw_keys = chr(keyval)
                self.refresh()
                return True
        return False

    def _toggle_english(self) -> bool:
        """中英切换：组字中先上屏已有拼音，再进入/退出英文直通。"""
        if self.state == ST_COMPOSING and self.buffer:
            self._commit_raw()
        if self.state == ST_ASSOCIATE:
            self.exit_associate()
        if self.state == ST_ENGLISH:
            self.state = ST_IDLE
        else:
            self.state = ST_ENGLISH
        return True


# --------------------------------------------------------------------------
# 进程入口
# --------------------------------------------------------------------------

BUS_NAME = "ycpy.pinyin.assist"


class DuplicateInstance(Exception):
    """已经有别的 ycpy 进程占着总线名，本进程没必要继续跑。"""


def _read_address_file(path: str) -> str | None:
    """从 ibus 的 socketfile 里读出 ``IBUS_ADDRESS=`` 那一行。"""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line.startswith("IBUS_ADDRESS="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _connect(address: str):
    """连不上就返回 None，由调用方决定重试还是放弃。"""
    try:
        return dbus.bus.BusConnection(address)
    except dbus.DBusException:
        return None


def resolve_ibus_address(extra_first: str | None = None) -> str | None:
    """找到 ibus-daemon 的私有 socket。

    ibus-daemon 拉起引擎子进程时并**不会**传 ``IBUS_ADDRESS``，所以只能自己找；
    查找顺序与 libibus 的 ``ibus_get_address()`` 保持一致。

    ``extra_first`` 放在最前面试（由守护循环传入，用来在连接断开后重探），
    其次是环境变量，最后扫 ``~/.config/ibus/bus/``。

    环境变量优先于扫描是刻意的：有人显式设了 ``IBUS_ADDRESS``（比如对着
    私有 daemon 做测试），就应当尊重。但如果它已经失效（比如那个 socket 被
    删了），一直重试它是没意义的，所以调用方会在连不上时把扫描结果作为
    备选传进来。
    """
    for candidate in (extra_first, os.environ.get("IBUS_ADDRESS")):
        if candidate:
            return candidate

    address_file = os.environ.get("IBUS_ADDRESS_FILE")
    if address_file:
        address = _read_address_file(address_file)
        if address:
            return address

    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    bus_dir = os.path.join(config, "ibus", "bus")
    try:
        candidates = [
            os.path.join(bus_dir, name)
            for name in os.listdir(bus_dir)
            if not name.startswith("socketfile")
        ]
    except OSError:
        candidates = []
    # 多个 socketfile 可能同时存在（不同 display），取最新的那个
    for path in sorted(candidates, key=os.path.getmtime, reverse=True):
        address = _read_address_file(path)
        if address:
            return address
    return None


def _scan_ibus_address() -> str | None:
    """忽略环境变量，只按 socketfile 扫描找 daemon。"""
    saved = os.environ.pop("IBUS_ADDRESS", None)
    try:
        return resolve_ibus_address()
    finally:
        if saved is not None:
            os.environ["IBUS_ADDRESS"] = saved


def _engine_already_registered(bus) -> bool:
    """daemon 是否已经原生认识 ycpy（组件装在 component 目录里的那种情况）。

    是的话说明是 daemon 主动拉起我们的，自注册就会多出一个重复条目——
    之前排查「按 c 出 cc」时看到过 daemon 里同���引擎被注册多份。
    """
    try:
        props = dbus.Interface(
            bus.get_object(proto.BUS_NAME, "/org/freedesktop/IBus"),
            proto.IFACE_PROPERTIES,
        )
        descs = props.Get(proto.BUS_NAME, "Engines")
        return any(str(d[2]) == "ycpy" for d in descs)
    except dbus.DBusException:
        return False


def _exec_path() -> str:
    """自身启动脚本的绝对路径，写进组件描述里，daemon 需要重启我们时用。"""
    explicit = os.environ.get("PYIM_EXEC")
    if explicit:
        return explicit
    return _script_path("ycpy-engine")


def _serve_once(bus_name: str, logic_factory, last_address: str | None = None) -> None:
    """连上一次 daemon，注册组件并进入消息循环；连接断开时返回 False。"""
    bus = None
    for candidate in (last_address, resolve_ibus_address(), _scan_ibus_address()):
        if not candidate:
            continue
        bus = _connect(candidate)
        if bus is not None:
            address = candidate
            break
    if bus is None:
        print("[ycpy] 还没找到可用的 ibus-daemon，稍后重试", file=sys.stderr, flush=True)
        return

    reply = bus.request_name(bus_name)
    # RequestNameReply: 1=PRIMARY_OWNER 2=IN_QUEUE 3=EXISTS 4=ALREADY_OWNER。
    # 默认 flags=0 表示「排队等待」，所以名字被别人占着时拿到的是 IN_QUEUE(2)，
    # 而不是 EXISTS(3)——两种都表示「已经有实例在跑」，本进程应当退出。
    if reply in (dbus.bus.REQUEST_NAME_REPLY_EXISTS,
                 dbus.bus.REQUEST_NAME_REPLY_IN_QUEUE):
        print("[ycpy] 已经有实例在运行（%s，reply=%s），本进程退出"
              % (bus_name, reply), file=sys.stderr, flush=True)
        raise DuplicateInstance()
    if reply not in (dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER,
                     dbus.bus.REQUEST_NAME_REPLY_ALREADY_OWNER):
        print("[ycpy] 拿不到总线名 %s（reply=%s）" % (bus_name, reply),
              file=sys.stderr, flush=True)
        return

    engine_path = "/org/freedesktop/IBus/Engine/ycpy%d" % os.getpid()
    factory = FactoryObject(bus, engine_path, logic_factory)
    logic = factory._make_engine()
    logic.ui = EngineObject(bus, "%s0" % engine_path, logic)

    # 顺序很关键：先占住总线名并导出 Factory，再向 daemon 注册组件。
    # daemon 会在**本进程**的连接上创建 Factory 代理，注册者自己必须真的提供它。
    #
    # 但如果组件已经装进了 component 目录（daemon 原生认识 ycpy），就**不要**再自注册，
    # 否则同一个引擎会在 daemon 里出现两份。
    if _engine_already_registered(bus):
        print("[ycpy] 组件已由 daemon 原生加载，跳过自注册", file=sys.stderr, flush=True)
    else:
        from .register import default_component, register_component

        register_component(bus, default_component(_exec_path()))
        print("[ycpy] 已自注册：bus=%s engine=%s" % (bus_name, engine_path),
              file=sys.stderr, flush=True)

    loop = GLib.MainLoop()

    def watchdog() -> bool:
        """daemon 挂了就退出本轮循环，重新去连新的 daemon。"""
        try:
            # 用最轻量的 Peer.Ping 做一次真实往返，纯本地查询探测不到断线。
            bus.get_object(proto.BUS_NAME, "/org/freedesktop/IBus") \
                .Ping(dbus_interface="org.freedesktop.DBus.Peer", timeout=5)
        except dbus.DBusException as exc:
            print("[ycpy] daemon 失联：%s" % exc, file=sys.stderr, flush=True)
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(5, watchdog)

    try:
        loop.run()
    finally:
        try:
            bus.close()
        except Exception:
            pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bus_name = os.environ.get("PYIM_BUS_NAME", BUS_NAME)

    dict_path = os.environ.get("PYIM_DICT")
    try:
        dictionary = Dictionary(dict_path if dict_path else None)
    except FileNotFoundError as exc:
        print("[ycpy] %s" % exc, file=sys.stderr, flush=True)
        return 2

    user = UserModel()
    # 把用户词频灌进索引
    config = Config()
    dictionary.apply_user_counts(user.words if config.enable_learning else {})
    composer = Composer(dictionary, user)
    # 启动时就载入用户勾选的专业领域词库，免得第一次按键才知道。
    # 勾选变化则由 ``reload_config_if_changed`` 处理。
    composer.domains.load(config.domains)
    print("[ycpy] 词典载入完成：%d 词条 / %d 联想上下文 / %s"
          % (len(dictionary), len(composer.next_table), composer.domains.stats()),
          file=sys.stderr, flush=True)

    DBusGMainLoop(set_as_default=True)

    def logic_factory() -> PinyinEngine:
        return PinyinEngine(dictionary, user, composer, config=config)

    stop = {"flag": False}

    def shutdown(*_args):
        stop["flag"] = True
        user.flush()
        # 从任意线程安全地退出主循环
        GLib.idle_add(lambda: (_ for _ in ()).throw(SystemExit(0)) and None)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # daemon 随时可能被重启（ibus exit / 注销重登），所以这里做成守护式的循环：
    # 掉线了就重新找地址、重连、重新注册。
    last_address = None
    while not stop["flag"]:
        try:
            _serve_once(bus_name, logic_factory, last_address)
            last_address = resolve_ibus_address()
        except DuplicateInstance:
            return 0
        except SystemExit:
            break
        except (dbus.DBusException, GLib.GError) as exc:
            print("[ycpy] 异常退出本轮服务：%s" % exc, file=sys.stderr, flush=True)
        if stop["flag"]:
            break
        time.sleep(1.0)

    user.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())