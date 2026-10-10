"""把 ycpy 注册到正在运行的 ibus-daemon。

ibus 1.5.x 的守护进程只扫描 ``/usr/share/ibus/component``（用户目录
``~/.local/share/ibus/component`` 在这个版本里是被禁用的），而且只在启动时扫描一次。
好在 daemon 暴露了 ``org.freedesktop.IBus.RegisterComponent``：任何进程都能把一个
IBusComponent 对象直接注册进去，注册完立刻可用，既不需要 root，也不用重启输入法。

deb 包把组件 XML 装进 ``/usr/share/ibus/component``，ibus-daemon 启动时会
自己扫到它并拉起引擎。但**它只在启动时扫一次**，而引擎自己可能是在 daemon
运行期间才起来的（比如 daemon 重启了、或者用户刚装完包还没重登）。

所以引擎启动时会调用本模块的 ``RegisterComponent``，把自己直接注册进
**当前**这个 daemon——这样不必重启输入法就能用。

这不是「第二条路」，而是兜底：``engine.py`` 启动时会检查 daemon 的
``Engines`` 里有没有自己，**有**就跳过自注册，避免同一个引擎注册两份、
进而抢总线名。
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

import dbus
import dbus.mainloop.glib
from dbus.mainloop.glib import DBusGMainLoop

from . import ibusproto as proto
from .branding import DISPLAY_NAME, LICENSE
from .engine import BUS_NAME, resolve_ibus_address

VERSION = "1.0.0"

_EMPTY_DICT = dbus.Dictionary({}, signature="sv")
_EMPTY_VARIANTS = dbus.Array([], signature="v")


def _engine_desc(node: ET.Element) -> "dbus.Struct":
    """按 ``ibus_engine_desc_serialize()`` 的字段顺序拼一个 IBusEngineDesc。"""
    def text(tag: str) -> str:
        child = node.find(tag)
        return (child.text or "") if child is not None else ""

    return dbus.Struct(
        [
            dbus.String("IBusEngineDesc"),
            _EMPTY_DICT,
            dbus.String(text("name")),
            dbus.String(text("longname")),
            dbus.String(text("description")),
            dbus.String(text("language")),
            dbus.String(text("license")),
            dbus.String(text("author")),
            dbus.String(text("icon")),
            dbus.String(text("layout")),
            dbus.UInt32(int(text("rank") or 0)),
            dbus.String(text("hotkeys")),
            dbus.String(text("symbol")),
            dbus.String(text("setup")),
            dbus.String(text("layout_variant")),
            dbus.String(text("layout_option")),
            dbus.String(text("version")),
            dbus.String(text("textdomain")),
            dbus.String(text("icon_prop_key")),
        ],
        signature="sa{sv}ssssssssusssssssss",
        variant_level=1,
    )


def _component(node: ET.Element) -> "dbus.Struct":
    """按 ``ibus_component_serialize()`` 的字段顺序拼一个 IBusComponent。"""
    def text(tag: str) -> str:
        child = node.find(tag)
        return (child.text or "") if child is not None else ""

    engines_node = node.find("engines")
    engines = []
    if engines_node is not None:
        engines = [_engine_desc(e) for e in engines_node.findall("engine")]

    return dbus.Struct(
        [
            dbus.String("IBusComponent"),
            _EMPTY_DICT,
            dbus.String(text("name")),
            dbus.String(text("description")),
            dbus.String(text("version")),
            dbus.String(text("license")),
            dbus.String(text("author")),
            dbus.String(text("homepage")),
            dbus.String(text("exec")),
            dbus.String(text("textdomain")),
            _EMPTY_VARIANTS,                                  # observed-paths
            dbus.Array(engines, signature="v"),
        ],
        signature="sa{sv}ssssssssavav",
        variant_level=1,
    )


def _engine_desc_from_fields(name: str, longname: str, language: str = "zh_CN",
                             symbol: str = "拼", rank: int = 90) -> "dbus.Struct":
    return dbus.Struct(
        [
            dbus.String("IBusEngineDesc"),
            _EMPTY_DICT,
            dbus.String(name),
            dbus.String(longname),
            dbus.String("%s（全拼 / 首字母 / 联想）" % DISPLAY_NAME),
            dbus.String(language),
            dbus.String(LICENSE),
            dbus.String("ycpy"),
            dbus.String("ycpy"),
            dbus.String("us"),
            dbus.UInt32(rank),
            dbus.String(""),          # hotkeys
            dbus.String(symbol),
            dbus.String(""),          # setup
            dbus.String(""),          # layout_variant
            dbus.String(""),          # layout_option
            dbus.String(VERSION),
            dbus.String(""),          # textdomain
            dbus.String(""),          # icon_prop_key
        ],
        signature="sa{sv}ssssssssusssssssss",
        variant_level=1,
    )


def default_component(exec_path: str) -> "dbus.Struct":
    """引擎自注册用的组件描述。"""
    return dbus.Struct(
        [
            dbus.String("IBusComponent"),
            _EMPTY_DICT,
            dbus.String(BUS_NAME),
            dbus.String("%s（纯 Python，支持首字母与联想）" % DISPLAY_NAME),
            dbus.String(VERSION),
            dbus.String(LICENSE),
            dbus.String("ycpy"),
            dbus.String(""),
            dbus.String(exec_path),
            dbus.String(""),
            _EMPTY_VARIANTS,
            dbus.Array([_engine_desc_from_fields("ycpy", DISPLAY_NAME)],
                       signature="v"),
        ],
        signature="sa{sv}ssssssssavav",
        variant_level=1,
    )


def register_component(bus, component) -> None:
    """把组件交给 daemon。

    **必须由组件自己的进程调用**：daemon 会在调用方的连接上创建指向
    ``/org/freedesktop/IBus/Factory`` 的代理，并且 ``destroy_with_factory``
    为真——如果调用方的连接上并没有人占着这个名字，代理会立刻销毁，
    注册的组件也随之被撤销。所以引擎进程要自己占着总线名并导出 Factory。
    """
    iface = dbus.Interface(
        bus.get_object(proto.BUS_NAME, "/org/freedesktop/IBus"), proto.IFACE_BUS
    )
    iface.RegisterComponent(component)


def register(xml_path: str, address: str | None = None) -> int:
    """把 ``xml_path`` 描述的组件注册进 daemon，返回注册的引擎名列表。"""
    node = ET.parse(xml_path).getroot()
    component = _component(node)

    DBusGMainLoop(set_as_default=True)
    address = address or resolve_ibus_address()
    if not address:
        raise SystemExit("找不到 ibus-daemon 地址")

    bus = dbus.bus.BusConnection(address)
    iface = dbus.Interface(bus.get_object("org.freedesktop.IBus", "/org/freedesktop/IBus"),
                           "org.freedesktop.IBus")
    iface.RegisterComponent(component)

    return [str(e[2]) for e in iface.ListEngines() if str(e[2]) == "ycpy"]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法: python3 -m ycpy.register <component.xml>", file=sys.stderr)
        return 2
    path = os.path.abspath(argv[0])
    if not os.path.exists(path):
        print("找不到组件文件: %s" % path, file=sys.stderr)
        return 2
    try:
        names = register(path)
    except dbus.DBusException as exc:
        print("注册失败: %s" % exc, file=sys.stderr)
        return 1
    print("已向 ibus-daemon 注册组件 %s，引擎: %s" % (BUS_NAME, ", ".join(names) or "(无)"))
    return 0 if names else 1


if __name__ == "__main__":
    sys.exit(main())