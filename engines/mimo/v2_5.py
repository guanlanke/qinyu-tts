"""MiMo V2.5 TTS 引擎（预置音色，支持唱歌模式）"""

import asyncio
import base64
import logging

import httpx

from ..base import TTSEngine
from .singing import (
    DEFAULT_SINGING_SPEED,
    DEFAULT_SINGING_STYLE,
    SINGING_CANONICAL_TAG,
    apply_style_prefix,
    build_singing_content,
    contains_cjk,
    cut_chitchat_head,
    has_lyric_content,
    cut_chitchat_tail,
    is_singing_style,
    FOLLOW_VOICE_VALUES,
    resolve_singing_format,
    resolve_singing_voice,
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

#: 英文预置音色，唱中文歌词时效果通常弱于 冰糖 / 茉莉
_ENGLISH_VOICES = {"mia", "chloe", "milo", "dean"}

#: 模型名里出现这些标记时不支持唱歌（官方限制）
_NON_SINGING_MODEL_MARKERS = ("voicedesign", "voiceclone")

#: 唱歌请求的保底超时：低于这个值会有概率在满预算歌词上超时失败
_MIN_SINGING_TIMEOUT = 120

#: 单次唱歌最多生成几条候选。官方建议 TTS 多生成几次挑一条，
#: 成本随条数线性上升，所以设上限 3
_MAX_SINGING_TAKES = 3


class MiMoV25Engine(TTSEngine):
    """小米 MiMo-V2.5-TTS 引擎

    API 特点：
    - 模型: mimo-v2.5-tts
    - 风格控制:
      - 自然语言控制: 放在 user message 的 content 中
      - 音频标签控制: (风格) 置于 assistant content 开头
    - 音色: 更多精品预置音色
    - 唱歌: 仅预置音色模型支持，必须写作 `(唱歌)歌词` 且标签位于文本最开头，
      歌词必须完整；唱歌时不建议叠加其它风格标签或自然语言指令
    """

    SUPPORTS_SINGING = True

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key") or ""
        self.api_base: str = config.get("api_base") or "https://api.xiaomimimo.com/v1"
        self.model: str = config.get("model") or "mimo-v2.5-tts"
        self.voice: str = config.get("voice") or ""
        self.audio_format: str = config.get("format") or "wav"
        self.style: str = config.get("style") or ""
        self.style_prompt: str = config.get("style_prompt") or ""
        self.timeout: int = config.get("timeout") or 60
        self._client: httpx.AsyncClient | None = None
        # 唱歌请求耗时长，串行化避免同时打多个长请求触发厂商限流
        self._singing_lock = asyncio.Lock()

        # 唱歌模式（与朗读完全隔离，各自独立参数）
        self.singing_mode: bool = bool(config.get("singing_mode", False))
        self.singing_format_override: str = ""  # 已废弃：唱歌固定 wav
        # 音色锁定：剔除歌词里的换嗓标签，防止一句女声一句男声
        self.singing_voice_lock: bool = bool(config.get("singing_voice_lock", True))
        self.singing_speed: str = (
            config.get("singing_speed") or DEFAULT_SINGING_SPEED
        ).strip() or DEFAULT_SINGING_SPEED
        self.singing_style: str = (
            config.get("singing_style") or DEFAULT_SINGING_STYLE
        ).strip() or DEFAULT_SINGING_STYLE
        try:
            self.singing_takes: int = int(config.get("singing_takes") or 1)
        except (TypeError, ValueError):
            self.singing_takes = 1
        self.singing_short_lines: bool = bool(config.get("singing_short_lines", False))
        # 歌词语义清洗开关，默认关：原样送出才是 v2.4.1 验证过的行为
        self.singing_cleanup: bool = bool(config.get("singing_cleanup", False))
        # 参考项目方案：默认**关**，必须与 _conf_schema.json 的默认值一致。
        # 以前这里写 True 而 schema 是 false —— 老配置缺这两个键时走「参考链路」，
        # 新配置走「复古链路」，同一个插件在私聊/群聊表现不同，日志却仍显示「复古=开」。
        self.singing_reference_mode: bool = bool(
            config.get("singing_reference_mode", False)
        )
        # 复古模式：默认开（与 schema 一致），这条链路实测最好听
        self.singing_legacy_mode: bool = bool(config.get("singing_legacy_mode", True))
        # 把多行合成一整段（逗号连接），对齐官方唱歌示例的单段形态
        self.singing_join_lines: bool = bool(config.get("singing_join_lines", False))
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
            raise ValueError("MiMo V2.5 TTS: api_key 不能为空")

        # 唱歌能力按模型名判定：用户把 model 改成 voicedesign / voiceclone 时不再误报支持
        self.SUPPORTS_SINGING = self._detect_singing_support()
        if not self.SUPPORTS_SINGING:
            self.SINGING_UNSUPPORTED_REASON = (
                f"当前模型 {self.model} 不支持唱歌模式（官方仅 mimo-v2.5-tts 预置音色支持），"
                "请把模型改回 mimo-v2.5-tts 或切换到 [MiMo] V2.5-TTS 引擎"
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
        """显式唱歌请求、常驻唱歌、或 style 里写了唱歌，且当前模型支持唱歌。"""
        wants_singing = bool(singing or self.singing_mode or is_singing_style(self.style))
        return wants_singing and self.SUPPORTS_SINGING

    # ---------- 两条链路：纯语音 / 唱歌，互相不读对方的参数 ----------

    def _build_speech_payload(self, text: str) -> dict:
        """纯语音链路：与原项目一致。

        只有用户自己配的 style_prompt（以及可选的内部语气）会作为 user 消息；
        正文走 assistant。不读唱歌参数、不做歌词处理。
        """
        messages: list[dict[str, str]] = []
        speech_parts = [
            part.strip()
            for part in ((self.style_prompt or ""), (self.voice_tone or ""))
            if part and part.strip()
        ]
        style_prompt_line = self._global_style_prompt(singing=False)
        if style_prompt_line:
            speech_parts.append(style_prompt_line)
        if speech_parts:
            messages.append({"role": "user", "content": "\n".join(speech_parts)})
        style_for_prefix = "" if is_singing_style(self.style) else self.style
        messages.append(
            {"role": "assistant", "content": self._assemble_speech_content(text, style_for_prefix)}
        )
        return {
            "model": self.model,
            "messages": messages,
            "audio": {"format": self.audio_format, "voice": self.voice},
        }

    def _build_singing_payload(self, lyrics: str) -> dict:
        """唱歌链路：strict_sing 写法，与朗读完全隔离。

        一条禁止朗读的 user 指令 + 一条 (唱歌)歌词 的 assistant 消息；
        音色/格式/超时全部走 singing_* 参数，不读朗读侧的配置。
        """
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
        # 只检查 text.strip() 已经不够：（换气）现在会被保留，但它不是歌词
        if not has_lyric_content(text):
            # 分开报错：空歌词与「只有标签」是两种不同原因，提示要能指导用户
            if not (text or "").strip():
                raise ValueError("MiMo V2.5 唱歌：歌词为空，没有可唱的内容")
            raise ValueError(
                "MiMo V2.5 唱歌：歌词里只有（换气）（叹气）这类标签，没有可唱的内容"
            )
        # 标签永远只写一个「唱歌」：v1.6.0 把气质叠进标签后反而退化
        # 气质与速度改走 user message（官方「自然语言控制」通道）
        voice = (self.voice or "mimo_default")
        audio_format = resolve_singing_format(self.singing_format_override, self.audio_format)

        # 中文歌词配英文嗓会唱出外国人口音，这里显式提示被替换过
        configured_voice = ""
        follow_set = {item.lower() for item in FOLLOW_VOICE_VALUES}
        origin = (
            configured_voice
            if configured_voice and configured_voice.lower() not in follow_set
            else (self.voice or "mimo_default")
        )
        if (origin or "").strip().lower() in _ENGLISH_VOICES or (
            origin or ""
        ).strip().lower() == "mimo_default":
            if contains_cjk(lyrics):
                logger.info(
                    "[MiMo V2.5] 唱歌音色 %s 为英文/默认音色，唱中文会带外国口音，已改用 %s"
                    "（可在「唱歌专用音色」自行指定 冰糖/茉莉/苏打/白桦）",
                    origin,
                    voice,
                )

        # 一条日志把「实际送出去的东西」摊开，方便对着听感排查
        logger.info(
            "[MiMo V2.5] 唱歌参数：音色=%s 格式=%s 模型=%s 歌词字数=%s 首行=%s",
            voice,
            audio_format,
            self.model,
            len(text.replace("\n", "")),
            (text.split("\n")[0][:24] if text else ""),
        )

        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": singing_directive(self.singing_speed, self.singing_style),
                },
                {
                    "role": "assistant",
                    "content": build_singing_content(text, sanitize=self.singing_cleanup),
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
        self._check_response("MiMo V2.5", response)

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"MiMo V2.5 TTS 返回空 choices: {data}")

        audio_data = choices[0].get("message", {}).get("audio", {}).get("data")
        if not audio_data:
            raise RuntimeError(f"MiMo V2.5 TTS 返回无音频数据: {data}")

        # 接口可能返回 data URI 前缀，直接 b64decode 会解出垃圾字节 → 播放变速/怪腔
        if isinstance(audio_data, str) and audio_data.startswith("data:"):
            audio_data = audio_data.split(",", 1)[-1]
        audio_bytes = base64.b64decode(audio_data)
        audio_format = payload["audio"]["format"]
        if audio_format == "wav" and not audio_bytes.startswith(b"RIFF"):
            logger.warning(
                "[MiMo V2.5] 返回音频缺少 RIFF 头（前 4 字节 %s），格式可能不是 wav",
                audio_bytes[:4],
            )
        return audio_bytes, audio_format

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        return await self._request(text, singing=False)

    async def synthesize_singing(self, lyrics: str) -> tuple[bytes, str]:
        warning = singing_length_warning(
            lyrics,
            truncates=not (self.singing_reference_mode or self.singing_legacy_mode),
        )
        if warning:
            logger.warning("[MiMo V2.5] %s", warning)
        # 超时保底 120 秒已由 _resolve_timeout 保证，不再提示「沿用 X 秒」以免误导排查
        if self.singing_busy:
            logger.info("[MiMo V2.5] 前一个唱歌请求仍在进行，本次排队等待")
        async with self._singing_lock:
            # 官方明说 TTS 有随机性、同样输入效果可能不同，可以多生成几次挑一条。
            # 演唱越完整气息越足，音频越长；所以取最长的那条作为最佳结果。
            takes = max(1, min(_MAX_SINGING_TAKES, int(self.singing_takes or 1)))
            best: tuple[bytes, str] | None = None
            last_error: Exception | None = None
            for index in range(takes):
                try:
                    result = await self._request(lyrics, singing=True)
                except Exception as exc:  # noqa: BLE001 - 单次失败不能让整次演唱失败
                    last_error = exc
                    logger.warning(
                        "[MiMo V2.5] 第 %s/%s 次演唱失败：%s", index + 1, takes, exc
                    )
                    continue
                if best is None or len(result[0]) > len(best[0]):
                    best = result
                if takes > 1:
                    logger.info(
                        "[MiMo V2.5] 第 %s/%s 次演唱：%s 字节%s",
                        index + 1,
                        takes,
                        len(result[0]),
                        "（当前最佳）" if best is result else "",
                    )
            if best is None:
                raise last_error or RuntimeError("MiMo V2.5 唱歌全部候选均失败")
            return best

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
