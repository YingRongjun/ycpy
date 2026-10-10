# 瀛澈拼音（ycpy）

跑在 IBus 上的拼音输入法引擎，纯 Python，**不需要编译、不需要 root、不装任何
第三方 pip 包**。

```
cxy         ->  程序员  陈兴瑜  常见于  产学研  磁效应 ...
nihao       ->  你好
```

- **全拼 + 首字母**两种输入，混着写也能命中前缀；
- **联想输入**：上屏一个词后按 `Ctrl+;` 给出后继词；
- **记忆用户高频词**：选过的词排到前面，落盘保存；
- **专业领域词库**：首选项里勾选 IT / 财经 / 法律 / 医学 / 饮食 / 汽车；
- 词典 50 万词条，引擎启动 0.05 秒。

## 安装

从 [Releases](https://github.com/YingRongjun/ycpy/releases) 下 deb：

```bash
sudo apt install ./ycpy_1.0.0_amd64.deb
ibus restart
```

装完不需要注销重登。然后 **设置 → 键盘 → 输入源 → ＋ → 其他语言 → 中文 →
瀛澈拼音**。

## 从源码编译

**仓库不含词典数据**——约 30 MB 的生成产物全部由脚本产出（内容是机械生成
的大表，进版本库只会让 clone 变慢、diff 全是噪声）。所以 clone 之后要先构建，
第一次需要联网：

```bash
git clone https://github.com/YingRongjun/ycpy.git
cd ycpy

python3 tools/build_dict.py          # 主词典 + 搭配表 + Unihan 分级
python3 tools/build_phrase_freq.py   # 口语整句词频
python3 tools/build_domain_dict.py   # 专业领域词表（可选）

sh tools/build_deb.sh                # 产出 build/deb/ycpy_1.0.0_amd64.deb
sudo apt install ./build/deb/ycpy_1.0.0_amd64.deb
ibus restart
```

三个脚本都支持 `--offline`（只用本地缓存重新生成），缓存默认在
`~/.cache/ycpy-build/`。

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