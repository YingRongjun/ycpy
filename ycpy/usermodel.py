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
import os
import tempfile
import time

_BATCH = 25
_FLUSH_INTERVAL = 5.0


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
        self.load()

    # ---------------------------------------------------------------- 持久化

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            return
        if data.get("version") != 1:
            return
        self.words = {k: int(v) for k, v in data.get("words", {}).items()}
        self.bigrams = {k: int(v) for k, v in data.get("bigrams", {}).items()}

    def flush(self) -> None:
        if not self._dirty:
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
        self.words.clear()
        self.bigrams.clear()
        self.flush()