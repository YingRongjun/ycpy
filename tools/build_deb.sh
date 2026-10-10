#!/bin/sh
# 构建 ycpy 的 deb 安装包（无需 root）。
#
# 为什么要有这个
# --------------
# 项目原本只有一条用户级安装路线（写 ~/.config/environment.d +
# ~/.local/share/ibus/component + 自启项），有两个绕不开的问题：
#
#  1. **ibus 1.5.x 不读用户组件目录**（源码里被 `#if 0` 掉了），所以必须靠
#     `IBUS_COMPONENT_PATH` 硬喂，而那要等下次登录才生效。
#  2. **没有 root 就装不了「系统组件」**，`/usr/share/ibus/component` 写不进去。
#
# deb 包把组件装到系统目录，正好绕开这两点：装完立刻生效，GNOME 的输入法
# 列表里能直接看到，而且 `dpkg -r` 能干净卸载（用户级安装散落在三个目录里，
# 卸载容易漏）。
#
# 那条用户级路线连同它的 install.sh / uninstall.sh 已一并删除——除了上面
# 两条，它还只跑 build_dict.py，而运行期需要 13 个数据文件，缺 9 个
# （包括 ycpy-trad.txt，缺了繁体字会涌到候选前列）。
#
# 打包布局
# --------
#   /usr/lib/ycpy/                      引擎与词典（纯 Python，无编译）
#   /usr/share/ibus/component/ycpy.xml  ibus 组件描述（关键：系统目录才会被扫）
#   /usr/share/icons/hicolor/scalable/apps/ycpy.svg
#   /usr/bin/ycpy-engine                薄包装，转发到 /usr/lib/ycpy
#   /usr/bin/ycpy-preferences
#
# 注意 **不要**在 postinst 里往 `~/.config/environment.d` 写东西：
# deb 是给所有用户装的，而那个目录属于当前登录用户，root 代写会导致
# 属主错乱。系统组件模式下根本不需要它（ibus 默认就扫 /usr/share）。

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="ycpy"
VERSION="1.0.0"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
BUILD="${ROOT}/build/deb"
STAGE="${BUILD}/${PKG}_${VERSION}_${ARCH}"

die() { echo "错误：$*" >&2; exit 1; }
info() { echo "  $*"; }

echo "构建 ${PKG}_${VERSION}_${ARCH}.deb"

# ---------------------------------------------------------------- 清理
rm -rf "$BUILD"
mkdir -p "$STAGE/DEBIAN"

# ---------------------------------------------------------------- 拷文件
install -d "$STAGE/usr/lib/ycpy"
install -d "$STAGE/usr/lib/ycpy/data"
install -d "$STAGE/usr/share/ibus/component"
install -d "$STAGE/usr/share/icons/hicolor/scalable/apps"
install -d "$STAGE/usr/bin"
install -d "$STAGE/usr/share/doc/${PKG}"
install -d "$STAGE/usr/share/${PKG}"
install -d "$STAGE/usr/share/applications"
install -d "$STAGE/usr/share/metainfo"

# 引擎代码 + 词典数据
cp -a "$ROOT/ycpy" "$STAGE/usr/lib/ycpy/ycpy"
cp -a "$ROOT/data/ycpy-dict.txt" "$STAGE/usr/lib/ycpy/data/ycpy-dict.txt"
cp -a "$ROOT/data/ycpy-next.txt" "$STAGE/usr/lib/ycpy/data/ycpy-next.txt"
cp -a "$ROOT/data/ycpy-char-next.txt" "$STAGE/usr/lib/ycpy/data/ycpy-char-next.txt"
# 整句词频表（口语整句）。缺失时 dictionary.py 会静默降级，但
# 「你好吗」这类输入就拿不到正确候选了，所以是必需项。
[ -f "$ROOT/data/ycpy-phrase-freq.txt" ] || die "缺 data/ycpy-phrase-freq.txt，请先跑 python3 tools/build_phrase_freq.py"
cp -a "$ROOT/data/ycpy-phrase-freq.txt" "$STAGE/usr/lib/ycpy/data/ycpy-phrase-freq.txt"

# 专业领域词表（THUOCL, MIT）。全部打包而不是按需下载：专业词库要在
# 用户勾选时**立刻**可用，而运行期下载会卡住引擎、也要求联网。
# 未勾选时这些文件不占内存（DomainSet 只在勾选后才读）。
for _d in it caijing law medical food car; do
	_f="$ROOT/data/ycpy-domain-$_d.txt"
	[ -f "$_f" ] || die "缺 $_f，请先跑 python3 tools/build_domain_dict.py"
	cp -a "$_f" "$STAGE/usr/lib/ycpy/data/ycpy-domain-$_d.txt"
done
cp -a "$ROOT/data/ycpy-char-freq.txt" "$STAGE/usr/lib/ycpy/data/ycpy-char-freq.txt"
cp -a "$ROOT/data/ycpy-trad.txt" "$STAGE/usr/lib/ycpy/data/ycpy-trad.txt"
cp -a "$ROOT/data/Unihan_IRGSources.txt" "$STAGE/usr/lib/ycpy/data/Unihan_IRGSources.txt"
# 许可证必须随包分发：
#   * 本项目代码的 MIT（放 /usr/share/common-licenses/MIT 是 Debian 惯例，
#     但那是给系统包的；对自带数据的包，直接放 doc/ 更不容易搞错）
#   * 数据来源与义务（Unicode V3 明确要求「声明必须与数据一并出现」）
cp -a "$ROOT/LICENSE" "$STAGE/usr/share/doc/${PKG}/copyright"
cp -a "$ROOT/data/CREDITS.md" "$STAGE/usr/share/doc/${PKG}/CREDITS.md"
cp -a "$ROOT/data/LICENSE-hanziDB.txt" "$STAGE/usr/share/doc/${PKG}/LICENSE-hanziDB.txt"

# 别带 __pycache__（否则包随 python 版本变脏）
find "$STAGE/usr/lib/ycpy" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE/usr/lib/ycpy" -name '*.pyc' -delete

install -m 0644 "$ROOT/data/ycpy.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/ycpy.svg"

# GNOME 输入源设置里那个齿轮按钮，是靠找 `ibus-setup-<引擎名>.desktop`
# 来定位首选项程序的（gnome-control-center 硬编码这个命名）。
# deb 里漏掉它的话，装完点齿轮会「找不到首选项程序」。
# 名字必须跟组件 XML 里的 <engine><name> 一致，也就是 ycpy。
install -m 0644 "$ROOT/data/ibus-setup-ycpy.desktop" \
    "$STAGE/usr/share/applications/ibus-setup-ycpy.desktop"

# AppStream 元数据：Ubuntu「应用中心」显示名称/描述/**许可证**靠它。
# 缺了它 snap-store 就退回解析 control，于是许可显示 unknown、
# 中文描述显示成问号（它的 deb 解析器不按 UTF-8 读 Description）。
# 必须校验 XML 合法性——非法的话 AppStream 会**静默忽略**整个文件，
# 症状跟没放一样。
python3 -c "import xml.etree.ElementTree as E, sys; E.parse(sys.argv[1])" \
    "$ROOT/data/ycpy.metainfo.xml" \
    || die "data/ycpy.metainfo.xml 不是合法 XML，软件中心会静默忽略它"
install -m 0644 "$ROOT/data/ycpy.metainfo.xml" \
    "$STAGE/usr/share/metainfo/ycpy.metainfo.xml"

# ---------------------------------------------------------------- 组件 XML
# 绝对路径写死成 /usr/bin/...：ibus-daemon 是在 daemon 的环境里起引擎的，
# 相对路径或依赖 PATH 都不可靠。
sed -e "s#__EXEC__#/usr/bin/ycpy-engine#g" \
    -e "s#__PREF__#/usr/bin/ycpy-preferences#g" \
    -e "s#__DOC__#/usr/share/doc/${PKG}#g" \
    "$ROOT/data/ycpy.xml.in" > "$STAGE/usr/share/ibus/component/ycpy.xml"

# 必须校验。ibus 读到非法组件文件是**静默跳过**的，症状是
# 「GNOME 输入法列表里没有 ycpy」且没有任何报错——历史上踩过一次，
# 原因是 XML 注释里写了连续两个连字符。
python3 -c "import xml.etree.ElementTree as E,sys; E.parse(sys.argv[1])" \
    "$STAGE/usr/share/ibus/component/ycpy.xml" \
    || die "生成的组件描述不是合法 XML，ibus-daemon 会静默忽略它"

# 校验引擎自身能导入（漏文件、语法错在这里就该暴露）
PYTHONPATH="$STAGE/usr/lib/ycpy" python3 -c "
import ycpy.engine, ycpy.composer, ycpy.preferences, ycpy.charscore
" || die "引擎模块导入失败，检查是否有文件漏打包"

# ---------------------------------------------------------------- 启动脚本
# 薄包装：只负责把 PYTHONPATH 指到 /usr/lib/ycpy。
# 不能直接软链 ycpy-engine——它靠「脚本所在目录的上一级」推导项目根，
# 装到 /usr/bin 后那个推导会指向 /usr，路径就错了。
# 引擎入口是 ``python3 -m ycpy``（包级 __main__.py），**不是** ``-m ycpy.engine``。
# 写成后者会直接跑掉 main()、什么都不干就退出——因为 engine.py 只是模块，
# 真正的入口在 ycpy/__main__.py 里。踩过一次这个坑。
for name in ycpy-engine ycpy-preferences; do
    target="${name#ycpy-}"
    if [ "$target" = "engine" ]; then
        module="ycpy"                 # 引擎走包级 __main__
    else
        module="ycpy.$target"
    fi
    cat > "$STAGE/usr/bin/$name" <<EOF
#!/bin/sh
# ycpy $target —— 由 ${PKG} ${VERSION} 安装，请勿手工修改。
set -e
PYTHONPATH=/usr/lib/ycpy\${PYTHONPATH:+:\$PYTHONPATH}
export PYTHONPATH
exec /usr/bin/python3 -B -m $module "\$@"
EOF
    chmod 0755 "$STAGE/usr/bin/$name"
done

# ---------------------------------------------------------------- DEBIAN
install -m 0644 "$ROOT/debian/control" "$STAGE/DEBIAN/control"
install -m 0755 "$ROOT/debian/postinst" "$STAGE/DEBIAN/postinst"
install -m 0644 "$ROOT/debian/postrm" "$STAGE/DEBIAN/postrm"
if [ -f "$ROOT/debian/copyright" ]; then
    install -m 0644 "$ROOT/debian/copyright" "$STAGE/usr/share/doc/${PKG}/debian-copyright"
fi
if [ -f "$ROOT/debian/conffiles" ]; then
    install -m 0644 "$ROOT/debian/conffiles" "$STAGE/DEBIAN/conffiles"
fi
if [ -f "$ROOT/CHANGELOG.md" ]; then
    # 直接写成 .gz 名字，install 之后再 gzip -9 —— 反过来（先装成无后缀再压）
    # 容易出现「已经有 .gz 后缀」然后 gzip 拒绝压缩的坑。
    cp "$ROOT/CHANGELOG.md" "$STAGE/usr/share/doc/${PKG}/changelog.Debian"
    gzip -9n "$STAGE/usr/share/doc/${PKG}/changelog.Debian"
fi

# 权限收敛。顺序很重要：**先把所有文件压成 0644，再把该可执行的挑出来**。
# 反过来做（先 0755 再统一 0644）会把启动脚本的可执行位抹掉——
# /usr/bin/ycpy-engine 不可执行的话，ibus 报的是「找不到可执行的组件」，
# 不会说权限不对，极难排查。是 `dpkg --root` 真装一遍才发现的。
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod 0644 {} +
# 启动脚本与 dpkg 维护脚本必须可执行
chmod 0755 "$STAGE/usr/bin/ycpy-engine" "$STAGE/usr/bin/ycpy-preferences"
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# 属主：deb 规范要求 root。这里可能没 root，那就保留当前用户并在下面提示。
if [ "$(id -u)" = "0" ]; then
    chown -R 0:0 "$STAGE"
else
    chown -R "$(id -u):$(id -g)" "$STAGE" 2>/dev/null || true
    info "注意：当前不是 root，dpkg 会警告属主不对。"
    info "      本地测试无妨；要发布请在打包机（root 或 fakeroot）上重建。"
fi

# ---------------------------------------------------------------- 生成
OUT="${BUILD}/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT"

echo
info "生成: $OUT"
ls -la --time-style=+%H:%M "$OUT" | awk '{printf "  大小 %.1f MB  %s\n", $5/1048576, $NF}'
echo
echo "包内内容概览:"
dpkg-deb --contents "$OUT" | awk '{print "  "$6}' | grep -v '/$' | head -20
echo "  ...（完整列表：dpkg-deb -c $OUT）"
echo
echo "安装：sudo dpkg -i $OUT"
echo "卸载：sudo dpkg -r ${PKG}"