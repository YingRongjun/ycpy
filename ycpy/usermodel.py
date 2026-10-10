"""用户词频 / 二元组（联想）模型。

数据落在 ``$XDG_DATA_HOME/py-pinyin-ibus/user.json``，格式::

    {
      "version": 1,
      "words":   {"程序员": 37},
      "bigrams": {"程序员|计算机": 12}
    }

写入做了防抖（``schedule_save``），并保证原子替换，避免进程被杀时损坏文件。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time

_BATCH = 25
_FLUSH_INTERVAL = 5.0


def _int_dict(raw) -> dict[str, int]:
    """把 JSON 里的 ``{词: 次数}`` 规整成 ``{str: int}``，跳过坏记录。

    词频文件是用户数据，会因跨版本升级、磁盘写坏、手工编辑而变形。**逐条丢弃
    坏记录、保留好的**，比整个文件不要合理得多——丢一条词频只是少一次排序
    提示，丢掉整个文件是用户攒了几个月的学习成果全没了。

    坏的形态实测过5 种：值不是数字（``"abc"`` / ``null`` / 列表 / 字典）、
    键不是字符串（JSON 允许数字键，``json`` 会把它变成 ``"123"``，那是好的）。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        # bool 是 int 的子类，但 True 不该被当成 1 次。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        # **必须显式排除 NaN / ±Infinity。** 上面那行 isinstance 只挡了
        # 非数字类型，这三个都是 float，都能通过——然后在 int() 上炸：
        #
        #   NaN       -> ValueError: cannot convert float NaN to integer
        #   Infinity  -> OverflowError: cannot convert float infinity to integer
        #   1e309     -> JSON 解析时就是 inf，同上
        #
        # 而 ``value <= 0`` 挡不住它们：**NaN 的任何比较都是 False**，
        # 所以它直接绕过那道过滤坠到 int() 上。这是上一版漏掉的——我在这里
        # 写着「绝不能抛异常」，却只测了 5 种坏输入，浮点特殊值没覆盖到。
        #
        # 用 math.isfinite 而不是 ``value == value``（NaN 自反为假）那种写法：
        # 前者顺带把 ±Infinity 一起挡掉，且意图直白。
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if value <= 0:
            continue
        out[key] = int(value)
    return out


class UserModel:
    def __init__(self, path: str | None = None):
        if path is None:
            base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
            path = os.path.join(base, "py-pinyin-ibus", "user.json")
        self.path = path
        self.words: dict[str, int] = {}
        self.bigrams: dict[str, int] = {}
        self._dirty = 0
        self._last_save = 0.0
        #: 磁盘上的文件不是本程序写的（version 不是 1）时置位，此后不再回写，
        #: 避免用空数据覆盖用户在新版机器上攒的词频。见 ``load``。
        self._read_only = False
        self.load()

    # ---------------------------------------------------------------- 持久化

    def load(self) -> None:
        """从磁盘载入用户词频。

        **这个函数绝不能抛异常**——它在 ``__init__`` 里，也就是引擎的启动
        路径上。一个坏文件让输入法完全起不来，比「词频丢了」严重得多。

        这里的 ``try`` 原来只包住了 ``json.load``，后面的 ``data.get()`` 和
        ``int(v)`` 都在外面，于是这些全都能让引擎崩掉（实测 5 种）：
        ``{"words":{"你好":"abc"}}``（int 失败）、``{"words":{"你好":null}}``
        （int 失败）、顶层是数组或 ``null``（``.get`` 失败）。

        用户数据会因跨版本升级、磁盘写坏、手工编辑而变形，所以**逐条丢弃
        坏记录、保留好的**，而不是整个文件不要。
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            return
        # 版本不是 1 时**标记为只读**，不再往回写。
        #
        # 原来的行为是「认不出来就当空文件」，可第一次 ``flush()`` 就会把
        # 用户的词频整个覆盖掉：用户从装了新版的机器回到旧版（或者手改了
        # version 字段），攒了几个月的学习成果就在下一次选词时没了，而且
        # **没有任何提示**——旧版读不出新版的数据本来就正常，静默覆盖才是
        # 不可接受的那一半。
        #
        # 只读状态下 learn() 仍可在内存里工作，但 flush() 直接返回，所以
        # 旧文件原封不动。
        if not isinstance(data, dict) or data.get("version") != 1:
            self._read_only = True
            return
        self.words = _int_dict(data.get("words"))
        self.bigrams = _int_dict(data.get("bigrams"))

    def flush(self) -> None:
        if not self._dirty or self._read_only:
            return
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(
            {
                "version": 1,
                "words": self.words,
                "bigrams": self.bigrams,
            },
            ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".user-", suffix=".json")
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
        self._dirty = 0
        self._last_save = time.monotonic()

    def schedule_save(self) -> None:
        """按写入次数 + 时间间隔防抖落盘。"""
        self._dirty += 1
        if self._dirty >= _BATCH or (time.monotonic() - self._last_save) > _FLUSH_INTERVAL:
            self.flush()

    # ------------------------------------------------------------------ 学习

    def learn(self, word: str, context: str | None = None) -> None:
        """记录一次选中：词频 +1；若 ``context`` 非空则二元组 +1。"""
        self.words[word] = self.words.get(word, 0) + 1
        if context:
            key = context + "|" + word
            self.bigrams[key] = self.bigrams.get(key, 0) + 1
        self.schedule_save()

    def word_count(self, word: str) -> int:
        return self.words.get(word, 0)

    def bigram_count(self, context: str | None, word: str) -> int:
        if not context:
            return 0
        return self.bigrams.get(context + "|" + word, 0)

    def context_words(self, context: str | None, limit: int = 32) -> list[tuple[str, int]]:
        """历史上跟在 ``context`` 后面出现过的词，按次数降序。"""
        if not context:
            return []
        prefix = context + "|"
        items = [
            (k[len(prefix):], v) for k, v in self.bigrams.items() if k.startswith(prefix)
        ]
        items.sort(key=lambda kv: -kv[1])
        return items[:limit]

    def forget(self) -> None:
        """清空全部学习数据并立刻落盘。

        ``flush()`` 有 ``_dirty`` 门（没脏就不写，省磁盘），
        ``clear()`` 不改 ``_dirty`` —— 所以原样调 ``flush()`` **什么都不会
        写盘**，清空只存在于内存里，重启就回来了。必须先置脏。
        """
        self.words.clear()
        self.bigrams.clear()
        self._dirty = 1
        self.flush()