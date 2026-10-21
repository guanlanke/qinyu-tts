# engines/ · 引擎层

这里放的是「怎么跟不同厂商的 TTS 服务说话」。每个提供商一个子包，对外暴露同一套接口。

## 接口契约

所有引擎都继承 `base.py` 里的 `TTSEngine`：

```python
class TTSEngine:
    SUPPORTS_SINGING: bool = False          # 是否支持唱歌
    SINGING_UNSUPPORTED_REASON: str = ""    # 不支持时的原因，用于诊断提示

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """返回 (音频字节, 容器格式)"""
    async def close(self) -> None:
        """释放连接池"""
```

主程序只调这两个方法。唱歌走 `synthesize_singing()`，只有 `SUPPORTS_SINGING = True` 的引擎会实现它。

## 目录

| 目录 | 提供商 | 唱歌 | 备注 |
|---|---|---|---|
| `mimo/` | 小米 MiMo | ✅ V2.5 完整支持 | 本项目的重点，唱歌链路全在这里 |
| `openai/` | OpenAI 兼容 | ❌ | `/v1/audio/speech` 标准接口 |
| `volcengine/` | 火山引擎 SAMI | ❌ | 字节跳动，发音人丰富 |
| `aliyun/` | 阿里云百炼 | ❌ | CosyVoice，DashScope 兼容 |
| `azure/` | Azure | ❌ | SSML + 情感风格 + 角色扮演 |
| `elevenlabs/` | ElevenLabs | ❌ | 多语言高保真 |
| `custom_http/` | 任意 HTTP API | ❌ | 模板化：URL / 请求体 / 响应路径都可配 |

## mimo/ 是唯一有唱歌的

它比其他引擎复杂得多，所以拆成多个文件：

| 文件 | 职责 |
|---|---|
| `v2_5.py` | V2.5 主引擎。朗读和唱歌两条链路的 payload 构造都在这里 |
| `v2.py` | V2 引擎，用 `<style>` 标签体系，是 V2.5 的前代 |
| `singing.py` | **唱歌核心**。歌词清洗、标签拼装、切片、音色解析、指令构造、风格聚合 |
| `tone.py` | 语气层。从人设提炼语气特征，生成朗读指导 |
| `v2_5_design.py` | VoiceDesign：用文本描述凭空生成音色 |
| `v2_5_clone.py` | VoiceClone：用音频样本克隆音色 |

`singing.py` 的 832 行是所有改动最密集的地方——歌词怎么洗、什么该删什么该留、呼吸点在哪、标签怎么拼，全在这里。改唱歌行为先读它。

## 加一个新引擎

1. 新建 `engines/yourvendor/`，放 `__init__.py` 和 `engine.py`
2. 继承 `TTSEngine`，实现 `synthesize()`
3. 如果需要唱歌，设 `SUPPORTS_SINGING = True` 并实现 `synthesize_singing()`
4. 在 `engines/__init__.py` 的 `ENGINE_REGISTRY` 里登记一行
5. 在 `_conf_schema.json` 的模板列表里加一项配置结构

`main.py` 不用改——它按注册表找引擎，按 `SUPPORTS_SINGING` 决定要不要显示唱歌配置。

## 两个容易踩的点

**音色 ID 必须精确**。MiMo 预置音色模型只认官方 Voice ID（冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean），写错了后端会静默回退成默认音色，听起来像"选了没生效"。

**语言要匹配**。英文音色（Mia / Chloe / Milo / Dean）读中文尚可，但**唱中文歌会带明显外国口音**。所以唱歌链路有 `resolve_singing_voice()`，检测到英文音色会自动换成中文音色。
