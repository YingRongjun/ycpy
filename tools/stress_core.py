"""ycpy 长时间压力测试 —— 核心层。

设计要点
========

**为什么要有「不变量」而不只是「不崩」。**
压测最怕的是「没报错但状态已经错了」。所以这里对每一个按键序列都断言
一组必须成立的不变量，任何一条不成立就记一条 bug。

**两类 bug 的检测能力**
1. *崩溃型*：异常、断言失败——直接捕获 traceback。
2. *静默错误型*：按键 press/release 配对错乱、状态机跑到非法态、
   缓冲区与原始键不一致、记录表无界增长。这类不崩，但正是用户会遇到
   「莫名其妙连续退格」「Shift 卡住」那类问题——必须靠不变量抓。

**怎么模拟「人的输入」**
不是随机敲 key。而是按权重混合几种真实场景：
- 正常打字（高频音节 + 随机长串）
- 打字��选词（空格、数字、上/下页）
- 退格纠错（含按住不放的连击）
- 焦点切换（FocusOut/FocusIn 打断）
- 中英切换
- 长句拼接（触发分词）
- 误触/边界（超长输入、标点、符号）

**并行度**
单进程跑满 CPU 才能在 5 小时里跑出足够多的序列。所以不 sleep，
只靠随机权重制造多样性。

用法::

    python3 tools/stress_core.py --duration 18000 --seed 1 --out /tmp/stress.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ycpy.composer import Composer
from ycpy.config import SHIFT_MASK
from ycpy.engine import (IBUS_RELEASE_MASK, KEY_BACKSPACE, KEY_DOWN, KEY_UP,
                         KEY_ESCAPE, KEY_PAGE_DOWN, KEY_PAGE_UP, KEY_RETURN,
                         KEY_SHIFT_L, KEY_SPACE, KEY_TAB)
from ycpy.dictionary import Dictionary
from ycpy.engine import (ST_ASSOCIATE, ST_COMPOSING, ST_ENGLISH, ST_IDLE,
                         PinyinEngine)
from ycpy.usermodel import UserModel

VALID_STATES = {ST_IDLE, ST_COMPOSING, ST_ASSOCIATE, ST_ENGLISH}

# 常见音节，按真实使用频率加权
COMMON_SYLL = [
    "z", "e", "a", "n", "i", "u", "g", "h", "o", "y", "b", "p",
    "shi", "zh", "ch", "sh", "ng", "ai", "ei", "ao", "ou", "an", "en",
    "ang", "eng", "ing", "ong", "ian", "uan", "ua", "uo", "ie", "ve", "van",
    "ni", "hao", "zhong", "guo", "shi", "jie", "xie", "de", "le", "ma",
    "wei", "le", "men", "xiang", "qu", "dao", "jin", "tian", "yun", "feng",
]
# 会故意打错的片段（缺键、多键、顺序错）
TYPO_BITS = ["zhogn", "zhguo", "shci", "niah", "ceshi", "pinyin", "abc",
             "woaini", "zhonghuarenmingongheguo"]


class RecordingUI:
    """记录引擎对 UI 做的所有调用，用来交叉验证。"""

    def __init__(self):
        self.commits: list[str] = []
        self.preedit_log: list[tuple[str, bool]] = []
        self.table_visible = False
        self.table_size = 0
        self.calls = 0

    def __getattr__(self, name):
        def _noop(*a, **k):
            self.calls += 1
            return None
        return _noop

    def CommitText(self, text):  # noqa: N802
        # 引擎发的是 IBusText 结构体，不是裸字符串（见 engine._emit_commit）
        self.calls += 1
        self.commits.append(str(text))

    def UpdatePreeditText(self, text, cursor=0, visible=0, mode=0):  # noqa: N802
        self.calls += 1
        self.preedit_log.append((str(text), bool(visible)))

    def UpdateLookupTable(self, table, visible):  # noqa: N802
        self.calls += 1
        self.table_visible = bool(visible)
        self.table_size = len(table[7]) if visible and len(table) > 7 else 0

    def show_lookup_table(self, *a):  # noqa: N802
        self.calls += 1
        self.table_visible = True

    def hide_lookup_table(self, *a):  # noqa: N802
        self.calls += 1
        self.table_visible = False


class Bug:
    def __init__(self, kind, detail, seq, trace=None):
        self.kind = kind
        self.detail = detail
        self.seq = seq
        self.trace = trace

    def to_json(self):
        return {
            "kind": self.kind,
            "detail": self.detail,
            "seq": self.seq,
            "trace": self.trace,
        }


class Stressor:
    def __init__(self, seed: int, out_path: str):
        self.rnd = random.Random(seed)
        self.seed = seed
        self.out_path = out_path
        self.bugs: list[Bug] = []
        self.seen_kinds: set[str] = set()
        self.keystrokes = 0
        self.sequences = 0
        self.current: list = []
        #: (场景名, 耗时)。用来定位「哪条序列特别慢」。
        self.seq_times: list[tuple[str, float]] = []

        tmp = tempfile.mkdtemp(prefix="ycpy-stress-")
        self.tmp = tmp
        self.dictionary = Dictionary()
        self.user = UserModel(os.path.join(tmp, "user.json"))
        self.dictionary.apply_user_counts({})
        self.composer = Composer(self.dictionary, self.user)
        from ycpy.config import Config
        self.engine = PinyinEngine(self.dictionary, self.user, self.composer,
                                   config=Config(os.path.join(tmp, "config.json")))
        self.ui = RecordingUI()
        self.engine.ui = self.ui
        self.engine.on_focus_in()

    # ------------------------------------------------------------ bug 记录

    def fail(self, kind, detail):
        """记一条 bug（同类只记一次，避免刷屏）。"""
        if kind in self.seen_kinds:
            self.bugs.append(Bug(kind, detail + " [重复]", list(self.current)))
            return
        self.seen_kinds.add(kind)
        seq = list(self.current)
        bug = Bug(kind, detail, seq)
        self.bugs.append(bug)
        try:
            with open(self.out_path, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(bug.to_json(), ensure_ascii=False) + "\n")
        except OSError:
            pass
        print("!! BUG[%s] %s" % (kind, detail), flush=True)
        print("   序列: %s" % _fmt(self.current[-25:]), flush=True)

    # ------------------------------------------------------------ 不变量

    def check_invariants(self, tag=""):
        e = self.engine
        if e.state not in VALID_STATES:
            self.fail("state_invalid",
                      "非法状态 state=%r（%s）" % (e.state, tag))
            return False
        # buffer 是 raw_keys 规范化后的结果，raw_keys 一定是 buffer 的子序列来源
        if e.raw_keys and not e.buffer:
            self.fail("buffer_inconsistent",
                      "raw_keys=%r 非空但 buffer 为空（%s）" % (e.raw_keys, tag))
            return False
        if e.buffer and not e.raw_keys:
            self.fail("buffer_inconsistent",
                      "buffer=%r 非空但 raw_keys 为空（%s）" % (e.buffer, tag))
            return False
        if e.state == ST_COMPOSING and not e.buffer:
            self.fail("state_buffer_mismatch",
                      "COMPOSING 态但 buffer 为空（%s）" % tag)
            return False
        # 记录表不能无界增长（release 丢了也不该一直攒）
        if len(e._press_consumed) > 512:
            self.fail("press_map_growth",
                      "按下记录表涨到 %d 条（%s）" % (len(e._press_consumed), tag))
            return False
        # 候选页不能越界
        if e.page_candidates:
            if e.page < 0:
                self.fail("page_negative", "page=%d（%s）" % (e.page, tag))
                return False
        return True

    def assert_state_composing(self, tag):
        """断言确实在组字态——否则「空格没上屏」是预期行为，不是 bug。"""
        if self.engine.state != ST_COMPOSING or not self.engine.buffer:
            return False
        return True

    def check_pairing(self, keyval, press_ret, release_ret, tag):
        """press / release 的结论必须一致——这是 3.11/3.12 那类 bug 的守门人。

        不一致意味着客户端只收到一半，会认为那个键还按着，于是开始自己
        补发事件（用户看到的就是「没碰退格却在连删」）。
        """
        if press_ret != release_ret:
            self.fail("press_release_mismatch",
                      "keyval=%#x 按下=%s 抬起=%s（%s）"
                      % (keyval, press_ret, release_ret, tag))
            return False
        return True

    # ------------------------------------------------------------ 输入生成

    def rand_syllable_text(self, max_len=8):
        return "".join(self.rnd.choice(COMMON_SYLL)
                       for _ in range(self.rnd.randint(1, max_len)))

    def do_press(self, keyval, mod=0):
        self.current.append(("D", keyval, mod))
        # 逐按键计时：卡死的序列**永远不会返回**，所以序列级计时抓不到，
        # 必须在按键这一层记。压测里见过「跑到 326 个序列后 CPU 100%
        # 自旋 5 分钟」，就是靠这层定位的。
        t0 = time.monotonic()
        r = self.engine.process_key(keyval, 0, mod)
        dt = time.monotonic() - t0
        if dt > 0.5:
            self.fail("slow_keystroke",
                      "press keyval=%#x 用了 %.1f 秒（缓冲区 %r）"
                      % (keyval, dt, self.engine.buffer[:40]))
            print("   上下文: %s" % _fmt(self.current[-12:]), flush=True)
        self.keystrokes += 1
        return r

    def do_release(self, keyval, mod=0):
        self.current.append(("U", keyval, mod))
        r = self.engine.process_key(keyval, 0, IBUS_RELEASE_MASK | mod)
        self.keystrokes += 1
        return r

    def tap(self, keyval, mod=0):
        """按下 + 抬起，并检查配对一致。"""
        p = self.do_press(keyval, mod)
        r = self.do_release(keyval, mod)
        self.check_pairing(keyval, p, r, "tap")
        self.check_invariants("tap %#x" % keyval)
        return p

    def type_text(self, text):
        for ch in text:
            self.tap(ord(ch))

    def scenario_typing(self):
        """正常打字 + 空格选词。"""
        self.type_text(self.rand_syllable_text(self.rnd.randint(1, 6)))
        n = self.rnd.randint(1, 4)
        for _ in range(n):
            if self.rnd.random() < 0.6:
                self.tap(KEY_SPACE)
            else:
                self.type_text("".join(self.rnd.choice(COMMON_SYLL)
                                       for _ in range(self.rnd.randint(1, 3))))

    def scenario_backspace(self):
        """退格纠错，含按住不放的连击。"""
        self.type_text(self.rand_syllable_text(self.rnd.randint(2, 8)))
        for _ in range(self.rnd.randint(1, 12)):
            self.tap(KEY_BACKSPACE)
        # 模拟按住退格连发：多个 press 无 release，最后一个 release
        k = self.rnd.randint(2, 8)
        for _ in range(k):
            self.do_press(KEY_BACKSPACE)
        self.do_release(KEY_BACKSPACE)
        self.check_invariants("连击退格")

    def scenario_selection(self):
        """上下翻页 + 数字选词。"""
        self.type_text(self.rand_syllable_text(self.rnd.randint(1, 5)))
        for _ in range(self.rnd.randint(0, 5)):
            self.tap(self.rnd.choice([KEY_DOWN, KEY_PAGE_DOWN, KEY_UP, KEY_PAGE_UP]))
        self.tap(ord(str(self.rnd.randint(1, 9))))

    def scenario_focus_switch(self):
        """焦点切换——最危险的一条：release 可能丢，检验残留记录。"""
        self.type_text(self.rand_syllable_text(self.rnd.randint(1, 5)))
        self.do_press(KEY_SHIFT_L)
        self.type_text(self.rand_syllable_text(2))
        # 切断：只发 FocusOut，不发任何 release
        self.current.append(("FOCUS_OUT",))
        self.engine.on_focus_out()
        self.current.append(("FOCUS_IN",))
        self.engine.on_focus_in()
        if self.engine._press_consumed:
            self.fail("focus_leftover",
                      "FocusOut 后仍残留 %d 条按下记录: %r"
                      % (len(self.engine._press_consumed),
                         list(self.engine._press_consumed)[:6]))
        # 焦点回来后继续正常输入，确认没坏
        self.type_text("zhong")

    def scenario_english_toggle(self):
        """中英切换，尤其是「单按 Shift」和「按住 Shift 打字」两种。"""
        self.type_text(self.rand_syllable_text(3))
        if self.rnd.random() < 0.5:
            # 单按 Shift（应该切中英）
            p = self.do_press(KEY_SHIFT_L)
            r = self.do_release(KEY_SHIFT_L)
            self.check_pairing(KEY_SHIFT_L, p, r, "单按Shift")
        else:
            # 按住 Shift 打字母（应该出大写，不切状态）
            self.do_press(KEY_SHIFT_L)
            for ch in self.rnd.choice(["abc", "xyz", "test"]):
                p = self.do_press(ord(ch), SHIFT_MASK)
                self.do_release(ord(ch), SHIFT_MASK)
                if p:
                    self.fail("shift_letter_consumed",
                              "中文态 Shift+%r 被引擎消费了，应该透传出大写" % ch)
            r = self.do_release(KEY_SHIFT_L)
            if r:
                self.fail("shift_release_swallowed",
                          "Shift 抬起被吞（中间夹了别的键）")
        self.check_invariants("中英切换")

    def scenario_long_sentence(self):
        """长句拼接（触发 k-best 分词）。"""
        parts = [self.rnd.choice(["dajia", "yiqi", "ganhuo", "zhongguo",
                                  "renmin", "gonghe", "weishenme", "zhege",
                                  "women", "keyi", "shuomian"])
                 for _ in range(self.rnd.randint(2, 5))]
        self.type_text("".join(parts))
        self.tap(KEY_RETURN)

    def scenario_typo(self):
        """故意打错，检验容错路径 + 拼音回显。"""
        self.type_text(self.rnd.choice(TYPO_BITS))
        if self.rnd.random() < 0.5:
            self.tap(KEY_ESCAPE)
        else:
            self.tap(KEY_RETURN)

    def scenario_long_input(self):
        """超长输入——拼接/分词的边界。"""
        self.type_text(self.rand_syllable_text(self.rnd.randint(15, 60)))
        self.tap(KEY_RETURN)

    def scenario_weird_keys(self):
        """边界按键：Tab、标点、数字、符号。"""
        for kv in (KEY_TAB, KEY_ESCAPE, KEY_RETURN,
                   ord(","), ord("."), ord("?"), ord("!"),
                   ord("1"), ord("9"), ord("-"), ord("="), 0x20):
            self.tap(kv)

    def scenario_repeated_select(self):
        """连续选词/上屏，检查 commit 记录是否与预期一致。"""
        before = len(self.ui.commits)
        self.type_text("nihao")
        self.assert_state_composing("nihao")
        self.tap(KEY_SPACE)
        self.tap(KEY_SPACE)
        self.tap(KEY_SPACE)
        if len(self.ui.commits) < before + 1:
            self.fail("no_commit",
                      "连敲 3 次空格却一次都没上屏（buffer=%r state=%r）"
                      % (self.engine.buffer, self.engine.state))
        self.check_invariants("连选")

    #: (场景方法名, 权重)。存名字而不是函数对象——类体内定义时拿到的是
    #: 未绑定函数，直接放进列表再调用会丢掉 self（踩过：全量 TypeError）。
    SCENARIOS = [
        ("scenario_typing", 30),
        ("scenario_backspace", 14),
        ("scenario_selection", 10),
        ("scenario_focus_switch", 8),
        ("scenario_english_toggle", 10),
        ("scenario_long_sentence", 8),
        ("scenario_typo", 8),
        ("scenario_long_input", 4),
        ("scenario_weird_keys", 5),
        ("scenario_repeated_select", 3),
    ]

    # ------------------------------------------------------------ 主循环

    def run(self, duration):
        weights = [w for _, w in self.SCENARIOS]
        # getattr(self, name) 拿到的是**绑定方法**，self 才会在
        names = [name for name, _ in self.SCENARIOS]
        t0 = time.monotonic()
        last_report = t0
        next_report = t0 + 60.0
        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= duration:
                break
            self.current = []
            self.sequences += 1
            # 场景之间先归零：真实使用中上一句打完状态会自己回空闲，
            # 不归零的话 ST_ENGLISH 会漏进下一个场景，造出「连敲空格不上屏」
            # 这类**假 bug**（踩过一次）。
            self.engine.reset()
            self.engine.on_focus_in()
            seq_name = ""
            seq_t0 = time.monotonic()
            try:
                name = self.rnd.choices(names, weights)[0]
                seq_name = name
                getattr(self, name)()
                self.seq_times.append((name, time.monotonic() - seq_t0))
            except Exception as exc:  # noqa: BLE001
                self.fail("exception",
                          "%s: %s" % (type(exc).__name__, exc))
                # 状态可能已经坏了，重建引擎保证后续序列仍能跑
                self._rebuild()
            dt = time.monotonic() - seq_t0
            if dt > 3.0:
                # 慢序列：单独记一笔。这类问题不会抛异常，只有耗时能暴露。
                rec = {"kind": "slow_sequence", "detail":
                       "%s 用了 %.1f 秒" % (seq_name, dt),
                       "seq": _fmt(self.current[-60:]), "time": time.time()}
                self.bugs.append(rec)
                try:
                    with open(self.out_path, "a", encoding="utf-8") as fp:
                        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass
                print("!! SLOW %s 用了 %.1f 秒" % (seq_name, dt), flush=True)
                print("   %s" % _fmt(self.current[-40:]), flush=True)
            now = time.monotonic()
            # 原来写的是 ``now - next_report >= t0``——把「绝对时刻」和「起始时刻」
            # 混在一起比。结果是第一次打印之后条件恒为真、之后再也不打印，
            # 看起来就像「卡住了」，其实序列一直在跑。踩过一次，白查了半天。
            if now >= next_report:
                rate = self.sequences / max(now - t0, 1e-9)
                print("[%6.0fs] 序列 %7d  按键 %9d  速率 %6.0f 序列/秒  bug %d"
                      % (now - t0, self.sequences, self.keystrokes, rate,
                         len(self.bugs)), flush=True)
                next_report = now + 60.0
        return t0

    def _rebuild(self):
        """重建引擎，让压测能继续跑（否则一次异常就卡死整个测试）。"""
        try:
            from ycpy.config import Config
            self.dictionary = Dictionary()
            self.user = UserModel(os.path.join(self.tmp, "user.json"))
            self.dictionary.apply_user_counts({})
            self.composer = Composer(self.dictionary, self.user)
            self.engine = PinyinEngine(self.dictionary, self.user, self.composer,
                                       config=Config(os.path.join(self.tmp, "config.json")))
            self.engine.ui = RecordingUI()
            self.engine.on_focus_in()
            print("   （已重建引擎）", flush=True)
        except Exception as exc:  # noqa: BLE001
            self.fail("rebuild_failed", "%s: %s" % (type(exc).__name__, exc))


def _fmt(seq):
    out = []
    for item in seq:
        if item[0] == "D":
            out.append("D:%#x/%d" % (item[1], item[2]))
        elif item[0] == "U":
            out.append("U:%#x" % item[1])
        else:
            out.append(item[0])
    return " ".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=18000,
                    help="跑多少秒（默认 5 小时）")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="/tmp/ycpy-stress.jsonl")
    args = ap.parse_args()

    print("ycpy 压力测试（核心层）", flush=True)
    print("  时长 %.0f 秒  种子 %d  结果 %s"
          % (args.duration, args.seed, args.out), flush=True)
    t0 = time.time()
    s = Stressor(args.seed, args.out)
    start = s.run(args.duration)
    dur = time.monotonic() - start

    print("", flush=True)
    print("=" * 60, flush=True)
    print("压测结束：跑了 %.1f 分钟" % (dur / 60), flush=True)
    print("  序列 %d   按键 %d" % (s.sequences, s.keystrokes), flush=True)
    print("  bug %d 条（%d 类）"
          % (len(s.bugs), len(s.seen_kinds)), flush=True)
    if s.seq_times:
        worst = sorted(s.seq_times, key=lambda t: -t[1])[:5]
        print("  最慢的 5 条序列：", flush=True)
        for name, dt in worst:
            print("    %-24s %.2f 秒" % (name, dt), flush=True)
        total = sum(t for _, t in s.seq_times)
        print("  平均每条序列 %.3f 秒" % (total / max(len(s.seq_times), 1)), flush=True)
    for kind in sorted(s.seen_kinds):
        n = sum(1 for b in s.bugs if b.kind == kind)
        first = next(b for b in s.bugs if b.kind == kind)
        print("   - %s (%d 次) %s" % (kind, n, first.detail[:90]), flush=True)
    if not s.bugs:
        print("  ✅ 没发现 bug", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())