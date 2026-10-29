"""MiMo V2.5 TTS VoiceClone 引擎"""

import base64
import logging
from pathlib import Path

import httpx

from ..base import TTSEngine

logger = logging.getLogger(__name__)


class MiMoV25CloneEngine(TTSEngine):
    """小米 MiMo-V2.5-TTS-VoiceClone 引擎（音频样本克隆音色）

    官方限制：voiceclone 模型不支持唱歌模式，唱歌请求会退回普通语音合成。
    """

    SUPPORTS_SINGING = False
    SINGING_UNSUPPORTED_REASON = "MiMo V2.5-VoiceClone 不支持唱歌模式，请切换到 [MiMo] V2.5-TTS（预置音色）"

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", "https://api.xiaomimimo.com/v1")
        self.model: str = config.get("model", "mimo-v2.5-tts-voiceclone")
        self.audio_format: str = config.get("format", "wav")
        self.style_prompt: str = config.get("style_prompt", "")
        # VoiceClone 每次都要把样本转成 base64 一起发上去，比预置音色慢得多。
        # 60 秒容易踩线超时，默认给 120 秒；超时后由主程序降级成文字，不换音色。
        self.timeout: int = int(config.get("timeout") or 120)
        self._client: httpx.AsyncClient | None = None
        self._voice_base64: str | None = None
        self._sample_paths: list[str] = self.plugin_config.get("voice_clone_samples", [])
        self._sample_name: str = config.get("voice_sample_name", "")

        # 参数校验
        if not self.api_key:
            raise ValueError("MiMo V2.5 VoiceClone TTS: api_key 不能为空")

        # 没有样本就没法克隆。这里提前拦下来，报错里直接写清下一步，
        # 而不是等到第一次合成才抛 FileNotFoundError。
        if not self._sample_paths:
            raise ValueError(
                "VoiceClone 需要音色样本：到配置页「音色克隆样本」上传一段 10~30 秒的"
                "单人清晰人声（mp3 / wav / m4a），再重载插件"
            )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _get_voice_base64(self) -> str:
        if self._voice_base64 is None:
            path = self._resolve_sample_path(self._sample_paths, self._sample_name)
            self._voice_base64 = self._encode_sample_base64(path, with_prefix=True)
        return self._voice_base64

    def list_samples(self) -> list[str]:
        """已上传的样本文件名列表。"""
        return [Path(p).name for p in self._sample_paths if p]

    def describe_sample(self) -> str:
        """当前生效的样本：填了名字就是它，没填就是第一个。"""
        if self._sample_name.strip():
            return self._sample_name.strip()
        if self._sample_paths:
            return f"{Path(self._sample_paths[0]).name}（默认取第一个）"
        return "（未上传样本）"

    def switch_sample(self, name: str) -> str:
        """切换样本并清掉缓存，返回实际生效的文件名。"""
        path = self._resolve_sample_path(self._sample_paths, name)
        self._sample_name = Path(path).name
        self._voice_base64 = None  # 换样本必须重编码，否则还在用旧声音
        return Path(path).name

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        client = self._get_client()
        logger.info(
            "[%s] 克隆朗读：样本=%s 字数=%s",
            self.instance_name,
            self.describe_sample(),
            len(text or ""),
        )
        url = f"{self.api_base.rstrip('/')}/chat/completions"

        messages = []
        # style_prompt 为空时不要发 content="" 的 user 消息，部分网关会直接 400
        style_prompt = (self.style_prompt or "").strip()
        if style_prompt:
            messages.append({"role": "user", "content": style_prompt})
        messages.append({"role": "assistant", "content": text})

        payload = {
            "model": self.model,
            "messages": messages,
            "audio": {"format": self.audio_format, "voice": self._get_voice_base64()},
        }

        resp = await client.post(
            url, headers={"api-key": self.api_key, "Content-Type": "application/json"}, json=payload
        )
        self._check_response("MiMo V2.5 VoiceClone", resp)

        data = resp.json()
        audio_data = data["choices"][0]["message"]["audio"]["data"]
        return base64.b64decode(audio_data), self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
