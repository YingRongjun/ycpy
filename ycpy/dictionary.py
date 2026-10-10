"""汉字/词组与拼音之间的对照表及索引。

词典文件格式（纯文本，UTF-8）::

    词<制表符>词频<制表符>拼音音节序列

* 拼音音节序列用空格分隔，例如 ``cheng xu yuan ji``；
* 同一个词可能出现多行，代表多音字的不同读音；
* 以 ``#`` 开头的行是注释。

索引策略
--------

* ``full``  —— 每个词条读音的全拼字母串（音节直接拼接），已排序；
* ``init``  —— 每个词条读音的首字母串（``ch``/``sh``/``zh`` 缩成 ``c``/``s``/``z``），已排序。

前缀查询在有序数组上二分，得到「所有以该前缀开头的词条」区间。
不建 Trie 的原因：38 万词条 × Trie 节点在 Python 里是几百 MB 内存，
而二分只占几十 MB，而且两种索引天然覆盖「全拼」与「首字母」两种输入风格。

所有字符串都打包成 ``bytes`` 大 blob + 偏移数组来存放，而不是几百万个
Python ``str`` 对象：既省内存，也让引擎启动时的索引加载从 3 秒降到 0.2 秒。
"""

from __future__ import annotations

import os
from array import array

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
DICT_PATH = os.path.join(ROOT, "data", "ycpy-dict.txt")

#: 整句词频表（``tools/build_phrase_freq.py`` 生成）。补 jieba 词典缺的
#: 口语整句——用户报「输入 nihaoma 得到『妳好吗』」，根因是 jieba 里根本
#: 没有「你好吗」这条词，3 个音节只拿到 1 个候选，而那个候选恰好是频次 3
#: 的边角词。没有这个文件是可选降级，只是查不到这类整句。
PHRASE_FREQ_PATH = os.path.join(ROOT, "data", "ycpy-phrase-freq.txt")

CACHE_VERSION = 5

#: 热点词榜长度：够填满好几页，又不至于让冷启动变慢
HOT_WORDS_LIMIT = 60
HOT_CHARS_LIMIT = 60


class PackedStrings:
    """紧凑的字符串列表：一个大 ``bytes`` 加一个 ``uint32`` 偏移数组。"""

    __slots__ = ("blob", "off")

    def __init__(self, blob: bytes, off: array):
        self.blob = blob
        self.off = off

    @classmethod
    def build(cls, items) -> "PackedStrings":
        off = array("I", bytes(4 * (len(items) + 1)))
        chunks = []
        pos = 0
        for i, item in enumerate(items):
            raw = item.encode("utf-8") if isinstance(item, str) else item
            chunks.append(raw)
            pos += len(raw)
            off[i + 1] = pos
        return cls(b"".join(chunks), off)

    def __len__(self) -> int:
        return len(self.off) - 1

    def raw(self, i: int) -> bytes:
        """取第 ``i`` 项的原始字节（拼音键都是 ASCII，可以直接参与比较）。"""
        return self.blob[self.off[i] : self.off[i + 1]]

    def __getitem__(self, i: int) -> str:
        return self.blob[self.off[i] : self.off[i + 1]].decode("utf-8")


class Dictionary:
    """词典及其前缀索引。"""

    def __init__(self, path: str | None = None):
        self.path = path or DICT_PATH
        self.words: PackedStrings
        self.pinyin: PackedStrings
        self.wlen = array("B")      # 词条字数
        self.freq = array("q")      # 基础词频
        self.user_freq = array("q") # 用户词频（启动时由 UserModel 填入）
        self._full_keys = PackedStrings(b"", array("I", bytes(4)))
        self._full_index = array("i")
        self._init_keys = PackedStrings(b"", array("I", bytes(4)))
        self._init_index = array("i")
        self._text_keys = PackedStrings(b"", array("I", bytes(4)))
        self._text_index = array("i")
        self._text_to_id: dict[str, int] | None = None
        self._hot: list[int] | None = None
        self._hot_chars: list[int] | None = None
        self.load()

    # ------------------------------------------------------------------ 载入

    def load(self) -> None:
        if not os.path.exists(self.path):
            raise FileNotFoundError(
                "词典文件不存在: %s\n请先运行 python3 tools/build_dict.py 生成词典。"
                % self.path
            )
        if not self._load_cache():
            self._load_text()
            self._save_cache()

    # ---- 索引缓存：省掉每次启动 3 秒的重建 ----

    def _cache_dir(self) -> str:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        return os.path.join(base, "py-pinyin-ibus")

    def _cache_path(self) -> str:
        st = os.stat(self.path)
        # 缓存键必须覆盖**所有**参与建索引的文件。只拿主词典的 size+mtime
        # 是不够的：整句词频表（phrase freq）单独变化时，缓存会命中一份
        # 过期的索引，新加的整句一个都查不到——而且这种错误是静默的。
        extra = ""
        if os.path.exists(PHRASE_FREQ_PATH):
            st2 = os.stat(PHRASE_FREQ_PATH)
            extra = "-%d-%d" % (st2.st_size, st2.st_mtime_ns)
        return os.path.join(
            self._cache_dir(),
            "index-v%d-%d-%d%s.bin" % (CACHE_VERSION, st.st_size, st.st_mtime_ns, extra),
        )

    def _load_cache(self) -> bool:
        try:
            import marshal

            with open(self._cache_path(), "rb") as fp:
                blob = marshal.load(fp)
        except (OSError, ValueError, EOFError):
            return False
        if not isinstance(blob, dict) or blob.get("version") != CACHE_VERSION:
            return False
        try:
            self.words = PackedStrings(blob["words_blob"], _frombytes(blob["words_off"], "I"))
            self.pinyin = PackedStrings(blob["pinyin_blob"], _frombytes(blob["pinyin_off"], "I"))
            self._full_keys = PackedStrings(blob["full_blob"], _frombytes(blob["full_off"], "I"))
            self._init_keys = PackedStrings(blob["init_blob"], _frombytes(blob["init_off"], "I"))
            self._text_keys = PackedStrings(blob["text_blob"], _frombytes(blob["text_off"], "I"))
            self.wlen = _frombytes(blob["wlen"], "B")
            self.freq = _frombytes(blob["freq"], "q")
            self._full_index = _frombytes(blob["full_index"], "i")
            self._init_index = _frombytes(blob["init_index"], "i")
            self._text_index = _frombytes(blob["text_index"], "i")
        except (KeyError, TypeError):
            return False
        self.user_freq = array("q", bytes(8 * len(self.words)))
        return True

    def _save_cache(self) -> None:
        try:
            import marshal

            directory = self._cache_dir()
            os.makedirs(directory, exist_ok=True)
            blob = {
                "version": CACHE_VERSION,
                "words_blob": self.words.blob,
                "words_off": self.words.off.tobytes(),
                "pinyin_blob": self.pinyin.blob,
                "pinyin_off": self.pinyin.off.tobytes(),
                "wlen": self.wlen.tobytes(),
                "freq": self.freq.tobytes(),
                "full_blob": self._full_keys.blob,
                "full_off": self._full_keys.off.tobytes(),
                "full_index": self._full_index.tobytes(),
                "init_blob": self._init_keys.blob,
                "init_off": self._init_keys.off.tobytes(),
                "init_index": self._init_index.tobytes(),
                "text_blob": self._text_keys.blob,
                "text_off": self._text_keys.off.tobytes(),
                "text_index": self._text_index.tobytes(),
            }
            tmp = self._cache_path() + ".tmp"
            with open(tmp, "wb") as fp:
                marshal.dump(blob, fp)
            os.replace(tmp, self._cache_path())
        except (OSError, ValueError, TypeError):
            pass  # 缓存只是加速，写不进去也无所谓

    def _load_text(self) -> None:
        words: list[str] = []
        pinyin: list[str] = []
        freq = array("q")
        text_to_id: dict[str, int] = {}

        rows: list[tuple[bytes, int]] = []
        init_rows: list[tuple[bytes, int]] = []
        text_rows: list[tuple[bytes, int]] = []

        def ingest(path: str) -> int:
            """读一个「词<TAB>词频<TAB>拼音」文件，灌进各个索引。"""
            added = 0
            with open(path, "rb") as fp:
                for line in fp:
                    if not line or line[:1] == b"#":
                        continue
                    parts = line.rstrip(b"\n").split(b"\t")
                    if len(parts) < 3:
                        continue
                    word_b, freq_b, py_b = parts
                    if not word_b or not py_b:
                        continue
                    word = word_b.decode("utf-8")
                    wid = text_to_id.get(word)
                    if wid is None:
                        wid = len(words)
                        text_to_id[word] = wid
                        words.append(word)
                        pinyin.append(py_b.decode("utf-8"))
                        freq.append(int(freq_b))
                        added += 1
                    rows.append((py_b.replace(b" ", b""), wid))
                    init_rows.append((b"".join(s[:1] for s in py_b.split()), wid))
                    text_rows.append((word_b, wid))
            return added

        ingest(self.path)
        # 整句词频表：补 jieba 词典没有的口语整句（见 build_phrase_freq.py）。
        # 没有它是可选降级——词典本身已经能用，只是「你好吗」这类输入拿不到
        # 正确候选。
        if os.path.exists(PHRASE_FREQ_PATH):
            added = ingest(PHRASE_FREQ_PATH)
            print("ycpy: 载入整句词频 +%d 词条" % added)

        self.words = PackedStrings.build(words)
        self.pinyin = PackedStrings.build(pinyin)
        self.wlen = array("B", (len(w) for w in words))
        self.freq = freq
        self.user_freq = array("q", bytes(8 * len(words)))
        self._text_to_id = text_to_id

        rows.sort()
        self._full_keys = PackedStrings.build([r[0] for r in rows])
        self._full_index = array("i", (r[1] for r in rows))

        init_rows.sort()
        self._init_keys = PackedStrings.build([r[0] for r in init_rows])
        self._init_index = array("i", (r[1] for r in init_rows))

        # 词文本索引：联想输入按「上文最后一个字」反查以该字开头的词
        text_rows.sort()
        self._text_keys = PackedStrings.build([r[0] for r in text_rows])
        self._text_index = array("i", (r[1] for r in text_rows))

    # -------------------------------------------------------------- 热点词

    def hot_words(self, limit: int = HOT_WORDS_LIMIT) -> list[int]:
        """语料里最高频的双字词，作为「打不出拼音时的候选」。

        为什么只要双字词：单字高频榜前几名是「了是在和有的他不我的人也为」这类
        虚词，拿它们当候选没什么用（用户想打的几乎都是双字词）。

        为什么需要它：GNOME 收到**空的** lookup table 根本不画窗口。组字时
        一个候选都匹配不上（比如只敲了一个 `v`）时，如果发空表，用户看到的就是
        「按了键但什么都没发生」，完全不知道输入法还在工作。塞一份热点词进去，
        窗口就一直在，而且这些词本身也是有用的高频输入。
        """
        if self._hot is not None and len(self._hot) >= limit:
            return self._hot[:limit]
        scored = [
            wid for wid in range(len(self.words))
            if self.wlen[wid] == 2 and self.freq[wid] > 0
        ]
        scored.sort(key=lambda wid: -self.freq[wid])
        self._hot = scored[: HOT_WORDS_LIMIT]
        return self._hot[:limit]

    def hot_chars(self, limit: int = HOT_CHARS_LIMIT) -> list[int]:
        """语料里最高频的**单字**，供「只敲了一个字母」时兜底。

        为什么单独一份：``hot_words`` 按设计只要双字词，因为组字时用户想打的
        几乎都是词。但只敲一个韵母（`i`、`u` 这种，它们不是合法音节，查不到
        任何词）时，双字词是误导——`i` 写不出 `yi ge`。这时给几个高频单字
        才符合「我敲了个韵母，想看看常用字」的心智。
        """
        if self._hot_chars is not None and len(self._hot_chars) >= limit:
            return self._hot_chars[:limit]
        scored = [
            wid for wid in range(len(self.words))
            if self.wlen[wid] == 1 and self.freq[wid] > 0
        ]
        scored.sort(key=lambda wid: -self.freq[wid])
        self._hot_chars = scored[: HOT_CHARS_LIMIT]
        return self._hot_chars[:limit]

    # -------------------------------------------------------------- 用户词频

    def apply_user_counts(self, counts: dict[str, int]) -> None:
        """把用户词频写进索引（只影响排序，不影响加载速度）。"""
        if not counts:
            return
        text_to_id = self.text_to_id()
        for word, count in counts.items():
            wid = text_to_id.get(word)
            if wid is not None:
                self.user_freq[wid] = count

    def text_to_id(self) -> dict[str, int]:
        if self._text_to_id is None:
            self._text_to_id = {self.words[i]: i for i in range(len(self.words))}
        return self._text_to_id

    def add_user_words(self, entries) -> int:
        """把用户自造词并入索引。``entries`` 为 ``[(word, pinyin, freq), ...]``。"""
        added = 0
        extra_full: list[tuple[bytes, int]] = []
        extra_init: list[tuple[bytes, int]] = []
        text_to_id = self.text_to_id()
        for word, py, f in entries:
            wid = text_to_id.get(word)
            if wid is None:
                wid = len(self.words)
                text_to_id[word] = wid
                self.words = _append(self.words, word)
                self.pinyin = _append(self.pinyin, py)
                self.wlen.append(len(word))
                self.freq.append(f)
                self.user_freq.append(0)
                added += 1
            flat = py.replace(" ", "").encode("utf-8")
            if flat:
                extra_full.append((flat, wid))
                extra_init.append((b"".join(s[:1] for s in py.encode("utf-8").split()), wid))
                added += 1
        if added:
            self._merge_extra(extra_full, extra_init)
        return added

    def _merge_extra(self, extra_full, extra_init) -> None:
        full_rows = [(self._full_keys.raw(i), self._full_index[i]) for i in range(len(self._full_index))]
        full_rows.extend(extra_full)
        full_rows.sort()
        self._full_keys = PackedStrings.build([r[0] for r in full_rows])
        self._full_index = array("i", (r[1] for r in full_rows))

        init_rows = [(self._init_keys.raw(i), self._init_index[i]) for i in range(len(self._init_index))]
        init_rows.extend(extra_init)
        init_rows.sort()
        self._init_keys = PackedStrings.build([r[0] for r in init_rows])
        self._init_index = array("i", (r[1] for r in init_rows))

    # ------------------------------------------------------------------ 查询

    @staticmethod
    def _lower_bound(keys: PackedStrings, prefix: bytes, count: int) -> int:
        lo, hi = 0, count
        while lo < hi:
            mid = (lo + hi) // 2
            if keys.raw(mid) < prefix:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def search(self, prefix_full: bytes, prefix_init: bytes = b""):
        """前缀查询。

        返回 ``(word_id, exact_full, exact_init)`` 迭代器。用原始字节比较
        判断「完全相等」，这样在解码词条文本之前就能算出大部分打分依据。
        """
        out: dict[int, list[bool]] = {}

        if prefix_full:
            keys, index = self._full_keys, self._full_index
            n = len(index)
            lo = self._lower_bound(keys, prefix_full, n)
            hi = self._lower_bound(keys, prefix_full + b"\xff", n)
            for i in range(lo, hi):
                wid = index[i]
                flags = out.get(wid)
                if flags is None:
                    out[wid] = [keys.raw(i) == prefix_full, False]
                elif keys.raw(i) == prefix_full:
                    flags[0] = True

        if prefix_init:
            keys, index = self._init_keys, self._init_index
            n = len(index)
            lo = self._lower_bound(keys, prefix_init, n)
            hi = self._lower_bound(keys, prefix_init + b"\xff", n)
            for i in range(lo, hi):
                wid = index[i]
                flags = out.get(wid)
                if flags is None:
                    out[wid] = [False, keys.raw(i) == prefix_init]
                elif keys.raw(i) == prefix_init:
                    flags[1] = True

        return [(wid, flags[0], flags[1]) for wid, flags in out.items()]

    def search_text(self, prefix: str, limit: int = 64) -> list[int]:
        """按词文本前缀查词（联想输入用），按基础词频降序返回。"""
        raw = prefix.encode("utf-8")
        keys, index = self._text_keys, self._text_index
        n = len(index)
        lo = self._lower_bound(keys, raw, n)
        hi = self._lower_bound(keys, raw + b"\xff", n)
        if lo >= hi:
            return []
        freq = self.freq
        ids = [index[i] for i in range(lo, min(hi, lo + limit * 4))]
        ids.sort(key=lambda wid: -freq[wid])
        return ids[:limit]

    def __len__(self) -> int:
        return len(self.words)


def _frombytes(data: bytes, typecode: str) -> array:
    arr = array(typecode)
    arr.frombytes(data)
    return arr


def _append(packed: PackedStrings, item: str) -> PackedStrings:
    """在末尾追加一项，返回新的 PackedStrings。"""
    tail = PackedStrings.build([item])
    base = packed.off[-1]
    new_off = array("I", packed.off[:-1])
    new_off.append(base)                       # tail.off[0] = 0
    new_off.append(base + tail.off[1])
    return PackedStrings(packed.blob + tail.blob, new_off)


def _initials_of(spaced_pinyin: str) -> str:
    """带空格的音节串 -> 首字母串。"""
    return "".join(part[0] for part in spaced_pinyin.split() if part)