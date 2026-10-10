"""专业领域词库：按需加载的独立索引。

背景
----
用户的需求：「在首选项里面让用户勾选专业词典，勾选后输入就会在候选框里
显示该领域的专业词汇」。

主词典（jieba 语料）系统性不覆盖专业词汇，实测缺失率：

    法律 91.4%   饮食 88.9%   IT 73.0%   医学 70.3%   财经 43.8%

为什么要做成**独立索引**而不是并进主词典
----------------------------------------
两个实测数据决定了这一点：

1. ``Dictionary.add_user_words(9895 条)`` 要 **36,848 ms**（它每次都
   ``_merge_extra`` 重建全部索引），而且 ``Dictionary`` **没有 remove /
   drop / reload 方法**——一旦灌进去就撤不回来。用户「勾了又取消」就没法处理。
2. 单独建一个小索引只要 **24 ms**，快 1549 倍。

而且独立索引有第二个好处：主词典的 marshal 缓存**永远命中**，不受用户
勾选变化影响。反过来说，领域文件就不该进 ``_cache_path`` 的键——
缓存键只该覆盖**参与建索引**的文件，不参与的就别加进去。

生命周期
--------
领域索引的生命周期**完全独立于** ``Dictionary``：不勾选的文件根本不读，
取消勾选就是丢弃那个索引对象。没有残留。

打分
----
命中的领域词额外加 ``W_DOMAIN``（见 ``composer``）。这是**加性**的，
不是「硬分区」——教训是分区键只在一处排序里生效，
``score_with_context`` 里第二处 ``sort(key=-score)`` 会把它整个抹掉，
而那一处的二元组加权 ``W_BIGRAM_USER=90000`` 一次就 +9 万分。加性加权
不存在这个问题。

许可
----
数据来自 THUOCL（MIT License）。MIT 是 GPL-3.0 明确允许的「更容易兼容」
许可之一（GPLv3 附录 C 列举），所以与 ycpy 主体的 GPL-3.0-or-later 并存
不冲突。详见 ``data/CREDITS.md`` 第 6 节。
"""

from __future__ import annotations

import os
from array import array

from .dictionary import PackedStrings, ROOT

#: 领域标识 -> (文件名, 中文名)。顺序即首选项里的展示顺序。
#:
#: **刻意只开放这 6 个**，排除的 5 个及理由见
#: ``tools/build_domain_dict.py`` 的模块文档。
DOMAINS: dict[str, tuple[str, str]] = {
    "it": ("ycpy-domain-it.txt", "IT"),
    "caijing": ("ycpy-domain-caijing.txt", "财经"),
    "law": ("ycpy-domain-law.txt", "法律"),
    "medical": ("ycpy-domain-medical.txt", "医学"),
    "food": ("ycpy-domain-food.txt", "饮食"),
    "car": ("ycpy-domain-car.txt", "汽车"),
}

DOMAIN_DIR = os.path.join(ROOT, "data")


def domain_path(key: str) -> str:
    return os.path.join(DOMAIN_DIR, DOMAINS[key][0])


def available_domains() -> list[str]:
    """本机实际存在的领域（数据文件没打包时不勾选，免得引擎报错）。"""
    out = []
    for key in DOMAINS:
        if os.path.exists(domain_path(key)):
            out.append(key)
    return out


class DomainIndex:
    """单个领域的词索引。

    结构与 ``Dictionary`` 的前缀索引同构（有序键 + id 数组 + 二分），
    但维护**两套键**：全拼（``shujuku``）和首字母（``sjk``）。

    为什么要首字母：产品要求是「专业词汇应该是用户输入**完整的拼音**
    **或者拼音的韵母缩写**，才能放到前面」。原先只建全拼键，于是敲 `sj`
    想打「数据库」时领域词完全不出现在候选里——而这恰恰是用户主动缩写
    之后的**明确意图**，比「只敲了 `bianhua` 就被塞一堆『变换…』」要强得多。
    """

    __slots__ = ("key", "label", "words", "pinyin", "freq",
                 "_keys", "_index", "_init_keys", "_init_index")

    def __init__(self, key: str, path: str):
        words: list[str] = []
        pinyin: list[str] = []
        freq = array("q")
        rows: list[tuple[bytes, int]] = []
        init_rows: list[tuple[bytes, int]] = []
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
                rows.append((py_b.replace(b" ", b""), len(words)))
                init_rows.append((b"".join(s[:1] for s in py_b.split()), len(words)))
                words.append(word_b.decode("utf-8"))
                pinyin.append(py_b.decode("utf-8"))
                freq.append(int(freq_b))
        rows.sort()
        init_rows.sort()
        self.key = key
        self.label = DOMAINS[key][1]
        self.words = PackedStrings.build(words)
        self.pinyin = PackedStrings.build(pinyin)
        self.freq = freq
        self._keys = PackedStrings.build([r[0] for r in rows])
        self._index = array("i", [r[1] for r in rows])
        self._init_keys = PackedStrings.build([r[0] for r in init_rows])
        self._init_index = array("i", [r[1] for r in init_rows])

    def __len__(self) -> int:
        return len(self.freq)

    def search(self, prefix_full: bytes, prefix_init: bytes = b""):
        """前缀查询，返回 ``(词内序号, 命中类型)`` 迭代器。

        命中类型是一个字符串，用来决定领域加权给多少（见
        ``composer._domain_bonus``）：

        ``"full"``  全拼**完全等于**输入（``bianhua`` -> ``bian hua``）
        ``"init"``  首字母**完全等于**输入（``sjk`` -> ``shu ju ku``）
        ``"pre"``   只是前缀（``bianhua`` -> ``bian hua n...``）

        ``full``/``init`` 才是「用户把拼音（或韵母缩写）完整打出来了」，
        配得上领域优先；``pre`` 不配——用户只敲了 ``bianhua``，凭什么
        替他决定要「变换规则」而不是「变化」。
        """
        seen: set[int] = set()
        if prefix_full:
            n = len(self._index)
            lo = _lower_bound(self._keys, prefix_full, n)
            hi = _lower_bound(self._keys, prefix_full + b"\xff", n)
            for i in range(lo, hi):
                wid = self._index[i]
                seen.add(wid)
                kind = "full" if self._keys.raw(i) == prefix_full else "pre"
                yield wid, kind
        if prefix_init:
            n = len(self._init_index)
            lo = _lower_bound(self._init_keys, prefix_init, n)
            hi = _lower_bound(self._init_keys, prefix_init + b"\xff", n)
            for i in range(lo, hi):
                wid = self._init_index[i]
                if wid in seen:
                    continue          # 同一个词，全拼已经报过了
                seen.add(wid)
                kind = "init" if self._init_keys.raw(i) == prefix_init else "pre"
                yield wid, kind

    def match_ids(self, prefix_full: bytes,
                  prefix_init: bytes = b"") -> dict[str, int]:
        """返回 ``{词: 词内序号}``，只含**完全命中**的（全拼或首字母）。"""
        out: dict[str, int] = {}
        for i, kind in self.search(prefix_full, prefix_init):
            if kind in ("full", "init"):
                out[self.words[i]] = i
        return out


def _lower_bound(keys: PackedStrings, prefix: bytes, count: int) -> int:
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        if keys.raw(mid) < prefix:
            lo = mid + 1
        else:
            hi = mid
    return lo


class DomainSet:
    """用户勾选的若干领域。

    ``load``/``unload`` 让勾选变化可以即时生效——这是独立索引架构的
    直接好处：不需要重建主词典，也不需要动 marshal 缓存。
    """

    __slots__ = ("_loaded",)

    def __init__(self) -> None:
        self._loaded: dict[str, DomainIndex] = {}

    def active(self) -> bool:
        return bool(self._loaded)

    def keys(self) -> list[str]:
        return list(self._loaded)

    def all(self) -> list[DomainIndex]:
        return list(self._loaded.values())

    def stats(self) -> str:
        if not self._loaded:
            return "未启用专业词库"
        total = sum(len(d) for d in self._loaded.values())
        labels = "、".join(d.label for d in self._loaded.values())
        return "%s（%d 词条）" % (labels, total)

    def load(self, keys) -> None:
        """载入指定的领域。已在的跳过，缺失的静默忽略。

        缺失的文件要静默忽略而不是报错——打包时可能没带某个领域的数据
        （或用户手工删了），这时该领域当没勾选即可，不该让整个引擎起不来。
        """
        wanted = set(keys) & set(available_domains())
        # 先丢弃不再需要的，释放内存
        for gone in set(self._loaded) - wanted:
            del self._loaded[gone]
        for key in wanted:
            if key in self._loaded:
                continue
            path = domain_path(key)
            if not os.path.exists(path):
                continue
            self._loaded[key] = DomainIndex(key, path)

    def lookup(self, prefix_full: bytes, prefix_init: bytes = b"",
                 exact_only: bool = False) -> list[tuple[str, int, str]]:
        """在所有已载入的领域里查前缀。

        返回 ``[(词, 领域内词频, 命中类型), ...]``，命中类型见
        ``DomainIndex.search``（``"full"`` / ``"init"`` / ``"pre"``）。

        同一个词出现在多个领域时取**词频最高**的那份——领域间重叠很少
        （最大 119 条），但取 max 至少保证不会因为重叠而丢词。命中类型取
        **最强**的那一个：全拼完全命中比前缀命中更配得上领域优先。
        """
        rank = {"full": 2, "init": 1, "pre": 0}
        merged: dict[str, tuple[int, str]] = {}
        for dom in self._loaded.values():
            for i, kind in dom.search(prefix_full, prefix_init):
                if exact_only and kind == "pre":
                    continue
                text = dom.words[i]
                prev = merged.get(text)
                if prev is None or (dom.freq[i], rank[kind]) > (prev[0], rank[prev[1]]):
                    merged[text] = (dom.freq[i], kind)
        return [(text, freq, kind)
                for text, (freq, kind) in merged.items()]


__all__ = ["DOMAINS", "DOMAIN_DIR", "DomainIndex", "DomainSet",
           "available_domains", "domain_path"]