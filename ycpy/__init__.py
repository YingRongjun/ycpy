"""ycpy — 纯 Python 实现的 IBus 拼音输入法引擎。

功能：
  * 全拼输入（chengxuyuanji -> 程序员）
  * 首字母 / 声母缩写输入（cxy -> 程序员）
  * 联想输入（基于用户自己的输入历史）
  * 用户词频学习（越用越顺手）

作者：项目内自研，MIT 许可（见仓库根目录 LICENSE）。

注意：**代码**是 MIT，但随包分发的 `data/*.txt` 词典各有独立授权
（jieba / pypinyin / hanziDB 为 MIT，Unicode Unihan 为 Unicode License V3），
不能当 MIT 代码处理。详见 ``data/CREDITS.md``。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]