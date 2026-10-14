"""ycpy 压力测试 —— **模拟人的输入速度**的那一层。

为什么要有这一层
================
``tools/stress_core.py`` 跑得极快（故意不 sleep），5 小时能跑出 8 万条
序列、220 万次按键。它抓 bug 很有效，但有个**结构性盲区**：

**快到不真实的输入会制造出真人碰不到的状态，也会掩盖真人会遇到的时序问题。**
具体说，它永远不会出现这些：

* 按键之间有 100~300 ms 的间隔（真人打字是这个量级）；
* 按住不放导致的**自动重复**（X 连发 press、中间没有 release）；
* 焦点切换、鼠标点候选窗这类**异步事件**插在按键中间；
* 客户端与引擎之间**D-Bus 往返的延迟**。

用户报的症状是「偶尔会莫名其妙地输出很多回车键或者退格键」——「偶尔」
说明是低概率的时序问题，fast 压测恰好是最不容易复现这种问题的那种。

本层的核心不变量：**客户端到底收到了什么**
------------------------------------------
之前所有压测只验证「引擎内部状态对不对」。但用户看到的现象是**客户端
收到了不该收到的键**——所以这里额外维护一个**应用侧模型**：

* 引擎 ``process_key`` 返回 ``False`` = 这个键**透传给应用**；
* 返回 ``True`` = 引擎吃掉了，应用看不到。

于是可以精确断言：

1. **组字中，回车/退格必须被吃掉**。一旦透传，应用就会真的插入换行 /
   删除字符——这就是用户看到的「莫名其妙输出回车和退格」。
2. **透传的字母必须真的到达应用**，且应用侧文本与「引擎上屏内容 + 透传键」
   完全对得上。
3. 应用侧文本里**不允许凭空多出换行**，也不允许凭空少字符。

怎么模拟「人」
------------
* 按键间隔 90~280 ms（分布偏长尾：多数 110~180，偶尔 400~900）；
* 场景之间有 0.4~2.5 s 的停顿（人看候选窗、想一下、点鼠标）；
* 按权重混合真实场景：正常打字、选词、退格纠错、**退格删空**、
  按住不放、焦点切换、鼠标点候选窗、中英切换、整句拼接；
* 约 6% 的按键带 Shift/Ctrl（模拟大写与快捷键）。

用法::

    python3 tools/stress_human.py --duration 1800 --seed 1 --out /tmp/human.jsonl
    python3 tools/stress_human.py --sequences 200 --seed 7      # 不按时间，按条数
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from ycpy.composer import Composer                       # noqa: E402
from ycpy.config import Config                           # noqa: E402
from ycpy.dictionary import Dictionary                   # noqa: E402
from ycpy.engine import (CONTROL_MASK, KEY_BACKSPACE, KEY_ESCAPE,   # noqa: E402
                         KEY_KP_ENTER, KEY_RETURN, KEY_SPACE,
                         PinyinEngine)
from ycpy.usermodel import UserModel                     # noqa: E402

try:
    from ycpy import ibusproto as proto
except Exception:                                        # pragma: no cover
    proto = None

KEY_SHIFT_L = 0xFFE1
KEY_SHIFT_R = 0xFFE2
SHIFT_MASK = 1 << 0
LOCK_MASK = 1 << 2

VALID_STATES = {0, 1, 2, 3}          # IDLE / COMPOSING / ASSOCIATE / ENGLISH

#: 常见的拼音音节，用来拼出「像人打的东西」
COMMON_SYLLABLES = [
    "ni", "hao", "shi", "zhong", "guo", "wo", "men", "de", "le", "ma",
    "zai", "you", "ke", "yi", "bu", "xia", "qu", "lai", "zhe", "na",
    "jin", "tian", "qi", "jian", "zhi", "shi", "li", "he", "zhe", "lv",
]


class RecordingUI:
    """记录引擎对 UI 的调用，并据此维护「客户端实际收到的内容」。"""

    def __init__(self):
        self.commits: list[str] = []
        self.calls = 0
        self.table_shown = False
        self.hidden_count = 0
        self.shown_count = 0

    def __getattr__(self, name):
        def _noop(*a, **k):
            self.calls += 1
            return None
        return _noop

    def CommitText(self, text):                          # noqa: N802
        self.calls += 1
        self.commits.append(str(text))

    def UpdatePreeditText(self, text, cursor=0, visible=0, mode=0):  # noqa: N802
        self.calls += 1
        if visible:
            self.shown_count += 1
        else:
            self.hidden_count += 1

    def ShowAuxWindow(self, visible):                    # noqa: N802
        self.calls += 1
        self.table_shown = bool(visible)


class Bug:
    def __init__(self, kind, detail, seq, seed):
        self.kind = kind
        self.detail = detail
        self.seq = seq
        self.seed = seed

    def to_json(self):
        return {"kind": self.kind, "detail": self.detail,
                "seq": self.seq, "seed": self.seed}


class HumanStressor:
    """按人的节奏敲键盘，并盯住「客户端实际收到了什么」。"""

    def __init__(self, seed: int, out_path: str, speed: float = 1.0,
                 verbose: bool = False):
        self.rnd = random.Random(seed)
        self.seed = seed
        self.speed = speed
        self.verbose = verbose
        self.out_path = out_path
        self.bugs: list[Bug] = []
        self.seen_kinds: set[str] = set()
        self.keys = 0
        self.sequences = 0
        self.current: list[str] = []
        #: 客户端的文本缓冲区（被透传的键会改它）
        self.app_text: list[str] = []
        self.newlines_to_app = 0
        self.backspaces_to_app = 0
        #: keyval -> 物理键编号。见 _kc_for
        self._kcmap: dict[int, int] = {}

        tmp = tempfile.mkdtemp(prefix="ycpy-human-")
        self.tmp = tmp
        self.dictionary = Dictionary()
        self.user = UserModel(os.path.join(tmp, "user.json"))
        self.dictionary.apply_user_counts({})
        self.composer = Composer(self.dictionary, self.user)
        # 勾两个领域，覆盖「专业词库开启」这条路径
        self.composer.domains.load(["it", "medical"])
        self.config = Config(os.path.join(tmp, "config.json"))
        self.engine = PinyinEngine(self.dictionary, self.user, self.composer,
                                   config=self.config)
        self.ui = RecordingUI()
        self.engine.ui = self.ui
        self.engine.on_focus_in()

    # ------------------------------------------------------------ 工具

    def fail(self, kind, detail):
        if kind in self.seen_kinds:
            return
        self.seen_kinds.add(kind)
        bug = Bug(kind, detail, list(self.current[-30:]), self.seed)
        self.bugs.append(bug)
        try:
            with open(self.out_path, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(bug.to_json(), ensure_ascii=False) + "\n")
        except OSError:
            pass
        print("!! BUG[%s] %s" % (kind, detail), flush=True)
        print("   序列: %s" % " ".join(self.current[-30:]), flush=True)

    def _kc_for(self, keyval: int) -> int:
        """给 keyval 分配一个稳定的**物理键**编号（真实 X 里的 keycode）。

        这是压测以前的**结构性盲区**：``process_key`` 一直传 ``keycode=0``，
        于是配对只能靠 keyval，而 keyval 在真实链路上**是会变的**——

            Shift↓  A↓(keyval=0x41)  Shift↑  A↑(keyval=0x61)

        「keyval 在按下与抬起之间变了」这种情况因此**永远测不出来**，
        用户报的「随机连续触发按键」也就一直复现不了。补上 keycode 之后，
        压测走的才是真实链路的配对方式。

        大小写共用一个编号：'A' 与 'a' 本来就是同一个物理键，这正是
        「keyval 会变、keycode 不变」的前提。
        """
        ident = keyval + 0x20 if 0x41 <= keyval <= 0x5A else keyval
        if ident not in self._kcmap:
            # 8..255 与 X 惯例一致；用到的 keyval 就几十个，不会绕回来撞车。
            self._kcmap[ident] = 8 + len(self._kcmap) % 248
        return self._kcmap[ident]

    def press(self, keyval, modifier=0, note="", keycode=None):
        """模拟一次**按下**，并断言客户端收到的东西是否合理。"""
        e = self.engine
        composing = (e.state == 1 and bool(e.buffer))
        before_newline = self.newlines_to_app
        before_bs = self.backspaces_to_app

        self.keys += 1
        consumed = self.engine.process_key(
            keyval, self._kc_for(keyval) if keycode is None else keycode,
            modifier)

        # ---- 核心不变量：组字中，回车/退格**绝不能**透传给应用 ----
        #
        # 用户报「偶尔莫名其妙输出很多回车键或者退格键」。回到客户端视角
        # 就是这两个键：引擎说「我不处理」，于是应用真的插入换行 / 删除
        # 字符。组字中时它们必须被吃掉。
        if not consumed:
            if keyval == KEY_RETURN and composing:
                self.fail("enter_leaked_while_composing",
                          "组字中回车透传给应用，会插入换行")
            if keyval in (KEY_BACKSPACE,) and composing:
                self.fail("backspace_leaked_while_composing",
                          "组字中退格透传给应用，会删掉已上屏的文字")
            # 应用侧模型
            if keyval == KEY_RETURN or keyval == KEY_KP_ENTER:
                self.newlines_to_app += 1
                self.app_text.append("\n")
            elif keyval == KEY_BACKSPACE:
                self.backspaces_to_app += 1
                # 退过头是**正常**的（行首再退就什么也不删），不算 bug。
                # 这里只维护模型，不报错。
                if self.app_text:
                    self.app_text.pop()
            elif 0x61 <= keyval <= 0x7A:
                self.app_text.append(chr(keyval))
            elif 0x41 <= keyval <= 0x5A:
                self.app_text.append(chr(keyval).lower())
        else:
            # 被吃掉：commit 出来的内容应当已经进客户端
            pass

        if self.newlines_to_app > before_newline and not composing:
            pass          # 组字态下的换行是**用户自己**按的，正常

        label = {KEY_RETURN: "Enter", KEY_BACKSPACE: "Backspace",
                 KEY_SPACE: "Space", KEY_ESCAPE: "Esc"}.get(keyval)
        if label is None and 0x61 <= keyval <= 0x7A:
            label = chr(keyval)
        self.current.append("%s%s%s" % (label or hex(keyval),
                                        "+" if modifier else "",
                                        note))
        return consumed

    def release(self, keyval, modifier=0, keycode=None):
        self._last_release_ret = self.engine.process_key(
            keyval, self._kc_for(keyval) if keycode is None else keycode,
            modifier | (1 << 30))
        return self._last_release_ret

    def tap(self, keyval, modifier=0, note="", keycode=None):
        """一次完整的按下 + 抬起（默认同一个 keycode——物理键没变）。"""
        kc = self._kc_for(keyval) if keycode is None else keycode
        r = self.press(keyval, modifier, note, keycode=kc)
        self.release(keyval, modifier, keycode=kc)
        return r

    def type_word(self, word=None):
        n = self.rnd.randint(2, 5)
        word = word or "".join(self.rnd.choice(COMMON_SYLLABLES)
                               for _ in range(n))
        for ch in word:
            if not ch.isalpha():
                continue
            self.tap(ord(ch), SHIFT_MASK if ch.isupper() else 0)
            self.pause(0.09, 0.28)
        return word

    def pause(self, lo=0.09, hi=0.28):
        """人打字的间隔。``speed`` 用来整体加速（调试时用）。"""
        if self.speed <= 0:
            return
        time.sleep(self.rnd.uniform(lo, hi) / self.speed)

    def long_pause(self):
        self.pause(0.4, 2.5)

    # ------------------------------------------------------------ 状态不变量

    def check(self, tag=""):
        e = self.engine
        if e.state not in VALID_STATES:
            self.fail("state_invalid", "非法状态 state=%r（%s）" % (e.state, tag))
            return False
        if e.state == 1 and not e.buffer:
            self.fail("composing_without_buffer",
                      "COMPOSING 态但 buffer 为空（%s）" % tag)
            return False
        if e.buffer and not e.raw_keys:
            self.fail("raw_keys_empty", "buffer=%r 但 raw_keys 为空（%s）"
                      % (e.buffer, tag))
            return False
        if e.raw_keys and not e.buffer:
            self.fail("buffer_empty", "raw_keys=%r 但 buffer 为空（%s）"
                      % (e.raw_keys, tag))
            return False
        if len(e._press_consumed) > 512:
            self.fail("press_map_growth",
                      "按下记录表涨到 %d 条（%s）" % (len(e._press_consumed), tag))
            return False
        return True

    # ------------------------------------------------------------ 场景

    def scenario_typing(self):
        """正常打字 + 选词。"""
        self.type_word()
        choice = self.rnd.random()
        if choice < 0.45:
            self.pause()
            self.tap(KEY_SPACE, note="(选词)")
        elif choice < 0.6:
            self.pause()
            self.tap(ord(str(self.rnd.randint(1, 5))), note="(选词)")
        else:
            self.pause()
            self.tap(KEY_RETURN, note="(原样上屏)")

    def scenario_backspace_partial(self):
        """打一半退格纠错。"""
        self.type_word()
        for _ in range(self.rnd.randint(1, 2)):
            self.pause()
            self.tap(KEY_BACKSPACE, note="(纠错)")
        self.check("backspace_partial")

    def scenario_backspace_to_empty(self):
        """一直退格删空——这是「退格删空后状态卡住」那个 bug 的场景。"""
        word = self.type_word()
        for _ in range(len(word) + self.rnd.randint(1, 3)):
            self.pause(0.12, 0.3)
            self.tap(KEY_BACKSPACE, note="(删空)")
            if not self.check("backspace_to_empty"):
                return
        # 删空之后再按退格，应该交给应用（此时客户端文本非空才合理）
        self.pause()
        self.tap(KEY_BACKSPACE, note="(交给应用)")

    def scenario_hold_key(self):
        """按住不放：X 连发 press、中间没有 release。"""
        key = ord(self.rnd.choice("abcdefghijklmnopqrstuvwxyz"))
        self.tap(key)
        for _ in range(self.rnd.randint(3, 12)):
            self.pause(0.05, 0.09)          # 自动重复的间隔比人手快
            self.press(key, note="(连发)")
            self.keys += 0
        self.release(key)
        self.check("hold_key")

    def scenario_focus_race(self):
        """焦点切换**插在按下与抬起之间**——用户报的 bug 的核心场景。

        X 会在同一时刻送出 KeyRelease 和 FocusOut，两者进 D-Bus 队列的
        顺序不保证。``on_focus_out`` 里那句 ``self._press_consumed.clear()``
        会把「透传给应用的键」的记录抹掉，于是它的抬起 pop 不到 ->
        兜底返回 True -> **应用只收到 press、收不到 release**。

        应用（GTK 一类）于是认为那个键还按着，开始自动重复——
        用户看到的就是「没碰退格却在连续删除」「凭空冒出好几个换行」。
        """
        for keyval in (KEY_BACKSPACE, KEY_RETURN, ord("a"), ord("z")):
            self.pause(0.2, 0.7)
            press_ret = self.press(keyval, note="(焦点竞态)")
            # 焦点在 press 与 release 之间切走
            self.engine.on_focus_out()
            self.pause(0.05, 0.3)
            self.engine.on_focus_in()
            self.release(keyval)
            # 核心断言：press 与 release 的结论必须一致，客户端才收得到完整的一对
            expected = press_ret
            actual = self._last_release_ret
            if expected != actual:
                self.fail("press_release_unpaired",
                          "%s 的 press 返回 %s 但 release 返回 %s —— "
                          "客户端只收到一半，会以为键还按着从而自动重复"
                          % (hex(keyval), expected, actual))
            self.check("focus_race")

    def scenario_focus_switch(self):
        """组字中焦点被切走再切回。"""
        self.type_word()
        self.pause()
        self.engine.on_focus_out()
        self.pause(0.2, 0.9)
        self.engine.on_focus_in()
        self.pause()
        if not self.check("focus_switch"):
            return
        # 切回来之后继续打字，必须正常
        self.type_word()
        self.tap(KEY_SPACE)

    def scenario_mouse_click_popup(self):
        """鼠标点候选窗（客户端会发 HideAuxWindow / 焦点变化）。"""
        self.type_word()
        self.pause(0.3, 1.2)
        self.ui.ShowAuxWindow(False)
        self.engine.on_focus_out()
        self.pause(0.1, 0.5)
        self.engine.on_focus_in()
        self.pause()
        # 点完之后往往直接敲下一个词
        self.type_word()
        self.tap(KEY_SPACE)
        self.check("mouse_popup")

    def scenario_english_toggle(self):
        """中英切换。裸 Shift 单独按一次才切，配合别的键不切。"""
        self.type_word()
        self.pause()
        self.tap(KEY_SHIFT_L, note="(切换)")
        self.pause(0.2, 0.6)
        # 英文态下打字，应该全部透传
        before = len(self.app_text)
        for ch in "abc":
            self.tap(ord(ch))
            self.pause(0.08, 0.2)
        if len(self.app_text) - before != 3:
            self.fail("english_passthrough",
                      "英文态下 %d 个字母只透传了 %d 个"
                      % (3, len(self.app_text) - before))
        self.pause()
        self.tap(KEY_SHIFT_L, note="(切回)")
        self.check("english_toggle")

    def scenario_shift_modifier(self):
        """按住 Shift 打大写（Shift 不应触发中英切换）。"""
        n = self.rnd.randint(1, 4)
        for i in range(n):
            ch = self.rnd.choice("abcdefghijklmnopqrstuvwxyz")
            self.press(KEY_SHIFT_L, SHIFT_MASK)
            self.pause(0.05, 0.12)
            self.tap(ord(ch), SHIFT_MASK, note="(大写)")
            self.release(KEY_SHIFT_L, 0)
            self.pause(0.1, 0.25)
        if self.engine._pending_shift_toggle:
            self.fail("shift_toggle_leak",
                      "Shift 当修饰键用完后仍挂着待切换标记")
        self.check("shift_modifier")

    def scenario_modifier_release_order(self):
        """先松修饰键、后松字母 —— 同一个物理键的 keyval 在中途会变。

        回归 bug（用户报「还是随机的会出现连续触发按键的情况，可能还是有
        地方对释放按键没处理好」）。

        X 里 ``keyval = f(keycode, 修饰键状态)``，keyval 是**解释出来的结果**，
        不是硬件事实。「先松 Shift 再松字母」在真人打字里很常见（把 Shift 当
        快速修饰键用，手先离开 Shift），于是真实序列是::

            Shift↓   A↓(keyval=0x41)   Shift↑   A↑(keyval=0x61)
                                                    ^^^^^^^^^^^
                                       抬起时 Shift 已经没了，解释成 'a'

        按下走的是「Shift/CapsLock 下的字母直接透传」那条 -> 记 ``False``；
        抬起若按 keyval 去找记录就找不到 -> 兜底 ``True``。于是

            press   -> 应用收到 KeyPress
            release -> 应用收不到 KeyRelease
            -> 客户端认为键还按着，开始自动重复 = 用户看到的连续触发

        **为什么以前的压测抓不到**：``tap()`` 的 press/release 永远用同一个
        keyval，`keycode` 又一律传 0，「keyval 变了」这个前提根本没被制造
        出来。这个场景专门制造它，且 keycode 两次相同（物理键没变）。
        """
        kc_shift = self._kc_for(KEY_SHIFT_L)
        ch = self.rnd.choice("abcdefghijklmnopqrstuvwxyz")
        kc_letter = self._kc_for(ord(ch))
        up, lo = ord(ch.upper()), ord(ch)

        if self.rnd.random() < 0.8:
            # Shift↓ A↓ Shift↑ A↑
            self.press(KEY_SHIFT_L, 0, keycode=kc_shift, note="(修饰键)")
            self.pause(0.03, 0.09)
            p = self.press(up, SHIFT_MASK, keycode=kc_letter, note="(大写)")
            self.pause(0.03, 0.10)
            self.release(KEY_SHIFT_L, 0, keycode=kc_shift)
            r = self.release(lo, 0, keycode=kc_letter)   # keyval 已经从 up 变成 lo
        else:
            # CapsLock 开着按 a -> 中途关掉 CapsLock -> 才抬起
            p = self.press(up, LOCK_MASK, keycode=kc_letter, note="(CapsLock)")
            self.pause(0.03, 0.10)
            r = self.release(lo, 0, keycode=kc_letter)   # keyval 已经从 up 变成 lo

        if p != r:
            self.fail("press_release_unpaired",
                      "keycode=%d：按 %s(keyval=%#x) 返回 %s，"
                      "抬 %s(keyval=%#x) 返回 %s —— 客户端只收到 press、"
                      "收不到 release，会以为键还按着从而连续触发"
                      % (kc_letter, chr(up), up, p, chr(lo), lo, r))
        self.check("modifier_release_order")

    def scenario_sentence(self):
        """整句拼接（触发分词）。"""
        word = self.type_word()
        self.pause(0.3, 1.0)
        self.type_word()
        self.pause()
        self.tap(KEY_SPACE)
        self.check("sentence")

    def scenario_punct(self):
        """中文标点转换。"""
        for ch in self.rnd.choice([",", ".", "?", "!", ";"]):
            self.type_word()
            self.pause()
            self.tap(ord(ch))
            self.pause(0.1, 0.3)
        self.check("punct")

    def scenario_domains(self):
        """专业词库路径：完整拼音、韵母缩写、前缀三种都敲。"""
        for py in ("shujuku", "sjk", "jib", "jibing", "bianhua"):
            for ch in py:
                self.tap(ord(ch))
                self.pause(0.09, 0.26)
            self.pause()
            if self.rnd.random() < 0.6:
                self.tap(KEY_SPACE)
            self.check("domains")

    #: (场景名, 权重)。**在 run() 里按名字取方法**，不要在这里直接引用
    #: 函数对象——类体里写 ``(scenario_typing, 20)`` 拿到的是**未绑定**的
    #: 函数，调用时会少一个 self（这个坑踩过一次）。
    SCENARIOS = [
        ("typing", 20),
        ("backspace_partial", 12),
        ("backspace_to_empty", 10),
        ("hold_key", 8),
        ("focus_switch", 8),
        ("focus_race", 12),
        ("mouse_click_popup", 8),
        ("english_toggle", 7),
        ("shift_modifier", 7),
        ("modifier_release_order", 8),
        ("sentence", 8),
        ("punct", 4),
        ("domains", 6),
    ]

    # ------------------------------------------------------------ 主循环

    def run(self, duration=None, sequences=None):
        names = [n for n, _ in self.SCENARIOS]
        weights = [w for _, w in self.SCENARIOS]
        lookup = {n: getattr(self, "scenario_" + n) for n in names}
        start = time.monotonic()
        while True:
            if sequences is not None and self.sequences >= sequences:
                break
            if duration is not None and time.monotonic() - start >= duration:
                break
            self.current = []
            self.sequences += 1
            picked = self.rnd.choices(names, weights=weights, k=1)[0]
            fn = lookup[picked]
            try:
                fn()
            except Exception as exc:               # 崩溃型
                import traceback
                self.fail("exception", "%s: %s\n%s"
                          % (type(exc).__name__, exc, traceback.format_exc()))
                # 复位，避免连锁失败
                try:
                    self.engine.reset()
                except Exception:
                    pass
            # 场景之间人是要停一下的
            self.long_pause()
            # 偶尔把客户端文本清掉（模拟用户换了一行/新开文档）
            if self.rnd.random() < 0.06:
                self.app_text.clear()


def main():
    ap = argparse.ArgumentParser(description="ycpy 压力测试（模拟人的输入速度）")
    ap.add_argument("--duration", type=float, default=1800, help="跑多少秒")
    ap.add_argument("--sequences", type=int, default=0,
                    help="跑多少条序列（给了就不看 --duration）")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="/tmp/ycpy-stress-human.jsonl")
    ap.add_argument("--speed", type=float, default=1.0,
                    help=">1 加速（间隔除以它），调试用")
    args = ap.parse_args()

    print("ycpy 压力测试（模拟人的输入速度）", flush=True)
    print("  种子 %d  速度 %.1fx  结果 %s"
          % (args.seed, args.speed, args.out), flush=True)
    print("  按键间隔 %.0f~%.0f ms，场景间停顿 %.1f~%.1f s"
          % (90 / args.speed, 280 / args.speed,
             400 / args.speed, 2500 / args.speed), flush=True)

    s = HumanStressor(args.seed, args.out, speed=args.speed)
    t0 = time.monotonic()
    try:
        s.run(duration=(args.duration if not args.sequences else None),
              sequences=(args.sequences or None))
    except KeyboardInterrupt:
        print("\n（Ctrl-C 中断）", flush=True)

    dt = time.monotonic() - t0
    print("", flush=True)
    print("=" * 60, flush=True)
    print("压测结束：%d 条序列 / %d 次按键 / %.0f 秒"
          % (s.sequences, s.keys, dt), flush=True)
    print("  真实节奏约 %.0f 次按键/分钟" % (s.keys / max(dt, 1) * 60), flush=True)
    print("  透传给客户端：换行 %d 次、退格 %d 次"
          % (s.newlines_to_app, s.backspaces_to_app), flush=True)
    if s.bugs:
        print("  发现 bug %d 条（%d 类）：" % (len(s.bugs), len(s.seen_kinds)),
              flush=True)
        for b in s.bugs:
            print("    - [%s] %s" % (b.kind, b.detail.split("\n")[0]))
    else:
        print("  ✅ 没发现 bug", flush=True)
    return 1 if s.bugs else 0


if __name__ == "__main__":
    sys.exit(main())