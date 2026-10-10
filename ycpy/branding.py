"""品牌信息：用户界面上显示的名字与许可标识。

单独一个模块是因为它需要被**两个依赖完全不同的地方**引用：

* ``ycpy/register.py`` —— 往 ibus 注册引擎（GNOME 输入源列表里显示的名字）；
* ``ycpy/preferences.py`` —— GTK 首选项窗口的标题。

而前者会 ``import dbus``、后者不会。让首选项为了一个窗口标题去拉 dbus
进来既没必要，也容易在缺 dbus 的环境里炸掉。所以这个模块不 import 任何
东西。

**显示名与技术标识是两回事**
----------------------------
``DISPLAY_NAME`` 只影响「用户看到的字」。下面这些一概**不能**跟着改，
否则会连累用户已有的配置：

* 总线名 ``ycpy.pinyin.assist``（``engine.BUS_NAME``）——必须与 GNOME
  记下的组件名一致，改了用户要重新添加一次输入法；
* 引擎 ``<name>``（``ycpy``）——GNOME 输入源列表里的内部 key；
* 包名 ``ycpy`` ——决定 ``/usr/bin/ycpy-engine``、``dpkg -r ycpy`` 这些；
* Python 包名 ``ycpy`` 与 ``/usr/lib/ycpy`` 安装路径。
"""

from __future__ import annotations

#: 用户在界面上看到的名字（GNOME 输入源列表、窗口标题、软件中心）。
DISPLAY_NAME = "瀛澈拼音"

#: 许可标识。数据里的整句词频来自 rime-frost 的 ``知频.txt``（GPL-3.0），
#: 所以整个项目是 GPL-3.0-or-later，不是 MIT。
#:
#: 改成 GPL 那次漏了两处：``register.py`` 的两个字段和 ``data/ycpy.xml.in``。
#: ``data/ycpy.metainfo.xml`` 当时改了，所以软件中心显示得对、ibus 注册
#: 显示得不对——这种不一致很难自己发现。
LICENSE = "GPL-3.0-or-later"

__all__ = ["DISPLAY_NAME", "LICENSE"]