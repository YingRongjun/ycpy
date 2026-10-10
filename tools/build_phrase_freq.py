#!/usr/bin/env python3
"""生成 ycpy 的整句词频表 ``data/ycpy-phrase-freq.txt``。

为什么需要这个表
----------------
用户报「输入 ``nihaoma`` 显示的是『妳好吗』而不是『你好吗』」。查下来根因是
**词典里根本没有 ``你好吗``**——输入 3 个音节只拿到 1 个候选，不是排序错了。

    $ 输入 nihaoma -> 共 1 个候选：妳好吗

而 jieba 的 ``dict.txt``（34 万词）里是这样的：

    你好吗   不在
    妳好吗   妳好吗 3 v      <- 频次 3，jieba 的默认兜底值
    妳       妳 29 zg
    你       你 234587 r

也就是说，**一个频次 3 的边角词赢了 234587 的「你」**——之所以能赢，只是因为
它的三音节形式被收录了，而正确形式没有。``你好``(725) 在词典里，``你好吗``
不在，词典只有拆开的两条。

这不是孤例。jieba 词典系统性不覆盖口语整句：

    是吗  对吗  好吗  行吗  可以吗  你好吗  谢谢你  吃了吗    全部不在
    不用谢                                                  在

于是任何 jieba 没收录的整句，你唯一能拿到的候选就是它那些边角异体词的组合
（``妳`` 下面就挂着 ``妳们 妳会 妳在 妳好吗 妳是 妳有 妳爱 妳自己`` 一串）。

换别的语料库解决不了
--------------------
试过几个，都会因为「已经分过词」而失效——``你好吗`` 在任何**分词后**的语料里
都会被切成 ``你好`` + ``吗``：

    BCC 语料库（北语，dialogue_word_freq.txt，136405 词条，口语域）
        实际下载验过：一条都补不上
    CCL 语料库（北大）词表下载入口404，站点只有检索界面
    jnext/chinese_word_frequency（HuggingFace，Apache-2.0）
        下载验过：**繁体** + 垃圾 n-gram（饒你狗 / 點你還），频次全是 1

所以要的是**未经分词的整句**及其频次。rime-frost 的 ``知频.txt`` 就是这个东西
——它虽然是用 jieba 分词统计的，但统计结果里保留了 ``你好吗`` 这样的整句
（因为分词前它就作为一个 token 存在于自定义分词词典里）。

数据来源
--------
    gaboolic/rime-frost（白霜拼音）  others/知频.txt
    https://github.com/gaboolic/rime-frost     GPL-3.0
    2,128,233 条，格式「词<TAB>频次」，UTF-8，约 28 MB

上游链路::

    MNBVC 语料（MIT，esbatmop/MNBVC）
        -> jieba 分词统计 -> 知频.txt 落在 GPL-3.0 仓库里

即**语料本身是 MIT 的**，GPL-3.0 来自整理/统计产物所在的仓库。ycpy 因此
整体改用 GPL-3.0-or-later（见 LICENSE「为什么从 MIT 换成 GPL-3.0」）。

为什么用 GitHub API 而不是 raw.githubusercontent.com
---------------------------------------------------
这个环境里 ``raw.githubusercontent.com`` 在 TLS 握手被重置（``Connection
reset by peer``），而 ``api.github.com`` 可达。所以走
``contents`` API 拿 blob sha、再用 ``git/blobs`` API 取内容——
大于 1 MB 的文件 ``contents`` API 不会内联内容，只有 blob API 能拿到。

阈值怎么定的
------------
``MIN_FREQ = 50``，按「修好用户报的那些口语整句」倒推：

    阈值    条数     估体积   18 句常用口语里能补上
      20   286,807    6.3MB   17/18
      50   133,770    2.9MB   17/18   <- 选这个
     100    70,625    1.6MB   16/18   （漏掉「吃了吗」126、「你好啊」89）
     200    36,510    0.8MB   13/18   （漏掉用户报的「你好吗」166）

再高就会漏掉 ``你好吗``(166)、``吃了吗``(126) 这些真实会被打的整句，
再低只是往里塞长尾噪音。

用法::

    python3 tools/build_phrase_freq.py             # 下载并生成
    python3 tools/build_phrase_freq.py --offline   # 只用本地缓存重新生成
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import tarfile
import unicodedata
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
DATA_DIR = os.path.join(_ROOT, "data")
CACHE_DIR = (os.environ.get("XDG_CACHE_HOME")
             or os.path.expanduser("~/.cache")) + "/ycpy-build"

#: 上游仓库与文件。``知频.txt`` 是中文文件名，走 API 要百分号编码。
RIME_FROST_REPO = "gaboolic/rime-frost"
RIME_FROST_PATH = "others/知频.txt"
GITHUB_API = "https://api.github.com"

PYPI_JSON = "https://pypi.org/pypi/%s/json"

#: 只收录 >=3 字的词：2 字词 jieba 覆盖得不错，缺的主要是整句。
MIN_WORD_LEN = 3
MAX_WORD_LEN = 8

#: 频次下限。见模块文档「阈值怎么定的」。
MIN_FREQ = 50

#: 知频词频 -> jieba 词频 的标定系数。
#:
#: **两张表的词频不在同一个量纲上，不能直接比。** 实测（用 246096 个两表
#: 都有的词做 log 空间回归）：
#:
#:     log(jieba_freq) = 0.4932 * log(知频) + 0.6904
#:
#: 也就是说知频的数值系统性偏大——直接写进词典会盖过 jieba 自己的词频，
#: 于是用户报的那些词全被带歪：
#:
#:     chengxuyuan  程序猿(知频 901，新引入) 压过 程序员(jieba 170)
#:     -> 5 个候选测试挂了 2 个
#:
#: 所以这里把知频的分数换算到 jieba 量纲再写出去。系数是**回归拟合**来的，
#: 不是拍脑袋：拟合用的是两表共有的 24.6 万个词，覆盖从长尾到头部的
#: 整个分布。
#:
#: 换算在 log 空间做（两表都是重尾分布，线性换算会让头部词爆掉），
#: 结果取整并夹在 ``>=1``——词频必须是正整数。
CALIB_SLOPE = 0.4932
CALIB_INTERCEPT = 0.6904

_BASIC_LO = 0x4E00
_BASIC_HI = 0x9FFF


def calibrate(know_freq: int) -> int:
    """把知频的词频换算成 jieba 量纲。"""
    import math
    scaled = math.exp(CALIB_INTERCEPT) * (know_freq ** CALIB_SLOPE)
    return max(1, int(round(scaled)))


# --------------------------------------------------------------------- 下载

def _http_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "ycpy-build",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def fetch_zhifu(offline: bool) -> str:
    """下载（或复用缓存）``知频.txt``，返回本地路径。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "rime-frost-zhifu.txt")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print("  缓存命中 %s" % path)
        return path
    if offline:
        raise SystemExit("离线模式但缓存缺失：%s" % path)

    print("  经 GitHub API 下载 %s/%s" % (RIME_FROST_REPO, RIME_FROST_PATH))
    # 先 contents 拿 blob sha——大文件 contents API 不内联内容
    quoted = urllib.parse.quote(RIME_FROST_PATH)
    meta = _http_json("%s/repos/%s/contents/%s" % (GITHUB_API, RIME_FROST_REPO, quoted))
    sha = meta["sha"]
    blob = _http_json("%s/repos/%s/git/blobs/%s" % (GITHUB_API, RIME_FROST_REPO, sha))
    if blob["encoding"] != "base64":
        raise SystemExit("意外的 blob 编码：%s" % blob["encoding"])
    data = base64.b64decode(blob["content"])
    with open(path, "wb") as fp:
        fp.write(data)
    print("  -> %s（%.1f MB）" % (path, len(data) / 1e6))
    return path


def load_pinyin():
    """载入 pypinyin 的单字读音与词组读音。返回 ``(单字, 词组)``。"""
    sdist = os.path.join(CACHE_DIR, "pypinyin-sdist.tar")
    if not os.path.exists(sdist):
        if not os.path.exists(sdist):
            url = "https://pypi.org/pypi/pypinyin/json"
            with urllib.request.urlopen(url, timeout=60) as resp:
                meta = json.load(resp)
            target = [f for f in meta["urls"] if f["packagetype"] == "sdist"][0]
            print("  下载 %s" % target["filename"])
            data = urllib.request.urlopen(target["url"], timeout=300).read()
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(sdist, "wb") as fp:
                fp.write(data)
    with tarfile.open(sdist) as tf:
        pinyin_raw = phrases_raw = None
        for name in tf.getnames():
            if name.endswith("/pinyin_dict.json"):
                pinyin_raw = json.load(tf.extractfile(name))
            elif name.endswith("/phrases_dict.json"):
                phrases_raw = json.load(tf.extractfile(name))
    if pinyin_raw is None or phrases_raw is None:
        raise SystemExit("pypinyin sdist 里找不到读音表")
    return pinyin_raw, phrases_raw


# --------------------------------------------------------------------- 生成

#: 生成文件的头部。
#:
#: **不是可选的礼节，是授权义务**：MIT 要求「版权声明与许可声明须包含在所有
#: 副本或实质性部分中」，GPL-3.0 第 4 节同样要求保留声明。
PHRASE_HEADER = """\
# ycpy 整句词频表  词<TAB>词频<TAB>拼音音节（空格分隔）
# 由 tools/build_phrase_freq.py 自动生成，请勿手工编辑
#
# 这个表解决的是「输入 nihaoma 拿到的是『妳好吗』而不是『你好吗』」——
# 根因是 jieba 词典里根本没有「你好吗」这条词，3 个音节只拿到 1 个候选，
# 那唯一的候选恰好是频次 3 的边角词。jieba 系统性不覆盖口语整句
# （是吗/对吗/好吗/行吗/可以吗/谢谢你 全都不在），详见工具模块文档。
#
# 【词频已换算到 jieba 量纲】
# 知频表和 jieba 的词频不在同一个尺度上，直接混用会盖过 jieba 自己的数值
# （程序猿 901 会压过 程序员 170）。这里的词频是按
#     log(jieba) = 0.4932 * log(知频) + 0.6904
# 换算过的，系数由 24.6 万个两表共有的词拟合得到。详见工具的 calibrate()。
#
# 数据来源与授权（详见 data/CREDITS.md 第 5 节）：
#   词、词频  rime-frost（白霜拼音）others/知频.txt
#             (c) 2024 gaboolic, GNU General Public License v3
#             https://github.com/gaboolic/rime-frost
#
#   上游语料  MNBVC (c) esbatmop, MIT License
#             https://github.com/esbatmop/MNBVC
#   读音      pypinyin (c) 2016 mozillazg, MIT License
#
# 本文件是上述数据的衍生作品，按 GPL-3.0-or-later 分发。ycpy 整体因此
# 采用 GPL-3.0-or-later（见 LICENSE）。
"""


def _is_han(word: str) -> bool:
    """是否纯汉字（基本区）。排除含标点、数字、字母的条目。"""
    return all(_BASIC_LO <= ord(ch) <= _BASIC_HI for ch in word)


def _is_segmentation_repeat(word: str) -> bool:
    """是否「同一个 token 被连吐两次」的产物。

    上游是拿 jieba 分词后统计的，所以会出现 ``对不起对不起``（= ``对不起`` +
    ``对不起``）这种**没有人会这么打**的整句——实测词频 101，是分词边界
    的产物，不是真实输入。

    判据：``w == X+X`` 且要求**整除**。这一条限制很关键，否则会误杀大量
    正常词——``仅仅是``（``仅`` + ``仅仅`` + ``是``）、``往往会``、
    ``慢慢的``、``谢谢你`` 都会被当成重复。

    代价：``非常非常``/``一个一个``/``哈哈哈哈`` 这些确实存在的叠词也会被
    剔掉。这可以接受——打 ``非常非常`` 时 ``非常`` 仍然是候选，少一个冗余
    叠词候选没有任何损失；而留着它们会让「对不起对不起」这类真垃圾混进候选。
    """
    n = len(word)
    for k in range(2, n // 2 + 1):
        if n % k == 0 and word[:k] * (n // k) == word:
            return True
    return False


def load_zhifu(path: str, known: set[str]) -> dict[str, int]:
    """读 ``知频.txt``，筛出「词典里没有、且够常用」的整句。

    ``known`` 是现有词典的词条集合——已在词典里的不重复收录（它们已经有
    jieba 的词频了，那份更权威）。
    """
    out: dict[str, int] = {}
    skipped_len = skipped_dup = skipped_rare = skipped_rep = 0
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2 or not parts[0]:
                continue
            word = parts[0]
            if not _is_han(word) or len(word) < MIN_WORD_LEN:
                skipped_len += 1
                continue
            try:
                freq = int(parts[1])
            except ValueError:
                continue
            if freq < MIN_FREQ:
                skipped_rare += 1
                continue
            if word in known:
                skipped_dup += 1
                continue
            if _is_segmentation_repeat(word):
                skipped_rep += 1
                continue
            out[word] = calibrate(freq)
    print("  收录 %d 条（跳过：非汉字或过短 %d / 已在词典 %d / 频次不足 %d "
          "/ 分词重复产物 %d）"
          % (len(out), skipped_len, skipped_dup, skipped_rare, skipped_rep))
    return out


def build(offline: bool = False) -> None:
    print("[1/3] 读取现有词典（用于去重）")
    known: set[str] = set()
    dict_path = os.path.join(DATA_DIR, "ycpy-dict.txt")
    if os.path.exists(dict_path):
        with open(dict_path, encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("#") or not line.strip():
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) >= 3:
                    known.add(fields[0])
    print("      %d 词条" % len(known))

    print("[2/3] 读取整句词频表 + pypinyin 读音")
    zhifu_path = fetch_zhifu(offline)
    phrases = load_zhifu(zhifu_path, known)
    pinyin_raw, phrases_raw = load_pinyin()

    print("[3/3] 写出 %s" % os.path.join(DATA_DIR, "ycpy-phrase-freq.txt"))
    # 逐字首选读音；词组读音优先（多音字更准）。读不全的整句直接丢掉——
    # 拿不全读音的词进词典会产生错误候选，比缺词更糟。
    def readings(word: str) -> list[str] | None:
        if word in phrases_raw:
            out = []
            for group in phrases_raw[word]:
                if not group:
                    return None
                syl = _strip_tone(group[0])
                if syl:
                    out.append(syl)
            return out if len(out) == len(word) else None
        out = []
        for ch in word:
            field = pinyin_raw.get(str(ord(ch)))
            if not field:
                return None
            syl = _strip_tone(field.split(",")[0])
            if not syl:
                return None
            out.append(syl)
        return out

    rows = []
    no_pinyin = 0
    for word, freq in phrases.items():
        syls = readings(word)
        if not syls:
            no_pinyin += 1
            continue
        rows.append("%s\t%d\t%s" % (word, freq, " ".join(syls)))
    if no_pinyin:
        print("      丢弃 %d 条读音不全的整句" % no_pinyin)

    rows.sort()
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, "ycpy-phrase-freq.txt")
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(PHRASE_HEADER)
        fp.write("\n".join(rows))
        fp.write("\n")
    print("      -> %s（%d 行, %.1f MB）"
          % (out_path, len(rows), os.path.getsize(out_path) / 1e6))


def _strip_tone(syllable: str) -> str:
    """去声调符号。与 ``tools/build_dict.py`` 里的同名函数保持一致。"""
    table = {
        "ā": "a", "á": "a", "ǎ": "a", "à": "a",
        "ē": "e", "é": "e", "ě": "e", "è": "e", "ê": "e",
        "ī": "i", "í": "i", "ǐ": "i", "ì": "i",
        "ō": "o", "ó": "o", "ǒ": "o", "ò": "o",
        "ū": "u", "ú": "u", "ǔ": "u", "ù": "u",
        "ǖ": "v", "ǘ": "v", "ǚ": "v", "ǜ": "v", "ü": "v",
        "ń": "n", "ň": "n", "ǹ": "n",
        "ḿ": "m",
    }
    if syllable in table:
        return table[syllable]
    return "".join(c for c in unicodedata.normalize("NFD", syllable)
                   if not unicodedata.combining(c))


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 ycpy 整句词频表")
    ap.add_argument("--offline", action="store_true",
                    help="只用本地缓存，不联网")
    args = ap.parse_args()
    build(offline=args.offline)


if __name__ == "__main__":
    main()