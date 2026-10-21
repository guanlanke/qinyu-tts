"""main.py 唱歌接线集成测试。

用轻量桩替换 AstrBot 与 httpx，验证钩子真的会走唱歌分支，
不会真的联网、也不会初始化真实插件运行时。
"""

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parents[1]
PARENT = PKG_DIR.parent
for path in (str(PKG_DIR), str(PARENT)):
    if path not in sys.path:
        sys.path.insert(0, path)

#: 固定包名。插件目录被改名（例如解压成 xxx_v1.4.0）时也能按路径加载。
PACKAGE_NAME = "astrbot_plugin_qinyu"


class _AnyDecorator:
    """同时兼容 on_xxx() / on_xxx(priority=0) / command("name") / 直接装饰。"""

    def __call__(self, *args, **kwargs):
        if len(args) == 1 and not kwargs and callable(args[0]):
            return args[0]
        return lambda func: func


class _Filter:
    def __init__(self):
        deco = _AnyDecorator()
        for name in (
            "command",
            "on_decorating_result",
            "on_waiting_llm_request",
            "on_llm_request",
        ):
            setattr(self, name, deco)


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _Record:
    def __init__(self, file="", url=""):
        self.file = file
        self.url = url


class _Reply:
    def __init__(self, chain=None):
        self.chain = chain or []


class _Star:
    def __init__(self, context=None):
        self.context = context


class _SessionServiceManager:
    @staticmethod
    async def should_process_tts_request(event):
        return True


class _SilentLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _install_stubs() -> None:
    if "httpx" not in sys.modules:
        httpx_stub = types.ModuleType("httpx")

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

        httpx_stub.Response = _Response
        httpx_stub.AsyncClient = _AsyncClient
        sys.modules["httpx"] = httpx_stub

    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    message_components = types.ModuleType("astrbot.api.message_components")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    session_llm_manager = types.ModuleType("astrbot.core.star.session_llm_manager")

    class _AstrBotConfig(dict):
        pass

    class _AstrMessageEvent:
        pass

    def _register(*args, **kwargs):
        def decorator(cls):
            cls.__plugin_meta__ = args
            return cls

        return decorator

    api.logger = _SilentLogger()
    api.AstrBotConfig = _AstrBotConfig
    api.message_components = message_components
    message_components.Plain = _Plain
    message_components.Record = _Record
    message_components.Reply = _Reply

    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = _AstrMessageEvent
    api.event = event_mod

    star_mod.Context = object
    star_mod.Star = _Star
    star_mod.register = _register
    api.star = star_mod

    session_llm_manager.SessionServiceManager = _SessionServiceManager
    core_star.session_llm_manager = session_llm_manager
    core.star = core_star

    astrbot.api = api
    astrbot.core = core

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.message_components"] = message_components
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.star"] = core_star
    sys.modules["astrbot.core.star.session_llm_manager"] = session_llm_manager


_install_stubs()


def _ensure_package_importable() -> None:
    """把插件目录注册成固定包名，避免目录改名后 `from astrbot_plugin_qinyu import ...` 失败。"""
    if PACKAGE_NAME in sys.modules:
        return
    init_file = PKG_DIR / "__init__.py"
    if init_file.is_file():
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME, init_file, submodule_search_locations=[str(PKG_DIR)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = module
        spec.loader.exec_module(module)
        return
    # 没有 __init__.py 时退回命名空间包
    namespace = types.ModuleType(PACKAGE_NAME)
    namespace.__path__ = [str(PKG_DIR)]
    sys.modules[PACKAGE_NAME] = namespace


_ensure_package_importable()

from astrbot_plugin_qinyu import main as plugin_main  # noqa: E402


class _FakeResult:
    def __init__(self, chain):
        self.chain = chain

    def is_llm_result(self):
        return True


class _FakeEvent:
    def __init__(self, chain, umo="aiocqhttp:GroupMessage:100", command_text=""):
        self._result = _FakeResult(chain)
        self.unified_msg_origin = umo
        self.message_str = command_text
        self.message_obj = types.SimpleNamespace(message_str=f"/{command_text}" if command_text else "")

    def get_result(self):
        return self._result

    def get_messages(self):
        return []

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


class _FakeEngine:
    SUPPORTS_SINGING = True
    SINGING_UNSUPPORTED_REASON = ""
    singing_mode = False

    def __init__(self):
        self.instance_name = "fake_v2.5"
        self.spoken = []
        self.sang = []
        self.voice_tone = ""
        self.singing_busy = False
        self.fail_singing = False

    async def synthesize(self, text):
        self.spoken.append(text)
        if getattr(self, "fail_speech", False):
            raise RuntimeError("boom")
        return b"speech", "wav"

    async def synthesize_singing(self, lyrics):
        self.sang.append(lyrics)
        if getattr(self, "fail_singing", False):
            raise RuntimeError("sing boom")
        return b"sing", "wav"

    async def close(self):
        return None


class _FakeContext:
    astrbot_config = {"provider_tts_settings": {"enable": True}}


def _build_plugin(config=None):
    cfg = {
        "enable": True,
        "tts_probability": 100,
        "tts_text_limit_enable": False,
        "singing_enable": True,
        "singing_auto_intent": True,
        "singing_command_admin_only": False,
    }
    cfg.update(config or {})
    return plugin_main.QingYuPlugin(_FakeContext(), cfg)


class SingingWiringTests(unittest.TestCase):
    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        # 插件会在 cwd 下创建 data/temp 目录，切到临时目录避免污染仓库
        import os

        os.chdir(self._tmp.name)

    def tearDown(self):
        import os

        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_singing_prompt_mentions_tag(self):
        plugin = _build_plugin()
        prompt = plugin._singing_prompt()
        self.assertIn("<sing>", prompt)
        # 提示词要求「只写一段完整副歌」，而不是写完再切
        self.assertIn("写完整的一整段歌词", prompt)
        self.assertIn("180 字", prompt)
        self.assertIn("会导致跑调", prompt)
        # 尾注那条要求已收回，提示词里不该再引导标签外写字
        self.assertNotIn("想对用户说的话", prompt)

    def test_singing_prompt_ignores_removed_config(self):
        """只调上限不该改目标：提示词里的目标字数是按 singing_target_seconds 算的。"""
        plugin = _build_plugin({"singing_max_seconds": 300})
        self.assertIn("180 字", plugin._singing_prompt())

    def test_singing_budget_is_configurable(self):
        """时长是配置项：默认 60/90 秒，填多大就按多大折算，并夹取到 15-300 秒。

        以前这里是写死常量（还配了测试锁死「不可调」），所以「唱太短」根本调不动。
        """
        plugin = _build_plugin()
        target_chars, hard_chars, cps = plugin._singing_budget()
        self.assertEqual(int(target_chars / cps), plugin_main.SINGING_TARGET_SECONDS)
        self.assertEqual(int(hard_chars / cps), plugin_main.SINGING_MAX_SECONDS)
        self.assertGreater(hard_chars, target_chars)

        longer = _build_plugin({"singing_target_seconds": 180, "singing_max_seconds": 240})
        t2, h2, c2 = longer._singing_budget()
        self.assertEqual(int(t2 / c2), 180)
        self.assertEqual(int(h2 / c2), 240)
        self.assertIn("540 字", longer._singing_prompt())

        clamped = _build_plugin(
            {"singing_target_seconds": 5, "singing_max_seconds": 99999}
        )
        t3, h3, c3 = clamped._singing_budget()
        self.assertEqual(int(t3 / c3), plugin_main.SINGING_MIN_ALLOWED_SECONDS)
        self.assertEqual(int(h3 / c3), plugin_main.SINGING_MAX_ALLOWED_SECONDS)

    def test_singing_prompt_disabled(self):
        plugin = _build_plugin({"singing_auto_intent": False})
        self.assertEqual(plugin._singing_prompt(), "")

    def test_support_check_uses_engine_flag(self):
        plugin = _build_plugin()
        self.assertFalse(plugin._supports_singing(_FakeEngine.__new__(_FakeEngine)) is None)
        engine = _FakeEngine()
        self.assertTrue(plugin._supports_singing(engine))
        engine.SUPPORTS_SINGING = False
        self.assertFalse(plugin._supports_singing(engine))
        self.assertFalse(plugin._supports_singing(None))

    def test_hook_routes_singing_to_synthesize_singing(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("开场白<sing>原谅我这一生不羁放纵爱自由</sing>")])

        asyncio.run(plugin.tts_hook(event))

        self.assertTrue(engine.sang and engine.sang[0].startswith("原谅我这一生不羁放纵爱自由"))
        self.assertEqual(engine.spoken, [])
        self.assertEqual(len(event.get_result().chain), 1)
        self.assertIsInstance(event.get_result().chain[0], _Record)
        self.assertTrue(event.get_result().chain[0].file.endswith(".wav"))
        self.assertTrue(
            plugin._last_tts_text["aiocqhttp:GroupMessage:100"].startswith("原谅我这一生不羁放纵爱自由")
        )

    def test_failed_singing_does_not_leak_tags(self):
        """合成失败时不能把 <sing> 标签当正文发给用户。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        engine.fail_singing = True
        plugin._engine = engine
        event = _FakeEvent([_Plain("<sing>月亮代表我的心</sing>")])

        asyncio.run(plugin.tts_hook(event))

        chain = event.get_result().chain
        text = "".join(getattr(item, "text", "") for item in chain)
        self.assertNotIn("<sing>", text)
        self.assertIn("月亮代表我的心", text)

    def test_busy_instance_shows_queue_hint(self):
        """实例繁忙时不能干等：主路径也要给一句反馈。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        engine.singing_busy = True
        plugin._engine = engine
        event = _FakeEvent([_Plain("<sing>月亮代表我的心</sing>")])

        asyncio.run(plugin.tts_hook(event))

        text = "".join(getattr(item, "text", "") for item in event.get_result().chain)
        self.assertIn("排队", text)
        self.assertEqual(engine.sang, [], "繁忙时不应发起合成")

    def test_hook_normal_text_uses_synthesize(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.spoken, ["今天天气不错"])
        self.assertEqual(engine.sang, [])

    def test_hook_falls_back_when_engine_cannot_sing(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        engine.SUPPORTS_SINGING = False
        engine.SINGING_UNSUPPORTED_REASON = "当前引擎不支持唱歌模式"
        plugin._engine = engine
        event = _FakeEvent([_Plain("<sing>小星星</sing>")])

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(engine.spoken, ["小星星"], "回退时应剥离标签，不能把标记念出来")

    def test_text_limit_skips_normal_but_allows_singing(self):
        plugin = _build_plugin({"tts_text_limit_enable": True, "tts_text_limit": 5})
        engine = _FakeEngine()
        plugin._engine = engine

        normal_event = _FakeEvent([_Plain("这是一段明显超过五个字的普通文本")])
        asyncio.run(plugin.tts_hook(normal_event))
        self.assertEqual(engine.spoken, [])

        sing_event = _FakeEvent([_Plain("<sing>这是一段明显超过五个字的歌词</sing>")])
        asyncio.run(plugin.tts_hook(sing_event))
        self.assertTrue(engine.sang and engine.sang[0].startswith("这是一段明显超过五个字的歌词"))

    def test_singing_disabled_still_strips_tag(self):
        """关闭唱歌后也不能把 <sing> 标记念出来。"""
        plugin = _build_plugin({"singing_enable": False})
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("<sing>小星星</sing>")])

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(engine.spoken, ["小星星"])

    def test_intent_keyword_triggers_singing_without_tag(self):
        """用户说「唱首歌」但模型没打标签时，关键词兜底直接当歌词唱。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("好的，这就唱：\n月亮代表我的心\n轻轻的一个吻")])
        event.message_obj.message_str = "唱首歌"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(len(engine.sang), 1)
        self.assertIn("月亮代表我的心", engine.sang[0])
        self.assertEqual(engine.spoken, [])

    def test_intent_keyword_can_be_disabled(self):
        plugin = _build_plugin({"singing_auto_intent": False})
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("好的，这就唱：\n月亮代表我的心")])
        event.message_obj.message_str = "唱首歌"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(len(engine.spoken), 1)

    def test_bare_char_keyword_no_longer_triggers(self):
        """裸「唱」不再是关键词：否则「这首歌谁的」「合唱团」都会把普通回复拿去唱。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("后来我总算学会了如何去爱，可惜你早已远去")])
        event.message_obj.message_str = "这首歌谁的"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])

    def test_explicit_sing_keyword_still_triggers(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("后来我总算学会了如何去爱，可惜你早已远去")])
        event.message_obj.message_str = "唱一个"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(len(engine.sang), 1)

    def test_legacy_keywords_upgraded_automatically(self):
        """老配置的关键词表必须自动升级成新版（新版不含裸「唱」）。"""
        plugin = _build_plugin({"singing_intent_keywords": plugin_main.LEGACY_SINGING_KEYWORDS[0]})
        keywords = plugin._singing_keywords()
        self.assertIn("唱歌", keywords)
        self.assertNotIn("唱", keywords, "裸「唱」会把普通回复误判成唱歌")

        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("后来我总算学会了如何去爱")])
        event.message_obj.message_str = "唱一个"
        asyncio.run(plugin.tts_hook(event))
        self.assertEqual(len(engine.sang), 1)

    def test_custom_keywords_are_respected(self):
        plugin = _build_plugin({"singing_intent_keywords": "哼歌,来一段"})
        keywords = plugin._singing_keywords()
        self.assertEqual(keywords, ("哼歌", "来一段"))
        self.assertNotIn("唱", keywords)

    def test_legacy_seconds_no_longer_configurable(self):
        """旧配置里的 100 秒不再生效，固定按 40/50 秒走。"""
        plugin = _build_plugin({"singing_max_seconds": 100})
        target_chars, _, cps = plugin._singing_budget()
        self.assertEqual(int(target_chars / cps), plugin_main.SINGING_TARGET_SECONDS)

    def test_tone_is_off_by_default(self):
        """默认不做语气处理：纯语音与原项目行为一致。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        engine.voice_tone = ""
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])
        event.message_obj.message_str = "你好"
        event._qinyu_persona = "你是一个性格温柔的少女。"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.voice_tone, "")

    def test_tone_injected_when_enabled(self):
        plugin = _build_plugin({"voice_tone_enable": True})
        engine = _FakeEngine()
        engine.voice_tone = ""
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])
        event.message_obj.message_str = "你好"
        event._qinyu_persona = "你是一个性格温柔的少女，说话轻声细语。"

        asyncio.run(plugin.tts_hook(event))

        self.assertTrue(engine.voice_tone)
        self.assertIn("温柔", engine.voice_tone)

    def test_tone_switch_off_falls_back_to_original(self):
        """关闭语气开关：完全不下发语气，回到早期版本行为。"""
        plugin = _build_plugin({"voice_tone_enable": False})
        engine = _FakeEngine()
        engine.voice_tone = ""
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])
        event.message_obj.message_str = "你好"
        event._qinyu_persona = "你是一个性格温柔的少女。"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.voice_tone, "", "关闭后不应注入任何语气")

    def test_singing_does_not_inject_tone(self):
        """唱歌走官方原版：不注入任何语气。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        engine.voice_tone = "残留"
        plugin._engine = engine
        event = _FakeEvent([_Plain("<sing>月亮代表我的心</sing>")])
        event._qinyu_persona = "你是一个性格温柔的少女。"

        asyncio.run(plugin.tts_hook(event))

        self.assertTrue(engine.sang and engine.sang[0].startswith("月亮代表我的心"))
        self.assertEqual(engine.voice_tone, "残留", "唱歌不应改写语气属性")

    def test_llm_hook_caches_persona_and_injects_prompt(self):
        plugin = _build_plugin()
        event = _FakeEvent([])

        class _Req:
            system_prompt = "你是性格温柔的少女，说话轻声细语。"

        req = _Req()
        asyncio.run(plugin.tts_sing_prompt(event, req))

        self.assertIn("温柔", getattr(event, "_qinyu_persona", ""))
        self.assertIn("<sing>", req.system_prompt)

    def test_singing_mode_sings_plain_replies(self):
        """常驻唱歌：普通回答也以唱歌形式发出。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        engine.singing_mode = True
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])
        event.message_obj.message_str = "你好"

        asyncio.run(plugin.tts_hook(event))

        self.assertTrue(engine.sang and engine.sang[0].startswith("今天天气不错"))
        self.assertEqual(engine.spoken, [])

    def test_singing_mode_off_keeps_plain_speech(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        engine.singing_mode = False
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])
        event.message_obj.message_str = "你好"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(engine.spoken, ["今天天气不错"])

    def test_singing_mode_respects_master_switch(self):
        plugin = _build_plugin({"singing_enable": False})
        engine = _FakeEngine()
        engine.singing_mode = True
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])
        event.message_obj.message_str = "你好"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(engine.spoken, ["今天天气不错"])

    def test_plain_message_without_keyword_not_treated_as_song(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])
        event.message_obj.message_str = "你好"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(engine.spoken, ["今天天气不错"])

    def test_tail_text_is_discarded(self):
        """标签外的吐槽直接丢弃：发出去会拉长语音、破坏节奏。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("好的我来唱<sing>月亮代表我的心</sing>唱完啦，晚安")])

        asyncio.run(plugin.tts_hook(event))

        self.assertTrue(engine.sang and engine.sang[0].startswith("月亮代表我的心"))
        self.assertEqual(engine.spoken, [])
        chain = event.get_result().chain
        self.assertEqual(len(chain), 1, "标签外的文字不应出现在结果里")
        self.assertIsInstance(chain[0], _Record)


    def test_leading_chatter_is_dropped(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("行，我给你唱两句<sing>月亮代表我的心</sing>")])

        asyncio.run(plugin.tts_hook(event))

        self.assertTrue(engine.sang and engine.sang[0].startswith("月亮代表我的心"))
        self.assertEqual(len(event.get_result().chain), 1)

    def test_refusal_keyword_fallback_does_not_sing(self):
        """模型拒绝时不能把拒绝语当歌词唱。"""
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("抱歉，我不能唱歌")])
        event.message_obj.message_str = "唱首歌"

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(engine.spoken, ["抱歉，我不能唱歌"])

    def test_long_lyrics_are_sliced_to_hard_limit(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        lyrics = "\n".join(f"第{i}句完整的歌词在这里写得比较长" for i in range(1, 40))
        event = _FakeEvent([_Plain(f"<sing>{lyrics}</sing>")])

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(len(engine.sang), 1)
        sung = engine.sang[0]
        # 只按字数兜底，不再限制行数（行数限制会把完整乐句切碎）
        hard_chars = int(plugin_main.SINGING_MAX_SECONDS * plugin_main.SINGING_CHARS_PER_SECOND)
        self.assertLessEqual(len(sung.replace(chr(10), "")), hard_chars)
        self.assertTrue(lyrics.startswith(sung), "必须取原文开头且是完整乐句")

    def test_singing_bypasses_probability(self):
        plugin = _build_plugin({"tts_probability": 1})
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("<sing>小星星</sing>")])

        with unittest.mock.patch.object(plugin_main.random, "randint", return_value=100):
            asyncio.run(plugin.tts_hook(event))

        self.assertTrue(engine.sang, "概率低于 100 时唱歌仍应执行")
        self.assertTrue(engine.sang[0].startswith("小星星"))

    def test_singing_stops_when_probability_zero(self):
        """probability=0 表示彻底关闭自动 TTS，唱歌也不能例外。"""
        plugin = _build_plugin({"tts_probability": 0})
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("<sing>小星星</sing>")])

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(engine.spoken, [])

    def test_string_probability_does_not_crash(self):
        """WebUI 把数字存成字符串时不能 TypeError。"""
        plugin = _build_plugin({"tts_probability": "100"})
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.spoken, ["今天天气不错"])

    def test_invalid_probability_falls_back_to_always(self):
        plugin = _build_plugin({"tts_probability": "abc"})
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([_Plain("今天天气不错")])

        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(engine.spoken, ["今天天气不错"])

    def test_temp_cleanup_drops_text_cache(self):
        """清理过期的临时音频时，文本缓存也要同步回收，否则长期运行会一直涨。"""
        plugin = _build_plugin()
        stale = plugin._temp_dir / "tts_stale001.wav"
        stale.write_bytes(b"old")
        plugin._tts_audio_text[str(stale)] = "旧文本"
        import os

        os.utime(stale, (0, 0))  # 置为极旧，触发 TTL 清理

        plugin._cleanup_temp_files(force=True)

        self.assertFalse(stale.exists())
        self.assertNotIn(str(stale), plugin._tts_audio_text)
        """model 改成 voiceclone 的真实引擎实例：判为不支持，且载荷里不能再出现唱歌标签。"""
        import base64

        from astrbot_plugin_qinyu.engines.mimo.v2_5 import MiMoV25Engine

        class _StubResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"choices": [{"message": {"audio": {"data": base64.b64encode(b"audio").decode()}}}]}

        class _StubClient:
            def __init__(self):
                self.is_closed = False
                self.calls = []

            async def post(self, url, headers=None, json=None, timeout=None):
                self.calls.append({"payload": json, "timeout": timeout})
                return _StubResponse()

            async def aclose(self):
                self.is_closed = True

        plugin = _build_plugin()
        engine = MiMoV25Engine(
            {"api_key": "k", "model": "mimo-v2.5-tts-voiceclone", "timeout": 30, "singing_timeout": 90}
        )
        stub_client = _StubClient()
        engine._client = stub_client
        plugin._engine = engine
        self.assertFalse(plugin._supports_singing(engine))

        event = _FakeEvent([_Plain("<sing>小星星</sing>")])
        asyncio.run(plugin.tts_hook(event))

        self.assertEqual(len(stub_client.calls), 1)
        content = stub_client.calls[0]["payload"]["messages"][-1]["content"]
        self.assertNotIn("(唱歌)", content)
        self.assertEqual(content, "小星星", "回退合成时应只念歌词，不带标记")
        self.assertEqual(stub_client.calls[0]["timeout"], 30, "非唱歌请求应沿用普通超时")
        self.assertIsInstance(event.get_result().chain[0], _Record)


class SingCommandTests(unittest.TestCase):
    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        import os

        os.chdir(self._tmp.name)

    def tearDown(self):
        import os

        os.chdir(self._cwd)
        self._tmp.cleanup()

    async def _run_command(self, plugin, event):
        return [item async for item in plugin.sing_tts(event)]

    def test_tts_sing_returns_audio_for_singing_engine(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        event = _FakeEvent([], command_text="tts_sing 茉莉花好一朵美丽的茉莉花")

        results = asyncio.run(self._run_command(plugin, event))

        # 用户显式指定歌词：一字不改，不补哼唱
        self.assertEqual(engine.sang, ["茉莉花好一朵美丽的茉莉花"])
        kinds = [kind for kind, _ in results]
        self.assertIn("chain", kinds)

    def test_tts_sing_reports_unsupported_engine(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        engine.SUPPORTS_SINGING = False
        engine.SINGING_UNSUPPORTED_REASON = "MiMo V2.5-VoiceClone 不支持唱歌模式"
        plugin._engine = engine
        event = _FakeEvent([], command_text="tts_sing 小星星")

        results = asyncio.run(self._run_command(plugin, event))

        self.assertEqual(engine.sang, [])
        self.assertEqual(len(results), 1)
        self.assertIn("不支持唱歌模式", results[0][1])

    def test_tts_sing_without_lyrics_shows_usage(self):
        plugin = _build_plugin()
        plugin._engine = _FakeEngine()
        event = _FakeEvent([], command_text="tts_sing")

        results = asyncio.run(self._run_command(plugin, event))

        self.assertIn("用法", results[0][1])

    def test_tts_sing_slices_long_lyrics(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine
        lyrics = "\n".join(f"第{i}句完整的歌词在这里写得比较长" for i in range(1, 40))
        event = _FakeEvent([], command_text=f"tts_sing {lyrics}")

        results = asyncio.run(self._run_command(plugin, event))

        self.assertEqual(len(engine.sang), 1)
        hard_chars = int(plugin_main.SINGING_MAX_SECONDS * plugin_main.SINGING_CHARS_PER_SECOND)
        # 按整句兜底切分时允许超出单句长度（完整性优先于严格字数）
        self.assertLessEqual(len(engine.sang[0]), hard_chars + 20)
        messages = " ".join(text for kind, text in results if kind == "plain")
        # 超长歌词现在统一按目标时长裁剪，提示语说明「超出目标时长」并给出关闭开关
        self.assertIn("超出目标时长", messages)
        self.assertIn("singing_clip_to_target", messages)

    def test_status_reports_singing_diagnostics(self):
        plugin = _build_plugin()
        plugin._engine = _FakeEngine()
        event = _FakeEvent([], command_text="tts_status")

        async def collect():
            return [item async for item in plugin.tts_status(event)]

        results = asyncio.run(collect())
        text = results[0][1]
        # 精简后：状态只回答「现在什么状态」，操作入口指向 /tts_panel
        self.assertIn("提示词", text)
        self.assertIn("兜底", text)
        self.assertIn("时长", text)
        self.assertIn("统一模式", text)
        self.assertIn("/tts_panel", text)

    def test_tts_sing_follows_astrbot_tts_switch(self):
        plugin = _build_plugin()
        engine = _FakeEngine()
        plugin._engine = engine

        async def _disabled(event):
            return False

        plugin._is_astrbot_tts_enabled = _disabled
        event = _FakeEvent([], command_text="tts_sing 小星星")

        results = asyncio.run(self._run_command(plugin, event))

        self.assertEqual(engine.sang, [])
        self.assertIn("TTS 开关未开启", results[0][1])

    def test_tts_sing_failure_hides_raw_error(self):
        """接口异常细节只进日志，不能直接回给用户。"""
        plugin = _build_plugin()

        class _BrokenEngine(_FakeEngine):
            async def synthesize_singing(self, lyrics):
                raise RuntimeError("500: api-key sk-secret-invalid, upstream body ...")

        plugin._engine = _BrokenEngine()
        event = _FakeEvent([], command_text="tts_sing 茉莉花好一朵美丽的茉莉花")

        results = asyncio.run(self._run_command(plugin, event))

        message = " ".join(text for kind, text in results if kind == "plain")
        self.assertIn("后台日志", message)
        self.assertNotIn("sk-secret-invalid", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
