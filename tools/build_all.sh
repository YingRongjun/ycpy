#!/bin/sh
# 生成全部词典数据（tools/build_all.py 的 shell 包装，便于 `sh tools/build_all.sh` 调用）。
#
# 为什么需要这个：运行期需要 **13** 个数据文件，由 **5** 个脚本产出，其中
# build_dict.py 只管4 个、另外两个（build_trad.py / build_char_freq.py）
# 并不被它调用。让人手动按顺序跑5 条命令，既容易漏，漏了也只在
# build_deb.sh 那一步才报「缺 xxx.txt」——那时已经跑了很久了。
set -e
cd "$(dirname "$0")/.."
exec python3 tools/build_all.py "$@"