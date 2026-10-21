"""语气与角色气质提取的单元测试。

tone.py 无第三方依赖，直接按路径加载，不需要 stub httpx / astrbot。
"""

import importlib.util
import unittest
from pathlib import Path

TONE_PATH = Path(__file__).resolve().parents[1] / "engines" / "mimo" / "tone.py"
_spec = importlib.util.spec_from_file_location("mimo_tone", TONE_PATH)
tone = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tone)


class ExtractPersonaTraitsTests(unittest.TestCase):
    def test_keeps_tone_related_sentences(self):
        persona = (
            "你是一个温柔的少女，说话轻声细语。\n"
            "性格有点害羞，但熟了之后很活泼。\n"
            "回复必须使用 Markdown 格式。"
        )
        traits = tone.extract_persona_traits(persona)
        # 只取气质词，不照搬整句；「害羞」先于「活泼」出现，取前两个
        self.assertIn("温柔", traits)
        self.assertNotIn("Markdown", traits)
        # 最多取两个词：温柔 + 害羞
        self.assertNotIn("Markdown", traits)

    def test_drops_rule_and_tool_sentences(self):
        persona = (
            "你是助手。请严格按 JSON 输出，禁止多余解释。"
            "调用工具前必须确认参数。声音特质：低沉稳重。"
        )
        traits = tone.extract_persona_traits(persona)
        self.assertIn("低沉", traits)
        self.assertIn("稳重", traits)
        self.assertNotIn("JSON", traits)
        self.assertNotIn("工具", traits)

    def test_empty_input_returns_empty(self):
        self.assertEqual(tone.extract_persona_traits(""), "")
        self.assertEqual(tone.extract_persona_traits(None), "")

    def test_no_tone_words_returns_empty(self):
        self.assertEqual(tone.extract_persona_traits("你是一个助手。"), "")

    def test_output_length_capped(self):
        persona = "你性格温柔" * 60
        traits = tone.extract_persona_traits(persona, max_chars=40)
        self.assertLessEqual(len(traits), 40)

    def test_fluency_noise_is_dropped(self):
        """人设里的「说话结巴」「偶尔停顿」不能被当成发音指令，否则真会读成结巴。"""
        persona = "性格温柔。说话有点结巴。\n回答时偶尔停顿。"
        traits = tone.extract_persona_traits(persona)
        self.assertIn("温柔", traits)
        for noise in ("结巴", "停顿"):
            self.assertNotIn(noise, traits)

    def test_persona_prefix_is_stripped(self):
        traits = tone.extract_persona_traits("你是一个性格温柔的少女。")
        self.assertIn("温柔", traits)
        self.assertNotIn("你是一个", traits)

    def test_at_most_two_sentences(self):
        persona = "性格温柔。声音清亮。脾气活泼。腔调慵懒。"
        traits = tone.extract_persona_traits(persona)
        self.assertLessEqual(traits.count("、") + 1, 2)


class BuildToneTests(unittest.TestCase):
    def test_fast_persona_gets_faster_speed(self):
        """「说话轻快」这种人设要读快，不能被「语速正常」拖慢。"""
        traits = tone.extract_persona_traits("说话轻快，比较顽皮贪玩，声音中等。")
        self.assertIn("轻快", traits)
        prompt = tone.build_speech_tone(traits)
        self.assertIn("语速稍快", prompt)

    def test_slow_persona_is_not_passed_to_tts(self):
        """人设里的「慵懒/慢条斯理」不能变成语气，否则朗读会变慢。"""
        traits = tone.extract_persona_traits("性格慵懒，说话慢条斯理。")
        self.assertNotIn("慵懒", traits)
        self.assertNotIn("慢", traits)

    def test_neutral_voice_word_is_dropped(self):
        traits = tone.extract_persona_traits("声音中等。性格活泼。")
        self.assertNotIn("中等", traits)
        self.assertIn("活泼", traits)

    def test_fallback_picks_unlisted_trait(self):
        """词表没收录的描述，也要能从「说话X」结构里截出来。"""
        traits = tone.extract_persona_traits("说话毛躁。")
        self.assertTrue(traits)

    def test_speech_tone_without_persona_is_generic(self):
        prompt = tone.build_speech_tone("")
        self.assertIn("自然", prompt)
        self.assertNotIn("角色", prompt)
        # 指令必须短：写成管理条款会让模型去执行规则，反而不自然
        self.assertLessEqual(len(prompt), 24)

    def test_speech_tone_follows_persona(self):
        prompt = tone.build_speech_tone("性格温柔，说话轻声细语")
        self.assertIn("性格温柔", prompt)
        self.assertLessEqual(len(prompt), 40)

    def test_tone_has_no_negative_rules(self):
        """不能出现「避免…」「不要…」这类禁令，否则模型会去执行规则而变生硬。"""
        for prompt in (tone.build_speech_tone(""), tone.build_speech_tone("温柔少女")):
            self.assertNotIn("避免", prompt)
            self.assertNotIn("不要", prompt)

    def test_singing_persona_helper_removed(self):
        """唱歌不再需要角色气质指令：官方原版只发 assistant 消息。"""
        self.assertFalse(hasattr(tone, "build_singing_persona"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
