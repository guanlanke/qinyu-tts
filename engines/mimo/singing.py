"""MiMo TTS 唱歌模式公共工具。

依据 MiMo-V2.5-TTS 官方文档（语音合成 / 音频标签控制）：
1. 唱歌标签必须位于 assistant message content 的**最开头**，格式 `(唱歌)歌词`；
2. 标签标识等效取值：`唱歌` / `sing` / `singing`，效果一致；
3. 歌词必须完整，残缺歌词会导致跑调、效果差；
4. 只有 `mimo-v2.5-tts`（预置音色）支持唱歌，
   `mimo-v2.5-tts-voicedesign` 与 `mimo-v2.5-tts-voiceclone` 不支持。

本模块只做纯字符串处理，不依赖 httpx / AstrBot，便于单独测试。
"""

from __future__ import annotations

import random
import re
from typing import Sequence

#: 官方推荐的标准唱歌标签
SINGING_CANONICAL_TAG = "唱歌"

#: 官方认可的等效标签取值（判断时按小写比对）
SINGING_ALIASES = ("唱歌", "唱", "sing", "singing", "song")

#: 含「唱」但不是唱歌的复合词。单字子串匹配会把它们全判成唱歌，
#: 导致「说唱」这类 style 在朗读链路被丢掉、普通回复被当唱歌处理
NOT_SINGING_WORDS = ("说唱", "哼唱", "伴唱", "合唱", "对唱", "轮唱", "齐唱", "清唱")

#: 官方支持的中英文风格标签括号。刻意不含 `<>`：
#: `<>` 是 XML 标签（如 <sing>、<style>）的定界符，混进来会让空标签被误判成唱歌
_BRACKETS = (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】"))

#: V2 体系的 <style>…</style> 包裹，用户把标签整段粘进 style 字段时先剥掉
_STYLE_WRAPPER = re.compile(r"^<style>(.*?)</style>$", re.IGNORECASE | re.DOTALL)

#: 标签内的风格分隔符（官方：多个风格置于同一对括号内）
_STYLE_SEPARATORS = re.compile(r"[\s,，、;；|/]+")

#: 歌词里需要清掉的 Markdown 装饰
_MARKDOWN_DECORATION = re.compile(r"[*`_~]{1,3}")
_MARKDOWN_PREFIX = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]*|>[ \t]?|[-*+][ \t]+)", re.MULTILINE)
_INLINE_WHITESPACE = re.compile(r"[ \t\u3000]+")

#: 太短的歌词官方会跑调，低于该长度给出提示
_MIN_LYRICS_CHARS = 12

#: 歌词偏长阈值 = 硬截断预算（main.SINGING_MAX_SECONDS × 3 字/秒 = 90 × 3）。
#: 这里曾长期写成 135（45 秒时代的遗留），于是 136~270 字的正常歌词会被误报
#: 「超出部分会被截断」，把排查方向带偏。调用方现在会显式传入真实预算，
#: 这个值只作为没有配置时的兜底。
_MAX_LYRICS_CHARS = 270

#: 会让音色突变的括号标签：唱歌时默认剔除，避免出现"上句女声下句男声"
_VOICE_DIRECTION_TAGS = re.compile(
    r"[（(\[【]\s*(?:男声|女声|男音|女音|少年音|少女音|童声|童音|御姐音|大叔音|正太音|萝莉音|"
    r"夹子音|换音色|切换音色|变声|音色切换|换成[^）)\]】]{0,8}音)\s*[）)\]】]"
)

#: 给模型「换气点」与「发声方式」的标签：**必须保留**。
#: 它们不是舞台动作，而是官方支持的演唱控制信号——
#: 删掉（换气）（停顿）会让长句一口气顶下来、后半段挤成一团；
#: 删掉（气声）（轻声）会让全曲一个力度、像带调朗读。
_KEEP_SINGING_TAGS = (
    "停顿",
    "换气",
    "吸气",
    "呼气",
    "深呼吸",
    "屏息",
    "气声",
    "轻声",
    "低语",
    "耳语",
    "拖长",
    "渐弱",
    "渐强",
)

#: 会让模型「演」出来、而不是唱出来的表情标签：这些才剔除
_DROP_SINGING_TAGS = (
    "叹气",
    "长叹一口气",
    "轻叹",
    "叹一口气",
    "喘息",
    "哽咽",
    "抽泣",
    "呜咽",
    "嚎啕大哭",
    "哭",
    "笑",
    "笑声",
    "轻笑",
    "大笑",
    "冷笑",
    "苦笑",
    "咳嗽",
    "打哈欠",
    "哈欠",
    "颤抖",
    "声音颤抖",
    "变调",
    "破音",
    "紧张",
    "害怕",
    "激动",
    "疲惫",
    "委屈",
    "撒娇",
    "心虚",
    "震惊",
    "不耐烦",
)

#: 所有「演唱控制标签」（保留类 + 剔除类）。用于判断歌词里是否还剩真内容：
#: （换气）现在会被保留，所以只检查 `text.strip()` 已经拦不住空歌词了
_MUSIC_TAGS = re.compile(
    r"[（(\[【]\s*(?:"
    + "|".join(
        sorted(set(_KEEP_SINGING_TAGS) | set(_DROP_SINGING_TAGS), key=len, reverse=True)
    )
    + r")\s*[）)\]】]"
)


def has_lyric_content(text: str | None) -> bool:
    """剥掉所有演唱控制标签后，是否还剩下真的歌词。"""
    return bool(_MUSIC_TAGS.sub("", text or "").strip())


#: 只剔除情绪表演类；呼吸与发声方式留给模型用
_ACTION_TAGS = re.compile(
    r"[（(\[【]\s*(?:"
    + "|".join(sorted(_DROP_SINGING_TAGS, key=len, reverse=True))
    + r")\s*[）)\]】]"
)

#: 句子的自然断点，切片只在断点处发生，避免半句被截断
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；，、,.!?;\n])")

#: 猜测歌词时判定为「寒暄」的行
_CHITCHAT_LINE = re.compile(
    r"^\s*(?:好的|好呀|好啊|来吧|来听|这就|马上|我给你|给你唱|听好|唱一|唱首|唱两句|那|嗯|OK|ok|Okay|Sure|Here)"
)

def _re_flat(text: str) -> str:
    """去掉所有空白，用于按「可见字数」判断长度。"""
    return re.sub(r"\s+", "", text or "")


#: 猜歌词时只认这些引号。**刻意不含书名号《》**：
#: 「我给你唱《雨爱》」里的书名号装的是歌名，取出来只有 2 个字，
#: 会被当成「歌词」唱 1 秒。歌名不是歌词。
_QUOTE_PAIRS = (("「", "」"), ("“", "”"), ("『", "』"), ('"', '"'))

#: 演唱指令（写在 user message 里）。
#: 官方把 user message 定位为「自然语言控制」，内容不会被朗读——这是加情绪的正确通道；
#: 情绪**绝不能**写进 `(唱歌)` 标签（v1.6.0 的教训）。
#:
#: 措辞有意回到「严格按唱歌、旋律哼唱」这一版：用户验证过那版能真唱。
#: 后来改成「把歌词完整唱出来」太软，模型退化成"带着旋律说话"（sing-song），
#: 所以这里明确要求每行都用旋律、且不许中途切回说话。
#: 演唱指令。措辞对齐参考项目（astrbot_plugin_tts_emotion_router v3.3.4）：
#: 关键是「整段连成一首歌从头唱到尾、不要漏句、不要一句一停」——
#: 早前写成「把每一行都用旋律唱出来」，模型会逐行起句，
#: 遇到跨行的长句（例如「…听清 / 雨下不停」）就在接缝处断裂跑调。
SINGING_DIRECTIVE = (
    "请严格按「唱歌、旋律哼唱」的方式生成音频：不要朗读，不要说话，不要播报说明。"
    "下一条 assistant 内容是完整歌词，请把整段歌词连成一首歌从头唱到尾，"
    "不要漏句，不要一句一停，中途不要切换成说话。"
)

#: 速度档位 -> 指令补充
SINGING_SPEED_HINTS: dict[str, str] = {
    "正常": "",
    "偏慢": "唱得慢一些，句尾可以拖长",
    "偏快": "唱得轻快一些",
}

#: 默认档位
DEFAULT_SINGING_SPEED = "正常"

#: 演唱气质：写进 user message 的自然语言描述，不进标签
SINGING_STYLE_OPTIONS = (
    "不指定",
    "温柔",
    "深情",
    "清亮",
    "磁性",
    "甜美",
    "慵懒",
    "活泼",
    "沙哑",
    # 人设腔调（官方风格维度）
    "夹子音",
    "御姐音",
    "正太音",
    "大叔音",
    "台湾腔",
    # 方言（官方风格维度）
    "东北话",
    "四川话",
    "河南话",
    "粤语",
)

#: 默认气质。刻意用「不指定」：气质会以「（整体气质：温柔）」形式追加在演唱指令后面，
#: 实测会稀释「唱」这个指令，让模型偏向「温柔地表演」。要气质再手动选
DEFAULT_SINGING_STYLE = "不指定"


def singing_directive(speed: str | None = None, style: str | None = None) -> str:
    """拼出演唱指令：主指令完整保留，气质与速度只作为末尾附注。

    刻意不让气质改写主句（"用温柔的方式演唱"会稀释"唱歌"这个指令，
    模型容易退化成 sing-song），只把它作为括号里的补充说明。
    """
    extras: list[str] = []
    style_value = (style or "").strip()
    if style_value and style_value != "不指定":
        extras.append(f"整体气质：{style_value}")
    speed_hint = SINGING_SPEED_HINTS.get((speed or "").strip(), "")
    if speed_hint:
        extras.append(speed_hint)
    if not extras:
        return SINGING_DIRECTIVE
    return f"{SINGING_DIRECTIVE}（{'；'.join(extras)}）"

#: 歌词最多保留几句（按句末标点计）。一句歌词内部可以有多个逗号短语，
#: 所以上限要放宽；切的时候永远保留整句，绝不切句内——官方明确说残缺歌词会跑调
SINGING_MAX_LINES = 10

#: 中文预置音色：唱歌只有这几个能正常唱中文
SINGING_CN_VOICES = ("冰糖", "茉莉", "苏打", "白桦")

#: 英文音色：唱中文歌词会带明显外国口音，唱歌时必须换掉
ENGLISH_VOICES = ("mia", "chloe", "milo", "dean")

#: 表示「跟随朗读音色」的占位值
FOLLOW_VOICE_VALUES = (
    "",
    "跟随预置音色",
    "跟随朗读音色",
    "跟随",
    "default",
    "same",
    # 英文别名：漏掉它们会把 "follow" 当成音色名发给接口，直接 400
    "follow",
    "auto",
    "none",
)

#: 官方 audio.format 只支持这两种
SINGING_FORMATS = ("wav", "pcm16")

#: 唱歌默认中文音色。选茉莉而不是冰糖：冰糖是活泼少女音、偏薄，
#: 用户反馈「声音孱弱」；茉莉是知性女声、厚度中等，更接近「声音中等」的预期
DEFAULT_SINGING_VOICE = "茉莉"


def resolve_singing_voice(configured: str | None, fallback: str | None = None) -> str:
    """决定唱歌音色。**唱歌必须是中文预置音色**。

    英文音色（Mia / Chloe / Milo / Dean）读中文朗读尚可，但**唱中文歌会带
    明显外国口音**——这是用户反馈「像外国人唱歌」的直接原因。
    `mimo_default` 在非中国集群也会解析成英文女声 Mia，同样要换掉。
    """
    value = (configured or "").strip()
    lowered = value.lower()
    # 大小写不敏感：`FOLLOW` / `Follow` 漏掉的话会被当音色名发给接口 → 400
    follow_set = {item.lower() for item in FOLLOW_VOICE_VALUES}
    if value and lowered not in follow_set:
        if lowered in ENGLISH_VOICES or lowered == "mimo_default":
            return DEFAULT_SINGING_VOICE
        return value
    inherited = (fallback or "").strip()
    if inherited in SINGING_CN_VOICES:
        return inherited
    return DEFAULT_SINGING_VOICE


def resolve_singing_format(configured: str | None = None, fallback: str | None = None) -> str:
    """唱歌输出格式**固定 wav**。

    `pcm16` 是裸 PCM、没有文件头，插件按 wav 播放会把采样率/位深解析错，
    听起来就是变调、怪腔怪调（甚至像外国口音）。所以唱歌一律 wav，
    不接受任何其他取值。
    """
    return "wav"


#: 歌词过短时补一句哼唱，减少整段被念出来的概率
SINGING_FILLER_LINE = "啦啦啦，轻轻唱"

#: 歌词里明显的对白/寒暄，直接删掉，避免被当成歌词唱。
#: 「唱X」只在后面紧跟分隔符或结尾时才算对白，否则会误伤「唱歌的孩子不会坏」这类正句
_SINGING_CHATTER = re.compile(
    r"(好嘞|好的|安排|来首|应景|献上|听好|听着|给你唱|我来唱|今天第几首|"
    r"门票|结一下|老铁|家人们|下面|接下来|"
    r"唱(?:首|一首|一句|歌)(?=[，,。；;：:\s]|$))"
)

#: 明显不是歌词的闲聊句，整句丢弃
_SINGING_GOSSIP = re.compile(r"(哈哈|呵呵|记得|别忘|怎么样|可以吗|要不要|不是吧|就是|这个|那个)")

#: 删掉对白词后残留的碎片（如「给你唱两句」删完只剩「两句」），不能当歌词唱
_SINGING_LEFTOVER = re.compile(r"^(?:[两一几]句|[首歌曲段来]|好听?)$")

#: 行首行尾的语气助词，留着会让演唱拖沓
_EDGE_PARTICLES = re.compile(r"^[的了啊呀呢嘛吧]+|[的了啊呀呢嘛吧]+$")


#: 【遗留，生产链路已不再调用】早期把歌词压成「歌词体」的实现。
#: 保留仅为兼容外部引用与历史测试，不要重新接回唱歌链路：
#: 它会把逗号变换行、限制行数，实测会打断旋律。清洗职责现在由
#: strip_explanation_lines / split_lyric_lines（默认关）分别承担。
LEGACY_PREPARE_LYRICS_NOTE = "legacy: 不要重新接入唱歌链路"


def prepare_lyrics(
    text: str | None,
    max_lines: int = SINGING_MAX_LINES,
    min_chars: int = 14,
    allow_filler: bool = True,
) -> str:
    """把回复压成「歌词体」，这是能不能唱起来的关键。

    模型对「歌词」的判定很依赖排版：一整段带逗号的句子会被当台词念，
    而一行一句、没有对白、没有寒暄的短句才会被唱出来。

    做法（不创作新内容，只做清洗与分行）：
    1. 去掉书名号/引号包裹、开头「唱首/来一首」这类引导语、对白词；
    2. 按标点切句，丢弃明显是闲聊的句子；
    3. **把句号、逗号、顿号统一换成换行**，强制一行一句；
    4. 去掉行首行尾的语气助词；
    5. 限制行数；内容过短且 `allow_filler` 为真时补一句哼唱。

    `allow_filler` 只对自动路径开启：用户自己用 `/tts_sing` 指定的歌词要一字不改。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    cleaned = re.sub(r"^\s*[《「『“\"']|[》」』”\"']\s*$", "", raw)
    # 开头的 (唱歌) 标签先剥掉；必须带括号，否则会把「唱歌的孩子」这类正句吃掉
    cleaned = re.sub(
        r"^\s*[（(【\[]\s*(?:唱歌|sing|singing)\s*[）)】\]]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # 去掉开头的「唱首：」「唱首歌，」这类引导语；后面必须跟分隔符，避免误伤正句
    cleaned = re.sub(r"^\s*唱[^，,。；;：:\n]{0,4}[:：，,\s]+", "", cleaned)
    cleaned = _SINGING_CHATTER.sub("", cleaned)

    parts = re.split(r"([。！？!?；;，,\n])", cleaned)
    merged: list[str] = []
    for index in range(0, len(parts), 2):
        sentence = (parts[index] or "").strip()
        punct = parts[index + 1] if index + 1 < len(parts) else ""
        if not sentence:
            continue
        if _SINGING_GOSSIP.search(sentence):
            continue
        merged.append(sentence + (punct if punct and punct != "," else ""))

    lyric = "".join(merged).strip(" ，,。；;") or cleaned.strip()
    if not lyric:
        return ""

    # 只在句末断行（。！？；），**保留句内逗号**。
    # 官方唱歌示例就是「(唱歌)原谅我这一生不羁放纵爱自由，也会怕有一天会跌倒，Oh no。」
    # ——一整段、逗号连接。把逗号也换成换行会把一条旋律拆成碎片，越切越难听。
    lyric = re.sub(r"[。！？!?；;]+", "\n", lyric)

    lines: list[str] = []
    for line in lyric.split("\n"):
        line = line.strip(" ，,。；;")
        line = _EDGE_PARTICLES.sub("", line)
        if not line or line in {"的", "了", "啊", "呀", "呢", "嘛", "吧"}:
            continue
        if _SINGING_LEFTOVER.match(line):
            continue
        lines.append(line)
    if not lines:
        return ""

    result = "\n".join(lines[:max_lines])
    if allow_filler and len(result.replace("\n", "")) < min_chars:
        result = f"{result}\n{SINGING_FILLER_LINE}"
    return result


def normalize_style(style: str | None) -> str:
    """去掉包裹括号与多余空白，得到标签内部文本。"""
    raw = (style or "").strip()
    wrapped = _STYLE_WRAPPER.match(raw)
    if wrapped:
        return wrapped.group(1).strip()
    for left, right in _BRACKETS:
        if raw.startswith(left) and raw.endswith(right) and len(raw) > len(left) + len(right):
            return raw[len(left) : -len(right)].strip()
    return raw


def is_singing_style(style: str | None) -> bool:
    """判断风格标签是否表达唱歌意图（兼容 `唱歌`、`(唱歌)`、`sing`、`singing`）。

    刻意**不用单字子串匹配**：那会把「说唱 / 哼唱 / 伴唱 / 合唱」全判成唱歌，
    进而让朗读丢掉 style、甚至把普通回复整段拿去唱。
    """
    inner = normalize_style(style).strip()
    if not inner:
        return False
    if any(word in inner for word in NOT_SINGING_WORDS):
        return False
    lowered = inner.lower()
    if lowered in SINGING_ALIASES or lowered.startswith("sing"):
        return True
    # 多风格写法 `唱歌 温柔`：逐个词精确比对
    for token in _STYLE_SEPARATORS.split(inner):
        token = token.strip().lower()
        if token and (token in SINGING_ALIASES or token.startswith("sing")):
            return True
    return False


def split_leading_singing_tag(text: str | None) -> tuple[bool, str]:
    """识别并剥离文本开头的唱歌标签，返回 (是否唱歌, 剩余文本)。"""
    raw = text or ""
    stripped = raw.lstrip()
    for left, right in _BRACKETS:
        if not stripped.startswith(left):
            continue
        end = stripped.find(right)
        if end <= len(left):
            continue
        if is_singing_style(stripped[len(left) : end]):
            return True, stripped[end + 1 :].lstrip()
    return False, raw


def has_singing_tag(text: str | None) -> bool:
    """文本是否已经自带唱歌标签。"""
    return split_leading_singing_tag(text)[0]


# ============================================================================
# 参考项目 astrbot_plugin_tts_emotion_router v3.3.4 的严格唱歌方案
# 文件来源：tts/provider_mimo.py（_prepare_strict_sing_text / _build_strict_sing_user_prompt）
# 原注释：旧实现会按词表删掉"唱/一首/就是/这个"等字样并把逗号换成换行，
# 还会把结果截断到 8 行。MiMo 官方明确说明残缺歌词会跑调、效果差，
# 所以这里改为保守清洗：保留全部歌词、全部标点、不截断、不改字。
# ============================================================================

#: 官方要求唱歌标签位于开头且独立出现；参考项目用全角括号
REFERENCE_SING_TAG = "（唱歌）"

#: 参考项目的严格唱歌 user 指令（逐字照搬）
REFERENCE_SING_USER_PROMPT = (
    "请严格按“唱歌/旋律哼唱”的方式生成音频，不要朗读，不要说话，"
    "不要播报说明。下一条 assistant 内容是完整歌词，请把整段歌词"
    "连成一首歌从头唱到尾，不要漏句，不要一句一停。"
)

#: 整行都是串场白时丢弃（照搬）
_REFERENCE_CHATTER_LINE = re.compile(
    r"^[^\n]{0,40}?(?:好嘞|好的呀?|安排|来首|来一首|应景|献上|听好|听着|"
    r"给你唱|我来唱|我唱|今天第几首|门票|老铁|家人们|下面|接下来)[^\n]{0,40}$"
)

#: 整行都是括号舞台说明时丢弃（照搬）
_REFERENCE_STAGE_LINE = re.compile(r"[（(\[][^（）()\[\]]{1,24}[）)\]]")

_REFERENCE_FALLBACK_LYRIC = "啦啦啦，轻轻唱一句小小的歌"


def _reference_strip_quotes(text: str) -> str:
    """剥掉外层成对引号/书名号（照搬参考项目）。"""
    pairs = (("《", "》"), ("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'"))
    value = (text or "").strip()
    changed = True
    while changed and value:
        changed = False
        for left, right in pairs:
            if len(value) >= 2 and value.startswith(left) and value.endswith(right):
                value = value[len(left) : -len(right)].strip()
                changed = True
    return value


def prepare_reference_sing_text(text: str | None) -> str:
    """把待唱文本整理成「歌词」：只删串场白，不删歌词本身（照搬参考项目）。

    与本地清洗路线的关键区别：
      - 不切句、不合段、不做语义判断
      - 不截断、不改标点、不改字
      - 只丢「整行都是串场白」和「整行都是括号说明」
      - 清洗过狠时回退原文，宁可留着串场白也不丢歌词
    """
    raw = (text or "").strip()
    if not raw:
        return _REFERENCE_FALLBACK_LYRIC

    cleaned = raw.replace("\r\n", "\n").replace("\r", "\n")

    # 用户/LLM 自带的唱歌标签要去掉：标签统一由外层钉在开头，留着会变成（唱歌）（唱歌）歌词
    cleaned = re.sub(r"[（(]\s*(?:唱歌|sing|singing)\s*[）)]", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"[\[【]\s*(?:唱歌|sing|singing)\s*[\]】]", "", cleaned, flags=re.I).strip()

    # 去掉开头的引导语，例如「好嘞，那我给你唱一首：」「来，唱首歌——」
    cleaned = re.sub(
        r"^\s*(?:好[的了呀]?|好嘞|行|可以|没问题|安排|来|那|那么)[^\n。！？!?]{0,24}?[：:，,]\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"^\s*(?:唱歌|唱一首|唱首歌|唱一句|唱首)\s*[：:，,、\s]*",
        "",
        cleaned,
    )
    cleaned = _reference_strip_quotes(cleaned)

    kept: list[str] = []
    for line in cleaned.split("\n"):
        value = line.strip()
        if not value:
            continue
        if _REFERENCE_STAGE_LINE.fullmatch(value):
            continue
        if _REFERENCE_CHATTER_LINE.match(value):
            continue
        kept.append(value)

    lyric = "\n".join(kept).strip() or cleaned.strip()
    # 收拾过狠时（例如整段都是「来一首……」）宁可回退原文，也不要丢掉歌词
    if len(re.sub(r"\s", "", lyric)) < 6:
        fallback = _reference_strip_quotes(cleaned.strip())
        if len(re.sub(r"\s", "", fallback)) >= len(re.sub(r"\s", "", lyric)):
            lyric = fallback
    lyric = _reference_strip_quotes(lyric)
    return lyric or _REFERENCE_FALLBACK_LYRIC


# ============================================================================
# 参考项目的 5 维风格体系（astrbot_plugin_tts_emotion_router v3.3.4）
# 原设计：overall_tone / timbre_positioning / persona_accent / dialect / role_play
# 五个字段互相独立，选中后聚合进同一对括号：（温柔、甜美、夹子音）
# 「甜美」属于音色质感、「夹子音」属于人设腔调，因此可以同时开启。
# ============================================================================

MIMO_STYLE_FIELD_NAMES = (
    "overall_tone",
    "timbre_positioning",
    "persona_accent",
    "dialect",
    "role_play",
)

#: 整体语调
STYLE_OVERALL_TONE = (
    "不设置", "温柔", "高冷", "活泼", "严肃", "慵懒", "俏皮", "深沉", "干练", "凌厉",
)

#: 音色质感（甜美在这里）
STYLE_TIMBRE = (
    "不设置", "磁性", "醇厚", "清亮", "空灵", "稚嫩", "苍老", "甜美", "沙哑", "醇雅",
)

#: 人设腔调（夹子音在这里）
STYLE_PERSONA = (
    "不设置", "夹子音", "御姐音", "正太音", "大叔音", "台湾腔",
)

#: 方言
STYLE_DIALECT = (
    "不设置", "东北话", "四川话", "河南话", "粤语",
)

#: 角色扮演
STYLE_ROLE_PLAY = (
    "不设置", "孙悟空", "林黛玉",
)

_NONE_STYLE_VALUES = {"", "不设置", "无", "none", "off"}


def build_style_prompt(
    overall_tone: str = "",
    timbre_positioning: str = "",
    persona_accent: str = "",
    dialect: str = "",
    role_play: str = "",
    style_prompt: str = "",
    *,
    singing: bool = False,
) -> str:
    """把 5 维风格拼成一句自然语言，放进 **user 消息**。

    为什么不放 assistant 的括号标签里：
    实测 `（温柔、甜美、夹子音）正文` 会被模型当成正文念出来
    （"夹子音""甜美"这些词直接出现在语音里）。
    官方把 user 消息定义为「自然语言风格控制」，这一路不会被念。

    返回空串表示不指定任何风格。
    """
    parts: list[str] = []
    for value in (overall_tone, timbre_positioning, persona_accent, dialect, role_play):
        text = str(value or "").strip()
        if text and text.lower() not in _NONE_STYLE_VALUES:
            parts.append(text)
    prompt = str(style_prompt or "").strip()
    if prompt:
        parts.append(prompt)
    if not parts:
        return ""
    traits = "、".join(parts)
    verb = "演唱" if singing else "说话"
    return f"请用{traits}的方式{verb}，保持整段一致，不要念出这些描述词。"




def normalize_lyrics(text: str | None) -> str:
    """清洗歌词：剥离已有标签、去 Markdown、行内折叠空白。

    刻意**保留换行**：乐句分行是演唱节奏的重要提示，早前把换行折叠成空格
    会让模型失去句读，唱出来接近朗读。
    """
    _, body = split_leading_singing_tag(text)
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = _MARKDOWN_PREFIX.sub("", body)
    body = _MARKDOWN_DECORATION.sub("", body)
    lines = [_INLINE_WHITESPACE.sub(" ", line).strip() for line in body.split("\n")]
    return "\n".join(line for line in lines if line).strip()


#: V2 体系的 <style>…</style>，粘到 V2.5 入口时先剥掉，避免套娃成 (唱歌)<style>唱歌</style>歌词
_STYLE_WRAP_TAG = re.compile(r"^\s*<style>.*?</style>\s*", re.IGNORECASE | re.DOTALL)


def strip_leading_tag_only(text: str | None) -> str:
    """最小处理：只剥开头的唱歌标签、统一换行，**不动歌词内容**。

    `normalize_lyrics` 会删 `~`（中文歌词里常表示拖长音）和 `**`/`#` 等装饰，
    那属于「改歌词」。用户把语义清洗关掉时应该走这条路径。
    """
    _, body = split_leading_singing_tag(text)
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in body.split("\n")).strip()


def strip_voice_direction_tags(text: str | None) -> str:
    """剔除会改变音色的括号标签（如（男声）、（换音色））。"""
    cleaned = _VOICE_DIRECTION_TAGS.sub("", text or "")
    # 清理标签移除后留下的空闲空白与多余空行
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return "\n".join(line.strip() for line in cleaned.split("\n") if line.strip()).strip()


#: 一句歌词最长多少字。超过就在逗号处切成短句——一行唱太长，
#: 模型会换气不当、吞字、甚至唱错词，这是「不会短句」的直接原因
_MAX_LINE_CHARS = 16

#: 切分点只认句内逗号，不切词
_LINE_SPLIT = re.compile(r"[，,]")


def split_lyric_lines(text: str | None, max_chars: int = _MAX_LINE_CHARS) -> str:
    """把过长的歌词行按逗号切成短句，一行一句。

    只在**整行超过 max_chars** 时才切，且切点必须是逗号，所以：
    - 长短适中的句子原样保留，不会被切碎（v1.5.x 把每个逗号都变换行的错误）
    - 48 字的一整段会被切成 4 个完整乐句，唱起来才有换气的地方
    """
    lines: list[str] = []
    for line in (text or "").split("\n"):
        current = line.strip()
        if not current:
            continue
        if len(current) <= max_chars:
            lines.append(current)
            continue
        buffer = ""
        for piece in _LINE_SPLIT.split(current):
            piece = piece.strip(" ，,。；;、")
            if not piece:
                continue
            if not buffer:
                buffer = piece
            elif len(buffer) + len(piece) + 1 <= max_chars:
                buffer = f"{buffer}，{piece}"
            else:
                lines.append(buffer)
                buffer = piece
        if buffer:
            lines.append(buffer)
    return "\n".join(lines)


#: 开头寒暄/引导语特征。
#: **绝不能用裸前缀匹配**：「好的事情总会来」「接下来的人生还很长」「马上就会明白」
#: 「来听我说一句」都是流行歌常见句，裸前缀会把用户手写歌词的第一行删掉。
#: 只认三种：整行就是一句应答、应答＋唱、明确的演唱类元话语。
_CHITCHAT_HEAD = re.compile(
    r"^(?:"
    r"好(?:的|嘞|呀|吧)?[呀嘞吧，,、～~]*\s*$|"
    r"(?:好的|好嘞|好啊|来吧)[呀嘞吧]?[，,、～~]?\s*(?:我|这就|马上|那)[^，,]{0,10}(?:唱|来一段)|"
    r"我?(?:这就|马上)?给你唱|下面(?:给|我)?你?唱|接下来(?:给|我)?你?唱|"
    r"希望你喜[欢歡]|希望您喜[欢歡]|喜[欢歡]的话|谢谢收听|谢谢观看"
    r")"
)


def cut_chitchat_head(text: str | None) -> str:
    """从开头删掉寒暄/引导行，至少保留一行歌词。

    与 `cut_chitchat_tail` 对称：判定同样保守（≤14 字、不含逗号、寒暄词开头），
    只是作用在头部。模型有时把「希望你喜欢这首歌」写在歌词前面，尾部函数管不到。
    """
    lines = [line for line in (text or "").split("\n")]
    index = 0
    while index < len(lines) - 1:  # 至少留一行歌词
        stripped = lines[index].strip()
        if (
            stripped
            and len(stripped) <= 14
            and "，" not in stripped
            and _CHITCHAT_HEAD.match(stripped)
        ):
            index += 1
            continue
        break
    return "\n".join(lines[index:]).strip()


#: 收尾寒暄的句首特征：只在这类句子出现时截断，避免误伤「希望你能听见」这类歌词
_CHITCHAT_TAIL_HEAD = re.compile(
    r"^(?:希望你喜[欢歡]|希望您喜[欢歡]|喜[欢歡]的话|记得点赞|记得关注|多多关注|求关注|"
    r"谢谢收听|谢谢观看|感谢收听|感谢观看|下期再见|我们下期|拜拜)"
)


def cut_chitchat_tail(text: str | None) -> str:
    """从第一句收尾寒暄开始，后面全部丢掉。

    这才是「唱到后面变成说话」的正解：模型在歌词末尾追加的寒暄，
    应该在拆标签这一步就切掉，而不是靠事后对每一行做语义判断
    （v2.3.0 删多了、v2.5.0 删少了，那条路不收敛）。

    判定很保守：整行 ≤12 字、不含逗号、不含「我」，且以寒暄词开头。
    「希望你能听见我唱的这首歌」含「我」、「记得那年夏天」不以寒暄词开头，都不会触发。
    """
    kept: list[str] = []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if (
            kept
            and stripped
            and len(stripped) <= 14
            and "。" not in stripped
            and stripped.count("，") <= 1
            and "我" not in stripped
            and _CHITCHAT_TAIL_HEAD.match(stripped)
        ):
            break
        kept.append(line)
    return "\n".join(kept).strip()


#: 行首的段落标记前缀：（副歌）（主歌）（间奏）… 判断解释句前先剥掉
_SECTION_PREFIX = re.compile(
    r"^\s*[（(\[【]?\s*(?:主歌|副歌|间奏|前奏|尾声|桥段|导歌|预副歌|副歌部分|主歌部分)"
    r"\s*[）)\]】]?\s*[:：、，,]?\s*"
)


#: **只有整行就是这个标记**才算解释句。绝不能用子串包含：
#: 「记得那次我们一起去海边」「希望你能听见我唱的这首歌」「晚安北京」「关注着你的每一步」
#: 都是正常歌词，一旦被整行删掉就变成残缺歌词 → 官方明确说残缺会跑调、听着像唱错词。
_EXPLAIN_LINE = re.compile(
    r"^\s*[（(【\[]?\s*(?:这首歌|这段歌词|下面是|以下是|歌词|主歌|副歌|主歌部分|副歌部分|"
    r"希望你喜欢|喜欢的话|记得点赞|记得关注|点赞|关注|一键三连|"
    r"谢谢收听|谢谢观看|唱完了|唱完啦|备注|说明)\s*[）)】\]]?\s*[:：、，,。！!？?~～]*\s*$"
)

#: 以「这首歌/这段歌词」开头、并带解释性动词的整句，也是说明而不是歌词
_EXPLAIN_SENTENCE = re.compile(
    r"^\s*(?:这首歌|这段歌词)[^。！？\n]{0,30}(?:表达|讲述|描述|寓意|唱的是|讲的是|关于)"
)


#: 整行就是"求点赞关注"这类号召，也属于非歌词
_CTA_LINE = re.compile(
    r"^\s*(?:记得|求|多多|别忘了)?\s*(?:点赞|关注|三连|收藏|转发|评论|打赏|订阅)"
    r"(?:[和与、，,]?\s*(?:点赞|关注|三连|收藏|转发|评论|打赏|订阅))*\s*[！!。.~～]*\s*$"
)


def strip_explanation_lines(text: str | None, max_chars: int = 30) -> str:
    """删掉**整行就是**解释/标题/寒暄的行，只留歌词本体。

    只删「整行等于标记」「以标记开头且带解释动词」「整行是求关注」这三种情况，
    绝不因为行里**含有**标记词就删——「记得…」「希望…」「晚安…」「关注…」
    全是高频歌词用词，误删就会变成残缺歌词 → 跑调、听着像唱错词。
    """
    kept: list[str] = []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # 剥掉行首段落标记（（副歌）（主歌）…）后再判断：
        # 「（副歌）希望你喜欢」整行是寒暄，该删；
        # 「（副歌）我们一起唱歌吧」剥完还剩真歌词，原样保留
        probe = _SECTION_PREFIX.sub("", stripped).strip()
        if not probe:
            continue
        if len(probe) <= max_chars and _EXPLAIN_LINE.match(probe):
            continue
        if _EXPLAIN_SENTENCE.match(probe):
            continue
        if len(probe) <= max_chars and _CTA_LINE.match(probe):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def strip_action_tags(text: str | None) -> str:
    """只剔除**情绪表演**类括号标签（叹气、哽咽、抽泣…）。

    关键区分：_KEEP_SINGING_TAGS 里的（换气）（停顿）（气声）（轻声）是
    官方支持的**演唱控制信号**，它们决定呼吸点和发声方式，**绝不能删**——
    删了长句只能一口气顶下来，后半段挤成一团；删了气声全曲一个力度，
    听感就是"像带调朗读"。早前版本把它们一起当"小动作"删掉了，是错的。
    """
    cleaned = _ACTION_TAGS.sub("", text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return "\n".join(line.strip() for line in cleaned.split("\n") if line.strip()).strip()


def build_singing_content(
    lyrics: str | None,
    tag: str = SINGING_CANONICAL_TAG,
    extra_tags: Sequence[str] = (),
    sanitize: bool = True,
) -> str:
    """构造唱歌内容：`(唱歌)歌词`。

    标签**永远只保留一个唱歌标识、用半角括号**。
    v1.6.0 试过叠加气质词（`（唱歌、温柔）`）实测反而更差——多标签会让模型不再
    把它识别为"唱歌"，退化成带腔调的朗读。风格只能靠指令与音色，不能靠标签堆。
    """
    # 先剥 V2 的 <style>…</style>，否则 sanitize=True 路径会套娃成 (唱歌)<style>唱歌</style>歌词
    source = _STYLE_WRAP_TAG.sub("", lyrics or "")
    clean = normalize_lyrics(source) if sanitize else strip_leading_tag_only(source)
    tag_inner = normalize_style(tag)
    if not tag_inner or not is_singing_style(tag_inner):
        tag_inner = SINGING_CANONICAL_TAG
    # 标签内只保留第一个有效标识，避免 (唱歌 唱歌) 这类冗余
    first = _STYLE_SEPARATORS.split(tag_inner)[0] if _STYLE_SEPARATORS.search(tag_inner) else tag_inner
    if not first or not is_singing_style(first):
        first = SINGING_CANONICAL_TAG
    return f"({first}){clean}"


def build_singing_content_style_tag(
    lyrics: str | None,
    extra_tags: Sequence[str] = (),
    sanitize: bool = True,
) -> str:
    """MiMo V2（<style> 标签体系）的唱歌写法。"""
    parts = [SINGING_CANONICAL_TAG]
    for extra in extra_tags or ():
        value = str(extra).strip()
        if value and value not in parts:
            parts.append(value)
    return f"<style>{' '.join(parts)}</style>{normalize_lyrics(lyrics)}"


def apply_style_prefix(text: str | None, style: str | None) -> str:
    """V2.5 音频标签控制：`(风格)文本`；唱歌风格自动转换为 `(唱歌)歌词`。"""
    body = (text or "").strip()
    inner = normalize_style(style)
    if not inner:
        return body
    if is_singing_style(inner):
        return build_singing_content(body, inner)
    return f"({inner}){body}"


def apply_style_prefix_legacy(text: str | None, style: str | None) -> str:
    """V2 `<style>` 标签控制：`<style>风格</style>文本`。"""
    body = (text or "").strip()
    inner = normalize_style(style)
    if not inner:
        return body
    if is_singing_style(inner):
        return build_singing_content_style_tag(body)
    return f"<style>{inner}</style>{body}"


def extract_singing_block(text: str | None, tag: str = "sing") -> tuple[bool, str]:
    """兼容包装：只关心 (是否唱歌, 歌词)。"""
    singing, lyrics, _ = split_singing_reply(text, tag)
    return singing, lyrics


def split_singing_reply(text: str | None, tag: str = "sing") -> tuple[bool, str, str]:
    """把模型回复拆成 (是否唱歌, 歌词, 尾注)。

    尾注只取**最后一个唱歌标签之后**的文本：模型通常把「好的，我来唱」写在前面，
    那部分属于引导语该丢掉；标签后面才是「想对用户说的话」，
    这部分会作为普通文字发出，不会被唱。
    """
    raw = text or ""
    name = (tag or "sing").strip() or "sing"
    patterns = (
        re.compile(
            r"<\s*" + re.escape(name) + r"\s*>(.*?)</\s*" + re.escape(name) + r"\s*>",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"\[\s*" + re.escape(name) + r"\s*\](.*?)\[\s*/\s*" + re.escape(name) + r"\s*\]",
            re.IGNORECASE | re.DOTALL,
        ),
    )
    for pattern in patterns:
        matches = list(pattern.finditer(raw))
        if not matches:
            continue
        lyrics = "\n".join(
            match.group(1).strip() for match in matches if match.group(1).strip()
        )
        if not lyrics:
            # 匹配到标签但内容为空：不唱，且把标签从正文里清掉
            return False, pattern.sub("", raw).strip(), ""
        tail = raw[matches[-1].end() :].strip()
        tail = re.sub(r"[*`~]{1,3}", "", tail).strip()
        return True, cut_chitchat_tail(lyrics), tail[:TAIL_MAX_CHARS]

    if has_singing_tag(raw):
        # 整体以 (唱歌) 开头时没有尾注概念；先切掉末尾寒暄，
        # 否则整段（含「希望你喜欢」）都会被当歌词唱出来
        _, body = split_leading_singing_tag(raw)
        return True, cut_chitchat_tail(body), ""

    # 兜底：剥掉未闭合/被 markdown 包裹的标签残片，
    # 否则 `<sing>` 会被 TTS 念成「小于 sing 大于」，`**` 会进正文
    cleaned = re.sub(r"</?\s*" + re.escape(name) + r"\s*>?", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[\s*/?\s*" + re.escape(name) + r"\s*\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[*`~]{1,3}", "", cleaned).strip()
    return False, cleaned, ""


#: 尾注最大长度，避免把整段解释当文本发出来
TAIL_MAX_CHARS = 200

#: 模型拒绝/推脱时的特征词，兜底路径不应把这类回复当歌词唱
_REFUSAL_HINTS = (
    "我不能",
    "我无法",
    "我不会唱",
    "我不会唱歌",
    "无法演唱",
    "不能唱歌",
    "作为 ai",
    "作为人工智能",
    "抱歉",
    "对不起",
)


def looks_like_refusal(text: str | None) -> bool:
    """回复是否更像拒绝/推脱，而不是歌词。"""
    haystack = (text or "").lower()
    return any(hint in haystack for hint in _REFUSAL_HINTS)


def guess_lyrics_from_reply(text: str | None) -> str:
    """从 LLM 回复里猜测「要唱的那部分」，猜不出就返回空串。

    关键词兜底路径下，模型可能回「好的，我来唱：\\n<歌词>」，
    这里优先取引号/书名号里的内容，其次去掉开头寒暄行；
    若整体像拒绝（"我不能唱歌"），直接返回空，避免把拒绝语唱出来。
    反问句（"你想听什么歌？"）也返回空——把它唱出来就是「直接造句」。
    """
    raw = (text or "").strip()
    if not raw or looks_like_refusal(raw):
        return ""
    # 反问/征询句不是歌词，唱出来就是造句。
    # 只认真正的问句特征，不能见「你」就杀——「我爱你中国」是歌词
    question_hints = ("什么歌", "听什么", "哪首歌", "听哪首", "要唱什么", "想听什么")
    if len(raw) < 20 and (
        re.search(r"[？?]\s*$", raw) or "吗" in raw or any(h in raw for h in question_hints)
    ):
        return ""

    best = ""
    for left, right in _QUOTE_PAIRS:
        start = raw.find(left)
        while start != -1:
            end = raw.find(right, start + len(left))
            if end == -1:
                break
            candidate = raw[start + len(left) : end].strip()
            if len(candidate) > len(best):
                best = candidate
            start = raw.find(left, end + len(right))
    # 「雨爱」这类两字候选是歌名，不是歌词；宁可返回空让上层报错，也不要唱一声
    if best and len(_re_flat(best)) >= 12:
        return best

    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    kept: list[str] = []
    for index, line in enumerate(lines):
        is_first = index == 0
        if is_first and len(line) <= 24 and (_CHITCHAT_LINE.match(line) or "唱" in line):
            continue
        kept.append(line)
    guessed = "\n".join(kept)
    if looks_like_refusal(guessed):
        return ""
    # 过滤完寒暄行后什么都不剩，说明整段都是「好的呀，我给你唱一首」这类
    # 引导语，没有歌词可唱——返回空让上层按「没拿到歌词」处理
    if not guessed:
        return ""
    return guessed


def estimate_seconds(text: str | None, chars_per_second: float = 3.0) -> float:
    """粗估演唱时长（秒）：按有效字数除以每秒字数，忽略空白。"""
    cps = chars_per_second if chars_per_second and chars_per_second > 0 else 3.0
    return len(re.sub(r"\s+", "", text or "")) / cps


def slice_lyrics(
    lyrics: str | None,
    max_chars: int,
    mode: str = "head",
    rng: random.Random | None = None,
    max_lines: int = 0,
) -> tuple[str, bool]:
    """截取一段歌词，以「行（乐句）」为最小单位，保证片段完整、不半截。

    宁可短也要完整：唱半句比唱两段都难听，所以只在换行处断开；
    只有整段写成一行时才退化为按标点断句。首行就超预算时保留整行，
    完整性优先于凑时长。

    Args:
        lyrics: 原始歌词。
        max_chars: 字数预算，<=0 表示不限制。
        mode: `head` 取开头；`random` 随机起唱；`off` 不切片。
        rng: 随机源，便于测试注入。
        max_lines: 行数上限，<=0 表示不限制。行数比字数更接近真实演唱时长。

    Returns:
        (歌词片段, 是否发生了截断)
    """
    text = normalize_lyrics(lyrics)
    normalized_mode = (mode or "head").strip().lower()
    if normalized_mode == "off" or max_chars <= 0 or len(text) <= max_chars:
        # 字数没超，但行数可能仍然超（短行堆积），仍需按行数收一次
        lines_all = [line for line in text.split("\n") if line.strip()]
        if max_lines > 0 and len(lines_all) > max_lines:
            return "\n".join(lines_all[:max_lines]).strip(), True
        return text, False

    lines = [line for line in text.split("\n") if line.strip()]
    if len(lines) > 1:
        units = lines
        joiner = "\n"
    else:
        # 只有一行时按标点断句，尽量保住句子完整
        units = [unit for unit in _SENTENCE_BOUNDARY.split(text) if unit.strip()]
        joiner = ""
    if not units:
        return text[:max_chars].strip(), True

    if normalized_mode == "random" and len(units) > 1:
        picker = rng or random
        start = picker.randrange(len(units))
        ordered = units[start:] + units[:start]
    else:
        ordered = units

    picked: list[str] = []
    total = 0
    for unit in ordered:
        if picked and total + len(unit) > max_chars:
            break
        if max_lines > 0 and len(picked) >= max_lines:
            break
        picked.append(unit)
        total += len(unit)
        if total >= max_chars:
            break

    if not picked:
        picked = [ordered[0]]
    joined = joiner.join(picked).strip()
    if max_chars > 0:
        flat = re.sub(r"\s+", "", joined)
        if len(flat) > max_chars:
            # 单行且没有任何标点可断时只能按字数硬切：
            # 绝不能把超长整段发出去再声称"已截取"，那是对用户说谎。
            # 按**可见字符**计数并保留原有空白，否则英文歌词会被挤成一串字母
            out: list[str] = []
            count = 0
            for char in joined:
                if not char.isspace():
                    if count >= max_chars:
                        break
                    count += 1
                out.append(char)
            joined = "".join(out).strip()
    return joined, True


def singing_length_warning(
    lyrics: str | None,
    min_chars: int = _MIN_LYRICS_CHARS,
    max_chars: int = _MAX_LYRICS_CHARS,
    *,
    truncates: bool = True,
) -> str | None:
    """歌词长度异常时返回提示：过短官方会跑调，过长容易超时或费用偏高。

    `truncates` 必须由调用方按真实链路传入：复古模式和参考模式都**不截断**，
    以前无论走哪条链路都提示「超出部分会被截断」，用户据此以为时长被砍了。
    """
    clean = normalize_lyrics(lyrics)
    flat = re.sub(r"\s+", "", clean)
    if len(flat) < min_chars:
        return (
            f"唱歌歌词过短（{len(flat)} 字，建议不少于 {min_chars} 字），"
            "MiMo 官方说明残缺歌词会跑调，请提供完整歌词。"
        )
    if len(flat) > max_chars:
        consequence = (
            f"超出部分会被截断（实际送出约 {max_chars} 字）"
            if truncates
            else "当前模式不截断，但后半段容易跑调、也更容易超时"
        )
        return (
            f"唱歌歌词偏长（{len(flat)} 字，上限约 {max_chars} 字），"
            f"{consequence}，建议只保留一段完整副歌。"
        )
    return None


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def contains_cjk(text: str | None) -> bool:
    """文本是否包含中日韩汉字，用于提示唱歌音色与歌词语言是否匹配。"""
    return bool(_CJK.search(text or ""))
