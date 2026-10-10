"""IBus 的 D-Bus 线格式封装。

ibus 的输入引擎侧协议（``org.freedesktop.IBus.Engine``）不是普通的方法调用，
而是用 IBus 自己的对象序列化格式在 ``variant`` 里传递文本和候选表。
这里手工拼出与 libibus ``ibus_serializable_serialize_object()`` 完全一致的
字节布局：

    IBusText          (sa{sv}sv) = ("IBusText", {}, text, attrs)
    IBusAttrList      (sa{sv}av) = ("IBusAttrList", {}, [])
    IBusLookupTable   (sa{sv}uubbiavav)
                      = ("IBusLookupTable", {}, page_size, cursor_pos,
                         cursor_visible, round, orientation, candidates, labels)

即：tuple(类名字符串, 父类字段…, 子类字段…)。
"""

from __future__ import annotations

import dbus

BUS_NAME = "org.freedesktop.IBus"
IFACE_ENGINE = "org.freedesktop.IBus.Engine"
IFACE_BUS = "org.freedesktop.IBus"
IFACE_FACTORY = "org.freedesktop.IBus.Factory"
IFACE_PROPERTIES = "org.freedesktop.DBus.Properties"
PATH_FACTORY = "/org/freedesktop/IBus/Factory"

# IBusPreeditFocusMode
PREEDIT_FOCUS_MODE_NONE = 0
PREEDIT_FOCUS_MODE_LOCATION = 1

# IBusOrientation
ORIENTATION_HORIZONTAL = 0

# IBusInputPurpose（我们只关心数字/电话这类需要切成英文的场景）
INPUT_PURPOSE_UNKNOWN = 0
INPUT_PURPOSE_FREE_FORM = 1
INPUT_PURPOSE_ALPHA = 2
INPUT_PURPOSE_DIGITS = 3
INPUT_PURPOSE_NUMBER = 4
INPUT_PURPOSE_PHONE = 5
INPUT_PURPOSE_URL = 6
INPUT_PURPOSE_EMAIL = 7
INPUT_PURPOSE_NAME = 8
INPUT_PURPOSE_PASSWORD = 9

_EMPTY_DICT = dbus.Dictionary({}, signature="sv")
_EMPTY_VARIANTS = dbus.Array([], signature="v")


def make_text(s: str) -> "dbus.Struct":
    """构造一个 ``v`` 类型的 IBusText。"""
    attrs = dbus.Struct(
        [dbus.String("IBusAttrList"), _EMPTY_DICT, _EMPTY_VARIANTS],
        signature="sa{sv}av",
        variant_level=1,
    )
    return dbus.Struct(
        [dbus.String("IBusText"), _EMPTY_DICT, dbus.String(s), attrs],
        signature="sa{sv}sv",
        variant_level=1,
    )


def make_lookup_table(candidates, cursor_pos: int, page_size: int = 9) -> "dbus.Struct":
    """构造一个 ``v`` 类型的 IBusLookupTable。"""
    cand = dbus.Array([make_text(c) for c in candidates], signature="v")
    return dbus.Struct(
        [
            dbus.String("IBusLookupTable"),
            _EMPTY_DICT,
            dbus.UInt32(page_size),
            dbus.UInt32(cursor_pos),
            dbus.Boolean(True),
            dbus.Boolean(False),
            dbus.Int32(ORIENTATION_HORIZONTAL),
            cand,
            _EMPTY_VARIANTS,
        ],
        signature="sa{sv}uubbiavav",
        variant_level=1,
    )


def _serialize_obj(children, signature: str) -> "dbus.Struct":
    """按 ibus 的「类名 + 父类字段 + 子类字段」布局打包成一个 variant。"""
    return dbus.Struct(children, signature=signature, variant_level=1)


# IBusPropType（src/ibusproperty.h）。注意 1 是 TOGGLE 而不是 MENU——
# 搞错的话 GNOME 会把「首选项」渲染成一个开关按钮，而不是可点击的菜单项。
PROP_TYPE_NORMAL = 0
PROP_TYPE_TOGGLE = 1
PROP_TYPE_RADIO = 2
PROP_TYPE_MENU = 3
PROP_TYPE_SEPARATOR = 4


def make_prop_list(props) -> "dbus.Struct":
    """IBusPropList = ("IBusPropList", {}, [IBusProperty...])，签名 sa{sv}av。"""
    return _serialize_obj(
        [dbus.String("IBusPropList"), _EMPTY_DICT,
         dbus.Array(list(props), signature="v")],
        "sa{sv}av",
    )


def make_property(key: str, label: str, state: int = 0,
                  prop_type: int = PROP_TYPE_NORMAL) -> "dbus.Struct":
    """IBusProperty 的字段顺序严格对应 ``ibus_property_serialize()``：

    ("IBusProperty", {}, key, type, label:v, icon:s, tooltip:v,
                    sensitive:b, visible:b, state:u, sub_props:v, symbol:v)

    注意 label/tooltip 是**嵌套 variant**（装着序列化的 IBusText），sub_props
    是嵌套的 IBusPropList——字段类型写错 daemon 那边就解析不出来。

    ``prop_type`` 决定 GNOME 怎么画它（见 ui/status/keyboard.js）：
    ``NORMAL`` 是普通可点项，``TOGGLE`` 会画成**开关按钮**，
    ``MENU`` 是子菜单。「首选项」要用 ``NORMAL``。
    """
    empty_props = make_prop_list([])
    empty_text = make_text("")
    text = make_text(label) if label else empty_text
    return _serialize_obj(
        [
            dbus.String("IBusProperty"),
            _EMPTY_DICT,
            dbus.String(key),
            dbus.UInt32(int(prop_type)),
            text,                                   # label  : v
            dbus.String(""),                        # icon   : s
            text,                                   # tooltip: v
            dbus.Boolean(True),                     # sensitive
            dbus.Boolean(True),                     # visible
            dbus.UInt32(int(state)),                # state
            empty_props,                            # sub_props : v
            empty_text,                             # symbol : v
        ],
        "sa{sv}usvsvbouvv",
    )


ENGINE_INTROSPECTION = """
<node>
  <interface name='org.freedesktop.IBus.Engine'>
    <method name='ProcessKeyEvent'>
      <arg direction='in'  type='u' name='keyval' />
      <arg direction='in'  type='u' name='keycode' />
      <arg direction='in'  type='u' name='modifier' />
      <arg direction='out' type='b' />
    </method>
    <method name='SetCursorLocation'>
      <arg direction='in' type='i' name='x' />
      <arg direction='in' type='i' name='y' />
      <arg direction='in' type='i' name='w' />
      <arg direction='in' type='i' name='h' />
    </method>
    <method name='FocusIn' />
    <method name='FocusInId'>
      <arg direction='in' type='s' name='object_path' />
      <arg direction='in' type='s' name='client' />
    </method>
    <method name='FocusOut' />
    <method name='FocusOutId'>
      <arg direction='in' type='s' name='object_path' />
    </method>
    <method name='Reset' />
    <method name='Enable' />
    <method name='Disable' />
    <method name='PageUp' />
    <method name='PageDown' />
    <method name='CursorUp' />
    <method name='CursorDown' />
    <method name='PropertyActivate'>
      <arg direction='in' type='s' name='name' />
      <arg direction='in' type='u' name='state' />
    </method>
    <method name='PropertyShow'>
      <arg direction='in' type='s' name='name' />
    </method>
    <method name='PropertyHide'>
      <arg direction='in' type='s' name='name' />
    </method>
    <method name='CandidateClicked'>
      <arg direction='in' type='u' name='index' />
      <arg direction='in' type='u' name='button' />
      <arg direction='in' type='u' name='state' />
    </method>
    <method name='SetSurroundingText'>
      <arg direction='in' type='v' name='text' />
      <arg direction='in' type='u' name='cursor_pos' />
      <arg direction='in' type='u' name='anchor_pos' />
    </method>
    <method name='ProcessHandWritingEvent'>
      <arg direction='in' type='ad' name='coordinates' />
    </method>
    <method name='CancelHandWriting'>
      <arg direction='in' type='u' name='n_strokes' />
    </method>
    <method name='SetCapabilities'>
      <arg direction='in' type='u' name='caps' />
    </method>
    <method name='SetContentType'>
      <arg direction='in' type='u' name='purpose' />
      <arg direction='in' type='u' name='hints' />
    </method>
    <property name='ContentType' type='(uu)' access='write' />
    <property name='FocusId' type='(b)' access='read' />
    <property name='ActiveSurroundingText' type='(b)' access='read' />
    <signal name='CommitText'>
      <arg type='v' name='text' />
    </signal>
    <signal name='UpdatePreeditText'>
      <arg type='v' name='text' />
      <arg type='u' name='cursor_pos' />
      <arg type='b' name='visible' />
      <arg type='u' name='mode' />
    </signal>
    <signal name='UpdateAuxiliaryText'>
      <arg type='v' name='text' />
      <arg type='b' name='visible' />
    </signal>
    <signal name='UpdateLookupTable'>
      <arg type='v' name='table' />
      <arg type='b' name='visible' />
    </signal>
    <signal name='RegisterProperties'>
      <arg type='v' name='props' />
    </signal>
    <signal name='UpdateProperty'>
      <arg type='v' name='prop' />
    </signal>
    <signal name='ForwardKeyEvent'>
      <arg type='u' name='keyval' />
      <arg type='u' name='keycode' />
      <arg type='u' name='modifier' />
    </signal>
    <signal name='PanelExtension'>
      <arg type='v' name='data' />
    </signal>
  </interface>
</node>
"""

FACTORY_INTROSPECTION = """
<node>
  <interface name='org.freedesktop.IBus.Factory'>
    <method name='CreateEngine'>
      <arg direction='in'  type='s' name='name' />
      <arg direction='out' type='o' />
    </method>
  </interface>
</node>
"""