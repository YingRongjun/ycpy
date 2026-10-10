#!/usr/bin/env python3
"""生成 ycpy 的词典文件。

数据来源（都是自由软件 / 开放数据）：

* **jieba** 的 ``dict.txt`` —— 34 万词条 + 真实语料词频，用来给候选排序；
* **pypinyin** 的 ``pinyin_dict.json`` —— 4 万汉字的全部读音（多音字全收）；
* **pypinyin** 的 ``phrases_dict.json`` —— 4.7 万词组的正确读音，同时用来
  挖掘「二字上下文 → 二字后继词」的静态搭配表，作为开箱即用的联想输入。

用法::

    python3 tools/build_dict.py            # 下载（带缓存）并生成
    python3 tools/build_dict.py --offline  # 只用本地缓存重新生成
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")) + "/ycpy-build"

PYPI_JSON = "https://pypi.org/pypi/%s/json"

# 去声调：pypinyin 的数据带声调符号
_TONE_MAP = {}
for _base, _marks in (
    ("a", "āáǎà"),
    ("e", "ēéěè"),
    ("i", "īíǐì"),
    ("o", "ōóǒò"),
    ("u", "ūúǔù"),
    ("ü", "ǖǘǚǜ"),
    ("n", "ńňǹ"),
    ("m", "ḿ"),
):
    for _mark in _marks:
        _TONE_MAP[_mark] = _base
_TONE_MAP["ê"] = "ê"

CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0xF900, 0xFAFF),
)

MAX_WORD_LEN = 10
# 字级二元组表：多少个高频字、每个字保留多少个后继
TOP_CHARS = 2500
TOP_SUCC = 16
MAX_READINGS_PER_WORD = 2
DEFAULT_FREQ = 30
# 单字默认词频：让单字候选不至于垫底，但也别盖过常用词
CHAR_FREQ = 400


def strip_tone(syllable: str) -> str:
    out = []
    for ch in syllable:
        out.append(_TONE_MAP.get(ch, ch))
    s = "".join(out)
    return s.replace("ü", "v").replace("u:", "v").lower()


def is_han(text: str) -> bool:
    return all(any(lo <= ord(c) <= hi for lo, hi in CJK_RANGES) for c in text)


def initials_of(flat: str) -> str:
    return "".join(part[0] for part in flat.split() if part)


# --------------------------------------------------------------------- 下载

def _fetch(url: str, cache_name: str, offline: bool) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print("  缓存命中 %s" % path)
        return path
    if offline:
        raise SystemExit("离线模式但缓存缺失：%s" % path)
    print("  下载 %s" % url)
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = resp.read()
    with open(path, "wb") as fp:
        fp.write(data)
    return path


def _pypi_sdist_url(package: str) -> str:
    with urllib.request.urlopen(PYPI_JSON % package, timeout=60) as resp:
        meta = json.load(resp)
    urls = meta["urls"]
    for f in urls:
        if f["packagetype"] == "sdist":
            return f["url"]
    raise SystemExit("找不到 %s 的 sdist" % package)


UNIHAN_URL = "https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip"
UNIHAN_MEMBER = "Unihan_IRGSources.txt"

#: 生成词典文件的头部声明。
#:
#: **这不是可选的礼节，是授权义务。** 词条/词频来自 jieba（MIT）、
#: 拼音来自 pypinyin（MIT）。MIT 要求「版权声明与许可声明须包含在所有
#: 副本或实质性部分中」——构建产物就是这个副本。只写「自动生成，请勿
#: 手工编辑」不满足要求。
#:
#: 详见 data/CREDITS.md（含 CC-CEDICT 为何**没有**混入的核查过程）。
DICT_HEADER = [
    "# ycpy 词典  词<TAB>词频<TAB>拼音音节（空格分隔）",
    "# 由 tools/build_dict.py 自动生成，请勿手工编辑",
    "#",
    "# 数据来源与授权（详见 data/CREDITS.md）：",
    "#   词条、词频  jieba (c) Sun Junyi, MIT License",
    "#              https://github.com/fxsjy/jieba",
    "#   单字读音    pypinyin (c) 2016 mozillazg, MIT License",
    "#   词组读音    https://github.com/mozillazg/python-pinyin",
    "# 本文件是上述数据的衍生作品，按相同条款分发。",
    "",
]

#: Unihan_IRGSources.txt 的头部。Unicode License V3 明确要求版权与许可
#: 声明随数据文件副本一同出现，不加就是违约。
UNIHAN_HEADER = """\
# Unihan_IRGSources.txt -- Unicode(R) Character Database
# Copyright (c) 1991-2026 Unicode, Inc.  All rights reserved.
# Distributed under the Terms of Use in https://www.unicode.org/copyright.html
#
# UNICODE LICENSE V3 -- 见 https://www.unicode.org/license.txt
# 简要：免费提供数据文件，允许使用/复制/修改/再分发（含销售），条件是
# 本版权与许可声明须随所有副本一同出现。数据按「现状」提供，不含任何
# 担保。
#
# 本副本由 tools/build_dict.py 自 Unihan.zip 解包而成，只保留
# kIRG_TSource（《通用规范汉字表》分级）相关行之前的原始内容。
# 完整许可与索引见 https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip
#
"""


def fetch_char_freq(offline: bool) -> str:
    """取 Unicode 官方的 Unihan.zip，解出《通用规范汉字表》分级。

    **为什么需要它**：jieba 的 ``dict.txt`` 给的是**词**频——统计「这个
    字符串作为独立词条出现多少次」。常用字大多只作为词的一部分出现
    （「框」只在框架/窗口/文本框里），于是它自己的 freq 只有 299，
    反而低于建库时的默认分 400，生僻字就排到前面去了。

    Unihan 的 ``kIRG_TSource`` 给出《通用规范汉字表》分级：一级字就是
    常用字。这是**字**级别的信号，正好补上词频缺的那一维。

    只取这一个字段的文件（约 13 MB），其它一概不用。

    Unicode License V3 要求：分发数据文件时，版权与许可声明必须随文件
    一同出现（"this copyright and permission notice appear with all copies
    of the Data Files"）。所以在文件头插入一段声明——**这一步不能省**，
    否则单独分发这个文件就是违约。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, "Unihan_IRGSources.txt")
    if offline and os.path.exists(out_path):
        print("  已有 Unihan_IRGSources.txt，跳过下载")
        return out_path

    url = UNIHAN_URL
    archive = _fetch(url, "Unihan.zip", offline)
    print("  解包 %s …" % UNIHAN_MEMBER)
    with zipfile.ZipFile(archive) as zf:
        with zf.open(UNIHAN_MEMBER) as src:
            data = src.read()
    with open(out_path, "wb") as fp:
        fp.write(UNIHAN_HEADER.encode("utf-8"))
        fp.write(data)
    print("      -> %s（%.1f MB，含许可声明）"
          % (out_path, os.path.getsize(out_path) / 1e6))
    return out_path


def load_jieba(offline: bool) -> dict[str, int]:
    url = _pypi_sdist_url("jieba")
    tar_path = _fetch(url, "jieba-sdist.tar", offline)
    print("  解包 dict.txt …")
    words: dict[str, int] = {}
    with tarfile.open(tar_path, "r:*") as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("jieba/dict.txt"))
        fp = tf.extractfile(member)
        with io.TextIOWrapper(fp, encoding="utf-8") as text:
            for line in text:
                fields = line.split()
                if not fields:
                    continue
                word = fields[0]
                if len(word) > MAX_WORD_LEN or not is_han(word):
                    continue
                try:
                    freq = int(fields[1])
                except (IndexError, ValueError):
                    continue
                words[word] = freq
    return words


def load_pypinyin(offline: bool):
    url = _pypi_sdist_url("pypinyin")
    sdist = _fetch(url, "pypinyin-sdist.tar", offline)
    if sdist.endswith(".zip"):
        zf = zipfile.ZipFile(sdist)
        read = lambda n: zf.read(n)  # noqa: E731
        names = set(zf.namelist())
    else:
        tf = tarfile.open(sdist, "r:*")
        read = lambda n: tf.extractfile(n).read()  # noqa: E731
        names = {m.name for m in tf.getmembers()}

    char_member = next(n for n in names if n.endswith("pypinyin/pinyin_dict.json"))
    phrase_member = next(n for n in names if n.endswith("pypinyin/phrases_dict.json"))
    print("  解包 pypinyin 词典 …")
    return json.loads(read(char_member)), json.loads(read(phrase_member))


# --------------------------------------------------------------------- 生成

def build(offline: bool = False) -> None:
    print("[1/5] 读取 jieba 词频表")
    jieba = load_jieba(offline)
    print("      %d 词条" % len(jieba))

    print("[2/5] 读取 pypinyin 读音表 + Unihan 汉字分级")
    char_raw, phrase_raw = load_pypinyin(offline)
    print("      %d 汉字 / %d 词组" % (len(char_raw), len(phrase_raw)))
    # 单字常用度：jieba 只有词频，「框」这类常用字的词频极低，
    # 会被生僻字压下去。《通用规范汉字表》分级正好补上这一维。
    fetch_char_freq(offline)

    print("[3/5] 组装词条")
    # 汉字 -> 读音列表
    char_readings: dict[str, list[str]] = {}
    for code, pinyin_field in char_raw.items():
        ch = chr(int(code))
        readings = [strip_tone(p) for p in pinyin_field.split(",")]
        readings = [r for r in readings if r]
        if readings:
            char_readings[ch] = readings

    # 词组 -> 每字读音列表
    phrase_readings: dict[str, list[list[str]]] = {}
    for phrase, per_char in phrase_raw.items():
        if not is_han(phrase) or len(phrase) > MAX_WORD_LEN:
            continue
        readings = [strip_tone(choice) for group in per_char for choice in group[:1]]
        if len(readings) == len(phrase):
            phrase_readings[phrase] = readings

    lines: list[str] = []
    seen: set[tuple[str, str]] = set()

    def emit(word: str, syllables: list[str], freq: int) -> None:
        py = " ".join(syllables)
        key = (word, py)
        if key in seen:
            return
        seen.add(key)
        lines.append("%s\t%d\t%s" % (word, freq, py))

    def primary_readings(word: str) -> list[str]:
        """该词的**首选**读音（不含多音字展开）。

        词组读音优先，其次逐字取首选。没有这个「不展开」的版本，
        后面的 ``attested`` 就会被自己生成的伪词污染——见那里的说明。
        """
        if word in phrase_readings:
            return list(phrase_readings[word])
        return [char_readings[ch][0] for ch in word if ch in char_readings]

    # ---- 先算出「有词证」的读音集合 -------------------------------
    #
    # 用户报「输入 ne 会出现『而』，而这个字也不是多音字」。pypinyin 的
    # pinyin_dict.json 里 而 = 'ér,néng'，于是一层层展开下来，词库给「而」
    # 也登记了 `neng` 读音，首字母索引变成 `n`——敲 `ne` 就命中了。而「而」
    # 读 néng 在现实中根本不存在（它不是多音字），那是词典数据里的讹误。
    # 同类还有 㑌(kuang->wang)、〇(ling->yuan) 等生僻字，共 7437 个。
    #
    # 判据：这个读音是否在**真实词**里出现过。证据必须取自「首选读音」——
    # 若拿展开后的读音去统计，`而能` 这类我们**自己生成的伪词**会反过来
    # 证明「而 的 neng 是合法的」，循环论证，垃圾读音永远删不掉。
    attested: dict[str, set[str]] = {}
    for word in list(jieba) + list(phrase_readings):
        syls = primary_readings(word)
        if len(syls) != len(word) or len(word) < 2:
            continue
        for i, ch in enumerate(word):
            attested.setdefault(ch, set()).add(syls[i])

    dropped_readings = 0

    def readings_for(word: str) -> list[list[str]]:
        """返回该词可能的读音组合（最多 MAX_READINGS_PER_WORD 组）。"""
        primary = primary_readings(word)
        combos = [primary] if primary else []
        if len(primary) != len(word):
            return combos[:MAX_READINGS_PER_WORD]

        # 多音字：把每个可替换的字展开，最多再补两组。
        # 只展开「有词证」的读音——否则「三十而立」会被造出
        # `san shi neng li` 这种根本不存在的读法。
        alternatives = []
        for i, ch in enumerate(word):
            for alt in char_readings.get(ch, ())[1:]:
                if alt not in attested.get(ch, ()):
                    continue
                combo = list(primary)
                combo[i] = alt
                alternatives.append(combo)
        combos.extend(alternatives[: MAX_READINGS_PER_WORD - 1])
        return combos[:MAX_READINGS_PER_WORD]

    for word, freq in jieba.items():
        for combo in readings_for(word):
            emit(word, combo, freq)

    # jieba 里没有、但 pypinyin 收录的词组，给一个保底词频
    for word in phrase_readings:
        if word in jieba or len(word) == 1:
            continue
        emit(word, phrase_readings[word], DEFAULT_FREQ)

    # 补齐所有汉字（含罕用字），保证单字永远能打出来
    for ch, readings in char_readings.items():
        freq = jieba.get(ch, CHAR_FREQ)
        emit(ch, [readings[0]], freq)          # 首选读音无条件保留
        for alt in readings[1:MAX_READINGS_PER_WORD]:
            if alt in attested.get(ch, ()):
                emit(ch, [alt], freq)
            else:
                dropped_readings += 1
    if dropped_readings:
        print("      剔除 %d 个无词证的单字读音（如「而」的 neng）"
              % dropped_readings)

    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, "ycpy-dict.txt")
    lines.sort()
    with open(out_path, "w", encoding="utf-8") as fp:
        # 每行都要带换行符，否则头部会挤成一行（踩过一次）
        fp.write("".join(h + "\n" for h in DICT_HEADER))
        fp.write("\n".join(lines))
        fp.write("\n")
    print("      -> %s（%d 行, %.1f MB）"
          % (out_path, len(lines), os.path.getsize(out_path) / 1e6))

    print("[4/5] 生成联想搭配表")
    # 联想质量的关键：上下文和后继都必须是真词，否则会推出「丹非素」这类碎片。
    known = set(jieba)
    next_rows: dict[str, dict[str, int]] = {}
    for phrase in phrase_raw:
        n = len(phrase)
        if n < 3 or not is_han(phrase):
            continue
        # 二字上下文 -> 二字后继
        for i in range(2, n - 1):
            context, nxt = phrase[i - 2 : i], phrase[i : i + 2]
            if context not in known or nxt not in known:
                continue
            bucket = next_rows.setdefault(context, {})
            bucket[nxt] = bucket.get(nxt, 0) + 1

    out_rows = []
    for context, bucket in next_rows.items():
        ranked = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
        for nxt, score in ranked:
            if score < 1:
                continue
            out_rows.append("%s\t%s\t%d" % (context, nxt, score))
    out_rows.sort()

    next_path = os.path.join(DATA_DIR, "ycpy-next.txt")
    with open(next_path, "w", encoding="utf-8") as fp:
        fp.write("# ycpy 联想搭配表  上下文<TAB>后继词<TAB>权重\n")
        fp.write("# 由 tools/build_dict.py 自动生成，请勿手工编辑\n")
        fp.write("# 词条来自 pypinyin phrases_dict（MIT, mozillazg）；"
                 "详见 data/CREDITS.md\n")
        fp.write("\n".join(out_rows))
        fp.write("\n")
    print("      -> %s（%d 行, %.1f MB）"
          % (next_path, len(out_rows), os.path.getsize(next_path) / 1e6))

    print("[5/5] 生成字级二元组（联想的统计兜底）")
    # 只保留高频字的强搭配，体积和噪声都可控。
    char_freq: dict[str, int] = {}
    for phrase in phrase_raw:
        for ch in phrase:
            char_freq[ch] = char_freq.get(ch, 0) + 1
    top_chars = set(
        ch for ch, _ in sorted(char_freq.items(), key=lambda kv: -kv[1])[:TOP_CHARS]
    )

    bigram: dict[str, dict[str, int]] = {}
    for phrase in phrase_raw:
        for a, b in zip(phrase, phrase[1:]):
            if a in top_chars:
                bucket = bigram.setdefault(a, {})
                bucket[b] = bucket.get(b, 0) + 1

    bigram_rows = []
    for a, bucket in bigram.items():
        ranked = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_SUCC]
        for b, score in ranked:
            bigram_rows.append("%s\t%s\t%d" % (a, b, score))
    bigram_rows.sort()

    bpath = os.path.join(DATA_DIR, "ycpy-char-next.txt")
    with open(bpath, "w", encoding="utf-8") as fp:
        fp.write("# ycpy 字级搭配表  前字<TAB>后字<TAB>权重\n")
        fp.write("# 由 tools/build_dict.py 自动生成，请勿手工编辑\n")
        fp.write("# 字表来自 pypinyin phrases_dict（MIT, mozillazg）；"
                 "详见 data/CREDITS.md\n")
        fp.write("\n".join(bigram_rows))
        fp.write("\n")
    print("      -> %s（%d 行, %.1f MB）"
          % (bpath, len(bigram_rows), os.path.getsize(bpath) / 1e6))


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 ycpy 拼音词典")
    ap.add_argument("--offline", action="store_true", help="只用本地缓存，不联网")
    args = ap.parse_args()
    build(offline=args.offline)
    return 0


if __name__ == "__main__":
    sys.exit(main())