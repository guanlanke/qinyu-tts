"""语音语气：从 AstrBot 人设里提取"声音该是什么样"，拼成 MiMo 的 user 指令。

设计约束：
- 语气是插件内部行为，不对外暴露文案，用户只控制开关；
- 必须跟随用户自己配置的人设，而不是写死一种腔调；
- 指令必须短：写成"管理条款"会让人去执行规则，反而不自然，所以只给一句风格提示；
- **不能把语速相关描述传进去**：「慵懒/慢条斯理」会让人设把朗读拖慢，而人设里的
  "慵懒"通常指行为懒散，不是说话方式。

本模块只做纯字符串处理，不依赖 httpx / AstrBot，便于单独测试。
"""

from __future__ import annotations

import re

#: 可直接作为"声音气质"的短词表。按出现位置取用，不整句照搬——
#: 整句塞进指令会让语气变成复读人设，反而不自然
_TRAIT_WORDS = (
    # 明快跳脱一组（用户人设最常见）
    "轻快",
    "跳脱",
    "顽皮",
    "贪玩",
    "调皮",
    "好动",
    "鬼马",
    "机灵",
    "活泼",
    "开朗",
    "元气",
    "明快",
    "爽朗",
    "爽脆",
    "干脆",
    "利落",
    "俏皮",
    "热情",
    "灵动",
    # 柔和一组
    "温柔",
    "甜美",
    "清亮",
    "软糯",
    "娇气",
    "撒娇",
    "粘人",
    "害羞",
    "乖巧",
    "安静",
    "呆萌",
    "可爱",
    # 音色与性格
    "磁性",
    "醇厚",
    "低沉",
    "深沉",
    "高冷",
    "清冷",
    "傲娇",
    "毒舌",
    "直率",
    "稳重",
    "成熟",
    "冷静",
    "理智",
)

#: 音色描述里的中性词：不代表气质，提取了反而让指令含糊
_NEUTRAL_WORDS = ("中等", "一般", "普通", "正常", "标准", "适中", "平常")

#: 会把语速拖慢的词：人设里有也**不传给 TTS**，否则朗读会变慢变沉
_SLOW_WORDS = (
    "慵懒",
    "懒散",
    "松散",
    "缓慢",
    "慢条斯理",
    "温吞",
    "低缓",
    "拖沓",
    "不紧不慢",
    "有气无力",
)

#: 会把「说话不流畅」直接教给 TTS 的描述：命中整句丢弃，否则真会读成结巴
_FLUENCY_NOISE = (
    "结巴",
    "口吃",
    "支支吾吾",
    "含糊",
    "口齿",
    "吞音",
    "咬字不清",
    "重复",
    "停顿",
    "欲言又止",
    "吞吞吐吐",
    "口音重",
)

#: 与声音无关、必须丢弃的内容（格式要求、工具说明、禁令等）
_NOISE_PATTERNS = (
    "markdown",
    "json",
    "格式",
    "输出",
    "代码",
    "工具",
    "函数",
    "调用",
    "禁止",
    "不要",
    "必须",
    "规则",
    "指令",
    "system",
    "prompt",
    "http",
    "api",
    "token",
    "技能",
    "插件",
    "数据库",
    "标签",
    "字段",
    "列表",
    "步骤",
)

#: 句子切分：中英文句末标点与换行
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
_WHITESPACE = re.compile(r"\s+")

#: 送给模型的角色气质长度上限与词数上限：越短越像风格提示
_MAX_TRAIT_CHARS = 12
_MAX_TRAIT_WORDS = 2


def _has_noise(text: str) -> bool:
    lowered = text.lower()
    if any(noise in lowered for noise in _NOISE_PATTERNS):
        return True
    return any(noise in text for noise in _FLUENCY_NOISE)


def extract_persona_traits(
    persona_text: str | None,
    max_chars: int = _MAX_TRAIT_CHARS,
    max_words: int = _MAX_TRAIT_WORDS,
) -> str:
    """从人设里挑出 1-2 个声音气质词，如「活泼、开朗」。

    只取气质词、不照搬整句；含格式/工具/口吃类描述的句子整句跳过；
    慢速词（慵懒等）一律不取，避免把朗读拖慢。挑不出时返回空串，
    调用方回退到通用自然语气。
    """
    raw = (persona_text or "").strip()
    if not raw:
        return ""

    usable: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(raw):
        cleaned = _WHITESPACE.sub("", sentence)
        if len(cleaned) < 2 or _has_noise(cleaned):
            continue
        usable.append(cleaned)
    if not usable:
        return ""

    joined = "\n".join(usable)
    found: list[tuple[int, str]] = []
    for word in _TRAIT_WORDS:
        if word in _SLOW_WORDS:
            continue
        position = joined.find(word)
        if position >= 0:
            found.append((position, word))
    found.sort()

    words: list[str] = []
    for _, word in found:
        if word in words:
            continue
        if any(word in neutral for neutral in _NEUTRAL_WORDS):
            continue
        # 「傲娇」与「娇气」这类同源词只留一个，避免堆叠
        if any(word in existing or existing in word for existing in words):
            continue
        words.append(word)
        if len(words) >= max_words:
            break

    if not words:
        words = _fallback_traits(joined, max_words)
    return "、".join(words)[:max_chars].rstrip("、")


#: 「说话轻快」这类结构：去掉前缀，取后面的气质短语
_SPEECH_PREFIX = re.compile(r"(?:说话|语气|声音|嗓音|口吻|性格|脾气)[：:，,、]?\s*")

#: 兜底短语里不该出现的字
_FALLBACK_STOP = ("的", "了", "是", "有", "和", "与", "很", "比较", "稍微", "一点")


def _fallback_traits(text: str, max_words: int) -> list[str]:
    """词表没命中时，从「说话轻快」这类结构里截一个 2-4 字短语。

    人设写法千变万化，词表不可能穷举；这一层保证「轻快」「跳脱」这种
    没进词表的描述也能被用上。
    """
    result: list[str] = []
    for match in _SPEECH_PREFIX.finditer(text):
        rest = text[match.end() : match.end() + 8]
        candidate = re.split(r"[，,。；;、\s]", rest)[0].strip()
        if not (2 <= len(candidate) <= 4):
            continue
        if any(stop in candidate for stop in _FALLBACK_STOP):
            continue
        if any(neutral in candidate for neutral in _NEUTRAL_WORDS):
            continue
        if any(slow in candidate for slow in _SLOW_WORDS):
            continue
        if candidate not in result:
            result.append(candidate)
        if len(result) >= max_words:
            break
    return result


#: 不依赖人设时的通用自然语气。刻意短、不列禁止项
NATURAL_SPEECH_GUIDANCE = "像真人随口说话，语气自然、有起伏，语速正常"

#: 明快类气质：命中就把语速往快推，不然「轻快」和「语速正常」互相打架
_FAST_TRAITS = (
    "轻快",
    "跳脱",
    "顽皮",
    "贪玩",
    "调皮",
    "好动",
    "鬼马",
    "活泼",
    "开朗",
    "元气",
    "明快",
    "爽朗",
    "爽脆",
    "干脆",
    "利落",
    "俏皮",
    "灵动",
)


def build_speech_tone(persona_traits: str = "") -> str:
    """朗读用语气指令：一句话，像风格提示而不是任务要求。

    「有起伏」治平铺直叙像播报；语速按气质走——明快类推快，
    其余用正常语速，对冲人设里拖慢语速的描述。
    """
    traits = (persona_traits or "").strip()
    if not traits:
        return NATURAL_SPEECH_GUIDANCE
    speed = "语速稍快" if any(word in traits for word in _FAST_TRAITS) else "语速正常"
    return f"说话{traits}，语气自然、有起伏，{speed}"
