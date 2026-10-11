# 瀛澈拼音（ycpy）

跑在 IBus 上的拼音输入法引擎，纯 Python，**不需要编译、不需要 root、
不装任何第三方 pip 包**。

```
cxy         ->  程序员  陈兴瑜  常见于  产学研  磁效应 ...
nihao       ->  你好
```

- **全拼 + 首字母**两种输入，混着写也能命中前缀；
- **联想输入**（`Ctrl+;`）与**用户词频记忆**；
- **专业领域词库**：勾选 IT / 财经 / 法律 / 医学 / 饮食 / 汽车；
- 词典 50 万词条，引擎启动 0.05 秒。

## 安装

从 [Releases](https://github.com/YingRongjun/ycpy/releases) 下 deb：

```bash
sudo apt install ./ycpy_1.0.0_amd64.deb
ibus restart
```

装完不需要注销重登。**还要把输入法加进列表**（deb 只负责让 ibus 认得它，
不会替你改 GNOME 的输入源设置）：

**设置 → 键盘 → 输入源 → ＋ → 其他语言 → 中文 → 瀛澈拼音**

> **加了之后列表里又没了？** 这是 `ibus restart` 的时序竞争：daemon 重启的
> 那一刻引擎还没就绪，GNOME 重建列表时把这一项丢了。重新添加即可，或者：
>
> ```bash
> gsettings set org.gnome.desktop.input-sources mru-sources \
>   "[('ibus', 'ycpy'), ('ibus', 'libpinyin'), ('xkb', 'cn')]"
> gsettings set org.gnome.desktop.input-sources sources \
>   "[('ibus', 'ycpy'), ('xkb', 'cn'), ('ibus', 'libpinyin')]"
> ```
>
> **两个键都要改**：GNOME 有「当前启用」（`sources`）和「最近使用」
> （`mru-sources`）两份配置，只改前一个会被后者**回填**——这就是「删了又
> 出现」。卸载后残留也是同一个机制（GNOME 只读自己的 dconf，不知道包卸没卸）。
>
> 仍然没有就按顺序查四点——**症状一样，成因至少有四个**：组件 XML 合法性
> → `ibus list-engine | grep ycpy` → `gsettings get … sources` → 删
> `~/.cache/ibus/bus/registry` 再重启。

## 从源码编译

词典数据（约 30 MB）**已在仓库里**，clone 下来直接打包：

```bash
git clone https://github.com/YingRongjun/ycpy.git
cd ycpy

sh tools/build_deb.sh                # 产出 build/deb/ycpy_1.0.0_amd64.deb
sudo apt install ./build/deb/ycpy_1.0.0_amd64.deb
ibus restart
```

### 重新生成词典（可选）

只有改了 `tools/` 下的生成脚本、或想验证它们时才需要，要联网：

```bash
python3 tools/build_all.py           # 生成全部 13 个数据文件

python3 tools/build_all.py --offline       # 只用本地缓存，不联网
python3 tools/build_all.py --skip-domain   # 跳过专业词库（最耗时）
python3 tools/build_all.py --force         # 已存在的也重建
```

它实际调用三个脚本（`build_trad.py` 和 `build_char_freq.py` 不被
`build_dict.py` 调用，所以一共五个）：

```bash
python3 tools/build_dict.py          # 主词典 + 搭配表 + Unihan 分级
python3 tools/build_phrase_freq.py   # 口语整句词频
python3 tools/build_domain_dict.py   # 专业领域词表
```

缓存默认在 `~/.cache/ycpy-build/`。

## 测试

```bash
python3 -B -m unittest discover -s tests   # 213 个离线单测，不需要词典数据
python3 tools/e2e_test.py                 # 走真实 D-Bus 链路
./tools/e2e_isolated.sh                   # 私有 daemon 压测，不干扰当前会话
```

`e2e_test.py` 直接对引擎发按键事件，走真实 daemon，会和**你正在进行的桌面
输入共用同一条总线**——跑的时候别同时敲键盘。

## 授权

GPL-3.0-or-later。词典全部由 `tools/` 下的脚本从公开数据源构建，不含手工编写
内容。整句词频取自 rime-frost 的 `知频.txt`（GPL-3.0），这一项决定了整体采用
GPL-3.0 而非 MIT。

逐个数据源的授权与义务见 **[data/CREDITS.md](data/CREDITS.md)**（MIT 与
Unicode License V3 要求随分发提供，该文件必须随包）。