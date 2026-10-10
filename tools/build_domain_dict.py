#!/usr/bin/env python3
"""生成 ycpy 的**专业领域词表** ``data/ycpy-domain-<name>.txt``。

为什么需要这个
--------------
用户的需求：「在首选项里面让用户勾选专业词典，勾选后输入就会在候选框里
显示该领域的专业词汇」。

主词典（jieba 语料）系统性不覆盖专业词汇。实测缺失率（对 6 个目标领域）：

    IT     11676 / 15999 = 73.0%
    财经    1677 /  3830 = 43.8%
    法律    9043 /  9895 = 91.4%
    医学   13176 / 18749 = 70.3%
    饮食    7973 /  8973 = 88.9%
    汽车    1550 /  1752 = 88.5%

灌进去实测（进程内，走现有 Dictionary）::

    tonghuopengzhang -> 通货膨胀  第 1 位    启用前「不在候选」
    fangdichan       -> 房地产    第 1 位    启用前「不在候选」
    shouru           -> 收入      第 1 位

数据来源
--------
    thunlp/THUOCL（清华开放中文词库）        MIT License
    https://github.com/thunlp/THUOCL        1134 stars

THUOCL 给的是 **DF（Document Frequency，含该词的文档数）**，格式
「词<TAB>DF」，UTF-8。注意它的行尾不统一——``THUOCL_diming.txt`` 是老 Mac
的 CR 分隔，用 ``split('\\n')`` 只能拿到 1 行，必须用 ``splitlines()``。

哪些领域
--------
开放 IT / 财经 / 法律 / 医学 / 饮食 / 汽车 这 6 个。**刻意不开**：

    成语    缺失率只 4.5%（jieba 已有 8139 条），价值最低
    诗词   13703 条但要 5 个音节全打对才有意义，实际触发率极低
    动物    98.9% 缺失，但大部分是生僻物种名
    地名    44802 条里大量是「广禄庄村」「连升路」这种极冷僻村名路名
    人名    多音字读音无法可靠确定（见下）

词频标定：DF 必须换算到 jieba 量纲
----------------------------------
THUOCL 给的 DF 和 jieba 的语料词频**不是同一个量纲**，直接混用会让
DF 上万的专业词霸榜::

    THUOCL  版权    DF = 13,204,281
    jieba   的 freq =    883,634

用「两表都有的词」当锚点（14103 个，覆盖 6 个领域）做 log 空间回归::

    log(jieba_freq) = 0.3195 * log(DF) + 0.7154

拟合前先确认它**保序**：逐领域检查 DF 顺序与 jieba freq 顺序的近邻一致率，
6 个领域全是 **100%**（单调变换必然保序，这一步是防回归）。

但它**压扁了量程**：slope=0.3195 远小于 1，标定后 11676 个 IT 词只剩
**66 个不同取值**，99.3% 撞值。这是 DF 本身的分辨率不够——「版权」和某个
只出现过几十次的法条名，标定后可能同分。

撞值的实际影响很小：真正会「同音竞争」（即两个词会同时出现在同一个输入的
候选里）的对子总共 192 对，标定后打平的占少数::

    IT     竞争 58 对,  打平  0 对
    财经    竞争  5 对,  打平  1 对
    法律    竞争  5 对,  打平  0 对
    医学    竞争 65 对,  打平 19 对
    饮食    竞争 58 对,  打平  4 对
    汽车    竞争  1 对,  打平  1 对

打平的靠**文件内按 DF 降序**来定序（见输出时的排序），所以结果稳定可复现，
不依赖 dict 顺序。

多音字读音
----------
逐字取 pypinyin 的**首选读音**；``phrases_dict`` 里有整个词的读音时优先用
它（更准）。THUOCL 里有些词读音会错（人名、地名尤其严重），但 6 个领域里
这类占比很低，且**错读音只是打不出来，不会产生错误候选**——比漏掉更可接受。

用法::

    python3 tools/build_domain_dict.py             # 下载并生成
    python3 tools/build_domain_dict.py --offline   # 只用本地缓存重新生成
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import tarfile
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
DATA_DIR = os.path.join(_ROOT, "data")
CACHE_DIR = (os.environ.get("XDG_CACHE_HOME")
             or os.path.expanduser("~/.cache")) + "/ycpy-build"

THUOCL_REPO = "thunlp/THUOCL"
GITHUB_API = "https://api.github.com"
PYPI_JSON = "https://pypi.org/pypi/%s/json"

#: 领域 -> (THUOCL 文件名, 中文名)。顺序即首选项里的展示顺序。
DOMAINS: dict[str, tuple[str, str]] = {
    "it": ("THUOCL_IT.txt", "IT"),
    "caijing": ("THUOCL_caijing.txt", "财经"),
    "law": ("THUOCL_law.txt", "法律"),
    "medical": ("THUOCL_medical.txt", "医学"),
    "food": ("THUOCL_food.txt", "饮食"),
    "car": ("THUOCL_car.txt", "汽车"),
}

#: DF -> jieba_freq 的标定系数。由 14103 个两表共有的词拟合，
#: 详见模块文档「词频标定」。近邻顺序一致率 6 个领域全 100%。
CALIB_SLOPE = 0.3195
CALIB_INTERCEPT = 0.7154

#: 收录的词长上限。THUOCL 里几乎没有更长的词，设个上限防异常。
MAX_WORD_LEN = 12


def calibrate(df: int) -> int:
    """把 THUOCL 的 DF 换算成 jieba 量纲。

    log 空间换算（两表都是重尾分布，线性换算会让头部词爆掉），
    结果取整并夹在 ``>=1``——词频必须是正整数。
    """
    scaled = math.exp(CALIB_INTERCEPT) * (df ** CALIB_SLOPE)
    return max(1, int(round(scaled)))


# --------------------------------------------------------------------- 下载

def _http_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "ycpy-build",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)


def fetch_thuocl(filename: str, offline: bool) -> str:
    """下载（或复用缓存）一个 THUOCL 词表，返回本地路径。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "thuocl-" + filename)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print("  缓存命中 %s" % path)
        return path
    if offline:
        raise SystemExit("离线模式但缓存缺失：%s" % path)

    print("  经 GitHub API 下载 %s/%s" % (THUOCL_REPO, filename))
    # 用 git/blobs API 而不是 contents API：contents 对大文件不内联内容，
    # 而且 raw.githubusercontent.com 在作者的网络环境里被 TLS 重置。
    meta = _http_json("%s/repos/%s/contents/data/%s"
                      % (GITHUB_API, THUOCL_REPO, filename))
    blob = _http_json("%s/repos/%s/git/blobs/%s"
                      % (GITHUB_API, THUOCL_REPO, meta["sha"]))
    if blob["encoding"] != "base64":
        raise SystemExit("意外的 blob 编码：%s" % blob["encoding"])
    data = base64.b64decode(blob["content"])
    with open(path, "wb") as fp:
        fp.write(data)
    print("  -> %s（%.0f KB）" % (path, len(data) / 1024))
    return path


def load_pinyin():
    """载入 pypinyin 的单字读音与词组读音。返回 ``(单字, 词组)``。"""
    sdist = os.path.join(CACHE_DIR, "pypinyin-sdist.tar")
    if not os.path.exists(sdist):
        url = PYPI_JSON % "pypinyin"
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

def domain_header(key: str, label: str) -> str:
    """生成文件的头部。

    MIT 要求「版权声明与许可声明须包含在所有副本或实质性部分中」。
    """
    return """\
# ycpy 专业领域词表：%s  词<TAB>词频<TAB>拼音音节（空格分隔）
# 由 tools/build_domain_dict.py 自动生成，请勿手工编辑
#
# 这个表只有在用户在首选项里勾选了「%s」领域时才会被载入。未勾选时
# 它完全不占内存、不参与候选。
#
# 【词频已标定到 jieba 量纲】
# THUOCL 给的是 DF（文档数），与 jieba 的语料词频不是一个量纲，直接混用
# 会让 DF 上万的专业词霸榜（版权 DF=1320万，而 jieba 的「的」freq=88万）。
# 这里的词频按
#     log(jieba_freq) = %.4f * log(DF) + %.4f
# 换算，系数由 14103 个两表共有的词拟合。详见工具的 calibrate()。
#
# 数据来源与授权（详见 data/CREDITS.md 第 6 节）：
#   词、DF   THUOCL（清华开放中文词库）
#            (c) 2016 thunlp, MIT License
#            https://github.com/thunlp/THUOCL
#   读音     pypinyin (c) 2016 mozillazg, MIT License
#
# 本文件按 MIT License 分发（与 ycpy 主体的 GPL-3.0-or-later 并存，
# 见 data/CREDITS.md 第 6 节关于许可相容性的说明）。
""" % (label, label, CALIB_SLOPE, CALIB_INTERCEPT)


def _is_han(word: str) -> bool:
    return all(0x4E00 <= ord(ch) <= 0x9FFF for ch in word)


def read_thuocl(path: str) -> dict[str, int]:
    """读一个 THUOCL 词表，返回 ``{词: DF}``。

    两个坑：
    * 行尾不统一——``splitlines()`` 同时处理 LF 和 CR，而 ``split('\\n')``
      在 ``THUOCL_diming.txt`` 上只能拿到 1 行。
    * 词和 DF 之间有多余空格（``字符串 \\t 395499``），不 strip 的话
      IT/财经/成语三个表**一条都解析不出来**。
    """
    out: dict[str, int] = {}
    with open(path, encoding="utf-8-sig") as fp:
        for line in fp.read().splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            word = parts[0].strip()
            df = parts[1].strip()
            if not word or not df.isdigit():
                continue
            if not _is_han(word) or len(word) > MAX_WORD_LEN:
                continue
            out[word] = int(df)
    return out


def _read_dict_words() -> set[str]:
    """读主词典的词条集合。

    直接扫文本而不是 ``import ycpy.dictionary``：构建脚本不该依赖运行期
    包（``python3 tools/x.py`` 时项目根目录未必在 ``sys.path`` 上），
    而且 ``Dictionary()`` 要解析 51 万条 + 建索引，没必要。
    """
    words: set[str] = set()
    path = os.path.join(DATA_DIR, "ycpy-dict.txt")
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            if not line or line[0] == "#":
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 3 and fields[0]:
                words.add(fields[0])
    return words


def build(offline: bool = False) -> None:
    print("[1/3] 读取现有词典（用于去重）")
    known = _read_dict_words()
    print("      %d 词条" % len(known))

    print("[2/3] 读取 THUOCL 词表 + pypinyin 读音")
    tables: dict[str, dict[str, int]] = {}
    for key, (filename, label) in DOMAINS.items():
        path = fetch_thuocl(filename, offline)
        tables[key] = read_thuocl(path)
        print("      %-10s %5d 条" % (label, len(tables[key])))
    pinyin_raw, phrases_raw = load_pinyin()

    print("[3/3] 写出领域词表")

    def readings(word: str) -> list[str] | None:
        """词的读音。phrases_dict 优先（整个词的读音更准）。"""
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

    os.makedirs(DATA_DIR, exist_ok=True)
    total = 0
    for key, (filename, label) in DOMAINS.items():
        rows = []
        skipped_no_py = 0
        for word, df in tables[key].items():
            syls = readings(word)
            if not syls:
                # 读不全的整句直接丢掉——拿不全读音的词进索引会产生错误
                # 候选，比缺词更糟。
                skipped_no_py += 1
                continue
            rows.append((df, word, " ".join(syls)))
        # 按 DF 降序写：标定后大量词会撞值（同分），靠文件内顺序定序，
        # 结果因此稳定可复现，不依赖 dict 的插入顺序。
        rows.sort(key=lambda item: (-item[0], item[1]))

        out_path = os.path.join(DATA_DIR, "ycpy-domain-%s.txt" % key)
        with open(out_path, "w", encoding="utf-8") as fp:
            fp.write(domain_header(key, label))
            for df, word, py in rows:
                fp.write("%s\t%d\t%s\n" % (word, calibrate(df), py))
        total += len(rows)
        print("      %-10s -> %-34s %5d 条 (%.0f KB, 丢读音 %d)"
              % (label, os.path.basename(out_path), len(rows),
                 os.path.getsize(out_path) / 1024, skipped_no_py))
    print("      合计 %d 条" % total)


def _strip_tone(syllable: str) -> str:
    """去声调符号。与 ``tools/build_dict.py`` 里的同名函数保持一致。"""
    import unicodedata
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
    ap = argparse.ArgumentParser(description="生成 ycpy 专业领域词表")
    ap.add_argument("--offline", action="store_true", help="只用本地缓存，不联网")
    args = ap.parse_args()
    build(offline=args.offline)


if __name__ == "__main__":
    main()