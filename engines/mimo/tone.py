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
    # 明快跳脱
    "轻快", "跳脱", "顽皮", "贪玩", "调皮", "好动", "鬼马", "机灵",
    "活泼", "开朗", "元气", "明快", "爽朗", "爽脆", "干脆", "利落",
    "俏皮", "热情", "灵动", "欢脱", "逗趣", "幽默", "风趣", "诙谐",
    # 柔和温软
    "温柔", "甜美", "清亮", "软糯", "娇气", "撒娇", "粘人", "害羞",
    "乖巧", "安静", "呆萌", "可爱", "软萌", "温婉", "体贴", "细心",
    "耐心", "治愈", "亲切", "和煦",
    # 低沉稳重
    "磁性", "醇厚", "低沉", "深沉", "稳重", "成熟", "冷静", "理智",
    "可靠", "从容", "笃定", "厚重", "威严", "庄重", "克制",
    # 冷冽锋利
    "高冷", "清冷", "傲娇", "毒舌", "直率", "凌厉", "果断", "干练",
    "利索", "疏离", "冷淡", "淡漠", "锐利", "犀利", "冷静自持",
    # 书卷文气
    "文雅", "儒雅", "斯文", "书卷", "文气", "博学", "从容不迫",
    "引经据典", "慢条斯理但字字清楚",
    # 江湖市井
    "豪爽", "飒爽", "痞气", "江湖气", "直爽", "仗义", "爽快",
    "热络", "市井", "烟火气",
    # 稚气少年
    "稚气", "奶气", "少年感", "少女感", "青涩", "羞怯", "雀跃",
    "冒失", "憨", "纯粹",
    # 特殊人格
    "病娇", "腹黑", "阴郁", "厌世", "慵懒", "散漫", "漫不经心",
    "机械", "无机质", "淡漠疏离", "傲岸", "倨傲",
    # 叙事腔
    "播音腔", "叙述感", "讲述感", "娓娓道来", "抑扬顿挫", "字正腔圆",
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
    "轻快", "跳脱", "顽皮", "贪玩", "调皮", "好动", "鬼马", "机灵",
    "活泼", "开朗", "元气", "明快", "爽朗", "爽脆", "干脆", "利落",
    "俏皮", "灵动", "欢脱", "逗趣", "幽默", "风趣", "诙谐", "热情",
    "雀跃", "冒失", "憨", "直爽", "爽快", "热络",
)

#: 拖慢类：命中就把语速往慢推，否则「慵懒」配「语速正常」听着别扭
_SLOW_TRAITS = (
    "慵懒", "散漫", "漫不经心", "低沉", "深沉", "庄重", "威严",
    "娓娓道来", "叙述感", "克制", "从容", "稳重", "阴郁", "厌世",
    "高冷", "清冷", "寡言", "冷淡", "淡漠", "疏离", "机械", "无机质",
)

#: 气声/轻声类：这类角色不该用「有起伏」的全开音量
_SOFT_TRAITS = (
    "温柔", "软糯", "娇气", "撒娇", "害羞", "安静", "气声", "轻声",
    "治愈", "亲切", "温婉", "体贴", "奶气", "稚气", "羞怯",
)


def _content_mood(text: str) -> str:
    """按回复内容本身判断该用什么语气。

    这是「没有人设」或「人设里读不出声音特征」时的兜底：
    说话的意图本来就写在句子里——问句要问的样子，安慰要安慰的样子。

    只看显性信号，不猜语义，避免自作聪明。
    """
    body = (text or "").strip()
    if not body:
        return ""

    # 问句：结尾是问号，或明显的询问句式
    if body.rstrip().endswith(("？", "?")) or body.startswith(
        ("怎么", "为什么", "要不要", "是不是", "能不能", "可以吗", "要不要我")
    ):
        return "带着疑问的语气，尾音微微上扬"

    # 道别
    if any(w in body for w in ("再见", "晚安", "拜拜", "下次聊", "先走了", "回头见")):
        return "语气放轻放缓，像在道别"

    # 安慰
    if any(w in body for w in ("别难过", "没事的", "会好的", "别担心", "抱抱", "我懂", "陪着你")):
        return "语气柔和，放慢一点，像在安慰人"

    # 道歉
    if any(w in body for w in ("对不起", "抱歉", "是我的错", "不好意思")):
        return "语气放低放软，带一点歉意"

    # 惊讶
    if any(w in body for w in ("竟然", "天啊", "没想到", "真的假的", "太夸张")):
        return "语气带一点惊讶，起点稍高"

    # 解释/说明
    if any(w in body for w in ("因为", "原因是", "也就是说", "简单说", "意思是")):
        return "语气清楚平稳，像在把话讲明白"

    # 长句叙述
    if len(body) > 80 and ("。" in body):
        return "自然叙述，句间有停顿，不要一口气念完"

    # 短促应答
    if len(body) <= 12:
        return "轻快随口，不要拖长音"

    return ""


def build_speech_tone(persona_traits: str = "", text: str = "") -> str:
    """朗读用语气指令：一句话，像风格提示而不是任务要求。

    优先级：
      1. 人设里有可用的声音特征 → 按特征走，语速随之调整
      2. 人设没给出特征 → 按**回复内容**推断（问句、安慰、道别各有各的说法）
      3. 都读不到 → 通用自然语气

    语速必须跟着人格走：给「慵懒」配「语速正常」，念出来是分裂的；
    给「轻快」配「语速正常」，又听不出轻快。
    """
    traits = (persona_traits or "").strip()

    if traits:
        if any(word in traits for word in _FAST_TRAITS):
            speed = "语速稍快"
        elif any(word in traits for word in _SLOW_TRAITS):
            speed = "语速稍慢"
        elif any(word in traits for word in _SOFT_TRAITS):
            speed = "语速平缓"
        else:
            speed = "语速正常"
        return f"说话{traits}，语气自然、有起伏，{speed}"

    mood = _content_mood(text)
    if mood:
        return mood

    return NATURAL_SPEECH_GUIDANCE
