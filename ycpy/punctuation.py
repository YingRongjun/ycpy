"""中文标点映射。

输入法在中文态下把英文标点转成中文标点，这是每个中文输入法都有的基本功能：
打 ``,`` 得到 ``，``、打 ``.`` 得到 ``。``，等等。

两处和别的输入法不一样的地方，都是为了不和本项目自己的键位冲突：

* **分号 ``;`` 归给标点，不再触发联想。** 联想热键可以在首选项里改，
  默认改成了 ``Ctrl+;``（其他输入法也普遍用这个组合键做联想/分句）。
* **不在组字过程中用标点上屏拼音**，���是先把拼音原样上屏（回车）再打标点，
  免得用户想打「你好，」时按错顺序。
"""

from __future__ import annotations

#: 无 Shift 时的映射
PUNCTUATION = {
    ",": "，",   # 逗号
    ".": "。",   # 句号
    "?": "？",   # 问号
    "!": "！",   # 感叹号
    ";": "；",   # 分号
    ":": "：",   # 冒号
    "'": "’",   # 右单引号
    "`": "、",   # 顿号
    "<": "〈",   # 单书名号
    ">": "〉",
    "(": "（",   # 全角括号
    ")": "）",
    "[": "【",   # 实心方括号
    "]": "】",
    "\\": "＼",  # 全角反斜杠
    "~": "～",   # 全角波浪号
    "^": "……",  # 省略号
    "_": "——",  # 破折号
    "$": "￥",   # 全角人民币符号
}

#: 按住 Shift 时的映射（同一物理键的「上档」）
SHIFT_PUNCTUATION = {
    '"': "”",   # 右双引号
    "<": "《",   # 书名号
    ">": "》",
    "~": "ˇ",   # 声调符号
    "?": "？",
    "!": "！",
    ":": "：",
    ";": "；",
    ".": "。",
    ",": "，",
}


def convert(key: str, shifted: bool = False) -> str:
    """英文标点 -> 中文标点；不是标点则返回空串。

    >>> convert(",")
    '，'
    >>> convert("<", shifted=True)
    '《'
    >>> convert("a")
    ''
    """
    if not key:
        return ""
    if shifted:
        ch = SHIFT_PUNCTUATION.get(key)
        if ch:
            return ch
    return PUNCTUATION.get(key, "")


def is_punctuation(key: int, modifier: int = 0) -> bool:
    """给定 keysym，判断它（在去掉 Shift 后）是不是可转换的标点。"""
    if not (0x20 <= key < 0x7F):
        return False
    ch = chr(key)
    if ch in PUNCTUATION:
        return True
    if modifier & (1 << 0):          # Shift
        return ch in SHIFT_PUNCTUATION
    return False