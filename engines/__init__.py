"""TTS 引擎工厂模块"""

from .base import TTSEngine
from .mimo import MiMoV2Engine, MiMoV25Engine, MiMoV25DesignEngine, MiMoV25CloneEngine
from .mimo.singing import (
    SINGING_CANONICAL_TAG,
    SINGING_DIRECTIVE,
    SINGING_MAX_LINES,
    build_singing_content,
    estimate_seconds,
    extract_singing_block,
    guess_lyrics_from_reply,
    has_singing_tag,
    is_singing_style,
    looks_like_refusal,
    normalize_lyrics,
    prepare_lyrics,
    resolve_singing_format,
    resolve_singing_voice,
    singing_length_warning,
    slice_lyrics,
    split_lyric_lines,
    split_singing_reply,
    strip_voice_direction_tags,
)
from .mimo.tone import build_speech_tone, extract_persona_traits
from .openai import OpenAICompatEngine
from .volcengine import VolcengineEngine
from .aliyun import AliyunEngine
from .azure import AzureEngine
from .elevenlabs import ElevenLabsEngine
from .minimax import MiniMaxEngine
from .custom_http import CustomHTTPEngine

ENGINE_REGISTRY: dict[str, type[TTSEngine]] = {
    "mimo_v2": MiMoV2Engine,
    "mimo_v2_5": MiMoV25Engine,
    "mimo_v2_5_voicedesign": MiMoV25DesignEngine,
    "mimo_v2_5_voiceclone": MiMoV25CloneEngine,
    "openai_compat": OpenAICompatEngine,
    "volcengine": VolcengineEngine,
    "aliyun": AliyunEngine,
    "azure": AzureEngine,
    "elevenlabs": ElevenLabsEngine,
    "custom_http": CustomHTTPEngine,
    "minimax": MiniMaxEngine,
}

#: 支持唱歌模式的引擎模板键，供指令层做能力标注
SINGING_TEMPLATE_KEYS = frozenset(
    key for key, engine_cls in ENGINE_REGISTRY.items()
    if getattr(engine_cls, "SUPPORTS_SINGING", False)
)


def get_engine(engine_config: dict, plugin_config: dict | None = None) -> TTSEngine:
    """根据配置创建引擎实例"""
    template_key = engine_config.get("__template_key", "")
    engine_cls = ENGINE_REGISTRY.get(template_key)
    if engine_cls is None:
        available = ", ".join(ENGINE_REGISTRY.keys())
        raise ValueError(f"未知的 TTS 引擎类型: '{template_key}'，可用类型: {available}")
    return engine_cls(engine_config, plugin_config or {})

#: 主题用途：朗读 / 唱歌。两者可以指向不同引擎实例。
ROLE_SPEECH = "speech"
ROLE_SINGING = "singing"


def engine_supports_singing(engine) -> bool:
    """这个引擎实例能不能唱歌。"""
    return bool(getattr(engine, "SUPPORTS_SINGING", False))
