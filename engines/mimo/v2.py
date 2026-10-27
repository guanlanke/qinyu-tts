"""MiMo V2 TTS 引擎（支持 <style> 唱歌标签）"""

import asyncio
import base64
import logging

import httpx

from ..base import TTSEngine
from .singing import (
    DEFAULT_SINGING_SPEED,
    DEFAULT_SINGING_STYLE,
    FOLLOW_VOICE_VALUES,
    resolve_singing_format,
    resolve_singing_voice,
    apply_style_prefix_legacy,
    build_singing_content_style_tag,
    cut_chitchat_head,
    has_lyric_content,
    cut_chitchat_tail,
    is_singing_style,
    SINGING_DIRECTIVE,
    build_style_prompt,
    prepare_reference_sing_text,
    REFERENCE_SING_TAG,
    REFERENCE_SING_USER_PROMPT,
    singing_directive,
    singing_length_warning,
    strip_action_tags,
    split_lyric_lines,
    strip_explanation_lines,
    strip_voice_direction_tags,
)

logger = logging.getLogger(__name__)

#: 模型名里出现这些标记时不支持唱歌
_NON_SINGING_MODEL_MARKERS = ("voicedesign", "voiceclone")

#: 唱歌请求的保底超时：低于这个值会有概率在满预算歌词上超时失败
_MIN_SINGING_TIMEOUT = 120


class MiMoV2Engine(TTSEngine):
    """小米 MiMo-V2-TTS 引擎

    API 特点：
    - 模型: mimo-v2-tts
    - 风格控制: <style>标签</style> 置于 assistant content 开头
    - 音色: 预置音色 (mimo_default, default_zh, default_en)
    - user message 为可选，可用于影响语气
    - 唱歌为实验性能力：V2 体系用 <style>唱歌</style> 前缀，正式支持以 V2.5 为准
    """

    SUPPORTS_SINGING = True
    SINGING_UNSUPPORTED_REASON = "MiMo V2 的唱歌为实验性能力，如需稳定唱歌请切换到 [MiMo] V2.5-TTS 引擎"

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key") or ""
        self.api_base: str = config.get("api_base") or "https://api.xiaomimimo.com/v1"
        self.model: str = config.get("model") or "mimo-v2-tts"
        self.voice: str = config.get("voice") or ""
        self.audio_format: str = config.get("format") or "wav"
        self.style: str = config.get("style") or ""
        self.seed_text: str = config.get("seed_text") or ""
        self.timeout: int = config.get("timeout") or 30
        self._client: httpx.AsyncClient | None = None
        # 唱歌请求耗时长，串行化避免同时打多个长请求触发厂商限流
        self._singing_lock = asyncio.Lock()

        # 唱歌模式（与朗读完全隔离，各自独立参数）
        self.singing_mode: bool = bool(config.get("singing_mode", False))
        self.singing_format_override: str = ""  # 已废弃：唱歌固定 wav
        self.singing_voice_lock: bool = bool(config.get("singing_voice_lock", True))
        self.singing_speed: str = (
            config.get("singing_speed") or DEFAULT_SINGING_SPEED
        ).strip() or DEFAULT_SINGING_SPEED
        self.singing_style: str = (
            config.get("singing_style") or DEFAULT_SINGING_STYLE
        ).strip() or DEFAULT_SINGING_STYLE
        self.singing_short_lines: bool = bool(config.get("singing_short_lines", False))
        # 歌词语义清洗开关，默认关：原样送出才是 v2.4.1 验证过的行为
        self.singing_cleanup: bool = bool(config.get("singing_cleanup", False))
        # 参考项目方案（默认开）：照搬 astrbot_plugin_tts_emotion_router v3.3.4
        self.singing_reference_mode: bool = bool(
            config.get("singing_reference_mode", True)
        )
        # 复古模式：等价于 v1.4.7（参考模式关掉后才生效）
        self.singing_legacy_mode: bool = bool(config.get("singing_legacy_mode", False))
        # 把多行合成一整段（逗号连接），对齐官方唱歌示例的单段形态
        self.singing_join_lines: bool = bool(config.get("singing_join_lines", False))
        try:
            self.singing_takes: int = int(config.get("singing_takes") or 1)
        except (TypeError, ValueError):
            self.singing_takes = 1
        try:
            self.singing_timeout: int = int(config.get("singing_timeout") or 0)
        except (TypeError, ValueError):
            self.singing_timeout = 0

        # 只属于纯语音链路的可选语气（插件注入；默认空 = 原项目行为）。唱歌链路不读它
        self.voice_tone: str = ""
        # ===== 全局风格控制（朗读与唱歌共用，照搬参考项目的 5 维体系）=====
        self.overall_tone: str = str(config.get("overall_tone") or "").strip()
        self.timbre_positioning: str = str(
            config.get("timbre_positioning") or ""
        ).strip()
        self.persona_accent: str = str(config.get("persona_accent") or "").strip()
        self.dialect: str = str(config.get("dialect") or "").strip()
        self.role_play: str = str(config.get("role_play") or "").strip()
        # 唱歌专用音色：与朗读音色互相独立；留「跟随朗读音色」时用朗读音色
        self.singing_voice: str = str(config.get("singing_voice") or "").strip()

        # 参数校验
        if not self.api_key:
            raise ValueError("MiMo V2 TTS: api_key 不能为空")

        self.SUPPORTS_SINGING = self._detect_singing_support()
        if not self.SUPPORTS_SINGING:
            self.SINGING_UNSUPPORTED_REASON = (
                f"当前模型 {self.model} 不支持唱歌模式，请把模型改回 mimo-v2-tts"
            )

    def _detect_singing_support(self) -> bool:
        model = (self.model or "").strip().lower()
        return not any(marker in model for marker in _NON_SINGING_MODEL_MARKERS)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    @property
    def singing_busy(self) -> bool:
        """是否有唱歌请求正在占用（多群共用同一实例时用来给用户反馈）。"""
        return self._singing_lock.locked()

    def _assemble_speech_content(
        self, text: str, style: str, *, legacy_style_tag: bool = False
    ) -> str:
        """把「单一 style 字段」和「5 维全局风格」合并进一对标签。

        参考项目的做法是五个维度各自独立、聚合进同一对括号；
        本插件保留原来的单一 style 字段，两者合并后形如：
        (温柔、甜美、夹子音)正文（V2 体系用 <style>…</style>）。
        """
        body = (text or "").strip()
        style_text = "" if is_singing_style(style) else (style or "").strip()
        if not style_text:
            return body
        if legacy_style_tag:
            return f"<style>{style_text}</style>{body}"
        return f"({style_text}){body}"

    def _global_style_prompt(self, *, singing: bool = False) -> str:
        """5 维风格的**自然语言**描述，放进 user 消息，不会被念出来。"""
        return build_style_prompt(
            self.overall_tone,
            self.timbre_positioning,
            self.persona_accent,
            self.dialect,
            self.role_play,
            "",
            singing=singing,
        )


    def _resolve_timeout(self, singing: bool) -> int:
        if singing:
            # 唱歌是长请求：满预算约 130 字，按实际演唱速度要 60~80 秒，
            # 沿用朗读的 60 秒会随机 ReadTimeout，用户只看到「合成失败」
            return max(self.singing_timeout or 0, _MIN_SINGING_TIMEOUT)
        return self.timeout

    def _is_singing(self, singing: bool) -> bool:
        """显式唱歌、常驻唱歌或 style 写了唱歌，且当前模型允许唱歌。"""
        wants_singing = bool(singing or self.singing_mode or is_singing_style(self.style))
        return wants_singing and self.SUPPORTS_SINGING

    # ---------- 两条链路：纯语音 / 唱歌，互相不读对方的参数 ----------

    def _build_speech_payload(self, text: str) -> dict:
        """纯语音链路：与原项目一致。"""
        messages: list[dict[str, str]] = []
        speech_parts = [
            part.strip()
            for part in ((self.seed_text or ""), (self.voice_tone or ""))
            if part and part.strip()
        ]
        style_prompt_line = self._global_style_prompt(singing=False)
        if style_prompt_line:
            speech_parts.append(style_prompt_line)
        if speech_parts:
            messages.append({"role": "user", "content": "\n".join(speech_parts)})
        style_for_prefix = "" if is_singing_style(self.style) else self.style
        messages.append(
            {"role": "assistant", "content": self._assemble_speech_content(text, style_for_prefix, legacy_style_tag=True)}
        )
        return {
            "model": self.model,
            "messages": messages,
            "audio": {"format": self.audio_format, "voice": self.voice},
        }

    def _build_singing_payload(self, lyrics: str) -> dict:
        """唱歌链路：演唱指令 + <style>唱歌</style>歌词，与朗读隔离。"""
        # ===== 参考项目方案（默认开）=====
        # 逐字照搬 astrbot_plugin_tts_emotion_router v3.3.4 的严格唱歌逻辑：
        # 全角（唱歌）钉在开头 + 一句「连成一首歌从头唱到尾」的指令 +
        # 只删整行串场白、保留全部歌词/标点、不截断、不切句、不合段。
        if self.singing_reference_mode:
            # 唱歌用专用音色；填「跟随朗读音色」时才用朗读音色
            voice = resolve_singing_voice(self.singing_voice, self.voice)
            lyric = prepare_reference_sing_text(lyrics)
            logger.info(
                "[%s] 唱歌（参考项目方案）：音色=%s 字数=%s 首行=%s",
                "MiMo V2.5" if "2.5" in self.instance_name else "MiMo V2",
                voice,
                len(lyric),
                lyric.split("\n")[0][:20],
            )
            # 风格走 user 自然语言，assistant 只保留（唱歌）标签 + 歌词
            style_line = self._global_style_prompt(singing=True)
            directive = (
                REFERENCE_SING_USER_PROMPT + "\n" + style_line
                if style_line
                else REFERENCE_SING_USER_PROMPT
            )
            head = REFERENCE_SING_TAG
            return {
                "model": self.model,
                "messages": [
                    {"role": "user", "content": directive},
                    {"role": "assistant", "content": head + lyric},
                ],
                "audio": {"format": "wav", "voice": voice},
            }

        # ===== 复古模式（默认开）=====
        # 等价于 v1.4.7 —— 用户明确说过「那时候能唱」的那一版：
        # 只保留 (唱歌) 标签 + 一句演唱指令，歌词原样送、音色跟随朗读，
        # 不做任何寒暄切尾、动作标签剔除、解释行清洗、切句、合段。
        # 后续那些「优化」都是我听不到声音、靠推理加上去的，实测越加越差，
        # 所以默认全部关掉；想要哪一项再单独打开。
        if self.singing_legacy_mode:
            # 唱歌用专用音色；填「跟随朗读音色」时才用朗读音色
            voice = resolve_singing_voice(self.singing_voice, self.voice)
            logger.info(
                "[MiMo V2.5] 唱歌（复古模式）：音色=%s 模式=%s",
                voice,
                self.model,
            )
            style_line = self._global_style_prompt(singing=True)
            directive = (
                SINGING_DIRECTIVE + "\n" + style_line if style_line else SINGING_DIRECTIVE
            )
            head = "（唱歌）"
            return {
                "model": self.model,
                "messages": [
                    {"role": "user", "content": directive},
                    {"role": "assistant", "content": head + (lyrics or "").strip()},
                ],
                "audio": {"format": "wav", "voice": voice},
            }

        # 尾部寒暄始终切（收益大）；头部只在语义清洗开启时切——
        # 「好的事情总会来」这类歌词首行太常见，误删代价远大于漏切
        text = cut_chitchat_tail(lyrics) or lyrics
        if self.singing_cleanup:
            text = cut_chitchat_head(text) or text
        # 动作标签删的是“标签”不是“歌词”，始终生效
        text = strip_action_tags(text)
        if self.singing_voice_lock:
            text = strip_voice_direction_tags(text)
        if self.singing_cleanup:
            cleaned = strip_explanation_lines(text)
            if cleaned.strip():
                text = cleaned
        # 顺序执行：先合段（对齐官方单段形态），再切短句（给换气点）。
        # 之前设成互斥是错的——两者都开时应当各司其职，而不是让后者失效
        if self.singing_join_lines:
            pieces = [line.strip() for line in text.split("\n") if line.strip()]
            joined = ""
            for piece in pieces:
                if not joined:
                    joined = piece
                elif joined[-1] in "，,。.；;！!？?、…~～）)]】":
                    # 上一段已经是标点结尾，不要再补逗号，否则会造出 。， 或 ，， 或 （换气），
                    joined += piece
                else:
                    joined += "，" + piece
            text = joined
        if self.singing_short_lines:
            # 一行太长会换气不当、吞字、唱错词；按逗号切成短句后才有呼吸点
            text = split_lyric_lines(text) or text
        if not has_lyric_content(text):
            raise ValueError("MiMo V2 唱歌：歌词里只有（换气）（叹气）这类标签，没有可唱的内容")
        # 唱歌音色：填了「唱歌专用音色」就用它，留「跟随朗读音色」才回落到朗读音色。
        # 这里曾经和 v2_5.py 一样写成 self.voice，导致唱歌音色永远不生效。
        voice = resolve_singing_voice(self.singing_voice, self.voice)
        audio_format = resolve_singing_format(self.singing_format_override, self.audio_format)
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": singing_directive(self.singing_speed, self.singing_style),
                },
                {
                    "role": "assistant",
                    "content": build_singing_content_style_tag(text, sanitize=self.singing_cleanup),
                },
            ],
            "audio": {"format": audio_format, "voice": voice},
        }

    def _build_payload(self, text: str, singing: bool = False) -> dict:
        """兼容入口：按模式分派到两条互不相干的链路。"""
        if self._is_singing(singing):
            return self._build_singing_payload(text)
        return self._build_speech_payload(text)

    async def _request(self, text: str, singing: bool) -> tuple[bytes, str]:
        payload = self._build_payload(text, singing=singing)
        client = self._get_client()
        url = f"{self.api_base.rstrip('/')}/chat/completions"
        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }

        response = await client.post(
            url,
            headers=headers,
            json=payload,
            timeout=self._resolve_timeout(singing),
        )
        self._check_response("MiMo V2", response)

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"MiMo V2 TTS 返回空 choices: {data}")

        audio_data = choices[0].get("message", {}).get("audio", {}).get("data")
        if not audio_data:
            raise RuntimeError(f"MiMo V2 TTS 返回无音频数据: {data}")

        return base64.b64decode(audio_data), payload["audio"]["format"]

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        return await self._request(text, singing=False)

    async def synthesize_singing(self, lyrics: str) -> tuple[bytes, str]:
        warning = singing_length_warning(lyrics)
        if warning:
            logger.warning("[MiMo V2] %s", warning)
        if self._singing_lock.locked():
            logger.info("[MiMo V2] 前一个唱歌请求仍在进行，本次排队等待")
        async with self._singing_lock:
            return await self._request(lyrics, singing=True)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
