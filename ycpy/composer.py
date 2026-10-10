"""组字引擎：把拼音串变成有序候选词。

匹配同时覆盖两种输入风格：

* **全拼** —— ``chengxuyuanji`` 命中「程序员」；
* **首字母** —— ``cxy`` / ``cxyj`` 同样命中「程序员」。

做法是对 ``全拼串`` 和 ``首字母串`` 两个有序索引各做一次前缀二分，
再按「基础词频 + 用户词频 + 上下文联想 + 精确命中」打分排序。
"""

from __future__ import annotations

import os

from . import syllables
from .domain import DomainSet
from .segment import Segmenter

# 打分权重。用户数据最终一定要压过语料词频，否则「记忆用户高频词」无从体现。
W_USER = 60_000
W_BIGRAM_USER = 90_000
W_BIGRAM_STATIC = 3_000
W_EXACT_FULL = 25_000
W_EXACT_INIT = 25_000
W_SYL_COUNT = 2_500
W_LEN_SHORT = 1_200
W_LEN_LONG = 400
#: 靠猜出来的变体命中的词，一律排在精确匹配之后
W_FUZZY = -8_000

#: 输入里的元音和候选拼音对得上时加分。用户报的
#: 「输入 zhguo 应当把『中国』或其他韵母一致的词排在前面」。
#:
#: 首字母匹配（`zg`）只比声母，`这个`（zhe ge）和 `中国`（zhong guo）
#: 在它眼里长得一模一样，全靠语料词频决胜负——`这个` 词频 6 万，
#: 就压到了本该第一的 `中国` 前面。但用户敲 `zhguo` 时把 `guo` 一个音节
#: 完整打了出来，`guo` 是明确的意图信号，不该被丢掉。
W_VOWEL_MATCH = 6_000

#: 单字的「常用度」加分系数。
#:
#: ``CharRarity`` 给的是 0~1000 的整数分（T1=1000 … T4=250，扩展 B=100）。
#: 乘 1000 后量级到 10 万，正好**压过** jieba 单字 freq 的典型差距
#: （框 299 vs 㑌 400），但**远小于**多字词的词频量级（40 万），
#: 所以不会把词从多字候选里挤掉。
W_CHAR_COMMON = 1_000

#: 整句拼接时，路径得分（负的 log 概率）换算成候选分的系数。
#:
#: 取 1 是「保序不放大」：log 概率本身已经是可比的 nat 值，
#: ``-path.score`` 只是把它翻正，量级保持原样，用户信号才能在
#: **接近**的切法之间起作用。
#:
#: 踩过的坑：一度放大到 40000（3.3 nat 变成 13 万分），本想让语料压住
#: 用户偏好，结果**方向反了**——「大家|好」明明比「到|将|好」合理，
#: 却因为「到」「将」「好」各被选过几次而被压下去。差距越大，
#: 放大后越荒谬。放大只适合表达「大小关系」，不适合当偏置。
W_SEGMENT_BASE = 1

#: 整句拼接时，每落后一名额外扣的分。
#:
#: 有了 BASE 保留 nat 差距，量级已经够小；但两条路径的 nat 差距可能
#: 极小（「一起」-30.22 vs「一切」-30.19，只差 0.03），光靠 nat 完全
#: 分不出先后。这一项给「名次」兜底，保证分段器的判断不会被用户信号
#: 整体掀翻，同时留出空间让用户在近似的情况下改写顺序。
W_SEGMENT_RANK_STEP = 30

#: 整句拼接时，用户词频的打折系数（整句和组成词都用这个）。
#:
#: 用户选过「将」「好」不代表他想打「到将好」。不打折的话单字偏好会
#: 累加成整句的分数，越权压过语料的切分判断。打折后它只在切法本身接近
#: 时起作用——也就是打破「一起/一切」这种词频几乎相同的歧义。
W_SEGMENT_USER_SHRINK = 0.15

#: 精确命中少于这么多条时，才额外去试变体。
#: `nha` 精确只命中「南海岸」1 条，明显不是用户想要的，此时值得再猜；
#: `cxy` 命中 104 条，说明敲得很准，再猜只是浪费。
FUZZY_MIN_EXACT = 12

#: 精确命中达到这么多条，就不再做「音节补全」这种深层纠错，只给浅层变体。
#:
#: 分档的原因见 ``_add_fuzzy_hits`` 的文档：`chenxuyuan` 精确命中 1 条
#:（「陈戌源」这个冷僻人名），若一律用 ``exact_ok=True`` 就拿不到
#: `chengxuyuan`，用户真正想打的「程序员」会从候选里消失。
#:
#: 取 3：1~2 条精确命中几乎都可以断定用户敲错了（多音节输入只命中一两个
#: 词的情况很常见）；到 3 条以上就认为输入基本准确，此时深层补全只会
#: 引入噪声——`cxy` 命中 104 条，任何补全都是浪费。
FUZZY_DEEP_MIN_EXACT = 3

#: 用户勾选了专业领域词库时，领域词的额外加分。
#:
#: **必须是加性的，不能用「硬分区」把领域词排到前面。** README 3.22 节
#: 记着一个教训：分区键只在一处排序里生效，而 ``score_with_context``
#: （engine 每敲一个键都会调）里还有第二处 ``sort(key=-score)``，会把分区
#: 整个抹掉——而它给的二元组加权 ``W_BIGRAM_USER=90000`` 一次就 +9 万分，
#: 比字频分的总波动（几千）大一个数量级，于是顺序整个翻转。
#:
#: 取 150000 的依据（实测多字候选的分数带 1016~156970）：
#:
#:     中国   freq=129470  总分 156970
#:     中国共产党 freq=6832    总分   7232
#:     中国人民解放军 freq=1328    总分   1728
#:
#: 15万略高于「中国」的最高分，保证勾选后 `zhongguo` 时
#: 「中国银监会」能压过「中国人民解放军」。实测 6 个领域词都胜出。
#:
#: **同时保证「用户词频 > 领域默认值」**（这是明确的产品要求）：领域默认
#: 分约 150050，而主词典词 ``freq + W_USER*n``，学 3 次就压过去：
#:
#:     1328 + 60000*3 = 181328 > 150050
#:
#: 也就是说 W_DOMAIN 是「勾选后的默认优先级」，不是不可逾越的墙。
#:
#: 这个值**待实测调整**——需要真实用户勾选后试出来的手感。
W_DOMAIN = 150_000

#: 领域词只在「用户把它的拼音**完整打出来**」时才享受全部 W_DOMAIN。
#:
#: 回归 bug（用户报「输入 bianhua，没有先出现『变化』而是出现『变换规则』，
#: 专业词汇都在前面了」）：原先 W_DOMAIN 是个**不看匹配质量**的固定加分，
#: 于是「变换规则」(bian huan gui ze) 只因为 `bianhua` 是它的**前缀**，
#: 就和「变化」(bian hua) 这个**完全命中**拿到同��份领域加权——再叠加它
#: 作为长词的 W_LEN_LONG，分数反而更高：
#:
#:     变化无穷(精确)   150036
#:     变换规则(前缀)   150011  <- 本该在「变化」之后
#:
#: 产品要求很明确：「专业词汇应该是用户输入**完整的拼音**或者拼音的韵母
#: 缩写，才能放到前面」。也就是说**前缀命中不该享受领域优先**——
#: 用户只敲了 `bianhua`，凭什么替他决定要的是「变换规则」而不是「变化」？
#:
#: 所以领域加权按匹配质量分档：
#:
#:     完全命中（`bianhua` -> `bian hua`）    W_DOMAIN * 1.00
#:     前缀命中（`bianhua` -> `bian hua n...`）W_DOMAIN * DOMAIN_PREFIX_RATIO
#:
#: 取 0.12 是为了让前缀命中的领域词**彻底让位**给完全命中的普通词：
#: 150000*0.12 = 18000，加上它自己的词频（约 2000）和长词分（400）也就
#: 2 万出头，而「变化」的精确命中分是 25000(freq) + 25000(EXACT) ≈ 5 万。
W_DOMAIN_PREFIX_RATIO = 0.12

MAX_CANDIDATES = 9

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_HERE), "data")
NEXT_PATH = os.path.join(_DATA_DIR, "ycpy-next.txt")
CHAR_NEXT_PATH = os.path.join(_DATA_DIR, "ycpy-char-next.txt")
#: 单字字频表：Jun Da《现代汉语字符频次表》的语料统计名次（MIT）。
#: 这是单字排序的**主信号**——的=1、一=2、是=3、这=11…
#: 缺这个文件不会崩，只是单字排序退回「只看词频」。
CHAR_FREQ_PATH = os.path.join(_DATA_DIR, "ycpy-char-freq.txt")
#: 《通用规范汉字表》分级（Unicode License V3），只作为字频的辅助微调。
CHAR_SCORE_PATH = os.path.join(_DATA_DIR, "Unihan_IRGSources.txt")
#: 繁体字表（Unicode License V3，来自 Unihan_Variants.txt）。
#: 命中的一律排在所有简体候选之后——用户要求「繁体字的候选字都放到简体字后面」。
TRAD_PATH = os.path.join(_DATA_DIR, "ycpy-trad.txt")

#: 字体覆盖表的缓存（由 ``fc-list :lang=zh`` 生成）。
FONT_COV_CACHE = os.path.join(
    os.environ.get("XDG_CACHE_HOME",
                   os.path.expanduser("~/.cache")),
    "py-pinyin-ibus", "font-cov.txt")


#: 拼音里算「元音」的字母。`v` 是 ü 的替身写法（`lv` = 女），算韵母。
_VOWELS = frozenset("aoeiuv")


def _vowels_in(spaced_pinyin: str) -> frozenset:
    """候选拼音里的元音字母集合。"""
    return frozenset(c for c in spaced_pinyin if c in _VOWELS)


def _domain_bonus(kind: str) -> float:
    """领域加权按匹配质量分档：``full``/``init`` 给满，``pre`` 只给一小部分。

    见 ``W_DOMAIN_PREFIX_RATIO`` 的说明——这个分档就是「输入 bianhua
    应该是『变化』而不是『变换规则』」那个 bug 的修法。

    ``kind`` 是 ``DomainIndex.search`` 给的命中类型：

    ``"full"``  全拼完整（``bianhua`` -> ``bian hua``）
    ``"init"``  韵母缩写完整（``sjk`` -> ``shu ju ku``）——产品要求里
                「完整的拼音**或者拼音的韵母缩写**」两者都算
    ``"pre"``   只是前缀，不配领域优先
    """
    if kind in ("full", "init"):
        return float(W_DOMAIN)
    return W_DOMAIN * W_DOMAIN_PREFIX_RATIO


class Candidate:
    __slots__ = ("text", "pinyin", "score")

    def __init__(self, text: str, pinyin: str, score: float):
        self.text = text
        self.pinyin = pinyin
        self.score = score

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "Candidate(%r, %r, %.0f)" % (self.text, self.pinyin, self.score)


class Composer:
    def __init__(self, dictionary: Dictionary, user: UserModel, next_path: str = NEXT_PATH,
                 char_next_path: str = CHAR_NEXT_PATH):
        self.dictionary = dictionary
        self.user = user
        #: 用户词频的权重，来自首选项；设成 0 相当于关掉用户偏好
        self.user_weight = W_USER
        #: 「学习我的用词」开关。关掉时单词频会被清空（见 engine 的
        #: ``apply_user_counts(user.words if config.enable_learning else {})``），
        #: 但**二元组是另一条路**，引擎那边管不到，所以这里自己记一份，
        #: 供 ``score_with_context`` 判断。
        self._learning_enabled = True
        #: 精确命中少于这个数时，才额外去试「猜出来的变体」。
        #: 设成 0 表示永远只走精确匹配，等于关掉容错。
        self.fuzzy_min_exact = FUZZY_MIN_EXACT
        #: 用户勾选的专业领域词库。未勾选时 ``active()`` 为 False，
        #: 打分阶段会完全跳过领域查询，零开销。
        self.domains = DomainSet()
        #: 主词典的词文本集合，用来给领域词去重：同一个词如果主词典已经
        #: 有了，它已经走完主词典那条打分链（含用户词频），不能再加一次
        #: ``W_DOMAIN``，否则会凭空多出 15 万分。
        #: 懒加载——51 万个字符串放进 set 大约 20 MB，只有勾选了领域时才值得。
        self._main_words: set[str] | None = None
        self.next_table: dict[str, list[tuple[str, int]]] = {}
        self.char_next_table: dict[str, list[tuple[str, int]]] = {}
        #: 长串拼接用的切分器，懒加载（构造它要扫一遍词频）
        self._segmenter = None
        #: 单字常用度表。缺文件就退化成「只看词频」，功能不受影响。
        self._char_score = None
        self._load_char_score()
        #: 系统字体覆盖表：过滤掉渲染成豆腐块的生僻字
        self._font_cov = None
        self._load_font_cov()
        self._load_next(next_path)
        self._load_char_next(char_next_path)
        self._load_static_bigrams()

    def _load_char_score(self) -> None:
        """载入单字常用度表。文件缺失就静默跳过。

        没有它不会崩，只是单字排序退回「纯看 jieba 词频」——也就是用户
        报的那个问题（常用字被生僻字压下去）。所以这是个可选增强。

        优先用**语料字频**（``ycpy-char-freq.txt``）；它不在时才退回
        Unihan 分级。《通用规范汉字表》的分级衡量的是字形规范化程度、
        不是使用频率，把「将/会/对/这/个」划到 T3 而「者/正/遮/框」划到
        T1——只靠它，用户报的那些高频字会排到很后面。详见 charscore.py。
        """
        from .charscore import CharRarity
        try:
            if os.path.exists(CHAR_FREQ_PATH):
                self._char_score = CharRarity.from_rank_file(
                    CHAR_FREQ_PATH,
                    CHAR_SCORE_PATH if os.path.exists(CHAR_SCORE_PATH) else None,
                )
                # 繁体字表存在时挂上，繁体一律排到简体候选后面。
                if os.path.exists(TRAD_PATH):
                    trad = CharRarity.from_trad_file(TRAD_PATH)
                    if trad:
                        self._char_score.attach_traditional(trad)
            elif os.path.exists(CHAR_SCORE_PATH):
                self._char_score = CharRarity.from_zhiku(CHAR_SCORE_PATH)
                if os.path.exists(TRAD_PATH):
                    trad = CharRarity.from_trad_file(TRAD_PATH)
                    if trad:
                        self._char_score.attach_traditional(trad)
        except OSError:
            self._char_score = None

    def _load_font_cov(self) -> None:
        """载入系统字体覆盖表，用来剔除渲染成豆腐块的字。

        用户报「输入 kuang 翻页到后面出现很多黑色色块」——那是 CJK 扩展 B
        及以后的字符（本例 104 个候选里 32 个），Noto Sans CJK 只覆盖到
        U+9FFF 一带，所以客户端渲染成一个个方框。

        问 fontconfig 要真实的覆盖区间最准；拿不到就退化成「扩展区一律
        排除」。任何情况下都不会崩。
        """
        from .fontcov import FontCoverage
        try:
            self._font_cov = FontCoverage.load(FONT_COV_CACHE)
        except Exception:                # 覆盖表只是锦上添花，绝不能拖垮输入法
            self._font_cov = None

    @staticmethod
    def _renderable(font_cov, wid: int, words) -> bool:
        """这个词条有没有字形。取第一个字判断——多字词里只要有一个字
        渲染不出来，整个词在候选窗里就是豆腐块。"""
        if font_cov is None:
            return True
        try:
            return font_cov.covers(words[wid][0])
        except (IndexError, TypeError):
            return True

    def _load_static_bigrams(self) -> None:
        """加载语料词间共现表，供整句拼接排序时打破歧义。

        ``ycpy-next.txt`` 存的是「词|后继词」的转移对，正好可以当二元组用：
        「大家|一起」出现过多少次。没有它的话 ``yiqi`` 只能靠词频在
        「一起」和「一切」之间二选一，而两者频次几乎一样。
        """
        self._static_bigrams = self.next_table

    # ------------------------------------------------------------ 联想词表

    @staticmethod
    def _load_pairs(path: str, cap: int) -> dict[str, list[tuple[str, int]]]:
        table: dict[str, list[tuple[str, int]]] = {}
        try:
            with open(path, "r", encoding="utf-8") as fp:
                for line in fp:
                    if not line or line[0] == "#":
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    key, value, score = parts[0], parts[1], int(parts[2])
                    table.setdefault(key, []).append((value, score))
        except OSError:
            return {}
        for items in table.values():
            items.sort(key=lambda kv: -kv[1])
            del items[cap:]
        return table

    def _load_next(self, path: str) -> None:
        self.next_table = self._load_pairs(path, 12)

    def _load_char_next(self, path: str) -> None:
        self.char_next_table = self._load_pairs(path, 16)

    # 联想的三档信号。三档的量纲完全不同（次数 / 共现权重 / 权重×词频），
    # 直接混在一起排序没有意义，所以先分档再在档内排序。
    TIER_USER = 3_000_000.0
    TIER_CORPUS = 2_000_000.0
    TIER_CHAR = 1_000_000.0
    #: 前三层全落空时的最后兜底，比字级搭配再低一档
    TIER_LAST_CHAR = 500_000.0
    #: 拼音完全打不出来时的高频词，只为让候选窗保持可见
    TIER_HOT = 100_000.0

    def next_words(self, context: str, limit: int = MAX_CANDIDATES) -> list[Candidate]:
        """给定已上屏文本，联想后续词条。"""
        if not context:
            return []
        out: list[Candidate] = []
        seen: set[str] = set()

        def push(text: str, score: float) -> None:
            if text and text not in seen:
                seen.add(text)
                out.append(Candidate(text, "", score))

        # 1) 用户自己的历史（最强信号）
        for text, count in self.user.context_words(context):
            push(text, self.TIER_USER + count)

        # 2) 语料静态搭配：上下文从长到短逐级回退
        for length in range(min(4, len(context)), 0, -1):
            for text, weight in self.next_table.get(context[-length:], ()):
                push(text, self.TIER_CORPUS + weight * length)

        # 3) 字级搭配统计兜底：上文末字常接的字 -> 以该字开头的常用双字词。
        #    单字和四字以上的人名/成语在这里纯属噪声，一律不要。
        fallback: list[tuple[str, float]] = []
        for nxt, weight in self.char_next_table.get(context[-1], ()):
            for wid in self.dictionary.search_text(nxt, limit=6):
                if 2 <= self.dictionary.wlen[wid] <= 3:
                    fallback.append(
                        (self.dictionary.words[wid],
                         float(weight) * self.dictionary.freq[wid])
                    )
        fallback.sort(key=lambda kv: -kv[1])
        for text, score in fallback:
            push(text, self.TIER_CHAR + score)

        # 4) 最后一层：上文末字缺搭配数据时，用用户词频里以该字开头的词。
        #
        #    踩过的坑：字级搭配表只收语料里最高频的 2500 个字（build_dict.py 的
        #    TOP_CHARS），生僻字不在其中。选完「陈兴瑜」再按分号，末字「瑜」
        #    查不到任何搭配，联想窗就空了——前三层全部落空时至少给点东西，
        #    不然用户看到的是一个空窗口。
        if not out and context:
            head = context[-1]
            for wid in self.dictionary.search_text(head, limit=limit * 3):
                text = self.dictionary.words[wid]
                if len(text) >= 2:
                    push(text, self.TIER_LAST_CHAR + self.dictionary.freq[wid])

        out.sort(key=lambda c: -c.score)
        return out[:limit]

    # ---------------------------------------------------------------- 组字

    def candidates(self, raw_input: str) -> list[Candidate]:
        """根据当前输入串给出排序后的候选。"""
        buf = syllables.normalize(raw_input)
        if not buf:
            return []
        expected = self._expected_syllable_count(buf)
        short_input = len(buf) <= 2

        dict_ = self.dictionary
        hits = dict_.search(buf.encode("ascii"), buf.encode("ascii"))
        single_only = self._is_single_syllable(buf)
        if single_only:
            # 只敲了**一个韵母**时只给单字候选。
            #
            # 用户的原话是「输入里出现大于一个韵母时才筛选多字候选词」。
            # 落到实现上就是「音节数 <= 1」，因为拼音里每个音节恰好一个
            # 韵母。**不要去数字母里的元音个数**——`yong` 里的 `g` 是声母
            # 的一部分（`ong` 是整体韵母），数字母会数错。
            #
            # 为什么必须收紧候选集而不是调权重：词库索引是**按词存的**，
            # `ni` 作为前缀能二分命中 `nihao`（「你好」），flags 全 False。
            # 也就是说只敲 `ni` 时「你好」本来就躺在命中列表里，靠打分是
            # 压不住的——除非用户从没选过它。收紧候选集才彻底。
            #
            # 踩过的坑分两回。最初只判断 `len(buf) == 1`，于是单字母 `n`
            # 没问题，但 `ni` 照样冒出「你好」、`yong` 照样冒出「用户」——
            # 因为一个字母和**一个韵母**是两回事，用户报的就是后者。
            hits = [h for h in hits if dict_.wlen[h[0]] == 1]
        if self._font_cov is not None:
            # 剔掉系统字体里没有字形的词条。
            #
            # 用户报「输入 kuang 翻页到后面出现很多黑色色块」——那是 CJK
            # 扩展 B 及以后的字符，104 个候选里 32 个是这种。绝大多数桌面
            # 没装这类字的字体（Noto Sans CJK 只到 U+9FFF），客户端渲染
            # 出来就是一个个豆腐块。
            #
            # 放在这里而不是打分里：黑块不是「排得靠后」，是**根本不该出现**。
            # 而且这些字常常词频还不低（建库默认分 400），靠降权压不住。
            hits = [h for h in hits
                    if self._renderable(self._font_cov, h[0], dict_.words)]

        # 专业领域词库：先算好，两条出口都要用。
        #
        # 放在这里（``if hits:`` 之前）而不是打分循环之后，是因为主词典
        # 查不到某个输入时，流程会走 _search_fuzzy / 分词 / 热点兜底这三条
        # **提前 return** 的路——见下面 ``if not hits:`` 里的说明。
        domain_scored = self._domain_scores(buf, expected, short_input,
                                            dict_.words)
        # 只有「领域索引独有的词」需要在这里返回；主词典已有的词
        # （「疾病」这类）走下面的提权路径，那条路才有完整的主词典打分。
        domain_out = [cand for _s, _t, cand in domain_scored
                      if cand is not None]

        # 标记哪些命中来自「猜出来的变体」，这些要额外扣分。
        fuzzy_ids: set[int] = set()
        if hits:
            # 精确命中已经不少时，只有「猜出来的变体能带进更多词」才值得做，
            # 否则纯属浪费。命中很多（比如 cxy 有 104 条）说明用户敲得很准。
            fuzzy_ids = self._add_fuzzy_hits(buf, hits, fuzzy_ids)
        else:
            hits, fuzzy_ids = self._search_fuzzy(buf, hits)
            if not hits:
                # 先问专业领域词库——**必须在这里、且在分词兜底之前**。
                #
                # 踩过的坑：领域词最早是在打分循环之后才并进去的，于是
                # 勾了「汽车」也打不出「新凯越」。原因是主词典查不到
                # `xinkaiyue`，于是走 _search_fuzzy，它给出「新开铺月」
                # 「新开月」这类**音节补全猜出来的垃圾**，hits 非空 →
                # `if not hits` 不成立 → 直接跳到打分循环，而那里只拿到
                # 主词典的 wid，领域词根本没进池子。
                #
                # 领域词有货就不走后面的兜底：它们是**精确**命中，比
                # 「音节补全猜出来的垃圾」可靠得多。
                if self.domains.active() and domain_out:
                    # 领域词有货就不用后面的兜底了。
                    domain_out.sort(key=lambda c: -c.score)
                    return domain_out

                # 先试「切成若干词拼成一句」（见 ``segment``）。
                #
                # 用户报的场景是敲 `dajiayiqiganhuo` 想打「大家一起干活」。
                # 「大家一起干活」作为一整串不在词库里，于是前缀查询 0 条，
                # 直接掉进下面那份和输入毫无关系的通用高频词兜底。
                seg = self._segment_candidates(buf)
                if seg:
                    return seg

                # 一个候选都匹配不上：给一份高频词兜底。
                #
                # 关键原因：GNOME 收到**空的** lookup table 根本不画窗口，
                # 于是只敲一个 `v` 之类的输入，用户看到的是「按了键但什么都没
                # 发生」，完全不知道输入法还活着。塞一份热点词进去，窗口就在了。
                #
                # 但只敲一个字母时要换成**单字**兜底：热点词榜按设计全是双字词
                # （见 `Dictionary.hot_words` 的说明），于是只敲 `i` 会列出
                # 「一个」「中国」「我们」——可 `i` 一个字母哪写得出 `yi ge`。
                # 用户想要的是敲了韵母之后看几个高频单字。
                return self._hot_candidates(single=single_only)

        freq = dict_.freq
        user_freq = dict_.user_freq
        wlen = dict_.wlen
        words = dict_.words
        pinyin = dict_.pinyin

        scored: list[tuple[float, int, Candidate]] = []
        for wid, exact_full, exact_init in hits:
            score = float(freq[wid])
            if exact_full:
                score += W_EXACT_FULL
            elif exact_init:
                score += W_EXACT_INIT
            if wlen[wid] == 1 and self._char_score is not None:
                # 单字再叠加「字的常用度」。见 ``charscore`` 的说明：
                # jieba 的 freq 是**词**频，统计的是「这个字单独成词出现了
                # 多少次」，而常用字大多只作为词的一部分出现（「框」只在
                # 框架/窗口/文本框里），于是自己的 freq 极低——被建库时
                # 的默认分 400 都能盖过去。生僻字反而拿到默认值排在前面。
                #
                # 只加在单字上：多字词的 jieba freq 本来就是真实的词频，
                # 不需要也不该再叠字级信号。
                score += W_CHAR_COMMON * self._char_score.score_for(words[wid])
            if wid in fuzzy_ids:
                # 猜出来的：无论它在变体里匹配得多准，都排在精确命中之后。
                # 但仍然保留「在变体里是完全命中」这一点带来的排序，
                # 这样 nha 的候选里 「你好」 会排在 「尼哈番」 前面。
                score += W_FUZZY

            if exact_init and not exact_full:
                # 只靠首字母命中时，用「输入里打出来的元音」再筛一遍。
                #
                # 用户敲 `zhguo`，首字母是 `zg`，于是 `这个`(zhe ge) 和
                # `中国`(zhong guo) 长得完全一样，全靠词频决胜——`这个`
                # 6 万压过了 `中国` 12 万之外该有的位置。但用户把 `guo`
                # 整个音节都打出来了，这是明确的意图，不该丢。
                #
                # 只在**首字母命中**时加分：全拼命中本来就比过了元音，
                # 再加就成了重复计分。
                vowels = self._vowels_of_input(buf)
                if vowels:
                    word_vowels = _vowels_in(pinyin[wid])
                    if vowels & word_vowels:
                        score += W_VOWEL_MATCH

            uf = user_freq[wid]
            n = wlen[wid]
            if uf:
                # 用户词频要按「输入覆盖了这个词多少」缩放，不能一刀切。
                #
                # 踩过的坑：用户高频打了 22 次「你好」，于是只敲一个 n 时
                # 「你好」 以 22×60000 的分数压过所有候选。可 n 只有 1 个字母，
                # 而「你好」 要 n-i-h-a-o 五个字母才写得完——n 顶多算它的开头，
                # 不是一次可信的指名。词越长、输入越短，就越不该给它满额权重。
                #
                # 只对用户真选过的词算：别人的词本来就不加分，而短前缀
                # （比如单个 n）能命中上万条，多跑一遍纯属浪费。
                score += self.user_weight * uf * self._coverage(buf, wid)

            if n == expected:
                score += W_SYL_COUNT
            if short_input:
                if n == 2:
                    score += W_LEN_SHORT
            elif n >= 5:
                score += W_LEN_LONG

            # **繁体单字直接从候选池里剔除**，不进排序。
            #
            # 踩过的坑（用户报「输入 hui，会在最前面，后面一堆繁体字，
            # 才看到灰」）：原来靠排序时的「硬分区」把繁体压到末尾，
            # 但那个分区**只在一处排序里生效**。`score_with_context`
            # （engine 每敲一个键都会调）里还有第二处 `sort(key=-score)`，
            # 分区被整个抹掉——而它给的二元组加权 W_BIGRAM_USER=90000，
            # 一次就 +9 万分，比字频分的总波动（几千）大一个数量级，
            # 于是「會」被从第 141 位顶到第 4 位，插到「灰」前面。
            # 复现时要先给繁体字造一条二元组（真实用户词库里就有 45 条含繁体的）。
            #
            # 所以改成**在打分阶段就不让它进池子**：任何后续排序都无从复活它。
            # 这比「排到最后」更彻底，也不再依赖每一处排序都记得带分区键——
            # 这次的 bug 正是因为有人加了一处排序却忘了带。
            #
            # 只剔单字：词典里根本没有繁体词（`中國`/`電腦` 全部不存在），
            # 而多字词里那 929 个「含繁体」的绝大多数是误伤——
            # `著名`(著)、`狼藉`(藉)、`覆盆子`(覆) 都是标准简体词，碰不得。
            if (wlen[wid] == 1 and self._char_score is not None
                    and self._char_score.is_traditional(words[wid])):
                continue
            scored.append((score, wid, None))

        # 主词典已有的领域词在这里提权（见 _boost_main_dict_words 的说明）
        self._boost_main_dict_words(scored, words, buf)
        scored.extend(domain_scored)

        scored.sort(key=lambda item: -item[0])
        # 候选池要**覆盖到全部同音字**，不能按分数截断。
        #
        # 回归 bug（用户报「输入 kuang 不显示『框』」）：`kuang` 精确命中
        # 587 条，其中拼音正好是 kuang 的单字有 87 个，按语料词频排序后
        # 「框」(freq=299) 排在第 65 位。原先只留 ``MAX_CANDIDATES * 4``
        # = 36 个，于是「框」**连候选池都进不去**——不是排得靠后，是翻
        # 完所有页都看不到。
        #
        # 为什么必须全留：拼音完全相同的字之间，唯一能区分的就是词频，
        # 而词频低的字（常用但很少单独成词，比如「框」）天然吃亏。用户
        # 敲了完整准确的 kuang，这些字都是合法答案，能不能被翻到不该由
        # 语料词频决定。同理，拼音只是前缀命中的长词（`kuan` 之于「宽」）
        # 也一并留着，翻页就能看到。
        #
        # 代价是打分列表变长，实测对 587 条的 kuang 也只要几毫秒。
        #
        # 领域词（用户勾选了专业词库时）也在这里汇合——它们的 word id 在
        # 独立索引里，与主词典的 id 空间无关，所以第三项直接带成品
        # ``Candidate``，主词典那批带的是 ``None``。
        out: list[Candidate] = []
        for score, wid, cand in scored:
            out.append(cand if cand is not None
                       else Candidate(words[wid], pinyin[wid], score))
        out.sort(key=lambda c: -c.score)
        return out

    def _hot_candidates(self, limit: int = MAX_CANDIDATES,
                        single: bool = False) -> list[Candidate]:
        """打不出任何拼音时的高频词兜底（见 ``candidates`` 里的说明）。

        给的分很低（``TIER_HOT``），所以一旦有真候选，它们必然排在后面；
        存在的意义只是**让候选窗保持可见**，而不是干扰选择。

        ``single=True`` 时改为取高频**单字**。只敲了一个字母（多为韵母）时必须
        走这条：一个字母不可能写出双字词，给「一个」「中国」纯属误导。
        """
        dict_ = self.dictionary
        words = dict_.words
        pinyin = dict_.pinyin
        out: list[Candidate] = []
        for wid in (dict_.hot_chars(limit) if single else dict_.hot_words(limit)):
            uf = dict_.user_freq[wid]
            score = float(dict_.freq[wid]) + self.TIER_HOT + self.user_weight * uf
            out.append(Candidate(words[wid], pinyin[wid], score))
        return out

    def _segment_candidates(self, buf: str) -> list[Candidate]:
        """把长串拼音切成若干词拼成一句，返回整句候选。

        见 ``ycpy/segment.py`` 的说明。语料概率只解决「切在哪」，
        真正的歧义（比如 ``yiqi`` 该是「一起」还是「一切」）要靠
        用户词频和词间联动来打破——用户天天打「一起」就该排「一起」前面。
        """
        if self._segmenter is None:
            self._segmenter = Segmenter(self.dictionary)
        paths = self._segmenter.segment(buf)
        if not paths:
            return []

        words = self.dictionary.words
        user_freq = self.dictionary.user_freq
        out: list[Candidate] = []
        for rank, path in enumerate(paths):
            # 语料得分是负的 log 概率（越接近 0 越好）。
            # **分段器自己的顺序就是正确答案**，这里只把它换算成可比的
            # 候选分，不要再让用户词频去翻盘。
            #
            # 踩过的坑：原先按 ``-path.score * BASE`` 加用户词频重排，
            # 两次都错。第一次是符号写反（``-=`` 写成了 ``=``），排序整个
            # 颠倒；第二次是 BASE 放大到 40000，于是「大家|好」明明比
            # 「到|将|好」合理，却因为「到」「将」「好」各被选过几次而被
            # 压下去——放大语料分等于让**更差**的切分拿更高分，方向反了。
            #
            # 正确做法：给每条路径一个**递减**的基准分（保留分段器的判断），
            # 用户信号只在这个基准之上做小幅加成。``rank`` 本身就编码了
            # 语料的结论——第 1 条就是最合理的切法。
            score = -path.score * W_SEGMENT_BASE - rank * W_SEGMENT_RANK_STEP

            # 用户确实打过这一整串，是最强信号，给满额。
            whole = self.user.word_count(path.text)
            if whole:
                score += self.user_weight * whole * W_SEGMENT_USER_SHRINK

            # 各组成词的词频也要打折：用户选过「将」「好」，不代表他想打
            # 「到将好」。这一项只负责在切法接近时打破「一起/一切」这类
            # 词频几乎相同的歧义。
            for wid in path.word_ids:
                uf = user_freq[wid]
                if uf:
                    score += self.user_weight * uf * W_SEGMENT_USER_SHRINK
            out.append(Candidate(path.text, path.pinyin, score))
        # 必须显式排序：基准分只是「给分段器顺序兜底」，用户信号加在它上面
        # 之后顺序就变了。踩过这个坑——改完看分数是对的（一起 180000 >
        # 一切 30），但返回的列表仍是分段器的原始顺序，于是第一项还是
        # 「大家一切干活」。
        out.sort(key=lambda c: -c.score)
        return out

    def _static_bigram(self, left: str, right: str) -> int:
        """语料里的词间共现次数（无用户数据时的兜底联动）。"""
        return self._static_bigrams.get("%s|%s" % (left, right), 0)

    @staticmethod
    def _is_single_syllable(buf: str) -> bool:
        """输入是不是「只敲了一个韵母」，即该不该只给单字候选。

        用户的规则是「输入里出现**大于一个**韵母时，才筛选多字候选词」。
        拼音里每个音节恰好一个韵母，所以等价于「音节数 <= 1」。

        **切不开音节的输入不限制**：那是首字母输入（``cxy``、``zhguo``），
        本身就是奔着词去的——``cxy`` 要打的是「程序员」，限制成单字就
        彻底没法用了。

        实现上不能直接数字母里的元音个数：``yong`` 里的 ``g`` 是声母的
        一部分（``ong`` 是完整韵母），``lv``/``nv`` 里的 ``v`` 是 ``ü``
        的替身。交给 ``split_syllables`` 切才不会数错。
        """
        parts = syllables.split_syllables(buf)
        if parts is not None:
            return len(parts) <= 1
        # 切不开的当中，**单个元音字母**本身就是韵母（`i`、`u`、`v`、
        # `o` 都是合法的零声母韵母），同样只给单字。
        #
        # 漏了这条会让只敲 `i` 时兜底列出「一个」「中国」——`i` 切不开音节，
        # 于是被当成首字母输入放行了，可它其实就是个韵母。
        return len(buf) == 1 and buf in "aoeiuv"

    def _vowels_of_input(self, buf: str) -> frozenset:
        """输入串里出现的元音字母集合。

        用户敲 ``zhguo`` 说明他明确打出了 ``guo`` 这个音节。那些元音字母
        就是意图信号：候选的拼音里如果也含这些元音，说明韵母方向一致，
        该往前排。不含则说明只是声母碰巧撞上。
        """
        return frozenset(c for c in buf if c in "aoeiuv")

    def _coverage(self, buf: str, wid: int) -> float:
        """输入串 ``buf`` 对这个词的覆盖度，0.35 ~ 1.0。

        用来给用户词频打折：**敲得越少，就越不该把这个词顶到第一。**

        判据是「输入长度 vs 词拼音的音节数」：

        * 输入能覆盖词的全部音节（``cxy`` -> 程序员）-> 1.0，满额
        * 输入只够首字母、词有 3 个音节（``n`` -> 你好）-> 按比例打折

        之前没有这一层，用户高频词会在只敲一两个字母时就霸占第一位，
        看起来像是「输入法自己乱猜」，其实是我们没检查匹配质量。
        """
        syllables = self.dictionary.pinyin[wid].split()
        if not syllables:
            return 1.0
        # 输入串能划出的音节数（首字母输入划不出来时按字母数算）
        expected = self._expected_syllable_count(buf) or len(buf)
        if expected >= len(syllables):
            return 1.0
        # 至少给 0.35，再短也不至于完全否定用户偏好
        return max(0.35, expected / len(syllables))

    def _search_fuzzy(self, buf: str, exact_hits):
        """精确匹配落空时，改用猜出来的变体去查。

        返回 ``(hits, fuzzy_ids)``。一个变体都没命中就返回 ``([], set())``。
        """
        hits: list[tuple[int, bool, bool]] = []
        fuzzy_ids: set[int] = set()
        # 精确匹配一条都没有，才启用「音节补全」这类更激进的纠正。
        for variant in syllables.fuzzy_variants(buf, exact_ok=False):
            raw = variant.encode("ascii")
            got = list(self.dictionary.search(raw, raw))
            if not got:
                continue
            for wid, ef, ei in got:
                if wid not in fuzzy_ids:
                    # 保留变体查询里的「完全命中」标记：它说明这个变体猜对了
                    # （比如 chxy -> cxy，而「程序员」的首字母正是 cxy），
                    # 丢掉的话正确结果会被语料词频埋掉。
                    hits.append((wid, ef, ei))
                    fuzzy_ids.add(wid)
        return hits, fuzzy_ids

    def _add_fuzzy_hits(self, buf: str, hits, fuzzy_ids: set[int]) -> set[int]:
        """已经有精确命中时，看看变体能不能带进来更多词。

        返回新增的 fuzzy 命中 id 集合；原 ``hits`` 就地追加。

        只在精确命中**很少**时才做：`nha` 精确只命中「南海岸」一条，
        用户显然想敲的是「你好」，这时值得再猜；`cxy` 已经命中 104 条，
        说明敲得很准，再猜纯属浪费。

        ``exact_ok`` 的取舍（踩过的坑）
        --------------------------------
        原来这里固定用 ``exact_ok=True``，于是**音节补全被禁掉**。这在
        「精确命中=0」时无所谓（那时走的是 ``_search_fuzzy``，用
        ``exact_ok=False``），但一旦有**任何**一条精确命中就出问题：

            chenxuyuan（用户少敲了一个 g，想打「程序员」）

        词库里曾经没有 `chen-xu-yuan` 这个组合，所以精确命中 0 条 ->
        走 ``_search_fuzzy`` -> ``exact_ok=False`` 给出 chengxuyuan ->
        「程序员」出现。接入整句词频表（data/ycpy-phrase-freq.txt）后，
        词典里有了「陈戌源」(chen xu yuan) 这个真实但极罕见的人名，
        精确命中变成 1 条 -> 走这里 -> ``exact_ok=True`` 拿不到
        chengxuyuan -> **「程序员」从候选里彻底消失**。

        也就是说：一条冷僻的精确命中，把用户真正想打的常用词挤没了。
        所以按**精确命中的条数**分档——命中很少时连音节补全一起给，
        命中多了才只给浅层变体。
        """
        if len(hits) >= self.fuzzy_min_exact:
            return fuzzy_ids
        # 精确命中少，说明用户大概率是敲错了/没敲完，值得连音节补全一起试。
        # 命中多时退回浅层变体，避免把「敲得很准」的输入搅乱。
        exact_ok = len(hits) >= FUZZY_DEEP_MIN_EXACT
        seen = {wid for wid, _, _ in hits}
        for variant in syllables.fuzzy_variants(buf, exact_ok=exact_ok):
            raw = variant.encode("ascii")
            for wid, ef, ei in self.dictionary.search(raw, raw):
                if wid in seen:
                    continue
                seen.add(wid)
                fuzzy_ids.add(wid)
                # 同上：保留完全命中标记，否则猜对了也排不上去。
                hits.append((wid, ef, ei))
        return fuzzy_ids

    def _domain_scores(self, buf: str, expected: int, short_input: bool,
                       words: PackedStrings) -> list[tuple[float, str, Candidate]]:
        """用户勾选的专业领域词，给它们额外加分后并入候选池。

        **为什么是加性加分而不是「硬分区」**：见 ``W_DOMAIN`` 的说明。
        简而言之分区键会被 ``score_with_context`` 里第二处 ``sort`` 抹掉，
        而那里的二元组加权一次就 +9 万分。加在 ``cand.score`` 上则不存在
        这个问题——任何后续排序重新算分都不会丢掉它。

        走的是与主词典**同一条打分链**，只是少了「用户词频」和
        ``W_CHAR_COMMON`` 这两项：

        * 用户词频对领域词没有意义——用户不可能「学过」还没勾选的领域词。
        * ``W_CHAR_COMMON`` 只对单字生效，而 THUOCL 全是 >=2 字的词。

        领域词**不进主词典索引**，所以 word id 与主词典的 id 空间无关。
        返回的第三项是成品 ``Candidate``（主词典那批是 ``None``），由
        ``candidates()`` 统一组装。

        **主词典已有的词在这里直接跳过**，由 ``_boost_main_dict_words``
        单独提权：那个词已经在主词典的打分循环里算完了（含用户词频、
        ``W_EXACT_FULL``…），这里再整条链重跑一遍会凭空多出 27500 分。
        """
        if not self.domains.active():
            return []
        if self._main_words is None:
            self._main_words = set(self.dictionary.text_to_id())
        # 全拼键和首字母键用**同一个** buf 去查——与主词典
        # ``dict_.search(buf, buf)`` 保持一致。领域表的 init 键是「每音节
        # 首字母」（``shu ju ku`` -> ``sjk``），所以敲 `sjk` 会精确命中。
        raw = buf.encode("ascii")
        out: list[tuple[float, str, Candidate]] = []
        for text, dom_freq, kind in self.domains.lookup(raw, raw):
            if text in self._main_words:
                continue
            score = float(dom_freq) + _domain_bonus(kind)
            if kind == "full":
                score += W_EXACT_FULL
            elif kind == "init":
                score += W_EXACT_INIT
            n = len(text)
            if n == expected:
                score += W_SYL_COUNT
            if short_input:
                if n == 2:
                    score += W_LEN_SHORT
            elif n >= 5:
                score += W_LEN_LONG
            # 字体覆盖：跟主词典同一条规则——渲染成豆腐块的字不该出现。
            if self._font_cov is not None and not self._font_cov.covers(text[0]):
                continue
            out.append((score, text, Candidate(text, buf, score)))
        return out

    def _boost_main_dict_words(self, scored: list, words, buf: str) -> None:
        """给主词典里已命中的领域词补上 ``W_DOMAIN``。

        「疾病」这类词主词典本来就有，但用户勾了「医学」之后它理应享受
        领域优先——所以不是跳过它，而是提权。

        踩过的坑：原先在 ``_domain_scores`` 里直接 ``continue``，结果
        「疾病」一点分都加不上，被同领域更生僻、**主词典里根本没有**的
        「肌病」压到第 4 位——用户勾了「医学」，最常见的那个医学词
        反而掉下去。
        """
        if not self.domains.active():
            return
        if self._main_words is None:
            self._main_words = set(self.dictionary.text_to_id())
        boost = {text: _domain_bonus(kind)
                 for text, _f, kind in self.domains.lookup(
                     buf.encode("ascii"), buf.encode("ascii"))
                 if text in self._main_words}
        if not boost:
            return
        for i, item in enumerate(scored):
            if item[2] is not None:      # 领域索引带的是成品 Candidate
                continue
            extra = boost.get(words[item[1]])
            if extra:
                scored[i] = (item[0] + extra, item[1], item[2])

    def score_with_context(self, candidates: list[Candidate], context: str | None) -> None:
        """按已上屏的上下文重排。原地修改。"""
        if not context:
            return
        # 二元组是**用户数据**，所以它的权重必须跟着用户词频权重走。
        #
        # 原来这里写的是硬编码 ``W_BIGRAM_USER``（90000），于是首选项里的两个
        # 开关对它完全无效：
        #
        # * ``enable_learning=False``（「关闭后退化为纯语料排序」）-> 仍生效；
        # * ``user_freq_weight=0``（滑块说明写着「0 = 完全按语料排序」）-> 仍生效。
        #
        # 而且后果比「开关失灵」更严重：90000 一次就 +9 万分，比语料分的总
        # 波动（几千）大一个数量级，所以**只要历史上记过一条二元组，它就压过
        # 一切语料信号**。用户把权重拉到 0 得到的并不是纯语料排序。
        #
        # 换算按比例而不是直接用 user_weight：二元组是「上下文 -> 后继词」的
        # 强信号，值得比单词频更高（原来 90000 vs 60000，比例 1.5），但不该
        # 脱离用户对「用户数据占多大分量」的判断。
        if self.user_weight <= 0 or not self._learning_enabled:
            return
        weight = W_BIGRAM_USER * (self.user_weight / W_USER)
        for cand in candidates:
            cand.score += weight * self.user.bigram_count(context, cand.text)
        candidates.sort(key=lambda c: -c.score)

    def apply_user_counts(self, counts: dict[str, int]) -> None:
        """把持久化的用户词频灌进索引。"""
        self.dictionary.apply_user_counts(counts)

    @staticmethod
    def _expected_syllable_count(buf: str) -> int:
        """贪心地把输入串切成音节，返回音节数（用于偏好长度合适的词）。

        切不动时按「一个字母 = 一个音节」处理，这样首字母输入 ``cxy``
        会得到 3，从而给「程序员」这类三字词加分。
        """
        count = 0
        pos = 0
        n = len(buf)
        while pos < n:
            best = 0
            for length in range(min(5, n - pos), 0, -1):
                if buf[pos : pos + length] in syllables.SYLLABLES:
                    best = length
                    break
            count += 1
            pos += best if best else 1
        return count