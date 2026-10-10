"""从 Unihan_Variants.txt 提取繁体字表，生成 ``data/ycpy-trad.txt``。

为什么需要它
------------
语料字频表（``ycpy-char-freq.txt``）**只有简体字**，所有繁体写法都查不到
名次，只能退回 T 值兜底分。而兜底分是个常数（原来 320），于是繁体字会
集体涌到候选前列——实测 ``jiang`` 的头 8 名是 槳/漿/醬/彊/強/獎/將/韁，
简体「将」被挤到第 22 位；更糟的是简体常用字本身得分可能比繁体还低
（江 309 < 320），于是繁体字插到了简体常用字**前面**。

单靠「不在简体字频表里」来识别繁体是不行的：那样会把大量生僻简体字
（醤/櫤/㧑这类）也当成繁体。必须用 Unicode 官方的繁简对应关系。

Unihan 的繁简关系分散在两个字段里，**只用其一会漏**：

* ``kSimplifiedVariant``：會→会、對→对、個→个、這→这…（键是繁体）
* ``kTraditionalVariant``：将→將（键是简体，值是繁体）

两边的方向是反的，只有合并才完整。还要**排除自映射**（会→会、了→了），
否则 会/对/个/这/了 这些简体字会被误判成繁体。

数据源：Unicode 官方 Unihan.zip（Unicode License V3）
"""
import io
import os
import sys
import urllib.request
import zipfile

UNIHAN_URL = "https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "ycpy-trad.txt")

HEADER = """\
# ycpy 繁体字表  每行一个繁体字
# 由 tools/build_trad.py 自动生成，请勿手工编辑
#
# 数据来源
#   https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip
#   文件：Unihan_Variants.txt 的 kSimplifiedVariant（7291 条，键为繁体）
#         与 kTraditionalVariant（6840 条，值为繁体）两个字段合并
#   许可：Unicode License V3（义务见 data/CREDITS.md 第 3 节）
#
# 合并的原因：两个字段方向相反，只用其一会漏掉一半
#   kSimplifiedVariant： 會->会  對->对  個->个  這->这
#   kTraditionalVariant：将->將
#
# 另外必须排除自映射（会->会、了->了），否则这些简体字会被误判成繁体。
"""


def fetch_variants():
    """取 Unihan.zip，解出 Unihan_Variants.txt 的内容。"""
    req = urllib.request.Request(UNIHAN_URL, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=180) as r:
        zf = zipfile.ZipFile(io.BytesIO(r.read()))
        return zf.read("Unihan_Variants.txt").decode("utf-8")


def parse_field(raw, field):
    """抽出某个字段的 codepoint -> codepoint 映射。"""
    out = {}
    for line in raw.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3 or parts[1] != field:
            continue
        try:
            src = int(parts[0][2:], 16)
            dst = int(parts[2].split()[0][2:], 16)
        except ValueError:      # 有些值是 𫝈 这种补充平面字，int() 会失败
            continue
        out[src] = dst
    return out


def collect(raw):
    """合并两个字段，得到全部繁体字的码位集合。"""
    simp = parse_field(raw, "kSimplifiedVariant")
    trad = parse_field(raw, "kTraditionalVariant")
    # 键是繁体，但自映射（会->会）说明它其实是简体，要排除
    codes = {src for src, dst in simp.items() if src != dst}
    # 值是繁体；已经在 simp 里当过键的就不重复加
    codes |= {dst for dst in trad.values() if dst not in simp}
    return codes


def main():
    # 一律直接从 Unihan.zip 取——那个 zip 在构建缓存里（build_dict.py 也会
    # 下载同一份）。原先这里先找 data/Unihan_Variants.txt，找不到才下载，
    # 于是那个中间文件成了事实上的必需项；开源时它已删除（死重量）。
    # 那个 try/except NameError 的分支更糟：常量被删掉后每次跑都会先抛
    # NameError 再走下载，错误被静默吞掉，看不出是配置问题还是逻辑问题。
    cache = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
        "ycpy-build", "Unihan.zip")
    if os.path.exists(cache):
        print("  使用已下载的 %s" % cache)
        with zipfile.ZipFile(cache) as zf:
            raw = zf.read("Unihan_Variants.txt").decode("utf-8")
    else:
        print("  从 unicode.org 下载 Unihan.zip…")
        raw = fetch_variants()

    codes = collect(raw)
    if not codes:
        raise SystemExit("没解析出任何繁体字")

    with open(OUT, "w", encoding="utf-8") as fp:
        fp.write(HEADER)
        for cp in sorted(codes):
            fp.write("%c\n" % cp)

    print("  写出 %s：%d 个繁体字" % (OUT, len(codes)))

    # 抽查：高频繁体必须在内，简体绝不能在内
    trad = {chr(c) for c in codes}
    sample = "將會對個這後們來時開關體"
    missing = [c for c in sample if c not in trad]
    print("  高频繁体抽查：%s" % ("全部识别 ✅" if not missing else "漏 %s ❌" % missing))
    false_pos = [c for c in "将会对个这们的了在人有我他" if c in trad]
    print("  简体误判检查：%s" % ("无误判 ✅" if not false_pos else "误判 %s ❌" % false_pos))


# 曾经这里还留着一个 data/Unihan_Variants.txt 的路径常量，那个中间文件是
# 本脚本解 Unihan.zip 时的副产物，**代码从不需要读它**。开源前删掉了（727 KB
# 的死重量），常量一并移除——留着会让后来的人以为运行期要读它。
if __name__ == "__main__":
    main()