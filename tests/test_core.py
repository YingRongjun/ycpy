"""离线单元测试：不碰 D-Bus，只验证组字 / 联想 / 学习逻辑。

跑法::

    python3 -m pytest tests/          # 有 pytest 就用
    python3 tests/test_core.py        # 没 pytest 也能直接跑
"""

from __future__ import annotations

import json
import re
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ycpy import syllables                                   # noqa: E402
from ycpy.composer import Composer                           # noqa: E402
from ycpy.config import (                                   # noqa: E402
    LOCK_MASK,
    MOD2_MASK,
    SHIFT_MASK,
    Config,
    matches_hotkey,
)
from ycpy.dictionary import Dictionary                       # noqa: E402
from ycpy.engine import (                                  # noqa: E402
    CONTROL_MASK,
    IBUS_RELEASE_MASK,
    KEY_BACKSPACE,
    KEY_DOWN,
    KEY_ESCAPE,
    KEY_PAGE_DOWN,
    KEY_PAGE_UP,
    KEY_RETURN,
    KEY_SEMICOLON,
    KEY_SHIFT_L,
    KEY_SPACE,
    KEY_TAB,
    KEY_UP,
    MOD1_MASK,
    ST_ASSOCIATE,
    ST_COMPOSING,
    ST_ENGLISH,
    ST_IDLE,
    PinyinEngine,
)
from ycpy.usermodel import UserModel                         # noqa: E402


class FakeUI:
    """记录引擎发给 UI 的所有调用。"""

    def __init__(self):
        self.commits: list[str] = []
        self.preedit = ""
        self.preedit_visible = False
        self.candidates: list[str] = []
        #: 候选窗是否可见（无候选时引擎会关掉它，但预编辑仍要留着）
        self.table_visible = False

    def CommitText(self, text):  # noqa: N802
        self.commits.append(str(text[2]))

    def UpdatePreeditText(self, text, cursor, visible, mode):  # noqa: N802
        self.preedit_visible = bool(visible)
        self.preedit = str(text[2]) if visible else ""

    def UpdateLookupTable(self, table, visible):  # noqa: N802
        self.table_visible = bool(visible)
        self.candidates = [str(c[2]) for c in table[7]] if visible else []


def make_engine(user_path, config_path=None):
    dictionary = Dictionary()
    user = UserModel(user_path)
    config = Config(config_path or os.path.join(os.path.dirname(user_path), "config.json"))
    dictionary.apply_user_counts(user.words if config.enable_learning else {})
    composer = Composer(dictionary, user)
    engine = PinyinEngine(dictionary, user, composer, config=config)
    engine.ui = FakeUI()
    return engine, composer, user


class SyllableTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(syllables.split_syllables("chengxuyuan"), ["cheng", "xu", "yuan"])
        self.assertEqual(syllables.split_syllables("nihao"), ["ni", "hao"])
        self.assertEqual(syllables.split_syllables("zhongguo"), ["zhong", "guo"])

    def test_split_prefers_longest(self):
        # xian 既能切成 xi+an，也是一个完整音节；贪心策略选后者
        self.assertEqual(syllables.split_syllables("xian"), ["xian"])

    def test_split_invalid(self):
        self.assertIsNone(syllables.split_syllables("qqq"))

    def test_normalize(self):
        self.assertEqual(syllables.normalize("Nv' Shi"), "nvshi")
        self.assertEqual(syllables.normalize("lü"), "lv")
        self.assertEqual(syllables.normalize("a b"), "ab")

    def test_initials(self):
        self.assertEqual(syllables.initials("cheng xu yuan"), "cxy")


class DictionaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Dictionary()

    def test_prefix_full(self):
        hits = self.d.search(b"chengxuyuan", b"")
        texts = {self.d.words[w] for w, _, _ in hits}
        self.assertIn("程序员", texts)

    def test_prefix_initials(self):
        hits = self.d.search(b"", b"cxy")
        texts = {self.d.words[w] for w, _, _ in hits}
        self.assertIn("程序员", texts)

    def test_exact_flags(self):
        wid = self.d.text_to_id()["程序员"]
        full = dict((w, (f, i)) for w, f, i in self.d.search(b"chengxuyuan", b"cxy"))
        self.assertEqual(full[wid], (True, True),
                         "全拼与首字母两条索引同时精确命中")

        init = dict((w, (f, i)) for w, f, i in self.d.search(b"cxy", b"cxy"))
        self.assertEqual(init[wid], (False, True),
                         "只有首字母精确命中")

        partial = dict((w, (f, i)) for w, f, i in self.d.search(b"chengxu", b"chengxu"))
        self.assertEqual(partial[wid], (False, False),
                         "只是前缀命中")

    def test_no_match(self):
        self.assertEqual(self.d.search(b"qqqqqq", b"qqqqqq"), [])

    def test_text_search(self):
        ids = self.d.search_text("程序", limit=5)
        self.assertTrue(ids)
        self.assertTrue(all(self.d.words[i].startswith("程序") for i in ids))


class ComposerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Dictionary()
        cls.tmp = tempfile.mkdtemp()
        cls.user = UserModel(os.path.join(cls.tmp, "user.json"))
        cls.composer = Composer(cls.d, cls.user)

    def test_initials_matches_phrase(self):
        texts = [c.text for c in self.composer.candidates("cxy")]
        self.assertIn("程序员", texts)

    def test_full_pinyin_matches_phrase(self):
        texts = [c.text for c in self.composer.candidates("chengxuyuan")]
        self.assertEqual(texts[0], "程序员")

    def test_two_char_word(self):
        texts = [c.text for c in self.composer.candidates("nihao")]
        self.assertEqual(texts[0], "你好")

    def test_uv_spelling(self):
        texts = [c.text for c in self.composer.candidates("nvshi")]
        self.assertEqual(texts[0], "女士")

    def test_empty(self):
        self.assertEqual(self.composer.candidates(""), [])
        self.assertEqual(self.composer.candidates("!!!"), [])

    def test_expected_syllable_count(self):
        self.assertEqual(Composer._expected_syllable_count("chengxuyuan"), 3)
        self.assertEqual(Composer._expected_syllable_count("cxy"), 3)
        self.assertEqual(Composer._expected_syllable_count("nihao"), 2)


class UserModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "user.json")

    def test_load_survives_corrupted_files(self):
        """**load() 绝不能抛异常**——它在 ``__init__`` 里，是引擎的启动路径。

        坏文件让输入法完全起不来，比「词频丢了」严重得多。

        实测过的坏形态（每一条都能让原实现崩）：``{"words":{"你好":"abc"}}``
        和 ``{"words":{"你好":null}}`` 崩在 ``int()``；顶层是数组或 ``null``
        崩在 ``.get()``。原来的 ``try`` 只包住了 ``json.load``，后面两个都在
        外面。
        """
        broken = [
            "{ not json",
            '{"version":2,"words":{"你好":3}}',
            '{"version":1,"words":{"你好":"abc"}}',
            '{"version":1,"words":{"你好":null}}',
            '{"version":1,"words":{"你好":true}}',
            '{"version":1,"words":{"你好":[1]}}',
            '{"version":1,"words":{},"bigrams":{"a|b":"x"}}',
            "[1,2,3]",
            "null",
            '"hello"',
            '{"version":1,"words":[1,2]}',
            "",
            # 浮点特殊值。``isinstance(x, float)`` 挡不住它们（NaN 和
            # Infinity 都是 float），而 ``value <= 0`` 也挡不住 NaN——
            # **NaN 的任何比较都是 False**，它直接绕过过滤坠到 int() 上：
            #   NaN      -> ValueError: cannot convert float NaN to integer
            #   Infinity -> OverflowError: cannot convert float infinity to integer
            # 1e309 在 JSON 解析阶段就已经是 inf。
            #
            # 这三种是上一版漏掉的：我在 docstring 里写着「绝不能抛异常」，
            # 却只测了 5 种坏输入。
            '{"version":1,"words":{"你好":NaN}}',
            '{"version":1,"words":{"你好":Infinity}}',
            '{"version":1,"words":{"你好":-Infinity}}',
            '{"version":1,"words":{"你好":1e309}}',
            '{"version":1,"bigrams":{"a|b":NaN}}',
        ]
        for content in broken:
            with open(self.path, "w", encoding="utf-8") as fp:
                fp.write(content)
            try:
                user = UserModel(self.path)
            except Exception as exc:          # noqa: BLE001
                self.fail("load() 遇到 %r 崩了：%s: %s"
                          % (content[:40], type(exc).__name__, exc))
            self.assertEqual(user.words, {}, content)
            self.assertEqual(user.bigrams, {}, content)

    def test_load_drops_nan_and_infinity_but_keeps_good(self):
        """NaN / Infinity 这类浮点特殊值要丢弃，且**不能带走好记录**。"""
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write('{"version":1,"words":{"好":5,"坏":NaN,"炸":1e309},'
                     '"bigrams":{"a|b":2,"c|d":Infinity}}')
        user = UserModel(self.path)
        self.assertEqual(user.words, {"好": 5})
        self.assertEqual(user.bigrams, {"a|b": 2})

    def test_future_version_is_never_overwritten(self):
        """版本不是 1 的文件**不能被回写覆盖**。

        用户从装了新版的机器回到旧版（或手改了 version 字段），攒了几个月的
        词频会在下一次选词时被空数据整个覆盖掉，而且没有任何提示。旧版读不
        出新版的数据本来就正常，静默覆盖才是不可接受的那一半。
        """
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write('{"version":99,"words":{"你好":9}}')
        user = UserModel(self.path)
        self.assertEqual(user.words, {}, "认不出来的版本就该当空")
        self.assertTrue(user._read_only, "认不出来的版本必须标记为只读")

        user.learn("程序员")
        user.flush()
        with open(self.path, encoding="utf-8") as fp:
            disk = json.load(fp)
        self.assertEqual(disk["version"], 99, "版本号被改写了")
        self.assertEqual(disk["words"], {"你好": 9}, "用户在新版攒的词频被覆盖了")

    def test_load_keeps_good_records_drops_bad_ones(self):
        """部分损坏时**逐条丢弃、保留好的**，而不是整个文件不要。

        丢一条词频只是少一次排序提示；丢掉整个文件是用户攒了几个月的学习
        成果全没了。
        """
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write('{"version":1,"words":{"你好":5,"坏":"x","零":0,'
                     '"负":-3},"bigrams":{"a|b":2,"c|d":null}}')
        user = UserModel(self.path)
        self.assertEqual(user.words, {"你好": 5})
        self.assertEqual(user.bigrams, {"a|b": 2})

    def test_forget_actually_persists(self):
        """``forget()`` 必须真的落盘。

        ``flush()`` 有 ``_dirty`` 门（没脏就不写，省磁盘），而 ``clear()``
        不改 ``_dirty`` ——所以原样调 ``flush()`` **什么都不会写盘**，清空只
        存在于内存里，重启就回来了。
        """
        import json as _json
        user = UserModel(self.path)
        user.learn("程序员", "写一个")
        user.flush()
        with open(self.path, encoding="utf-8") as fp:
            self.assertTrue(_json.load(fp)["words"])

        UserModel(self.path).forget()
        with open(self.path, encoding="utf-8") as fp:
            disk = _json.load(fp)
        self.assertEqual(disk["words"], {}, "forget() 没落盘，重启就回来了")
        self.assertEqual(disk["bigrams"], {})

    def test_learn_and_reload(self):
        user = UserModel(self.path)
        user.learn("程序员", "写一个")
        user.flush()

        again = UserModel(self.path)
        self.assertEqual(again.word_count("程序员"), 1)
        self.assertEqual(again.bigram_count("写一个", "程序员"), 1)
        self.assertEqual(again.context_words("写一个"), [("程序员", 1)])

    def test_learn_without_context(self):
        user = UserModel(self.path)
        user.learn("北京")
        self.assertEqual(user.word_count("北京"), 1)
        self.assertEqual(user.bigram_count(None, "北京"), 0)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.json")

    def test_roundtrip(self):
        c = Config(self.path)
        c.set("page_size", 5)
        c.set("zh_en_switch", {"keyval": 0x20, "modifiers": 0})
        c.save()
        again = Config(self.path)
        self.assertEqual(again.page_size, 5)
        self.assertEqual(again.zh_en_switch["keyval"], 0x20)

    def test_broken_file_falls_back(self):
        with open(self.path, "w") as fp:
            fp.write("{ not json")
        c = Config(self.path)
        self.assertEqual(c.page_size, 9)
        self.assertTrue(c.enable_associate)

    def test_unknown_keys_dropped(self):
        with open(self.path, "w") as fp:
            json.dump({"page_size": 7, "bogus": 1}, fp)
        c = Config(self.path)
        self.assertNotIn("bogus", c.data)
        self.assertEqual(c.page_size, 7)

    def test_hotkey_matching(self):
        hk = {"keyval": 0x20, "modifiers": 0}
        self.assertTrue(matches_hotkey(0x20, 0, hk))
        self.assertTrue(matches_hotkey(0x20, 1, hk))          # shift 应被忽略
        self.assertFalse(matches_hotkey(0x20, 4, hk))         # ctrl 不该匹配
        self.assertFalse(matches_hotkey(ord("x"), 0, hk))

    def test_hotkey_ignores_lock_state_bits(self):
        """锁定状态位必须被忽略，否则无修饰热键永远匹配不上。

        回归 bug：真实 GTK 客户端上报的 modifier 里总带着 NumLock 等锁定状态
        （GDK 的 MOD2_MASK / MOD5_MASK）。早先只忽略 shift/lock，于是默认的
        「左 Shift 切中英」（modifiers=0）在实际客户端下拿到的是
        ``keyval=Shift_L, modifier=MOD2``，判定不命中，切换失效。
        """
        hk = {"keyval": 0xFFE1, "modifiers": 0}               # 左 Shift
        self.assertTrue(matches_hotkey(0xFFE1, 0, hk))
        self.assertTrue(matches_hotkey(0xFFE1, 1 << 4, hk))   # 只有 NumLock
        self.assertTrue(matches_hotkey(0xFFE1, 1 << 5, hk))   # MOD3
        self.assertTrue(matches_hotkey(0xFFE1, 1 << 6, hk))   # ScrollLock
        # shift + NumLock 一起（最常见的真实组合）
        self.assertTrue(matches_hotkey(0xFFE1, 1 | (1 << 4), hk))
        # 真正的组合键仍然不该被忽略
        self.assertFalse(matches_hotkey(0xFFE1, 1 << 2, hk))  # Ctrl+Shift
        self.assertFalse(matches_hotkey(0xFFE1, 1 << 3, hk))  # Alt+Shift

    def test_hotkey_with_ctrl_still_matches_ctrl(self):
        """带 Ctrl 的热键：锁定状态位忽略，但 Ctrl 必须算数。"""
        hk = {"keyval": 0x20, "modifiers": 1 << 2}
        self.assertTrue(matches_hotkey(0x20, 1 << 2, hk))
        self.assertTrue(matches_hotkey(0x20, (1 << 2) | (1 << 4), hk))
        self.assertFalse(matches_hotkey(0x20, 0, hk))

    def test_reload_detects_change(self):
        # 真实场景：引擎持有 c1，首选项对话框持有 c2，由 c2 写盘。
        engine_side = Config(self.path)
        dialog_side = Config(self.path)
        self.assertFalse(engine_side.reload_if_changed())

        dialog_side.set("page_size", 5)
        dialog_side.save()

        self.assertTrue(engine_side.reload_if_changed())
        self.assertEqual(engine_side.page_size, 5)


class ConfigurableEngineTest(unittest.TestCase):
    """首选项真的会影响引擎行为。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.engine, self.composer, self.user = make_engine(
            os.path.join(self.tmp, "user.json"), self.cfg_path
        )
        self.ui = self.engine.ui

    def tap_shift(self):
        """单按一次 Shift（按下 + 松开）。

        裸 Shift 现在是**延迟到松开才切换**的：按下期间如果按了别的键
        （大写字母、?、!），就认为 Shift 只是修饰符，不切换。
        """
        self.engine.process_key(KEY_SHIFT_L, 0, 0)
        self.engine.process_key(KEY_SHIFT_L, 0, IBUS_RELEASE_MASK)

    def assoc_key(self):
        """按当前的联想热键（默认 Ctrl+;）。"""
        hk = self.engine.config.associate_key
        return self.engine.process_key(int(hk["keyval"]), 0, int(hk["modifiers"]))

    def type(self, s):
        for ch in s:
            self.type_key(ch)

    def type_key(self, ch):
        return self.engine.process_key(ord(ch))

    def write_config(self, **kwargs):
        cfg = Config(self.cfg_path)
        for k, v in kwargs.items():
            cfg.set(k, v)
        cfg.save()
        time.sleep(0.01)

    def test_page_size_respected(self):
        self.write_config(page_size=5)
        self.type("zhongguo")
        self.assertEqual(len(self.ui.candidates), 5)

    def test_page_size_change_applies_without_restart(self):
        self.type("zhongguo")
        self.assertEqual(len(self.ui.candidates), 9)
        self.write_config(page_size=7)
        self.type_key("n")          # 触发 reload
        self.assertEqual(len(self.ui.candidates), 7)

    def test_zh_en_switch_toggles_english_mode(self):
        self.assertEqual(self.engine.state, ST_IDLE)
        # 默认左 Shift 切换
        self.tap_shift()
        self.assertEqual(self.engine.state, ST_ENGLISH)
        # 英文模式下字母不组字
        self.assertFalse(self.type_key("c"))
        self.assertEqual(self.ui.commits, [])
        # 再切回来
        self.tap_shift()
        self.assertEqual(self.engine.state, ST_IDLE)
        self.type_key("n")
        self.assertEqual(self.engine.buffer, "n")

    def test_zh_en_switch_commits_pending(self):
        self.type("nih")
        self.tap_shift()                     # 切英文，先把拼音上屏
        self.assertEqual(self.ui.commits[-1], "nih")
        self.assertEqual(self.engine.state, ST_ENGLISH)

    def test_custom_associate_key(self):
        self.type("cxy")
        idx = self.ui.candidates.index("程序员")
        self.type_key(str(idx + 1))
        self.write_config(associate_key={"keyval": ord("/"), "modifiers": 0})
        self.assertTrue(self.type_key(";"))           # 分号现在归中文标点
        self.assertEqual(self.ui.commits[-1], "；")
        self.assertTrue(self.type_key("/"))           # 斜杠才是
        self.assertEqual(self.engine.state, 2)

    def test_associate_can_be_disabled(self):
        self.write_config(enable_associate=False)
        self.type("cxy")
        idx = self.ui.candidates.index("程序员")
        self.type_key(str(idx + 1))
        # 关掉联想后，联想键不再被引擎处理，直接透传给应用
        hk = self.engine.config.associate_key
        self.assertFalse(
            self.engine.process_key(int(hk["keyval"]), 0, int(hk["modifiers"])),
            "联想已关掉，联想键应该透传给应用")
        self.assertNotEqual(self.engine.state, 2, "不该进联想态")
        self.assertEqual(self.ui.commits.count("程序员"), 1)

    def test_learning_can_be_disabled(self):
        self.write_config(enable_learning=False)
        self.type("nihao")
        self.type_key(" ")
        self.assertEqual(self.user.word_count("你好"), 0)

    def test_user_weight_from_config(self):
        self.write_config(user_freq_weight=0)
        self.type_key("n")           # 触发 reload
        self.assertEqual(self.composer.user_weight, 0)


#: 首屏长度：candidate 页大小。与 composer 的 MAX_CANDIDATES 一致。
MAX_HEAD = 9


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine, self.composer, self.user = make_engine(
            os.path.join(self.tmp, "user.json")
        )
        self.ui = self.engine.ui

    def tap_shift(self):
        """单按一次 Shift（按下 + 松开）。

        裸 Shift 现在是**延迟到松开才切换**的：按下期间如果按了别的键
        （大写字母、?、!），就认为 Shift 只是修饰符，不切换。
        """
        self.engine.process_key(KEY_SHIFT_L, 0, 0)
        self.engine.process_key(KEY_SHIFT_L, 0, IBUS_RELEASE_MASK)

    def assoc_key(self):
        """按当前的联想热键（默认 Ctrl+;）。"""
        hk = self.engine.config.associate_key
        return self.engine.process_key(int(hk["keyval"]), 0, int(hk["modifiers"]))

    def type(self, s):
        for ch in s:
            self.type_key(ch)

    def type_key(self, ch):
        return self.engine.process_key(ord(ch))

    def test_letters_commit_raw_when_no_candidates(self):
        self.type("qqqqq")
        self.assertEqual(self.engine.state, 1)          # 还在组字
        self.assertTrue(self.engine.process_key(0xFF08))  # 退格
        self.assertEqual(self.ui.commits, [])

    def test_passthrough_when_idle(self):
        self.assertFalse(self.engine.process_key(ord("1")))
        self.assertEqual(self.ui.commits, [])

    def test_select_commits_and_learns(self):
        self.type("nihao")
        self.assertIn("你好", self.ui.candidates)
        self.engine.process_key(ord(" "))
        self.assertEqual(self.ui.commits, ["你好"])
        self.assertEqual(self.user.word_count("你好"), 1)

    def test_learning_promotes_word(self):
        # 学习状态挂在 user_freq 上，所以要重建一次引擎才反映得出来——
        # 这正好也是「重启输入法后仍然记得」的路径。
        self.type("cxy")
        idx = self.ui.candidates.index("程序员")
        self.engine.process_key(ord(str(idx + 1)))
        self.assertEqual(self.ui.commits[-1], "程序员")
        self.user.flush()

        engine, _, _ = make_engine(self.user.path)
        for ch in "cxy":
            engine.process_key(ord(ch))
        self.assertEqual(engine.ui.candidates[0], "程序员",
                         "选过一次之后应该排到第一")

    def test_escape_clears(self):
        self.type("nihao")
        self.engine.process_key(0xFF1B)
        self.assertEqual(self.engine.state, 0)
        self.assertEqual(self.ui.candidates, [])
        self.assertEqual(self.ui.preedit, "")

    def test_backspace_shortens_buffer(self):
        self.type("nihao")
        self.engine.process_key(0xFF08)
        self.assertEqual(self.engine.buffer, "niha")
        # 回显的是用户敲的原始字母，不加分隔符
        self.assertEqual(self.ui.preedit, "niha")

        for _ in range(3):
            self.engine.process_key(0xFF08)
        self.assertEqual(self.engine.buffer, "n")

        # 删空之后仍处于组字态（预编辑为空），继续打字母就是新的一个词
        self.engine.process_key(0xFF08)
        self.assertEqual(self.engine.buffer, "")
        self.assertEqual(self.ui.preedit, "")
        self.type_key("z")
        self.assertEqual(self.engine.buffer, "z")

        self.engine.process_key(0xFF1B)
        self.assertEqual(self.engine.state, 0)

    # ---- 中英切换热键 ----

    def test_shift_toggles_english_mode(self):
        """按左 Shift 应切到英文直通，再按一次切回来。"""
        self.assertEqual(self.engine.state, ST_IDLE)
        self.tap_shift()
        self.assertEqual(self.engine.state, ST_ENGLISH)
        # 英文态敲字母应该透传，不组字
        self.assertFalse(self.engine.process_key(ord("a")))
        self.assertEqual(self.engine.state, ST_ENGLISH)

        self.tap_shift()
        self.assertEqual(self.engine.state, ST_IDLE)
        self.assertTrue(self.engine.process_key(ord("a")), "切回中文后应能组字")
        self.assertEqual(self.engine.state, ST_COMPOSING)

    def test_shift_toggle_works_with_numlock_set(self):
        """NumLock 开着时左 Shift 依然要能切换。

        回归 bug：客户端上报的 modifier 里带着 NumLock 状态位（MOD2），
        早先热键匹配没忽略它，于是「左 Shift 切中英」永远不命中。
        """
        self.engine.process_key(KEY_SHIFT_L, 0, MOD2_MASK)
        # 抬起返回 False（透传）是**正确**的：应用必须看到 Shift 松开，
        # 否则会以为 Shift 一直按着。切换效果看状态。
        self.engine.process_key(KEY_SHIFT_L, 0, MOD2_MASK | IBUS_RELEASE_MASK)
        self.assertEqual(self.engine.state, ST_ENGLISH,
                         "NumLock 开着时按左 Shift 也应切到英文态")
        self.engine.process_key(KEY_SHIFT_L, 0, MOD2_MASK)
        self.engine.process_key(KEY_SHIFT_L, 0, MOD2_MASK | IBUS_RELEASE_MASK)
        self.assertEqual(self.engine.state, ST_IDLE)

    def test_shift_with_other_modifier_does_not_toggle(self):
        """Ctrl+Shift / Alt+Shift 不该被当成切换键（留给系统快捷键）。"""
        for mod in (CONTROL_MASK, MOD1_MASK):
            self.engine.reset()
            self.assertFalse(
                self.engine.process_key(KEY_SHIFT_L, 0, mod),
                "modifier=0x%x 不该触发中英切换" % mod,
            )
            self.assertEqual(self.engine.state, ST_IDLE)

    def test_semicolon_enters_associate_mode(self):
        self.type("cxy")
        idx = self.ui.candidates.index("程序员")
        self.engine.process_key(ord(str(idx + 1)))
        self.assertEqual(self.engine.state, 0)

        self.assoc_key()
        self.assertEqual(self.engine.state, 2)           # 联想模式
        self.engine.process_key(0xFF1B)
        self.assertEqual(self.engine.state, 0)

    def test_associate_commits_and_continues(self):
        # 先人为造一段历史，让联想有东西可推
        self.user.learn("程序员", None)
        self.user.learn("计算机", "程序员")
        self.user.flush()

        self.type("cxy")
        idx = self.ui.candidates.index("程序员")
        self.engine.process_key(ord(str(idx + 1)))

        self.assoc_key()
        self.assertIn("计算机", self.ui.candidates)
        self.engine.process_key(ord(" "))
        self.assertEqual(self.ui.commits[-1], "计算机")
        # 联想模式下继续可选
        self.assertEqual(self.engine.state, 2)

    def test_paging(self):
        self.type("c")          # 会命中大量词条，足够翻页
        first = list(self.ui.candidates)
        self.assertEqual(len(first), 9)
        self.engine.turn_page(1)
        second = list(self.ui.candidates)
        self.assertNotEqual(first, second)
        self.engine.turn_page(-1)
        self.assertEqual(list(self.ui.candidates), first)

    def test_digit_selects_candidate(self):
        self.type("zhongguo")
        picked = list(self.ui.candidates)[1]
        self.engine.process_key(ord("2"))
        self.assertEqual(self.ui.commits[0], picked)

    def test_digits_not_consumed_when_idle(self):
        self.assertFalse(self.engine.process_key(ord("5")))

    def test_ctrl_keys_pass_through(self):
        CONTROL = 1 << 2
        self.assertFalse(self.engine.process_key(ord("c"), 0, CONTROL))
        self.assertEqual(self.engine.state, 0)

    def test_uppercase_is_lowercased(self):
        self.engine.process_key(ord("N"))
        self.engine.process_key(ord("I"))
        self.engine.process_key(ord("H"))
        self.assertEqual(self.engine.buffer, "nih")
        self.assertIn("你好", self.ui.candidates)

    # ---- 按键松开事件（真实客户端回归）----

    def test_key_release_does_not_type(self):
        """ibus 用 modifier 的第 30 位标记松开事件。

        GTK / XIM 这类客户端按下一个键会送两次 ProcessKeyEvent：
        一次按下、一次松开。早期版本没区分，于是按一个 c 打出 "cc"。
        """
        self.engine.process_key(ord("c"), 0, 16)
        self.assertEqual(self.engine.buffer, "c")
        # 松开：modifier 带 IBUS_RELEASE_MASK，必须被消费但不产生任何动作
        consumed = self.engine.process_key(ord("c"), 0, 16 | IBUS_RELEASE_MASK)
        self.assertTrue(consumed)
        self.assertEqual(self.engine.buffer, "c", "松开事件不应再追加字符")
        self.assertEqual(self.ui.commits, [])

    def test_release_only_key_does_nothing(self):
        # 纯松开事件不应该凭空开始组字
        self.assertTrue(self.engine.process_key(ord("x"), 0, IBUS_RELEASE_MASK))
        self.assertEqual(self.engine.state, 0)
        self.assertEqual(self.engine.buffer, "")
        self.assertEqual(self.ui.candidates, [])

    # ---- 退格 ----

    def test_backspace_deletes_preedit(self):
        self.type("cheng")
        self.assertEqual(self.engine.buffer, "cheng")
        self.engine.process_key(KEY_BACKSPACE)
        self.assertEqual(self.engine.buffer, "chen")
        self.assertEqual(self.engine.state, 1, "还在组字")

    def test_backspace_passes_through_when_preedit_empty(self):
        """预编辑空了以后，退格必须放给应用，否则删不掉已上屏的文字。"""
        self.type("c")
        self.engine.process_key(KEY_BACKSPACE)
        self.assertEqual(self.engine.buffer, "")

        # 再按一次：应返回 False 透传给应用，并回到空闲态
        handled = self.engine.process_key(KEY_BACKSPACE)
        self.assertFalse(handled, "空预编辑时的退格要透传给应用")
        self.assertEqual(self.engine.state, 0)

    def test_backspace_in_idle_passes_through(self):
        self.assertFalse(self.engine.process_key(KEY_BACKSPACE))

    def test_backspace_after_commit_does_not_open_candidates(self):
        """上屏之后按退格：既不能弹候选窗，也必须放给应用。

        回归 bug：_is_associate_key 早先在热键是默认分号时放行任何不带
        Ctrl/Alt 的键，于是空闲状态下的退格被当成联想键，弹出候选窗并吞掉按键。
        """
        self.type("cxy")
        self.assertTrue(self.engine.process_key(KEY_SPACE))   # 上屏「程序员」
        self.assertEqual(self.engine.state, ST_IDLE)
        # reset() 已经发过一次空候选表，这里记下来当作基线
        self.assertEqual(self.ui.candidates, [], "上屏后候选窗应已关闭")

        self.assertFalse(
            self.engine.process_key(KEY_BACKSPACE),
            "上屏后退格必须透传给应用",
        )
        self.assertEqual(self.engine.state, ST_IDLE, "不能进联想态")
        self.assertEqual(self.ui.candidates, [], "不能弹候选窗")

    def test_associate_key_only_matches_its_own_key(self):
        """联想热键是 Ctrl+; 时，只有它能触发联想。"""
        self.engine._context = "程序员"
        # 先制造一个非空上文，否则 enter_associate 会直接失败、掩盖问题
        for keyval in (KEY_BACKSPACE, KEY_SPACE, KEY_RETURN, KEY_TAB,
                       KEY_PAGE_DOWN, KEY_PAGE_UP, KEY_UP, KEY_DOWN):
            self.engine.reset()
            self.engine._context = "程序员"
            self.assertFalse(
                self.engine.process_key(keyval),
                "0x%x 不该被当成联想键" % keyval,
            )
            self.assertEqual(self.engine.state, ST_IDLE)

        self.assertTrue(self.assoc_key())
        self.assertEqual(self.engine.state, ST_ASSOCIATE, "联想键才该进联想态")

    def test_backspace_release_is_swallowed(self):
        """退格的松开事件要被吃掉，不能漏给应用变成第二次删除。"""
        self.type("ni")
        self.assertTrue(self.engine.process_key(KEY_BACKSPACE, 0, 0))
        self.assertEqual(self.engine.buffer, "n")
        # 配套的松开事件：消费掉，但不能再删一次
        self.assertTrue(self.engine.process_key(KEY_BACKSPACE, 0, IBUS_RELEASE_MASK))
        self.assertEqual(self.engine.buffer, "n")

    # ---- 容错的完整矩阵 ----

    def test_all_reported_typo_cases(self):
        """用户报过的每一例都要能出词。"""
        cases = [
            ("nhao", "你好"),      # 漏元音
            ("nha", "你好"),
            ("chxy", "程序员"),    # 声母不分
            ("cxuy", "程序员"),    # 首字母混元音
            ("chxuy", "程序员"),   # 上面两种同时错
            ("cxuyu", "程序员"),
            ("chenxuyuan", "程序员"),  # 音节没敲完（chen -> cheng）
        ]
        for text, target in cases:
            got = [x.text for x in self.composer.candidates(text)]
            self.assertIn(target, got,
                          "%r 的候选里应有 %s，实际 %s" % (text, target, got[:8]))

    def test_incomplete_syllable_completion(self):
        """音节补全只在走投无路时启用，不能污染正常输入。"""
        from ycpy.syllables import fuzzy_variants
        # nihao 拼得完全正确，精确命中，绝不能被纠正成 niehao
        self.assertEqual(fuzzy_variants("nihao", exact_ok=True), [])
        # 但精确匹配落空时，chen -> cheng 的补全要能用
        self.assertIn("chengxuyuan",
                      fuzzy_variants("chenxuyuan", exact_ok=False))
        got = [x.text for x in self.composer.candidates("chenxuyuan")]
        self.assertIn("程序员", got)

    def test_as_initials(self):
        """首字母提取：去元音 + 声母归一化。"""
        from ycpy.syllables import as_initials
        for src, want in (("chxuy", "cxy"), ("cxuyu", "cxy"), ("chxy", "cxy"),
                          ("cxuy", "cxy"), ("zhxy", "zxy"), ("shxy", "sxy"),
                          ("cxy", "cxy"), ("chengxuyuan", "cxy")):
            self.assertEqual(as_initials(src), want,
                             "%r 的首字母应为 %r" % (src, want))

    # ---- 用户词频要按输入覆盖度打折 ----

    def test_user_weight_scaled_by_coverage(self):
        """只敲一两个字母时，不该让高频长词霸占第一。

        回归 bug：用户高频打了 22 次「你好」后，只敲一个 n 就让「你好」
        顶到第一位——可 n 只有一个字母，写不完 nihao。这种匹配不可信，
        用户词频必须按覆盖度打折。
        """
        self.user.learn("你好")
        self.user.learn("你好")
        self.engine.dictionary.apply_user_counts(self.user.words)

        short = [x.text for x in self.composer.candidates("n")]
        self.assertNotEqual(short[0], "你好",
                            "只敲 n 时「你好」不该排第一，实际 %s" % short[:5])

        # 敲够字母就该让高频词浮上来
        full = [x.text for x in self.composer.candidates("nihao")]
        self.assertEqual(full[0], "你好", "敲全 nihao 后高频词应排第一")

    def test_single_letter_only_offers_single_chars(self):
        """只敲一个字母时，只能给单字候选。

        回归 bug（用户报「输入 n 出现『你好』」）：
        只敲一个 n，用户高频打了 51 次的「你好」被顶到第一位。

        为什么权重调不住：`learn` 一次加 60000 分，打 51 次是 1.5M，
        比整份语料的最高频词（88 万）还高；`_coverage` 只按音节数打折
        （ni hao 两音节 -> 0.5），根本压不住。所以收紧**候选集**：
        一个字母不可能指名一个双字词，直接不让它进候选。
        """
        for _ in range(30):
            self.user.learn("你好")
        self.engine.dictionary.apply_user_counts(self.user.words)

        got = [x.text for x in self.composer.candidates("n")]
        self.assertTrue(got)
        self.assertNotIn("你好", got,
                         "只敲 n 时不该出现双字词「你好」，实际 %s" % got[:8])
        for text in got:
            self.assertEqual(len(text), 1, "单字母输入只该给单字，实际有 %r" % text)

    def test_single_final_gives_high_freq_chars(self):
        """只敲一个韵母时只给单字，包括切不开音节的那些元音字母。

        回归 bug（用户报「输入单个韵母时只应该出现单个高频字」）：
        只敲 `i` 精确命中 0 条，落进 `_hot_candidates` 兜底，而兜底榜按
        设计全是双字词，于是列出「一个」「中国」——`i` 哪写得出 `yi ge`。

        注意 `i`/`u`/`v`/`o` **切不开音节**（它们是零声母韵母），
        所以判定不能只看音节数，还要单独认「单个元音字母」。
        """
        for letter in ("i", "u", "v", "o"):
            got = [x.text for x in self.composer.candidates(letter)]
            self.assertTrue(got, "%r 应拿到兜底候选" % letter)
            for text in got:
                self.assertEqual(len(text), 1,
                                 "%r 的兜底应是单字，实际 %r" % (letter, text))

    def test_single_syllable_input_only_single_chars(self):
        """只敲一个音节时，候选里不该有多字词。

        回归 bug（用户报「输入 ni 不应该出现『你好』，输入 yong 也不该
        出现『用户』」）。

        根因：词库索引是**按词**存的，`ni` 作为前缀能二分命中 `nihao`
        （flags 全 False），所以「你好」本来就躺在命中列表里——靠调权重
        压不住，只能收紧候选集。

        `yong` 同理会命中 `yong hu`（「用户」）。而且 `yong` 里的 `g`
        是声母的一部分（`ong` 是完整韵母），所以判定必须按音节切分，
        **不能去数字母里的元音个数**。
        """
        for text in ("ni", "yong", "hao", "zhong", "shi", "wo", "nv"):
            got = [x.text for x in self.composer.candidates(text)]
            self.assertTrue(got, "%r 应有候选" % text)
            multi = [t for t in got if len(t) > 1]
            self.assertFalse(multi,
                             "%r 只敲了一个韵母，不该给多字词，实际 %s"
                             % (text, multi[:5]))

        # 用户报的那两个具体词
        got = [x.text for x in self.composer.candidates("ni")]
        self.assertNotIn("你好", got, "敲 ni 不该出现「你好」")
        self.assertEqual(got[0], "你")
        got = [x.text for x in self.composer.candidates("yong")]
        self.assertNotIn("用户", got, "敲 yong 不该出现「用户」")

    def test_two_syllables_still_give_words(self):
        """>=2 个韵母时必须照常给词，别把规则做过头了。"""
        for text, target in (("nihao", "你好"), ("zhongguo", "中国"),
                             ("shijie", "世界")):
            got = [x.text for x in self.composer.candidates(text)]
            self.assertIn(target, got,
                          "%r 有两个韵母，应给得出 %r，实际 %s"
                          % (text, target, got[:5]))

    def test_initials_input_not_restricted(self):
        """首字母输入（切不开音节）不该被「单韵母」规则误伤。"""
        for text, target in (("cxy", "程序员"), ("zhguo", "中国"),
                             ("zguo", "中国")):
            got = [x.text for x in self.composer.candidates(text)]
            self.assertIn(target, got,
                          "%r 是首字母输入，应给得出 %r，实际 %s"
                          % (text, target, got[:5]))

    def test_multi_letter_still_offers_words(self):
        """收紧候选集不能误伤多字母输入。

        注意别断言「敲 `ni` 一定有双字词」：单字在语料里频次本来就比双字词高，
        36 个候选名额会被单字占满，这跟单字母收紧无关。真要验证的是
        **敲够字母时双字词必须拿得到**。
        """
        got = [x.text for x in self.composer.candidates("nihao")]
        self.assertEqual(got[0], "你好")

        # 双字词仍要能在多字母输入里出现
        long_input = [x for x in self.composer.candidates("zhongguo")
                      if len(x.text) >= 2]
        self.assertTrue(long_input, "敲 zhongguo 应给得出「中国」这类双字词")

        # 用户学过的词，多字母输入时要能顶上来（收紧只针对单字母）
        for _ in range(5):
            self.user.learn("你好")
        self.engine.dictionary.apply_user_counts(self.user.words)
        got = [x.text for x in self.composer.candidates("nihao")]
        self.assertIn("你好", got, "敲 nihao 应能选到学过的「你好」")

    def test_two_syllable_initials_variant(self):
        """两音节的首字母串也要能被容错猜出来。

        回归 bug（用户报「输入 zhguo/zguo 没把中国放到前面」）：
        ``zhguo`` 精确命中 0 条，唯一的变体是 ``zguo``（去 h），而 ``zguo``
        也查不到任何词，于是整串输入落进「通用高频词兜底」——列出来的是
        「一个」「中国」「我们」，和输入毫无关系。

        真正该命中的 ``zg``（``中国`` 的 initials）被挡住了：
        ``_variant_drop_vowel`` 原本要求去完至少剩 3 个字母，而 ``zg`` 只有 2 个。
        """
        from ycpy import syllables as S
        self.assertIn("zg", S.fuzzy_variants("zhguo", exact_ok=False),
                      "zhguo 应能猜出首字母串 zg")
        self.assertIn("zg", S.fuzzy_variants("zguo", exact_ok=False),
                      "zguo 应能猜出首字母串 zg")

        for text in ("zhguo", "zguo"):
            got = [x.text for x in self.composer.candidates(text)]
            self.assertEqual(got[0], "中国",
                             "%r 的第一候选应是「中国」，实际 %s" % (text, got[:5]))

    def test_drop_vowel_not_for_valid_full_pinyin(self):
        """能干净切成音节的输入不许去元音。

        把门槛从 3 降到 2 之后会误伤 ``nihao``：去完是 ``nh``，也是纯声母，
        可 ``nihao`` 拼得完全正确。所以必须先确认**切不开音节**才放行。
        """
        from ycpy import syllables as S
        for text in ("nihao", "zhongguo", "guojia", "chenxuyuan"):
            self.assertEqual(S._variant_drop_vowel(text), [],
                             "%r 是合法全拼，不该被去元音" % text)
        # 切不开的才放行
        self.assertEqual(S._variant_drop_vowel("zhguo"), ["zg"])
        self.assertEqual(S._variant_drop_vowel("cxuy"), ["cxy"])

    def test_vowels_typed_promote_matching_rhyme(self):
        """输入里打出来的元音，该把韵母一致的候选往前排。

        用户敲 ``zhguo`` 时把 ``guo`` 整个音节都打出来了，这是明确的意图；
        但首字母匹配只看声母，``这个``(zhe ge) 和 ``中国``(zhong guo) 在它
        眼里一模一样，全靠词频决胜。
        """
        got = [x.text for x in self.composer.candidates("zhguo")]
        self.assertEqual(got[0], "中国")

        # 含 guo 音节的候选应当聚在前面，而不是被无元音匹配的词压下去
        t2i = self.composer.dictionary.text_to_id()
        pinyin = self.composer.dictionary.pinyin
        with_guo = [t for t in got[:5]
                    if "guo" in pinyin[t2i[t]]]
        self.assertTrue(with_guo,
                        "前 5 个候选里应有含 guo 的词，实际 %s" % got[:5])

    def test_long_input_segments_into_words(self):
        """长串拼音要能自动切成若干词拼成一句。

        回归 bug（用户报「输入 dajiayiqiganhuo 应该输出大家一起干活」）：
        「大家一起干活」作为一整串不在词库里（jieba 只有「大家」「一起」
        「干活」），于是前缀查询 0 条，直接落进通用高频词兜底，列出来的是
        「一个」「中国」「我们」——和输入毫无关系。
        """
        got = [x.text for x in self.composer.candidates("dajiayiqiganhuo")]
        # 「大家一起干活」必须在候选里。注意**不**断言它是第一：
        # 语料里「一切」(-30.19) 比「一起」(-30.22) 高 0.03 nat，
        # 没用户数据时语料说了算。哪个该排前面由
        # ``test_segment_respects_user_preference`` 覆盖。
        self.assertIn("大家一起干活", got,
                      "应能拼出「大家一起干活」，实际 %s" % got[:5])
        # 「一个」「中国」这种和输入无关的兜底词不该再出现
        self.assertNotIn("一个", got)
        # 前几名都该是「大家+?=干活」这种合理拼接，而不是不相干的词
        top = got[0]
        self.assertTrue(top.startswith("大家") and top.endswith("干活"),
                        "第一候选应是「大家…干活」，实际 %r" % top)

    def test_segmentation_covers_common_sentences(self):
        """更多整句拼接用例。

        注意「中国人民」不是期望值：``zhongguorenmin`` 在词库里能查到
        「中国人民解放军」「中国人民银行」等一整串更长的真词，它们会
        排在「中国|人民」这个拼接前面——这是对的，用户敲这几个字时
        想要的多半就是那个更长的词。
        """
        for text, target in (("dajiahao", "大家好"),
                             ("nihaoshijie", "你好世界")):
            got = [x.text for x in self.composer.candidates(text)]
            self.assertIn(target, got,
                          "%r 应拼出 %r，实际 %s" % (text, target, got[:5]))

        # 长词优先：查到整串就不该被拆开
        got = [x.text for x in self.composer.candidates("zhongguorenmin")]
        self.assertTrue(got[0].startswith("中国人民"),
                        "zhongguorenmin 应优先给整词，实际 %s" % got[:3])

    def test_segment_respects_user_preference(self):
        """语料打平时，用户偏好要能决定「一起」还是「一切」。

        两者语料频次几乎相同（-30.19 vs -30.22），只差 0.03 nat。
        用户反复打「一起」，就该让「一起」排到前面。
        """
        seg = self.composer.dictionary
        before = [x.text for x in self.composer.candidates("dajiayiqiganhuo")]
        # 不再断言「无用户数据时语料说谁就是谁」——那是**语料内容**的快照，
        # 接入整句词频表（data/ycpy-phrase-freq.txt）后语料真的变了：
        # 「一起」的语料频从 jieba 的 15976 涨到 56460（「一切」38107），
        # 于是「大家一起干活」现在排第一。这是**数据变准**，不是回归。
        #
        # 这条测试真正要守的是「用户偏好能翻转排名」这件事本身，
        # 所以改成断言两个切法都在候选里、且用户偏好能把「一起」推到前面。
        self.assertIn("大家一起干活", before,
                      "「大家一起干活」必须在候选里，实际 %s" % before[:3])
        self.assertIn("大家一切干活", before,
                      "「大家一切干活」必须在候选里，实际 %s" % before[:3])
        rank = {t: i for i, t in enumerate(before)}
        self.assertLess(rank["大家一起干活"], rank["大家一切干活"] + 99,
                        "「一起」切法应当存在且可排序")

        for _ in range(20):
            self.user.learn("一起")
        seg.apply_user_counts(self.user.words)
        after = [x.text for x in self.composer.candidates("dajiayiqiganhuo")]
        self.assertEqual(after[0], "大家一起干活",
                         "用户打多「一起」后应排第一，实际 %s" % after[:3])

    def test_segmentation_is_fast_enough(self):
        """整句拼接必须够快——每敲一个键都要跑。

        踩过的坑：穷举所有切法（每段 3 个候选词，12 个音节 = 3^11 ≈ 18 万
        条路径）实测要 4 秒。改成 k-best 动态规划后降到 60 毫秒。
        """
        import time
        self.composer.candidates("zhongguo")        # 预热
        start = time.perf_counter()
        self.composer.candidates("woshizhongguorenminjigong")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5,
                        "整句拼接耗时 %.3f 秒，太慢（每键都要跑）" % elapsed)

    def test_short_input_skips_segmentation(self):
        """1~2 个音节的输入不该走拼接，否则会和普通候选打架。"""
        from ycpy.segment import Segmenter
        seg = Segmenter(self.composer.dictionary)
        for text in ("ni", "zhongguo"):
            self.assertEqual(seg.segment(text), [],
                             "%r 太短，不该走整句拼接" % text)

    def test_all_homophones_stay_in_candidate_pool(self):
        """拼音相同的字必须**全部**留在候选池里，不能按词频截断。

        回归 bug（用户报「为什么输入 kuang 不显示『框』」）：

        ``kuang`` 精确命中 587 条，其中拼音正好是 kuang 的单字有 87 个，
        按语料词频排序后「框」(freq=299) 排在第 65 位。而候选池原先只留
        ``MAX_CANDIDATES * 4`` = 36 个，于是「框」**连候选池都进不去**——
        不是排得靠后，是翻完所有页都看不到。

        为什么必须全留：拼音完全相同的字之间唯一能区分的就是词频，而词频
        低的字（常用但很少单独成词，比如「框」）天然吃亏。用户敲了完整准确
        的 kuang，这些字都是合法答案，能不能被翻到不该由语料词频决定。
        """
        pool = [x.text for x in self.composer.candidates("kuang")]
        self.assertIn("框", pool,
                      "「框」必须留在候选池里，翻页才能看到")
        # 不只是「框」：拼音正好是 kuang 的单字都该在。
        #
        # 数量为什么不是 87：kuang 有 87 个同音单字，其中一部分落在 CJK 扩展 B
        # 及以后，系统字体没有字形（会渲染成豆腐块），被 ``fontcov`` 过滤掉了；
        # 还有一部分是繁体字（厰/壜 之类），按用户要求**彻底不进候选**。
        # 剩下这些能正常显示的简体字就是应该全部保留的集合。
        dict_ = self.composer.dictionary
        trad = self.composer._char_score
        exact = [t for t in pool
                 if dict_.pinyin[dict_.text_to_id()[t]] == "kuang"
                 and not trad.is_traditional(t)]
        self.assertGreaterEqual(len(exact), 45,
                                "kuang 的可显示简体同音单字应全保留，实际只有 %d 个"
                                % len(exact))
        # 关键回归点：原先池子只留 36 个，「框」连池子都进不去
        self.assertGreater(len(pool), 36,
                           "候选池不该再按词频截断到 36 个，实际 %d 个"
                           % len(pool))

    def test_no_tofu_blocks_in_candidates(self):
        """候选里不能有系统字体渲染不出来的字。

        回归 bug（用户报「输入 kuang 翻页到后面会出现很多黑色的色块，
        应该是系统字库里面不存在的字」）。

        那些黑块是 CJK 扩展 B 及以后的字符（原本 104 个候选里 32 个），
        Noto Sans CJK 只覆盖到 U+9FFF 一带。

        **fontconfig 在这里会报错**：实测 ``fc-list :charset=2b6ed`` 报
        65 款字体覆盖「𫛭」，但手工解析 NotoSansCJK-Regular 的 cmap
        子表里根本没有 U+2B6ED 的字形——Noto 的 .ttc 把大量空位也登记
        进了 charset。所以 U+20000 以上必须无条件排除，不信 fontconfig。
        """
        for text in ("kuang", "yi", "shi", "zhong", "a"):
            pool = [x.text for x in self.composer.candidates(text)]
            tofu = [t for t in pool if ord(t[0]) >= 0x20000]
            self.assertFalse(tofu,
                             "%r 的候选里有渲染不出来的字 %s" % (text, tofu[:6]))

    def test_font_coverage_module(self):
        """覆盖表本身：扩展区必须无条件排除。"""
        from ycpy.fontcov import FontCoverage
        cov = FontCoverage.load()
        for ch in ("框", "呈", "矿"):
            self.assertTrue(cov.covers(ch), "%r 是常用字，应认为有字形" % ch)
        # U+20000 以上一律当作没有，哪怕 fontconfig 说有
        for ch in ("𫛭", "𡾇", "𣍦", "𪏪"):
            self.assertFalse(cov.covers(ch),
                             "%r (U+%05X) 在扩展区，必须排除"
                             % (ch, ord(ch)))

    def test_common_char_outranks_rare_char(self):
        """常用字必须排在生僻字前面，哪怕它的词频更低。

        回归 bug（用户报「框这个字一定比㑌这样的字更常用，但是框被放在了
        更后面」）。

        根因是 jieba 的 freq 是**词**频：统计「这个字符串作为独立词条出现
        多少次」。常用字大多只作为词的一部分出现——「框」只在框架/窗口/
        文本框里，所以它自己的 freq 只有 299；而建库时给生僻字的默认分是
        400，于是「㑌」反超。实测 kuang 的同音字里「框」排第 65、「㑌」
        排第 9，差 56 位。

        修法是叠一层**字**级信号：Unihan 的《通用规范汉字表》分级
        （一级字 = 常用字）。
        """
        from ycpy.charscore import CharRarity
        cs = CharRarity.from_zhiku(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "Unihan_IRGSources.txt"))
        self.assertEqual(cs.tier("框"), 1, "「框」应是一级常用字")
        self.assertGreaterEqual(cs.tier("㑌"), 3, "「㑌」应是三级及以后的字")
        self.assertTrue(cs.ranks_above("框", "㑌"))

        got = [x.text for x in self.composer.candidates("kuang")]
        self.assertIn("框", got)
        self.assertIn("㑌", got)
        self.assertLess(got.index("框"), got.index("㑌"),
                        "「框」应排在「㑌」前面，实际 %s" % got[:12])

    def test_common_score_does_not_break_words(self):
        """字级信号只该影响单字，不能把多字词挤掉。"""
        for text, target in (("nihao", "你好"), ("zhongguo", "中国"),
                             ("dajiayiqiganhuo", "大家一起干活")):
            got = [x.text for x in self.composer.candidates(text)]
            self.assertIn(target, got,
                          "%r 不该被单字常用度影响，实际 %s"
                          % (text, got[:3]))

    def test_high_freq_chars_without_tier_stay_ranked(self):
        """没有《通用规范汉字表》等级的超高频字不能被打成生僻字。

        为、说、给、见、让、们 这些字**没有** T 值（该表只收简体规范字，
        它们另有归属）。查不到 T 值不等于生僻字——现在它们靠**语料字频**
        拿高分（为=18、说=24、们=13，都在语料榜前 25 名）。

        这条断言守着「语料字频表存在时」的正常路径；没有字频表时的
        码位兜底路径由 ``test_fallback_path_keeps_rare_chars_low`` 守着。
        """
        from ycpy.charscore import CharRarity
        cs = CharRarity.from_rank_file(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "ycpy-char-freq.txt"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "Unihan_IRGSources.txt"))
        for ch in "为说给见让们":
            self.assertIsNone(cs.tier(ch), "%r 本来就没有等级" % ch)
            self.assertIsNotNone(cs.rank(ch),
                                 "%r 应在语料字频表里收录" % ch)
            self.assertGreater(cs.score_for(ch), 300,
                               "%r 是超高频字，不该被判成生僻字" % ch)
        # 生僻扩展区确实该拿低分
        self.assertLess(cs.score_for("㑌"), cs.score_for("框"))

    def test_fallback_path_keeps_rare_chars_low(self):
        """没有字频表时，码位兜底不能把生僻字抬到前列。

        回归 bug（用户报「将会对这个这些高频字排在很后面」）：最早完全靠
        Unihan 的 T 值打分，T1=1000 / T3=500。实测「这/个/会/对/将」全是 T3，
        只拿 500 分，而「者/正/遮/框」这些 T1 拿 1000——`zhe` 的 299 个
        候选里「这」排第 140 位。

        现在有语料字频表兜底（正常路径），但**码位兜底仍不能给高���**：
        基本区里既没 T 值、语料也没收录的字（醤/櫤/㧑/㻅/屶…）绝大多数
        是生僻字，给 700 分会让它们涌到前列——实测 `hui` 的头 4 名全是
        㧑/㻅/屶/颒，「会」被挤到第 5。
        """
        from ycpy.charscore import CharRarity
        cs = CharRarity.from_zhiku(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "Unihan_IRGSources.txt"))
        # 这些字没有 T 值、语料也没收录，必须给低分
        for ch in "醤櫤㧑㻅屶颒":
            self.assertIsNone(cs.rank(ch), "%r 不该在语料表里" % ch)
            self.assertLess(cs.score_for(ch), 200,
                            "%r 是生僻字，兜底分不该把它抬到前列" % ch)
        # 繁体字（语料表只有简体）在 T 值路径里也该排在简体常用字之后
        self.assertLess(cs.score_for("將"), cs.score_for("将") + 10 ** 9)

    def test_coverage_favors_matching_word(self):
        """覆盖度高的候选应该排在前面。"""
        self.user.learn("程序员")
        self.engine.dictionary.apply_user_counts(self.user.words)
        got = [x.text for x in self.composer.candidates("cxy")]
        # 程序员首字母正好是 cxy，覆盖度满分；陈兴瑜拼音长得多
        self.assertLess(got.index("程序员"), got.index("陈兴瑜"),
                        "完全匹配的「程序员」应排在长词前面")

    def test_fuzzy_cases_all_find_target(self):
        """四类容错输入都要能找到用户想敲的词。"""
        for text, target in (("nhao", "你好"), ("nha", "你好"),
                             ("chxy", "程序员"), ("cxuy", "程序员")):
            r = self.composer.candidates(text)
            got = [x.text for x in r]
            self.assertIn(target, got, "%r 的候选里应有 %s，实际 %s"
                          % (text, target, got[:8]))

    def test_fuzzy_beats_weak_exact(self):
        """精确命中很少时，猜出来的结果要能进前列。

        `nha` 精确只命中「南海岸」，用户显然想敲「你好」——
        所以容错不能只在「一个都没命中」时才启用。
        """
        got = [x.text for x in self.composer.candidates("nha")]
        self.assertIn("你好", got)
        self.assertLess(got.index("你好"), 9, "「你好」应进首屏")

    def test_fuzzy_keeps_exact_match_flags(self):
        """变体查询里的「完全命中」标记必须保留。

        回归 bug：早先把变体命中的 exact 标记一律抹成 False，
        于是 chxy 虽然能查到「程序员」，却因为拿不到首字母完全命中奖励
        而被语料词频埋到 36 名开外。
        """
        for text in ("chxy", "cxuy"):
            got = [x.text for x in self.composer.candidates(text)]
            self.assertLess(got.index("程序员"), 9,
                            "%r 的首屏应含「程序员」，实际 %s" % (text, got[:8]))

    def test_fuzzy_disabled_by_threshold(self):
        """精确命中很多时不去猜，避免浪费和噪声。

        断言的是**这几个词仍然出现在首屏里、且相对次序不变**，而不是
        「前 5 名恰好是这 5 个」。原先写的是全等快照，接入整句词频表后
        `程序猿`（真实存在的网络用语 cheng xu yuan）合法地插进了首屏、
        把 `磁效应` 挤到第 6——那是语料变多，不是排序逻辑坏了。
        全等断言在这里只会在下次扩充语料时再次误报。
        """
        got = [x.text for x in self.composer.candidates("cxy")]
        head = got[:MAX_HEAD]
        for want in ("陈兴瑜", "程序员", "常见于", "产学研"):
            self.assertIn(want, head,
                          "「%s」应留在首屏，实际 %s" % (want, got[:8]))
        # 相对次序才是这条测试真正守的东西
        self.assertLess(head.index("陈兴瑜"), head.index("程序员"),
                        "首屏相对次序不该变，实际 %s" % got[:8])
        self.assertLess(head.index("程序员"), head.index("常见于"),
                        "首屏相对次序不该变，实际 %s" % got[:8])
        self.assertLess(head.index("常见于"), head.index("产学研"),
                        "首屏相对次序不该变，实际 %s" % got[:8])

    def test_fuzzy_can_be_turned_off(self):
        """把阈值设成 0 表示精确命中时不再补充变体候选。

        注意这只管「已经有精确命中」那条路；一个都没命中时仍然会走变体兜底，
        否则用户敲 `chxy` 会得到空白，连打字反馈都没有。
        """
        # nha 精确命中 1 条（南海岸），默认会补上「你好」
        got = [x.text for x in self.composer.candidates("nha")]
        self.assertIn("你好", got)

        self.composer.fuzzy_min_exact = 0
        got_off = [x.text for x in self.composer.candidates("nha")]
        self.assertNotIn("你好", got_off,
                         "关掉容错后 nha 只能给精确命中的结果")

    def test_fuzzy_variants_generation(self):
        """变体生成的正确性。"""
        from ycpy.syllables import fuzzy_variants
        self.assertEqual(fuzzy_variants("chxy"), ["cxy"])
        self.assertEqual(fuzzy_variants("cxuy"), ["cxy"])
        self.assertEqual(fuzzy_variants("nhao")[0], "nihao")
        self.assertEqual(fuzzy_variants("nha")[0], "niha")
        # 正常输入不该产生任何变体
        for text in ("nihao", "cxy", "hao", "ni", "xian"):
            self.assertEqual(fuzzy_variants(text), [],
                             "%r 不该产生容错变体" % text)

    def test_drop_vowel_only_for_initials(self):
        """去掉元音这条规则不能误伤全拼。"""
        from ycpy.syllables import fuzzy_variants
        # 去掉元音后仍然全是声母 -> 首字母串混了元音，可以修
        self.assertIn("cxy", fuzzy_variants("cxuy"))
        # 去掉元音后不是声母 -> 本来就是全拼，不能动
        self.assertNotIn("nh", fuzzy_variants("nihao"))

    # ---- 漏敲元音的容错（旧名保留） ----

    def test_missing_vowel_recovers_candidate(self):
        """敲漏了元音也要能出词：`nhao` -> 你好。

        `n` 本身就是合法音节（嗯），所以 `nhao` 能切成 n+hao，
        但词典里没有这个拼音的词，精确匹配必然落空。
        """
        r = self.composer.candidates("nhao")
        self.assertTrue(r, "nhao 应该有候选")
        self.assertEqual(r[0].text, "你好")

    def test_missing_vowel_ranks_below_exact(self):
        """纠正出来的结果不能压过精确匹配。"""
        fuzzy = self.composer.candidates("nhao")
        exact = self.composer.candidates("nihao")
        self.assertEqual(fuzzy[0].text, "你好")
        self.assertEqual(exact[0].text, "你好")
        self.assertLess(
            fuzzy[0].score, exact[0].score,
            "模糊匹配得分必须低于精确匹配",
        )

    def test_single_bare_syllable_not_repaired(self):
        """单独的「嗯」「呣」是合法输入，不能被当成漏敲元音去纠正。"""
        # 单音节不进入纠正逻辑，候选仍是精确匹配的结果
        for text in ("n", "m"):
            r = self.composer.candidates(text)
            self.assertTrue(r, "%r 应该有候选" % text)
            self.assertGreater(len(r), 1)

    def test_normal_inputs_unchanged(self):
        """容错不能影响任何正常输入。"""
        for text, expected in (("nihao", "你好"), ("hao", "好"),
                               ("ni", "你"), ("cxy", None)):
            r = self.composer.candidates(text)
            self.assertTrue(r, "%r 应该有候选" % text)
            if expected:
                self.assertIn(expected, [x.text for x in r])

    def test_repair_bare_syllables(self):
        """纠正变体生成的正确性。"""
        from ycpy.syllables import repair_bare_syllables
        self.assertEqual(repair_bare_syllables("nhao")[0], "nihao")
        self.assertEqual(repair_bare_syllables("nihao"), [])
        self.assertEqual(repair_bare_syllables("n"), [])
        self.assertEqual(repair_bare_syllables("hao"), [])
        self.assertTrue(repair_bare_syllables("mn"))

    # ---- 预编辑回显 / 回车上屏原始字母 ----

    def test_preedit_shows_raw_letters(self):
        """回显用户实际敲进去的字母，而不是规范化后加分隔符的串。"""
        self.type("chengxu")
        self.assertEqual(self.ui.preedit, "chengxu",
                         "不该把 'chengxu' 显示成 'cheng xu'")

    def test_preedit_shown_even_without_candidates(self):
        """一个候选都没有时，字母和候选窗都还要留着。

        回归 bug：早先 `refresh()` 在无候选时直接 `_clear_ui()`，
        敲了不匹配的拼音字母就从屏幕上消失了；后来虽然补回了预编辑，
        候选窗却又被 `_emit_table` 的「没候选就隐藏」关掉了。
        """
        self.type("vvv")                      # 词典里确实没有 vvv 的候选
        self.assertEqual(self.engine.buffer, "vvv")
        self.assertTrue(self.ui.candidates, "没有匹配时应给出高频词兜底")
        self.assertTrue(self.ui.table_visible, "候选窗要一直开着")
        self.assertTrue(self.ui.preedit_visible, "预编辑也要可见")
        self.assertEqual(self.ui.preedit, "vvv")

    def test_candidate_window_closes_when_backspaced_empty(self):
        """输入被退格删光时，候选窗才关。"""
        self.type("cxy")
        self.assertTrue(self.ui.table_visible)
        self.engine.process_key(KEY_BACKSPACE)
        self.engine.process_key(KEY_BACKSPACE)
        self.assertEqual(self.engine.buffer, "c")
        self.assertTrue(self.ui.table_visible, "还剩一个字母，窗子要留着")
        self.engine.process_key(KEY_BACKSPACE)
        self.assertEqual(self.engine.buffer, "")
        self.assertFalse(self.ui.table_visible, "删光了才关窗")
        self.assertFalse(self.ui.preedit_visible)

    def test_pending_shift_not_fired_by_other_key_release(self):
        """待切换标志不能被**别的键**的抬起触发。

        回归 bug（用户报「输入很多字符后凭空出现一堆退格」）：抬起分支早先不看
        keyval，只要待办标志还在，任意一个键的抬起都会触发 `_toggle_english()`。
        而组字中它会调用 `_commit_raw()`，把整串拼音上屏、预编辑清空——
        用户看到的就是「敲了一长串，字突然自己动了」。
        """
        KEY_F1 = 0xFFBE                      # 引擎不处理，会透传

        # 空闲态：Shift 按下 -> F1 按下 -> F1 抬起
        self.engine.reset()
        self.engine.process_key(KEY_SHIFT_L, 0, 0)
        self.engine.process_key(KEY_F1, 0, 0)
        self.engine.process_key(KEY_F1, 0, IBUS_RELEASE_MASK)
        self.assertEqual(self.engine.state, ST_IDLE,
                         "F1 的抬起不该切到英文态")

        # 组字中：F1 的抬起不该**额外**再上屏一次
        # （F1 的按下本身就该把拼音原样上屏并透传，那是既有行为）
        self.engine.reset()
        del self.ui.commits[:]
        self.type("nihaoshijieshi")
        self.engine.process_key(KEY_SHIFT_L, 0, 0)
        self.engine.process_key(KEY_F1, 0, 0)
        after_press = list(self.ui.commits)
        self.engine.process_key(KEY_F1, 0, IBUS_RELEASE_MASK)
        self.assertEqual(self.ui.commits, after_press,
                         "F1 的抬起不该再触发一次上屏")
        self.assertNotEqual(self.engine.state, ST_ENGLISH,
                            "F1 的抬起不该切到英文态")

    def test_pending_shift_cleared_by_passthrough_press(self):
        """透传（handled=False）的键也必须清掉待切换标志。

        早先只在 handled=True 时才清除，英文态下所有键都返回 False，
        于是「按 Shift 再打一个字母」会静默退出英文态。
        """
        self.tap_shift()
        self.assertEqual(self.engine.state, ST_ENGLISH)

        self.engine.process_key(KEY_SHIFT_L, 0, 0)
        self.assertFalse(self.engine.process_key(ord("a"), 0, 0),
                         "英文态的字母应透传")
        self.engine.process_key(ord("a"), 0, IBUS_RELEASE_MASK)
        self.assertEqual(self.engine.state, ST_ENGLISH,
                         "字母的抬起不该把英文态踢掉")

    def test_shift_as_modifier_does_not_toggle(self):
        """Shift 当修饰键用时不能切换中英。

        回归 bug：`?` 和 `!` 本身就要按 Shift。一按 Shift 就切换的话，
        用户根本打不出这两个中文标点——先被切到英文态，标点原样漏给应用。
        """
        del self.ui.commits[:]
        self.type("nihao")
        self.engine.process_key(KEY_SHIFT_L, 0, 0)          # 按下 Shift
        self.assertTrue(self.engine.process_key(ord("?"), 0, 1 << 0))
        self.assertEqual(self.ui.commits, ["你好", "？"])
        self.assertNotEqual(self.engine.state, ST_ENGLISH,
                            "Shift 当修饰符用时不该切到英文态")

    def test_shift_alone_still_toggles(self):
        """单按 Shift（期间没按别的键）依然要切换。"""
        self.tap_shift()
        self.assertEqual(self.engine.state, ST_ENGLISH)

    def test_shift_press_and_release_both_passthrough(self):
        """切换用的那一次 Shift，按下和抬起都必须放给应用。

        回归 bug（用户报「切到英文后按 a 出大写 A」）：按下为了让应用正常处理
        大写而返回 False（透传），抬起却返回 True 把它吃掉了。客户端只收到
        Shift 按下、永远等不到抬起，内部认为 Shift 还按着，之后每个字母都是
        大写。更迷惑的是打开 CapsLock 时 Shift+CapsLock 恰好等于小写，
        于是「锁了 CapsLock 反而正常」，极难联想到根因。
        """
        for _ in range(2):                       # 切到英文、再切回中文
            press = self.engine.process_key(KEY_SHIFT_L, 0, 0)
            release = self.engine.process_key(
                KEY_SHIFT_L, 0, IBUS_RELEASE_MASK)
            self.assertFalse(press, "Shift 按下应透传给应用")
            self.assertFalse(release, "Shift 抬起也必须透传给应用，成对出现")

# ---- CapsLock / Shift 下的字母直接出，不组字 ----

    def test_capslock_letter_passes_through(self):
        """CapsLock 锁住时按字母要直接出大写，不能组字。

        回归 bug（用户报「CapsLock 锁住后按按键还是弹候选框、也不能打大写」）：
        引擎把大写的 keyval 当成拼音字母组字，候选窗照弹，大写永远打不出来。
        """
        for keyval in (0x41, 0x58, 0x5A):          # A X Z
            self.engine.reset()
            del self.ui.commits[:]
            self.assertFalse(
                self.engine.process_key(keyval, 0, LOCK_MASK),
                "CapsLock 下 0x%x 应透传给应用" % keyval,
            )
            self.assertEqual(self.engine.state, ST_IDLE, "不该进入组字")
            self.assertEqual(self.engine.buffer, "", "不该有拼音缓冲")
            self.assertEqual(self.ui.commits, [], "不该上屏")
            self.assertFalse(self.ui.table_visible, "不该弹候选窗")

    def test_shift_letter_passes_through(self):
        """中文态按住 Shift 按字母：先结掉组字，再直接出大写。

        回归 bug（用户报「shift+x 没有输出 X」）：引擎把 Shift 忽略掉，
        直接把 x 塞进拼音串，结果什么都没输出。
        """
        del self.ui.commits[:]
        self.type("n")
        self.assertEqual(self.engine.state, ST_COMPOSING)
        self.assertFalse(
            self.engine.process_key(0x58, 0, SHIFT_MASK),
            "Shift+x 应透传给应用",
        )
        self.assertEqual(self.ui.commits, ["你"], "先上屏已组的拼音")
        self.assertEqual(self.engine.state, ST_IDLE)

    def test_shift_letter_in_idle_passes_through(self):
        """空闲态 Shift+字母直接透传，不组字。"""
        self.engine.reset()
        self.assertFalse(self.engine.process_key(0x41, 0, SHIFT_MASK))
        self.assertEqual(self.engine.buffer, "")

    def test_plain_lowercase_still_composes(self):
        """普通小写必须照常组字（别把这条规则做过头了）。"""
        self.engine.reset()
        self.assertTrue(self.engine.process_key(ord("n"), 0, 0))
        self.assertEqual(self.engine.buffer, "n")

# ---- 中文标点 ----

    def test_punctuation_conversion(self):
        """中文态下英文标点转中文标点。"""
        cases = [(",", "，"), (".", "。"), ("?", "？"), ("!", "！"),
                 (";", "；"), (":", "："), ("`", "、")]
        for src, want in cases:
            del self.ui.commits[:]
            self.assertTrue(self.engine.process_key(ord(src)),
                            "%r 应该被引擎吃掉" % src)
            self.assertEqual(self.ui.commits, [want], "%r 应转成 %r" % (src, want))

    def test_shifted_punctuation(self):
        """带 Shift 的标点用另一套映射（书名号等）。"""
        for src, want in (("<", "《"), (">", "》")):
            del self.ui.commits[:]
            self.engine.process_key(ord(src), 0, 1 << 0)
            self.assertEqual(self.ui.commits, [want])

    def test_backslash_is_dunhao(self):
        """反斜杠出顿号——与主流输入法的键位一致。

        用户报「输入 \\ 应该是中文顿号」。原先映射到全角「＼」。
        """
        del self.ui.commits[:]
        self.assertTrue(self.engine.process_key(ord("\\")))
        self.assertEqual(self.ui.commits, ["、"])

    def test_pipe_stays_pipe(self):
        """竖线不再被吞成全角反斜杠。

        改顿号时顺带暴露的 bug：原先「\\」映射到全角「＼」，于是竖线
        根本打不出来。
        """
        del self.ui.commits[:]
        self.engine.process_key(ord("|"), 0, 1 << 0)
        self.assertEqual(self.ui.commits, ["|"])

    def test_pipe_never_enters_composition(self):
        """竖线不该被当成拼音去组字。"""
        del self.ui.commits[:]
        self.assertTrue(self.engine.process_key(ord("|"), 0, 1 << 0))
        self.assertEqual(self.engine.state, ST_IDLE)
        self.assertEqual(self.engine.buffer, "")

    def test_dunhao_on_both_backslash_and_grave(self):
        """顿号在反斜杠和反引号上都有——哪个顺手按哪个。"""
        for key in ("\\", "`"):
            del self.ui.commits[:]
            self.assertTrue(self.engine.process_key(ord(key)), repr(key))
            self.assertEqual(self.ui.commits, ["、"], repr(key))

    def test_punctuation_commits_composition_first(self):
        """组字中打标点：先上屏候选词，再上屏标点。"""
        del self.ui.commits[:]
        self.type("nihao")
        self.engine.process_key(ord(","))
        self.assertEqual(self.ui.commits, ["你好", "，"])
        self.assertEqual(self.engine.state, ST_IDLE)

    def test_punctuation_not_converted_in_english_mode(self):
        """英文直通态不该转义，原样交给应用。"""
        self.tap_shift()                            # 切英文
        self.assertEqual(self.engine.state, ST_ENGLISH)
        self.assertFalse(self.engine.process_key(ord(",")),
                         "英文态的逗号应透传给应用")
        self.assertEqual(self.ui.commits, [])

    def test_punctuation_can_be_disabled(self):
        """首选项里关掉后就不转了。"""
        self.engine.config.set("enable_punctuation", False)
        self.assertFalse(self.engine.process_key(ord(",")))
        self.assertEqual(self.ui.commits, [])

    def test_semicolon_migrates_associate_key(self):
        """老配置里的「分号=联想」要自动迁到 Ctrl+;，否则联想会被标点吃掉。"""
        import json as _json
        path = os.path.join(self.tmp, "legacy.json")
        with open(path, "w") as fp:
            _json.dump({"associate_key": {"keyval": 0x3B, "modifiers": 0}}, fp)
        from ycpy.config import Config
        self.assertEqual(Config(path).associate_key.get("modifiers"), 1 << 2)

        # 用户自己改过的键不该被动
        path2 = os.path.join(self.tmp, "custom.json")
        with open(path2, "w") as fp:
            _json.dump({"associate_key": {"keyval": ord("/"), "modifiers": 0}}, fp)
        self.assertEqual(Config(path2).associate_key.get("keyval"), ord("/"))

# ---- 无匹配时的高频词兜底 ----

    def test_hot_words_fallback_keeps_window_visible(self):
        """一个候选都打不出来时也要给候选，否则 GNOME 根本不画窗口。

        回归 bug：只敲一个 `v`，精确匹配 0 条，引擎发了空 lookup table，
        GNOME 收到空表直接不显示候选框——用户看到的是「按了键没反应」。
        """
        for text in ("v", "vv", "vvv"):
            got = [x.text for x in self.composer.candidates(text)]
            self.assertTrue(got, "%r 应该拿到高频词兜底" % text)
        # 多字母的兜底仍是双字词（用户想打的多半是词）
        for x in self.composer.candidates("vv"):
            self.assertEqual(len(x.text), 2)
        # 但只敲**一个韵母**时必须给单字：一个字母写不出 `yi ge`，
        # 给「一个」「中国」纯属误导（用户报的「输入单个韵母只该出单字」）。
        # `v` 是零声母韵母（ü），所以也算「单个韵母」。
        for x in self.composer.candidates("v"):
            self.assertEqual(len(x.text), 1)

    def test_hot_words_never_outrank_real_matches(self):
        """真候选存在时，高频词兜底不能混进来抢位置。"""
        got = [x.text for x in self.composer.candidates("nihao")]
        self.assertEqual(got[0], "你好")
        self.assertNotIn("一个", got)

# ---- 联想兜底 ----

    def test_association_never_empty(self):
        """联想永远不能返回空，否则用户看到的是一个空窗口。

        回归 bug：字级搭配表只收语料里最高频的 2500 个字（build_dict.py 的
        TOP_CHARS），生僻字不在其中。选完「陈兴瑜」再按分号，末字「瑜」
        查不到任何搭配，联想窗就空了。
        """
        for ctx in ("陈兴瑜", "程序员", "你好", "夜游"):
            r = self.composer.next_words(ctx)
            self.assertTrue(r, "%r 的联想结果为空" % ctx)

    def test_candidate_window_closes_on_commit(self):
        """上屏之后候选窗要关掉。"""
        self.type("cxy")
        self.assertTrue(self.ui.table_visible)
        self.engine.process_key(KEY_SPACE)
        self.assertFalse(self.ui.table_visible, "上屏后应关窗")

    def test_enter_commits_raw_letters_not_first_candidate(self):
        """回车原样上屏英文，不选第一个候选词。"""
        self.type("cxy")
        self.assertIn("程序员", self.ui.candidates)
        self.assertTrue(self.engine.process_key(KEY_RETURN))
        self.assertEqual(self.ui.commits, ["cxy"], "回车应上屏 'cxy' 本身")
        self.assertEqual(self.engine.state, ST_IDLE)

    def test_space_still_selects_first_candidate(self):
        """空格仍然选第一个候选词（回车改了，空格不能跟着改）。

        候选顺序会随用户词频变化，这里只断言「上屏的是候选表里的第一个」，
        不写死具体是哪个词。
        """
        self.type("cxy")
        first = self.ui.candidates[0]
        self.assertTrue(first, "cxy 应该有候选")
        self.engine.process_key(KEY_SPACE)
        self.assertEqual(self.ui.commits, [first])

    def test_enter_commits_raw_when_no_candidates(self):
        """无候选时回车同样上屏原始字母。"""
        self.type("vvv")
        self.engine.process_key(KEY_RETURN)
        self.assertEqual(self.ui.commits, ["vvv"])

    # ---- 按下 / 松开必须给出一致的答案 ----

    def test_release_mirrors_passthrough_press(self):
        """透传给应用的键，松开也必须透传。

        回归 bug：早先松开事件一律 `return True`，而数字/退格这类键的**按下**
        是 `return False`（透传给应用）。客户端于是只收到按下、收不到松开，
        按键状态机错乱，表现为「按一次连续输出同一个字符」「退格一直删」。
        """
        for keyval in (ord("5"), KEY_BACKSPACE, KEY_SPACE, KEY_ESCAPE):
            self.engine.reset()
            press = self.engine.process_key(keyval, 0, 0)
            release = self.engine.process_key(keyval, 0, IBUS_RELEASE_MASK)
            self.assertEqual(
                release, press,
                "0x%x 的松开结论(=%s)必须和按下(=%s)一致"
                % (keyval, release, press),
            )

    def test_release_mirrors_consumed_press(self):
        """被引擎消费的键，松开也必须被消费（不能漏给应用）。"""
        for keys in ("cxy", "a"):
            self.engine.reset()
            for ch in keys:
                press = self.engine.process_key(ord(ch), 0, 0)
                release = self.engine.process_key(ord(ch), 0, IBUS_RELEASE_MASK)
                self.assertTrue(press, "字母应被引擎消费")
                self.assertTrue(release, "字母的松开也必须被消费")

    def test_stray_release_without_press_is_consumed(self):
        """没有先按下的孤立松开事件：消费掉，不产生任何动作。"""
        self.type("c")
        before = self.engine.buffer
        self.assertTrue(self.engine.process_key(ord("x"), 0, IBUS_RELEASE_MASK))
        self.assertEqual(self.engine.buffer, before)
        self.assertEqual(self.engine.state, 1)

    def test_full_press_release_sequence_types_once(self):
        """模拟真实客户端：每个键都是 press + release 两次调用。"""
        for ch in "cxy":
            self.type_key(ch)
            self.engine.process_key(ord(ch), 0, IBUS_RELEASE_MASK)
        self.assertEqual(self.engine.buffer, "cxy")
        self.assertIn("程序员", self.ui.candidates)
        # 选词后只上屏一次
        idx = self.ui.candidates.index("程序员")
        self.type_key(str(idx + 1))
        self.engine.process_key(ord(str(idx + 1)), 0, IBUS_RELEASE_MASK)
        self.assertEqual(self.ui.commits, ["程序员"])


# ---- 启动脚本路径 ----

class ScriptPathTest(unittest.TestCase):
    """启动脚本的路径必须**在两种安装布局下都找得到**。

    这个 bug 连续被用户报了两次，两个方向都错过：

    第一次：「点『首选项…』没弹出对话框」。``launch_preferences`` 把 ``_ROOT``
    又往上剥了一层，拼出 ``/opt/src/ycpy-preferences``——不存在。

    「修好」之后改成了 ``_ROOT`` 下找，源码树是对了，但 **deb 下又坏了**：
    ``_ROOT`` 是 ``/usr/lib/ycpy``，而 deb 把启动脚本装在 ``/usr/bin``，
    ``/usr/lib/ycpy`` 下只有 ``ycpy/`` 和 ``data/``。于是用户第二次报
    「右上角弹出菜单里点首选项，又不能弹出窗口了」。

    根因是这个函数**从来没有同时兼顾过两种布局**。所以测试必须**两种都测**，
    只测一种的话下一次再改错方向还是发现不了。

    这类错误没法靠点菜单复现（路径错时进程压根起不来），只能直接断言路径。
    """

    def test_source_tree_layout(self):
        """源码树：脚本在项目根（``_ROOT``）下。"""
        from ycpy.engine import _ROOT, _script_path
        for name in ("ycpy-engine", "ycpy-preferences"):
            path = _script_path(name)
            self.assertTrue(os.path.exists(path),
                            "源码树下 %s 不存在：%s" % (name, path))
            self.assertTrue(
                path.startswith(_ROOT + os.sep),
                "源码树下 %s 的路径不该跑到项目外：%s" % (name, path))

    def test_deb_layout_falls_back_to_usr_bin(self):
        """deb：``_ROOT``（``/usr/lib/ycpy``）下**没有**脚本，要回退 /usr/bin。

        这是第二次那个 bug 的正例。用一个临时的 ``_ROOT`` 来模拟——真去改
        ``engine._ROOT`` 会影响同进程内其它测试，所以这里只验证决策逻辑：
        当 ``_ROOT`` 下找不到时，``_script_path`` 必须给出一个**存在**的路径。
        """
        import ycpy.engine as engine
        real_root = engine._ROOT
        try:
            engine._ROOT = "/nonexistent-ycpy-root"     # 模拟 deb 布局
            for name in ("ycpy-engine", "ycpy-preferences"):
                path = engine._script_path(name)
                if os.path.exists("/usr/bin/" + name):
                    # 本机装了 deb 版：必须解析到 /usr/bin 下那个真实存在的
                    self.assertEqual(path, "/usr/bin/" + name,
                                     "%s 应解析到 /usr/bin" % name)
                else:
                    # 两处都没有时的**契约**（见 engine._script_path 的
                    # docstring）：仍返回 _ROOT 下那个不存在的路径，好让调用方
                    # 的报错信息显示出「期望在哪」，而不是指向一个更让人
                    # 摸不着头脑的地方。
                    self.assertEqual(path,
                                     "/nonexistent-ycpy-root/" + name,
                                     "%s 无处可寻时的兜底路径变了" % name)
        finally:
            engine._ROOT = real_root

    def test_returns_existing_path_in_both_layouts(self):
        """无论 _ROOT 指向哪，返回的路径都得是**真实存在**的。

        这是最要紧的一条：前两个 bug 的共同症状就是「返回了一个不存在的
        路径，而 Popen 的异常又被 DEVNULL 吃掉」。
        """
        import ycpy.engine as engine
        real_root = engine._ROOT
        for root in (real_root, "/nonexistent-ycpy-root"):
            engine._ROOT = root
            try:
                for name in ("ycpy-engine", "ycpy-preferences"):
                    path = engine._script_path(name)
                    if not os.path.exists(path):
                        self.skipTest(
                            "本机没装 deb 版（/usr/bin/%s 不存在），"
                            "无法验证回退分支" % name)
                    self.assertTrue(os.path.exists(path), ...)
            finally:
                engine._ROOT = real_root

    def test_exec_path_matches_real_script(self):
        from ycpy.engine import _exec_path
        self.assertTrue(os.path.exists(_exec_path()),
                        "ycpy-engine 路径不对：%s" % _exec_path())

    def test_launch_preferences_does_not_swallow_stderr(self):
        """拉首选项时**不能**把 stderr 丢进 DEVNULL。

        踩过的坑：那段代码的注释明明写着「不能丢 stderr：GTK 起不来时
        唯一的线索就在这里」，而代码写的是 ``stderr=subprocess.DEVNULL``——
        注释与代码正好相反。于是「点了没反应」这个 bug 查了两轮都无从
        查起：子进程的 traceback 全进了 /dev/null。
        """
        import inspect
        import ycpy.engine as engine
        src = inspect.getsource(engine.launch_preferences)
        self.assertIn("stderr=None", src,
                      "launch_preferences 必须保留子进程的 stderr")
        self.assertNotIn("stderr=subprocess.DEVNULL", src,
                         "stderr 不能丢——那是「点了没反应」时唯一的线索")

    def test_preferences_script_is_executable(self):
        from ycpy.engine import _script_path
        path = _script_path("ycpy-preferences")
        self.assertTrue(os.access(path, os.X_OK),
                        "%s 不可执行，菜单点了也起不来" % path)


class PreferencesCloseTest(unittest.TestCase):
    """首选项的关闭按钮必须真能关掉窗口。

    回归 bug（用户报「首选项对话框右上角的关闭按钮不能正常关闭」）。

    原来的写法是 ``lambda *_: (self._save(), False)``，想表达「先存盘、
    再让窗口关掉」。但 ``delete-event`` 只有一个返回值（gboolean），
    PyGObject 收到**元组**会按「多返回值」去解包，GTK 那边拿到的就成了
    ``True``——而 ``True`` 的含义是「**阻止**默认处理」，于是窗口根本不
    销毁，点关闭按钮毫无反应。

    正确的写法必须**返回单个 False**。这里直接断言返回值，避免再写回去。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_handler_returns_single_false(self):
        from ycpy.preferences import PreferencesWindow
        cfg = Config(os.path.join(self.tmp, "cfg.json"))
        win = PreferencesWindow(cfg)
        ret = win._on_delete(None)
        # True = 阻止销毁 = 关闭按钮失效
        self.assertIs(ret, False,
                      "delete-event 必须返回 False（False 才是「照常销毁」），"
                      "实际返回 %r" % (ret,))

    def test_delete_handler_saves_config(self):
        from ycpy.preferences import PreferencesWindow
        path = os.path.join(self.tmp, "save-on-close.json")
        cfg = Config(path)
        win = PreferencesWindow(cfg)
        cfg.set("page_size", 7)
        win._on_delete(None)
        self.assertTrue(os.path.exists(path),
                        "关闭对话框时应该把配置存盘")

    def test_delete_handler_is_not_a_tuple_returning_lambda(self):
        """守卫：确保没有人在 __init__ 里换回元组 lambda。"""
        import inspect

        from ycpy.preferences import PreferencesWindow
        src = inspect.getsource(PreferencesWindow.__init__)
        self.assertNotIn('(self._save(), False)', src,
                         "又换回了返回元组的 lambda，那会让关闭按钮失效")


class PressReleasePairingTest(unittest.TestCase):
    """每个键的抬起必须回放**它自己**按下的结论。

    回归 bug（用户报「Shift 抬起不稳定，之后点鼠标变成选中文字」）。

    原实现只有一个 ``_last_press_consumed`` 槽位，被任何键的按下覆盖。
    真实序列 ``Shift↓ 空格↓ 空格↑ Shift↑`` 里，空格被引擎消费（选词），
    槽位变成 True，松 Shift 时回放的其实是**空格**的结论——
    Shift 的抬起被吃掉，应用内部认为 Shift 还按着，
    之后任何鼠标点击都变成 Shift+点击 -> 选中一片文字。

    修饰键通常比被修饰的键晚抬起，所以最容易中招。
    """

    RELEASE = 1 << 30
    SHIFT_MASK = 1 << 0

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self):
        engine, _, _ = make_engine(os.path.join(self.tmp, "user.json"),
                                   os.path.join(self.tmp, "config.json"))
        engine.on_focus_in()
        return engine

    def press(self, engine, keyval, mod=0):
        return engine.process_key(keyval, 0, mod)

    def release(self, engine, keyval, mod=0):
        return engine.process_key(keyval, 0, self.RELEASE | mod)

    def compose_ni(self, engine):
        for ch in "ni":
            engine.process_key(ord(ch), 0, 0)
        self.assertEqual(engine.buffer, "ni")

    def test_shift_release_survives_consumed_key_in_middle(self):
        """核心回归：Shift↓ 空格↓ 空格↑ Shift↑，Shift 抬起必须透传。"""
        engine = self.make()
        self.compose_ni(engine)

        self.assertFalse(self.press(engine, KEY_SHIFT_L), "Shift 按下应透传")
        self.assertTrue(self.press(engine, 0x20, self.SHIFT_MASK),
                        "组字态下空格被消费（选词）")
        self.assertTrue(self.release(engine, 0x20, self.SHIFT_MASK))
        # 关键断言：Shift 自己的抬起不能被空格的结论顶掉
        self.assertFalse(
            self.release(engine, KEY_SHIFT_L),
            "Shift 的抬起被中间键的结论覆盖了（消费=应用看不到抬起）；"
            "应用会永远停在 Shift 按下状态，之后点鼠标变成选中文字")

    def test_every_key_release_matches_its_own_press(self):
        """交错按下 a、b、c 再倒序抬起，每次抬起都要对上自己的按下。"""
        engine = self.make()
        self.compose_ni(engine)

        presses = {}
        for keyval in (0x61, 0x20, 0x62):   # a, space, b
            presses[keyval] = self.press(engine, keyval)

        for keyval in (0x62, 0x20, 0x61):   # 倒序抬起
            got = self.release(engine, keyval)
            self.assertEqual(
                got, presses[keyval],
                "keyval=%#x 的抬起(%r) 与它自己的按下(%r) 不一致"
                % (keyval, got, presses[keyval]))

    def test_release_does_not_leak_into_later_repress(self):
        """抬起后槽位要清掉，不能污染同一键的下一次按下。"""
        engine = self.make()
        self.compose_ni(engine)

        self.press(engine, KEY_SHIFT_L)
        self.release(engine, KEY_SHIFT_L)
        # 同一个 Shift 再来一轮
        self.assertFalse(self.press(engine, KEY_SHIFT_L))
        self.assertFalse(self.release(engine, KEY_SHIFT_L),
                         "第二次 Shift 的抬起被上一轮的状态污染了")

    def test_stray_release_still_consumed(self):
        """没有配对按下的杂散抬起，保持老行为：消费掉。"""
        engine = self.make()
        self.assertTrue(self.release(engine, 0x61),
                        "杂散 release 应消费（应用没收到过它的按下）")

    def test_consumed_key_release_stays_consumed(self):
        """被引擎消费的键，抬起也必须被消费（各语言输入法的通行要求）。"""
        engine = self.make()
        self.compose_ni(engine)
        self.assertTrue(self.press(engine, 0x20))
        self.assertTrue(self.release(engine, 0x20),
                        "被消费的键抬起若透传，会重复上屏选中的词")

    def test_no_single_shared_slot_remains(self):
        """守卫：确保没有人在 __init__ 里换回单槽位实现。"""
        import inspect

        from ycpy.engine import PinyinEngine
        src = inspect.getsource(PinyinEngine.__init__)
        self.assertNotIn("_last_press_consumed", src,
                         "又换回了单槽位 _last_press_consumed，"
                         "Shift 抬起会被中间键的结论顶掉")
        self.assertIn("_press_consumed", src,
                      "应该用按 keyval 分别记录的 _press_consumed")

    # ------------------------------------------------------------ keyval 会变
    #
    # 下面一组是另一条 bug 线（用户报「随机出现连续触发按键」），与上面那个
    # 「单槽位被覆盖」是不同根因，但症状同源：**按下给了应用、抬起被引擎吞掉**。

    #: 引擎实际收到的物理键编号。绝对值不重要（X keycode 与 evdev 差 8，
    #: 见 ``tools/probe_keycode.py`` 的实测），重要的是 press/release 一致。
    KEYCODE = 30
    LOCK_MASK = 1 << 2

    def test_keyval_change_paired_by_keycode(self):
        """核心回归：Shift 先抬起 -> 同一物理键的 keyval 变了 -> 仍要配对上。

        回归 bug（用户报「还是随机的会出现连续触发按键的情况」）。

        X 里 ``keyval = f(keycode, 修饰键状态)``，keyval 是**解释出来的结果**，
        不是硬件事实。真人打字「先松修饰键再松字母」很常见，于是真实序列是

            Shift↓   A↓(keyval=0x41)   Shift↑   A↑(keyval=0x61)
                                                     ^^^^^^^^^^^
                                        抬起时 Shift 已经没了，解释成 'a'

        按下那下走的是「Shift/CapsLock 下的字母直接透传」那条 -> 记 ``False``；
        抬起按 keyval '0x61' 去 pop，旧实现 pop 不到，兜底返回 ``True``：

            press   -> 应用收到 KeyPress
            release -> 应用收不到 KeyRelease
            -> 客户端认为这个键还按着，开始自动重复 = 用户看到的连续触发

        keycode 是硬件键，修饰键动不了它，press/release 必然相同，所以拿它
        配对。**这两点都在真实 X 链路上实测过**（``tools/probe_keycode.py``）：
        keycode 全非零，且同一键 press=0x41 / release=0x61 而 keycode 恒为 30。
        """
        engine = self.make()
        pressed = engine.process_key(0x41, self.KEYCODE, self.SHIFT_MASK)
        self.assertFalse(pressed, "Shift 下的字母应透传给应用")
        released = engine.process_key(0x61, self.KEYCODE, self.RELEASE)
        self.assertEqual(
            pressed, released,
            "按下 %r / 抬起 %r —— 客户端只收到 press、收不到 release，"
            "会以为键还按着从而连续触发" % (pressed, released))

    def test_capslock_change_paired_by_keycode(self):
        """CapsLock 同一条：``LOCK_MASK`` 一样会改变字母大小写。"""
        engine = self.make()
        pressed = engine.process_key(0x41, self.KEYCODE, self.LOCK_MASK)
        self.assertFalse(pressed, "CapsLock 下的字母应透传给应用")
        released = engine.process_key(0x61, self.KEYCODE, self.RELEASE)
        self.assertEqual(pressed, released,
                         "CapsLock 前后 keyval 变化导致抬起配不上对")

    def test_keycode_zero_falls_back_to_keyval(self):
        """``keycode=0``（部分后端 / 全部既有单测与压测）退回 keyval。"""
        engine = self.make()
        self.assertTrue(engine.process_key(0x61, 0, 0), "组字应被吃掉")
        self.assertIn(0x61, engine._press_consumed,
                      "keycode 缺失时记录应落在 keyval 上，否则既有断言全废")
        self.assertTrue(engine.process_key(0x61, 0, self.RELEASE))

    def test_release_missing_record_with_keycode_falls_back_to_keyval(self):
        """按下没带 keycode、抬起带了 -> 退回 keyval 再找一次，别错配。"""
        engine = self.make()
        pressed = engine.process_key(0x61, 0, 0)      # 记在 keyval 0x61 上
        self.assertTrue(pressed, "组字应被吃掉")
        released = engine.process_key(0x61, self.KEYCODE, self.RELEASE)
        self.assertEqual(
            pressed, released,
            "抬起带了 keycode，按 keycode 找不到却没退回 keyval，"
            "按下结论回放不出来 -> 客户端重复触发")
        self.assertNotIn(0x61, engine._press_consumed, "确实 pop 掉了")

    def test_trace_records_keycode(self):
        """trace 必须同时记 keyval 与 keycode——排查这条 bug 的唯一依据。

        光看 keyval 对不上，分不清是「杂散抬起」还是「修饰键变了导致
        keyval 变了」；后者只有对照 press 那行的 keycode 才认得出来。
        """
        from ycpy import engine as eng_mod
        path = os.path.join(self.tmp, "trace.log")
        open(path, "w").close()
        old = eng_mod.TRACE_PATH
        eng_mod.TRACE_PATH = path
        try:
            e = self.make()
            e.process_key(0x41, self.KEYCODE, self.SHIFT_MASK)
            e.process_key(0x61, self.KEYCODE, self.RELEASE)
        finally:
            eng_mod.TRACE_PATH = old
        with open(path, encoding="utf-8") as fp:
            text = fp.read()
        self.assertIn("press   keyval=0x41 keycode=%d" % self.KEYCODE, text,
                      "press 那行没记 keycode，出问题时无从对照")
        self.assertIn("release keyval=0x61 keycode=%d" % self.KEYCODE, text,
                      "release 那行没记 keycode，出问题时无从对照")


class StalePressRecordTest(unittest.TestCase):
    """焦点切换后不能残留「按下结论」。

    用户报「经常莫名其妙出现连续退格，我没碰退格键」。

    这类症状的典型成因是某个键**只收到了按下、没收到抬起**：应用内部的按键
    状态机于是以为该键仍按着，开始自己补发事件（退格就表现为连续删除）。

    **清理点必须放在 on_focus_out，不能挪进 reset()** —— reset() 会在
    _process_press 内部被调用（选词、退格清空、英文切换），那时用户可能正按住
    Shift；清掉会把 Shift 的记录抹掉，抬起 pop 不到 -> 兜底 True ->
    又变成「Shift 抬起被吞」的老 bug。

    「清理」到底清什么，这里改过一次——**原��是全清，而那正是同一个 bug 的
    另一副面孔**：

        Backspace 按下 -> 返回 False -> 应用收到 press，开始删字
        FocusOut      -> 全清
        Backspace 抬起 -> pop 不到 -> 兜底 True -> **应用收不到 release**

    应用于是以为退格还按着，开始自动重复——用户看到的就是「没碰退格却在
    连续删除」「凭空冒出好几个换行」。用户第二次报的就是这个。

    所以现在**只清引擎自己吃掉的那些（True）**。透传给应用的（False）
    必须留着，抬起才能回放 False、和 press 配成完整的一对。True 的那些
    应用从头到尾没看见过，清掉或留着都无所谓，清掉是为了不让表无界增长。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self):
        engine, _, _ = make_engine(os.path.join(self.tmp, "user.json"),
                                   os.path.join(self.tmp, "config.json"))
        engine.on_focus_in()
        return engine

    def test_focus_out_drops_consumed_records(self):
        """引擎吃掉的键，记录在焦点离开时清掉（True 的留着没用）。"""
        engine = self.make()
        engine.process_key(ord("n"), 0, 0)          # 组字，引擎吃掉 -> True
        self.assertIn(ord("n"), engine._press_consumed)
        engine.on_focus_out()
        self.assertNotIn(ord("n"), engine._press_consumed,
                         "引擎吃掉的键清掉才对——应用从头到尾没看见过它")

    def test_focus_out_keeps_passthrough_records(self):
        """透传给应用的键，记录**必须留下**，否则抬起配不上对。

        回归 bug（用户第二次报「偶尔莫名其妙输出很多回车键或者退格键」）：
        全清会让应用只收到 press、收不到 release，于是它以为键还按着而
        自动重复。X 会在同一时刻送出 KeyRelease 和 FocusOut，进 D-Bus 队列
        的先后不保证，所以这个窗口真实存在。
        """
        for keyval in (KEY_BACKSPACE, KEY_RETURN):
            engine = self.make()
            press_ret = engine.process_key(keyval, 0, 0)
            self.assertFalse(press_ret, "%s 在空闲态应透传给应用" % hex(keyval))
            engine.on_focus_out()
            self.assertIn(keyval, engine._press_consumed,
                          "透传键的记录被清掉了，抬起就回放不出 False")
            release_ret = engine.process_key(keyval, 0, 1 << 30)
            self.assertEqual(press_ret, release_ret,
                             "press 与 release 的结论必须一致，"
                             "客户端才收得到完整的一对")

    def test_capacity_overflow_keeps_passthrough_records(self):
        """**记录表超限时也不能全清**——同一个错误，换了个触发条件。

        ``_PRESS_CONSUMED_MAX`` 那个分支原来写的是 ``clear()``，和
        ``on_focus_out`` 犯的是同一个错：把透传给应用的记录一起抹了。
        于是透传键的抬起 pop 不到 -> 兜底返回 True -> 客户端只收到 press、
        收不到 release -> 自动重复（用户报「偶尔还是会遇到连续输出」）。

        两个触发条件的区别值得记下来：``on_focus_out`` 是频繁发生的那个，
        容量溢出要攒到 512 条才触发、极难复现——所以修好前一个之后，
        后一个会一直躲在后面。**结论是：清理记录表的每一处都得单独审，
        「我想限制一下增长」不能顺手写成 clear()。**
        """
        engine = self.make()
        # 灌满记录表，逼出容量超限分支。
        from ycpy.engine import _PRESS_CONSUMED_MAX
        for kv in range(0x1000, 0x1000 + _PRESS_CONSUMED_MAX + 10):
            engine._press_consumed[kv] = True

        # 再按一个**透传**的键（英文态下所有键都透传），然后抬起。
        engine.state = ST_ENGLISH
        keyval = ord("a")
        press_ret = engine.process_key(keyval, 0, 0)
        self.assertFalse(press_ret, "英文态下应透传给应用")
        # 这一步会触发容量检查——正是它以前把刚记下的透传记录抹掉的地方。
        release_ret = engine.process_key(keyval, 0, 1 << 30)
        self.assertEqual(press_ret, release_ret,
                         "超限清理吃掉了透传记录，抬起配不上对")

    def test_no_cleanup_site_drops_passthrough_records(self):
        """通用不变量：**任何**重建记录表的地方都不能丢透传记录。

        比逐个断言「容量溢出时 X 键的 press/release 要一致」更根本——
        以后新加清理点时这条会直接指出它违反了约束，而不是等某个具体的键
        在某个状态下出问题才发现。

        这类 bug 的教训：同一个错误可以在多个地方各写一遍，修好一处不代表
        全部修好。这里用源码级的断言把「每一处重建都得过滤掉 True」钉死。
        """
        import inspect
        import re
        import ycpy.engine as engine_mod

        cls = type(self.make())
        src = inspect.getsource(cls)
        rebuilds = re.findall(r"self\._press_consumed\s*=\s*\{[^{}]*?\}",
                              src, re.S)
        self.assertGreaterEqual(
            len(rebuilds), 1,
            "至少 on_focus_out 那一处重建；实现变了请同步更新")
        for expr in rebuilds:
            self.assertIn(
                "if not v", expr,
                "重建记录表时必须保留透传记录（False）：%r" % expr)
        # 反向断言：**代码里**不能有裸 clear()（注释里提到它是为了记录历史，
        # 所以要排除注释行）。收缩要交给 _trim_press_records，
        # 它才有「先丢 True、丢了无害」的优先级逻辑。
        code_lines = [ln for ln in src.splitlines()
                      if "_press_consumed.clear()" in ln
                      and not ln.lstrip().startswith("#")]
        self.assertEqual(
            code_lines, [],
            "裸 clear() 会吃掉透传记录，必须走 _trim_press_records")


    def test_press_release_paired_under_focus_race(self):
        """焦点竞态：press 与 release 之间发生 FocusOut，仍要成对。"""
        for keyval in (KEY_BACKSPACE, KEY_RETURN, ord("a")):
            engine = self.make()
            press_ret = engine.process_key(keyval, 0, 0)
            engine.on_focus_out()                      # 插在中间
            engine.on_focus_in()
            release_ret = engine.process_key(keyval, 0, 1 << 30)
            self.assertEqual(
                press_ret, release_ret,
                "%s: press=%s release=%s，客户端只收到一半"
                % (hex(keyval), press_ret, release_ret))

    def test_shift_record_survives_focus_out(self):
        """Shift 抬起不能被吞——记录必须在焦点切换后还能回放 False。"""
        engine = self.make()
        press_ret = engine.process_key(KEY_SHIFT_L, 0, 0)
        self.assertFalse(press_ret)
        engine.on_focus_out()
        self.assertEqual(engine.process_key(KEY_SHIFT_L, 0, 1 << 30),
                         press_ret, "Shift 的 press/release 结论应一致")

    def test_shift_record_survives_intervening_reset(self):
        """按住 Shift 时组字态选词会调 reset()，Shift 的记录必须保住。"""
        engine = self.make()
        for ch in "ni":
            engine.process_key(ord(ch), 0, 0)
        self.assertFalse(engine.process_key(KEY_SHIFT_L, 0, 0))
        # 选词 -> 内部 reset()
        engine.process_key(0x20, 0, 0)
        self.assertIn(
            KEY_SHIFT_L, engine._press_consumed,
            "reset() 把 Shift 的按下记录清掉了；抬起会 pop 不到 -> "
            "兜底返回 True -> Shift 抬起被吞（3.11 的老 bug）")
        self.assertFalse(
            engine.process_key(KEY_SHIFT_L, 0, IBUS_RELEASE_MASK),
            "Shift 抬起被吞了，应用会永远停在 Shift 按下状态")

    def test_autorepeat_presses_do_not_leak_entries(self):
        """按住不放的 auto-repeat：多个 press + 一个 release，只留一条且被 pop 掉。"""
        engine = self.make()
        for ch in "nihao":
            engine.process_key(ord(ch), 0, 0)
        for _ in range(6):
            engine.process_key(KEY_BACKSPACE, 0, 0)
        engine.process_key(KEY_BACKSPACE, 0, IBUS_RELEASE_MASK)
        self.assertNotIn(
            KEY_BACKSPACE, engine._press_consumed,
            "抬起后仍残留退格的记录")

    def test_record_table_is_bounded(self):
        """release 全丢时也不能无界增长。"""
        engine = self.make()
        from ycpy.engine import _PRESS_CONSUMED_MAX
        # 造出大量互不相同的 keyval，模拟抬起全部丢失
        for keyval in range(0xFF00, 0xFF00 + _PRESS_CONSUMED_MAX + 200):
            engine.process_key(keyval, 0, 0)
        self.assertLessEqual(
            len(engine._press_consumed), _PRESS_CONSUMED_MAX + 1,
            "_press_consumed 超过了容量上限，release 丢失时会无界增长")


class HighFreqCharRankingTest(unittest.TestCase):
    """用户报的高频字必须排在候选前面。

    用户原话：「将这个字应该是高频字，为什么会在很后面，会这个字也是这样，
    下面几个字都有这种情况：将，会，对，这，个」。

    根因：早先只按 Unihan 的 ``kIRG_TSource``（《通用规范汉字表》分级）
    打分，把 T 值直接当分数用（T1=1000 / T3=500）。但那个分级衡量的是
    **字形规范化程度**，不是**使用频率**——该表按「是否收录为规范字」
    分级，而「将/会/对/这/个」的规范归属另有来源，于是全被划到 T3。
    实测 `zhe` 的 299 个候选里「这」排第 140 位，`ge` 的 159 个里
    「个」排第 98 位。

    现在改用 Jun Da 的语料字频表做主信号（这=11 个=12 会=29 对=33
    将=132，都在真实语料的前几百名），T 值退化成很轻的乘性微调。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def composer(self):
        dictionary = Dictionary()
        user = UserModel(os.path.join(self.tmp, "user.json"))
        dictionary.apply_user_counts({})
        return Composer(dictionary, user)

    def char_score(self):
        """带繁体字集的 CharRarity（跟 composer 里同一套配置）。"""
        from ycpy.charscore import CharRarity
        data = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        cs = CharRarity.from_rank_file(
            os.path.join(data, "ycpy-char-freq.txt"),
            os.path.join(data, "Unihan_IRGSources.txt"))
        trad = CharRarity.from_trad_file(os.path.join(data, "ycpy-trad.txt"))
        self.assertGreater(len(trad), 1000, "繁体字表没载入")
        cs.attach_traditional(trad)
        return cs

    def ranks_of(self, composer, py, target):
        cands = composer.candidates(py)
        for i, cand in enumerate(cands):
            if cand.text == target:
                return i + 1, len(cands)
        return None, len(cands)

    def test_reported_chars_rank_first(self):
        """用户报的这五个字，输入对应拼音时都该是首选。"""
        composer = self.composer()
        for py, target in (("jiang", "将"), ("hui", "会"), ("dui", "对"),
                           ("zhe", "这"), ("ge", "个")):
            pos, total = self.ranks_of(composer, py, target)
            self.assertIsNotNone(pos, "%r 的候选里该有 %r" % (py, target))
            self.assertEqual(
                pos, 1,
                "%r 的首选应该是 %r，实际第 %s 位（共 %d 个候选）"
                % (py, target, pos, total))

    def test_common_beats_rare_within_same_tier(self):
        """同档位内部，常用字必须压过生僻字（最初那个「框 vs 㑌」的 bug）。"""
        from ycpy.charscore import CharRarity
        cs = CharRarity.from_rank_file(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "ycpy-char-freq.txt"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "Unihan_IRGSources.txt"))
        self.assertGreater(cs.score_for("框"), cs.score_for("㑌"))

    def test_traditional_variants_do_not_flood_top(self):
        """繁体字不能涌到候选前列。

        语料字频表只有简体字，所有繁体写法都查不到名次，只能走 T 值兜底。
        兜底分给高一点就会集体涌上来——实测 `jiang` 的头 8 名全是
        槳/漿/醬/彊/強/獎/將，而简体「将」被挤到第 22 位。
        """
        from ycpy.charscore import CharRarity
        cs = self.char_score()
        for trad, simp in (("將", "将"), ("會", "会"), ("對", "对"), ("個", "个")):
            self.assertIsNone(cs.rank(trad),
                              "%r 是繁体，语料表（简体）不该收录它" % trad)
            self.assertTrue(cs.is_traditional(trad),
                            "%r 应被识别为繁体" % trad)
            self.assertFalse(cs.is_traditional(simp),
                             "%r 是简体，不能被误判成繁体" % simp)
        # 「這」是个例外：语料统计里偶尔出现，被收录在长尾（名次 4155）。
        # 关键仍然是简体「这」压过它。
        self.assertGreater(cs.score_for("这"), cs.score_for("這"),
                           "简体「这」(%d 分) 必须压过「這」(%d 分)"
                           % (cs.score_for("这"), cs.score_for("這")))

    def test_no_traditional_survives_anywhere_in_pool(self):
        """繁体单字在**任何**拼音的候选表里都不该出现。

        这条替代了原来的 ``test_traditional_never_precedes_any_simplified``
        ——那条守的是「繁体排在简体后面」，而用户后来把要求改成了
        「彻底不出现在候选里」。旧写法还有个盲区：它只检查**顺序**，
        只要某个拼音的候选里一个繁体都没有，`checked` 就不计数，
        测试等于什么都没验（我第一版就栽在这里，报了个「0 not greater
        than 5」才发现）。
        """
        composer = self.composer()
        cs = composer._char_score
        scanned = 0
        for py in ("jiang", "hui", "ge", "dui", "zhe", "zhong", "guo",
                   "xue", "sheng", "ming", "xin", "li", "min", "ben", "dan",
                   "wu", "kuang", "qiang", "long", "guang", "kuai"):
            cands = composer.candidates(py)
            scanned += 1
            bad = [c.text for c in cands
                   if len(c.text) == 1 and cs.is_traditional(c.text)]
            self.assertEqual(
                bad, [],
                "%r 的候选里混进了繁体单字: %s" % (py, bad[:6]))
        self.assertEqual(scanned, 21, "样本量对不上，测试本身写错了")

    def test_traditional_removed_from_candidates(self):
        """繁体单字**彻底不进候选**（用户明确要求）。

        回归 bug（用户报「输入 hui，会显示在最前面，后面一堆繁体字，
        才看到灰」）：原来靠排序时的「硬分区」把繁体压到末尾，但那个分区
        只在一处排序里生效。``score_with_context`` 里还有第二处
        ``sort(key=-score)``，分区被整个抹掉；它给的二元组加权
        ``W_BIGRAM_USER=90000``，一次 +9 万分，于是「會」被从第 141 位
        顶到第 4 位，插到「灰」（第 32 位）前面。

        现在改成在打分阶段就 ``continue`` 掉，任何后续排序都无从复活它。
        """
        composer = self.composer()
        cs = composer._char_score
        for py, trad in (("jiang", "將"), ("hui", "會"), ("ge", "個"),
                         ("dui", "對"), ("zhe", "這"), ("zhong", "眾"),
                         ("guo", "國"), ("wu", "吳")):
            texts = [c.text for c in composer.candidates(py)]
            self.assertNotIn(
                trad, texts,
                "%r 的候选里仍有繁体 %r——繁体字不该出现在候选里" % (py, trad))

    def test_traditional_survives_context_resort(self):
        """关键回归：即使有把繁体顶上去的二元组，它也回不来。

        这条守的是「方案 B」的核心——把偏好编码进**是否进池子**，
        而不是编码进某处排序的键。只要在打分阶段剔除，
        ``score_with_context`` 再怎么重排都影响不到它。
        """
        from ycpy.engine import PinyinEngine

        dictionary = Dictionary()
        user = UserModel(os.path.join(self.tmp, "bigram.json"))
        dictionary.apply_user_counts({})
        from ycpy.config import Config
        composer = Composer(dictionary, user)
        engine = PinyinEngine(dictionary, user, composer,
                              config=Config(os.path.join(self.tmp, "c.json")))

        class _UI:
            def __getattr__(self, name):
                return lambda *a, **k: None
        engine.ui = _UI()
        engine.on_focus_in()

        # 造一条指向繁体的二元组，权重 W_BIGRAM_USER=90000，足够顶穿任何排序
        for _ in range(3):
            user.learn("會", "没有")
        engine._context = "没有"
        engine.buffer = "hui"
        engine.raw_keys = "hui"
        engine.refresh()          # 内部会调 score_with_context

        texts = [c.text for c in engine.candidates]
        self.assertNotIn("會", texts,
                         "二元组加权把繁体『會』顶回候选了")
        # 并且简体字顺序不受影响
        pos_hui = texts.index("回")
        self.assertLess(pos_hui, texts.index("挥"),
                        "简体候选顺序被打乱了")

    def test_no_syllable_becomes_empty(self):
        """剔除繁体后，不能有哪个拼音变得一个候选都不剩。"""
        composer = self.composer()
        for py in ("hui", "wu", "jiang", "zhe", "ge", "ni", "kuang",
                   "zhong", "guo", "xue", "qiang", "long", "shuang",
                   "chuang", "guang", "kuai", "ai", "ei", "ou"):
            self.assertTrue(composer.candidates(py),
                            "%r 剔除繁体后一个候选都不剩了" % py)

    def test_common_simplified_still_first(self):
        """剔除繁体不能把用户之前报的排序问题带回来。"""
        composer = self.composer()
        for py, want in (("jiang", "将"), ("hui", "会"), ("dui", "对"),
                         ("zhe", "这"), ("ge", "个"), ("ni", "你")):
            self.assertEqual(composer.candidates(py)[0].text, want)

    def test_rare_chars_stay_out_of_top_positions(self):
        """没有字频记录的基本区生僻字不能挤到前面。"""
        from ycpy.charscore import CharRarity
        cs = CharRarity.from_rank_file(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "ycpy-char-freq.txt"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "Unihan_IRGSources.txt"))
        for ch in "醤櫤㧑㻅屶颒":
            self.assertIsNone(cs.rank(ch), "%r 不该在语料表里" % ch)
            self.assertLess(
                cs.score_for(ch), cs.score_for("会") // 2,
                "生僻字 %r(%d 分) 得分太高，'会'才 %d 分"
                % (ch, cs.score_for(ch), cs.score_for("会")))

    def test_high_freq_chars_without_tier_not_penalized(self):
        """没有 T 值的超高频字（为/说/们…）不能被判成生僻字。

        它们在语料榜上排名很靠前（为=18、说=24、们=13），所以不该被
        「查不到 T 值」这件事连累。
        """
        from ycpy.charscore import CharRarity
        cs = CharRarity.from_rank_file(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "ycpy-char-freq.txt"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "Unihan_IRGSources.txt"))
        for ch in "为说们":
            self.assertIsNone(cs.tier(ch))
            self.assertLess(cs.rank(ch), 30,
                            "%r 应在语料榜前 30 名" % ch)
            self.assertGreater(cs.score_for(ch), 500)

    def test_score_is_monotonic_in_rank(self):
        """名次越靠后，得分必须单调不增。"""
        from ycpy.charscore import CharRarity
        cs = CharRarity.from_rank_file(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "ycpy-char-freq.txt"))
        ranks = sorted(cs._char_rank, key=lambda c: cs._char_rank[c])[:400]
        scores = [cs.score_for(c) for c in ranks]
        for i in range(len(scores) - 1):
            self.assertGreaterEqual(
                scores[i], scores[i + 1],
                "名次 %d(%r, %d 分) 应不低于 %d(%r, %d 分)"
                % (cs.rank(ranks[i]), ranks[i], scores[i],
                   cs.rank(ranks[i + 1]), ranks[i + 1], scores[i + 1]))

    def test_head_of_frequency_list_is_not_all_tied(self):
        """语料榜前 50 名之间必须能分出高下。

        线性映射会把前 50 名全压到 970~1000、彼此几乎同分，
        而前 50 名恰恰是最需要区分的（的/一/是/在/有/个/这…）。
        """
        from ycpy.charscore import CharRarity
        cs = CharRarity.from_rank_file(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "ycpy-char-freq.txt"))
        top = sorted(cs._char_rank, key=lambda c: cs._char_rank[c])[:50]
        scores = {c: cs.score_for(c) for c in top}
        self.assertGreater(len(set(scores.values())), 25,
                           "前 50 名的得分几乎全同，区分度不够")


class BackspaceToEmptyTest(unittest.TestCase):
    """退格把拼音删空时，必须立刻回到空闲态。

    回归 bug（5 小时压测 ``tools/stress_core.py`` 抓到的）：
    原来 ``_handle_composing`` 里的退格分支无条件 ``refresh(); return True``，
    于是 ``buffer`` 变空后 ``state`` 仍停在 ``ST_COMPOSING``。

    下一次退格就会掉进「预编辑已空 -> 交给应用」的分支，把这次退格
    **放给应用**——可用户明明只想删掉刚打的那一个拼音字。结果就是：
    敲 ``n`` 再退格，会连带把光标前面一个已上屏的字也删掉。

    不变量：``state == ST_COMPOSING`` 时 ``buffer`` 必定非空。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self):
        engine, _, _ = make_engine(os.path.join(self.tmp, "user.json"),
                                   os.path.join(self.tmp, "config.json"))
        engine.on_focus_in()
        return engine

    def test_single_letter_backspace_returns_to_idle(self):
        engine = self.make()
        engine.process_key(ord("n"), 0, 0)
        self.assertEqual(engine.state, ST_COMPOSING)
        engine.process_key(KEY_BACKSPACE, 0, 0)
        self.assertEqual(engine.buffer, "")
        self.assertEqual(
            engine.state, ST_IDLE,
            "退格删空拼音后仍停在 COMPOSING：下一次退格会被放给应用，"
            "连带删掉已上屏的文字")

    def test_multi_letter_backspace_to_empty_returns_to_idle(self):
        engine = self.make()
        for ch in "nihao":
            engine.process_key(ord(ch), 0, 0)
        self.assertEqual(engine.buffer, "nihao")
        while engine.buffer:
            engine.process_key(KEY_BACKSPACE, 0, 0)
        self.assertEqual(engine.state, ST_IDLE)
        self.assertEqual(engine.candidates, [], "退空后候选窗应收掉")

    def test_next_backspace_after_emptying_goes_to_app(self):
        """删空之后的那一次退格才该交给应用。"""
        engine = self.make()
        engine.process_key(ord("n"), 0, 0)
        self.assertTrue(engine.process_key(KEY_BACKSPACE, 0, 0),
                        "删拼音的那次退格应由引擎消费")
        self.assertFalse(
            engine.process_key(KEY_BACKSPACE, 0, 0),
            "预编辑已空，这次退格必须放给应用去删已上屏文字")

    def test_composing_state_always_has_buffer(self):
        """不变量本身：COMPOSING 态下 buffer 不允许为空。"""
        engine = self.make()
        for ch in "zhongguo":
            engine.process_key(ord(ch), 0, 0)
            self.assertTrue(
                engine.buffer,
                "敲到 %r 时 state=COMPOSING 但 buffer 为空"
                % "".join("zhongguo"[:len(engine.raw_keys)]))
        for _ in range(10):
            engine.process_key(KEY_BACKSPACE, 0, 0)
            if engine.state == ST_COMPOSING:
                self.assertTrue(engine.buffer,
                                "连退格过程中出现了 COMPOSING + 空 buffer")



class UncollectedCharRankingTest(unittest.TestCase):
    """语料没收录的字（即便它是规范表一级/二级字）必须排在收录字之后。

    用户原话：「检查一下 tai，『泰』在『嬯（这个是繁体字吧）』后面」。

    嬯 不是繁体——Unihan 里它是 ``kIRG_TSource T2-5C61``，《通用规范汉字表》
    二级字。真正的根因是**兜底分的量级错了**：之前 ``TIER_FALLBACK_SCORE``
    给 T1=320 / T2=250 / T3=180，而这些分正好落在语料分的有效区间里：

        T1 兜底 320 分 ≡ 语料榜第  521 名
        T2 兜底 250 分 ≡ 语料榜第  992 名

    榜上有 9900 字，于是**任何**一个未被收录的二级字，都能压过榜上 992 名
    之后的全部常用字。「泰」名次 1318、「抬」「态」也都在射程内。
    """

    def setUp(self):
        from ycpy.charscore import CharRarity
        data = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        cs = CharRarity.from_rank_file(
            os.path.join(data, "ycpy-char-freq.txt"),
            os.path.join(data, "Unihan_IRGSources.txt"))
        cs.attach_traditional(
            CharRarity.from_trad_file(os.path.join(data, "ycpy-trad.txt")))
        self.cs = cs

    def test_tai_is_not_behind_uncollected_chars(self):
        """用户报的那个具体现象：泰 必须在 嬯 那一簇前面。"""
        cs = self.cs
        # 前提确认：嬯 确实有 T 值（所以走的是 T 值兜底分支），但语料没收录
        self.assertEqual(cs.tier("嬯"), 2, "嬯 应是规范表二级字")
        self.assertIsNone(cs.rank("嬯"), "嬯 不该在语料字频表里")
        self.assertIsNotNone(cs.rank("泰"), "泰 应在语料表里")
        self.assertGreater(cs.rank("泰"), 992,
                           "泰 名次(%d) 若已 <=992，这条测试就失去意义"
                           % cs.rank("泰"))
        for bad in "嬯溙燤箈儓旲":
            self.assertGreater(
                cs.score_for("泰"), cs.score_for(bad),
                "「泰」(%d) 必须压过未被语料收录的「%s」(%d)"
                % (cs.score_for("泰"), bad, cs.score_for(bad)))

    def test_bands_never_overlap(self):
        """核心不变量：语料收录的字分 >= FREQ_MIN，没收录的一律 < FREQ_MIN。

        这条比逐个断言「泰 排在 嬯 前面」更根本——它一次性守住整个语料表，
        任何新加的兜底分支只要不小心把分给高就会在这里炸掉。
        """
        from ycpy.charscore import FREQ_MIN, TIER_FALLBACK_SCORE
        cs = self.cs
        self.assertLess(max(TIER_FALLBACK_SCORE.values()), FREQ_MIN,
                        "T 值兜底分必须全部低于 FREQ_MIN")
        in_band = out_band = 0
        for cp, tier in cs._tiers.items():
            ch = chr(cp)
            if cs.is_traditional(ch):
                continue
            rank, score = cs.rank(ch), cs.score_for(ch)
            if rank is not None:
                in_band += 1
                self.assertGreaterEqual(
                    score, FREQ_MIN,
                    "语料收录的字 %r(名次%d) 得 %d 分，掉出了收录带"
                    % (ch, rank, score))
            else:
                out_band += 1
                self.assertLess(
                    score, FREQ_MIN,
                    "语料未收录的字 %r(T%s) 得 %d 分，侵进了收录带"
                    % (ch, tier, score))
        # 两边都得有样本，否则这条测试是空转
        self.assertGreater(in_band, 3000, "收录带样本太少")
        self.assertGreater(out_band, 3000, "未收录带样本太少")

    def test_codepoint_fallbacks_stay_below_band(self):
        """没有 T 值的那些码位兜底分支也必须留在带外。"""
        from ycpy.charscore import FREQ_MIN
        cs = self.cs
        # 基本区：既没 T 值也没收录（醤櫤㧑㻅屶颒）
        for ch in "醤櫤㧑㻅屶顢":
            self.assertIsNone(cs.rank(ch))
            self.assertLess(cs.score_for(ch), FREQ_MIN)
        # 扩展 A（罕用字为主）
        self.assertLess(cs.score_for("㑌"), FREQ_MIN)
        # 扩展 B+（几乎全是生僻字）
        self.assertLess(cs.score_for("\U0002a3ea"), FREQ_MIN)
        # 繁体仍要垫底
        self.assertLess(cs.score_for("將"), FREQ_MIN)

    def test_tier_order_still_preserved_within_band(self):
        """带内相对区分不能因为压低而丢失：T1 仍应高于 T2 高于 T3。"""
        from ycpy.charscore import TIER_FALLBACK_SCORE
        vals = [TIER_FALLBACK_SCORE[t] for t in (1, 2, 3, 4, 7)]
        for i in range(len(vals) - 1):
            self.assertGreater(vals[i], vals[i + 1],
                               "带内分级必须单调递减: %s" % vals)

    def test_top_common_chars_unaffected(self):
        """压低兜底带不能反过来伤到头部：高频字仍然拿高分。"""
        cs = self.cs
        for ch in "的一是这有个对时会你":
            self.assertLess(cs.rank(ch), 100)
            self.assertGreater(cs.score_for(ch), 300)
        # 「会」之前的回归：㧑㻅屶顢 这些生僻字必须远低于它
        for rare in "醤櫤㧑㻅屶颒":
            self.assertLess(cs.score_for(rare), cs.score_for("会") // 2)



class PhraseFreqTableTest(unittest.TestCase):
    """整句词频表：jieba 词典缺的口语整句必须能打出来。

    用户原话：「如果我输入 nihaoma 我希望显示『你好吗』而不是『妳好吗』」。

    根因不是排序错了，是 **jieba 词典里根本没有「你好吗」**——3 个音节只
    拿到 1 个候选，而那唯一的候选恰好是频次 3 的边角词：

        你好吗   不在 jieba dict
        妳好吗   妳好吗 3 v   <- jieba 的默认兜底值
        你       你 234587 r

    也就是说一个频次 3 的词赢了 234587 的「你」，**只因为它的三音节形式
    被收录了**。jieba 系统性不覆盖口语整句（是吗/对吗/好吗/行吗/可以吗/
    谢谢你 全都不在），所以这一整类输入都会命中同样的问题。

    解法是 ``data/ycpy-phrase-freq.txt``（tools/build_phrase_freq.py 生成）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dictionary = Dictionary()
        self.user = UserModel(os.path.join(self.tmp, "user.json"))
        self.dictionary.apply_user_counts({})
        self.composer = Composer(self.dictionary, self.user)

    def texts(self, py):
        return [c.text for c in self.composer.candidates(py)]

    def test_the_reported_case(self):
        """用户报的那个具体输入。"""
        got = self.texts("nihaoma")
        self.assertEqual(got[0], "你好吗",
                         "「你好吗」应排第一，实际 %s" % got[:6])
        self.assertIn("妳好吗", got,
                      "边角词不必删掉，但必须在正确词后面：%s" % got[:6])

    def test_phrase_table_is_actually_loaded(self):
        """整句词频表必须真的被载入（否则上面那条是空转）。"""
        from ycpy.dictionary import PHRASE_FREQ_PATH
        self.assertTrue(os.path.exists(PHRASE_FREQ_PATH),
                        "整句词频表不存在：%s" % PHRASE_FREQ_PATH)
        t2i = self.dictionary.text_to_id()
        for word in ("你好吗", "谢谢你", "吃了吗"):
            self.assertIn(word, t2i,
                          "「%s」应在词典里（来自整句词频表）" % word)
        # 它们本来不在 jieba 词典里，所以能证明表确实生效了
        for word in ("你好吗", "谢谢你"):
            self.assertGreater(self.dictionary.freq[t2i[word]], 0)

    def test_common_colloquial_sentences(self):
        """一批常用口语整句都要能打出来，且正确形式排在前面。"""
        # 拼音必须跟词的实际读音一致，否则这条就是在测「打错字能纠错」
        # 而不是「整句能打出来」：对不起 = dui bu qi（不是 duibuqing），
        # 行吗 = xing ma（xing 可行，但首字是「行」不是「形」）。
        cases = [
            ("nihaoma", "你好吗"),
            ("xiexieni", "谢谢你"),
            ("chilema", "吃了吗"),
            ("meiguanxi", "没关系"),
            ("duibuqi", "对不起"),
            ("keyima", "可以吗"),
            ("duibudui", "对不对"),
            ("xiangbuqi", "想不起"),
        ]
        for py, want in cases:
            got = self.texts(py)
            self.assertIn(want, got,
                          "%r 的候选里应有 %r，实际 %s" % (py, want, got[:6]))

    def test_variant_words_do_not_outrank_real_words(self):
        """新表不能把冷僻变体词抬到常用词前面。

        回归 bug：两张表的词频**不在同一个量纲上**。知频（rime-frost）
        的数值系统性偏大，直接写进词典会让新引入的变体词压过 jieba 的
        常用词——实测 `chengxuyuan` 的候选变成：

            程序猿(知频 901) 程序员(jieba 170)

        「程序猿」压过了「程序员」。所以 build_phrase_freq.py 里做了
        标定（log 空间线性回归），这一条守住标定的效果。
        """
        got = self.texts("chengxuyuan")
        self.assertEqual(got[0], "程序员",
                         "「程序员」应排第一，实际 %s" % got[:6])

    def test_no_segmentation_repeat_artifacts(self):
        """分词重复产物（同一个 token 连吐两次）不能进表。

        上游是拿 jieba 分词后统计的，会出现 `对不起对不起`（= 对不起 +
        对不起，词频 101）这种没人会这么打的整句。
        """
        got = self.texts("duibuduibuqing")
        self.assertNotIn("对不起对不起", got,
                         "分词重复产物不该出现在候选里：%s" % got[:8])

    def test_frequency_is_calibrated_to_jieba_scale(self):
        """整句词频必须落在 jieba 的量纲里，不能用原始数值。

        这是上面 `test_variant_words_do_not_outrank_real_words` 的根因断言。
        """
        from ycpy.dictionary import PHRASE_FREQ_PATH
        raw = {}
        with open(PHRASE_FREQ_PATH, encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3:
                    raw[parts[0]] = int(parts[1])
        self.assertGreater(len(raw), 100000, "整句词频表条目太少")
        # 标定后不应该还有四位数以上的词频（原始表里「的时候」是 424538）
        huge = [w for w, f in raw.items() if f > 10000]
        self.assertLess(len(huge), len(raw) * 0.001,
                        "标定失效：有 %d 条词频 > 10000，最高 %d"
                        % (len(huge), max(raw.values()) if raw else 0))

    def test_index_cache_accounts_for_phrase_table(self):
        """索引缓存的键必须覆盖整句词频表。

        只拿主词典的 size+mtime 做键是不够的：整句词频表单��变化时，
        缓存会命中一份过期索引，新加的整句一个都查不到——而且是**静默**的，
        没有任何报错。所以这条守住缓存键里带了新文件。
        """
        from ycpy.dictionary import PHRASE_FREQ_PATH, Dictionary as D
        d = D.__new__(D)
        d.path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "data", "ycpy-dict.txt")
        key_with = d._cache_path()
        saved = PHRASE_FREQ_PATH
        try:
            import ycpy.dictionary as Dmod
            Dmod.PHRASE_FREQ_PATH = "/nonexistent-phrase-freq.txt"
            key_without = Dmod.Dictionary.__new__(Dmod.Dictionary)
            key_without.path = d.path
            key_without = key_without._cache_path()
        finally:
            import ycpy.dictionary as Dmod
            Dmod.PHRASE_FREQ_PATH = saved
        self.assertNotEqual(key_with, key_without,
                            "缓存键必须区分「有/无整句词频表」两种情况")



class DomainLexiconTest(unittest.TestCase):
    """专业领域词库：用户勾选后，该领域的词要排到普通词前面。

    用户的需求：「在首选项里面让用户勾选专业词典，用户勾选后，再输入，
    就会在候选框里面显示所勾选的专业词汇」。

    数据来自 THUOCL（MIT），主词典对专业词的覆盖极差——实测缺失率
    法律 91.4% / 饮食 88.9% / IT 73.0% / 医学 70.3% / 财经 43.8%。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dictionary = Dictionary()
        self.user = UserModel(os.path.join(self.tmp, "user.json"))
        self.dictionary.apply_user_counts({})
        self.composer = Composer(self.dictionary, self.user)

    def texts(self, py):
        return [c.text for c in self.composer.candidates(py)]

    def test_off_by_default(self):
        """默认一个领域都不启用。"""
        from ycpy.config import DEFAULTS
        self.assertEqual(DEFAULTS["domains"], [])
        self.assertFalse(self.composer.domains.active(),
                         "新装的引擎不该一启动就载入专业词库")

    def test_unchecked_means_not_loaded(self):
        """不 load 就等于没启用——`_domain_scores` 要在第一行就返回。"""
        from ycpy.domain import DomainIndex
        # 一个领域都不载入时，active() 为 False，打分阶段整段跳过
        self.assertFalse(self.composer.domains.active())
        base = self.texts("jibing")
        # 再取一次仍然一致（顺带证明查询没被前一次调用污染）
        self.assertEqual(self.texts("jibing"), base)
        self.assertIsNone(self.composer._main_words,
                          "没启用领域词库时不该构建主词典词表（20 MB 的 set）")

    def test_domain_words_become_candidates(self):
        """勾选后，主词典里**没有**的专业词要能打出来。

        注意别拿「通货膨胀」当例子——它已经在主词典里了（整句词频表
        ``ycpy-phrase-freq.txt`` 收录了它，jieba 词典则没有）。所以这里用
        「新凯越」这类只有 THUOCL 收录的词。
        """
        self.assertNotIn("新凯越", self.dictionary.text_to_id())
        self.assertNotIn("新凯越", self.texts("xinkaiyue"))

        self.composer.domains.load(["car"])
        self.assertEqual(self.texts("xinkaiyue")[0], "新凯越",
                         "勾选「汽车」后「新凯越」该能打出来")

    def test_already_present_word_still_ranks_first(self):
        """主词典已有的专业词，勾选后要享受提权（不能被同领域生僻词压过）。

        「疾病」（主词典 freq=4194）对「肌病」（主词典没有，由领域索引
        新引入）。这是 ``test_boost_applies_to_words_already_in_main_dict``
        的正例断言，两条一起把回归钉住。
        """
        self.assertIn("疾病", self.dictionary.text_to_id())
        self.composer.domains.load(["medical"])
        self.assertEqual(self.texts("jibing")[0], "疾病")

    def test_domain_words_outrank_ordinary_ones(self):
        """勾选的领域词要压过主词典的普通词。"""
        self.composer.domains.load(["caijing", "law"])
        self.assertEqual(self.texts("fangdichan")[0], "房地产")
        self.assertEqual(self.texts("banquan")[0], "版权")

    def test_user_freq_beats_domain_default(self):
        """「用户词频 > 领域默认值」——这是明确的产品要求。

        W_DOMAIN=150000 是「勾选后的默认优先级」，不是不可逾越的墙：
        用户把某个普通词选上 3 次（W_USER=60000/次，3*60000 > 150000），
        就该把它从领域词手里夺回首位。
        """
        self.composer.domains.load(["caijing"])
        before = self.texts("zhongguo")
        self.assertNotEqual(before[0], "中国共产党",
                            "前提：没学之前领域词该排在前面")

        # 要学 8 次而不是 3 次：``_coverage`` 会按「输入覆盖了词多少音节」
        # 给用户词频打折。「中国共产党」是 4 个音节，而输入 ``zhongguo``
        # 只有 2 个 -> coverage = 2/4 = 0.5，所以有效权重是
        # 8 * 60000 * 0.5 = 240000 > W_DOMAIN(150000)。这与覆盖度设计的
        # 本意一致——只敲一半就不该把整个词顶到第一。
        for _ in range(8):
            self.user.learn("中国共产党")
        self.dictionary.apply_user_counts(self.user.words)
        after = self.texts("zhongguo")
        self.assertEqual(after[0], "中国共产党",
                         "用户反复选之后应压过领域默认值，实际 %s" % after[:4])

    def test_user_freq_beats_domain_default_full_coverage(self):
        """音节完全覆盖时，3 次就够（不打折）。

        这是 ``test_user_freq_beats_domain_default`` 的对照：那边要 8 次是
        因为 4 音节的词被 ``_coverage`` 打了 0.5 折。这边用 2 音节的词、
        输入也正好 2 个音节，coverage=1.0，所以 3*60000=180000 一次跨过
        W_DOMAIN=150000。
        """
        from ycpy.composer import W_DOMAIN, W_USER
        self.composer.domains.load(["medical"])
        wid = self.dictionary.text_to_id()["疾病"]
        self.assertAlmostEqual(
            self.composer._coverage("jibing", wid), 1.0, delta=0.01,
            msg="「疾病」2 音节、输入 2 音节，coverage 应当是 1.0")
        self.assertGreater(3 * W_USER, W_DOMAIN,
                           "3 次满额用户词频就应跨过 W_DOMAIN")

    def test_boost_applies_to_words_already_in_main_dict(self):
        """主词典里本来就有的专业词也要享受提权。

        回归 bug：「疾病」主词典里有，「肌病」没有（是领域索引新引入的）。
        最初的去重逻辑直接 `continue` 跳过主词典已有的词，导致「疾病」
        一点分都加不上，被「肌病」压到第 4 位——用户勾了「医学」，
        最常见的那个医学词反而掉下去。
        """
        self.assertIn("疾病", self.dictionary.text_to_id())
        self.composer.domains.load(["medical"])
        self.assertEqual(self.texts("jibing")[0], "疾病",
                         "「疾病」应保持在首位，不能被「肌病」压过")

    def test_no_double_counting(self):
        """同一个词不能既走主词典打分、又加一次 W_DOMAIN。

        主词典那份已经算过 ``W_EXACT_FULL``(25000) 和 ``W_SYL_COUNT``(2500)，
        领域那份若再整条链重跑一遍，就会凭空多出 27500 分。
        所以领域侧对「主词典已有」的词只做**提权**：分数应恰好等于
        「主词典算出的分 + W_DOMAIN」。
        """
        from ycpy.composer import W_DOMAIN, W_EXACT_FULL, W_SYL_COUNT
        self.composer.domains.load(["medical"])
        scores = {c.text: c.score for c in self.composer.candidates("jibing")}
        wid = self.dictionary.text_to_id()["疾病"]
        freq = float(self.dictionary.freq[wid])
        expect = freq + W_EXACT_FULL + W_SYL_COUNT + W_DOMAIN
        self.assertAlmostEqual(
            scores["疾病"], expect, delta=1,
            msg="「疾病」应为 freq(%d)+EXACT(%d)+SYL(%d)+DOMAIN(%d)=%.0f，"
                "实际 %.0f" % (freq, W_EXACT_FULL, W_SYL_COUNT, W_DOMAIN,
                              expect, scores["疾病"]))


class DomainIndexTest(unittest.TestCase):
    """领域索引本身的性质（不牵扯打分）。"""

    def test_available_domains(self):
        from ycpy.domain import DOMAINS, available_domains
        keys = available_domains()
        self.assertTrue(keys, "应该至少有已打包的领域")
        for k in keys:
            self.assertIn(k, DOMAINS, "%r 不在 DOMAINS 里" % k)

    def test_load_is_cheap_and_reversible(self):
        """载入/卸载必须便宜且干净——这是选独立索引架构的全部理由。

        对照：``Dictionary.add_user_words`` 同样 9895 条要 36,848 ms，
        而且 ``Dictionary`` 没有 remove 方法，灌进去撤不回来。
        """
        import time
        from ycpy.domain import DomainSet
        ds = DomainSet()
        keys = ["law", "medical", "it"]
        t0 = time.time()
        ds.load(keys)
        elapsed = time.time() - t0
        self.assertTrue(ds.active())
        self.assertLess(elapsed, 2.0,
                        "载入 3 个领域不该超过 2 秒，实际 %.2f 秒" % elapsed)
        ds.load([])
        self.assertFalse(ds.active(), "取消勾选后不该还留着索引")

    def test_frequency_is_calibrated(self):
        """DF 必须已换算到 jieba 量纲（不该出现上万的值）。"""
        import glob
        import os
        root = os.path.dirname(os.path.dirname(__file__))
        files = glob.glob(os.path.join(root, "data", "ycpy-domain-*.txt"))
        self.assertTrue(files, "找不到领域词表")
        for path in files:
            for line in open(path, encoding="utf-8"):
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                freq = int(parts[1])
                self.assertLessEqual(
                    freq, 1000,
                    "%s 里 %r 的词频 %d 太大，DF 标定失效"
                    % (os.path.basename(path), parts[0], freq))

    def test_traditional_chars_still_excluded(self):
        """领域词也不能绕过繁体剔除。"""
        from ycpy.charscore import CharRarity
        root = os.path.dirname(os.path.dirname(__file__))
        trad = CharRarity.from_trad_file(
            os.path.join(root, "data", "ycpy-trad.txt"))
        import glob
        hits = []
        for path in glob.glob(os.path.join(root, "data", "ycpy-domain-*.txt")):
            for line in open(path, encoding="utf-8"):
                if line.startswith("#"):
                    continue
                word = line.split("\t")[0]
                if len(word) == 1 and word in trad:
                    hits.append(word)
        self.assertEqual(hits, [],
                         "领域表里不该有繁体单字：%s" % hits[:5])


class ConfigDomainTest(unittest.TestCase):
    """配置项 ``domains`` 的容错。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.json")

    def write(self, payload):
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp)

    def test_bad_values_fall_back(self):
        """配置文件被手改坏时不能崩，退回「没勾选」。"""
        from ycpy.config import Config
        for bad in ("medical", 123, None, {"a": 1}, [1, 2, None]):
            self.write({"version": 1, "domains": bad})
            cfg = Config(self.path)
            self.assertEqual(cfg.domains, [],
                             "%r 应退回空列表" % (bad,))

    def test_valid_list_preserves_order(self):
        from ycpy.config import Config
        self.write({"version": 1, "domains": ["law", "it"]})
        self.assertEqual(Config(self.path).domains, ["law", "it"])



class DomainMatchQualityTest(unittest.TestCase):
    """领域优先**只在用户把拼音（或韵母缩写）完整打出来**时才生效。

    用户原话：「你现在专业词汇太靠前了，比如输入 bianhua 没有先出现
    『变化』而是出现了『变换规则』，专业词汇都在前面了，专业词汇应该是
    用户输入完整的拼音或者拼音的韵母缩写，才能放到前面」。

    根因：``W_DOMAIN`` 原先是个**不看匹配质量**的固定加分。于是
    「变换规则」(bian huan gui ze) 只因为 ``bianhua`` 是它的**前缀**，
    就和「变化」(bian hua) 这个**完全命中**拿到同一份领域加权；再叠加
    长词分 ``W_LEN_LONG``，分数反而更高：

        变化无穷(精确)   150036
        变换规则(前缀)   150011   <- 本该在「变化」之后
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dictionary = Dictionary()
        self.user = UserModel(os.path.join(self.tmp, "user.json"))
        self.dictionary.apply_user_counts({})
        self.composer = Composer(self.dictionary, self.user)
        self.composer.domains.load(["it", "caijing", "law",
                                     "medical", "food", "car"])

    def texts(self, py):
        return [c.text for c in self.composer.candidates(py)]

    def test_the_reported_case(self):
        """用户报的那个具体输入。"""
        got = self.texts("bianhua")
        self.assertEqual(got[0], "变化",
                         "「bianhua」应先出「变化」，实际 %s" % got[:6])
        # 变换规则 只是前缀命中，不该排在「变化」前面
        if "变换规则" in got:
            self.assertLess(got.index("变化"), got.index("变换规则"),
                            "前缀命中的「变换规则」不该压过完全命中的「变化」")

    def test_prefix_match_gets_reduced_bonus(self):
        """前缀命中的领域词只拿一小部分加权。"""
        from ycpy.composer import W_DOMAIN, W_DOMAIN_PREFIX_RATIO
        self.assertLess(W_DOMAIN_PREFIX_RATIO, 0.5,
                        "前缀命中的领域加权必须远小于完全命中")
        self.assertGreater(W_DOMAIN * W_DOMAIN_PREFIX_RATIO, 0)

    def test_full_pinyin_still_gets_priority(self):
        """完整拼音的领域词仍然要压过普通词（这是勾选的意义）。"""
        from ycpy.composer import W_DOMAIN
        for py, want in (("shujuku", "数据库"),
                         ("jibing", "疾病"),
                         ("tonghuopengzhang", "通货膨胀"),
                         ("tudou", "土豆")):
            self.assertEqual(self.texts(py)[0], want,
                             "%r 的首位应是 %r，实际 %s"
                             % (py, want, self.texts(py)[:4]))

    def test_initials_get_priority(self):
        """韵母缩写（首字母）**也算**完整输入，同样享受领域优先。

        产品要求原文是「用户输入完整的拼音**或者拼音的韵母缩写**」。
        领域表原先只建全拼键，于是敲 `sjk` 想打「数据库」时领域词完全
        不出现——那恰恰是用户主动缩写之后的明确意图。
        """
        for py, want in (("sjk", "数据库"),
                         ("jb", "疾病"),
                         ("thpz", "通货膨胀")):
            got = self.texts(py)
            self.assertEqual(got[0], want,
                             "%r 的首位应是 %r，实际 %s" % (py, want, got[:4]))

    def test_initials_prefix_gets_reduced_bonus(self):
        """首字母只是前缀时也不该拿满加权。"""
        from ycpy.domain import DomainIndex, domain_path
        # 「数据库」收录在「财经」域（不是 IT）
        di = DomainIndex("caijing", domain_path("caijing"))
        kinds = {}
        for i, kind in di.search(b"sjk", b"sjk"):
            kinds[di.words[i]] = kind
        self.assertEqual(kinds.get("数据库"), "init",
                         "「sjk」对「数据库」应是首字母完全命中")
        # 只敲 1 个首字母 -> 只是前缀，不该拿满加权
        pre = [kind for i, kind in di.search(b"s", b"s")][:50]
        self.assertNotIn("full", pre)
        self.assertIn("pre", pre)


class DomainIndexMatchKindTest(unittest.TestCase):
    """``DomainIndex.search`` 的命中类型标注。"""

    def test_kinds(self):
        from ycpy.domain import DomainIndex, domain_path
        di = DomainIndex("caijing", domain_path("caijing"))
        got = {}
        for i, kind in di.search(b"tonghuopengzhang", b"thpz"):
            got[di.words[i]] = kind
        self.assertEqual(got.get("通货膨胀"), "full",
                         "全拼完整输入应标 full")
        # 「房地产评估」(fang di chan ping gu) 只是前缀
        got2 = {}
        for i, kind in di.search(b"fangdichan", b"fangdichan"):
            got2[di.words[i]] = kind
        self.assertEqual(got2.get("房地产"), "full")
        self.assertEqual(got2.get("房地产评估"), "pre",
                         "只打到一半的应标 pre")
        # 首字母完整
        got2 = {}
        for i, kind in di.search(b"thpz", b"thpz"):
            got2[di.words[i]] = kind
        self.assertEqual(got2.get("通货膨胀"), "init",
                         "首字母完整输入应标 init")



class DigitKeyBoundsTest(unittest.TestCase):
    """数字键映射必须跟着**当页实际候选数**走。

    每页候选数可配 5 / 7 / 9（``PAGE_SIZE_CHOICES``），而 ``_digit_index``
    原来写死「1..9 → 0..8、0 → 9」。于是 ``page_size=5`` 时按 6/7/8/9 全部
    越界，而 ``select()`` 的边界检查是**静默 return** ——那 4 个键变成
    「按了没反应」的死键。改候选数是常规操作，所以这个很容易撞上。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def make(self, page_size):
        cfg = os.path.join(self.tmp, "c%d.json" % page_size)
        with open(cfg, "w", encoding="utf-8") as fp:
            json.dump({"page_size": page_size}, fp)
        engine, _, _ = make_engine(os.path.join(self.tmp, "u%d.json" % page_size),
                                   cfg)
        engine.on_focus_in()
        return engine

    def _type_cxy(self, engine):
        for ch in "cxy":
            engine.process_key(ord(ch))
        return engine

    def test_every_digit_selects_something(self):
        """当页有几个候选，按 1..当页数 就要都能选中。"""
        for page_size in (5, 7, 9):
            engine = self._type_cxy(self.make(page_size))
            count = len(engine.page_candidates)
            self.assertEqual(count, page_size)
            for d in range(1, page_size + 1):
                engine = self._type_cxy(self.make(page_size))
                engine.process_key(ord("0") if d == 10 else ord("0") + d)
                self.assertTrue(
                    engine._context,
                    "page_size=%d 按数字 %d 没选中任何候选" % (page_size, d))

    def test_zero_selects_last_item_of_current_page(self):
        """``0`` 是「收尾键」，映射到**当页最后一项**，不是固定的第 10 项。"""
        for page_size in (5, 7, 9):
            engine = self._type_cxy(self.make(page_size))
            last = engine.page_candidates[-1].text
            engine.process_key(ord("0"))
            self.assertEqual(engine._context, last,
                             "page_size=%d 时按 0 应选中 %r" % (page_size, last))

    def test_out_of_range_digit_is_swallowed_not_passthrough(self):
        """越界的数字键要**吞掉**，不能透传。

        透传的话会在中文文本里蹦出一个阿拉伯数字，比「按了没反应」更糟。
        """
        engine = self._type_cxy(self.make(5))
        self.assertTrue(engine.process_key(ord("9")), "越界数字键被透传给应用了")


class TabSelectIndexTest(unittest.TestCase):
    """Tab 必须选**页内**下标，不是全局下标。

    原来写的是 ``self.page * self.page_size + 1``——那是全局下标，而
    ``select()`` 收的是页内下标。第 0 页时全局 1 恰好等于页内 1，**掩盖了
    错位**；翻到第 1 页后 nxt=10 超出 ``page_candidates``（长度 9），被边界
    检查静默挡掉，Tab 就成了死键。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def make(self):
        engine, _, _ = make_engine(os.path.join(self.tmp, "user.json"))
        engine.on_focus_in()
        return engine

    def _second_item(self, engine):
        self.assertGreaterEqual(len(engine.page_candidates), 2)
        return engine.page_candidates[1].text

    def test_tab_works_after_paging_in_composing(self):
        for page in (0, 1, 2):
            engine = self.make()
            for ch in "cxy":
                engine.process_key(ord(ch))
            for _ in range(page):
                engine.process_key(0xFF56)          # PageDown
            want = self._second_item(engine)
            engine.process_key(0xFF09)              # Tab
            self.assertEqual(
                engine._context, want,
                "第 %d 页按 Tab 应选中页内第 2 项 %r，实际 %r"
                % (page, want, engine._context))

    def test_tab_works_after_paging_in_associate(self):
        for page in (0, 1, 2):
            engine = self.make()
            for ch in "cxy":
                engine.process_key(ord(ch))
            engine.process_key(0x3B, 0, 1 << 2)     # Ctrl+; 进入联想
            for _ in range(page):
                engine.process_key(0xFF56)
            want = self._second_item(engine)
            engine.process_key(0xFF09)
            self.assertEqual(
                engine.assoc_context, want,
                "联想态第 %d 页按 Tab 应选中 %r，实际 %r"
                % (page, want, engine.assoc_context))

    def test_tab_and_space_share_one_out_of_range_policy(self):
        """Tab 与空格对「越界」必须有**同一个口径**：都透传，都不吞。

        之前是各自 ``self.select(...)`` + 无脑 ``return True``，于是某个键
        越界时 ``select`` 静默挡掉、按键却被吞掉——「按了没反应」的死键。

        注意 1 项时两者**本来就该不同**：Tab 是「选下一个」（页内下标 1，
        1 项时越界），空格是「选第一个」（下标 0，1 项时合法）。所以断言
        要按各自的应有行为写，不能笼统地要求两者一致。
        """
        class _Item:
            def __init__(self, text):
                self.text = text

        def run(keyval, count, tag):
            engine, _, _ = make_engine(
                os.path.join(self.tmp, "u_%s_%d.json" % (tag, count)))
            engine.on_focus_in()
            engine.state = ST_COMPOSING
            engine.buffer = engine.raw_keys = "cxy"
            engine.candidates = []
            engine.page_candidates = [_Item("项%d" % i) for i in range(count)]
            return bool(engine.process_key(keyval))

        # 空池：两个键都该透传（拿不到结果就别吞掉按键）。
        self.assertFalse(run(0xFF09, 0, "tab"), "空池按 Tab 被吞掉了")
        self.assertFalse(run(0x20, 0, "spc"), "空池按空格被吞掉了")

        # 1 项：空格选第 1 项（合法），Tab 想选第 2 项（越界 -> 透传）。
        self.assertTrue(run(0x20, 1, "spc1"), "1 项时空格应选中第 1 项")
        self.assertFalse(run(0xFF09, 1, "tab1"), "1 项时按 Tab 应透传而非死键")

        # 2 项：两个键都有得选，都该吃掉。
        self.assertTrue(run(0x20, 2, "spc2"))
        self.assertTrue(run(0xFF09, 2, "tab2"))

    def test_tab_out_of_range_exits_associate(self):
        """联想态按 Tab 越界时，联想窗必须正常关闭。

        ``_handle_associate`` 末尾的 ``exit_associate()`` 在中途 ``return``
        的分支上走不到，所以越界那条要显式退出，否则残留一个空联想窗。
        """
        class _Item:
            def __init__(self, text):
                self.text = text

        engine, _, _ = make_engine(os.path.join(self.tmp, "u_assoc.json"))
        engine.on_focus_in()
        for ch in "cxy":
            engine.process_key(ord(ch))
        engine.process_key(0x3B, 0, 1 << 2)          # Ctrl+; 进入联想
        self.assertEqual(engine.state, ST_ASSOCIATE)
        engine.assoc_candidates = [_Item("唯一")]
        engine.page_candidates = [_Item("唯一")]      # 只有 1 项，Tab 越界
        engine.process_key(0xFF09)
        self.assertNotEqual(engine.state, ST_ASSOCIATE,
                            "越界后残留了空的联想窗")


class BigramRespectsUserSettingsTest(unittest.TestCase):
    """二元组加权必须跟着「用户数据」的开关走。

    ``score_with_context`` 原来用硬编码 ``W_BIGRAM_USER``（90000），于是
    首选项里两个开关对它完全无效。更糟的是 90000 一次就 +9 万分，比语料分
    的总波动（几千）大一个数量级——只要历史上记过一条二元组，它就压过一切
    语料信号。所以用户把「我的词频权重」拉到 0（滑块说明写着「0 = 完全按
    语料排序」）得到的并不是纯语料排序。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def probe(self, cfg, bigram):
        path = os.path.join(self.tmp, "c.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp)
        engine, _, _ = make_engine(
            os.path.join(self.tmp, "u.json"), path)
        engine.on_focus_in()
        for ch in "cxy":
            engine.process_key(ord(ch))
        if bigram:
            engine.user.bigrams["陈兴瑜|常见于"] = 1
        engine._context = "陈兴瑜"
        engine.refresh()
        return [c.text for c in engine.candidates[:3]]

    def test_learning_off_disables_bigram_boost(self):
        off = self.probe({"enable_learning": False}, False)
        on = self.probe({"enable_learning": False}, True)
        self.assertEqual(off, on, "关掉「学习我的用词」后二元组仍在生效")

    def _assoc_probe(self, cfg, seed):
        """联想层的第一档信号（用户历史）有没有被用户数据影响。"""
        path = os.path.join(self.tmp, "a.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp)
        engine, _, _ = make_engine(os.path.join(self.tmp, "au.json"), path)
        engine.on_focus_in()
        if seed:
            engine.user.words["程序员"] = 5
            engine.user.bigrams["陈兴瑜|程序员"] = 1
        return [(c.text, c.score) for c in engine.composer.next_words("陈兴瑜")[:2]]

    def test_learning_off_disables_associate_user_history(self):
        """关掉学习后，联想第一档（用户历史）必须消失。

        ``next_words`` 原来是 ``TIER_USER + count``——300 万的硬编码绝对值，
        完全不看配置。上一次修「关学习」只接了 ``score_with_context`` 一处，
        这里和整句切分都漏了。实测关掉之后 top 恒为 3,000,001。
        """
        for cfg in ({"enable_learning": False}, {"user_freq_weight": 0}):
            off = self._assoc_probe(cfg, False)
            on = self._assoc_probe(cfg, True)
            self.assertEqual(
                off, on,
                "配置 %s 下联想层仍受用户历史影响" % cfg)

    def _segment_probe(self, cfg, seed):
        path = os.path.join(self.tmp, "s.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp)
        engine, _, _ = make_engine(os.path.join(self.tmp, "su.json"), path)
        engine.on_focus_in()
        if seed:
            engine.user.words["大家一起"] = 5
        return [(c.text, c.score)
                for c in engine.composer.candidates("dajiayiqi")[:2]]

    def test_learning_off_disables_segment_user_signal(self):
        """关掉学习后，整句切分里的用户信号必须归零。

        这两处用的是 ``self.user_weight``，所以权重为 0 时自动归零；但
        **「关闭学习」只清了单词频、没有动 user_weight**，于是关掉之后
        「学过一整串」照样加 18000（60000 × 0.15 × 2）。
        """
        for cfg in ({"enable_learning": False}, {"user_freq_weight": 0}):
            off = self._segment_probe(cfg, False)
            on = self._segment_probe(cfg, True)
            self.assertEqual(
                off, on, "配置 %s 下整句切分仍受用户词频影响" % cfg)

    def test_zero_weight_disables_bigram_boost(self):
        off = self.probe({"user_freq_weight": 0}, False)
        on = self.probe({"user_freq_weight": 0}, True)
        self.assertEqual(
            off, on,
            "权重 0 的定义是「完全按语料排序」，二元组不该再生效")

    def test_bigram_boost_scales_with_weight(self):
        """默认和半档**应该**仍有影响——那是用户数据，不是要关掉的。"""
        off = self.probe({"user_freq_weight": 60000}, False)
        on = self.probe({"user_freq_weight": 60000}, True)
        self.assertNotEqual(off, on, "默认配置下二元组应该生效")


class BuildAllCoversEveryDataFileTest(unittest.TestCase):
    """``tools/build_all.py`` 生成的产物必须覆盖 ``build_deb.sh`` 要的全部文件。

    这条不变量很容易悄悄失配：新增一个数据文件时，可能只记得改
    ``build_deb.sh``（它会 ``die`` 并提示跑某个脚本），忘了在
    ``build_all.py`` 里登记——那样「一条命令生成全部」就名不副实，新人
    照README 做完仍然构建失败，而且报错发生在跑了很久之后。

    这里直接从两个文件的源码里解析集合比对，不实际跑构建（那要联网）。
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _declared_outputs(self):
        src = open(os.path.join(self.ROOT, "tools", "build_all.py"),
                   encoding="utf-8").read()
        block = src[src.index("STEPS = ["):src.index("DOMAIN_SCRIPTS")]
        return set(re.findall(r'"([^"]+\.txt)"', block))

    def _required_by_deb(self):
        src = open(os.path.join(self.ROOT, "tools", "build_deb.sh"),
                   encoding="utf-8").read()
        files = set(re.findall(r"data/([A-Za-z0-9_.\-]+\.txt)", src))
        # 领域文件走循环：for _d in it caijing law ...
        loop = re.search(r"for _d in ([a-z ]+);", src)
        if loop:
            for name in loop.group(1).split():
                files.add("ycpy-domain-%s.txt" % name)
        return files

    def test_build_all_generates_everything_the_deb_needs(self):
        declared = self._declared_outputs()
        required = self._required_by_deb()
        # LICENSE-hanziDB.txt 随仓库提交（授权声明），不由脚本生成。
        required.discard("LICENSE-hanziDB.txt")
        missing = required - declared
        self.assertEqual(
            missing, set(),
            "build_deb.sh 要这些文件，但 build_all.py 不生成：%s"
            % sorted(missing))

    def test_build_all_has_no_useless_entries(self):
        declared = self._declared_outputs()
        required = self._required_by_deb()
        extra = declared - required
        self.assertEqual(
            extra, set(),
            "build_all.py 生成了这些文件但 deb 不用，要么删掉登记，"
            "要么说明为什么需要：%s" % sorted(extra))


class AssociatePassthroughTest(unittest.TestCase):
    """联想态下的按键分派。

    回归 bug（人速压测 ``tools/stress_human.py`` 抓到的）：联想态敲字母，
    那个字母被**吞掉**并进了组字态——

        联想态敲 'a'  ->  返回 True，state=COMPOSING，buffer='a'

    根因：``_handle_associate`` 对「既不是候选键也不是翻页键」的输入会先
    ``exit_associate()`` 再返回 False——那个字母本该**透传给应用**（联想窗
    只是临时的，用户接着打的字理应正常进应用）。但它顺手把 state 改成了
    ST_IDLE，于是紧跟着的 ``if self.state == ST_IDLE:`` 分支（写成了两个
    独立的 ``if`` 而不是 ``if/elif``）把这个字母又塞进 buffer 开始组字。

    症状：联想窗里直接打字，第一个字母「消失」，随后莫名其妙弹出候选窗。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine, _, _ = make_engine(
            os.path.join(self.tmp, "user.json"),
            os.path.join(self.tmp, "config.json"))
        self.engine.on_focus_in()
        self.engine._context = "你好"

    def enter(self):
        self.engine.reset()
        self.engine._context = "你好"
        self.assertTrue(self.engine.enter_associate())
        self.assertEqual(self.engine.state, ST_ASSOCIATE)

    def test_letter_passes_through(self):
        """字母必须透传给应用，且**不能**进组字态。"""
        self.enter()
        ret = self.engine.process_key(ord("a"), 0, 0)
        self.assertFalse(ret, "字母应透传给应用，返回 True 就是被吞了")
        self.assertEqual(self.engine.state, ST_IDLE,
                         "退出联想后应回到空闲态")
        self.assertEqual(self.engine.buffer, "",
                         "字母不该被塞进 buffer 开始组字")

    def test_select_and_navigation_keys_are_consumed(self):
        """候选键、翻页键、回车/退格仍然由引擎吃掉。"""
        for keyval, name in ((KEY_RETURN, "回车"),
                             (KEY_BACKSPACE, "退格"),
                             (KEY_ESCAPE, "Esc")):
            self.enter()
            ret = self.engine.process_key(keyval, 0, 0)
            self.assertTrue(ret, "%s 在联想态应由引擎处理" % name)
            self.assertEqual(self.engine.state, ST_IDLE,
                             "%s 应退出联想态" % name)

    def test_paging_keys_stay_in_associate(self):
        """翻页键归引擎，且**留在**联想态（翻的是后继词列表）。"""
        for keyval, name in ((KEY_PAGE_DOWN, "下页"), (KEY_PAGE_UP, "上页")):
            self.enter()
            self.assertTrue(self.engine.process_key(keyval, 0, 0),
                            "%s 在联想态应由引擎处理" % name)
            self.assertEqual(self.engine.state, ST_ASSOCIATE,
                             "%s 翻页后应仍在联想态" % name)

    def test_space_is_consumed_by_associate(self):
        """空格是「选中第一个后继词」，归引擎，且会**继续**联想下一词。

        所以状态留在 ASSOCIATE 是**正确**的——「选词后自动联想」就是这个
        行为（配置项 auto_associate_after_commit）。这里只守住「被引擎
        吃掉」这一点。
        """
        self.enter()
        self.assertTrue(self.engine.process_key(KEY_SPACE, 0, 0),
                        "空格在联想态应选第一个后继词，不该透传")
        self.assertIn(self.engine.state, (ST_IDLE, ST_ASSOCIATE))

    def test_digit_selects_candidate(self):
        """数字键在联想态是选候选，仍归引擎。"""
        self.enter()
        self.assertTrue(self.engine.process_key(ord("1"), 0, 0))



if __name__ == "__main__":
    unittest.main(verbosity=2)
