"""普通话音节表与拼音串解析工具。"""

from __future__ import annotations

# 全部合法音节（无声调，ü 统一写成 v）
_SYLLABLES_RAW = """
a ai an ang ao
ba bai ban bang bao bei ben beng bi bian biao bie bin bing bo bu
ca cai can cang cao ce cen ceng ci cong cou cu cuan cui cun cuo
cha chai chan chang chao che chen cheng chi chong chou chu chua chuai
chuan chuang chui chun chuo
da dai dan dang dao de dei den deng di dia dian diao die ding diu
dong dou du duan dui dun duo
e en eng er
fa fan fang fei fen feng fo fou fu
ga gai gan gang gao ge gei gen geng gong gou gu gua guai guan
guang gui gun guo
ha hai han hang hao he hei hen heng hong hou hu hua huai huan huang
hui hun huo
ji jia jian jiang jiao jie jin jing jiong jiu ju juan jue jun
ka kai kan kang kao ke kei ken keng kong kou ku kua kuai kuan kuang
kui kun kuo
la lai lan lang lao le lei leng li lia lian liang liao lie lin ling
liu lo long lou lu luan lun luo lv lve
ma mai man mang mao me mei men meng mi mian miao mie min ming miu mo
mou mu
na nai nan nang nao ne nei nen neng ni nian niang niao nie nin ning
niu nong nou nu nv nve nuan nuo
pa pai pan pang pao pei pen peng pi pian piao pie pin ping po pou pu
qi qia qian qiang qiao qie qin qing qiong qiu qu quan que qun
ran rang rao re ren reng ri rong rou ru ruan rui run ruo
sa sai san sang sao se sen seng si song sou su suan sui sun suo
sha shai shan shang shao she shei shen sheng shi shou shu shua shuai
shuan shuang shui shun shuo
ta tai tan tang tao te teng ti tian tiao tie ting tong tou tu tuan tui
tun tuo
wa wai wan wang wei wen weng wo wu
xi xia xian xiang xiao xie xin xing xiong xiu xu xuan xue xun
ya yan yang yao ye yi yin ying yo yong you yu yuan yue yun
za zai zan zang zao ze zei zen zeng zi zong zou zu zuan zui zun zuo
zha zhai zhan zhang zhao zhe zhei zhen zheng zhi zhong zhou zhu zhua
zhuai zhuan zhuang zhui zhun zhuo
n m ng hm hng
"""

SYLLABLES: frozenset[str] = frozenset(_SYLLABLES_RAW.split())

MAX_SYLLABLE_LEN = max(len(s) for s in SYLLABLES)


def normalize(raw: str) -> str:
    """规范化用户输入：统一小写、把 ``ü``/``u:`` 写成 ``v``、去掉分隔符。"""
    raw = raw.strip().lower()
    out = []
    for ch in raw:
        if ch in "ü:Ü":
            out.append("v")
        elif ch.isascii() and ch.isalpha():
            out.append(ch)
        elif ch in "'’` -":
            continue
        else:
            return ""
    return "".join(out)


#: 「裸辅音」音节：只有一个辅音、没带元音。``n``/``m`` 单独用是语气词
#: （嗯、呣），但出现在多音节词的中间时，几乎总是用户漏敲了一个元音。
BARE_SYLLABLES = frozenset(s for s in SYLLABLES if not any(c in "aeiouv" for c in s))

#: 漏敲元音时最可能补上的那些，按常见度排序。生成候选变体时只试这几个，
#: 避免把 n 展开成 na/ne/ni/nu/nv/ng 六个分支去做无谓的索引查询。
_VOWEL_FIXES = {
    "n": ("ni", "na", "nu", "ne", "nv"),
    "m": ("mi", "ma", "mu", "me"),
}

#: 首字母不分：这些声母在口语里经常混着念，打字时也常敲错。
#: 左边是「用户可能敲出来的」，右边是「词典里真实写的」。
_FUZZY_INITIALS = (
    ("zh", "z"),
    ("ch", "c"),
    ("sh", "s"),
)

#: 生成变体时最多试几个。索引查询是二分，很便宜，但结果要打分排序，
#: 变体太多会把噪声候选也拉进来，得不偿失。
MAX_VARIANTS = 8

#: 单个音节最多补几个完整音节
_MAX_COMPLETIONS = 3


def _variant_bare(flat: str) -> list[str]:
    """补上漏掉的元音：``nhao`` -> ``nihao``、``nha`` -> ``niha``。"""
    parts = split_syllables(flat)
    if parts is None or len(parts) < 2:
        return []
    for part in parts:
        options = _VOWEL_FIXES.get(part)
        if not options:
            continue
        # 只修第一个裸辅音音节：多个位置同时出错的概率极低，全展开会让
        # 候选组合爆炸。``n`` 单独成音节时（就是「嗯」）不进这里，
        # 因为上面的 ``len(parts) < 2`` 已经把它挡住了。
        rest = "".join(parts[1:]) if parts[0] == part else \
            "".join(p for p in parts if p is not part)
        return [opt + rest for opt in options]
    return []


def _variant_fuzzy_initial(flat: str) -> list[str]:
    """声母不分：``chxy`` -> ``cxy``、``shxy`` -> ``sxy``。

    只做「去掉 h」这一个方向。反方向（``cxy`` -> ``chxy``）不做，
    因为它会把大量本来能精确命中的输入搅浑。
    """
    out: list[str] = []
    for wrong, right in _FUZZY_INITIALS:
        idx = flat.find(wrong)
        while idx >= 0:
            variant = flat[:idx] + right + flat[idx + len(wrong):]
            if variant not in out:
                out.append(variant)
            idx = flat.find(wrong, idx + 1)
    return out


#: 全部声母的首字母。首字母串按定义只由这些字母组成。
_INITIAL_LETTERS = frozenset("bpmfdtnlgkhjqxrzcsyw")


def _build_completions() -> dict[str, tuple[str, ...]]:
    """音节 -> 以它为真前缀的更长音节（用来补全没敲完的音节）。

    ``chen`` 会被登记成「可以补成 cheng」，于是 ``chenxuyuan`` 有机会
    被纠正成 ``chengxuyuan``。只登记长度为 2~4 的前缀：更长的前缀几乎
    都是另一个完整音节（``xian``/``xiang``），补它们只会制造噪声。
    """
    table: dict[str, list[str]] = {}
    for syl in SYLLABLES:
        for cut in range(2, min(5, len(syl))):
            table.setdefault(syl[:cut], []).append(syl)
    return {k: tuple(sorted(v, key=len))[:3] for k, v in table.items()}


#: 音节补全表，模块加载时算一次
_COMPLETIONS: dict[str, tuple[str, ...]] = _build_completions()


def as_initials(flat: str) -> str:
    """把任意拼音串压成首字母串：切音节、取每节声母、归一化 zh/ch/sh。

    这是「用户其实在打首字母」的判定和提取一步到位::

        chengxuyuan -> cxy    （正常全拼取首字母）
        chxuy       -> cxy    （元音是误敲，ch/c 不分）
        cxuyu       -> cxy

    实现上**必须先切音节再取首字母**，不能全局删元音——``cheng`` 删元音
    得到 ``chng``，里面的 ``h`` 是尾音，再去做 ``ch -> c`` 替换就会错位成
    ``cng``。

    切不开（``cxuy`` 这种混着元音的）就退回「删掉元音字母」，再归一化声母。

    对已经是首字母的输入（``cxy``）是幂等的，所以可以放心对所有输入调用。
    """
    parts = split_syllables(flat)
    if parts is not None and len(parts) >= 2:
        out = []
        for part in parts:
            if not part:
                continue
            initial = part[0]
            for wrong, right in _FUZZY_INITIALS:
                if part.startswith(wrong):
                    initial = right
                    break
            out.append(initial)
        return "".join(out)

    # 切不开：当成首字母串里混了元音来处理
    stripped = "".join(c for c in flat if c not in "aoeiu")
    for wrong, right in _FUZZY_INITIALS:
        stripped = stripped.replace(wrong, right)
    return stripped


def _variant_drop_vowel(flat: str) -> list[str]:
    """输入里混进了元音，但骨架是首字母串：``cxuy`` -> ``cxy``。

    出现 ``aoeiu`` 基本就是误敲。去掉之后必须**仍然全是声母字母**，
    否则说明这本来是全拼，不能这么改。

    门槛是「至少 2 个声母」，不是 3 个。之前写成 3 会漏掉两音节词：
    ``zhguo`` / ``zguo`` 去掉元音正好是 ``zg``，而 ``zg`` 正是词库里
    「中国」的 initials——差一步就把用户最想打的词排除在容错之外，
    结果只敲 ``zhguo`` 只能拿到那份和输入无关的通用高频词兜底。

    光看「去完还全是声母」**不够**：``nihao`` 去完是 ``nh``，同样是纯声母，
    可 ``nihao`` 拼得完全正确（``ni``+``hao``），只是碰巧也能当首字母串读。
    所以还要一条硬约束：**能干净切成音节的一律不许动**——那说明用户打的是
    全拼，不是首字母串里混了元音。切不开（``zhguo``、``cxuy``）才放行。
    """
    if split_syllables(flat) is not None:
        return []
    stripped = as_initials(flat)
    if stripped == flat or len(stripped) < 2:
        return []
    if not all(c in _INITIAL_LETTERS for c in stripped):
        return []
    return [stripped]


def _variant_complete_syllable(flat: str, allow: bool = True) -> list[str]:
    """音节没敲完：``chenxuyuan`` -> ``chengxuyuan``。

    切分成功、但某个音节其实是另一个更长音节的**前缀**时，把它补全。
    ``chen`` 是 ``cheng`` 的前缀，所以用户少敲了一个 g——这种情况
    无论怎么调前缀都救不回来，因为 ``chen`` 压根不是 ``cheng`` 的前缀。

    ``allow=False`` 时只做检查、不产出变体。调用方用它来判断「这个串本身
    是不是拼错了」：``nihao`` 的 ``ni`` 也是 ``nie`` 的前缀，但 ``nihao``
    本身完全合法，不该被纠正成 ``niehao``。只有当精确匹配也查不到东西时
    才真的去补——那时「多一个字母」的候选才比「什么都没有」更可信。

    一次只补一个音节，且只补前两个：用户打错的地方通常在开头，
    而且每个音节的补全分支会互相相乘，全展开组合会爆。
    """
    parts = split_syllables(flat)
    if parts is None or len(parts) < 2:
        return []
    out: list[str] = []
    for i in range(min(2, len(parts))):
        syl = parts[i]
        for longer in _COMPLETIONS.get(syl, ()):
            variant = "".join(parts[:i]) + longer + "".join(parts[i + 1:])
            if variant == flat:
                continue
            if allow:
                out.append(variant)
            else:
                return [variant]        # 只需知道「有可能被补」
            if len(out) >= _MAX_COMPLETIONS:
                return out
    return out


def fuzzy_variants(flat: str, exact_ok: bool = True) -> list[str]:
    """列出这个输入「可能本来想敲的」那些串，按可信度从高到低。

    精确前缀匹配是主路径，这里只救那些**前缀根本不成立**的输入：

    * 漏敲元音 —— ``nhao`` -> ``nihao``
    * 声母不分 —— ``chxy`` -> ``cxy``
    * 首字母串里混进元音 —— ``cxuy`` -> ``cxy``
    * 音节没敲完 —— ``chenxuyuan`` -> ``chengxuyuan``

    ``exact_ok=False`` 表示「精确匹配已经查到东西了」。这时**不做音节补全**：
    ``nihao`` 本身拼得完全正确，只是碰巧 ``ni`` 也是 ``nie`` 的前缀，
    去纠正它只会凭空造出 ``niehao`` 这种噪声。补全只在走投无路时才启用。

    返回值不含原串，长度受 ``MAX_VARIANTS`` 限制，越靠前越可信。

    >>> fuzzy_variants("nihao")
    []
    >>> fuzzy_variants("nihao", exact_ok=False)[:1]
    ['niehao']
    >>> fuzzy_variants("chenxuyuan", exact_ok=False)[0]
    ['chengxuyuan']
    >>> fuzzy_variants("chxuy")
    ['cxuy', 'cxy']
    """
    out: list[str] = []
    groups = [_variant_bare]
    if not exact_ok:
        groups.append(_variant_complete_syllable)
    groups += [_variant_fuzzy_initial, _variant_drop_vowel]

    for group in groups:
        for variant in group(flat):
            if variant != flat and variant not in out:
                out.append(variant)
            if len(out) >= MAX_VARIANTS:
                return out
    return out


#: 旧名字，保留兼容
repair_bare_syllables = fuzzy_variants


def split_syllables(flat: str) -> list[str] | None:
    """把不带分隔符的拼音串切成音节列表；无法完整切分时返回 ``None``。

    切分不唯一（例如 ``xian`` 既是 ``xi an`` 也是 ``xian``），这里采用
    「优先最长音节」的确定性策略，保证同一输入永远得到同一结果。
    """
    result: list[str] = []
    pos = 0
    n = len(flat)
    while pos < n:
        for length in range(min(MAX_SYLLABLE_LEN, n - pos), 0, -1):
            candidate = flat[pos : pos + length]
            if candidate in SYLLABLES:
                result.append(candidate)
                pos += length
                break
        else:
            return None
    return result


def pretty(flat: str) -> str:
    """把扁平的拼音串显示成 ``cheng xu yuan ji`` 的形式。"""
    syllables = split_syllables(flat)
    if syllables is None:
        return flat
    return " ".join(syllables)


def initials(flat_pinyin: str) -> str:
    """全拼串 -> 首字母串（音节以空格分隔时也可直接调用）。"""
    return "".join(part[0] for part in flat_pinyin.split() if part)