"""MiniMax 引擎：朗读走 t2a_v2，唱歌走 music_generation。

两套完全不同的接口，所以这个引擎把「说话」和「唱歌」拆成两条独立链路：

  朗读  POST /v1/t2a_v2            文本 → 语音（支持情绪、音色、语速）
  唱歌  POST /v1/music_generation  歌词 + 风格描述 → 带人声的歌曲

唱歌时 MiniMax 不接受「把一句歌词念出来」这种思路，它要的是：
  prompt   音乐风格描述（曲风、情绪、场景）
  lyrics   带段落标记的歌词（[verse] / [chorus] / [bridge]）

这跟 MiMo 的 `(唱歌)歌词` 是两种范式，所以两条链路各自构造 payload，互不干扰。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any

import httpx

from ..base import TTSEngine

logger = logging.getLogger(__name__)

#: 界面显示的中文音色名 → MiniMax voice_id。
#: 配置里存中文，请求前换成 ID；直接填 voice_id 也认（兼容控制台里的其他音色）。
MINIMAX_VOICE_MAP: dict[str, str] = {
    "少女（清亮，推荐）": "female-shaonv",
    "御姐（成熟磁性）": "female-yujie",
    "成熟女性（沉稳）": "female-chengshu",
    "甜美女性（软糯）": "female-tianmei",
    "青涩青年（男·少年感）": "male-qn-qingse",
    "精英青年（男·干练）": "male-qn-jingying",
    "霸道青年（男·低音）": "male-qn-badao",
    "青年大学生（男·自然）": "male-qn-daxuesheng",
    "男性主持人（播音腔）": "presenter_male",
    "女性主持人（播音腔）": "presenter_female",
    "男性有声书1（叙述感）": "audiobook_male_1",
    "女性有声书1（叙述感）": "audiobook_female_1",
    "聪明男童": "clever_boy",
    "可爱男童": "cute_boy",
    "萌萌女童": "lovely_girl",
    "病娇弟弟": "bingjiao_didi",
    "俊朗男友": "junlang_nanyou",
    "纯真学弟": "chunzhen_xuedi",
    "冷淡学长": "lengdan_xiongzhang",
    "霸道少爷": "badao_shaoye",
    "甜心小玲": "tianxin_xiaoling",
    "俏皮萌妹": "qiaopi_mengmei",
    "妩媚御姐": "wumei_yujie",
    "嗲嗲学妹": "diadia_xuemei",
    "淡雅学姐": "danya_xuejie",
}

#: 反向表，用于日志和状态显示
MINIMAX_VOICE_LABELS: dict[str, str] = {v: k for k, v in MINIMAX_VOICE_MAP.items()}

#: 兼容旧配置：之前可能存的是英文 ID
MINIMAX_VOICES = MINIMAX_VOICE_MAP


def resolve_voice_id(value: str) -> str:
    """把中文音色名换成 voice_id；已经是 ID 的原样返回。"""
    raw = str(value or "").strip()
    if not raw:
        return "female-shaonv"
    return MINIMAX_VOICE_MAP.get(raw, raw)


#: 界面显示的中文情绪 → MiniMax emotion 枚举
MINIMAX_EMOTION_MAP: dict[str, str] = {
    "不设置": "",
    "开心": "happy",
    "悲伤": "sad",
    "愤怒": "angry",
    "害怕": "fearful",
    "厌恶": "disgusted",
    "惊讶": "surprised",
    "平静": "neutral",
}

#: 兼容旧配置里的英文写法
MINIMAX_EMOTION_ALIASES: dict[str, str] = {
    "happy": "happy",
    "sad": "sad",
    "angry": "angry",
    "fearful": "fearful",
    "disgusted": "disgusted",
    "surprised": "surprised",
    "neutral": "neutral",
}

MINIMAX_EMOTIONS = tuple(MINIMAX_EMOTION_MAP.keys())


def resolve_emotion(value: str) -> str:
    """把中文情绪换成枚举；英文或空值也认。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw in MINIMAX_EMOTION_MAP:
        return MINIMAX_EMOTION_MAP[raw]
    return MINIMAX_EMOTION_ALIASES.get(raw.lower(), "")


#: 唱歌用的音乐模型
DEFAULT_MUSIC_MODEL = "music-3.0"

#: 支持的音频容器
MINIMAX_FORMATS = ("mp3", "wav", "flac", "pcm")

#: 段落标记：(verse)(chorus)(bridge) —— MiniMax 靠它理解歌曲结构
_SEGMENT_TAG = re.compile(r"\[(?:verse|chorus|bridge|intro|outro|hook)\s*\d*\]", re.I)


class MiniMaxEngine(TTSEngine):
    """MiniMax TTS + 音乐生成。

    朗读与唱歌走两个不同接口，因此音色、格式、超时都分开配置。
    """

    SUPPORTS_SINGING = True
    SINGING_UNSUPPORTED_REASON = ""

    def __init__(self, config: dict, plugin_config: dict | None = None):
        super().__init__(config, plugin_config or {})
        self.instance_name: str = str(config.get("instance_name") or "minimax")

        self.api_key: str = str(config.get("api_key") or "").strip()
        self.api_base: str = (
            str(config.get("api_base") or "https://api.minimax.chat").strip().rstrip("/")
        )
        # 国际站是 api.minimax.io，国内站是 api.minimax.chat，两者路径一致
        self.model: str = str(config.get("model") or "speech-02-hd").strip()
        # 配置里存的是中文音色名，这里换成 voice_id
        self.voice_label: str = str(config.get("voice") or "").strip()
        self.voice: str = resolve_voice_id(self.voice_label)
        self.emotion_label: str = str(config.get("emotion") or "不设置").strip()
        self.emotion: str = resolve_emotion(self.emotion_label)
        self.format: str = str(config.get("format") or "mp3").strip().lower()
        self.speed: float = float(config.get("speed") or 1.0)
        self.vol: float = float(config.get("vol") or 1.0)
        self.pitch: int = int(config.get("pitch") or 0)
        self.timeout: int = int(config.get("timeout") or 60)

        # ===== 唱歌专用配置 =====
        self.music_model: str = str(config.get("music_model") or DEFAULT_MUSIC_MODEL).strip()
        self.music_prompt: str = str(
            config.get("music_prompt") or "华语流行，抒情，女声，中速，温暖"
        ).strip()
        self.music_format: str = str(config.get("music_format") or "mp3").strip().lower()
        self.music_sample_rate: int = int(config.get("music_sample_rate") or 44100)
        self.music_bitrate: int = int(config.get("music_bitrate") or 256000)
        self.music_timeout: int = int(config.get("music_timeout") or 180)

        # 纯语音链路的可选语气（插件注入）
        self.voice_tone: str = ""

        if not self.api_key:
            raise ValueError("MiniMax TTS: api_key 不能为空")

        self._client: httpx.AsyncClient | None = None

    # ---------------------------------------------------------------- 客户端

    def _get_client(self, singing: bool = False) -> httpx.AsyncClient:
        timeout = self.music_timeout if singing else self.timeout
        if self._client is None or self._client.timeout != httpx.Timeout(timeout):
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ---------------------------------------------------------------- 朗读

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """文本转语音：POST /v1/t2a_v2。"""
        body = str(text or "").strip()
        if not body:
            raise ValueError("MiniMax TTS: 待合成文本为空")

        # 语气属于提示性描述，MiniMax 没有对应字段，所以只在没有显式 emotion 时
        # 用它做一次情绪推断——绝不拼接进正文，避免被念出来。
        emotion = self.emotion
        if not emotion and self.voice_tone:
            emotion = self._guess_emotion(self.voice_tone)

        voice_setting: dict[str, Any] = {
            "voice_id": self.voice,
            "speed": self.speed,
            "vol": self.vol,
            "pitch": self.pitch,
        }

        payload: dict[str, Any] = {
            "model": self.model,
            "text": body,
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": self.format,
                "channel": 1,
            },
        }
        if emotion:
            payload["emotion"] = emotion

        url = f"{self.api_base}/v1/t2a_v2"
        resp = await self._get_client().post(url, headers=self._headers(), json=payload)
        data = self._check(resp, "朗读")

        audio = self._extract_audio(data, "朗读")
        logger.info(
            "[MiniMax] 朗读完成：%s 字节，音色=%s(%s) 情绪=%s",
            len(audio),
            self.voice_label or MINIMAX_VOICE_LABELS.get(self.voice, self.voice),
            self.voice,
            emotion or "默认",
        )
        return audio, self.format

    @staticmethod
    def _guess_emotion(tone: str) -> str:
        """把语气描述粗略映射到 MiniMax 的情绪枚举。"""
        table = (
            (("开心", "轻快", "活泼", "兴奋", "俏皮"), "happy"),
            (("悲伤", "低落", "难过", "失落"), "sad"),
            (("生气", "愤怒", "不满"), "angry"),
            (("害怕", "紧张", "不安"), "fearful"),
            (("惊讶", "震惊"), "surprised"),
            (("厌恶", "嫌弃"), "disgusted"),
        )
        for words, emotion in table:
            if any(word in tone for word in words):
                return emotion
        return ""

    # ---------------------------------------------------------------- 唱歌

    async def synthesize_singing(self, lyrics: str) -> tuple[bytes, str]:
        """唱歌：POST /v1/music_generation。

        MiniMax 不接受「念歌词」，它要 prompt（曲风）+ lyrics（带段落标记）。
        所以这里把纯歌词补成合法结构，再带上风格描述。
        """
        body = str(lyrics or "").strip()
        if not body:
            raise ValueError("MiniMax 唱歌: 歌词为空")

        structured = self._structure_lyrics(body)
        payload: dict[str, Any] = {
            "model": self.music_model,
            "prompt": self.music_prompt or "华语流行，抒情，女声，中速",
            "lyrics": structured,
            "audio_setting": {
                "sample_rate": self.music_sample_rate,
                "bitrate": self.music_bitrate,
                "format": self.music_format,
            },
        }

        url = f"{self.api_base}/v1/music_generation"
        logger.info(
            "[MiniMax] 唱歌请求：模型=%s 风格=%s 歌词=%s 字",
            self.music_model,
            payload["prompt"],
            len(re.sub(r"\s", "", structured)),
        )
        resp = await self._get_client(singing=True).post(
            url, headers=self._headers(), json=payload
        )
        data = self._check(resp, "唱歌")
        audio = self._extract_audio(data, "唱歌")
        logger.info("[MiniMax] 唱歌完成：%s 字节", len(audio))
        return audio, self.music_format

    @staticmethod
    def _structure_lyrics(text: str) -> str:
        """把纯歌词补成 MiniMax 认识的段落结构。

        已经有 [verse] / [chorus] 标记的原样保留；没有的话按空行分段，
        依次标成 verse / chorus / bridge / outro。
        """
        raw = str(text or "").strip()
        if _SEGMENT_TAG.search(raw):
            return raw

        blocks = [b.strip() for b in re.split(r"\n\s*\n", raw) if b.strip()]
        if not blocks:
            blocks = [raw]

        order = ("verse", "chorus", "bridge", "outro")
        lines: list[str] = []
        for index, block in enumerate(blocks):
            tag = order[min(index, len(order) - 1)]
            lines.append(f"[{tag}]")
            lines.append(block)
        return "\n".join(lines)

    # ---------------------------------------------------------------- 公共

    @staticmethod
    def _check(resp: httpx.Response, stage: str) -> dict:
        """校验 HTTP 状态与 MiniMax 的业务错误码。"""
        if resp.status_code >= 400:
            detail = resp.text[:300]
            hint = ""
            if "music" in detail.lower() or "not available" in detail.lower():
                hint = (
                    "（提示：MiniMax 音乐生成接口自 2026-08-20 起不再对新用户开放，"
                    "只有历史付费用户可用；也可改用开源 MiniMax-Music3 本地部署）"
                )
            raise RuntimeError(f"MiniMax {stage}失败 HTTP {resp.status_code}: {detail}{hint}")

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"MiniMax {stage}返回非 JSON: {resp.text[:200]}") from exc

        base_resp = data.get("base_resp") or {}
        code = base_resp.get("status_code", 0)
        if code not in (0, None):
            msg = base_resp.get("status_msg") or ""
            raise RuntimeError(f"MiniMax {stage}失败 [{code}]: {msg}")
        return data

    @staticmethod
    def _extract_audio(data: dict, stage: str) -> bytes:
        """从响应里取音频字节。

        MiniMax 各接口的返回形态不完全一致，这里按可能性依次尝试：
        1. data.audio 是 hex 字符串（t2a_v2 的标准形态）
        2. data.audio 是 base64
        3. data.audio / audio_file / url 是可下载的 http 地址
        """
        payload = data.get("data") or {}
        raw = payload.get("audio") or payload.get("audio_file") or data.get("audio")

        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)

        if isinstance(raw, str) and raw:
            text = raw.strip()
            if text.startswith("http://") or text.startswith("https://"):
                raise RuntimeError(
                    f"MiniMax {stage}返回的是音频链接而不是音频数据：{text[:120]}\n"
                    "该形态需要额外下载步骤，请把这条信息反馈给插件作者。"
                )
            # hex 优先：MiniMax 的 t2a_v2 返回十六进制
            try:
                if len(text) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", text):
                    return bytes.fromhex(text)
            except (ValueError, binascii.Error):
                pass
            try:
                return base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError):
                pass

        # 兜底：把响应整个打出来，方便定位实际格式
        keys = list(data.keys())
        raise RuntimeError(
            f"MiniMax {stage}: 响应里没有可识别的音频字段（顶层键：{keys}）"
        )
