"""ycpy 首选项对话框（GTK3）。

单独进程运行，保存配置后引擎会在下一次按键时自动重新读取，不需要重启输入法。

用法::

    python3 -m ycpy.preferences
    ycpy-preferences            # deb 装出来的启动器（同 ycpy-engine）
"""

from __future__ import annotations

import sys

from .config import (
    CONTROL_MASK,
    MOD1_MASK,
    MODIFIER_NAMES,
    PAGE_SIZE_CHOICES,
    Config,
    hotkey_name,
    matches_hotkey,
)

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk
except (ImportError, ValueError) as exc:  # pragma: no cover - 环境问题
    print("无法加载 GTK3：%s\n请先安装：sudo apt install python3-gi gir1.2-gtk-3.0" % exc,
          file=sys.stderr)
    raise SystemExit(1)


class HotkeyButton(Gtk.Button):
    """点一下然后按组合键即可重新绑定。"""

    def __init__(self, label: str, on_changed, initial: dict):
        super().__init__(label=label)
        self.set_hexpand(True)
        self._hotkey = dict(initial) if initial else {"keyval": 0, "modifiers": 0}
        self._on_changed = on_changed
        self._capturing = False
        self.update_label()
        self.connect("clicked", self._on_clicked)

    def hotkey(self) -> dict:
        return dict(self._hotkey)

    def set_hotkey(self, hotkey: dict) -> None:
        self._hotkey = dict(hotkey)
        self.update_label()

    def update_label(self) -> None:
        self.set_label(hotkey_name(self._hotkey))

    def _on_clicked(self, _btn) -> None:
        self._capturing = True
        self.set_label("请按下组合键…（Esc 取消）")
        dialog = self.get_toplevel()
        if isinstance(dialog, Gtk.Window):
            dialog._capture_target = self  # noqa: SLF001
            Gdk.Seat.grab(Gdk.Display.get_default(), Gdk.SeatCapabilities.ALL, False,
                          None, None, None, None)

    def handle_key(self, keyval: int, modifier: int) -> bool:
        """对话框收到按键时调用；返回 True 表示已处理。"""
        if not self._capturing:
            return False
        if keyval == 0xFF1B:  # Esc 取消
            self._capturing = False
            self.update_label()
            return True
        mods = 0
        if modifier & CONTROL_MASK:
            mods |= CONTROL_MASK
        if modifier & MOD1_MASK:
            mods |= MOD1_MASK
        self._hotkey = {"keyval": int(keyval), "modifiers": mods}
        self._capturing = False
        self.update_label()
        self._on_changed(self._hotkey)
        return True


class PreferencesWindow(Gtk.Window):
    def __init__(self, config: Config):
        from .branding import DISPLAY_NAME
        super().__init__(title="%s — 首选项" % DISPLAY_NAME)
        self.config = config
        self.set_default_size(560, 460)
        self.set_border_width(12)
        self._capture_target = None
        self._building = True

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add(root)

        root.pack_start(self._build_general(), True, True, 0)
        root.pack_start(self._build_keys(), True, True, 0)
        root.pack_start(self._build_domains(), True, True, 0)
        root.pack_start(self._build_learning(), True, True, 0)
        root.pack_start(self._build_buttons(), False, False, 0)

        self._building = False
        self.connect("key-press-event", self._on_key_press)
        self.connect("delete-event", self._on_delete)

    def _on_delete(self, *_args) -> bool:
        """标题栏的关闭按钮 / Alt+F4。

        **必须返回单个 ``False``，不能返回元组。**

        原来写的是 ``lambda *_: (self._save(), False)``，想表达「先存盘再让
        窗口关掉」。但 ``delete-event`` 只有一个返回值（gboolean），PyGObject
        收到元组会按「多返回值」去解包，结果 GTK 那边拿到的是 **True**，
        而 True 的含义是「**阻止**默认处理」——于是窗口根本不销毁，点关闭
        按钮毫无反应（用户报的就是这个）。

        返回 False 才是「照常销毁」。
        """
        self._save()
        return False

    # ------------------------------------------------------------- 各个分区

    def _build_general(self) -> Gtk.Widget:
        frame = Gtk.Frame(label="常规")
        grid = Gtk.Grid(row_spacing=8, column_spacing=12, border_width=12)
        frame.add(grid)

        grid.attach(Gtk.Label(label="每页候选词数量:", xalign=1), 0, 0, 1, 1)
        self.page_size_combo = Gtk.ComboBoxText()
        for n in PAGE_SIZE_CHOICES:
            self.page_size_combo.append(str(n), str(n))
        self.page_size_combo.set_active_id(str(self.config.page_size))
        self.page_size_combo.set_hexpand(True)
        self.page_size_combo.connect("changed", self._on_page_size_changed)
        grid.attach(self.page_size_combo, 1, 0, 1, 1)

        # 数字键的选法对 10 这一档才不显然（0 才轮得到第 10 个），
        # 与「联想与学习」里那两条 hint 一样写清楚，省得用户按 0 没反应。
        hint = Gtk.Label(
            label="数字键 1~9 直选第 1~9 个，0 选当页最后一个；"
                  "上限 10，正好用满这 10 个键。",
            xalign=0, wrap=True)
        hint.get_style_context().add_class("dim-label")
        grid.attach(hint, 0, 1, 2, 1)

        grid.attach(Gtk.Label(label="配置文件:", xalign=1), 0, 2, 1, 1)
        path_label = Gtk.Label(label=self.config.path, xalign=0)
        path_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        path_label.set_selectable(True)
        path_label.set_hexpand(True)
        grid.attach(path_label, 1, 2, 1, 1)
        return frame

    def _build_keys(self) -> Gtk.Widget:
        frame = Gtk.Frame(label="快捷键")
        grid = Gtk.Grid(row_spacing=8, column_spacing=12, border_width=12)
        frame.add(grid)

        grid.attach(Gtk.Label(label="中文 / 英文切换:", xalign=1), 0, 0, 1, 1)
        self.switch_button = HotkeyButton("", self._on_switch_changed,
                                          self.config.zh_en_switch)
        grid.attach(self.switch_button, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="联想输入（按一次出后继词）:", xalign=1), 0, 1, 1, 1)
        self.assoc_button = HotkeyButton("", self._on_assoc_changed,
                                         self.config.associate_key)
        grid.attach(self.assoc_button, 1, 1, 1, 1)
        return frame

    def _build_domains(self) -> Gtk.Widget:
        """专业领域词库的勾选区。"""
        from .domain import DOMAINS, available_domains
        frame = Gtk.Frame(label="专业词库")
        frame.set_margin_top(6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      border_width=12)
        frame.add(box)

        present = available_domains()
        if not present:
            # 数据文件没随包分发（可能手工删了）。这时给一句说明，而不是
            # 摆一个空白的框让人以为功能坏了。
            empty = Gtk.Label(
                label="未找到专业词库数据文件（data/ycpy-domain-*.txt）。",
                xalign=0, wrap=True)
            empty.get_style_context().add_class("dim-label")
            box.pack_start(empty, False, False, 0)
            return frame

        self._domain_checks: dict[str, Gtk.CheckButton] = {}
        chosen = set(self.config.domains)
        grid = Gtk.Grid(row_spacing=6, column_spacing=18)
        for i, key in enumerate(present):
            label = DOMAINS[key][1]
            check = Gtk.CheckButton(label=label)
            check.set_active(key in chosen)
            check.connect("toggled", self._on_domain_toggled, key)
            grid.attach(check, i % 3, i // 3, 1, 1)
            self._domain_checks[key] = check
        box.pack_start(grid, False, False, 0)

        hint = Gtk.Label(
            label="勾选后，该领域的专业词汇会排在普通词前面。\n"
                  "数据来自清华开放词库 THUOCL（MIT）。改动下次按键即生效，"
                  "不需要重启输入法。",
            xalign=0, wrap=True)
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)
        return frame

    def _on_domain_toggled(self, button: Gtk.CheckButton, key: str) -> None:
        if self._building:
            return
        self.config.set("domains", self._selected_domains())

    def _selected_domains(self) -> list[str]:
        """按 ``DOMAINS`` 的固定顺序返回已勾选的领域。

        顺序固定是为了让 ``config.json`` 里的列表稳定——不然用户来回勾两下
        就会写成不同顺序，配置文件每次都变，没法做「有没有改」的判断。
        """
        return [k for k, checks in self._domain_checks.items()
                if checks.get_active()]

    def _build_learning(self) -> Gtk.Widget:
        frame = Gtk.Frame(label="联想与学习")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=12)
        frame.add(box)

        self.assoc_switch = Gtk.Switch()
        self.assoc_switch.set_active(self.config.enable_associate)
        self.assoc_switch.connect("notify::active", self._on_assoc_toggled)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.pack_start(Gtk.Label(label="启用联想输入", xalign=0), True, True, 0)
        row.pack_end(self.assoc_switch, False, False, 0)
        box.pack_start(row, False, False, 0)

        self.auto_assoc_switch = Gtk.Switch()
        self.auto_assoc_switch.set_active(self.config.auto_associate_after_commit)
        self.auto_assoc_switch.connect("notify::active", self._on_auto_assoc_toggled)
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row2.pack_start(Gtk.Label(label="每次上屏后自动进入联想", xalign=0), True, True, 0)
        row2.pack_end(self.auto_assoc_switch, False, False, 0)
        box.pack_start(row2, False, False, 0)

        self.learn_switch = Gtk.Switch()
        self.learn_switch.set_active(self.config.enable_learning)
        self.learn_switch.connect("notify::active", self._on_learn_toggled)
        row3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row3.pack_start(Gtk.Label(label="学习我的用词（越用越准）", xalign=0), True, True, 0)
        row3.pack_end(self.learn_switch, False, False, 0)
        box.pack_start(row3, False, False, 0)

        self.punct_switch = Gtk.Switch()
        self.punct_switch.set_active(self.config.enable_punctuation)
        self.punct_switch.connect("notify::active", self._on_punct_toggled)
        row4 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row4.pack_start(Gtk.Label(label="中文标点（, → ，  . → 。）", xalign=0), True, True, 0)
        row4.pack_end(self.punct_switch, False, False, 0)
        box.pack_start(row4, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        grid.attach(Gtk.Label(label="我的词频权重:", xalign=1), 0, 0, 1, 1)
        self.weight_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100000, 1000
        )
        self.weight_scale.set_value(self.config.user_freq_weight)
        self.weight_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.weight_scale.set_hexpand(True)
        self.weight_scale.connect("value-changed", self._on_weight_changed)
        grid.attach(self.weight_scale, 1, 0, 1, 1)
        box.pack_start(grid, False, False, 0)

        hint = Gtk.Label(
            label="权重越大，你自己常用的词排得越前；调到 0 则完全按语料词频排序。",
            xalign=0, wrap=True)
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)

        self.fuzzy_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 60, 1
        )
        self.fuzzy_scale.set_value(self.config.fuzzy_min_exact)
        self.fuzzy_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.fuzzy_scale.set_hexpand(True)
        self.fuzzy_scale.connect("value-changed", self._on_fuzzy_changed)
        grid2 = Gtk.Grid(row_spacing=8, column_spacing=12)
        grid2.attach(Gtk.Label(label="容错阈值:", xalign=1), 0, 0, 1, 1)
        grid2.attach(self.fuzzy_scale, 1, 0, 1, 1)
        box.pack_start(grid2, False, False, 0)

        hint2 = Gtk.Label(
            label="精确匹配少于这么多条时，额外猜一下你是不是漏敲了元音、"
                  "声母不分，或者首字母里混进了元音。\n"
                  "例如 nha→你好、chxy→程序员、cxuy→程序员。"
                  "调到 0 则不猜，只按你敲的字面匹配。",
            xalign=0, wrap=True)
        hint2.get_style_context().add_class("dim-label")
        box.pack_start(hint2, False, False, 0)
        return frame

    def _build_buttons(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_halign(Gtk.Align.END)

        reset = Gtk.Button(label="恢复默认")
        reset.connect("clicked", self._on_reset)
        box.pack_start(reset, False, False, 0)

        cancel = Gtk.Button(label="取消")
        cancel.connect("clicked", lambda *_: self.destroy())
        box.pack_start(cancel, False, False, 0)

        ok = Gtk.Button(label="保存")
        ok.get_style_context().add_class("suggested-action")
        ok.connect("clicked", self._on_save)
        box.pack_start(ok, False, False, 0)
        return box

    # --------------------------------------------------------------- 事件

    def _on_key_press(self, _widget, event) -> bool:
        target = self._capture_target
        if target is not None and target.handle_key(event.keyval, event.state):
            self._capture_target = None
            return True
        return False

    def _on_page_size_changed(self, combo) -> None:
        if self._building:
            return
        value = combo.get_active_id()
        if value:
            self.config.set("page_size", int(value))

    def _on_switch_changed(self, hotkey) -> None:
        if not self._building:
            self.config.set("zh_en_switch", hotkey)

    def _on_assoc_changed(self, hotkey) -> None:
        if not self._building:
            self.config.set("associate_key", hotkey)

    def _on_assoc_toggled(self, switch, _param) -> None:
        if not self._building:
            self.config.set("enable_associate", switch.get_active())

    def _on_auto_assoc_toggled(self, switch, _param) -> None:
        if not self._building:
            self.config.set("auto_associate_after_commit", switch.get_active())

    def _on_learn_toggled(self, switch, _param) -> None:
        if not self._building:
            self.config.set("enable_learning", switch.get_active())

    def _on_punct_toggled(self, switch, _param) -> None:
        if not self._building:
            self.config.set("enable_punctuation", switch.get_active())

    def _on_weight_changed(self, scale) -> None:
        if self._building:
            return
        self.config.set("user_freq_weight", int(scale.get_value()))

    def _on_fuzzy_changed(self, scale) -> None:
        if self._building:
            return
        self.config.set("fuzzy_min_exact", int(scale.get_value()))

    def _on_reset(self, _btn) -> None:
        from .config import DEFAULTS

        self.config.data = dict(DEFAULTS)
        self._save()
        win = PreferencesWindow(self.config)
        win.show_all()
        self.destroy()

    def _on_save(self, _btn) -> None:
        self._save()
        self.destroy()

    def _save(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            print("保存配置失败：%s" % exc, file=sys.stderr)

    def show_all(self) -> None:
        super().show_all()


def main(argv=None) -> int:
    config = Config()
    win = PreferencesWindow(config)
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())