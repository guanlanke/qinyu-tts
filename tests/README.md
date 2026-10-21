# tests/ · 单元测试

214 项测试，覆盖歌词处理、标签拼装、意图解析、调度决策、语气提炼。跑一遍约 0.3 秒。

## 怎么跑

```bash
cd astrbot_plugin_qinyu
python -m unittest discover -s tests
```

不需要 API Key，不发网络请求——所有引擎调用都被 `_FakeEngine` 替身接住了。

## 三个文件

| 文件 | 行数 | 测什么 |
|---|---|---|
| `test_singing.py` | 1015 | 歌词处理、切片、标签、风格聚合、引擎 payload 构造 |
| `test_main_singing.py` | 680 | 插件层：唱歌意图解析、调度决策、指令行为 |
| `test_tone.py` | 96 | 人设 → 语气特征的提炼 |

## 测试组织方式

**按"行为"分组，不按函数分组**。比如 `test_singing.py` 里的类：

```
LyricsSlicingTests        切片只在整句边界发生，绝不半句
SingingTagTests           标签永远是单一半角 (唱歌)，不叠气质不叠语速
ActionTagTests            哪些标签该剔、哪些该留
GuessLyricsTests          从 LLM 回复里猜歌词的边界情况
ReferenceModeTests        参考项目方案的行为
LegacyModeTests           复古模式的行为
```

一个类对应一条用户能感知的规则。测试挂了，看类名就知道坏的是哪条行为。

## 假引擎

```python
class _FakeEngine:
    SUPPORTS_SINGING = True
    singing_mode = False

    async def synthesize(self, text):
        self.spoken.append(text)
        return b"speech", "wav"

    async def synthesize_singing(self, lyrics):
        self.sang.append(lyrics)
        return b"sing", "wav"
```

它把"调了哪个方法、传了什么文本"记下来，测试直接断言这两件事——不需要真的合成音频。

## 三个反复踩过的坑

**测试会把错误行为固化成期望值**。改行为时如果只改实现不改测试，测试会挂着；如果只改测试不改实现，测试绿了但功能是坏的。**先确认改动是对的，再改期望**。

**不要用真实音频验证听感**。测试只能验证"发给 API 的 JSON 对不对"，验证不了"唱出来好不好听"。听感问题必须靠人耳或音频指标，不要写成单测。

**边界用例比正常用例重要**。看这几个名字：

```
test_slice_hard_cuts_when_no_boundary      单行无标点时按字数硬切
test_lyric_lines_with_marker_words_are_kept  「记得…」「希望…」不该被当解释句删
test_rap_and_chorus_words_are_not_singing   「说唱」「合唱」不该判成唱歌
test_short_lyric_not_killed_by_question_guard  「我爱你中国」不该被反问拦截误杀
```

每一条都对应一个真实踩过的 bug。加功能时顺手把这些边界补上，比事后排查省力得多。
