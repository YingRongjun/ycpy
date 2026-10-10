"""汉字常用度打分：语料字频为主，《通用规范汉字表》分档为辅。

为什么换掉原来那套
------------------
最早只读 Unihan 的 ``kIRG_TSource``（《通用规范汉字表》分级）当常用度，
本意是把常用字排到生僻字前面。用户报的 bug 是「框一定比㑌这样的字更常用，
但框被放在后面」——确实修好了。但后来用户又报：

    「将、会、对、这、个 这些高频字为什么会在很后面」

查下来根因是：**《通用规范汉字表》的分级衡量的是「字形规范化程度」，
不是「使用频率」**。该表按「是否收录为规范字」分级，而「将/会/对/这/个」
的规范归属另有来源，于是全被划到 T3（稀缺字，500 分），而「者/正/遮/框」
这些远没那么常用的字反倒拿到 T1（1000 分）。

实测（改之前的 `score_for`）::

    将/会/对/这/个   T3 → 500 分
    者/正/框/遮      T1 → 1000 分

更糟的是 **T1 内部所有字分数完全相同**——这个信号在真正需要区分的地方
（「者」对「遮」）一点作用都没有，只能退回 jieba 词频。而 jieba 的单字 freq
统计的是「该字**独立成词**的次数」，「遮」几乎从不独立成词（893），
于是高频字被生僻字压下去：

    zhe 的 299 个候选里，「这」排第 140 位
    ge  的 159 个候选里，「个」排第 98 位

现在的做法
----------
**以真实语料字频为主信号**（``data/ycpy-char-freq.txt``，Jun Da 的
《现代汉语字符频次表》语料统计，MIT）。那张表给的是 ``frequency_rank``：
的=1、一=2、是=3、这=11、个=12、会=29、你=32、对=33……
「将/会/对/这/个」全在前 200 名内，正是它们该在的位置。

《通用规范汉字表》的分级保留下来，只用在两个地方：

1. **语料里没收录的字**给兜底分（扩展区、繁体区那些）；
2. 给主信号做**很轻的**乘性微调——它确实携带「这个字是不是规范收进来的
   常用字」的信息，但绝不能盖过字频（第 3 节那条 0.85 的系数就是为此）。

但第 1 条的兜底分必须**和语料分完全不重叠**
------------------------------------------
兜底分不能按「T 值越高分越高」直接给一个大数——那样它就会落进语料分的
有效区间里，把语料榜上真实常用字压下去。这正是用户报的 bug：

    「tai 里面 泰 排在 嬯 后面」（嬯 不是繁体）

查下来是这样的：``嬯`` 在 Unihan 里有 ``kIRG_TSource T2-5C61``，是《通用规范
汉字表》的**二级字**，但语料字频表没收录它，于是走 ``TIER_FALLBACK_SCORE[2]``。
而这个兜底分的**量级是错的**::

    T1 兜底 320 分 ≡ 语料榜第  521 名
    T2 兜底 250 分 ≡ 语料榜第  992 名
    T3 兜底 180 分 ≡ 语料榜第 1890 名

榜上有 9900 字，所以**任何一个未被收录的二级字，都能压过榜上 992 名之后的
全部常用字**。``泰`` 名次 1318、``抬``/``态`` 也都在射程内，于是生僻字插到了
常用字前面。

抽样看过那批「有 T 值但语料未收录」的字，190 个 T1 里是
``佈 佔 唸 喫 汙 淨 淒 湧 崑 妳``——清一色**异体字/地区用字**，它们的规范
写法（布 占 念 吃 污 净 涌 昆）本来就在榜上。把它们压到榜尾是对的。

所以现在的做法是**划带**：``[FREQ_MIN, 1000]`` 归语料收录的字，
``[0, FREQ_MIN)`` 归所有没被收录的字。于是「语料里查得到」本身就成了最强的
常用度信号，再细的分档只在带内做相对区分。

数据源
------
* 字频：``data/ycpy-char-freq.txt``（MIT，Rudd Fawcett / Jun Da 语料统计）
* 分级：``data/Unihan_IRGSources.txt``（Unicode License V3）
* 词频（多字词排序用）：jieba ``dict.txt``（MIT）

详见 ``data/CREDITS.md``。
"""

from __future__ import annotations

import math
import re

#: T 值 -> 乘性系数（**不是**分数）。T1 最高，T7 最低。
#:
#: 刻意压得很轻（0.85~1.0）：这个分级衡量的是字形规范化程度，不是使用频率。
#: 曾经把它当基准分用（T1=1000 / T3=500），结果「这/个/会/对/将」这些
#: 真实高频字被系统性打死，T 值大���等于「常用度低」是错的。
#: 现在它只是给字频分做一点微调。
TIER_FACTOR = {
    1: 1.00,      # 一级字（规范表收录的常用字）
    2: 0.97,
    3: 0.94,
    4: 0.70,      # 四级：繁体/港台用字，明显该往后排
    5: 0.68,
    6: 0.65,
    7: 0.62,
}

#: 语料字频映射成分数的满分档。
FREQ_SCORE_MAX = 1000

#: **保��带的下沿**：语料字频分一律 >= 这个值，没被语料收录的字一律 < 它。
#:
#: 这是「泰 排在 嬯 后面」那个 bug 的修法（详见模块文档）。之前兜底分
#: （T2=250）恰好落在语料分的中段，于是未被收录的二级字能压过语料榜上
#: 992 名之后的全部常用字。现在两边彻底分开，「语料里查得到」本身
#: 就是最强的常用度信号。
#:
#: 250 是怎么定的：语料榜 1318 名（泰）的分是 219，所以下沿必须远低于它
#: 才能保证「收录过的常用字」永远压过「没收录的字」。而 T1 里那些没收录的
#: 绝大多数是异体/地区用字（佈佔唸喫汙淨淒湧崑妳），压到带尾正是想要的。
FREQ_MIN = 150

#: 繁体字的固定分。压得比**任何**简体兜底分都低，确保繁体一律排在
#: 所有简体候选之后（用户要求「将繁体字的候选字都放到简体字后面」）。
#:
#: 为什么不用「不在字频表里就算繁体」：那会把大量生僻简体字（醤/櫤/㧑）
#: 也当成繁体，而且简体常用字本身得分可能比它低——江 309 分，繁体兜底 320 分，
#: 于是繁体字插到了**简体常用字前面**。必须用 Unicode 官方的繁简对应
#: （``ycpy-trad.txt``）来精确识别，见 ``tools/build_trad.py``。
TRADITIONAL_SCORE = 30

#: 语料里没收录、也不是繁体的字（扩展区生僻字等），按 T 值给的兜底分。
#:
#: **全部压在 ``FREQ_MIN`` 以下**——带内只做相对区分，不再和语料分抢位置。
#: 之前 T1=320/T2=250/T3=180 直接落进语料分的有效区间（320 分≈语料第 521 名），
#: 于是「泰」(1318) 被一个没收录过的二级字 嬯 压了下去。
TIER_FALLBACK_SCORE = {
    1: 110,
    2: 100,
    3: 85,
    4: 55,
    5: 50,
    6: 45,
    7: 40,
}

#: 连 T 值也没有的兜底分。
UNKNOWN_COMMON = 75

#: 码位区间。**判断顺序必须从大到小**：基本区（U+4E00-9FFF）的码位
#: 比扩展 A（U+3400-4DBF）**大**，先判 ``cp >= EXT_A`` 会把「为」
#: （U+4E3A）、「说」（U+8BF4）这些超高频字误判成罕用字——
#: 它们恰好没有 T 值，只能靠码位救。写反了会直接把它们打死。
EXT_B = 0x20000          # 扩展 B 及以后：几乎全是生僻字
COMPAT = 0xF900          # 兼容表意文字：繁体
BASIC_END = 0x9FFF       # 基本区末尾
EXT_A = 0x3400           # 扩展 A：罕用字为主

_LINE = re.compile(r"^U\+([0-9A-F]+)\tkIRG_TSource\tT(\d)-")


class CharRarity:
    """给汉字打常用度分（0~1000，越大越常用）。

    主信号是语料字频（Jun Da 字频表），分档（Unihan T 值）只做辅助。
    详见模块文档——那��段「为什么换掉原来那套」记录了踩过的坑。
    """

    def __init__(self, tiers: dict[int, int] | None = None,
                 char_rank: dict[str, int] | None = None):
        # codepoint -> T 值
        self._tiers: dict[int, int] = tiers or {}
        # 汉字 -> 语料频次名次（越小越常用）
        self._char_rank: dict[str, int] = char_rank or {}
        self._rank_min: int = min(self._char_rank.values()) if self._char_rank else 0
        self._rank_max: int = max(self._char_rank.values()) if self._char_rank else 0
        #: 繁体字集合。命中的一律排在所有简体候选之后（用户要求）。
        self._traditional: set[str] = set()
        self._cache: dict[str, int] = {}

    # ---------------------------------------------------------------- 载入

    @classmethod
    def from_zhiku(cls, path: str) -> "CharRarity":
        """从 ``Unihan_IRGSources.txt`` 载入《通用规范汉字表》分级。

        只读 ``kIRG_TSource`` 一个字段——现在它只是个辅助信号，
        要的只是「这个字有没有被规范表收录、收在哪一档」。
        """
        tiers: dict[int, int] = {}
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                if "kIRG_TSource" not in line:
                    continue
                m = _LINE.match(line)
                if m:
                    tiers[int(m.group(1), 16)] = int(m.group(2))
        return cls(tiers)

    @classmethod
    def from_trad_file(cls, path: str) -> set[str]:
        """读 ``ycpy-trad.txt``（每行一个繁体字），返回繁体字集合。"""
        out: set[str] = set()
        try:
            with open(path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.rstrip("\n")
                    if line and len(line) == 1:
                        out.add(line)
        except OSError:
            pass
        return out

    @classmethod
    def from_rank_file(cls, path: str, zhiku_path: str | None = None) -> "CharRarity":
        """从 ``ycpy-char-freq.txt`` 载入语料字频。

        文件格式是「字<TAB>频次名次」，由 ``tools/build_char_freq.py`` 生成。
        ``zhiku_path`` 给定时顺带载入分档，作为辅助信号。
        """
        rank: dict[str, int] = {}
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                if not line or line[0] == "#":
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2 or len(parts[0]) != 1:
                    continue
                try:
                    rank[parts[0]] = int(parts[1])
                except ValueError:
                    continue
        tiers: dict[int, int] = {}
        if zhiku_path:
            try:
                with open(zhiku_path, "r", encoding="utf-8") as fp:
                    for line in fp:
                        if "kIRG_TSource" not in line:
                            continue
                        m = _LINE.match(line)
                        if m:
                            tiers[int(m.group(1), 16)] = int(m.group(2))
            except OSError:
                pass
        return cls(tiers, rank)

    def attach_rank(self, char_rank: dict[str, int]) -> None:
        """挂上字频名次并清缓存（分数会变）。"""
        self._char_rank = char_rank
        self._rank_min = min(char_rank.values()) if char_rank else 0
        self._rank_max = max(char_rank.values()) if char_rank else 0
        self._cache.clear()

    def attach_traditional(self, chars: set[str]) -> None:
        """挂上繁体字集合并清缓存。

        繁体字会被压到 ``TRADITIONAL_SCORE``（见 ``score_for``）。
        """
        self._traditional = chars
        self._cache.clear()

    def is_traditional(self, ch: str) -> bool:
        """该字是否是繁体写法。"""
        return ch in self._traditional

    # ---------------------------------------------------------------- 查询

    def tier(self, ch: str) -> int | None:
        """该字的《通用规范汉字表》等级，查不到返回 ``None``。"""
        return self._tiers.get(ord(ch))

    def rank(self, ch: str) -> int | None:
        """该字的语料频次名次，查不到返回 ``None``（越小越常用）。"""
        return self._char_rank.get(ch)

    def _freq_score(self, ch: str) -> int | None:
        """按语料名次打分，0~``FREQ_SCORE_MAX``；没收录返回 ``None``。

        映射用 ``log``:``score = MAX * log(R/rank) / log(R/N)``，
        其中 ``R`` 是榜上最靠后的名次、``N`` 是最靠前的（这里 N=1）。

        **为什么必须是 log 而不是线性或倒数**（两个方向都踩过）：

        * 线性（``MAX*(1-rank/R)``）：前 50 名全挤在 970~1000，彼此几乎
          分不开，而前 50 名恰恰是最需要区分的（的/一/是/在/有/个/这…）。
        * 倒数（``MAX/rank``）：前 10 名就吃掉绝大部分量程，第 100 名之后
          全被压成个位数，「将」(132 名) 和「狂」(1147 名) 几乎同分——
          等于把「常用」和「次常用」的界限抹平了。
        * log：头部对数展开、尾部自然压缩，正好落在「前几百名分得开、
          长尾不喧宾夺主」的尺度上。
        """
        rank = self._char_rank.get(ch)
        if not rank or rank <= 0:
            return None
        lo = min(rank, self._rank_max)
        span = math.log(self._rank_max / float(self._rank_min))
        if span <= 0:
            return FREQ_SCORE_MAX
        return int(round(FREQ_SCORE_MAX * math.log(self._rank_max / float(lo)) / span))

    def score_for(self, ch: str) -> int:
        """该字的常用度分，0~1000，越大越常用。

        策略：**语料有字频就以字频为主**，T 值只做很轻的乘性微调；
        语料没收录的字才退回按 T 值/码位给兜底分。

        两条带严格分开（``FREQ_MIN`` 是分界）：语料收录过的字 >= ``FREQ_MIN``，
        没收录的 < ``FREQ_MIN``。这样「语料里查得到」本身就是最强的常用度
        信号，详见模块文档「兜底分必须和语料分完全不重叠」。

        **查不到 T 值不等于生僻字。** 为、说、给、见这些超高频字都没有
        T 值，所以这种情况必须靠码位区分：

        * 扩展 B 及以后 -> 几乎全是生僻字（`𪏪`），给最低分
        * 扩展 A -> 罕用字为主，给低分
        * 基本区 -> 正常汉字，只是没进规范表，给一个**中上**分
        """
        cached = self._cache.get(ch)
        if cached is not None:
            return cached

        # 繁体字：一律给最低的固定分，压到所有简体候选之后。
        # 先于其它分支判断——哪怕语料字频表碰巧收录了某个繁体写法
        # （「這」在表里，名次 4155，属于语料统计里的长尾），
        # 也照样按繁体处理。
        if ch in self._traditional:
            self._cache[ch] = TRADITIONAL_SCORE
            return TRADITIONAL_SCORE

        cp = ord(ch)
        tier = self._tiers.get(cp)

        freq_score = self._freq_score(ch)
        if freq_score is not None:
            factor = TIER_FACTOR.get(tier, 0.92)
            # 提到 ``FREQ_MIN`` 以上：只有语料收录的字能进这条带。
            # 乘完再钳位，防止 T4~T7 那几个重系数把收录字压穿到带下面
            # （T7=0.62 遇到长尾低分字时会）。
            score = max(int(round(freq_score * factor)), FREQ_MIN)
        elif tier is not None:
            score = TIER_FALLBACK_SCORE.get(tier, UNKNOWN_COMMON)
        elif cp >= EXT_B:
            score = 30                       # 扩展 B+：生僻字
        elif COMPAT <= cp <= 0xFAFF:
            score = 60                       # 兼容表意文字：繁体
        elif cp <= BASIC_END:
            # 基本区，但**既没有 T 值、语料里也没收录**。
            #
            # 这类字绝大多数是生僻字（醤/櫤/㧑/㻅/屶…），必须给低分——
            # 一开始给 700，结果它们集体涌到候选前列：`hui` 的头 4 名全是
            # 㧑/㻅/屶/颒，「会」被挤到第 5。
            #
            # 语料表有 9900 字，覆盖了绝大多数真正会打的字；落在这里的
            # 基本区字本来就该排在后面。至于「为/说/给/见」这类超高频字，
            # 它们**在语料表里有名次**（为=8、说=71、给=98、见=142），
            # 走的是上面的 freq 分支，根本不会落到这里。
            #
            # 码位区间**必须从大到小判**：基本区起点 0x4E00 大于扩展 A 的
            # 0x3400，先判扩展 A 会把这些字误判成扩展 A（更低的分）。
            score = 90
        elif cp >= EXT_A:
            score = 70                       # 扩展 A：罕用字为主
        else:
            score = UNKNOWN_COMMON
        self._cache[ch] = score
        return score

    def ranks_above(self, common: str, rare: str) -> bool:
        """``common`` 的常用度是否高于 ``rare``。"""
        return self.score_for(common) > self.score_for(rare)


__all__ = ["CharRarity", "TIER_FACTOR", "TIER_FALLBACK_SCORE",
           "FREQ_SCORE_MAX", "FREQ_MIN", "TRADITIONAL_SCORE",
           "UNKNOWN_COMMON"]