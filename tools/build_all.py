#!/usr/bin/env python3
"""一次生成全部词典数据。

运行期需要 **13** 个数据文件，由 **5** 个脚本产出::

    build_dict.py         -> ycpy-dict.txt / ycpy-next.txt / ycpy-char-next.txt
                               / Unihan_IRGSources.txt
    build_trad.py         -> ycpy-trad.txt
    build_char_freq.py    -> ycpy-char-freq.txt
    build_phrase_freq.py  -> ycpy-phrase-freq.txt
    build_domain_dict.py  -> ycpy-domain-{it,caijing,law,medical,food,car}.txt

注意 ``build_dict.py`` **只**产出前4 个——``build_trad.py`` 和
``build_char_freq.py`` 不被它调用。所以「跑三条命令」的旧说法是错的，
少了任何一个都会在 ``tools/build_deb.sh`` 那一步才报「缺 xxx.txt」，
而那时已经跑了很久。

用法::

    python3 tools/build_all.py              # 全都生成
    python3 tools/build_all.py --offline    # 只用本地缓存（不联网）
    python3 tools/build_all.py --skip-domain  # 跳过专业词库（体积大、耗时）

已存在且非空的文件默认跳过（``--force`` 可强制重建）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

#: (脚本, 产出的数据文件, 说明)
STEPS = [
    ("build_dict.py", ["ycpy-dict.txt", "ycpy-next.txt",
                       "ycpy-char-next.txt", "Unihan_IRGSources.txt"],
     "主词典 + 搭配表 + Unihan 分级"),
    ("build_trad.py", ["ycpy-trad.txt"],
     "繁体字表（缺了繁体字会涌到候选前列，不是可选功能）"),
    ("build_char_freq.py", ["ycpy-char-freq.txt"],
     "汉字频次名次（单字排序的主信号）"),
    ("build_phrase_freq.py", ["ycpy-phrase-freq.txt"],
     "口语整句词频（「你好吗」这类输入靠它）"),
    ("build_domain_dict.py", ["ycpy-domain-it.txt", "ycpy-domain-caijing.txt",
                              "ycpy-domain-law.txt", "ycpy-domain-medical.txt",
                              "ycpy-domain-food.txt", "ycpy-domain-car.txt"],
     "专业领域词表 6 个（只在勾选后加载，不勾选不占内存）"),
]

DOMAIN_SCRIPTS = {"build_domain_dict.py"}


def already_built(files: list[str]) -> bool:
    """产物是否都已存在且非空。"""
    return all(os.path.exists(os.path.join(DATA, f))
               and os.path.getsize(os.path.join(DATA, f)) > 0
               for f in files)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成全部词典数据")
    ap.add_argument("--offline", action="store_true",
                    help="只用本地缓存，不联网")
    ap.add_argument("--force", action="store_true",
                    help="已存在的产物也重建")
    ap.add_argument("--skip-domain", action="store_true",
                    help="跳过专业领域词库（6 个文件，耗时最长）")
    args = ap.parse_args()

    print("生成词典数据（第一次需要联网，约几分钟）")
    failed = []
    for script, files, note in STEPS:
        if args.skip_domain and script in DOMAIN_SCRIPTS:
            print("\n-- 跳过 %s（%s）" % (script, note))
            print("   专业词库会在打包时报缺文件，或用 --skip-domain 打包")
            continue
        if not args.force and already_built(files):
            print("\n-- 跳过 %s（产物已存在，加 --force 可重建）" % script)
            continue

        cmd = [sys.executable, os.path.join(ROOT, "tools", script)]
        if args.offline:
            cmd.append("--offline")
        print("\n== %s  (%s)" % (script, note))
        started = time.monotonic()
        result = subprocess.run(cmd, cwd=ROOT)
        elapsed = time.monotonic() - started
        if result.returncode != 0:
            # 不立刻退出：其余步骤可能已经成功，让用户一次看全所有问题。
            print("   ✗ 失败（退出码 %d）" % result.returncode)
            failed.append(script)
            continue

        missing = [f for f in files
                   if not os.path.exists(os.path.join(DATA, f))]
        if missing:
            print("   ✗ 声称成功但产物缺失：%s" % ", ".join(missing))
            failed.append(script)
        else:
            size = sum(os.path.getsize(os.path.join(DATA, f)) for f in files)
            print("   ✓ %d 个文件，%.1f MB，%.0f 秒"
                  % (len(files), size / 1e6, elapsed))

    if failed:
        print("\n失败：%s" % ", ".join(failed))
        print("单独重跑某一个即可，例如：python3 tools/%s" % failed[0])
        return 1

    print("\n全部完成。接着：sh tools/build_deb.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())