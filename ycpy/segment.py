"""多段拼接：把一长串拼音切成若干词，拼成一句候选。

用户报的场景是敲 ``dajiayiqiganhuo`` 想得到「大家一起干活」。

为什么需要这个
--------------
词库是**按词**存的，「大家一起干活」作为一整串从来不在词库里——jieba
的 ``dict.txt`` 里只有「大家」「一起」「干活」，没有它们的拼接。于是整串
输入在前缀索引里查 0 条，直接落进「通用高频词兜底」，列出来的是
「一个」「中国」「我们」，和用户敲的东西毫无关系。

**按音节切、再按词查**就能解决，但这本身是有歧义的：``dajiayiqiganhuo``
能切 32 种方式，「大家+一起+干活」只是其中一种。所以要用打分模型挑。

打分模型
--------
一个候选 = 一串词（一条「切分路径」）。路径得分是各词得分之和::

    score(词) = log(词频) - log(音节数)

``log(词频)`` 是这个词在整个语料里的**绝对**出现概率——「大家」比「到」
少见得多，但「到」只有一个音节，而「大家」有两个。减去音节数就是按
「每音节」归一化，让长词不被单字的高绝对频次压垮。

这个公式是关键。踩过的坑是「每音节频次 = 词频 / 字数」：那样算出来
「到」是 20 万/音节、「大家」只有 0.95 万/音节，于是永远选出
「到+将+也+其+干+或」这种全单字串。用户要的是词，不是把每个音节都
猜成最常见的单字。

用 log 概率而不是直接相加，是因为各词之间存在竞争：整串输入的音节总数
是固定的（6 个），哪一段多分、哪一段少分要在全局比较，只有取 log 之后
相加才等价于比较联合概率。

歧义与去重
----------
``yiqi`` 既能是「一起」也能是「一切」，光看频次会只留一个。所以每个切分
位置都取**若干个**词候选，再由 DP 组合出 N 条最优路径，保证「一起」和
「一切」都能出现在候选列表里让用户挑。
"""

from __future__ import annotations

import heapq
import math

from .syllables import SYLLABLES, split_syllables

#: 一段里最多取多少个候选词。太少会漏掉「一起/一切」这种歧义，
#: 太多会让 DP 的分支变宽。
PER_SEGMENT_WORDS = 3

#: DP 在每个位置保留多少条方案。被砍掉的长尾不影响前三名，
#: 但让复杂度从指数级降到 O(音节数 × 段长 × 本值)。
KEEP_PATHS_PER_POS = 6

#: 最终返回多少条候选。
BEST_PATHS = 8

#: 单段最长多少个音节。够覆盖「中国人民政治协商会议」这类长词就行，
#: 再长几乎都是噪声。
MAX_SEGMENT_SYLLABLES = 8

#: 整句最多多少个音节。超过这个数要么是敲错了，要么压根不该按整句拼，
#: 交给普通候选逻辑更合适。
MAX_TOTAL_SYLLABLES = 12


class Segmented:
    """一条切分路径的结果。"""

    __slots__ = ("text", "pinyin", "score", "word_ids")

    def __init__(self, text: str, pinyin: str, score: float, word_ids: tuple):
        self.text = text
        self.pinyin = pinyin
        self.score = score
        #: 组成这句话的各个词在词库中的 id，供调用方加用户词频 / 联动词
        self.word_ids = word_ids


class Segmenter:
    """按词切分长串拼音，给出打分排序的整句候选。"""

    def __init__(self, dictionary):
        self.dictionary = dictionary
        self._log_total = None

    # ------------------------------------------------------------ 基础数据

    def _log_total_freq(self) -> float:
        """语料总频次的 log，用来做归一化。"""
        if self._log_total is None:
            total = 0
            for value in self.dictionary.freq:
                total += value
            # 词库总量很大，取 log 前先确保不溢出成 0
            self._log_total = math.log(total if total > 0 else 1)
        return self._log_total

    def _words_at(self, segment: str, limit: int) -> list[tuple]:
        """某段拼音下得分最高的若干个词，返回 ``(得分, wid, text, pinyin)``。

        ``segment`` 越长命中越多——``dajia`` 能命中 84 个词，
        ``dajiayiqiganhuo`` 的后半段能命中上万。全量打分再排序是最慢
        的一环（实测占 97% 时间）。这里用堆只留前 ``limit`` 个，
        把 O(命中数 × log 命中数) 降到 O(命中数 × log limit)。
        """
        raw = segment.encode("ascii")
        hits = self.dictionary.search(raw, raw)
        if not hits:
            return []
        n_syll = len(split_syllables(segment)) or 1
        log_total = self._log_total_freq()
        penalty = log_total + math.log(n_syll)
        freq = self.dictionary.freq

        # 同一个词可能有多条索引命中，按 wid 去重只保留最高分。
        # 命中率大的段去重后往往只剩少数几个词，所以先收窄再上堆。
        best: dict[int, float] = {}
        for wid, _full, _init in hits:
            f = freq[wid]
            if f <= 0:
                continue
            score = math.log(f) - penalty
            if score > best.get(wid, -1e18):
                best[wid] = score

        if len(best) <= limit:
            ranked = [(s, w) for w, s in best.items()]
        else:
            # nlargest 按 ``best`` 的键值对，返回的是 ``(wid, score)``，
            # 要转回统一的 ``(score, wid)`` 顺序
            ranked = [(s, w) for w, s in
                      heapq.nlargest(limit, best.items(), key=lambda kv: kv[1])]
        ranked.sort(reverse=True)
        words = self.dictionary.words
        pinyin = self.dictionary.pinyin
        return [(s, w, words[w], pinyin[w]) for s, w in ranked]

    # ---------------------------------------------------------------- 切分

    def _edges(self, syllables_list: list[str]) -> list:
        """从每个音节位置出发能接上的词。

        ``edges[i]`` 是 ``(结束位置, 得分, 词id, 词, 拼音)`` 的列表。
        """
        n = len(syllables_list)
        pinyin = self.dictionary.pinyin
        edges: list[list[tuple]] = [[] for _ in range(n)]
        for i in range(n):
            span = min(MAX_SEGMENT_SYLLABLES, n - i)
            for length in range(1, span + 1):
                segment = "".join(syllables_list[i:i + length])
                for score, wid, text, _py in self._words_at(
                        segment, PER_SEGMENT_WORDS):
                    edges[i].append((i + length, score, wid, text, pinyin[wid]))
        return edges

    def _segment_options(self, syllables_list: list[str]):
        """找出所有能把 ``syllables_list`` 切干净的方案，按得分降序。

        用**k-best 动态规划**，不是穷举。

        穷举的代价：每段最多 ``PER_SEGMENT_WORDS``(=3) 个候选词，
        6 个音节就是 3^5=243 条路径，12 个音节是 3^11≈18 万条。
        实测 ``dajiayiqiganhuo`` 要 **4 秒**——每敲一个键都等 4 秒
        是不能接受的。

        DP 的做法：从后往前算「位置 i 往后的最佳 K 条方案」，
        每个位置只保留 K 条，长尾被砍掉但不影响前三名。
        复杂度 O(音节数 × 段长 × K)，毫秒级。

        保留 K>1 条是必要的：``yiqi`` 该拼「一起」还是「一切」，
        两者语料频次几乎相同，只取最优路径就永远二选一没法让用户挑。

        返回 ``[(得分, 词序列, 词id序列), ...]``。
        """
        n = len(syllables_list)
        edges = self._edges(syllables_list)

        # best[i] = 从位置 i 到结尾的最佳 K 条方案
        best: list[list[tuple]] = [[] for _ in range(n + 1)]
        best[n] = [(0.0, (), ())]          # (得分, 词序列, 词id序列)

        for i in range(n - 1, -1, -1):
            found: list[tuple] = []
            for end, score, wid, text, py in edges[i]:
                for sub_score, sub_words, sub_ids in best[end]:
                    found.append((
                        score + sub_score,
                        ((text, py, score),) + sub_words,
                        (wid,) + sub_ids,
                    ))
            found.sort(key=lambda item: -item[0])
            seen: set[str] = set()
            deduped: list[tuple] = []
            for item in found:
                text = "".join(w for w, _py, _s in item[1])
                if text in seen:
                    continue
                seen.add(text)
                deduped.append(item)
                if len(deduped) >= KEEP_PATHS_PER_POS:
                    break
            best[i] = deduped
        return best[0]

    # ---------------------------------------------------------------- 对外

    def segment(self, raw_input: str) -> list[Segmented]:
        """把一长串拼音切成词句，返回排序后的候选。"""
        parts = split_syllables(raw_input)
        if parts is None:
            return []
        # 太短的输入（1~2 个音节）直接就是单个词，不该走拼接，
        # 否则会和普通候选打架。
        if len(parts) < 3:
            return []
        if len(parts) > MAX_TOTAL_SYLLABLES:
            return []

        out: list[Segmented] = []
        for total, words, ids in self._segment_options(parts)[:BEST_PATHS]:
            text = "".join(w for w, _py, _s in words)
            pinyin = " ".join(py for _w, py, _s in words)
            out.append(Segmented(text, pinyin, total, ids))
        return out


__all__ = ["Segmenter", "Segmented", "SYLLABLES"]