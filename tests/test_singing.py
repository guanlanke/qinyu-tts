"""MiMo 唱歌模式单元测试。

不联网、不依赖 AstrBot 运行时：httpx 与 astrbot.api 都用轻量桩替换，
只验证请求载荷与标签构造逻辑。

运行：python -m unittest tests.test_singing -v
"""

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_httpx_stub() -> None:
    if "httpx" in sys.modules:
        return
    stub = types.ModuleType("httpx")

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {}

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            self.is_closed = False

        async def post(self, *args, **kwargs):
            raise AssertionError("测试不应发起真实网络请求")

        async def aclose(self):
            self.is_closed = True

    stub.Response = _Response
    stub.AsyncClient = _AsyncClient
    sys.modules["httpx"] = stub


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _SilentLogger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    api.logger = _SilentLogger()
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


_install_httpx_stub()
_install_astrbot_stub()

# 桩就绪后才能导入插件模块
from engines import SINGING_TEMPLATE_KEYS  # noqa: E402
from engines.base import TTSEngine  # noqa: E402
from engines.mimo.singing import (  # noqa: E402
    SINGING_DIRECTIVE,
    SINGING_FILLER_LINE,
    build_singing_content,
    build_style_prompt,
    prepare_reference_sing_text,
    cut_chitchat_head,
    cut_chitchat_tail,
    build_singing_content_style_tag,
    estimate_seconds,
    extract_singing_block,
    guess_lyrics_from_reply,
    has_singing_tag,
    is_singing_style,
    looks_like_refusal,
    normalize_lyrics,
    prepare_lyrics,
    singing_directive,
    resolve_singing_voice,
    singing_length_warning,
    slice_lyrics,
    split_lyric_lines,
    split_singing_reply,
    strip_action_tags,
    strip_explanation_lines,
    strip_voice_direction_tags,
)
from engines.mimo.v2 import MiMoV2Engine  # noqa: E402
from engines.mimo.v2_5 import MiMoV25Engine  # noqa: E402

LYRICS = "原谅我这一生不羁放纵爱自由，也会怕有一天会跌倒，背弃了理想谁人都可以。"


def _v25(**overrides) -> MiMoV25Engine:
    # 测试默认走完整链路（非复古）；复古模式由专门测试显式开启
    config = {
        "api_key": "test-key",
        "instance_name": "mimo_v2.5",
        # 测试默认走本地链路；参考项目方案与复古模式由专门测试显式开启
        "singing_reference_mode": False,
        "singing_legacy_mode": False,
    }
    config.update(overrides)
    return MiMoV25Engine(config)


class SingingTagTests(unittest.TestCase):
    def test_build_singing_content_puts_tag_first(self):
        content = build_singing_content(LYRICS)
        self.assertTrue(content.startswith("(唱歌)"))
        self.assertIn("原谅我这一生", content)

    def test_build_singing_content_is_idempotent(self):
        once = build_singing_content(LYRICS)
        twice = build_singing_content(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("唱歌"), 1)

    def test_build_singing_content_accepts_alias_tag(self):
        content = build_singing_content(LYRICS, "sing")
        self.assertTrue(content.startswith("(sing"))

    def test_fullwidth_and_bracket_tags_are_recognized(self):
        for wrapped in ("（唱歌）" + LYRICS, "[唱歌]" + LYRICS, "(singing)" + LYRICS):
            with self.subTest(wrapped=wrapped[:6]):
                self.assertTrue(has_singing_tag(wrapped))
                self.assertEqual(normalize_lyrics(wrapped), normalize_lyrics(LYRICS))

    def test_is_singing_style(self):
        self.assertTrue(is_singing_style("唱歌"))
        self.assertTrue(is_singing_style("(唱歌)"))
        self.assertTrue(is_singing_style("Sing"))
        self.assertFalse(is_singing_style("怅然"))
        self.assertFalse(is_singing_style(""))

    def test_normalize_lyrics_keeps_line_breaks(self):
        """乐句换行必须保留：折叠成空格会让模型失去句读，唱出来接近朗读。"""
        raw = "## 副歌\n- **原谅我**这一生\n不羁放纵爱自由"
        self.assertEqual(normalize_lyrics(raw), "副歌\n原谅我这一生\n不羁放纵爱自由")

    def test_style_tag_singing_variant(self):
        self.assertTrue(build_singing_content_style_tag(LYRICS).startswith("<style>唱歌"))

    def test_normalize_style_strips_style_wrapper(self):
        """用户把 <style>唱歌</style> 整段粘进 style 字段时不能套娃。"""
        from engines.mimo.singing import apply_style_prefix_legacy, normalize_style

        self.assertEqual(normalize_style("<style>开心</style>"), "开心")
        self.assertEqual(apply_style_prefix_legacy("你好", "<style>开心</style>"), "<style>开心</style>你好")
        self.assertEqual(
            apply_style_prefix_legacy("你好", "<style>唱歌</style>"),
            "<style>唱歌</style>你好",
        )

    def test_short_lyrics_warning(self):
        self.assertIsNotNone(singing_length_warning("啦"))
        self.assertIsNone(singing_length_warning(LYRICS))

    def test_long_lyrics_warning(self):
        long_lyrics = "原谅我这一生不羁放纵爱自由" * 130  # > 1500 字
        warning = singing_length_warning(long_lyrics)
        self.assertIsNotNone(warning)
        self.assertIn("偏长", warning)

    def test_singing_requests_are_serialized(self):
        """唱歌请求要串行，避免同时打多个长请求触发厂商限流。"""
        import asyncio as _asyncio

        engine = _v25()
        order = []

        async def fake_request(text, singing):
            order.append(("start", text))
            await _asyncio.sleep(0.01)
            order.append(("end", text))
            return b"audio", "wav"

        engine._request = fake_request

        async def run():
            await _asyncio.gather(
                engine.synthesize_singing("第一段完整歌词内容"),
                engine.synthesize_singing("第二段完整歌词内容"),
            )

        _asyncio.run(run())

        self.assertEqual(
            order,
            [
                ("start", "第一段完整歌词内容"),
                ("end", "第一段完整歌词内容"),
                ("start", "第二段完整歌词内容"),
                ("end", "第二段完整歌词内容"),
            ],
            "两个唱歌请求不能交错执行",
        )

    def test_contains_cjk(self):
        from engines.mimo.singing import contains_cjk

        self.assertTrue(contains_cjk("冰糖"))
        self.assertFalse(contains_cjk("Mia sings"))


class SingingExtractionTests(unittest.TestCase):
    def test_extract_html_tag(self):
        flag, lyrics = extract_singing_block("来听：<sing>月亮代表我的心</sing>", "sing")
        self.assertTrue(flag)
        self.assertEqual(lyrics, "月亮代表我的心")

    def test_extract_square_tag(self):
        flag, lyrics = extract_singing_block("[sing]小星星[/sing]", "sing")
        self.assertTrue(flag)
        self.assertEqual(lyrics, "小星星")

    def test_extract_custom_tag(self):
        flag, lyrics = extract_singing_block("<song>茉莉花</song>", "song")
        self.assertTrue(flag)
        self.assertEqual(lyrics, "茉莉花")

    def test_extract_plain_text_is_not_singing(self):
        flag, lyrics = extract_singing_block("今天天气不错", "sing")
        self.assertFalse(flag)
        self.assertEqual(lyrics, "今天天气不错")

    def test_extract_inline_singing_tag(self):
        flag, lyrics = extract_singing_block("(唱歌)茉莉花好一朵美丽的茉莉花", "sing")
        self.assertTrue(flag)

    def test_extract_merges_multiple_blocks(self):
        raw = "<sing>第一段歌词</sing>间奏<sing>第二段歌词</sing>"
        flag, lyrics = extract_singing_block(raw, "sing")
        self.assertTrue(flag)
        self.assertEqual(lyrics, "第一段歌词\n第二段歌词")

    def test_extract_merges_multiple_bracket_blocks(self):
        raw = "[sing]主歌[/sing] [sing]副歌[/sing]"
        flag, lyrics = extract_singing_block(raw, "sing")
        self.assertTrue(flag)
        self.assertEqual(lyrics, "主歌\n副歌")

    def test_extract_ignores_empty_blocks(self):
        flag, _ = extract_singing_block("<sing>   </sing>", "sing")
        self.assertFalse(flag)

    def test_extract_strips_empty_block_from_text(self):
        """空标签既不能当成唱歌，也不能把标记念出来。"""
        flag, lyrics = extract_singing_block("开场白<sing></sing>", "sing")
        self.assertFalse(flag)
        self.assertEqual(lyrics, "开场白")


class SingingReplySplitTests(unittest.TestCase):
    def test_split_returns_tail_after_last_tag(self):
        raw = "好的，我来唱：\n<sing>月亮代表我的心</sing>\n唱完了，晚安。"
        singing, lyrics, tail = split_singing_reply(raw)
        self.assertTrue(singing)
        self.assertEqual(lyrics, "月亮代表我的心")
        self.assertEqual(tail, "唱完了，晚安。")

    def test_split_drops_leading_chatter(self):
        raw = "行，我给你唱两句\n<sing>月亮代表我的心</sing>"
        _, lyrics, tail = split_singing_reply(raw)
        self.assertEqual(lyrics, "月亮代表我的心")
        self.assertEqual(tail, "")

    def test_split_empty_tag_is_not_singing(self):
        singing, lyrics, tail = split_singing_reply("开场白<sing></sing>")
        self.assertFalse(singing)
        self.assertEqual(lyrics, "开场白")
        self.assertEqual(tail, "")

    def test_split_plain_text_passthrough(self):
        singing, lyrics, tail = split_singing_reply("今天天气不错")
        self.assertFalse(singing)
        self.assertEqual(lyrics, "今天天气不错")
        self.assertEqual(tail, "")

    def test_split_tail_is_length_capped(self):
        raw = "<sing>月亮代表我的心</sing>" + "啊" * 500
        _, _, tail = split_singing_reply(raw)
        self.assertLessEqual(len(tail), 200)


class MiMoV25PayloadTests(unittest.TestCase):
    def test_singing_request_builds_leading_tag(self):
        engine = _v25()
        payload = engine._build_payload(LYRICS, singing=True)
        assistant = payload["messages"][-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertTrue(assistant["content"].startswith("(唱歌)"))
        # 演唱模式：一条禁止朗读的 user 指令 + 一条歌词 assistant 消息
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant"])
        self.assertIn("不要朗读", payload["messages"][0]["content"])

    def test_style_singing_also_triggers(self):
        engine = _v25(style="唱歌")
        payload = engine._build_payload(LYRICS)
        self.assertTrue(
            payload["messages"][-1]["content"].startswith(("(唱歌)", "（唱歌）"))
        )

    def test_style_prompt_dropped_while_singing_by_default(self):
        engine = _v25(style_prompt="用温柔的语气，语速稍慢")
        payload = engine._build_payload(LYRICS, singing=True)
        user_text = " ".join(m["content"] for m in payload["messages"] if m["role"] == "user")
        self.assertNotIn("用温柔的语气，语速稍慢", user_text)

    def test_normal_request_keeps_voice_and_style_prefix(self):
        engine = _v25(style="怅然", voice="Mia")
        payload = engine._build_payload("这么多年过去了")
        self.assertEqual(payload["messages"][-1]["content"], "(怅然)这么多年过去了")
        self.assertEqual(payload["audio"]["voice"], "Mia")
        self.assertFalse(payload["messages"][-1]["content"].startswith("(唱歌)"))

    def test_singing_content_is_not_double_tagged(self):
        engine = _v25()
        payload = engine._build_payload("(唱歌)" + LYRICS, singing=True)
        self.assertEqual(payload["messages"][-1]["content"].count("唱歌"), 1)

    def test_voiceclone_model_disables_singing(self):
        """用户把 model 改成 voiceclone 时不能再报告支持唱歌。"""
        engine = _v25(model="mimo-v2.5-tts-voiceclone")
        self.assertFalse(engine.SUPPORTS_SINGING)
        self.assertIn("voiceclone", engine.SINGING_UNSUPPORTED_REASON)
        payload = engine._build_payload(LYRICS, singing=True)
        self.assertFalse(payload["messages"][-1]["content"].startswith("(唱歌)"))

    def test_voicedesign_model_disables_singing(self):
        engine = _v25(model="mimo-v2.5-tts-voicedesign")
        self.assertFalse(engine.SUPPORTS_SINGING)

    def test_style_singing_ignored_when_model_cannot_sing(self):
        engine = _v25(model="mimo-v2.5-tts-voiceclone", style="唱歌")
        payload = engine._build_payload("随便一句话")
        self.assertNotIn("唱歌", payload["messages"][-1]["content"])

    def test_singing_timeout_override(self):
        engine = _v25(singing_timeout=180, timeout=60)
        self.assertEqual(engine._resolve_timeout(True), 180)
        self.assertEqual(engine._resolve_timeout(False), 60)
        # 未配置时保底 120 秒：满预算歌词实际要 60~80 秒，沿用朗读 60 秒会随机超时
        fallback = _v25(singing_timeout=0, timeout=60)
        self.assertEqual(fallback._resolve_timeout(True), 120)
        # 配置低于保底也按保底走
        low = _v25(singing_timeout=30, timeout=60)
        self.assertEqual(low._resolve_timeout(True), 120)

    def test_none_config_values_do_not_crash(self):
        engine = MiMoV25Engine(
            {
                "api_key": "k",
                "style": None,
                "style_prompt": None,
                "voice": None,
                "format": None,
                "singing_voice": None,
                "singing_timeout": None,
                "singing_format": None,
            }
        )
        payload = engine._build_payload(LYRICS, singing=True)
        self.assertTrue(
            payload["messages"][-1]["content"].startswith(("(唱歌)", "（唱歌）"))
        )
        self.assertEqual(payload["audio"]["format"], "wav")


class MiMoV2PayloadTests(unittest.TestCase):
    def test_legacy_singing_uses_style_tag(self):
        engine = MiMoV2Engine({"api_key": "k", "instance_name": "mimo_v2", "singing_reference_mode": False, "singing_legacy_mode": False})
        payload = engine._build_payload(LYRICS, singing=True)
        self.assertTrue(payload["messages"][-1]["content"].startswith("<style>唱歌"))

    def test_legacy_normal_prefix(self):
        engine = MiMoV2Engine({"api_key": "k", "style": "开心", "singing_reference_mode": False, "singing_legacy_mode": False})
        payload = engine._build_payload("你好")
        self.assertEqual(payload["messages"][-1]["content"], "<style>开心</style>你好")


class LyricsSlicingTests(unittest.TestCase):
    LONG = (
        "第一句完整的歌词在这里。\n"
        "第二句完整的歌词在这里，也有点长度。\n"
        "第三句完整的歌词在这里。\n"
        "第四句完整的歌词在这里，还有标点。"
    )

    def test_slice_head_cuts_on_line_boundary(self):
        """只在换行处断开：宁可少于预算，也不切半句。"""
        sliced, truncated = slice_lyrics(self.LONG, 20, "head")
        self.assertTrue(truncated)
        # 必须是原文里连续的完整行
        self.assertIn(sliced, normalize_lyrics(self.LONG))
        self.assertEqual(sliced.split("\n")[0], "第一句完整的歌词在这里。")
        # 不能正好切在逗号后面留下半句
        self.assertFalse(sliced.endswith("，"))

    def test_slice_hard_cuts_when_no_boundary(self):
        """预算比首行还小时按字数硬切：绝不能整段放行还声称已截取。"""
        sliced, truncated = slice_lyrics(self.LONG, 5, "head")
        self.assertTrue(truncated)
        self.assertLessEqual(len(sliced), 5)

    def test_slice_off_keeps_everything(self):
        sliced, truncated = slice_lyrics(self.LONG, 20, "off")
        self.assertFalse(truncated)
        self.assertEqual(sliced, normalize_lyrics(self.LONG))

    def test_slice_short_lyrics_untouched(self):
        sliced, truncated = slice_lyrics("月亮代表我的心", 300, "head")
        self.assertFalse(truncated)
        self.assertEqual(sliced, "月亮代表我的心")

    def test_slice_zero_budget_means_no_limit(self):
        sliced, truncated = slice_lyrics(self.LONG, 0, "head")
        self.assertFalse(truncated)

    def test_slice_single_line_falls_back_to_sentences(self):
        """整段只有一行时按标点断句。"""
        one_line = "第一句完整的歌词。第二句完整的歌词。第三句完整的歌词。"
        sliced, truncated = slice_lyrics(one_line, 12, "head")
        self.assertTrue(truncated)
        self.assertIn(sliced, one_line)

    def test_slice_random_returns_contiguous_segment(self):
        import random as _random

        sliced, truncated = slice_lyrics(self.LONG, 20, "random", rng=_random.Random(7))
        self.assertTrue(truncated)
        self.assertTrue(sliced)
        # 随机片段必须是原文里的连续一段，不能是拼凑出来的
        self.assertIn(sliced, normalize_lyrics(self.LONG))

    def test_slice_respects_max_lines(self):
        """行数是硬约束：字数没超也要按行数收，否则短行堆积会唱很久。"""
        lines = chr(10).join(f"第{i}句简短歌词" for i in range(1, 20))
        sliced, truncated = slice_lyrics(lines, 500, "head", max_lines=6)
        self.assertTrue(truncated)
        self.assertEqual(len(sliced.split(chr(10))), 6)

    def test_slice_lines_limit_without_char_limit(self):
        lines = chr(10).join(f"第{i}句" for i in range(1, 20))
        sliced, truncated = slice_lyrics(lines, 9999, "head", max_lines=6)
        self.assertTrue(truncated)
        self.assertEqual(len(sliced.split(chr(10))), 6)

    def test_estimate_seconds(self):
        self.assertAlmostEqual(estimate_seconds("一二三四五六", 3.0), 2.0)
        self.assertAlmostEqual(estimate_seconds("一 二 三", 3.0), 1.0)
        self.assertAlmostEqual(estimate_seconds("", 3.0), 0.0)


class VoiceLockTests(unittest.TestCase):
    def test_singing_uses_speech_voice(self):
        """唱歌与朗读共用一个音色（单独的唱歌音色配置已删除）。"""
        engine = MiMoV25Engine(
            {
                "api_key": "k",
                "instance_name": "mimo_v2.5",
                "voice": "冰糖",
                "singing_reference_mode": False,
                "singing_legacy_mode": True,
            }
        )
        payload = engine._build_singing_payload("月亮代表我的心")
        self.assertEqual(payload["audio"]["voice"], "冰糖")

    def test_strip_voice_direction_tags(self):
        raw = "（男声）第一句\n（换音色）第二句\n（女声）第三句"
        cleaned = strip_voice_direction_tags(raw)
        self.assertNotIn("男声", cleaned)
        self.assertNotIn("女声", cleaned)
        self.assertNotIn("换音色", cleaned)
        self.assertIn("第一句", cleaned)
        self.assertIn("第三句", cleaned)

    def test_voice_direction_stripped_in_payload(self):
        engine = _v25()
        payload = engine._build_payload("（男声）月亮代表我的心\n（女声）轻轻的一个吻", singing=True)
        content = payload["messages"][-1]["content"]
        self.assertNotIn("男声", content)
        self.assertNotIn("女声", content)
        self.assertTrue(content.startswith("(唱歌)"))

    def test_voice_lock_can_be_disabled(self):
        engine = _v25(singing_voice_lock=False)
        payload = engine._build_payload("（男声）月亮代表我的心", singing=True)
        self.assertIn("男声", payload["messages"][-1]["content"])

    def test_follow_voice_sentinel(self):
        engine = _v25(voice="茉莉", singing_voice="跟随预置音色")
        payload = engine._build_payload(LYRICS, singing=True)
        self.assertEqual(payload["audio"]["voice"], "茉莉")

    def test_singing_always_sends_directive(self):
        """唱歌恒带一条演唱指令，与朗读语气无关。"""
        engine = _v25()
        payload = engine._build_payload(LYRICS, singing=True)
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant"])
        self.assertIn("不要朗读", payload["messages"][0]["content"])

    def test_voice_tone_injected_for_speech(self):
        engine = _v25()
        engine.voice_tone = "用日常对话的自然语气说"
        payload = engine._build_payload("今天天气不错")
        user = [m for m in payload["messages"] if m["role"] == "user"]
        self.assertEqual(len(user), 1)
        self.assertIn("日常对话", user[0]["content"])

    def test_singing_uses_directive_not_tone(self):
        """唱歌下发的是演唱指令，不是朗读语气。"""
        engine = _v25()
        engine.voice_tone = "用温柔的感觉随口说话"
        payload = engine._build_payload(LYRICS, singing=True)
        user_text = " ".join(m["content"] for m in payload["messages"] if m["role"] == "user")
        self.assertIn("不要朗读", user_text)
        self.assertNotIn("随口说话", user_text)

    def test_no_user_message_without_tone(self):
        engine = _v25()
        payload = engine._build_payload("今天天气不错")
        self.assertEqual([m["role"] for m in payload["messages"]], ["assistant"])


class GuessLyricsTests(unittest.TestCase):
    def test_prefers_quoted_block(self):
        text = "好的，我来唱：\n「月亮代表我的心，轻轻的一个吻」"
        self.assertEqual(guess_lyrics_from_reply(text), "月亮代表我的心，轻轻的一个吻")

    def test_strips_leading_chitchat_line(self):
        text = "好的，这就唱给你听\n月亮代表我的心\n轻轻的一个吻"
        guessed = guess_lyrics_from_reply(text)
        self.assertEqual(guessed, "月亮代表我的心\n轻轻的一个吻")

    def test_keeps_plain_text(self):
        self.assertEqual(guess_lyrics_from_reply("月亮代表我的心"), "月亮代表我的心")

    def test_empty_input(self):
        self.assertEqual(guess_lyrics_from_reply(""), "")

    def test_refusal_is_not_treated_as_lyrics(self):
        """模型拒绝时不能把拒绝语唱出来。"""
        self.assertEqual(guess_lyrics_from_reply("抱歉，我不能唱歌"), "")
        self.assertEqual(guess_lyrics_from_reply("作为 AI，我无法演唱这首歌"), "")

    def test_looks_like_refusal(self):
        self.assertTrue(looks_like_refusal("我无法演唱这首歌"))
        self.assertTrue(looks_like_refusal("我不会唱歌"))
        self.assertFalse(looks_like_refusal("月亮代表我的心"))


class PrepareLyricsTests(unittest.TestCase):
    """歌词体化：决定模型把文本当歌词唱还是当台词念。"""

    def test_splits_only_on_sentence_end(self):
        """只在句末断行，句内逗号保留——官方唱歌示例就是一整段逗号连接。"""
        out = prepare_lyrics("月亮代表我的心，轻轻的一个吻。你问我爱你有多深，我爱你有几分")
        self.assertEqual(
            out.split("\n"),
            ["月亮代表我的心，轻轻的一个吻", "你问我爱你有多深，我爱你有几分"],
        )

    def test_strips_leading_chatter(self):
        out = prepare_lyrics("好嘞，给你唱两句，月亮代表我的心")
        self.assertNotIn("好嘞", out)
        self.assertIn("月亮代表我的心", out)

    def test_drops_gossip_sentence(self):
        out = prepare_lyrics("月亮代表我的心。记得给我点赞哦。轻轻的一个吻")
        self.assertNotIn("点赞", out)
        self.assertIn("轻轻的一个吻", out)

    def test_strips_leading_sing_prefix(self):
        self.assertEqual(
            prepare_lyrics("唱首：月亮代表我的心", allow_filler=False), "月亮代表我的心"
        )

    def test_limits_lines(self):
        raw = "。".join(f"第{i}句歌词内容" for i in range(20))
        out = prepare_lyrics(raw, max_lines=5)
        self.assertLessEqual(len(out.split("\n")), 5)

    def test_filler_added_when_short(self):
        out = prepare_lyrics("啦啦", allow_filler=True)
        self.assertIn(SINGING_FILLER_LINE, out)

    def test_filler_disabled_for_user_lyrics(self):
        """用户显式指定的歌词要一字不改。"""
        self.assertEqual(prepare_lyrics("啦啦", allow_filler=False), "啦啦")

    def test_empty_input(self):
        self.assertEqual(prepare_lyrics(""), "")
        self.assertEqual(prepare_lyrics(None), "")

    def test_edge_particles_removed_at_line_edges(self):
        """只清理整句首尾的语气助词，句内不动，避免破坏歌词。"""
        out = prepare_lyrics("啊月亮代表我的心，轻轻的一个吻吧")
        self.assertFalse(out.startswith("啊"))
        self.assertFalse(out.endswith("吧"))

    def test_does_not_eat_singing_words_in_lyrics(self):
        """「唱歌的孩子不会坏」是正句，不能被当成对白删掉。"""
        out = prepare_lyrics("唱歌的孩子不会坏，他们心里有片海")
        self.assertIn("唱歌的孩子不会坏", out)

    def test_leading_tag_is_stripped(self):
        out = prepare_lyrics("（唱歌）后来我总算学会了如何去爱", allow_filler=False)
        self.assertEqual(out, "后来我总算学会了如何去爱")

    def test_long_sentence_is_kept_whole(self):
        """整句必须保留完整：官方说歌词残缺会跑调，不能为了短而切碎。"""
        raw = "这是一个非常非常长的句子，长度明显超过了十四个字的上限"
        self.assertEqual(prepare_lyrics(raw, allow_filler=False), raw)

    def test_long_line_without_punctuation_is_kept(self):
        """没有断点可切时保留整行，不切词。"""
        raw = "一二三四五六七八九十一二三四五六七八九十"
        self.assertEqual(prepare_lyrics(raw, allow_filler=False), raw)

    def test_leaves_clean_lyrics_intact(self):
        out = prepare_lyrics("原谅我这一生不羁放纵爱自由\n也会怕有一天会跌倒", allow_filler=False)
        self.assertEqual(out, "原谅我这一生不羁放纵爱自由\n也会怕有一天会跌倒")


class SingingDirectiveTests(unittest.TestCase):
    def test_directive_forbids_reading(self):
        self.assertIn("不要朗读", SINGING_DIRECTIVE)
        self.assertIn("歌词", SINGING_DIRECTIVE)

    def test_directive_avoids_negative_phrases(self):
        """指令里不能出现「笑声/叹气/气声」这类词：提了会激活概念，模型反而去演。"""
        for word in ("笑声", "叹气", "气声", "哽咽", "抽泣"):
            self.assertNotIn(word, SINGING_DIRECTIVE)

    def test_directive_speed_hint_by_speed(self):
        """速度提示只在偏慢/偏快时出现，正常档不加（否则会被带慢）。"""
        self.assertEqual(singing_directive("正常"), SINGING_DIRECTIVE)
        self.assertIn("慢一些", singing_directive("偏慢"))
        self.assertIn("轻快", singing_directive("偏快"))

    def test_directive_has_no_tone_wording(self):
        """演唱指令不是语气提示，不能出现「语气」「情绪」这类会干扰演唱的词。"""
        self.assertNotIn("语气", SINGING_DIRECTIVE)
        self.assertNotIn("情绪", SINGING_DIRECTIVE)


class ActionTagTests(unittest.TestCase):
    def test_strip_action_tags(self):
        cleaned = strip_action_tags("（叹气）月亮代表我的心（轻笑）\n（哽咽）轻轻的一个吻")
        for noise in ("叹气", "轻笑", "哽咽"):
            self.assertNotIn(noise, cleaned)
        self.assertIn("月亮代表我的心", cleaned)

    def test_strip_action_tags_keeps_lyrics(self):
        self.assertEqual(strip_action_tags("唱歌的孩子不会坏"), "唱歌的孩子不会坏")

    def test_action_tags_stripped_in_payload(self):
        engine = _v25(singing_cleanup=True)
        payload = engine._build_singing_payload("（叹气）月亮代表我的心\n（轻笑）轻轻的一个吻")
        content = payload["messages"][-1]["content"]
        self.assertNotIn("叹气", content)
        self.assertNotIn("轻笑", content)

    def test_action_tags_always_stripped(self):
        """动作标签删的是「标签」不是「歌词」，与语义清洗开关无关，始终生效。"""
        engine = _v25()
        payload = engine._build_singing_payload("（叹气）月亮代表我的心")
        self.assertNotIn("叹气", payload["messages"][-1]["content"])
        self.assertIn("月亮代表我的心", payload["messages"][-1]["content"])

    def test_singing_tag_is_always_plain(self):
        """标签永远是 `(唱歌)`：不叠气质、不叠语速，半角单标签。"""
        engine = _v25()
        payload = engine._build_singing_payload("月亮代表我的心\n轻轻的一个吻")
        content = payload["messages"][-1]["content"]
        self.assertTrue(content.startswith("(唱歌)"))
        self.assertNotIn("温柔", content)
        self.assertNotIn("语速", content)

    def test_singing_speed_slow_changes_directive_only(self):
        """速度只改指令，不动标签——标签一多就不被识别为唱歌。"""
        engine = _v25(singing_speed="偏慢")
        payload = engine._build_singing_payload("月亮代表我的心\n轻轻的一个吻")
        self.assertTrue(
            payload["messages"][-1]["content"].startswith(("(唱歌)", "（唱歌）"))
        )
        self.assertNotIn("语速", payload["messages"][-1]["content"])
        self.assertIn("慢一些", payload["messages"][0]["content"])

    def test_singing_speed_fast_changes_directive_only(self):
        engine = _v25(singing_speed="偏快")
        payload = engine._build_singing_payload("月亮代表我的心\n轻轻的一个吻")
        self.assertTrue(
            payload["messages"][-1]["content"].startswith(("(唱歌)", "（唱歌）"))
        )
        self.assertIn("轻快", payload["messages"][0]["content"])

    def test_unknown_speed_falls_back_to_normal(self):
        engine = _v25(singing_speed="乱填")
        payload = engine._build_singing_payload("月亮代表我的心\n轻轻的一个吻")
        self.assertNotIn("语速", payload["messages"][-1]["content"])


class ChainSeparationTests(unittest.TestCase):
    "唱歌与朗读共用同一个音色（单独设唱歌音色会冲突，该项已删除）。"

    """纯语音链路与唱歌链路必须完全隔离，互不读取对方参数。"""

    def test_speech_payload_has_no_singing_artifacts(self):
        engine = _v25(style_prompt="用温柔的语气", singing_voice="冰糖", singing_format="mp3")
        payload = engine._build_speech_payload("今天天气不错")
        joined = " ".join(m["content"] for m in payload["messages"])
        self.assertNotIn("不要朗读", joined)
        self.assertNotIn("旋律", joined)
        self.assertNotIn("唱歌", joined)
        # 朗读走自己的音色与格式，不受唱歌配置影响
        self.assertEqual(payload["audio"]["format"], "wav")

    def test_singing_payload_has_no_speech_artifacts(self):
        engine = _v25(style_prompt="用温柔的语气，语速稍慢")
        engine.voice_tone = "像平常聊天那样自然地说"
        payload = engine._build_singing_payload("月亮代表我的心")
        joined = " ".join(m["content"] for m in payload["messages"])
        self.assertNotIn("语速稍慢", joined)
        self.assertNotIn("平常聊天", joined)
        self.assertIn("不要朗读", joined)

    def test_singing_inherits_chinese_speech_voice(self):
        """没单独配唱歌音色时，沿用朗读音色（前提是中文音色）。"""
        engine = _v25(voice="冰糖", singing_voice="")
        self.assertEqual(
            engine._build_singing_payload("月亮代表我的心")["audio"]["voice"], "冰糖"
        )

    def test_singing_style_goes_to_user_message(self):
        """气质与速度写在 user message（自然语言控制），不进标签。"""
        engine = _v25(singing_style="深情", singing_speed="偏慢")
        payload = engine._build_singing_payload("月亮代表我的心")
        self.assertIn("整体气质：深情", payload["messages"][0]["content"])
        self.assertIn("慢一些", payload["messages"][0]["content"])
        self.assertTrue(
            payload["messages"][-1]["content"].startswith(("(唱歌)", "（唱歌）"))
        )

    def test_directive_demands_melody_and_forbids_switching(self):
        """要防「唱着说话」和「中途切回说话」：主指令必须强，不能被气质稀释。"""
        self.assertIn("旋律哼唱", SINGING_DIRECTIVE)
        self.assertIn("不要说话", SINGING_DIRECTIVE)
        self.assertIn("中途不要切换成说话", SINGING_DIRECTIVE)
        # 气质只能作为末尾附注出现
        directive = singing_directive("正常", "温柔")
        self.assertTrue(directive.startswith("请严格按"))

    def test_long_line_is_split_into_short_phrases(self):
        """一行唱太长会换气不当、吞字、唱错词，要切成短句。"""
        lyric = '原谅我这一生不羁放纵爱自由，也会怕有一天会跌倒，背弃了理想谁人都可以'
        out = split_lyric_lines(lyric)
        self.assertEqual(
            out.split(chr(10)),
            ['原谅我这一生不羁放纵爱自由', '也会怕有一天会跌倒', '背弃了理想谁人都可以'],
        )

    def test_short_line_is_not_split(self):
        """本就够短的句子原样保留，不能被切碎。"""
        self.assertEqual(split_lyric_lines('月亮代表我的心，轻轻的一个吻'), '月亮代表我的心，轻轻的一个吻')

    def test_singing_payload_keeps_lyrics_by_default(self):
        """默认不切句：歌词原样送出。"""
        engine = _v25()
        lyric = '原谅我这一生不羁放纵爱自由，也会怕有一天会跌倒，背弃了理想谁人都可以'
        payload = engine._build_singing_payload(lyric)
        self.assertIn(lyric, payload['messages'][-1]['content'])

    def test_singing_payload_splits_when_enabled(self):
        """手动打开短句切分时才切。"""
        engine = _v25(singing_short_lines=True)
        payload = engine._build_singing_payload(
            '原谅我这一生不羁放纵爱自由，也会怕有一天会跌倒，背弃了理想谁人都可以'
        )
        content = payload['messages'][-1]['content']
        self.assertGreaterEqual(len(content.split(chr(10))), 3)

    def test_explanation_lines_removed(self):
        """解释句和寒暄句会被「唱着说出来」，必须删掉。"""
        raw = "这首歌表达了对自由的渴望\n原谅我这一生不羁放纵爱自由\n希望你喜欢"
        cleaned = strip_explanation_lines(raw)
        self.assertNotIn("这首歌表达", cleaned)
        self.assertNotIn("希望你喜欢", cleaned)
        self.assertIn("原谅我这一生不羁放纵爱自由", cleaned)

    def test_speech_does_not_lyricize(self):
        """朗读不做分行、不去对白：原文照念。"""
        engine = _v25()
        payload = engine._build_speech_payload("好嘞，给你唱两句，月亮代表我的心")
        self.assertIn("好嘞", payload["messages"][-1]["content"])

    def test_speech_tone_only_affects_speech(self):
        engine = _v25()
        engine.voice_tone = "像平常聊天那样自然地说"
        speech = engine._build_speech_payload("你好")
        singing = engine._build_singing_payload("月亮代表我的心")
        self.assertIn("平常聊天", " ".join(m["content"] for m in speech["messages"]))
        self.assertNotIn("平常聊天", " ".join(m["content"] for m in singing["messages"]))


class ReviewRegressionTests(unittest.TestCase):
    """回归：外审报告指出的三处误判，任何一处复发都会让歌词缺句或突然变成念白。"""

    def test_lyric_lines_with_marker_words_are_kept(self):
        keep = [
            "记得那次我们一起去海边看日落",
            "希望你能听见我唱的这首歌",
            "主歌里藏着一句没说出口的话",
            "晚安北京，晚安所有未眠的人们",
            "关注着你的每一步",
        ]
        for line in keep:
            self.assertTrue(strip_explanation_lines(line), f"歌词被误删: {line}")

    def test_real_explanation_lines_are_dropped(self):
        drop = ["这首歌表达了对自由的渴望", "（副歌）", "副歌", "希望你喜欢", "记得点赞关注", "歌词：", "谢谢收听"]
        for line in drop:
            self.assertFalse(strip_explanation_lines(line), f"解释句没删: {line}")

    def test_rap_and_chorus_words_are_not_singing(self):
        for style in ("说唱", "哼唱", "伴唱", "合唱", "对唱", "清唱"):
            self.assertFalse(is_singing_style(style), f"{style} 被误判成唱歌")

    def test_singing_still_recognized(self):
        for style in ("唱歌", "唱", "sing", "singing", "唱歌 温柔"):
            self.assertTrue(is_singing_style(style), f"{style} 应识别为唱歌")

    def test_rhetorical_question_is_not_lyrics(self):
        for reply in ("我不知道你想听什么歌", "你想听哪首歌？"):
            self.assertEqual(guess_lyrics_from_reply(reply), "")

    def test_unclosed_tag_is_stripped_not_spoken(self):
        singing, body, _ = split_singing_reply("<sing>未闭合的歌词")
        self.assertFalse(singing)
        self.assertNotIn("<sing>", body)


class Round2RegressionTests(unittest.TestCase):
    """第二轮外审：配置别名、多候选容错、空歌词、短歌词误杀。"""

    def test_follow_aliases_are_not_voice_names(self):
        """follow / auto / none 必须被当成「跟随」，否则会被当音色名发给接口 → 400。"""
        for alias in ("follow", "auto", "none", "跟随朗读音色", "", "default", "same"):
            self.assertEqual(
                resolve_singing_voice(alias, "冰糖"), "冰糖", f"{alias} 未被识别为跟随"
            )

    def test_short_lyric_not_killed_by_question_guard(self):
        """「我爱你中国」是歌词，不能被反问拦截误杀。"""
        self.assertEqual(guess_lyrics_from_reply("我爱你中国"), "我爱你中国")

    def test_real_question_is_killed(self):
        for reply in ("你想听哪首歌？", "我不知道你想听什么歌", "要唱什么吗"):
            self.assertEqual(guess_lyrics_from_reply(reply), "", f"问句未被拦截: {reply}")

    def test_length_warning_can_fire(self):
        """偏长阈值必须低于实际截断值，否则提示永远触发不了。"""
        long_lyrics = "月亮代表我的心" * 40
        self.assertIsNotNone(singing_length_warning(long_lyrics))

    def test_empty_after_cleaning_raises(self):
        """歌词只剩括号标签时，不能发一条 (唱歌) 空请求。"""
        engine = _v25(singing_cleanup=True)
        with self.assertRaises(ValueError):
            engine._build_singing_payload("（男声）（叹气）")


class Round3RegressionTests(unittest.TestCase):
    """第三轮外审：寒暄漏网、开关粒度、英文硬切。"""

    def test_chitchat_cut_in_direct_payload(self):
        """直调路径也要切寒暄——常驻唱歌不经过 split_singing_reply。"""
        engine = _v25()
        payload = engine._build_singing_payload(
            "月亮代表我的心" + chr(10) + "希望你喜欢这首歌"
        )
        content = payload["messages"][-1]["content"]
        self.assertNotIn("希望你喜欢", content)
        self.assertIn("月亮代表我的心", content)

    def test_chitchat_cut_in_sing_tag_branch(self):
        singing, lyrics, _ = split_singing_reply(
            "<sing>歌词一" + chr(10) + "希望你喜欢</sing>"
        )
        self.assertTrue(singing)
        self.assertNotIn("希望你喜欢", lyrics)

    def test_english_lyrics_keep_spaces_when_hard_cut(self):
        """硬切要按可见字符，不能把英文挤成一串字母。"""
        out, _ = slice_lyrics("we will rock you " * 20, 40, "head")
        self.assertIn(" ", out)
        self.assertNotIn("wewillrockyou", out)

    def test_chitchat_traditional_form(self):
        out = cut_chitchat_tail("歌词" + chr(10) + "希望你喜歡這首歌")
        self.assertNotIn("喜歡", out)

    def test_cleanup_off_still_removes_action_tags(self):
        """关清洗只该保住歌词行，不该把（叹气）放回给模型演戏。"""
        engine = _v25(singing_cleanup=False)
        content = engine._build_singing_payload("月亮代表我的心" + chr(10) + "（叹气）")[
            "messages"
        ][-1]["content"]
        self.assertNotIn("叹气", content)
        self.assertIn("月亮代表我的心", content)


class Round4RegressionTests(unittest.TestCase):
    """第四轮外审：切头不能删用户手写歌词。"""

    LYRICS = [
        "好的事情总会来",
        "接下来的人生还很长",
        "马上就会明白",
        "来听我说一句",
        "下面那句你听好",
        "这就够了",
        "好呀我的爱人",
        "好的,我们一起去看海",
        "好的，我们一起去看海",
    ]

    CHITCHAT = [
        "好的呀～我给你唱一首",
        "好嘞我这就唱",
        "下面给你唱一段",
        "好",
        "希望你喜欢这首歌",
        "谢谢收听",
    ]

    def test_lyric_first_line_is_kept(self):
        for line in self.LYRICS:
            out = cut_chitchat_head(line + chr(10) + "真正的歌词")
            self.assertIn("真正的歌词", out, f"歌词被删: {line}")
            self.assertEqual(len(out.split(chr(10))), 2, f"首行被删: {line}")

    def test_real_chitchat_head_is_cut(self):
        for line in self.CHITCHAT:
            out = cut_chitchat_head(line + chr(10) + "真正的歌词")
            self.assertIn("真正的歌词", out, f"寒暄没切: {line}")
            self.assertNotIn(line, out, f"寒暄没切: {line}")

    def test_head_cut_off_by_default(self):
        """切头默认关闭：收益小、误删代价大。"""
        engine = _v25()
        content = engine._build_singing_payload("好的事情总会来" + chr(10) + "明天会更好")[
            "messages"
        ][-1]["content"]
        self.assertIn("好的事情总会来", content)

    def test_head_cut_when_cleanup_enabled(self):
        engine = _v25(singing_cleanup=True)
        content = engine._build_singing_payload("好嘞我这就唱" + chr(10) + "明天会更好")[
            "messages"
        ][-1]["content"]
        self.assertNotIn("好嘞我这就唱", content)
        self.assertIn("明天会更好", content)

    def test_style_tag_stripped_at_v25_entry(self):
        """V2 的 <style>标签粘到 V2.5 入口时不能套娃。"""
        engine = _v25()
        content = engine._build_singing_payload("<style>唱歌</style>月亮代表我的心")[
            "messages"
        ][-1]["content"]
        self.assertNotIn("<style>", content)
        self.assertNotIn("</style>", content)


class Round5RegressionTests(unittest.TestCase):
    """第五轮外审：失败路径不能露标签、超时日志不误导、排队要有反馈。"""

    def test_singing_busy_property(self):
        engine = _v25()
        self.assertFalse(engine.singing_busy)
        self.assertTrue(hasattr(engine, "singing_busy"), "引擎需要暴露排队状态")

    def test_timeout_floor_is_120(self):
        """留 0 也按 120 保底：满预算歌词实际要 60~80 秒。"""
        engine = _v25(singing_timeout=0, timeout=60)
        self.assertEqual(engine._resolve_timeout(True), 120)
        self.assertEqual(engine._resolve_timeout(False), 60)


class BreathingTagTests(unittest.TestCase):
    """呼吸点与发声方式必须留给模型，只剔情绪表演。"""

    KEEP = ['（停顿）', '（换气）', '（吸气）', '（呼气）', '（深呼吸）', '（屏息）',
            '（气声）', '（轻声）', '（低语）', '（耳语）']
    DROP = ['（叹气）', '（哽咽）', '（抽泣）', '（呜咽）', '（笑声）', '（轻笑）',
            '（咳嗽）', '（颤抖）', '（不耐烦）']

    def test_breathing_tags_kept(self):
        for tag in self.KEEP:
            self.assertIn(tag.strip('（）'), strip_action_tags(tag + '歌词'), f'呼吸标签被删: {tag}')

    def test_emotion_tags_dropped(self):
        for tag in self.DROP:
            self.assertNotIn(tag.strip('（）'), strip_action_tags(tag + '歌词'), f'情绪标签没删: {tag}')

    def test_payload_keeps_breathing_tags(self):
        engine = _v25()
        content = engine._build_singing_payload('（换气）第一句\n（气声）第二句')['messages'][-1]['content']
        self.assertIn('换气', content)
        self.assertIn('气声', content)

    def test_join_lines_switch(self):
        engine = _v25(singing_join_lines=True)
        content = engine._build_singing_payload('第一句\n第二句')['messages'][-1]['content']
        self.assertNotIn(chr(10), content)

    def test_join_lines_off_by_default(self):
        engine = _v25()
        content = engine._build_singing_payload('第一句\n第二句')['messages'][-1]['content']
        self.assertIn(chr(10), content)


class JoinLinesTests(unittest.TestCase):
    """join_lines 的边界：不造畸形标点、与切短句互斥、空歌词拦得住。"""

    def test_no_double_punctuation(self):
        engine = _v25(singing_join_lines=True)
        content = engine._build_singing_payload('第一句。\n第二句。\n第三句')['messages'][-1]['content']
        self.assertNotIn('。，', content)
        self.assertNotIn('，，', content)

    def test_breathing_tag_not_glued_by_comma(self):
        engine = _v25(singing_join_lines=True)
        content = engine._build_singing_payload('（换气）\n第一句\n第二句')['messages'][-1]['content']
        self.assertNotIn('（换气），', content)
        self.assertIn('（换气）', content)

    def test_join_merges_lines(self):
        """只开合段时，多行应被连成一整段。"""
        engine = _v25(singing_join_lines=True, singing_short_lines=False)
        content = engine._build_singing_payload(
            '原谅我这一生不羁放纵爱自由\n也会怕有一天会跌倒'
        )['messages'][-1]['content']
        self.assertNotIn(chr(10), content)
        self.assertIn('自由，也会怕', content)

    def test_join_then_short_produces_short_lines(self):
        """两者同开时顺序执行：先合段归整，再统一切成短句。"""
        engine = _v25(singing_join_lines=True, singing_short_lines=True)
        content = engine._build_singing_payload(
            '原谅我这一生不羁放纵爱自由\n也会怕有一天会跌倒'
        )['messages'][-1]['content']
        body = content.replace('(唱歌)', '')
        for line in body.split(chr(10)):
            self.assertLessEqual(len(line.strip()), 16, f'未切成短句: {line}')

    def test_only_music_tags_are_rejected(self):
        engine = _v25()
        for text in ('（换气）', '（停顿）', '（换气）（停顿）'):
            with self.assertRaises(ValueError):
                engine._build_singing_payload(text)

    def test_keep_and_drop_do_not_overlap(self):
        """契约：KEEP 与 DROP 不能有交集，否则呼吸标签会静默消失。"""
        from engines.mimo.singing import _DROP_SINGING_TAGS, _KEEP_SINGING_TAGS

        self.assertEqual(set(_KEEP_SINGING_TAGS) & set(_DROP_SINGING_TAGS), set())


class LegacyModeTests(unittest.TestCase):
    """复古模式：等价于 v1.4.7，不做任何清洗，这是用户说过「能唱」的形态。"""

    def test_legacy_is_on_by_default(self):
        """默认走复古模式：必须与 _conf_schema.json 和 v3.15.0 更新日志一致。

        改前这里是 False（代码 fallback），而 schema 写 true、日志写「复古=开」——
        老配置缺键时走参考链路、新配置走复古链路，同一个插件两种行为。
        """
        engine = MiMoV25Engine({"api_key": "k", "instance_name": "mimo_v2.5"})
        self.assertTrue(engine.singing_legacy_mode)
        self.assertFalse(engine.singing_reference_mode, "参考项目方案应是可选，不是默认")

    def test_legacy_keeps_lyrics_verbatim(self):
        engine = MiMoV25Engine({"api_key": "k", "instance_name": "mimo_v2.5"})
        lyric = "（叹气）我的歌词\n希望你喜欢"
        content = engine._build_singing_payload(lyric)["messages"][-1]["content"]
        # 复古模式不做任何清洗：动作标签、寒暄都原样保留
        self.assertIn("叹气", content)
        self.assertIn("希望你喜欢", content)

    def test_legacy_follows_speech_voice(self):
        engine = MiMoV25Engine(
            {"api_key": "k", "instance_name": "mimo_v2.5", "voice": "冰糖"}
        )
        payload = engine._build_singing_payload("月亮代表我的心")
        self.assertEqual(payload["audio"]["voice"], "冰糖")
        self.assertEqual(payload["audio"]["format"], "wav")

    def test_legacy_uses_single_tag_and_directive(self):
        engine = MiMoV25Engine({"api_key": "k", "instance_name": "mimo_v2.5"})
        payload = engine._build_singing_payload("月亮代表我的心")
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant"])
        self.assertTrue(
            payload["messages"][-1]["content"].startswith(("(唱歌)", "（唱歌）"))
        )
        self.assertIn("旋律哼唱", payload["messages"][0]["content"])


class ReferenceModeTests(unittest.TestCase):
    """参考项目 astrbot_plugin_tts_emotion_router v3.3.4 的严格唱歌方案。"""

    def _engine(self, **overrides):
        config = {
            "api_key": "k",
            "instance_name": "mimo_v2.5",
            "singing_reference_mode": True,
            "voice": "冰糖",
        }
        config.update(overrides)
        return MiMoV25Engine(config)

    def test_reference_is_opt_in(self):
        """参考模式要显式开启：默认仍是复古模式（与 schema 一致）。"""
        engine = MiMoV25Engine({"api_key": "k", "instance_name": "mimo_v2.5"})
        self.assertFalse(engine.singing_reference_mode)
        self.assertTrue(engine.singing_legacy_mode)

    def test_uses_fullwidth_tag_and_official_directive(self):
        payload = self._engine()._build_singing_payload("月亮代表我的心")
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant"])
        self.assertTrue(payload["messages"][-1]["content"].startswith("（唱歌）"))
        self.assertIn("连成一首歌从头唱到尾", payload["messages"][0]["content"])
        self.assertIn("不要漏句", payload["messages"][0]["content"])

    def test_keeps_lyrics_verbatim(self):
        """参考项目只删串场白，不改歌词：保留波浪号、标点、括号段落标记。"""
        lyric = "（副歌）我~想你，想你~"
        content = self._engine()._build_singing_payload(lyric)["messages"][-1]["content"]
        self.assertIn("我~想你，想你~", content)
        self.assertIn("（副歌）", content)

    def test_drops_leading_chatter(self):
        lyric = "好嘞，那我给你唱一首：" + chr(10) + "月亮代表我的心" + chr(10) + "轻轻的一个吻"
        content = self._engine()._build_singing_payload(lyric)["messages"][-1]["content"]
        self.assertNotIn("给你唱一首", content)
        self.assertIn("月亮代表我的心", content)

    def test_no_truncation(self):
        """参考项目不截断：官方说残缺歌词会跑调。"""
        lyric = "；".join(["原谅我这一生不羁放纵爱自由"] * 20)
        content = self._engine()._build_singing_payload(lyric)["messages"][-1]["content"]
        self.assertGreater(len(content), 180)
        self.assertIn("背弃了理想", content) if "背弃了理想" in lyric else None

    def test_empty_falls_back_to_humming(self):
        content = self._engine()._build_singing_payload("")["messages"][-1]["content"]
        self.assertIn("啦啦啦", content)

    def test_does_not_apply_local_cleanup(self):
        """参考模式下本地的解释行清洗不生效。"""
        engine = self._engine(singing_cleanup=True)
        content = engine._build_singing_payload("希望你能听见我唱的这首歌")["messages"][-1]["content"]
        self.assertIn("希望你能听见我唱的这首歌", content)


class GlobalStyleTests(unittest.TestCase):
    """5 维风格全局生效：朗读与唱歌共用，夹子音与甜美互不影响。"""

    def _engine(self, **overrides):
        config = {
            "api_key": "k",
            "instance_name": "mimo_v2.5",
            "singing_reference_mode": False,
            "singing_legacy_mode": True,
        }
        config.update(overrides)
        return MiMoV25Engine(config)

    def test_five_dimensions_aggregate(self):
        content = build_style_prompt("温柔", "甜美", "夹子音")
        self.assertIn("温柔、甜美、夹子音", content)
        self.assertNotIn("（", content, "风格不能放进 assistant 标签，会被念出来")

    def test_persona_and_timbre_are_independent(self):
        """夹子音（人设腔调）与甜美（音色质感）是不同维度，可单独或同时开启。"""
        self.assertIn("甜美", build_style_prompt("", "甜美", ""))
        self.assertIn("夹子音", build_style_prompt("", "", "夹子音"))
        both = build_style_prompt("", "甜美", "夹子音")
        self.assertIn("甜美", both)
        self.assertIn("夹子音", both)

    def test_sing_keeps_style_in_second_bracket(self):
        engine = self._engine(timbre_positioning="甜美", persona_accent="夹子音")
        payload = engine._build_singing_payload("月亮代表我的心")
        assistant = payload["messages"][-1]["content"]
        user = payload["messages"][0]["content"]
        self.assertTrue(assistant.startswith("（唱歌）"))
        # 风格只出现在 user 消息里，绝不出现在 assistant（否则会被念）
        self.assertNotIn("甜美", assistant)
        self.assertNotIn("夹子音", assistant)
        self.assertIn("甜美", user)
        self.assertIn("夹子音", user)

    def test_speech_also_carries_style(self):
        """全局风格对朗读同样生效。"""
        engine = self._engine(persona_accent="夹子音")
        payload = engine._build_payload("你好", singing=False)
        joined = "".join(m["content"] for m in payload["messages"])
        self.assertIn("夹子音", joined)
        self.assertNotIn("夹子音", payload["messages"][-1]["content"], "不能出现在正文本体里")

    def test_no_style_means_no_bracket(self):
        engine = self._engine()
        payload = engine._build_payload("你好", singing=False)
        self.assertNotIn("（）", payload["messages"][-1]["content"])


class MiniMaxSingingTests(unittest.TestCase):
    """MiniMax 引擎：朗读与唱歌走两个不同接口。"""

    def _engine(self, **overrides):
        from engines.minimax import MiniMaxEngine

        cfg = {"api_key": "k", "instance_name": "minimax"}
        cfg.update(overrides)
        return MiniMaxEngine(cfg)

    def test_declares_singing_support(self):
        from engines.minimax import MiniMaxEngine

        self.assertTrue(MiniMaxEngine.SUPPORTS_SINGING)

    def test_lyrics_get_structure_tags(self):
        """纯歌词会被补上 [verse]/[chorus] 结构标记，MiniMax 靠它理解段落。"""
        engine = self._engine()
        out = engine._structure_lyrics("第一段歌词\n还在第一段\n\n第二段歌词")
        self.assertIn("[verse]", out)
        self.assertIn("[chorus]", out)
        self.assertIn("第一段歌词", out)

    def test_existing_tags_preserved(self):
        engine = self._engine()
        raw = "[verse]\n已有标记\n[chorus]\n副歌"
        self.assertEqual(engine._structure_lyrics(raw), raw)

    def test_speech_and_music_payload_differ(self):
        """朗读用 text 字段，唱歌用 prompt+lyrics，两者不共用。"""
        engine = self._engine()
        engine.music_prompt = "摇滚，男声，快节奏"
        structured = engine._structure_lyrics("月亮代表我的心")
        self.assertIn("[verse]", structured)
        self.assertIn("月亮代表我的心", structured)
        self.assertEqual(engine.music_prompt, "摇滚，男声，快节奏")

    def test_voice_and_music_format_are_independent(self):
        engine = self._engine(voice="female-yujie", format="wav",
                              music_format="mp3", music_model="music-3.0")
        self.assertEqual(engine.voice, "female-yujie")
        self.assertEqual(engine.format, "wav")
        self.assertEqual(engine.music_format, "mp3")
        self.assertEqual(engine.music_model, "music-3.0")

    def test_hex_audio_decoded(self):
        """MiniMax 的 t2a_v2 返回十六进制音频。"""
        engine = self._engine()
        data = {"data": {"audio": "48656c6c6f"}}  # "Hello"
        self.assertEqual(engine._extract_audio(data, "朗读"), b"Hello")

    def test_emotion_inferred_from_tone(self):
        engine = self._engine()
        self.assertEqual(engine._guess_emotion("活泼轻快"), "happy")
        self.assertEqual(engine._guess_emotion("低落难过"), "sad")
        self.assertEqual(engine._guess_emotion("中性描述"), "")


class SingingCapabilityTests(unittest.TestCase):
    def test_registry_exposes_singing_engines(self):
        self.assertIn("mimo_v2_5", SINGING_TEMPLATE_KEYS)
        self.assertIn("mimo_v2", SINGING_TEMPLATE_KEYS)
        self.assertNotIn("mimo_v2_5_voicedesign", SINGING_TEMPLATE_KEYS)
        self.assertNotIn("mimo_v2_5_voiceclone", SINGING_TEMPLATE_KEYS)

    def test_engine_flags(self):
        self.assertTrue(MiMoV25Engine.SUPPORTS_SINGING)
        from engines.mimo.v2_5_clone import MiMoV25CloneEngine
        from engines.mimo.v2_5_design import MiMoV25DesignEngine

        self.assertFalse(MiMoV25CloneEngine.SUPPORTS_SINGING)
        self.assertFalse(MiMoV25DesignEngine.SUPPORTS_SINGING)

    def test_default_synthesize_singing_falls_back(self):
        class _Dummy(TTSEngine):
            async def synthesize(self, text):
                return b"audio:" + text.encode(), "wav"

        import asyncio

        data, fmt = asyncio.run(_Dummy({}).synthesize_singing("你好"))
        self.assertEqual(data, "audio:你好".encode())
        self.assertEqual(fmt, "wav")
        self.assertFalse(_Dummy.SUPPORTS_SINGING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
