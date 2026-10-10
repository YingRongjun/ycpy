# 数据来源与授权

本项目**不包含任何自行编写的词库**——全部词典都是用 `tools/build_dict.py`
从下列公开数据源构建的。这份文档记录每个数据源的授权，以及由此产生的
义务。核查时间：2026-10。

## 一览

| 数据 | 用途 | 来源 | 授权 | 义务 |
|---|---|---|---|---|
| jieba `dict.txt` | 词条 + **词频** | [fxsjy/jieba](https://github.com/fxsjy/jieba) 0.42.1（PyPI sdist）| MIT | 保留版权声明 |
| pypinyin `pinyin_dict.json` | 单字读音（4.2 万字）| [mozillazg/python-pinyin](https://github.com/mozillazg/python-pinyin) 0.55.0 | MIT | 保留版权声明 |
| pypinyin `phrases_dict.json` | 词组读音（4.7 万词）| 同上（源自 phrase-pinyin-data）| MIT | 保留版权声明 |
| **Jun Da 字频表** | **汉字频次名次（单字排序主信号）** | [ruddfawcett/hanziDB.csv](https://github.com/ruddfawcett/hanziDB.csv)（`hanzi_db.csv`）| MIT | 保留版权声明 |
| THUOCL | 专业领域词表（6 个领域）| [thunlp/THUOCL](https://github.com/thunlp/THUOCL) | MIT | 保留版权声明 |
| **rime-frost 知频表** | **整句词频（口语整句）** | [gaboolic/rime-frost](https://github.com/gaboolic/rime-frost) `others/知频.txt` | **GPL-3.0** | **保留声明；本项目因此改用 GPL-3.0** |
| Unicode Unihan | 汉字**分档**（字频的辅助微调）| [unicode.org](https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip) | Unicode License V3 | **保留版权与许可声明** |
| 系统字体元数据 | 判断哪些字能显示 | 运行期 `fc-list` | 不分发任何字体 | 无 |

运行期**不下载、不分发**任何数据；``tools/build_dict.py`、
`tools/build_phrase_freq.py`、`tools/build_domain_dict.py` 只在手动执行时联网。

---

## 1. jieba —— 词条与词频

- 地址：<https://github.com/fxsjy/jieba>
- 版本：0.42.1（经 PyPI sdist 获取，`tar` 缓存于 `~/.cache/ycpy-build/`）
- 授权：**MIT**（`PKG-INFO` 的 `License: MIT`、README「MIT 授权协议」，
  GitHub API 亦返回 `spdx_id: MIT`）
- 作者：Sun, Junyi
- 我使用的内容：`jieba/dict.txt` 的**词条**与**词频**两列（349,046 条）。
  词性标注列未使用。

**数据本身的来源未明确声明。** jieba 的 README 与 issue 里都没有说明
dict.txt 的词频由哪个语料统计得出（前维护者在 issue #953 里提到「训练集
是用其他分词工具给小说和社科类语料分词获得的」，但这是个人回顾、非正式
文档）。因此词频数据的授权只能依赖 jieba 仓库整体适用的 MIT 声明。

风险评估：**低**。词条与词频属于事实性数据（不构成原创表达），MIT 覆盖
整个仓库分发物。MIT 唯一实质义务是保留版权声明，已在本文件记录。

### 本项目对 pypinyin 多音字数据做的过滤

pypinyin 的 `pinyin_dict.json` 收录了 6,526 个「多音」单字，其中一部分是
词典讹误而非真正的多音字——最典型的是「而」被标成 `ér,néng`，而「而」
读 néng 在现实中根本不存在。这类讹误会让字的首字母索引变成假的
（「而」的 initials 成了 `n`，敲 `ne` 就会命中它）。

因此 `tools/build_dict.py` 增加了「有词证」过滤：一个读音只有在**真实词**
里出现过才保留，判据取自各词的**首选读音**（若用展开后的读音去统计，
我们**自己生成的伪词**会反过来证明伪读音合法，循环论证，垃圾永远删不掉）。
共剔除 7,437 个无词证的单字读音。

这是对**数据内容的过滤**，不改变 pypinyin 本身的授权归属——MIT 仍然适用。

## 2. pypinyin —— 拼音数据

- 地址：<https://github.com/mozillazg/python-pinyin>
- 版本：0.55.0
- 授权：**MIT**（`LICENSE.txt`，Copyright (c) 2016 mozillazg、闲耘）
- 我使用的内容：`pypinyin/pinyin_dict.json`（单字读音）、
  `pypinyin/phrases_dict.json`（词组读音）

**数据链路已追到底**，上游两个整理仓库也都是 MIT：

- <https://github.com/mozillazg/pinyin-data>（MIT）—— 单字读音，
  本身又取自 Unihan 的 `kTGHZ2013`（《通用规范汉字字典》）、
  `kHanyuPinyin`（《漢語大字典》）、`kXHC1983`（《现代汉语词典》）、
  `kHanyuPinlu`（《現代漢語頻率詞典》）
- <https://github.com/mozillazg/phrase-pinyin-data>（MIT）—— 词组读音

### 关键：CC-CEDICT 没有混进来

phrase-pinyin-data 另收了 [CC-CEDICT](https://cc-cedict.org/) 的数据。
**CC-CEDICT 是 CC BY-SA 4.0**，一旦混入就有 share-alike（衍生作品须同协议
开放）和署名义务，会实质影响本项目的授权。已确认**没有混入**：

- pypinyin 的 `Makefile` 里 `gen_phrases_dict` 的输入是
  `phrase-pinyin-data/pinyin.txt`；
- 该仓库的 `Makefile` 显示 `pinyin.txt` = 原始 `pinyin.txt` + `overwrite.txt`，
  **不含** `cc_cedict.txt`；
- `cc_cedict.txt` 只参与 `large_pinyin.txt` 的合成，而 pypinyin 不使用它；
- 实测两边词条数**完全一致**（各 47,111 条），且 CC-CEDICT 特征词
  「软件」在 `pinyin.txt` 中不存在——进一步印证同源且未含 CC-CEDICT。

若将来改用 `large_pinyin.txt`，本节结论必须重新评估。

另：phrase-pinyin-data 另收了汉典网（zdic.net）的数据，但同样只用于
`large_pinyin.txt`，未进入本项目。

## 3. Unicode Unihan —— 汉字分档（**仅作辅助**）

- 地址：<https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip>
- 我使用的内容：
  - `Unihan_IRGSources.txt` 中的 `kIRG_TSource` 字段，即《通用规范汉字表》
    分级（**辅助**信号，不是单字排序的主信号，见第 4 节）；
  - `Unihan_Variants.txt` 中的 `kSimplifiedVariant` 与
    `kTraditionalVariant` 两个字段，合成 `data/ycpy-trad.txt`（繁体字表）。
    两个字段方向相反：前者键是繁体（會→会），后者键是简体、值是繁体
    （将→將），只用其一会漏掉一半；还要排除自映射（会→会），否则
    会/对/个/这/了 这些简体字会被误判成繁体。
  - 其余字段一概未用。
- 授权：**Unicode License V3**
- **地位**：分级是辅助信号；繁简对应则是繁体降级的**必要**依据。

本副本由 `tools/build_dict.py` 与 `tools/build_trad.py` 自 `Unihan.zip`
解包而成（`data/Unihan_IRGSources.txt`、`data/Unihan_Variants.txt`）。

Unicode License V3 的实质义务：**复制或分发数据文件时，版权与许可声明
必须一并出现**（"provided that either (a) this copyright and permission
notice appear with all copies of the Data Files, or (b) … appear in
associated Documentation"）。

本文件即为该声明的载体。若把 `data/Unihan_IRGSources.txt` 单独分发，
必须把本节内容随附。

**为什么它不能当主信号**：这个分级衡量的是**字形规范化程度**，不是
**使用频率**。该表按「是否收录为规范字」分级，而「将/会/对/这/个」的规范
归属另有来源，于是全被划到 T3（稀缺字），而「者/正/遮/框」这些远没那么
常用的字反倒拿到 T1。实测 `zhe` 的 299 个候选里「这」排第 140 位，
`ge` 的 159 个里「个」排第 98 位。现在只拿它做很轻的乘性微调
（0.94~1.00），主信号交给第 4 节的语料字频。

注意：本项目**不下载也不分发任何字体**。`ycpy/fontcov.py` 只在运行期
执行 `fc-list` 读取系统已装字体的覆盖信息，结果缓存在
`~/.cache/py-pinyin-ibus/font-cov.txt`，那只是一串码位区间，不含字体数据，
不构成字体分发。

---

## 4. Jun Da 字频表 —— 汉字频次名次（**单字排序主信号**）

- 项目：<https://github.com/ruddfawcett/hanziDB.csv>
- 文件：`hanzi_db.csv`（546 KB，9900 个简体字）
- 授权：**MIT License, Copyright (c) 2017 Rudd Fawcett**
  （已取得 `LICENSE` 原文，随本仓库保存于
  `data/LICENSE-hanziDB.txt`）
- 底本：**Jun Da,《现代汉语字符频次表》**（Modern Chinese Character
  Frequency List）——基于真实语料的频次统计，**不是人工分级**

### 我使用的内容

只取 `frequency_rank` 一列（以及校验用的 `character` 列），即每个简体字在
语料中的频次名次（越小越常用）。`definition`、`radical`、`stroke_count`、
`hsk_level`、`general_standard_num` 等列一概未用。

名次对照（前 40）：

```
的=1  一=2  是=3  不=4  了=5  在=6  有=8  人=7  他=10  这=11
个=12 们=13  中=14 为=18 说=24 会=29 你=32 对=33 年=45 …
框=2144  狂=1147  矿=1385
```

MIT 的义务是**保留版权与许可声明**：`data/ycpy-char-freq.txt` 的文件头
已写入来源与 MIT 声明，本节与 `data/LICENSE-hanziDB.txt` 是它的完整载体。

### 为什么换用它

原来的单字排序只用《通用规范汉字表》分级（见第 3 节）。用户报「将会对
这个这些高频字为什么会在很后面」——根因就是那个分级不是频率：它把
「将/会/对/这/个」划到 T3、把「者/正/遮/框」划到 T1，于是高频字被系统性
打死；而 T1 内部所有字分数**完全相同**，在真正需要区分的地方
（「者」对「遮」）一点作用都没有，只能退回 jieba 词频——而 jieba 的单字
freq 统计的是「该字**独立成词**的次数」，「遮」几乎从不独立成词（893），
于是生僻字被排到高频字前面。

换成真实语料频次后，「将/会/对/这/个」全部排到对应拼音的**首选**
（原来分别是第 20/66/24/140/98 位）。

### 许可调研中排除的源

- `techkang/chinese_character_frequence`（同样基于维基百科语料）：
  GitHub API 返回 `license: null`，**未声明许可**，不能用。
- `cnchar` / `cnchar-types` / `pinyin-pro` / `segmentit`：都是 MIT，
  但逐一检查过包内容，**不含汉字频次数据**（`cnchar-order` 是笔顺，
  `pinyin-pro` 只有分词概率）。

## 5. rime-frost 知频表 —— 整句词频（**GPL-3.0**）

| 项 | 值 |
|---|---|
| 来源 | [gaboolic/rime-frost](https://github.com/gaboolic/rime-frost)（白霜拼音）`others/知频.txt` |
| 授权 | **GPL-3.0**（仓库声明）|
| 上游语料 | [esbatmop/MNBVC](https://github.com/esbatmop/MNBVC) —— **MIT** |
| 规模 | 2,128,233 条，格式「词<TAB>频次」，28 MB |
| 构建工具 | `tools/build_phrase_freq.py` |

### 为什么需要它

用户报「输入 `nihaoma` 显示『妳好吗』而不是『你好吗』」。根因不是排序错了，
是 **jieba 词典里根本没有「你好吗」**——3 个音节只拿到 1 个候选：

    你好吗   不在 jieba dict
    妳好吗   妳好吗 3 v    <- jieba 的默认兜底值
    你       你 234587 r

一个频次 3 的边角词赢了 234587 的「你」，**只因为它的三音节形式被收录了**。
jieba 系统性不覆盖口语整句（`是吗`/`对吗`/`好吗`/`行吗`/`可以吗`/`谢谢你`
全都不在），所以这一整类输入都会命中同样的问题。

### 为什么其它语料库解决不了

关键在于需要**未经分词**的整句。`你好吗` 在任何**分词后**的语料里都会被切成
`你好` + `吗`，所以逐一实测过：

| 源 | 授权 | 实测结果 |
|---|---|---|
| **BCC 语料库**（北语）`dialogue_word_freq.txt` | 可下载 | 实际下载过，136405 词条，**一条都补不上** |
| **CCL 语料库**（北大）| — | 词表下载入口 404，站点只有检索界面 |
| `jnext/chinese_word_frequency`（HuggingFace）| Apache-2.0 | 下载实测：**繁体** + 垃圾 n-gram（`饒你狗`/`點你還`），频次全是 1 |
| THUOCL | MIT | 领域词表（IT/财经/医学/法律），`你好吗`/`谢谢你` 一个都没有 |
| 中文维基 dump | CC BY-SA | 百科语料，不是口语 |
| OpenCC | Apache-2.0 | 是「台湾↔大陆用词对照」表，不提供大陆口语词频，且不含 `妳` |
| hanzidentifier / zhon / cnchar | MIT | 无词频数据 |

知频表虽然是用 jieba 分词统计的，但统计结果里保留了 `你好吗` 这样的整句
（分词前它就作为一个 token 存在于上游自定义分词词典里）。

### 这条把本项目改成了 GPL-3.0

许可链是这样的：

    MNBVC 语料（MIT）-> jieba 分词统计 -> 知频.txt 落在 GPL-3.0 仓库

**语料本身是 MIT 的**，GPL-3.0 来自整理/统计产物所在的仓库。ycpy 因此
整体改用 **GPL-3.0-or-later**（见根目录 `LICENSE`）。这个决定不是能靠
「只取子集」绕开的——GPL 的传染性针对的是衍生作品，不看取多少。

## 6. THUOCL —— 专业领域词表（**MIT**）

| 项 | 值 |
|---|---|
| 来源 | [thunlp/THUOCL](https://github.com/thunlp/THUOCL)（清华开放中文词库）`data/*.txt` |
| 授权 | **MIT License** |
| 规模 | 6 个领域共 54,565 条（含读音，1.8 MB）|
| 构建工具 | `tools/build_domain_dict.py` |

### 为什么需要它

用户的需求是「在首选项里勾选专业词典，勾选后输入就显示该领域的专业词汇」。
主词典（jieba 语料）系统性不覆盖专业词汇，实测缺失率：

| 领域 | 缺失率 |
|---|---|
| 法律 | 91.4% |
| 饮食 | 88.9% |
| IT | 73.0% |
| 医学 | 70.3% |
| 财经 | 43.8% |

### 只开放 6 个领域，刻意排除其余 5 个

| 排除 | 原因 |
|---|---|
| 成语 | 缺失率只 4.5%（jieba 已有 8139 条），价值最低 |
| 诗词 | 13,703 条，但要 5 个音节全打对才有意义，实际触发率极低 |
| 动物 | 98.9% 缺失，但大部分是生僻物种名 |
| 地名 | 44,802 条里大量是「广禄庄村」「连升路」这种极冷僻村名路名 |
| 人名 | 多音字读音无法可靠确定（见下面的坑）|

### DF 必须标定到 jieba 量纲

THUOCL 给的是 **DF（文档数）**，与 jieba 的**语料词频**不是一个量纲：

    THUOCL  版权    DF = 13,204,281
    jieba   的  freq =    883,634

直接混用会让 DF 上万的专业词霸榜。用「两表都有的词」当锚点（14,103 个，
覆盖 6 个领域）做 log 空间回归：

    log(jieba_freq) = 0.3195 * log(DF) + 0.7154

**保序性已验证**：逐领域检查 DF 顺序与 jieba freq 顺序的近邻一致率，
6 个领域全部 **100%**。

**但它压扁了量程**：slope=0.3195 远小于 1，标定后 12,854 个 IT 词只剩
**66 个不同取值**，99.3% 撞值——这是 DF 本身的分辨率不够。撞值的实际影响
很小：真正会「同音竞争」（两个词同时出现在同一输入的候选里）的对子共
192 对，标定后打平的只是少数。打平的靠**文件内按 DF 降序**定序，结果稳定
可复现，不依赖 dict 插入顺序。

### THUOCL 数据本身的两个坑

1. **行尾不统一**：`THUOCL_diming.txt` 是老 Mac 的 CR 分隔，用
   `split('\n')` 只能拿到 **1 行**。必须用 `splitlines()`。
2. **词和 DF 之间有多余空格**（`字符串 \t 395499`）。不 `.strip()` 的话
   IT/财经/成语三个表**一条都解析不出来**——这个坑很隐蔽，因为其它表正常。

### 许可相容性：MIT 与 GPL-3.0 并存

本项目主体是 GPL-3.0-or-later（因为整句词频来自 rime-frost 的 GPL-3.0
数据），而 THUOCL 是 MIT。这**没有冲突**——MIT 是 GPL-3.0 明确允许的
「更容易兼容」许可之一，GPLv3 附录 C 明确列举。`ycpy-domain-*.txt`
按 MIT 分发，主项目按 GPL-3.0-or-later 分发，两者可共存。

## 7. 本项目自身的授权

**GPL-3.0-or-later**（由上一节的数据源决定）。根目录 `LICENSE` 里是 GPL-3.0
全文（取自 gnu.org，逐字未改）加一段「适用范围说明」，写明为什么从 MIT
换成 GPL-3.0、以及 `data/` 下各文件各自的授权。

`debian/copyright` 已按机器可读格式重写，`Files: *` 标 GPL-3+，各数据文件
单独标注其上游授权。文件头（`DICT_HEADER` / `PHRASE_HEADER` / `UNIHAN_HEADER`）
在构建时写入来源与协议，满足 MIT「保留版权声明」和 Unicode V3
「保留版权与许可声明」两项义务。

### 复核 rime-frost 知频表

```bash
# 许可（仓库声明 GPL-3.0）
python3 - <<'EOF'
import urllib.request, json
d = json.load(urllib.request.urlopen(urllib.request.Request(
    "https://api.github.com/repos/gaboolic/rime-frost",
    headers={"User-Agent": "audit", "Accept": "application/vnd.github+json"})))
print("license:", (d.get("license") or {}).get("spdx_id"))
EOF

# 上游语料 MNBVC 是 MIT
python3 - <<'EOF'
import urllib.request, json
d = json.load(urllib.request.urlopen(urllib.request.Request(
    "https://api.github.com/repos/esbatmop/MNBVC",
    headers={"User-Agent": "audit", "Accept": "application/vnd.github+json"})))
print("MNBVC license:", (d.get("license") or {}).get("spdx_id"))
EOF

# 确认「妳好吗」这类边角词没有被收进表里
grep -c -P "^妳好吗\t" data/ycpy-phrase-freq.txt    # 期望 0
```

注意 `raw.githubusercontent.com` 在本项目作者的网络环境里被 TLS 重置，
所以 `tools/build_phrase_freq.py` 走 `api.github.com`：先用 `contents`
API 取 blob sha，再用 `git/blobs` API 取内容——大于 1 MB 的文件
`contents` API 不内联内容，只有 blob API 能拿到。

## 复核方式

```bash
# jieba / pypinyin 的许可
tar xf ~/.cache/ycpy-build/jieba-sdist.tar  -O jieba-0.42.1/PKG-INFO | grep -i license
tar xf ~/.cache/ycpy-build/pypinyin-sdist.tar -O pypinyin-0.55.0/LICENSE.txt

# CC-CEDICT 的许可（注意：应确认它**没有**进入本项目）
curl -s https://www.mdbg.net/chinese/dictionary?page=cc-cedict | grep -i -A3 creative

# Unicode 许可
curl -s https://www.unicode.org/license.txt

# 确认 phrases_dict 与 phrase-pinyin-data/pinyin.txt 同源（都 47111 条）
python3 -c "
import tarfile, json
tf = tarfile.open('~/.cache/ycpy-build/pypinyin-sdist.tar'.replace('~', __import__('os').path.expanduser('~')))
m = [n for n in tf.getnames() if n.endswith('phrases_dict.json')][0]
print(len(json.loads(tf.extractfile(m).read().decode('utf-8'))))"
```