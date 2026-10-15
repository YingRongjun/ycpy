"""用户配置（首选项）。

配置存放在 ``~/.config/py-pinyin-ibus/config.json``，由首选项对话框写入、
引擎读取。引擎会盯着文件修改时间，改完偏好不用重启输入法就能生效。

读取时永远和 :data:`DEFAULTS` 合并，所以文件缺字段、被手改坏、或者是从旧版本
升级下来的，都不会让引擎起不来。
"""

from __future__ import annotations

import json
import os
import tempfile
import time

# 修饰键位掩码（和 GDK/X11 一致）
SHIFT_MASK = 1 << 0
LOCK_MASK = 1 << 1
CONTROL_MASK = 1 << 2
MOD1_MASK = 1 << 3
MOD2_MASK = 1 << 4
MOD3_MASK = 1 << 5
MOD5_MASK = 1 << 6

#: 匹配热键时必须忽略的位。
#:
#: 除了 shift/lock，**锁定状态位也必须忽略**。客户端上报的 modifier 里总是
#: 带着 NumLock / CapsLock / ScrollLock 的当前状态（GDK 里分别是
#: MOD2_MASK / LOCK_MASK / MOD5_MASK），这些不是用户为这个键按下的。
#:
#: 早先只忽略了 shift/lock，于是「左 Shift 切换中英」这个默认热键
#: （配置里 modifiers=0）在真实 GTK 客户端下永远匹配不上：客户端送来的是
#: ``keyval=Shift_L, modifier=MOD2``，want=0 而 got=MOD2，判定不命中，
#: Shift 被当成普通键透传给应用，表现为「切换不起作用」。
#:
#: Ctrl / Alt 不在忽略之列——它们是真正的组合键，必须参与匹配。
IGNORED_MASK = SHIFT_MASK | LOCK_MASK | MOD2_MASK | MOD3_MASK | MOD5_MASK

#: 常用的修饰键名 -> 掩码
MODIFIER_NAMES = [
    ("", 0),
    ("Ctrl", CONTROL_MASK),
    ("Alt", MOD1_MASK),
    ("Ctrl+Alt", CONTROL_MASK | MOD1_MASK),
]

DEFAULTS = {
    # 每页显示几个候选词
    "page_size": 10,
    # 中文/英文切换：默认左 Shift（很多 IM 就是这么做的）
    "zh_en_switch": {"keyval": 0xFFE1, "modifiers": 0},
    # 联想功能开关与热键。
    # 联想键默认 Ctrl+; —— 分号要留给中文标点「；」，而「上屏并联想后继词」
    # 用组合键更不容易误触（其他输入法也普遍用 Ctrl+; 做联想/分句）。
    "enable_associate": True,
    "associate_key": {"keyval": 0x3B, "modifiers": 1 << 2},  # Ctrl + ;
    # 是否记录用户词频（关掉就退化成纯语料排序）
    "enable_learning": True,
    # 用户词频在打分里的权重，0 表示不参与排序
    "user_freq_weight": 60000,
    # 选词后是否自动进入联想（关掉则每选一次都回到普通输入）
    "auto_associate_after_commit": False,
    # 容错：精确命中少于这么多条时，额外去试「猜出来的变体」。
    # 12 意味着 `nha`（只命中「南海岸」1 条）会被照顾到，而 `cxy`
    # （命中 104 条）说明敲得很准，不再打扰。设成 0 可关闭这条补充路径。
    "fuzzy_min_exact": 12,
    # 中文态下把英文标点转成中文标点（, -> ， . -> 。 等）
    "enable_punctuation": True,
    # 用户勾选的专业领域词库。默认全空——专业词会插到普通词前面
    # （W_DOMAIN=150000），这是强行为，按需求就该由用户主动开启。
    # 取值是领域标识，见 ``ycpy/domain.py`` 的 ``DOMAINS``。
    "domains": [],
}

#: 老版本把分号当联想键。现在分号要留给中文标点「；」，
#: 老配置里仍然是「无修饰的分号」时自动改成 ``Ctrl+;``，免得用户升级后
#: 联想失灵（分号被标点吃掉了）却找不到原因。
_LEGACY_SEMICOLON_ASSOCIATE = {"keyval": 0x3B, "modifiers": 0}
_MIGRATIONS = (
    ("associate_key", _LEGACY_SEMICOLON_ASSOCIATE, DEFAULTS["associate_key"]),
)

#: 候选数量可选值：**5 / 7 / 10，没有 9**。
#:
#: 上限是 10 —— 数字键总共只有 1~9 和 0 这 10 个，再多就没法用数字键直选。
#: 10 这一档靠 ``_digit_index`` 的「1~9 -> 第 1~9 项、0 -> 当页最后一项」
#: 自然成立：当页正好 10 个候选时 ``0`` 落在第 10 项，一条新分支都不用加。
#:
#: 页长小于 10 时 ``0`` 和最大的那个数字键会指向同一项（9 时按 0 与按 9
#: 都是第 9 项，5、7 同理），所以只有 10 这一档是 10 个数字键各司其职。
#: **9 已按需求去掉**——它比 10 少一项，而选词方式上没有任何独到之处。
PAGE_SIZE_CHOICES = (5, 7, 10)


class Config:
    """一份带默认值兜底的用户配置。"""

    def __init__(self, path: str | None = None):
        if path is None:
            base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
            path = os.path.join(base, "py-pinyin-ibus", "config.json")
        self.path = path
        self.data = dict(DEFAULTS)
        self._mtime = 0.0
        self.load()

    # ------------------------------------------------------------------ 读写

    def load(self) -> None:
        raw = {}
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
        except (OSError, ValueError):
            raw = {}
        self.data = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
        if isinstance(raw, dict):
            self._migrate(raw)
        self._mtime = self._stamp()

    def _migrate(self, raw: dict) -> None:
        """把老配置里含义已经变了的键改成新默认值。

        目前只有一处：分号曾经是联想键，现在归中文标点「；」了。
        用户没手动改过的话（正好等于老默认值）就跟着迁移，不用他操心。
        """
        for key, old, new in _MIGRATIONS:
            if raw.get(key) == old:
                self.data[key] = dict(new)

    def _stamp(self) -> int:
        """文件指纹。

        用 ``st_mtime_ns`` 而不是 ``getmtime()``：后者是浮点秒，精度有限，
        短时间内连续保存两次可能落在同一个刻度上，引擎就发现不了变化了。
        """
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return -1

    def reload_if_changed(self) -> bool:
        """配置文件变了就重新读。返回是否真的变了。"""
        if self._stamp() == self._mtime:
            return False
        self.load()
        return True

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._mtime = self._stamp()

    # ------------------------------------------------------------------ 访问

    def get(self, key, fallback=None):
        if key in self.data:
            return self.data[key]
        return DEFAULTS.get(key, fallback)

    def set(self, key, value) -> None:
        self.data[key] = value

    # 常用项的便捷属性
    @property
    def page_size(self) -> int:
        value = self.get("page_size", DEFAULTS["page_size"])
        # 不在可选表里**不报错也不提示**，直接回落默认值——所以漏更新
        # PAGE_SIZE_CHOICES 的症状是「首选项能选、存盘再读就变回去」。
        # 兜底值也走 DEFAULTS，免得两处 9/10 各改各的。
        return value if value in PAGE_SIZE_CHOICES else DEFAULTS["page_size"]

    @property
    def zh_en_switch(self) -> dict:
        return self.get("zh_en_switch") or DEFAULTS["zh_en_switch"]

    @property
    def associate_key(self) -> dict:
        return self.get("associate_key") or DEFAULTS["associate_key"]

    @property
    def fuzzy_min_exact(self) -> int:
        """容错阈值；负数一律当 0（关掉）处理。"""
        value = int(self.get("fuzzy_min_exact",
                             DEFAULTS["fuzzy_min_exact"]))
        return max(0, value)

    @property
    def enable_punctuation(self) -> bool:
        return bool(self.get("enable_punctuation",
                             DEFAULTS["enable_punctuation"]))

    @property
    def domains(self) -> list[str]:
        """用户勾选的专业领域。

        容错要到位：配置文件可能被手改坏（写成字符串、写成 null），
        这时退回空列表（等于没勾选），不能因此让引擎起不来。
        """
        raw = self.get("domains", DEFAULTS["domains"])
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(x) for x in raw if isinstance(x, str)]

    @property
    def enable_associate(self) -> bool:
        return bool(self.get("enable_associate", True))

    @property
    def enable_learning(self) -> bool:
        return bool(self.get("enable_learning", True))

    @property
    def user_freq_weight(self) -> float:
        try:
            return float(self.get("user_freq_weight", 60000))
        except (TypeError, ValueError):
            return 60000.0

    @property
    def auto_associate_after_commit(self) -> bool:
        return bool(self.get("auto_associate_after_commit", False))


def _merge(defaults: dict, override: dict) -> dict:
    """用 override 覆盖 defaults，未知键丢弃。"""
    out = {}
    for key, value in defaults.items():
        if key not in override:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = _merge(value, override[key] if isinstance(override[key], dict) else {})
        else:
            out[key] = override[key]
    return out


def matches_hotkey(keyval: int, modifier: int, hotkey: dict) -> bool:
    """按下的组合键是否命中配置的热键（忽略 shift/lock）。"""
    if not hotkey:
        return False
    if int(hotkey.get("keyval", 0)) != int(keyval):
        return False
    want = int(hotkey.get("modifiers", 0)) & ~IGNORED_MASK
    got = int(modifier) & ~IGNORED_MASK
    return want == got


# --------------------------------------------------------------- 按键显示名

#: 界面里展示用的可读按键名（只覆盖用得到的）
KEY_NAMES = {
    0xFFE1: "左 Shift",
    0xFFE2: "右 Shift",
    0xFFE3: "左 Ctrl",
    0xFFE4: "右 Ctrl",
    0xFFE9: "左 Alt",
    0xFFEA: "右 Alt",
    0x20: "空格",
    0xFF08: "Backspace",
    0xFF09: "Tab",
    0xFF0D: "回车",
    0xFF1B: "Esc",
    0x2C: "逗号 ,",
    0x2E: "句号 .",
    0x2D: "减号 -",
    0x3D: "等号 =",
    0x5C: "反斜杠 \\",
    0x3B: "分号 ;",
    0x27: "单引号 '",
    0x5B: "[",
    0x5D: "]",
    0x2F: "斜杠 /",
}


def key_name(keyval: int) -> str:
    """把 keyval 显示成人能看懂的名字。"""
    keyval = int(keyval)
    if keyval in KEY_NAMES:
        return KEY_NAMES[keyval]
    if 0x61 <= keyval <= 0x7A:
        return chr(keyval).upper()
    if 0x30 <= keyval <= 0x39:
        return chr(keyval)
    if 0xFF00 <= keyval <= 0xFFFF:
        return "按键 0x%02X" % keyval
    return "按键 0x%02X" % keyval


def hotkey_name(hotkey: dict) -> str:
    """把热键字典显示成 ``Ctrl+空格`` 这样的字符串。"""
    if not hotkey:
        return "(未设置)"
    mods = int(hotkey.get("modifiers", 0))
    parts = []
    if mods & CONTROL_MASK:
        parts.append("Ctrl")
    if mods & MOD1_MASK:
        parts.append("Alt")
    parts.append(key_name(int(hotkey.get("keyval", 0))))
    return "+".join(parts)