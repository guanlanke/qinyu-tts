"""TTS 引擎抽象基类"""

import base64
from abc import ABC, abstractmethod
from pathlib import Path

import httpx


class TTSEngine(ABC):
    """所有 TTS 引擎的抽象基类"""

    #: 是否支持唱歌模式；只有 MiMo 的预置音色模型支持，子类按需覆盖
    SUPPORTS_SINGING: bool = False

    #: 唱歌模式不支持时的原因，用于给用户可读提示
    SINGING_UNSUPPORTED_REASON: str = "当前引擎不支持唱歌模式"

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        self.config = config
        self.plugin_config = plugin_config or {}
        self.instance_name: str = config.get("instance_name", "") or config.get("__template_key", "unnamed")

    @abstractmethod
    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """合成语音，返回 (audio_bytes, format)"""
        ...

    async def synthesize_singing(self, lyrics: str) -> tuple[bytes, str]:
        """唱歌合成，返回 (audio_bytes, format)。

        默认实现忽略唱歌意图，退化为普通语音合成；支持唱歌的引擎应覆盖此方法。
        """
        return await self.synthesize(lyrics)

    async def close(self) -> None:
        """释放资源，子类可选实现"""
        pass

    def _check_response(self, provider: str, response: httpx.Response) -> None:
        """检查 HTTP 响应，错误时抛出详细异常"""
        if response.status_code >= 400:
            detail = response.text[:300]
            raise RuntimeError(f"{provider} TTS 错误 [{response.status_code}]: {detail}")

    def _resolve_sample_path(self, sample_paths: list[str], sample_name: str = "") -> Path:
        """解析音色样本文件路径（共用逻辑）"""
        if not sample_paths:
            raise FileNotFoundError("未上传音色样本文件，请在插件配置中上传 mp3 或 wav 格式的音频文件")

        if not sample_name.strip():
            raw_path = sample_paths[0]
        else:
            wanted = sample_name.strip()
            # 精确匹配优先；其次允许省略扩展名（配置里常填 "样本名"，实际文件是 "样本名.mp3"），
            # 再次允许填的是文件主干名。否则会出现「明明传上去了却说找不到」。
            exact = next((p for p in sample_paths if Path(p).name == wanted), None)
            if exact is not None:
                raw_path = exact
            else:
                stem_matches = [
                    p for p in sample_paths if Path(p).stem == wanted
                ]
                if len(stem_matches) == 1:
                    raw_path = stem_matches[0]
                elif len(stem_matches) > 1:
                    names = ", ".join(Path(p).name for p in stem_matches)
                    raise FileNotFoundError(
                        f"'{wanted}' 对应多个样本文件（{names}），"
                        "请在「音色样本文件名」里写完整文件名（含扩展名）"
                    )
                else:
                    available = ", ".join(Path(p).name for p in sample_paths)
                    raise FileNotFoundError(
                        f"未找到名为 '{sample_name}' 的样本文件。已上传: {available}"
                        "（填文件名时可省略扩展名）"
                    )

        candidates = [
            Path(raw_path),
            Path("data") / "plugin_data" / "astrbot_plugin_qinyu" / raw_path,
            Path("data") / raw_path,
            Path.cwd() / raw_path,
        ]
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_path
            candidates.append(Path(get_astrbot_path()) / raw_path)
        except Exception:
            pass

        for c in candidates:
            if c.exists():
                return c

        raise FileNotFoundError(f"音色样本文件不存在: {raw_path}，文件可能已被删除，请重新上传。")

    def _encode_sample_base64(self, sample_path: Path, with_prefix: bool = True) -> str:
        """将音色样本编码为 base64"""
        audio_bytes = sample_path.read_bytes()
        encoded = base64.b64encode(audio_bytes).decode("utf-8")
        if not with_prefix:
            return encoded
        mime_map = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}
        mime_type = mime_map.get(sample_path.suffix.lower(), "application/octet-stream")
        return f"data:{mime_type};base64,{encoded}"
