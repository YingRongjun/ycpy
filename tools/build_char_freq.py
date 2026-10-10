"""把 Jun Da 字频表转成 ycpy 的紧凑数据文件。

源：ruddfawcett/hanziDB.csv（MIT），基于 Jun Da 的
《现代汉语字符频次表》（Modern Chinese Character Frequency List）。
这是语料统计出来的**真实字频排名**，不是《通用规范汉字表》那种
「字形规范化分级」。

输出格式（``data/ycpy-char-freq.txt``）::

    # 头部注释（来源 + 许可）
    <字><TAB><频次名次>

名次越小越常用。只保留名次 <= 9000 的（即全部收录字）。
运行期按 1/rank 的对数映射成分数，避免长尾把量级拉爆。
"""
import json
import os
import sys
import urllib.request

API = "https://api.github.com/repos/ruddfawcett/hanziDB.csv/contents/"

# 路径按脚本位置算，不能写死。
#
# 开源前修的 bug：原先 OUT 写的是 ``/opt/src/input-method/data/...``，
# 那是作者本机的绝对路径——别人 clone 下来一跑就把字频表写到**自己家目录
# 下别人的文件夹**，或者干脆因为目录不存在而失败。这类路径在公开仓库里
# 尤其扎眼（同时也是隐私）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
OUT = os.path.join(_ROOT, "data", "ycpy-char-freq.txt")
RAW = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "ycpy-build", "hanzi_db.csv")

HEADER = """\
# ycpy 字频表  字<TAB>频次名次（越小越常用）
# 由 tools/build_char_freq.py 自动生成，请勿手工编辑
#
# 数据来源
#   项目：  https://github.com/ruddfawcett/hanziDB.csv
#   内容：  9900 个简体字的 frequency_rank（按现代汉语语料统计的频次名次）
#   底本：  Jun Da, "A Frequency-Count Study of Chinese" (Modern Chinese
#           Character Frequency List)，基于真实语料的统计，不是人工分级
#   许可：  MIT License, Copyright (c) 2017 Rudd Fawcett
#           （原始 LICENSE 全文见 data/CREDITS.md）
#
# 为什么不用 Unihan 的 kIRG_TSource（通用规范汉字表分级）
#   那个分级衡量的是**字形规范化程度**，不是使用频率：它把
#   「将/会/对/这/个」这些超高频字划到 T3（稀缺字），把「者/正/遮/框」
#   划到 T1（常用字）。实测 `zhe` 的 299 个候选里「这」排第 140 位。
"""


def fetch():
    """从 GitHub API 取原始 CSV（github.com 被墙，api.github.com 可用）。"""
    import base64

    req = urllib.request.Request(
        API + "hanzi_db.csv",
        headers={"User-Agent": "curl/8", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    return base64.b64decode(payload["content"]).decode("utf-8")


def parse(raw):
    """CSV -> [(字, 名次)]。首行是表头。"""
    out = []
    for i, line in enumerate(raw.splitlines()):
        if i == 0 or not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        ch, rank = parts[1], parts[0]
        try:
            rank = int(rank)
        except ValueError:
            continue
        if len(ch) == 1:
            out.append((ch, rank))
    return out


def main():
    try:
        raw = open(RAW, encoding="utf-8").read()
        print("  使用已下载的 %s" % RAW)
    except OSError:
        print("  从 GitHub API 下载…")
        raw = fetch()

    entries = parse(raw)
    if not entries:
        raise SystemExit("解析不到任何字频条目")

    # 同一字出现多次时保留最小名次
    best = {}
    for ch, rank in entries:
        if ch not in best or rank < best[ch]:
            best[ch] = rank

    with open(OUT, "w", encoding="utf-8") as fp:
        fp.write(HEADER)
        for ch in sorted(best, key=lambda c: best[c]):
            fp.write("%s\t%d\n" % (ch, best[ch]))

    print("  写出 %s：%d 个字" % (OUT, len(best)))

    # 抽查用户报的那些字
    print("  抽查：", end=" ")
    for ch in "将会对这个":
        print("%s=%s " % (ch, best.get(ch, "?")), end="")
    print()


if __name__ == "__main__":
    main()