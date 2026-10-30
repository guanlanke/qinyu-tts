"""情语 - 通过钩子拦截消息，自行完成 TTS 合成"""

import inspect
import json
import random
import re
import time
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

from .engines import (
    ENGINE_REGISTRY,
    get_engine,
    TTSEngine,
    SINGING_TEMPLATE_KEYS,
    build_speech_tone,
    estimate_seconds,
    extract_persona_traits,
    guess_lyrics_from_reply,
    looks_like_refusal,
    singing_length_warning,
    slice_lyrics,
    split_singing_reply,
)

try:
    from astrbot.core.star.session_llm_manager import SessionServiceManager
except Exception:
    SessionServiceManager = None

#: 插件可视化视图用的 Web API helper。缺失时（旧版 AstrBot）降级为直接返回 dict。
try:
    from astrbot.api.web import error_response as _web_error
    from astrbot.api.web import json_response as _web_json
    from astrbot.api.web import request as _web_request
except Exception:  # noqa: BLE001
    _web_error = None
    _web_json = None
    _web_request = None


DISABLED_ENGINE = "__disabled__"

#: singing_engine 里表示「跟随朗读引擎」的取值（界面下拉的第一项）
_FOLLOW_ENGINE_VALUES = frozenset(
    {"", "跟随朗读引擎", "跟随", "follow", "same", "default"}
)

#: 插件版本，供 /tts_doctor 自检显示（与 metadata.yaml 保持一致）
PLUGIN_VERSION = "4.6.0"

#: 自检里用来标记状态的符号，刻意选 GBK 也能编码的字符，避免控制台编码报错
MARK_OK = "√"
MARK_BAD = "×"

#: LLM 输出唱歌片段使用的标签名（固定值，不做配置以免多一个填错的入口）
DEFAULT_SINGING_TAG = "sing"

#: 演唱语速换算的**初始**估计：中文流行歌大约 3 字/秒。
#: 实际语速由插件自己测量（见 _wav_duration_seconds + _update_calibration）：
#: MiMo 实测比 3 字/秒快，按 3 折算会让「目标 40 秒」只写出 120 字、真唱只有二十几秒。
SINGING_CHARS_PER_SECOND = 3.0

#: 自动校准允许的语速范围，超出这个区间的测量值视为异常（含长段静音/接口异常）直接丢弃
SINGING_CPS_MIN = 1.5
SINGING_CPS_MAX = 9.0

#: 校准时保留旧值的比重：越大越保守，避免单次异常把估计带偏
SINGING_CPS_SMOOTHING = 0.7

#: 少于这么多字或这么短的音频不参与校准，样本太小噪声太大
SINGING_CALIBRATION_MIN_CHARS = 20
SINGING_CALIBRATION_MIN_SECONDS = 3.0

#: 单次演唱目标时长的**默认值**（秒）。可被配置项 singing_target_seconds 覆盖。
#: 135 字只够 20-40 秒；要接近一分钟得给到 180 字（8-12 句）
SINGING_TARGET_SECONDS = 60

#: 单次演唱硬上限的**默认值**（秒）：目标 60 秒，留 30 秒余量——慢歌 180 字能唱到 80 秒以上
SINGING_MAX_SECONDS = 90

#: 配置项允许的时长范围：低于 15 秒基本只够一句，高于 300 秒会拖垮接口超时
SINGING_MIN_ALLOWED_SECONDS = 15
SINGING_MAX_ALLOWED_SECONDS = 300

#: 提前给歌词定长时按目标时长的这个比例要字数，剩下的余量留给模型自由发挥
SINGING_PROMPT_FLOOR_RATIO = 0.85

#: 「禁止唱歌时段」的默认值，形如 23:00-07:00，多段用逗号分隔
DEFAULT_SINGING_QUIET_HOURS = ""

#: 「唱多久」预设：普通用户只需要点一个词，不需要理解秒数与字数。
#: key -> (中文名, 目标秒数)
SINGING_LENGTH_PRESETS = {
    "short": ("短", 20),
    "medium": ("中", 40),
    "long": ("长", 60),
    "xlong": ("很长", 90),
}

#: 中文（以及常见叫法）到预设 key 的映射，供聊天指令使用
SINGING_LENGTH_ALIASES = {
    "短": "short",
    "一点": "short",
    "短一点": "short",
    "short": "short",
    "中": "medium",
    "中等": "medium",
    "正常": "medium",
    "medium": "medium",
    "长": "long",
    "长一点": "long",
    "long": "long",
    "很长": "xlong",
    "最长": "xlong",
    "加长": "xlong",
    "xlong": "xlong",
}

#: 可视化视图允许修改的键：白名单，避免面板变成任意配置写入器。
#: 整数键里 *_private / *_group 填 0 表示「跟随全局」。
WEB_EDITABLE_INT_KEYS = frozenset(
    {
        "singing_target_seconds",
        "singing_max_seconds",
        "singing_target_seconds_private",
        "singing_target_seconds_group",
        "singing_max_seconds_private",
        "singing_max_seconds_group",
    }
)
WEB_EDITABLE_BOOL_KEYS = frozenset(
    {
        "singing_enable",
        "singing_auto_intent",
        "singing_quiet_hours_enable",
        "singing_clip_to_target",
        "singing_fill_short",
        "singing_unified_mode",
    }
)
#: 浮点键：每秒钟唱多少字。0 表示由插件实测自动校准。
WEB_EDITABLE_FLOAT_KEYS = frozenset({"singing_chars_per_second"})
#: 真正需要「时段格式校验」的键。其他字符串键（引擎名、预设名）不该走这套校验，
#: 否则「跟随朗读引擎」会被当成时间范围解析，报「时段格式不对」。
QUIET_HOUR_KEYS = frozenset(
    {
        "singing_quiet_hours",
        "singing_quiet_hours_private",
        "singing_quiet_hours_group",
    }
)

WEB_EDITABLE_STR_KEYS = frozenset(
    {
        "singing_quiet_hours",
        "singing_quiet_hours_private",
        "singing_quiet_hours_group",
        "singing_length_preset",
        # 面板上可以点选朗读/唱歌引擎，不用回配置页手打实例名
        "active_engine",
        "singing_engine",
    }
)
WEB_EDITABLE_KEYS = (
    WEB_EDITABLE_INT_KEYS | WEB_EDITABLE_BOOL_KEYS | WEB_EDITABLE_STR_KEYS | WEB_EDITABLE_FLOAT_KEYS
)

#: 聊天里的短写法 → 配置键。让「/tts_set 私聊 40」这种一键指令真的可用，
#: 不必记住 singing_target_seconds_private 这种长名字。
SETTING_ALIASES = {
    "全局": "singing_target_seconds",
    "全局时长": "singing_target_seconds",
    "全局上限": "singing_max_seconds",
    "私聊": "singing_target_seconds_private",
    "私聊时长": "singing_target_seconds_private",
    "private": "singing_target_seconds_private",
    "p": "singing_target_seconds_private",
    "群聊": "singing_target_seconds_group",
    "群聊时长": "singing_target_seconds_group",
    "group": "singing_target_seconds_group",
    "g": "singing_target_seconds_group",
    "私聊上限": "singing_max_seconds_private",
    "群聊上限": "singing_max_seconds_group",
    "时段": "singing_quiet_hours",
    "私聊时段": "singing_quiet_hours_private",
    "群聊时段": "singing_quiet_hours_group",
    "开关": "singing_quiet_hours_enable",
    "裁剪": "singing_clip_to_target",
    # 最常用的一项放在最好记的名字上
    "时长": "singing_length_preset",
    "唱多久": "singing_length_preset",
    "唱歌时长": "singing_length_preset",
    "补足": "singing_fill_short",
    "补唱": "singing_fill_short",
    "语速": "singing_chars_per_second",
    "统一": "singing_unified_mode",
    "统一模式": "singing_unified_mode",
    "全统一": "singing_unified_mode",
}


def _resolve_setting_key(raw: str) -> str | None:
    """把短写法或完整键名解析成配置键；认不出来返回 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text in WEB_EDITABLE_KEYS:
        return text
    return SETTING_ALIASES.get(text) or SETTING_ALIASES.get(text.lower())


def _parse_quiet_windows(raw: str) -> tuple[list[tuple[int, int]], list[str]]:
    """把「23:00-07:00,12:00-13:30」解析成分钟区间，返回 (区间, 无法解析的片段)。"""
    windows: list[tuple[int, int]] = []
    bad: list[str] = []
    for chunk in re.split(r"[,，;；\s]+", raw or ""):
        if not chunk:
            continue
        if "-" not in chunk:
            bad.append(chunk)
            continue
        start_text, _, end_text = chunk.partition("-")
        try:
            start_h, start_m = (int(x) for x in start_text.strip().split(":"))
            end_h, end_m = (int(x) for x in end_text.strip().split(":"))
        except (TypeError, ValueError):
            bad.append(chunk)
            continue
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
            bad.append(chunk)
            continue
        if not (0 <= start_m <= 59 and 0 <= end_m <= 59):
            bad.append(chunk)
            continue
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start == end:
            bad.append(chunk)
            continue
        windows.append((start, end))
    return windows, bad

#: 旧版本的关键词默认值（没有单字「唱」）。命中它说明用户没自定义过，直接升级
LEGACY_SINGING_KEYWORDS = (
    "唱歌,唱首歌,唱一段,来首歌,给我唱,唱首,唱两句,唱一小段,哼一段,唱个,来一段歌",
    "唱,唱歌,唱首歌,唱一段,来首歌,给我唱,唱首,唱两句,唱一小段,哼一段,来一段歌",
)


def _as_float(value, default: float = 0.0) -> float:
    """把可能为字符串或 None 的配置值安全转成浮点数。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wav_duration_seconds(data: bytes) -> float:
    """从 WAV 字节里算出真实时长（秒）；不是可解析的 WAV 就返回 0。

    唱歌默认输出 wav，所以这是唯一能拿到「这次到底唱了几秒」的可靠途径。
    mp3/pcm 无法用纯标准库可靠推算，直接返回 0 跳过校准。
    """
    total = len(data)
    if total < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 0.0
    byte_rate = 0
    data_size = 0
    pos = 12
    while pos + 8 <= total:
        chunk_id = data[pos : pos + 4]
        chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = pos + 8
        # 流式写入的 WAV 会把 size 写成 0 或 0xFFFFFFFF，按实际剩余字节兜底
        available = total - body
        if chunk_size > available:
            chunk_size = max(0, available)
        if chunk_id == b"fmt " and chunk_size >= 16:
            channels = int.from_bytes(data[body + 2 : body + 4], "little")
            sample_rate = int.from_bytes(data[body + 4 : body + 8], "little")
            bits = int.from_bytes(data[body + 14 : body + 16], "little")
            if channels and sample_rate and bits:
                byte_rate = channels * sample_rate * max(1, bits // 8)
        elif chunk_id == b"data":
            data_size = chunk_size
        pos = body + chunk_size + (chunk_size & 1)
    if byte_rate <= 0 or data_size <= 0:
        return 0.0
    return data_size / byte_rate


#: 自动识别「用户要求唱歌」的默认关键词。
#: 刻意**不含裸「唱」**：只要出现「唱」字就触发的话，「这首歌谁的」「说唱」「合唱团」
#: 都会把模型的普通回复整段拿去唱，用户听感就是「怎么突然念着唱起来」。
DEFAULT_SINGING_KEYWORDS = (
    "唱歌,唱首歌,唱一段,来首歌,给我唱,唱首,唱两句,唱一小段,哼一段,来一段歌,唱一个,唱给我听"
)

#: 记录 AstrBot 事件钩子是否注册成功，供 /tts_status 显示诊断
HOOK_REGISTRY: dict[str, bool] = {}

#: 唱歌片段的系统提示词模板，变量：tag / chars / seconds
SINGING_PROMPT_TEMPLATE = """【唱歌模式】
用户要求唱歌、哼唱、唱一段，或只给出歌名/歌词开头时，直接输出：
<{tag}>歌词</{tag}>
规则：
1. 标签内只写歌词：不要歌名标题、不要"主歌/副歌"、不要解释、不要 Markdown；标签外**一个字都不要写**（不要唱完再补一句吐槽或点评）；
2. 必须用这首歌的**原版歌词原文**：不许改写、不许自己编词、不许替换字词，也不要反问用户要歌词；
3. **写完整的一整段歌词（主歌或副歌都可以），写到自然收尾**：**字数不得少于 {floor} 字**（约 {chars} 字更好），
   少于 {floor} 字算不合格——只挑最出圈的几句就断会导致跑调，也唱不到 40 秒；同样不要一段接一段没完没了；
4. 每句内部用逗号连接，句子之间用句号，像真实歌词那样有节奏；
5. 用户没要求唱歌时，不要输出该标签。

示例（长度参考，不要照抄内容）：
<{tag}>窗外的雨下不停，我的心也跟着乱了节奏，想起你说过的每一句话，像风一样掠过心头。我把回忆折成纸船，让它顺着雨水漂走，只希望你能听见，我还在原地等你回头。雨一直下，我一直等，等你把伞撑到我头顶。当雨停了天亮了，我们还能不能回到从前。</{tag}>"""


def _optional_hook(name: str):
    """按 AstrBot 版本动态获取事件钩子装饰器，缺失时降级为无操作。

    只接受可调用的返回结果：钩子工厂签名变化时宁可不注册，
    也不能把类属性替换成 None，否则 AstrBot 加载插件时会直接报错。
    注册结果记录到 HOOK_REGISTRY，供 /tts_status 排查。
    """

    def decorator(func):
        factory = getattr(filter, name, None)
        if factory is None:
            HOOK_REGISTRY[name] = False
            return func
        # 形态一：工厂式，on_xxx() 返回装饰器
        try:
            built = factory()
        except Exception:  # noqa: BLE001
            built = None
        if callable(built):
            try:
                wrapped = built(func)
            except Exception:  # noqa: BLE001
                wrapped = None
            if callable(wrapped):
                HOOK_REGISTRY[name] = True
                return wrapped
        # 形态二：装饰器本身，直接接收函数
        try:
            wrapped = factory(func)
        except Exception:  # noqa: BLE001
            wrapped = None
        if callable(wrapped):
            HOOK_REGISTRY[name] = True
            return wrapped
        HOOK_REGISTRY[name] = False
        return func

    return decorator


def _as_str(value, default: str = "") -> str:
    """把可能为 None 或非字符串的配置值安全转换成去空白的字符串。"""
    if value is None:
        return default
    return str(value).strip()


def _as_int(value, default: int) -> int:
    """把可能为字符串或 None 的配置值安全转换成整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_per_kind_key(key: str) -> bool:
    """是否为「只作用于群聊或私聊」的覆盖项。

    这类键在统一模式下会被整体忽略，所以写它们时必须先把统一模式关掉，
    否则就是「写得进去、取时不算」的假设置。
    """
    return key.endswith("_private") or key.endswith("_group")


def _override_active(value) -> bool:
    """覆盖值是否真的在起作用：0 与空字符串都表示「清除覆盖、跟随全局」。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    return bool(str(value).strip())


def _is_slash(event: AstrMessageEvent) -> bool:
    """判断原始消息是否以斜杠开头（强制要求斜杠指令）"""
    raw = getattr(event.message_obj, "message_str", "") or ""
    return raw.lstrip().startswith("/")


#: 已知指令名，按**长度从长到短**排列：剥离命令名前缀时必须最长优先，
#: 否则 `/tts_limit` 会被 `tts` 吃掉前缀、只剩 `_limit` 被当成参数。
KNOWN_COMMANDS = (
    "tts_unbind_all",
    "tts_bindings",
    "tts_engines",
    "tts_unbind",
    "tts_switch",
    "tts_status",
    "tts_reload",
    "tts_unify",
    "tts_panel",
    "tts_limit",
    "tts_diff",
    "tts_sing",
    "tts_test",
    "tts_bind",
    "tts_set",
    "tts_say",
    "tts_off",
    "tts_on",
    "tts",
)


def _get_cmd_args(event: AstrMessageEvent) -> str:
    """从 message_str 中提取命令参数（去掉命令名本身）。

    先按空白切；`/tts_sing原谅我…` 这种不加空格时再剥掉已知命令名，
    否则用户不打空格就拿不到歌词、只回用法提示。
    """
    text = event.message_str.strip()
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        return parts[1].strip()
    body = re.sub(r"^\s*/?", "", text)
    for name in KNOWN_COMMANDS:
        if body.startswith(name):
            return body[len(name) :].strip()
    return body.strip()


def _get_session_id(event: AstrMessageEvent) -> str:
    """获取当前会话的唯一标识（UMO）"""
    return getattr(event, "unified_msg_origin", "") or ""


def _format_session_id(session_id: str) -> str:
    """将 UMO 格式化为稳定、可读的会话标识。"""
    parts = session_id.split(":")
    if len(parts) >= 3:
        message_type = parts[-2]
        target_id = parts[-1]
        type_map = {
            "GroupMessage": "群聊",
            "FriendMessage": "私聊",
        }
        label = type_map.get(message_type, message_type)
        return f"{label}:{target_id}"
    return session_id if len(session_id) <= 48 else session_id[:45] + "..."


def _get_event_user_ids(event: AstrMessageEvent) -> set[str]:
    """尽量从不同平台事件结构中提取发送者 ID。"""
    user_ids: set[str] = set()
    for obj in (event, getattr(event, "message_obj", None)):
        if obj is None:
            continue
        for attr in ("sender_id", "user_id", "userId", "qq", "wxid"):
            value = getattr(obj, attr, None)
            if value:
                user_ids.add(str(value))
        sender = getattr(obj, "sender", None)
        if isinstance(sender, dict):
            for key in ("user_id", "sender_id", "id", "qq", "wxid"):
                value = sender.get(key)
                if value:
                    user_ids.add(str(value))
        elif sender is not None:
            for attr in ("user_id", "sender_id", "id", "qq", "wxid"):
                value = getattr(sender, attr, None)
                if value:
                    user_ids.add(str(value))

    get_sender_id = getattr(event, "get_sender_id", None)
    if callable(get_sender_id):
        try:
            value = get_sender_id()
            if value:
                user_ids.add(str(value))
        except Exception:
            pass
    return user_ids


@register(
    "astrbot_plugin_qinyu",
    "某不科学的高数",
    "情语，支持多引擎与唱歌模式，通过钩子拦截实现 TTS",
    PLUGIN_VERSION,
)
class QingYuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._engine: TTSEngine | None = None
        self._session_engines: dict[str, TTSEngine] = {}  # [已弃用] session_id -> engine，保持兼容但已不使用
        self._cached_engines: dict[str, TTSEngine] = {}  # instance_name -> engine（同名引擎复用实例）
        self._session_bindings: dict[str, str] = {}  # session_id -> instance_name
        self._runtime_enabled = True
        self._last_tts_text: dict[str, str] = {}  # session_id -> 最近一次 TTS 文本
        self._last_sing_meta: dict[str, dict] = {}  # session_id -> 最近一次唱歌的时长诊断
        self._last_sing_any: dict = {}  # 最近一次唱歌（跨会话，供 WebUI 面板展示）
        self._tts_audio_text: dict[str, str] = {}  # audio_path -> TTS 文本（用于引用还原）
        self._last_temp_cleanup_at = 0.0
        self._config_drift: list[str] = []  # 启动时发现的配置重置
        self._temp_dir = Path("data") / "temp" / "qinyu"
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = Path("data") / "plugin_data" / "astrbot_plugin_qinyu"
        self._bindings_file = self._data_dir / "session_bindings.json"
        self._runtime_state_file = self._data_dir / "runtime_state.json"
        self._calibration_file = self._data_dir / "singing_calibration.json"
        self._calibration: dict = {}
        self._data_dir.mkdir(parents=True, exist_ok=True)
        # 插件改名后数据目录也变了：老用户会话绑定/运行状态/语速校准都在旧目录，
        # 这里做一次一次性迁移，避免升级后「绑定丢了、校准也丢了」。
        self._migrate_legacy_data_dir()
        # 注册可视化视图的后端接口。失败不影响插件主体，只是面板按钮不可用。
        self._register_web_apis()

    def _migrate_legacy_data_dir(self):
        """把旧插件名目录下的数据一次性搬到新目录（只搬目标不存在的那几个文件）。"""
        legacy = Path("data") / "plugin_data" / "astrbot_plugin_universal_tts"
        if not legacy.is_dir() or legacy == self._data_dir:
            return
        for name in ("session_bindings.json", "runtime_state.json", "singing_calibration.json"):
            src = legacy / name
            dst = self._data_dir / name
            if src.is_file() and not dst.exists():
                try:
                    dst.write_bytes(src.read_bytes())
                    logger.info("[情语] 已迁移旧数据: %s", name)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[情语] 迁移 %s 失败: %s", name, e)

    def _load_bindings(self):
        """从文件加载会话绑定"""
        if self._bindings_file.exists():
            try:
                self._session_bindings = json.loads(self._bindings_file.read_text(encoding="utf-8"))
            except Exception:
                self._session_bindings = {}

    def _save_bindings(self):
        """持久化会话绑定到文件"""
        try:
            self._bindings_file.write_text(
                json.dumps(self._session_bindings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[情语] 保存会话绑定失败: {e}")

    def _load_runtime_state(self):
        """加载运行时开关状态。"""
        if not self._runtime_state_file.exists():
            return
        try:
            data = json.loads(self._runtime_state_file.read_text(encoding="utf-8"))
            self._runtime_enabled = bool(data.get("runtime_enabled", True))
        except Exception as e:
            logger.warning(f"[情语] 加载运行状态失败，使用默认开启状态: {e}")
            self._runtime_enabled = True

    def _save_runtime_state(self):
        """持久化运行时开关状态。"""
        try:
            self._runtime_state_file.write_text(
                json.dumps({"runtime_enabled": self._runtime_enabled}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[情语] 保存运行状态失败: {e}")

    async def _has_admin_permission(self, event: AstrMessageEvent) -> bool:
        """检查是否允许执行全局管理命令。"""
        if not self.config.get("admin_only_commands", True):
            return True

        admin_ids = {
            str(item).strip()
            for item in (self.config.get("admin_user_ids") or [])
            if str(item).strip()
        }
        if admin_ids and _get_event_user_ids(event) & admin_ids:
            return True

        for method_name in ("is_admin", "is_at_admin", "is_msg_admin"):
            method = getattr(event, method_name, None)
            if not callable(method):
                continue
            try:
                result = method()
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    return True
            except Exception as e:
                logger.debug(f"[情语] 管理员权限检查 {method_name} 失败: {e}")

        return False

    async def _ensure_admin_permission(self, event: AstrMessageEvent) -> bool:
        if await self._has_admin_permission(event):
            return True
        logger.warning("[情语] 已拒绝非管理员执行 TTS 管理命令")
        return False

    async def _is_astrbot_tts_enabled(self, event: AstrMessageEvent) -> bool:
        """对齐 AstrBot 内置 TTS 开关：全局和会话均启用时才接管。"""
        try:
            astrbot_config = (
                getattr(self.context, "astrbot_config", None)
                or getattr(self.context, "_config", None)
            )
            tts_settings = astrbot_config.get("provider_tts_settings", {}) if astrbot_config else {}
            if not bool(tts_settings.get("enable", False)):
                return False
        except Exception as e:
            logger.debug(f"[情语] 读取 AstrBot TTS 全局开关失败，跳过接管: {e}")
            return False

        if SessionServiceManager is not None:
            try:
                return await SessionServiceManager.should_process_tts_request(event)
            except Exception as e:
                logger.debug(f"[情语] 读取 AstrBot TTS 会话开关失败，跳过接管: {e}")
                return False

        return True

    def _cleanup_temp_files(self, force: bool = False):
        """清理插件生成的过期临时音频文件。"""
        now = time.time()
        if not force and now - self._last_temp_cleanup_at < 300:
            return
        self._last_temp_cleanup_at = now

        ttl_hours = self.config.get("temp_file_ttl_hours", 72)
        max_count = self.config.get("temp_file_max_count", 1000)
        try:
            ttl_hours = max(0, int(ttl_hours))
        except (TypeError, ValueError):
            ttl_hours = 72
        try:
            max_count = max(0, int(max_count))
        except (TypeError, ValueError):
            max_count = 1000

        files = []
        for path in self._temp_dir.iterdir():
            if not path.is_file() or not path.name.startswith(("tts_", "tts_test_")):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((path, stat.st_mtime))

        removed = 0
        expire_before = now - ttl_hours * 3600 if ttl_hours > 0 else None
        remaining = []
        for path, mtime in files:
            if expire_before is not None and mtime < expire_before:
                try:
                    path.unlink()
                    removed += 1
                    # 同步回收文本缓存，避免长期运行时字典只增不减
                    self._tts_audio_text.pop(str(path), None)
                except OSError as e:
                    logger.warning(f"[情语] 删除过期临时文件失败 {path}: {e}")
            else:
                remaining.append((path, mtime))

        if max_count > 0 and len(remaining) > max_count:
            remaining.sort(key=lambda item: item[1])
            for path, _ in remaining[: len(remaining) - max_count]:
                try:
                    path.unlink()
                    removed += 1
                    self._tts_audio_text.pop(str(path), None)
                except OSError as e:
                    logger.warning(f"[情语] 删除超量临时文件失败 {path}: {e}")

        if removed:
            logger.info(f"[情语] 已清理 {removed} 个临时音频文件")

    def _find_engine_config(self, name_or_idx: str) -> dict | None:
        """按序号或实例名查找引擎配置"""
        if name_or_idx == "0":
            return None
        engines_config = self._list_engines()
        if name_or_idx.isdigit():
            idx = int(name_or_idx)
            if 1 <= idx <= len(engines_config):
                return engines_config[idx - 1]
        for ec in engines_config:
            if (ec.get("instance_name") or "").strip() == name_or_idx:
                return ec
        return None

    def _get_engine_for_session(self, session_id: str) -> TTSEngine | None:
        """获取指定会话应使用的引擎（有绑定用绑定，否则用全局）"""
        if session_id and session_id in self._session_bindings:
            bound_name = self._session_bindings[session_id]
            if bound_name == DISABLED_ENGINE:
                return None
            # 统一模式：绑定只保留「关掉这个会话」的语义，不再允许换音色。
            # 否则别人一条 /tts_bind 就能让某个群和私聊又变成两个声音。
            if self._unified_mode():
                return self._engine
            # 绑定名和全局引擎同名时直接复用全局实例。
            # 以前这里会另建一个对象：同名不同实例 → 唱歌串行锁各锁各的、
            # voice_tone 各写各的、配置改完一个旧一个新，
            # 表现出来就是「私聊和群聊同一个引擎却行为不同」。
            if (
                self._engine is not None
                and getattr(self._engine, "instance_name", "") == bound_name
            ):
                return self._engine
            # 检查同名引擎是否已缓存，复用实例
            if bound_name in self._cached_engines:
                return self._cached_engines[bound_name]
            # 创建新引擎实例并缓存
            ec = self._find_engine_config(bound_name)
            if ec:
                try:
                    engine = get_engine(ec, self.config)
                    self._cached_engines[bound_name] = engine
                    return engine
                except Exception as e:
                    logger.error(f"[情语] 会话绑定引擎创建失败 ({bound_name}): {e}")
                    # 绑定失效，移除
                    del self._session_bindings[session_id]
                    self._save_bindings()
            else:
                # 绑定的引擎已不存在，移除
                logger.warning(f"[情语] 会话绑定的引擎 '{bound_name}' 已不存在，已解除绑定")
                del self._session_bindings[session_id]
                self._save_bindings()
        return self._engine

    #: 值得盯住的配置项。这些一旦被重置，用户能直接听出来（音色变了、语音分段了）
    SNAPSHOT_KEYS = (
        "active_engine",
        "singing_engine",
        "tts_single_message",
        "singing_resident_mode",
        "singing_speed",
        "singing_length_preset",
        "voice_tone_enable",
    )

    def _snapshot_file(self) -> Path:
        return self._data_dir / "config_snapshot.json"

    def _collect_config_fingerprint(self) -> dict:
        """收集「当前实际生效」的关键配置，含每个引擎实例的音色。"""
        data: dict = {}
        for key in self.SNAPSHOT_KEYS:
            if key in self.config:
                data[key] = self.config.get(key)
        engines = {}
        for cfg in self._list_engines():
            name = _as_str(cfg.get("instance_name")) or _as_str(cfg.get("__template_key"))
            engines[name] = {
                "voice": cfg.get("voice"),
                "singing_voice": cfg.get("singing_voice"),
                "format": cfg.get("format"),
                "__template_key": cfg.get("__template_key"),
            }
        data["__engines__"] = engines
        return data

    def _save_config_snapshot(self) -> None:
        """把当前配置存成快照。只保存，不恢复。"""
        try:
            import json as _json

            self._snapshot_file().write_text(
                _json.dumps(self._collect_config_fingerprint(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("[情语] 写配置快照失败: %s", e)

    def _check_config_drift(self) -> list[str]:
        """启动时对比快照，找出被重置的关键项。

        只报告，不自动改——用户可能是有意改回默认值的。
        """
        import json as _json

        path = self._snapshot_file()
        if not path.exists():
            self._save_config_snapshot()
            return []
        try:
            old = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(old, dict):
            return []

        now = self._collect_config_fingerprint()
        drift: list[str] = []
        for key in self.SNAPSHOT_KEYS:
            if key in old and key in now and old[key] != now[key]:
                drift.append(f"{key}: {old[key]!r} → {now[key]!r}")

        old_engines = old.get("__engines__") or {}
        new_engines = now.get("__engines__") or {}
        for name, fields in old_engines.items():
            current = new_engines.get(name)
            if not isinstance(current, dict) or not isinstance(fields, dict):
                continue
            for field in ("voice", "singing_voice"):
                before, after = fields.get(field), current.get(field)
                if before and before != after:
                    drift.append(f"{name}.{field}: {before!r} → {after!r}")
        return drift

    async def _restore_config_snapshot(self) -> bool:
        """把快照里的值写回当前配置并持久化。"""
        import json as _json

        path = self._snapshot_file()
        if not path.exists():
            return False
        try:
            old = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(old, dict):
            return False

        for key in self.SNAPSHOT_KEYS:
            if key in old:
                self.config[key] = old[key]

        old_engines = old.get("__engines__") or {}
        for cfg in self._list_engines():
            name = _as_str(cfg.get("instance_name")) or _as_str(cfg.get("__template_key"))
            saved = old_engines.get(name)
            if not isinstance(saved, dict):
                continue
            for field in ("voice", "singing_voice"):
                if saved.get(field):
                    cfg[field] = saved[field]

        await self._persist_config()
        await self._rebuild_engines()
        self._save_config_snapshot()
        return True

    def _sync_engine_voice(self, engine) -> None:
        """把配置里的音色同步到引擎实例。

        引擎对象在构造时把 `voice` / `singing_voice` 抄成属性，之后改配置页不会
        自动同步——必须重载插件才生效。用户体感就是「音色切换不了、选了男声出来女声」。

        这里在每次合成前对齐一次：配置是唯一事实来源，实例跟着配置走。
        只同步音色这类「改了应当立刻生效」的字段，不碰引擎内部状态。
        """
        if engine is None:
            return
        name = getattr(engine, "instance_name", "")
        cfg = self._find_engine_config(name) if name else None
        if not isinstance(cfg, dict):
            return

        for attr, key in (
            ("voice", "voice"),
            ("singing_voice", "singing_voice"),
            ("audio_format", "format"),
            ("style_prompt", "style_prompt"),
        ):
            if key not in cfg:
                continue
            value = cfg.get(key)
            if value is None:
                continue
            try:
                current = getattr(engine, attr, None)
                if current != value:
                    setattr(engine, attr, value)
                    logger.info(
                        "[情语] 同步引擎配置 %s.%s: %r -> %r",
                        name,
                        attr,
                        current,
                        value,
                    )
            except Exception:  # noqa: BLE001 - 个别引擎不允许写属性
                pass

    def _apply_top_singing_switches(self, engine) -> None:
        """把顶层的「常驻唱歌」「演唱速度」写进引擎实例。

        这两项以前是 MiMo 实例的私有配置，换别的唱歌引擎就没有。
        现在放在插件顶层，任何引擎都能拿到。
        """
        if engine is None:
            return
        try:
            engine.singing_mode = bool(self.config.get("singing_resident_mode", False))
        except Exception:  # noqa: BLE001 - 个别引擎不允许写属性
            pass

        speed = _as_str(self.config.get("singing_speed")).strip() or "正常"
        try:
            engine.singing_speed = speed
        except Exception:  # noqa: BLE001
            pass
        # MiMo 两个引擎用 speed_hint 拼进演唱指令，这里一并同步
        try:
            engine.speed_hint = "" if speed == "正常" else speed
        except Exception:  # noqa: BLE001
            pass

    def _get_singing_engine(self, session_id: str, speech_engine):
        """唱歌用的引擎。

        与朗读引擎完全独立：
          - 配置里 singing_engine 留空 / 「跟随朗读引擎」→ 用朗读引擎
          - 填了其他实例名 → 用那个实例，朗读不受影响

        所以可以「MiMo 说话 + MiniMax 唱歌」，也可以反过来。
        目标引擎不支持唱歌时回退朗读引擎，并给出可读的原因。
        """
        raw = _as_str(self.config.get("singing_engine")).strip()
        if not raw or raw in _FOLLOW_ENGINE_VALUES:
            return speech_engine

        engine = self._cached_engines.get(raw)
        if engine is None:
            cfg = self._find_engine_config(raw)
            if cfg is None:
                logger.warning(
                    "[情语] 唱歌引擎 '%s' 不存在，本次改用朗读引擎 %s",
                    raw,
                    getattr(speech_engine, "instance_name", "?"),
                )
                return speech_engine
            try:
                engine = get_engine(cfg, self.config)
                self._cached_engines[raw] = engine
            except Exception as e:  # noqa: BLE001
                logger.error("[情语] 唱歌引擎 '%s' 创建失败: %s，回退朗读引擎", raw, e)
                return speech_engine

        if not getattr(engine, "SUPPORTS_SINGING", False):
            reason = getattr(engine, "SINGING_UNSUPPORTED_REASON", "该引擎不支持唱歌")
            logger.warning(
                "[情语] 唱歌引擎 %s 不可用（%s），本次改用朗读引擎 %s",
                raw,
                reason,
                getattr(speech_engine, "instance_name", "?"),
            )
            return speech_engine

        return engine

    # ---------- 唱歌模式 ----------

    def _singing_enabled(self) -> bool:
        """唱歌能力总开关。"""
        return bool(self.config.get("singing_enable", True))

    def _singing_prompt(self, kind: str = "") -> str:
        """构造注入给 LLM 的唱歌提示词，关闭时返回空串。

        常驻唱歌（singing_mode）会把 LLM 的**普通回复全文**当歌词唱——
        普通回复只有几十字，所以只唱十几秒。这里让常驻模式也拿到提示词，
        逼模型按完整副歌输出，否则时长永远上不去。

        `kind` 是这个请求来自群聊还是私聊：两边的目标时长可以不同，
        提示词要求多少字也就必须跟着不同，否则私聊设 40 秒、群聊设 120 秒会互相打架。
        """
        if not self._singing_enabled():
            return ""
        auto_intent = bool(self.config.get("singing_auto_intent", True))
        resident = bool(getattr(self._engine, "singing_mode", False))
        if not auto_intent and not resident:
            return ""
        target_chars, _, cps = self._singing_budget(kind)
        floor = max(120, int(target_chars * SINGING_PROMPT_FLOOR_RATIO))
        return SINGING_PROMPT_TEMPLATE.format(
            tag=DEFAULT_SINGING_TAG,
            chars=target_chars,
            floor=floor,
            seconds=int(target_chars / cps) if cps > 0 else self._singing_target_seconds(kind),
        )

    def _session_kind(self, session_id: str = "") -> str:
        """会话类型：group / private / unknown。

        UMO 形如 `default:FriendMessage:1799013357`、`default:GroupMessage:123456`，
        这里只做保守解析，认不出来就当 unknown（按全局配置处理）。
        """
        text = session_id or ""
        if "GroupMessage" in text:
            return "group"
        if "FriendMessage" in text:
            return "private"
        return "unknown"

    def _unified_mode(self) -> bool:
        """统一模式：开启后所有会话强制使用同一套配置与同一个引擎。

        这是「结构上不可能分叉」，而不是「清一次然后还能再改回去」：
        开启时 group/private 的覆盖项与换音色的会话绑定一律被忽略，
        只保留「某个会话禁用 TTS」这一种例外（那是开关，不是配置差异）。
        """
        return bool(self.config.get("singing_unified_mode", True))

    def _kind_config_key(self, base: str, kind: str) -> str:
        """按会话类型拼出配置键名，例如 singing_target_seconds → ..._group。"""
        return f"{base}_{kind}" if kind in ("group", "private") else base

    def _kind_override_int(self, base: str, kind: str, default: int) -> int:
        """读「群聊/私聊专用」的整数覆盖值；未填或 0 表示跟随全局。"""
        if kind in ("group", "private"):
            raw = self.config.get(self._kind_config_key(base, kind))
            if raw not in (None, ""):
                value = _as_int(raw, 0)
                if value > 0:
                    return max(SINGING_MIN_ALLOWED_SECONDS, min(SINGING_MAX_ALLOWED_SECONDS, value))
        return default

    def _kind_override_str(self, base: str, kind: str, default: str) -> str:
        """读「群聊/私聊专用」的字符串覆盖值；留空表示跟随全局。"""
        if kind in ("group", "private"):
            raw = _as_str(self.config.get(self._kind_config_key(base, kind)))
            if raw:
                return raw
        return default

    def _singing_target_seconds(self, kind: str = "") -> int:
        """单次演唱目标时长（秒）。

        统一模式下 kind 直接作废——群聊和私聊永远用同一个值。
        """
        raw = _as_int(
            self.config.get("singing_target_seconds"), SINGING_TARGET_SECONDS
        )
        base = max(SINGING_MIN_ALLOWED_SECONDS, min(SINGING_MAX_ALLOWED_SECONDS, raw))
        if self._unified_mode():
            return base
        return self._kind_override_int("singing_target_seconds", kind, base)

    def _singing_max_seconds(self, kind: str = "") -> int:
        """单次演唱硬上限（秒）：不允许比目标更小，否则一开口就被切断。"""
        target = self._singing_target_seconds(kind)
        raw = _as_int(self.config.get("singing_max_seconds"), SINGING_MAX_SECONDS)
        base = max(target, min(SINGING_MAX_ALLOWED_SECONDS, raw))
        if self._unified_mode():
            return base
        return self._kind_override_int("singing_max_seconds", kind, base)

    def _singing_budget(self, kind: str = "") -> tuple[int, int, float]:
        """演唱字数预算，返回 (目标字数, 硬上限字数, 每秒字数)。

        秒数由配置决定（群聊/私聊可各自覆盖），字数 = 秒数 × **实测语速**。
        原来这里的语速是写死的 3.0，比 MiMo 实际慢，于是「目标 40 秒」只算出 120 字，
        真唱出来只有二十几秒——这就是「设 40 秒却唱不满」的主因。
        """
        cps = self._singing_cps()
        target_chars = int(self._singing_target_seconds(kind) * cps)
        hard_chars = int(self._singing_max_seconds(kind) * cps)
        return target_chars, hard_chars, cps

    # ---------- 语速自校准 ----------

    def _load_calibration(self) -> None:
        """读取实测语速。文件不存在就用默认 3 字/秒。"""
        self._calibration = {}
        path = getattr(self, "_calibration_file", None)
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[情语] 读取语速校准失败，用默认值: {e}")
            return
        if isinstance(data, dict):
            self._calibration = data

    def _save_calibration(self) -> None:
        path = getattr(self, "_calibration_file", None)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._calibration, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[情语] 保存语速校准失败: {e}")

    def _singing_cps(self) -> float:
        """当前使用的每秒字数：配置手动覆盖 > 实测校准值 > 默认 3.0。"""
        override = _as_float(self.config.get("singing_chars_per_second"), 0.0)
        if override > 0:
            return max(SINGING_CPS_MIN, min(SINGING_CPS_MAX, override))
        measured = _as_float(self._calibration.get("cps"), 0.0)
        if measured > 0:
            return max(SINGING_CPS_MIN, min(SINGING_CPS_MAX, measured))
        return SINGING_CHARS_PER_SECOND

    def _update_calibration(self, chars: int, seconds: float) -> None:
        """用一次真实合成结果修正语速估计（指数平滑，抗单次异常）。"""
        if chars < SINGING_CALIBRATION_MIN_CHARS or seconds < SINGING_CALIBRATION_MIN_SECONDS:
            return
        # 手动填了语速就以手动值为准，不再自动校准
        if _as_float(self.config.get("singing_chars_per_second"), 0.0) > 0:
            return
        measured = chars / seconds
        if not (SINGING_CPS_MIN <= measured <= SINGING_CPS_MAX):
            logger.debug(
                "[情语] 语速测量 %.2f 字/秒超出合理区间（%s 字 / %.1f 秒），本次忽略",
                measured,
                chars,
                seconds,
            )
            return
        old = _as_float(self._calibration.get("cps"), SINGING_CHARS_PER_SECOND)
        new = old * SINGING_CPS_SMOOTHING + measured * (1 - SINGING_CPS_SMOOTHING)
        self._calibration = {
            "cps": round(new, 3),
            "samples": int(self._calibration.get("samples", 0)) + 1,
            "last_measured": round(measured, 3),
            "last_chars": chars,
            "last_seconds": round(seconds, 2),
        }
        self._save_calibration()
        logger.info(
            "[情语] 语速校准: %.2f 字/秒（本次实测 %.2f，%s 字 / %.1f 秒，样本 %s）",
            new,
            measured,
            chars,
            seconds,
            self._calibration["samples"],
        )

    def _calibrate_from_audio(self, text: str, audio_bytes: bytes, fmt: str) -> None:
        """合成完成后按音频真实时长校准语速（只有 wav 能算）。"""
        if (fmt or "").lower() != "wav":
            return
        seconds = _wav_duration_seconds(audio_bytes)
        if seconds <= 0:
            return
        chars = len(re.sub(r"\s+", "", text or ""))
        self._update_calibration(chars, seconds)
        last = self._last_sing_any if getattr(self, "_last_sing_any", None) else None
        if isinstance(last, dict):
            last["audio_seconds"] = round(seconds, 2)

    def _singing_clip_enabled(self) -> bool:
        """是否把歌词裁到目标时长。关掉就变成「模型写多少唱多少」。"""
        return bool(self.config.get("singing_clip_to_target", True))

    def _active_length_preset(self) -> str:
        """当前选中的「唱多久」预设；没选（或自定义过秒数）返回空串。

        配置里可能被手填成中文（短/中/长/很长），这里统一成内部 key。
        """
        raw = _as_str(self.config.get("singing_length_preset")).lower()
        raw = SINGING_LENGTH_ALIASES.get(raw, raw)
        return raw if raw in SINGING_LENGTH_PRESETS else ""

    def _preset_values(self, name: str) -> dict:
        """把「短/中/长/很长」一次展开成具体配置。

        普通用户只需要点一个词。展开内容包括目标时长、上限，以及顺手清掉
        群聊/私聊的时长覆盖——不然「点了统一还是两边不一样」会再出现一次。
        """
        seconds = SINGING_LENGTH_PRESETS[name][1]
        max_seconds = min(
            SINGING_MAX_ALLOWED_SECONDS, max(seconds + 10, int(seconds * 1.5))
        )
        return {
            "singing_length_preset": name,
            "singing_target_seconds": seconds,
            "singing_max_seconds": max_seconds,
            "singing_clip_to_target": True,
            "singing_target_seconds_private": 0,
            "singing_target_seconds_group": 0,
            "singing_max_seconds_private": 0,
            "singing_max_seconds_group": 0,
        }

    def _singing_mode_label(self, engine: TTSEngine | None) -> str:
        """当前实例实际走哪条唱歌链路，用于诊断「同一个引擎为什么时长不一样」。"""
        # 「参考项目写法」模式已下线；这里恒为 False，保留分支只为兼容旧实例
        if getattr(engine, "singing_reference_mode", False):
            return "参考(不裁)"
        if getattr(engine, "singing_legacy_mode", False):
            return "复古"
        return "默认"

    def _fit_singing_text(
        self, text: str, target_chars: int, hard_chars: int
    ) -> tuple[str, bool]:
        """把歌词裁到目标时长附近。

        以前只有「非参考链路」才裁：`singing_reference_mode=True` 的实例一个字都不裁，
        于是同一首歌在那个实例上会唱出整段（60~80 秒），另一个实例被裁到目标（40 秒）。
        现在三条链路走同一套预算，超预算才裁，并且只在行/句边界断开。
        """
        raw = (text or "").strip()
        if not raw or not self._singing_clip_enabled():
            return text, False
        flat = re.sub(r"\s+", "", raw)
        # 留 15% 余量：为了省两三秒砍掉最后一句，听感上得不偿失
        allow = int(target_chars * 1.15)
        if len(flat) <= allow:
            return text, False
        sliced, _ = slice_lyrics(raw, target_chars, "head")
        if len(re.sub(r"\s+", "", sliced)) > hard_chars:
            sliced, _ = slice_lyrics(sliced, hard_chars, "head")
        return sliced or raw, True

    def _fill_short_lyrics(self, text: str, target_chars: int) -> tuple[str, int]:
        """歌词明显短于目标时把整段再唱一遍，补足时长（默认关闭）。

        默认关是因为「同一段翻来覆去唱」很多人觉得难听（v3.14.0 就为此删过重复逻辑）。
        但如果你就是要「必须唱满」，打开 singing_fill_short 即可：
        最多补到 3 遍，不会无限循环。
        """
        if not bool(self.config.get("singing_fill_short", False)):
            return text, 1
        raw = (text or "").strip()
        flat = len(re.sub(r"\s+", "", raw))
        if not raw or flat < 20:
            return text, 1
        if flat >= target_chars * SINGING_PROMPT_FLOOR_RATIO:
            return text, 1
        repeat = 2
        while repeat < 3 and flat * repeat < target_chars * SINGING_PROMPT_FLOOR_RATIO:
            repeat += 1
        return "\n".join([raw] * repeat), repeat

    def _remember_singing(
        self,
        session_id: str,
        text: str,
        engine: TTSEngine | None,
        cps: float,
        truncated: bool,
        kind: str = "",
    ) -> None:
        """记下这次实际送出去的字数与估算时长，供 /tts_status 对比两个会话。"""
        flat = len(re.sub(r"\s+", "", text or ""))
        meta = {
            "chars": flat,
            "seconds": estimate_seconds(text, cps),
            "mode": self._singing_mode_label(engine),
            "engine": getattr(engine, "instance_name", "?"),
            "truncated": truncated,
            "kind": kind or self._session_kind(session_id),
        }
        self._last_sing_meta[session_id] = meta
        self._last_sing_any = dict(meta)

    # ---------- 禁止唱歌时段 ----------

    def _quiet_windows(self, kind: str = "") -> list[tuple[int, int]]:
        """解析「23:00-07:00,12:00-13:30」为分钟区间列表。

        群聊和私聊可以各自配置：singing_quiet_hours_group / _private，
        留空就跟随全局 singing_quiet_hours。
        """
        if not bool(self.config.get("singing_quiet_hours_enable", False)):
            return []
        base = _as_str(self.config.get("singing_quiet_hours")) or DEFAULT_SINGING_QUIET_HOURS
        if self._unified_mode():
            raw = base
        else:
            raw = self._kind_override_str("singing_quiet_hours", kind, base)
        windows, bad = _parse_quiet_windows(raw)
        for chunk in bad:
            logger.warning(f"[情语] 已忽略无法解析的禁止唱歌时段片段: {chunk!r}")
        return windows

    def _singing_quiet_reason(self, kind: str = "") -> str:
        """当前是否处于禁止唱歌时段；是则返回给用户看的提示，否则返回空串。"""
        windows = self._quiet_windows(kind)
        if not windows:
            return ""
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        for start, end in windows:
            inside = (start <= minute < end) if start < end else (minute >= start or minute < end)
            if inside:
                pretty = ", ".join(
                    f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"
                    for s, e in windows
                )
                scope = {"group": "群聊", "private": "私聊"}.get(kind, "全局")
                return f"现在是休息时段（{scope}禁止唱歌: {pretty}），换个时间再点歌吧"
        return ""

    def _singing_keywords(self) -> tuple[str, ...]:
        raw = _as_str(self.config.get("singing_intent_keywords"))
        if not raw or raw in LEGACY_SINGING_KEYWORDS:
            # 老配置自动升级到带单字「唱」的新默认，否则升级后唱不出来
            raw = DEFAULT_SINGING_KEYWORDS
        return tuple(item for item in re.split(r"[,，、;；\s]+", raw) if item)

    def _wants_singing_from_message(self, event: AstrMessageEvent) -> bool:
        """用户这句话里是否明确要求唱歌（不依赖 LLM 是否打标签）。"""
        raw = ""
        for obj in (getattr(event, "message_obj", None), event):
            if obj is None:
                continue
            value = getattr(obj, "message_str", "") or ""
            if value:
                raw = value
                break
        if not raw:
            return False
        return any(keyword in raw for keyword in self._singing_keywords())

    def _supports_singing(self, engine: TTSEngine | None) -> bool:
        return bool(engine is not None and getattr(engine, "SUPPORTS_SINGING", False))

    def _resolve_singing_reply(self, text: str) -> tuple[bool, str, str]:
        """解析唱歌意图，返回 (是否唱歌, 歌词, 尾注)。

        即使唱歌功能被关闭，也会剥离唱歌标签：否则 <sing> 标记会被当成正文念出来。
        """
        singing, lyrics, tail = split_singing_reply(text, DEFAULT_SINGING_TAG)
        if not self._singing_enabled():
            return False, lyrics, tail
        return singing, lyrics, tail

    # ---------- 语气与角色气质（内部使用，不对外暴露配置） ----------

    async def _persona_text(self, event: AstrMessageEvent) -> str:
        """取当前会话的人设文本。

        优先用 on_llm_request 缓存的那次系统提示词——那是这一次请求**实际用的**人设，
        比再去 persona_manager 查一遍更准。

        但缓存只在本事件内有效：改了人设之后，AstrBot 会重新组装系统提示词，
        新事件里的缓存自然就是新内容。所以这里**不看任何跨事件缓存**，
        否则改完人设得清消息记录才生效。
        """
        cached = getattr(event, "_qinyu_persona", "")
        if isinstance(cached, str) and cached.strip():
            return cached.strip()

        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        for method_name in ("get_default_persona_v3", "get_default_persona"):
            getter = getattr(manager, method_name, None)
            if not callable(getter):
                continue
            try:
                result = getter(_get_session_id(event))
                if inspect.isawaitable(result):
                    result = await result
            except Exception as e:  # noqa: BLE001 - 不同 AstrBot 版本 API 有差异
                logger.debug(f"[情语] 读取人设失败 ({method_name}): {e}")
                continue
            text = ""
            if isinstance(result, str):
                text = result
            elif isinstance(result, dict):
                for key in ("prompt", "system_prompt", "persona", "persona_text"):
                    value = result.get(key)
                    if isinstance(value, str) and value.strip():
                        text = value
                        break
            if text.strip():
                return text.strip()[:1200]
        return ""

    def _last_singing_summary(self, event: AstrMessageEvent) -> str:
        """展示最近一次实际发给 TTS 的唱歌文本与时长诊断。

        文本链路的清洗逻辑改过很多版，有了这几行就不用靠猜：
        真发出去的 `(唱歌)…` 是什么样、多少字、估算几秒、走的哪条链路，直接看得到。
        """
        session_id = _get_session_id(event)
        kind = self._session_kind(session_id)
        text = (self._last_tts_text.get(session_id) or "").strip()
        if not text:
            return "最近一次唱歌: （本次会话还没有）"
        preview = text.replace("\n", " / ")
        if len(preview) > 60:
            preview = preview[:60] + "…"
        meta = self._last_sing_meta.get(session_id)
        if not meta:
            return f"最近一次唱歌文本: {preview}"
        scope = {"group": "群聊", "private": "私聊"}.get(meta.get("kind") or kind, "未知会话")
        line = (
            f"最近一次唱歌({scope}): {meta['chars']} 字 ≈ {meta['seconds']:.0f} 秒"
            f"（目标 {self._singing_target_seconds(kind)} 秒，链路 {meta['mode']}，"
            f"引擎 {meta['engine']}{'，已裁到目标' if meta['truncated'] else '，未裁剪'}）"
        )
        return f"{line}\n最近一次唱歌文本: {preview}"

    def _session_diag(self, event: AstrMessageEvent) -> list[str]:
        """当前会话的配置来源，用来排查「私聊和群聊行为不一致」。"""
        session_id = _get_session_id(event)
        kind = self._session_kind(session_id)
        bound = self._session_bindings.get(session_id)
        engine = self._get_engine_for_session(session_id)
        scope = {"group": "群聊", "private": "私聊"}.get(kind, "未知类型")
        lines = [f"会话 UMO: {session_id or '（空）'}（{scope}）"]
        if bound == DISABLED_ENGINE:
            lines.append("会话绑定: 已禁用 TTS")
        elif bound:
            lines.append(f"会话绑定: {bound}（用的是这个实例，不是全局）")
        else:
            lines.append("会话绑定: 无（跟随全局引擎）")
        if engine is not None:
            name = getattr(engine, "instance_name", "?")
            lines.append(f"实际引擎: {name}")
            target_s = self._singing_target_seconds(kind)
            max_s = self._singing_max_seconds(kind)
            override = ""
            if kind in ("group", "private"):
                own = self.config.get(self._kind_config_key("singing_target_seconds", kind))
                if own not in (None, "") and _as_int(own, 0) > 0:
                    override = f"（{scope}专用）"
            lines.append(f"唱歌时长: 目标 {target_s} 秒 / 上限 {max_s} 秒{override}")
            quiet = self._singing_quiet_reason(kind)
            if bool(self.config.get("singing_quiet_hours_enable", False)):
                window = self._kind_override_str(
                    "singing_quiet_hours",
                    kind,
                    _as_str(self.config.get("singing_quiet_hours")),
                ) or "未填写"
                lines.append(
                    f"禁止唱歌时段: {window}"
                    f"（当前{'生效中' if quiet else '不在时段内'}）"
                )
            else:
                lines.append("禁止唱歌时段: 未启用")
            lines.append(
                f"唱歌模式: 复古={'开' if getattr(engine, 'singing_legacy_mode', False) else '关'} "
                f"/ 参考={'开' if getattr(engine, 'singing_reference_mode', False) else '关'} "
                f"/ 常驻={'开' if getattr(engine, 'singing_mode', False) else '关'}"
            )
            lines.append(
                f"音色: 朗读={getattr(engine, 'voice', '?')} 唱歌={getattr(engine, 'singing_voice', '') or '跟随朗读'}"
            )
        else:
            lines.append("实际引擎: 无（该会话已禁用）")
        # 人设：私聊和群聊的人设通常不同，这是「说话语气不一样」的来源
        persona = getattr(event, "_qinyu_persona", "")
        lines.append(f"人设来源: {'已缓存系统提示词' if persona else '未取到（用插件默认）'}")
        lines.append(f"会话绑定总数: {len(self._session_bindings)}")
        if self._session_bindings:
            preview = list(self._session_bindings.items())[:5]
            lines.append(
                "绑定明细: " + "；".join(f"{k.split(':')[-2] if ':' in k else k}->{v}" for k, v in preview)
            )
        return lines

    async def _speech_tone(self, event: AstrMessageEvent, text: str = "") -> str:
        """生成朗读语气。

        优先级：人设里的声音特征 → 回复内容本身 → 通用自然语气。
        不限定角色类型：任何人设、以及完全没有设人设的场合，都能给出一句合适的语气。
        唱歌链路不用它，保持官方原版。
        """
        if not bool(self.config.get("voice_tone_enable", False)):
            # 默认关闭：纯语音与项目原始实现完全一致，不做任何语气处理
            return ""
        persona = await self._persona_text(event)
        return build_speech_tone(extract_persona_traits(persona), text)

    async def initialize(self):
        """插件初始化：创建引擎实例"""
        if not self.config.get("enable", True):
            logger.info("[情语] 插件已禁用，跳过初始化")
            return

        # 配置漂移检测：插件更新过 schema 后，AstrBot 可能按新默认值重生成配置，
        # 用户改过的音色会悄悄回到默认（冰糖=女声）。这里把差异摆出来。
        try:
            drift = self._check_config_drift()
            if drift:
                logger.warning(
                    "[情语] 检测到配置被重置（可能与插件更新有关）：%s。"
                    "发 /tts_restore 可恢复上次的值",
                    "；".join(drift),
                )
                self._config_drift = drift
        except Exception as e:  # noqa: BLE001
            logger.debug("[情语] 配置漂移检测失败: %s", e)

        # 加载会话绑定
        self._load_bindings()
        self._load_runtime_state()
        self._load_calibration()
        self._cleanup_temp_files(force=True)

        # 获取引擎配置列表
        engines_config: list[dict] = self.config.get("engines", [])
        if not engines_config:
            logger.warning("[情语] 未配置任何 TTS 引擎，插件不会生效")
            return

        # 根据 active_engine 选择生效的全局引擎
        active_engine_name = _as_str(self.config.get("active_engine"))
        engine_config = None

        if active_engine_name == DISABLED_ENGINE:
            logger.info("[情语] 已启用，全局默认 TTS 已关闭")
            if self._session_bindings:
                logger.info(f"[情语] 已加载 {len(self._session_bindings)} 个会话绑定")
            return

        if active_engine_name:
            for ec in engines_config:
                if ec.get("instance_name", "") == active_engine_name:
                    engine_config = ec
                    break
            if engine_config is None:
                logger.warning(
                    f"[情语] 未找到名为 '{active_engine_name}' 的引擎，将使用第一个"
                )

        if engine_config is None:
            engine_config = engines_config[0]

        template_key = engine_config.get("__template_key", "")
        instance_name = _as_str(engine_config.get("instance_name")) or template_key

        try:
            self._engine = get_engine(engine_config, self.config)
        except Exception as e:
            logger.error(
                f"[情语] 创建引擎失败 (类型: {template_key}, 实例: {instance_name}): "
                f"{type(e).__name__}: {e}"
            )
            return

        logger.info(f"[情语] 已启用，全局引擎: {instance_name} (类型: {template_key})")
        # 记录当前生效配置，供下次启动比对
        self._save_config_snapshot()
        # 把实际音色打进日志：出问题时一眼看出「配置读到的是什么」
        try:
            _v = getattr(self._engine, "voice", "")
            _sv = getattr(self._engine, "singing_voice", "")
            logger.info(
                "[情语] 音色生效：朗读=%s 唱歌=%s 单条语音=%s",
                _v or "(未设)",
                _sv or "跟随朗读",
                "开" if bool(self.config.get("tts_single_message", True)) else "关",
            )
        except Exception:  # noqa: BLE001
            pass
        # 打印实际生效的唱歌配置，便于排查「关键词没生效 / 时长没变」
        keywords = self._singing_keywords()
        target_chars, hard_chars, _ = self._singing_budget()
        logger.info(
            "[情语] 唱歌配置: 自动=%s, 关键词 %s 项(%s...), 目标 %s 秒(%s 字)/上限 %s 秒, 常驻唱歌=%s",
            "开" if bool(self.config.get("singing_auto_intent", True)) else "关",
            len(keywords),
            "、".join(keywords[:6]),
            SINGING_TARGET_SECONDS,
            target_chars,
            SINGING_MAX_SECONDS,
            "开" if getattr(self._engine, "singing_mode", False) else "关",
        )
        if getattr(self._engine, "singing_mode", False) and not self._supports_singing(self._engine):
            reason = getattr(self._engine, "SINGING_UNSUPPORTED_REASON", "当前引擎不支持唱歌模式")
            logger.warning(
                f"[情语] 引擎 {instance_name} 开启了常驻唱歌，但{reason}；"
                "该实例会静默退回普通语音合成"
            )
        if self._session_bindings:
            logger.info(f"[情语] 已加载 {len(self._session_bindings)} 个会话绑定")

    @filter.on_decorating_result(priority=0)
    async def tts_hook(self, event: AstrMessageEvent):
        """钩子：在消息发送前将文本转为语音"""
        if not self.config.get("enable", True):
            return
        if not self._runtime_enabled:
            return
        if not await self._is_astrbot_tts_enabled(event):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        if not result.is_llm_result():
            return

        # 收集所有文本
        text_parts = []
        for component in result.chain:
            if isinstance(component, Comp.Plain) and component.text:
                text_parts.append(component.text)

        if not text_parts:
            return

        full_text = "".join(text_parts).strip()
        if len(full_text) < 1:
            return

        # 唱歌意图解析：LLM 可用 <sing>歌词</sing> 标记，或文本本身以 (唱歌) 开头
        # 唱歌意图解析：模型用 <sing>歌词</sing> 标记，标签之后的文字作为尾注保留
        singing, sing_text, tail_text = self._resolve_singing_reply(full_text)
        if singing and len(re.sub(r"\s+", "", sing_text)) < 30:
            # 标签里只有歌名或一句话：唱出来只有几秒，几乎肯定是 LLM 没写歌词
            logger.warning(
                "[情语] 歌词只有 %s 字（%r）：模型可能在标签里只写了歌名。"
                "想唱满 40 秒，请让它输出完整副歌，或用 /tts_sing <完整歌词>",
                len(re.sub(r"\s+", "", sing_text)),
                sing_text[:30],
            )

        # 兜底：用户明确说了要唱歌，但模型没打标签时，把回复里的歌词部分拿去唱
        if (
            not singing
            and self._singing_enabled()
            and bool(self.config.get("singing_auto_intent", True))
            and self._wants_singing_from_message(event)
        ):
            candidate = guess_lyrics_from_reply(full_text)
            if candidate:
                singing = True
                sing_text = candidate
                tail_text = ""
                logger.info("[情语] 用户要求唱歌且模型未标记，按歌词合成")
            elif looks_like_refusal(full_text):
                logger.warning("[情语] 模型拒绝了唱歌请求，本次按普通语音处理")

        # 获取当前会话应使用的引擎
        session_id = _get_session_id(event)
        engine = self._get_engine_for_session(session_id)
        if engine is None:
            return

        # 常驻唱歌：该实例的所有输出（含普通回答）都以唱歌形式发出
        if not singing and self._singing_enabled() and getattr(engine, "singing_mode", False):
            singing = True
            sing_text = full_text
            tail_text = ""
            logger.info("[情语] 引擎处于常驻唱歌模式，本条回复按唱歌合成")

        # 配置的引擎没起来时，_get_engine_for_session 会悄悄退回全局引擎。
        # 对克隆音色来说那是致命的：用户听到的是另一个人的声音，还不知道为什么。
        # 这里直接拦下来，改用文字，绝不换音色。
        # 合成前把音色对齐配置，改完配置页立即生效，不必重载插件
        if engine is not None:
            self._sync_engine_voice(engine)

        if not singing and engine is not None:
            configured_name = _as_str(self.config.get("active_engine")).strip()
            actual_name = getattr(engine, "instance_name", "")
            if (
                configured_name
                and configured_name not in (DISABLED_ENGINE, *_FOLLOW_ENGINE_VALUES)
                and actual_name
                and actual_name != configured_name
            ):
                logger.warning(
                    "[情语] 朗读引擎降级：配置是 %s，实际拿到 %s。"
                    "为避免用别的音色念出来，本次改发文字。请 /tts_doctor 检查该引擎为何没起来",
                    configured_name,
                    actual_name,
                )
                return

        # 唱歌走独立的引擎：可以不与朗读同源
        if singing:
            singing_engine = self._get_singing_engine(session_id, engine)
            self._apply_top_singing_switches(singing_engine)
            if singing_engine is not engine:
                logger.info(
                    "[情语] 唱歌使用独立引擎：%s（朗读仍是 %s）",
                    getattr(singing_engine, "instance_name", "?"),
                    getattr(engine, "instance_name", "?"),
                )
                engine = singing_engine

        if singing and not self._supports_singing(engine):
            reason = getattr(engine, "SINGING_UNSUPPORTED_REASON", "当前引擎不支持唱歌模式")
            logger.warning(f"[情语] {reason}，已回退普通语音合成")
            singing = False

        # 禁止唱歌时段：命中就明确告诉用户，不要静默退回朗读（否则听感像「唱坏了」）。
        # 时段按会话类型分别判定：群聊和私聊可以各配一套，也可以都跟随全局。
        kind = self._session_kind(session_id)
        if singing:
            quiet_reason = self._singing_quiet_reason(kind)
            if quiet_reason:
                logger.info(f"[情语] 命中禁止唱歌时段，本次不演唱: {quiet_reason}")
                result.chain = [Comp.Plain(quiet_reason)]
                return

        # TTS 触发概率；唱歌是明确请求，固定不受概率影响
        try:
            probability = int(self.config.get("tts_probability", 100))
        except (TypeError, ValueError):
            probability = 100
        if probability <= 0:
            # 0 表示彻底关闭自动 TTS，唱歌一并停止
            return
        if not singing and probability < 100 and random.randint(1, 100) > probability:
            return

        # 字数上限；唱歌需要完整歌词，固定豁免
        if not singing and self.config.get("tts_text_limit_enable", False):
            limit = _as_int(self.config.get("tts_text_limit", 50), 50)
            if len(full_text) > limit:
                logger.debug(
                    f"[情语] 文本字数 ({len(full_text)}) 超过上限 ({limit})，跳过 TTS"
                )
                return

        # 唱歌只做「超长兜底切片」，不做歌词体化清洗。
        # 早期版本（还没拆两条链路时）就是原样送歌词，实测比清洗后更好：
        # 清洗会删标点、拆句子，反而破坏旋律连贯性。字数没超就一个字不动。
        sing_truncated = False
        sing_cps = SINGING_CHARS_PER_SECOND
        if singing:
            target_chars, hard_chars, cps = self._singing_budget(kind)
            sing_cps = cps
            # 歌词不够长时先补足（只在 singing_fill_short 打开时），再按预算裁
            sing_text, fill_repeat = self._fill_short_lyrics(sing_text, target_chars)
            if fill_repeat > 1:
                logger.info(
                    "[情语] 歌词不足目标 %s 字，重复 %s 遍补足时长",
                    target_chars,
                    fill_repeat,
                )
            # 三条链路统一走预算：参考/复古/默认都不许绕过 target 与 max，
            # 否则同一个 target 会在不同实例上唱出 20 秒和 80 秒两种结果。
            sliced, sing_truncated = self._fit_singing_text(
                sing_text, target_chars, hard_chars
            )
            _flat = re.sub(r"\s+", "", sliced)
            # 阈值跟着配置走：以前硬编码 90 字，把目标调到 120 秒后这条警告就再也不会响
            short_threshold = max(30, min(90, int(target_chars * 0.5)))
            if len(_flat) < short_threshold:
                source = (
                    "常驻唱歌模式把普通回复当歌词"
                    if getattr(engine, "singing_mode", False)
                    else "模型只写了很短的几句"
                )
                logger.warning(
                    "[情语] 歌词偏短（%s 字，约 %s 秒，目标 %s 秒）：%s。"
                    "要更长可以 /tts_sing 直接贴完整一段，或确认提示词已注入（/tts_status 可查）",
                    len(_flat),
                    estimate_seconds(sliced, cps),
                    self._singing_target_seconds(kind),
                    source,
                )
            if sing_truncated:
                logger.info(
                    f"[情语] 歌词超出目标已裁到约 {estimate_seconds(sliced, cps):.0f} 秒"
                    f"（目标 {self._singing_target_seconds(kind)} 秒 / 上限 {self._singing_max_seconds(kind)} 秒，"
                    f"会话 {kind or '未知'}，链路 {self._singing_mode_label(engine)}）"
                )
            sing_text = sliced

        # 播报文本始终用解析后的文本：唱歌时是歌词，非唱歌时是剥离标签后的原文
        speak_text = sing_text

        # 朗读语气只在纯语音链路上使用，且默认关闭（= 原项目行为）。
        # 唱歌链路彻底不读它，避免两边互相影响
        if not singing:
            try:
                engine.voice_tone = await self._speech_tone(event, speak_text)
            except Exception as e:  # noqa: BLE001 - 第三方引擎可能不允许写属性
                logger.debug(f"[情语] 注入语气失败: {e}")
            logger.debug(
                f"[情语] 朗读语气: {engine.voice_tone or '(关闭，使用原项目行为)'}"
            )

        # 合成语音
        try:
            if singing:
                _, hard_chars_now, _ = self._singing_budget(kind)
                warning = singing_length_warning(
                    speak_text,
                    max_chars=hard_chars_now,
                    truncates=not getattr(engine, "singing_reference_mode", False),
                )
                if warning:
                    logger.warning(f"[情语] {warning}")
                logger.debug(
                    f"[情语] 开始合成唱歌 (引擎: {engine.instance_name}): {speak_text[:50]}..."
                )
                if getattr(engine, "singing_busy", False):
                    # hook 里不能 yield：把本条回复换成排队提示再退出，
                    # 否则用户在群里就是「发出去 → 最长 120 秒无声」，会以为唱歌坏了
                    logger.info("[情语] 唱歌实例繁忙，本次改为排队提示")
                    result.chain = [Comp.Plain("上一首还在唱，我排队接着唱，稍等～")]
                    return
                audio_bytes, fmt = await engine.synthesize_singing(speak_text)
            else:
                logger.debug(
                    f"[情语] 开始合成 (引擎: {engine.instance_name}): {speak_text[:50]}..."
                )
                audio_bytes, fmt = await engine.synthesize(speak_text)
        except Exception as e:
            logger.error(
                f"[情语] TTS 合成失败 (引擎: {engine.instance_name}): "
                f"{type(e).__name__}: {e}"
            )
            # 失败一律退回文字，而且**只留一条文字**。
            # 之前只有唱歌分支做了清理，普通朗读失败时 chain 里还是原来那几个组件，
            # 分段回复会把它逐条发出去，看起来就像「语音被切成好几段」。
            fallback_text = (
                (sing_text or "（这段没唱出来，稍后再试）")
                if singing
                else (full_text or "")
            )
            if bool(self.config.get("tts_single_message", True)):
                result.chain = [Comp.Plain(fallback_text)] if fallback_text else []
            elif singing:
                # 关掉「单条语音」时保持老行为：歌词替换掉原来的 Plain
                new_chain: list = []
                inserted = False
                for component in result.chain:
                    if isinstance(component, Comp.Plain):
                        if not inserted:
                            new_chain.append(
                                Comp.Plain(fallback_text or "（这段没唱出来，稍后再试）")
                            )
                            inserted = True
                        continue
                    new_chain.append(component)
                result.chain = new_chain
            return

        # 保存音频文件
        audio_path = self._temp_dir / f"tts_{uuid.uuid4().hex}.{fmt}"
        audio_path.write_bytes(audio_bytes)
        self._cleanup_temp_files()

        # 替换消息链中的文本部分为语音，非文本组件保持原有顺序。
        audio_path_str = str(audio_path)
        self._tts_audio_text[audio_path_str] = speak_text
        self._last_tts_text[session_id] = speak_text
        if singing:
            self._remember_singing(session_id, speak_text, engine, sing_cps, sing_truncated, kind)
            # 用这次音频的真实时长校准语速，下次的字数预算就准了
            self._calibrate_from_audio(speak_text, audio_bytes, fmt)
        kind = "唱歌语音" if singing else "语音"
        logger.info(f"[情语] 发送{kind} ({engine.instance_name}): {speak_text[:80]}")
        record = Comp.Record(file=audio_path_str, url=audio_path_str)

        # 「一条回复只发一条语音」。
        # AstrBot 的 RespondStage 在分段模式下会**按 chain 里的组件逐个发送**，
        # 所以只要 chain 里还剩图片、引用、@ 这类组件，就会被拆成好几条消息发出去。
        # 开启这个开关后，合成成功时把 chain 压成单一 Record，
        # 无论平台是否开了分段回复，都只会发出一条语音。
        if bool(self.config.get("tts_single_message", True)):
            dropped = max(0, len(result.chain) - 1)
            result.chain = [record]
            if dropped:
                logger.debug("[情语] 单条语音模式：丢弃 %s 个附带组件（图片/引用/@）", dropped)
            if tail_text:
                logger.info("[情语] 已丢弃唱歌标签外的 %s 字", len(tail_text))
            return

        new_chain = []
        inserted_record = False
        for component in result.chain:
            if isinstance(component, Comp.Plain):
                if component.text and not inserted_record:
                    new_chain.append(record)
                    inserted_record = True
                continue
            new_chain.append(component)
        # 标签之外的话**直接丢弃**：模型常在唱完后加一句吐槽，既拉长语音又破坏节奏。
        # 早期版本会把这段当普通文字发出去，属于"话痨"来源之一，现已取消
        if tail_text:
            logger.info("[情语] 已丢弃唱歌标签外的 %s 字", len(tail_text))
        result.chain = new_chain

    @_optional_hook("on_llm_request")
    async def tts_sing_prompt(self, event: AstrMessageEvent, req=None):
        """在 LLM 请求前注入唱歌提示词，并顺手缓存人设供合成时生成语气。"""
        if req is None:
            return
        system_prompt = getattr(req, "system_prompt", None)
        # 缓存人设：语气由插件内部消化，不展示、也不做成配置项
        if isinstance(system_prompt, str) and system_prompt.strip():
            try:
                setattr(event, "_qinyu_persona", system_prompt.strip()[:1200])
            except Exception:  # noqa: BLE001
                pass

        # 提示词按「这条消息来自群聊还是私聊」取对应预算，两块设定才不会互相覆盖
        prompt = self._singing_prompt(self._session_kind(_get_session_id(event)))
        if not prompt:
            return
        try:
            if isinstance(system_prompt, str):
                if prompt in system_prompt:
                    return
                req.system_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
                return
            if system_prompt is None:
                req.system_prompt = prompt
                return
        except Exception as e:  # noqa: BLE001 - 注入失败不应影响正常对话
            logger.debug(f"[情语] 注入唱歌提示词失败: {e}")
            return
        try:
            base_prompt = getattr(req, "prompt", None)
            if isinstance(base_prompt, str) and prompt not in base_prompt:
                req.prompt = f"{base_prompt}\n\n{prompt}"
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[情语] 追加唱歌提示词失败: {e}")

    # 这里必须走 _optional_hook：直接写 @filter.on_waiting_llm_request() 时，
    # 只要 AstrBot 版本没有这个钩子，import 阶段就 AttributeError，整个插件加载失败
    # （与 _optional_hook 的降级设计自相矛盾）。
    @_optional_hook("on_waiting_llm_request")
    async def tts_reply_fix(self, event: AstrMessageEvent):
        """修复引用语音消息时因 NapCat 临时文件已被清除而导致 'not a valid file' 报错的问题。
        在 LLM 请求构建之前，将 Reply 链中的 Record 替换为缓存的文本。"""
        session_id = _get_session_id(event)
        session_last_text = self._last_tts_text.get(session_id, "")

        for comp in event.get_messages():
            if not isinstance(comp, Comp.Reply) or not comp.chain:
                continue
            new_chain = []
            modified = False
            for reply_comp in comp.chain:
                if isinstance(reply_comp, Comp.Record):
                    # 优先按文件路径匹配，其次使用会话最近一次文本
                    cached = None
                    file_path = getattr(reply_comp, "file", "") or getattr(reply_comp, "url", "")
                    if file_path:
                        cached = self._tts_audio_text.get(file_path)
                    if not cached and session_last_text:
                        cached = session_last_text

                    if cached:
                        new_chain.append(Comp.Plain(cached))
                        logger.debug(
                            f"[情语] 已将引用语音替换为缓存文本: {cached[:30]}..."
                        )
                    else:
                        new_chain.append(Comp.Plain("[语音消息]"))
                        logger.debug(
                            "[情语] 未找到缓存文本，使用占位符替代引用语音"
                        )
                    modified = True
                else:
                    new_chain.append(reply_comp)
            if modified:
                comp.chain = new_chain

    def _list_engines(self) -> list[dict]:
        """获取已配置的引擎列表"""
        return self.config.get("engines", []) or []

    def _format_binding_name(self, name: str) -> str:
        """格式化绑定名称。"""
        return "禁用 TTS" if name == DISABLED_ENGINE else name

    def _global_engine_name(self) -> str:
        if _as_str(self.config.get("active_engine")) == DISABLED_ENGINE:
            return "关闭"
        return self._engine.instance_name if self._engine else "(未初始化)"

    def _session_tts_status(self, session_id: str) -> str:
        binding = self._session_bindings.get(session_id)
        if binding == DISABLED_ENGINE:
            return "已禁用 TTS"
        if binding:
            if self._unified_mode():
                return f"绑定 {binding}（统一模式下已忽略）"
            return f"绑定 {binding}"
        return f"跟随全局 {self._global_engine_name()}"

    def _format_instance(self, idx: int, ec: dict, session_id: str = "") -> str:
        """格式化引擎条目"""
        name = (ec.get("instance_name") or "").strip() or ec.get("__template_key", "unknown")
        ttype = ec.get("__template_key", "")
        markers = []
        if self._engine and _as_str(self.config.get("active_engine")) != DISABLED_ENGINE and self._engine.instance_name == name:
            markers.append("全局")
        if session_id and self._session_bindings.get(session_id) == name:
            markers.append("本会话")
        marker = f" ← {'|'.join(markers)}" if markers else ""
        sing_marker = " [唱歌]" if ttype in SINGING_TEMPLATE_KEYS else ""
        return f"  {idx}. {name} ({ttype}){marker}{sing_marker}"

    @filter.command("tts_switch")
    async def switch_engine(self, event: AstrMessageEvent):
        """[管理员] 切换全局 TTS 引擎。用法: /tts_switch <序号或实例名>"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        args = _get_cmd_args(event)
        engines_config = self._list_engines()
        session_id = _get_session_id(event)

        if not args:
            if not engines_config:
                yield event.plain_result("未配置任何 TTS 引擎")
                return
            lines = [
                f"TTS 总开关: {'开启' if self._runtime_enabled else '关闭'}",
                f"全局默认引擎: {self._global_engine_name()}",
                self._session_tts_status(session_id),
                "",
                "当前配置的 TTS 引擎：",
                "  0. 关闭全局默认 TTS",
            ]
            for i, ec in enumerate(engines_config, 1):
                lines.append(self._format_instance(i, ec, session_id))
            lines.append("\n用法: /tts_switch <序号或实例名>，/tts_switch 0 关闭全局默认 TTS")
            yield event.plain_result("\n".join(lines))
            return

        if args == "0":
            if self._engine:
                await self._engine.close()
            self._engine = None
            self.config["active_engine"] = DISABLED_ENGINE
            logger.info("[情语] 已关闭全局默认 TTS")
            yield event.plain_result("已关闭全局默认 TTS。已绑定具体引擎的会话不受影响，使用 /tts_off 可暂停全部会话。")
            return

        target_config = self._find_engine_config(args)
        if target_config is None:
            yield event.plain_result(
                f"未找到 '{args}' 对应的引擎。使用 /tts_switch 查看可用列表。"
            )
            return

        try:
            new_engine = get_engine(target_config, self.config)
        except Exception as e:
            yield event.plain_result(f"创建引擎失败: {e}")
            return

        if self._engine:
            await self._engine.close()
        self._engine = new_engine
        name = self._engine.instance_name
        # 同步更新配置中的 active_engine 字段
        self.config["active_engine"] = name
        logger.info(f"[情语] 已切换全局引擎: {name}")
        yield event.plain_result(f"已切换全局 TTS 引擎为: {name}")

    @filter.command("tts_bind")
    async def bind_engine(self, event: AstrMessageEvent):
        """为当前会话绑定 TTS 引擎。用法: /tts_bind <序号或实例名>"""
        if not _is_slash(event):
            return
        args = _get_cmd_args(event)
        session_id = _get_session_id(event)

        if not session_id:
            yield event.plain_result("无法获取当前会话标识，绑定失败")
            return

        if not args:
            # 显示当前会话绑定状态
            bound = self._session_bindings.get(session_id)
            if bound == DISABLED_ENGINE:
                yield event.plain_result(
                    "当前会话已禁用 TTS\n"
                    "使用 /tts_unbind 回退全局默认\n"
                    "使用 /tts_bind <序号或实例名> 绑定具体引擎"
                )
            elif bound:
                yield event.plain_result(
                    f"当前会话已绑定引擎: {bound}\n"
                    f"使用 /tts_unbind 解除绑定\n"
                    f"使用 /tts_bind <序号或实例名> 更换绑定"
                )
            else:
                yield event.plain_result(
                    "当前会话未绑定引擎，使用全局默认\n"
                    "使用 /tts_bind <序号或实例名> 绑定"
                )
            return

        if args == "0":
            self._session_bindings[session_id] = DISABLED_ENGINE
            self._save_bindings()
            logger.info(f"[情语] 会话 {session_id[:20]}... 已禁用 TTS")
            yield event.plain_result("已为当前会话禁用 TTS。使用 /tts_unbind 可回退全局默认。")
            return

        target_config = self._find_engine_config(args)
        if target_config is None:
            yield event.plain_result(
                f"未找到 '{args}' 对应的引擎。使用 /tts_engines 查看可用列表。"
            )
            return

        instance_name = (target_config.get("instance_name") or "").strip() or target_config.get("__template_key", "")

        # 验证引擎配置有效，成功后直接缓存实例，避免重复构造。
        try:
            engine = get_engine(target_config, self.config)
        except Exception as e:
            yield event.plain_result(f"创建引擎失败: {e}")
            return
        old_engine = self._cached_engines.get(instance_name)
        if old_engine and old_engine is not engine:
            await old_engine.close()
        self._cached_engines[instance_name] = engine

        # 保存绑定（引擎实例在首次使用时从 _cached_engines 复用）
        self._session_bindings[session_id] = instance_name
        self._save_bindings()

        logger.info(f"[情语] 会话 {session_id[:20]}... 绑定引擎: {instance_name}")
        yield event.plain_result(f"已为当前会话绑定 TTS 引擎: {instance_name}")

    @filter.command("tts_unbind")
    async def unbind_engine(self, event: AstrMessageEvent):
        """解除当前会话的 TTS 引擎绑定。用法: /tts_unbind"""
        if not _is_slash(event):
            return
        session_id = _get_session_id(event)

        if not session_id:
            yield event.plain_result("无法获取当前会话标识")
            return

        if session_id not in self._session_bindings:
            yield event.plain_result("当前会话未绑定引擎，已使用全局默认")
            return

        old_name = self._session_bindings.pop(session_id)
        # 不再关闭引擎实例，其他会话可能仍在复用
        self._save_bindings()

        logger.info(f"[情语] 会话 {session_id[:20]}... 解除绑定 (原: {old_name})")
        yield event.plain_result(f"已解除当前会话的引擎绑定 (原: {old_name})，将使用全局默认")

    @filter.command("tts_test")
    async def test_tts(self, event: AstrMessageEvent):
        """测试 TTS 合成。用法: /tts_test <文本>"""
        if not _is_slash(event):
            return
        text = _get_cmd_args(event) or "你好，这是一条 TTS 测试消息。"

        if not await self._is_astrbot_tts_enabled(event):
            yield event.plain_result("AstrBot 默认 TTS 开关未开启，插件不会接管 TTS")
            return

        session_id = _get_session_id(event)
        engine = self._get_engine_for_session(session_id)
        if not engine:
            yield event.plain_result("当前会话没有可用 TTS 引擎，请检查全局默认或会话绑定状态")
            return

        try:
            audio_bytes, fmt = await engine.synthesize(text)
            audio_path = self._temp_dir / f"tts_test_{uuid.uuid4().hex}.{fmt}"
            audio_path.write_bytes(audio_bytes)
            self._cleanup_temp_files()
            yield event.chain_result(
                [Comp.Record(file=str(audio_path), url=str(audio_path))]
            )
        except Exception as e:
            logger.error(f"[情语] 测试合成失败: {e}")
            yield event.plain_result(f"TTS 合成失败: {e}")

    @filter.command("tts_sing")
    async def sing_tts(self, event: AstrMessageEvent):
        """用唱歌模式合成语音。用法: /tts_sing <完整歌词>"""
        if not _is_slash(event):
            return
        if bool(self.config.get("singing_command_admin_only", False)):
            if not await self._ensure_admin_permission(event):
                yield event.plain_result("权限不足：唱歌命令仅管理员可用")
                return

        lyrics = _get_cmd_args(event).strip()
        if not lyrics:
            yield event.plain_result(
                "用法: /tts_sing <完整歌词>\n"
                "示例: /tts_sing 原谅我这一生不羁放纵爱自由，也会怕有一天会跌倒\n"
                "提示: 给一段完整副歌最好（4-6 句、100 字左右）；歌词残缺会跑调，过长则后段不稳；建议使用中文音色（冰糖、茉莉等）。"
            )
            return

        session_id = _get_session_id(event)
        engine = self._get_engine_for_session(session_id)
        if engine is None:
            yield event.plain_result("当前会话没有可用 TTS 引擎，请检查全局默认或会话绑定状态")
            return
        if not self._supports_singing(engine):
            reason = getattr(engine, "SINGING_UNSUPPORTED_REASON", "当前引擎不支持唱歌模式")
            yield event.plain_result(f"无法演唱：{reason}")
            return
        if not await self._is_astrbot_tts_enabled(event):
            yield event.plain_result(
                "AstrBot 的 TTS 开关未开启，唱歌命令已跳过。"
                "请先在 AstrBot 的 TTS 设置里开启，插件只在 TTS 开启时工作。"
            )
            return

        # /tts_off 关掉插件后，/tts_sing 也不应该还能发声；检查要排在长度提示之前
        if not self._runtime_enabled:
            yield event.plain_result("TTS 已关闭，发送 /tts_on 后再试")
            return

        kind = self._session_kind(session_id)
        quiet_reason = self._singing_quiet_reason(kind)
        if quiet_reason:
            logger.info(f"[情语] 命中禁止唱歌时段，拒绝 /tts_sing: {quiet_reason}")
            yield event.plain_result(quiet_reason)
            return

        # 提示阈值跟真实预算走：以前固定 135 字，导致 141 字的歌词被误报「会被截断」，
        # 而实际上 reference / legacy 两条链路根本不截断——用户据此以为时长被砍了
        target_chars, hard_chars, cps = self._singing_budget(kind)
        warning = singing_length_warning(
            lyrics,
            max_chars=hard_chars,
            truncates=not getattr(engine, "singing_reference_mode", False),
        )
        if warning:
            yield event.plain_result(f"提示：{warning}")

        # /tts_sing：与自动链路共用同一套时长预算，超预算才在行/句边界裁
        sliced, truncated = self._fit_singing_text(lyrics, target_chars, hard_chars)
        if truncated:
            yield event.plain_result(
                f"歌词超出目标时长，已保留前面一段（约 {estimate_seconds(sliced, cps):.0f} 秒，"
                f"目标 {self._singing_target_seconds(kind)} 秒 / 上限 {self._singing_max_seconds(kind)} 秒）。"
                f"想整段唱完请把 singing_clip_to_target 关掉，或调大 singing_target_seconds"
            )
        lyrics = sliced

        # 唱歌不做任何语气注入，走 MiMo 官方原版写法
        # 多群共用同一实例时唱歌是串行的，排队期间必须给反馈，否则用户以为坏了
        if getattr(engine, "singing_busy", False):
            yield event.plain_result("上一首还在唱，我排队接着唱，稍等～")

        try:
            audio_bytes, fmt = await engine.synthesize_singing(lyrics)
        except Exception as e:
            logger.error(
                f"[情语] 唱歌合成失败 (引擎: {engine.instance_name}): "
                f"{type(e).__name__}: {e}"
            )
            # 异常正文可能含接口细节，只进日志，不直接回给用户
            yield event.plain_result("唱歌合成失败，具体原因见 AstrBot 后台日志")
            return

        audio_path = self._temp_dir / f"tts_test_{uuid.uuid4().hex}.{fmt}"
        audio_path.write_bytes(audio_bytes)
        self._cleanup_temp_files()
        self._tts_audio_text[str(audio_path)] = lyrics
        self._last_tts_text[session_id] = lyrics
        self._remember_singing(session_id, lyrics, engine, cps, truncated, kind)
        self._calibrate_from_audio(lyrics, audio_bytes, fmt)
        logger.info(f"[情语] 发送唱歌语音 ({engine.instance_name}): {lyrics[:60]}")
        yield event.chain_result(
            [Comp.Record(file=str(audio_path), url=str(audio_path))]
        )

    def _engines_overview(self, session_id: str) -> list[str]:
        """引擎列表与会话状态的公共输出，/tts_engines 与 /tts_switch 共用。"""
        engines_config = self._list_engines()
        lines = [
            f"TTS 总开关: {'开启' if self._runtime_enabled else '关闭'}",
            f"全局默认引擎: {self._global_engine_name()}",
            self._session_tts_status(session_id),
            "",
            "可用 TTS 引擎：",
            "  0. 关闭当前会话 TTS",
        ]
        for i, ec in enumerate(engines_config, 1):
            lines.append(self._format_instance(i, ec, session_id))
        if not engines_config:
            lines.append("  (未配置任何 TTS 引擎)")
        lines.append("")
        lines.append("切换全局: /tts_switch <序号>（0 关闭全局）")
        lines.append("绑定本会话: /tts_bind <序号>（0 关闭本会话）")
        return lines

    @filter.command("tts_engines")
    async def list_engines(self, event: AstrMessageEvent):
        """列出所有已配置的 TTS 引擎实例。用法: /tts_engines"""
        if not _is_slash(event):
            return
        yield event.plain_result("\n".join(self._engines_overview(_get_session_id(event))))

    @filter.command("tts_bindings")
    async def list_bindings(self, event: AstrMessageEvent):
        """[管理员] 查看所有会话绑定。用法: /tts_bindings"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        lines = [
            f"TTS 总开关: {'开启' if self._runtime_enabled else '关闭'}",
            f"全局默认引擎: {self._global_engine_name()}",
            "",
        ]

        if not self._session_bindings:
            lines.append("暂无会话绑定，所有会话使用全局默认")
        else:
            lines.append(f"会话绑定 ({len(self._session_bindings)} 个)：")
            for i, (sid, name) in enumerate(self._session_bindings.items(), 1):
                lines.append(f"  {i}. {_format_session_id(sid)} → {self._format_binding_name(name)}")

        lines.append("\n/tts_unbind_all 清除全部绑定")
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_unbind_all")
    async def unbind_all(self, event: AstrMessageEvent):
        """[管理员] 清除所有会话绑定。用法: /tts_unbind_all"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        if not self._session_bindings:
            yield event.plain_result("当前没有任何会话绑定")
            return

        count = len(self._session_bindings)
        # 关闭所有被会话绑定了的缓存引擎
        for engine in self._cached_engines.values():
            await engine.close()
        self._cached_engines.clear()
        self._session_bindings.clear()
        self._save_bindings()

        logger.info(f"[情语] 已清除全部 {count} 个会话绑定")
        yield event.plain_result(f"已清除全部 {count} 个会话绑定，所有会话将使用全局默认")

    @filter.command("tts_reload")
    async def reload_engines(self, event: AstrMessageEvent):
        """清空所有引擎实例缓存，让配置改动立即生效。用法: /tts_reload"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            return
        closed = 0
        for name, engine in list(self._cached_engines.items()):
            try:
                await engine.close()
                closed += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[情语] 关闭缓存引擎 {name} 失败: {e}")
        self._cached_engines.clear()
        if self._engine is not None:
            try:
                await self._engine.close()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[情语] 关闭全局引擎失败: {e}")
            self._engine = None

        # 按 active_engine 重新构造全局引擎，配置改动才会真正生效
        engines_config = self._list_engines()
        active = _as_str(self.config.get("active_engine"))
        target = None
        for ec in engines_config:
            name = _as_str(ec.get("instance_name")) or _as_str(ec.get("__template_key"))
            if active and name == active:
                target = ec
                break
        if target is None and engines_config:
            target = engines_config[0]
        if target is None:
            yield event.plain_result("重载失败：没有可用的引擎配置")
            return
        try:
            self._engine = get_engine(target, self.config)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"重建全局引擎失败: {e}")
            return

        yield event.plain_result(
            f"已重载：清空会话缓存实例，全局引擎重建为 "
            f"{getattr(self._engine, 'instance_name', '?')}；"
            f"当前会话绑定 {len(self._session_bindings)} 个"
        )

    @filter.command("tts_off")
    async def disable_runtime(self, event: AstrMessageEvent):
        """[管理员] 暂停所有会话的自动 TTS。用法: /tts_off"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        self._runtime_enabled = False
        self._save_runtime_state()
        logger.info("[情语] 已暂停全部会话的自动 TTS")
        yield event.plain_result("已暂停全部会话的自动 TTS。会话绑定不会被修改，使用 /tts_on 恢复。")

    @filter.command("tts_on")
    async def enable_runtime(self, event: AstrMessageEvent):
        """[管理员] 恢复所有会话的自动 TTS。用法: /tts_on"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        self._runtime_enabled = True
        self._save_runtime_state()
        logger.info("[情语] 已恢复自动 TTS")
        yield event.plain_result("已恢复自动 TTS。全局默认和会话绑定将按原状态生效。")

    @filter.command("tts_status")
    async def tts_status(self, event: AstrMessageEvent):
        """查看 TTS 开关和当前会话状态。用法: /tts_status"""
        if not _is_slash(event):
            return
        session_id = _get_session_id(event)
        kind = self._session_kind(session_id)
        engine = self._get_engine_for_session(session_id)
        if not self._singing_enabled():
            singing_status = "已关闭"
        elif self._supports_singing(engine):
            singing_status = "可用（/tts_sing 或聊天中说「唱首歌」）"
        elif engine is None:
            singing_status = "无可用引擎"
        else:
            singing_status = f"当前引擎不支持：{getattr(engine, 'SINGING_UNSUPPORTED_REASON', '未知原因')}"

        if HOOK_REGISTRY.get("on_llm_request"):
            prompt_status = f"已注入（<{DEFAULT_SINGING_TAG}>）"
        else:
            prompt_status = "未注入（缺钩子，靠关键词兜底）"
        intent_status = "开启" if bool(self.config.get("singing_auto_intent", True)) else "关闭"
        keywords = self._singing_keywords()
        keyword_status = f"{len(keywords)} 项"
        quiet_reason = self._singing_quiet_reason(kind)
        quiet_status = "未启用"
        if bool(self.config.get("singing_quiet_hours_enable", False)):
            window = self._kind_override_str(
                "singing_quiet_hours",
                kind,
                _as_str(self.config.get("singing_quiet_hours")),
            ) or "未填写"
            quiet_status = f"{window}（{'当前生效中' if quiet_reason else '当前不在时段内'}）"

        preset = self._active_length_preset()
        length_text = (
            f"{SINGING_LENGTH_PRESETS[preset][0]}（{self._singing_target_seconds(kind)} 秒）"
            if preset
            else f"自定义（{self._singing_target_seconds(kind)} 秒）"
        )
        binding = self._session_tts_status(session_id)
        last_line = self._last_singing_summary(event).split("\n")[0]

        lines = [
            f"引擎：{binding} · 统一模式 {'开' if self._unified_mode() else '关'}",
            f"唱歌：{singing_status}",
            f"时长：{length_text} · 时段：{quiet_status}",
            f"提示词：{prompt_status} · 兜底 {intent_status} · 关键词 {keyword_status}",
            last_line,
            "",
            "操作：/tts_panel 改设置 · /tts_diff 查差异 · /tts_sing <歌词> 点歌",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_limit")
    async def text_limit(self, event: AstrMessageEvent):
        """[管理员] 查看/设置 TTS 字数上限。用法: /tts_limit [数字|off]"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return

        limit = _as_int(self.config.get("tts_text_limit", 50), 50)
        enabled = bool(self.config.get("tts_text_limit_enable", False))
        args = _get_cmd_args(event).strip()

        if not args:
            yield event.plain_result(
                f"TTS 字数上限：{'开启' if enabled else '关闭'}"
                f"（超过 {limit} 字的回复不转语音，唱歌不受影响）\n"
                f"用法：/tts_limit 50 设置并开启 · /tts_limit off 关闭"
            )
            return

        if args.lower() in {"off", "0", "关", "关闭"}:
            self.config["tts_text_limit_enable"] = False
            yield event.plain_result("已关闭 TTS 字数上限")
            return

        try:
            value = int(args)
            if value <= 0:
                raise ValueError
        except ValueError:
            yield event.plain_result(f"无效的数字: {args}（用法：/tts_limit 50 或 /tts_limit off）")
            return

        self.config["tts_text_limit"] = value
        self.config["tts_text_limit_enable"] = True
        yield event.plain_result(f"已设置并开启 TTS 字数上限：{value} 字")

    @filter.command("tts_diff")
    async def diff_sessions(self, event: AstrMessageEvent):
        """[管理员] 逐项对比当前会话与全局的听感设置。用法: /tts_diff"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        session_id = _get_session_id(event)
        kind = self._session_kind(session_id)
        scope = {"group": "群聊", "private": "私聊"}.get(kind, "未知会话")
        bound = self._session_bindings.get(session_id)
        current = self._get_engine_for_session(session_id)
        cur = self._engine_snapshot(current)
        glob = self._engine_snapshot(self._engine)

        lines = [f"会话诊断 · {scope}", f"  UMO: {session_id or '（空）'}"]
        lines.append(f"  统一模式: {'开（绑定不再换音色）' if self._unified_mode() else '关'}")
        if bound == DISABLED_ENGINE:
            lines.append("  绑定记录: 已禁用 TTS")
        elif bound:
            note = "（统一模式下已忽略）" if self._unified_mode() else ""
            lines.append(f"  绑定记录: {bound}{note}")
        else:
            lines.append("  绑定记录: 无（跟随全局）")
        lines.append(f"  实际引擎: {cur.get('实例', '无')}")
        lines.append("")
        lines.append("逐项对比（当前会话 vs 全局）：")
        diff_count = 0
        for key in (
            "模型",
            "朗读音色",
            "唱歌音色",
            "演唱速度",
            "整体语调",
            "音色质感",
            "人设腔调",
            "方言",
            "角色扮演",
            "唱歌链路",
            "朗读语气注入",
        ):
            mine = cur.get(key, "")
            base = glob.get(key, "")
            if mine == base:
                lines.append(f"  {key}：{mine or '（空）'}")
            else:
                diff_count += 1
                lines.append(f"  {key}：{mine or '（空）'} ≠ 全局 {base or '（空）'}")
        lines.append("")
        if diff_count:
            lines.append(f"发现 {diff_count} 项不同 —— 这就是「两边听起来不一样」的来源。")
            lines.append("发 /tts_unify 可以全部拉平。")
        else:
            lines.append("所有听感设置一致：差异只可能来自上下文、人设或模型发挥。")

        persona = getattr(event, "_qinyu_persona", "")
        lines.append("")
        lines.append(f"  会话人设: {'有（来自 AstrBot，不归插件管）' if persona else '未取到'}")
        try:
            probability = int(self.config.get("tts_probability", 100))
        except (TypeError, ValueError):
            probability = 100
        lines.append(f"  发语音概率: {probability}%（全局统一，不分会话）")
        lines.append(
            f"  唱歌时长: 目标 {self._singing_target_seconds(kind)} 秒 / "
            f"上限 {self._singing_max_seconds(kind)} 秒"
        )
        # 统一模式下这些值仍留在配置里但不参与计算，必须显式报出来，
        # 否则用户会以为「我设过，怎么没变」。
        ignored = self._singing_state().get("ignored_overrides") or []
        if ignored:
            lines.append(
                f"  已被忽略: {len(ignored)} 项群聊/私聊单独设置（统一模式开着，不参与计算）"
            )
            lines.append(f"            {'、'.join(ignored)}")
            lines.append("            想让它生效：发 /tts_unify 关掉统一模式，或直接 /tts_set 对应项。")
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_restore")
    async def restore_config(self, event: AstrMessageEvent):
        """[管理员] 恢复上次快照里的配置（音色等被重置时用）。用法: /tts_restore"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        drift = self._config_drift or self._check_config_drift()
        if not drift:
            yield event.plain_result(
                "没有检测到配置变化。\n"
                "如果音色不对，直接去配置页改「朗读音色」——现在改完就生效，不用重载插件。"
            )
            return
        ok = await self._restore_config_snapshot()
        if ok:
            head = "已恢复上次的配置：\n" + "\n".join(f"  {d}" for d in drift)
            voice = getattr(self._engine, "voice", "")
            yield event.plain_result(
                f"{head}\n当前朗读音色：{voice or '(未设)'}\n"
                "发一句测试它读不读得对。"
            )
        else:
            yield event.plain_result("恢复失败：没有可用的快照文件")

    @filter.command("tts_doctor")
    async def doctor(self, event: AstrMessageEvent):
        """[管理员] 自检：检查哪些功能不可用、原因与下一步。用法: /tts_doctor"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        lines, ok = self._self_check()
        logger.info("[情语] 自检完成，结论: %s", "可用" if ok else "有问题")
        yield event.plain_result("情语 自检\n" + "\n".join(lines))

    @filter.command("tts_set")
    async def set_singing_option(self, event: AstrMessageEvent):
        """[管理员] 一键改唱歌配置并立即生效。用法: /tts_set <项目> <值>"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        args = _get_cmd_args(event).split()
        if len(args) < 2:
            yield event.plain_result(
                "用法: /tts_set <项目> <值>\n"
                "最常用的一条：\n"
                "  /tts_set 时长 中          唱 40 秒（可选 短 / 中 / 长 / 很长）\n"
                "其他：\n"
                "  /tts_set 私聊 40            只改私聊\n"
                "  /tts_set 群聊 120           只改群聊\n"
                "  /tts_set 时段 23:00-07:00   全局禁止唱歌时段\n"
                "  /tts_set 开关 1             启用时段限制\n"
                "  /tts_set 补足 1             歌词不够时补唱一遍\n"
                "发送 /tts_panel 查看当前值。"
            )
            return

        raw_key = args[0]
        raw_value = " ".join(args[1:])
        key = _resolve_setting_key(raw_key)
        if key is None:
            yield event.plain_result(
                f"不认识的配置项: {raw_key}\n"
                "可以用: " + "、".join(sorted(SETTING_ALIASES)) + "\n"
                "或完整键名: " + "、".join(sorted(WEB_EDITABLE_KEYS))
            )
            return
        try:
            value = self._coerce_web_value(key, raw_value)
        except ValueError as e:
            yield event.plain_result(str(e))
            return

        expanded: dict = {}
        if key == "singing_length_preset":
            expanded = self._preset_values(value)
        else:
            expanded = {key: value}
            # 单独设群聊/私聊项 = 用户要两边不同，那就必须把统一模式关掉，
            # 不然值写了也不参与计算，用户会以为「改了没用」。
            if _is_per_kind_key(key) and _override_active(value) and self._unified_mode():
                expanded["singing_unified_mode"] = False
        self.config.update(expanded)
        persisted = await self._persist_config()
        rebuilt = await self._rebuild_engines()
        logger.info(
            "[情语] /tts_set 已应用 %s=%s（持久化=%s, 重建引擎=%s）",
            key,
            value,
            persisted,
            rebuilt or "无",
        )
        extra = ""
        if expanded.get("singing_unified_mode") is False:
            extra = (
                "\n已自动关闭统一模式：不关掉的话，群聊/私聊的单独设置会被忽略，"
                "等于白设。想恢复两边一致，发 /tts_unify。"
            )
        elif key == "singing_length_preset":
            label = SINGING_LENGTH_PRESETS[value][0]
            extra = f"\n已按「{label}」设为目标 {self._singing_target_seconds('')} 秒，" \
                    f"并把群聊/私聊的时长覆盖清掉"
        elif (
            key
            in (
                "singing_quiet_hours",
                "singing_quiet_hours_private",
                "singing_quiet_hours_group",
            )
            and value
            and not bool(self.config.get("singing_quiet_hours_enable", False))
        ):
            extra = "\n注意: 禁止唱歌时段的总开关还没开，发 /tts_set 开关 1 才会生效"
        elif key in ("singing_quiet_hours", "singing_quiet_hours_private", "singing_quiet_hours_group") and not value:
            extra = "\n已清空该时段，将回落到全局设置"
        yield event.plain_result(
            f"已设置 {raw_key} = {value}\n"
            f"持久化: {'已写入配置文件' if persisted else '仅当前进程（配置对象不支持保存）'}\n"
            f"引擎重建: {rebuilt or '未重建'}\n"
            f"当前生效: 私聊 {self._singing_target_seconds('private')} 秒 / "
            f"群聊 {self._singing_target_seconds('group')} 秒{extra}"
        )

    @filter.command("tts_panel")
    async def singing_panel(self, event: AstrMessageEvent):
        """[管理员] 一屏看全并给出可直接发送的短指令。用法: /tts_panel"""
        if not _is_slash(event):
            return
        kind = self._session_kind(_get_session_id(event))
        scope = {"group": "群聊", "private": "私聊"}.get(kind, "未知会话")

        def show_target(k: str) -> str:
            own = self.config.get(self._kind_config_key("singing_target_seconds", k))
            inherited = "（跟随全局）" if own in (None, "") or _as_int(own, 0) <= 0 else ""
            return f"{self._singing_target_seconds(k)} 秒{inherited}"

        def show_quiet(k: str) -> str:
            window = self._kind_override_str(
                "singing_quiet_hours", k, _as_str(self.config.get("singing_quiet_hours"))
            )
            if not window:
                return "不限"
            inherited = "" if _as_str(self.config.get(self._kind_config_key("singing_quiet_hours", k))) else "（跟随全局）"
            return f"{window}{inherited}"

        quiet_on = bool(self.config.get("singing_quiet_hours_enable", False))
        preset = self._active_length_preset()
        if preset:
            length_line = (
                f"{SINGING_LENGTH_PRESETS[preset][0]}"
                f"（目标 {self._singing_target_seconds(kind)} 秒）"
            )
        else:
            length_line = f"自定义（{self._singing_target_seconds(kind)} 秒）"
        voice = self._global_voice_info()
        lines = [
            f"唱歌控制 · 当前是{scope}",
            f"  唱多久：{length_line}",
            f"  音色：朗读 {voice.get('voice') or '默认'} / 唱歌 {voice.get('singing_voice') or '跟随朗读'}"
            f"（引擎 {voice.get('instance') or '未初始化'}）",
            f"  禁止唱歌时段：{'启用' if quiet_on else '关闭'}"
            f"（私聊 {show_quiet('private')} / 群聊 {show_quiet('group')}）",
            "",
            "改「唱多久」只发这一条：",
            "  /tts_set 时长 短     约 20 秒",
            "  /tts_set 时长 中     约 40 秒",
            "  /tts_set 时长 长     约 60 秒",
            "  /tts_set 时长 很长   约 90 秒",
            "",
            "想统一私聊和群聊（音色、时长、时段全部一致）：",
            "  /tts_unify",
            "",
            "进阶（一般用不到）：",
            "  /tts_set 时段 23:00-07:00   禁止唱歌时段",
            "  /tts_set 开关 1             启用时段限制",
            "  /tts_set 补足 1             歌词不够时补唱一遍",
            "  /tts_set 私聊 40            只改私聊",
            "  /tts_set 群聊 120           只改群聊",
            "",
            "WebUI 插件详情页 →「唱歌控制」有同样的按钮。",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_unify")
    async def unify_singing(self, event: AstrMessageEvent):
        """[管理员] 一键统一所有会话的音色与配置。用法: /tts_unify"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        result = await self._unify_singing()
        detail = ""
        if result["cleared_overrides"]:
            detail = f"（{'、'.join(result['cleared_overrides'])}）"
        voice = result.get("global_voice") or {}
        singing_voice = voice.get("singing_voice") or "跟随朗读"
        preset = self._active_length_preset()
        preset_text = (
            f"{SINGING_LENGTH_PRESETS[preset][0]}（{self._singing_target_seconds('')} 秒）"
            if preset
            else f"自定义（{self._singing_target_seconds('')} 秒）"
        )
        yield event.plain_result(
            f"已统一：除对话记忆与上下文外，其余全部一致。\n"
            f"  统一模式：已开启（以后私聊/群聊的覆盖项和绑定都不会再生效）\n"
            f"  音色：朗读 {voice.get('voice') or '默认'} / 唱歌 {singing_voice}"
            f"（引擎 {voice.get('instance') or '未初始化'}）\n"
            f"  时长：{preset_text}，上限 {self._singing_max_seconds('')} 秒\n"
            f"  发语音概率：{_as_int(self.config.get('tts_probability'), 100)}%（全局，本来就一致）\n"
            f"  清理：换音色绑定 {result['cleared_bindings']} 个、"
            f"群聊/私聊覆盖 {len(result['cleared_overrides'])} 项{detail}、"
            f"保留禁用 {result.get('kept_disabled', 0)} 个\n"
            f"持久化: {'已写入配置文件' if result['persisted'] else '仅当前进程（配置对象不支持保存）'}\n"
            f"引擎重建: {result['rebuilt_engine'] or '未重建'}\n"
            f"另：会话人设和对话上下文由 AstrBot 管理，插件不碰——这是设计，不是遗漏。\n"
            f"想确认两边是否真的完全一致，各发一次 /tts_diff。"
        )

    # ---------- 可视化视图（WebUI「唱歌控制」） ----------

    def _register_web_apis(self) -> None:
        """注册插件视图的后端接口。

        AstrBot 的插件页面通过 `context.register_web_api()` 调后端。没有这个能力时
        把 `HOOK_REGISTRY["web_api"]` 标成 False，面板据此显示「接口不可用」，
        而不是给一个点了没反应的假按钮。
        """
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            HOOK_REGISTRY["web_api"] = False
            logger.warning(
                "[情语] 当前 AstrBot 未提供 register_web_api，"
                "WebUI 唱歌控制面板不可用；聊天指令 /tts_set 仍然可用"
            )
            return
        base = "/astrbot_plugin_qinyu"
        try:
            register(f"{base}/singing/state", self.api_singing_state, ["GET"], "唱歌控制：读取状态")
            register(
                f"{base}/singing/set",
                self.api_singing_set,
                ["POST", "GET"],
                "唱歌控制：修改设置并立即生效",
            )
            register(
                f"{base}/singing/reset",
                self.api_singing_reset,
                ["POST", "GET"],
                "唱歌控制：恢复默认",
            )
            register(
                f"{base}/singing/unify",
                self.api_singing_unify,
                ["POST", "GET"],
                "唱歌控制：一键统一所有会话",
            )
        except Exception as e:  # noqa: BLE001 - 注册失败不应拖垮插件
            HOOK_REGISTRY["web_api"] = False
            logger.warning(f"[情语] 注册 Web API 失败: {e}")
            return
        HOOK_REGISTRY["web_api"] = True
        logger.info("[情语] 已注册 WebUI 唱歌控制接口")

    @staticmethod
    def _json_ok(payload: dict):
        """统一成功响应：有 astrbot.api.web 就用它的 helper，否则直接返回 dict。"""
        if _web_json is not None:
            return _web_json(payload)
        return payload

    @staticmethod
    def _json_err(message: str, status_code: int = 400):
        if _web_error is not None:
            return _web_error(message, status_code=status_code)
        return {"ok": False, "error": message}

    async def _web_payload(self) -> dict:
        """读面板提交的 JSON。GET 兜底路径没有 body 时返回空 dict。"""
        if _web_request is None:
            return {}
        try:
            data = await _web_request.json(default={})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[情语] 读取面板请求失败: {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def _coerce_web_value(self, key: str, value):
        """把面板传来的值转成合法配置值，不合法直接抛 ValueError。"""
        if key == "singing_length_preset":
            text = str(value).strip().lower()
            if not text:
                return ""  # 空 = 自定义，用 singing_target_seconds 的秒数
            text = SINGING_LENGTH_ALIASES.get(text, text)
            if text not in SINGING_LENGTH_PRESETS:
                names = "/".join(item[0] for item in SINGING_LENGTH_PRESETS.values())
                raise ValueError(f"唱多久只能选 {names}")
            return text
        if key in WEB_EDITABLE_BOOL_KEYS:
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "on", "yes", "开"}:
                return True
            if text in {"0", "false", "off", "no", "关", ""}:
                return False
            raise ValueError(f"{key} 需要布尔值")
        if key in WEB_EDITABLE_INT_KEYS:
            try:
                number = int(float(str(value).strip()))
            except (TypeError, ValueError):
                raise ValueError(f"{key} 需要整数秒") from None
            # 只有「群聊/私聊专用」这几个键允许填 0 表示跟随全局
            if (
                number == 0
                and key
                not in {"singing_target_seconds", "singing_max_seconds"}
            ):
                return 0
            if not (SINGING_MIN_ALLOWED_SECONDS <= number <= SINGING_MAX_ALLOWED_SECONDS):
                raise ValueError(
                    f"{key} 必须在 {SINGING_MIN_ALLOWED_SECONDS}~"
                    f"{SINGING_MAX_ALLOWED_SECONDS} 秒之间"
                )
            return number
        if key in WEB_EDITABLE_FLOAT_KEYS:
            try:
                number = float(str(value).strip())
            except (TypeError, ValueError):
                raise ValueError(f"{key} 需要数字（每秒字数）") from None
            if number == 0:
                return 0.0  # 0 = 交给插件自动校准
            if not (SINGING_CPS_MIN <= number <= SINGING_CPS_MAX):
                raise ValueError(
                    f"{key} 必须在 {SINGING_CPS_MIN}~{SINGING_CPS_MAX} 字/秒之间，"
                    "填 0 表示自动校准"
                )
            return round(number, 2)
        text = str(value or "").strip()
        # 只有时段类键才做时间格式校验。
        # 之前这里对所有字符串键都跑一遍，结果 singing_engine 填「跟随朗读引擎」
        # 被当成时间范围解析，报「时段格式不对」。
        if key in QUIET_HOUR_KEYS and text:
            _, bad = _parse_quiet_windows(text)
            if bad:
                raise ValueError(
                    f"时段格式不对：{'、'.join(bad)}（正确写法 23:30-07:00，多段用逗号分隔）"
                )
        return text

    async def _persist_config(self) -> bool:
        """把改动写回 AstrBot 配置文件，重启后仍然生效。"""
        saver = getattr(self.config, "save_config", None)
        if not callable(saver):
            logger.warning(
                "[情语] 当前配置对象不支持 save_config，"
                "本次改动只在当前进程内生效（重载插件会丢）"
            )
            return False
        try:
            result = saver()
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"[情语] 保存配置失败: {e}")
            return False

    async def _rebuild_engines(self) -> str:
        """按当前 config 重建引擎实例。

        引擎对象在构造时把 singing_* 参数抄进属性里，所以只改 config 不重建，
        面板就是「假按钮」。这里关掉旧实例再按新配置造一个，改动立刻生效。
        """
        for name, engine in list(self._cached_engines.items()):
            try:
                await engine.close()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[情语] 关闭缓存引擎 {name} 失败: {e}")
        self._cached_engines.clear()
        if self._engine is not None:
            try:
                await self._engine.close()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[情语] 关闭全局引擎失败: {e}")
            self._engine = None

        if _as_str(self.config.get("active_engine")) == DISABLED_ENGINE:
            return ""
        engines_config = self._list_engines()
        if not engines_config:
            return ""
        active = _as_str(self.config.get("active_engine"))
        target = None
        for ec in engines_config:
            name = _as_str(ec.get("instance_name")) or _as_str(ec.get("__template_key"))
            if active and name == active:
                target = ec
                break
        if target is None:
            target = engines_config[0]
        try:
            self._engine = get_engine(target, self.config)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[情语] 重建全局引擎失败: {e}")
            return ""
        return getattr(self._engine, "instance_name", "")

    def _engine_snapshot(self, engine: TTSEngine | None) -> dict:
        """把影响听感的属性全部摊平，用来逐项对比两个引擎实例。

        「私聊说话和唱歌都偏慢」这类问题，答案基本都在这张表里：
        绑定的实例不同，朗读音色、五维风格、演唱速度就都可能不同。
        """
        if engine is None:
            return {}
        return {
            "实例": getattr(engine, "instance_name", "?") or "?",
            "模型": getattr(engine, "model", "") or "",
            "朗读音色": getattr(engine, "voice", "") or "",
            "唱歌音色": getattr(engine, "singing_voice", "") or "",
            "输出格式": getattr(engine, "audio_format", "") or "",
            "整体语调": getattr(engine, "overall_tone", "") or "",
            "音色质感": getattr(engine, "timbre_positioning", "") or "",
            "人设腔调": getattr(engine, "persona_accent", "") or "",
            "方言": getattr(engine, "dialect", "") or "",
            "角色扮演": getattr(engine, "role_play", "") or "",
            "演唱速度": getattr(engine, "singing_speed", "") or "",
            "演唱气质": getattr(engine, "singing_style", "") or "",
            "唱歌链路": self._singing_mode_label(engine),
            "常驻唱歌": "开" if getattr(engine, "singing_mode", False) else "关",
            "音色锁定": "开" if getattr(engine, "singing_voice_lock", False) else "关",
            "朗读语气注入": getattr(engine, "voice_tone", "") or "关",
            "风格提示词": getattr(engine, "style_prompt", "") or "",
        }

    def _global_voice_info(self) -> dict:
        """全局引擎的音色信息：所有「没被单独绑定」的会话都用这一份。"""
        engine = self._engine
        return {
            "instance": getattr(engine, "instance_name", "") if engine else "",
            "voice": (getattr(engine, "voice", "") or "") if engine else "",
            "singing_voice": (getattr(engine, "singing_voice", "") or "") if engine else "",
            "model": (getattr(engine, "model", "") or "") if engine else "",
        }

    def _binding_report(self) -> list[dict]:
        """列出所有会话引擎绑定，并标出它与全局音色是否一致。

        「群里一个音、私聊一个音」的唯一来源就是这张表：被绑定的会话用绑定实例的音色，
        没绑定的用全局音色。要统一，就是把这张表清空。
        """
        configs: dict[str, dict] = {}
        for ec in self._list_engines():
            name = _as_str(ec.get("instance_name")) or _as_str(ec.get("__template_key"))
            if name:
                configs[name] = ec
        global_info = self._global_voice_info()
        report: list[dict] = []
        for umo, name in list(self._session_bindings.items()):
            kind = self._session_kind(umo)
            if name == DISABLED_ENGINE:
                report.append(
                    {
                        "umo": umo,
                        "kind": kind,
                        "instance": "已禁用 TTS",
                        "voice": "",
                        "singing_voice": "",
                        "same_as_global": True,
                        "ignored": False,
                    }
                )
                continue
            ec = configs.get(name) or {}
            voice = _as_str(ec.get("voice"))
            singing_voice = _as_str(ec.get("singing_voice"))
            matched = name == global_info["instance"] and (
                voice in ("", global_info["voice"])
                and singing_voice in ("", global_info["singing_voice"])
            )
            # 统一模式下这条绑定根本不生效，所以永远算「一致」，并标出被忽略
            unified = self._unified_mode()
            report.append(
                {
                    "umo": umo,
                    "kind": kind,
                    "instance": name,
                    "voice": voice,
                    "singing_voice": singing_voice,
                    "same_as_global": unified or matched,
                    "ignored": unified and not matched,
                }
            )
        return report

    def _singing_state(self) -> dict:
        """面板要的全部状态：配置、实际生效值、引擎与钩子是否可用。"""

        def scope(kind: str) -> dict:
            target_chars, hard_chars, cps = self._singing_budget(kind)
            return {
                "target_seconds": self._singing_target_seconds(kind),
                "max_seconds": self._singing_max_seconds(kind),
                "target_chars": target_chars,
                "quiet_hours": self._kind_override_str(
                    "singing_quiet_hours",
                    kind,
                    _as_str(self.config.get("singing_quiet_hours")),
                ),
                "quiet_now": bool(self._singing_quiet_reason(kind)),
            }

        engine = self._engine
        cps = self._singing_cps()

        # 引擎卡片清单：面板据此渲染「点一下换引擎」的图形选择器。
        # 只看模板类的能力声明，不实例化引擎——避免面板每刷新一次就建一堆对象。
        engine_cards: list[dict] = []
        for idx, cfg in enumerate(self._list_engines(), start=1):
            name = _as_str(cfg.get("instance_name")) or _as_str(cfg.get("__template_key"))
            template = _as_str(cfg.get("__template_key"))
            cls = ENGINE_REGISTRY.get(template)
            can_sing = bool(getattr(cls, "SUPPORTS_SINGING", False)) if cls else False
            engine_cards.append(
                {
                    "index": idx,
                    "name": name,
                    "template": template,
                    "can_sing": can_sing,
                    "reason": (
                        ""
                        if can_sing
                        else (getattr(cls, "SINGING_UNSUPPORTED_REASON", "") if cls else "未知模板")
                    ),
                }
            )
        current_speech = _as_str(self.config.get("active_engine")).strip()
        if not current_speech and engine_cards:
            current_speech = engine_cards[0]["name"]
        current_singing = _as_str(self.config.get("singing_engine")).strip()
        calibration = dict(self._calibration or {})
        bindings = self._binding_report()
        divergent = [b for b in bindings if not b["same_as_global"]]
        # 统一模式下，这些「群聊/私聊单独设置」有值却不参与计算。
        # 不报出来，用户会以为设过就生效，等哪天关掉统一模式又突然变了。
        ignored_overrides = [
            key
            for key in (
                "singing_target_seconds_private",
                "singing_target_seconds_group",
                "singing_max_seconds_private",
                "singing_max_seconds_group",
                "singing_quiet_hours_private",
                "singing_quiet_hours_group",
            )
            if _override_active(self.config.get(key)) and self._unified_mode()
        ]
        return {
            "ok": True,
            "web_api": bool(HOOK_REGISTRY.get("web_api")),
            "prompt_hook": bool(HOOK_REGISTRY.get("on_llm_request")),
            "unified_mode": self._unified_mode(),
            "ignored_overrides": ignored_overrides,
            "global_voice": self._global_voice_info(),
            "bindings": bindings,
            "voice_unified": not divergent,
            "divergent_bindings": len(divergent),
            "singing_speed": {
                "cps": round(cps, 3),
                "source": "手动指定"
                if _as_float(self.config.get("singing_chars_per_second"), 0.0) > 0
                else ("实测校准" if calibration.get("cps") else "默认估计"),
                "samples": int(calibration.get("samples", 0) or 0),
                "last_measured": calibration.get("last_measured"),
                "last_chars": calibration.get("last_chars"),
                "last_seconds": calibration.get("last_seconds"),
            },
            "engine_choices": {
                "cards": engine_cards,
                "speech": current_speech,
                "singing": current_singing,
            },
            "engine": {
                "name": getattr(engine, "instance_name", "") if engine else "",
                "supports_singing": self._supports_singing(engine),
                "mode": self._singing_mode_label(engine),
                "singing_voice": getattr(engine, "singing_voice", "") or "",
                "legacy": bool(getattr(engine, "singing_legacy_mode", False)),
                "reference": bool(getattr(engine, "singing_reference_mode", False)),
                "resident": bool(getattr(engine, "singing_mode", False)),
            },
            "config": {
                "singing_enable": bool(self.config.get("singing_enable", True)),
                "singing_auto_intent": bool(self.config.get("singing_auto_intent", True)),
                "singing_clip_to_target": bool(
                    self.config.get("singing_clip_to_target", True)
                ),
                "singing_fill_short": bool(self.config.get("singing_fill_short", False)),
                "singing_length_preset": self._active_length_preset(),
                "singing_unified_mode": self._unified_mode(),
                "singing_chars_per_second": _as_float(
                    self.config.get("singing_chars_per_second"), 0.0
                ),
                "singing_target_seconds": _as_int(
                    self.config.get("singing_target_seconds"), SINGING_TARGET_SECONDS
                ),
                "singing_max_seconds": _as_int(
                    self.config.get("singing_max_seconds"), SINGING_MAX_SECONDS
                ),
                "singing_target_seconds_private": _as_int(
                    self.config.get("singing_target_seconds_private"), 0
                ),
                "singing_target_seconds_group": _as_int(
                    self.config.get("singing_target_seconds_group"), 0
                ),
                "singing_max_seconds_private": _as_int(
                    self.config.get("singing_max_seconds_private"), 0
                ),
                "singing_max_seconds_group": _as_int(
                    self.config.get("singing_max_seconds_group"), 0
                ),
                "singing_quiet_hours_enable": bool(
                    self.config.get("singing_quiet_hours_enable", False)
                ),
                "singing_quiet_hours": _as_str(self.config.get("singing_quiet_hours")),
                "singing_quiet_hours_private": _as_str(
                    self.config.get("singing_quiet_hours_private")
                ),
                "singing_quiet_hours_group": _as_str(
                    self.config.get("singing_quiet_hours_group")
                ),
            },
            "effective": {"private": scope("private"), "group": scope("group")},
            "limits": {
                "min_seconds": SINGING_MIN_ALLOWED_SECONDS,
                "max_seconds": SINGING_MAX_ALLOWED_SECONDS,
            },
            "last": dict(self._last_sing_any),
        }

    async def api_singing_state(self, **kwargs):
        """GET 状态。"""
        return self._json_ok(self._singing_state())

    async def api_singing_set(self, **kwargs):
        """修改配置 → 持久化 → 重建引擎 → 回传新状态。

        返回体里 `persisted` / `rebuilt_engine` 是给面板做「真的生效了吗」的判据：
        没有持久化就说没有，没有重建出引擎就说没有，绝不含糊。
        """
        payload = await self._web_payload()
        if not payload and kwargs:
            # 兼容两种情况：没有 HTTP 请求上下文（例如内部调用 / 单测），
            # 以及 AstrBot 把路由动态片段当关键字参数传进来。
            payload = dict(kwargs)
        values = payload.get("values")
        if not isinstance(values, dict):
            key = payload.get("key")
            if key is None:
                return self._json_err("缺少 key 或 values")
            values = {str(key): payload.get("value")}

        applied: dict = {}
        errors: dict = {}
        for raw_key, raw_value in values.items():
            key = str(raw_key)

            if key not in WEB_EDITABLE_KEYS:
                errors[key] = "不支持的配置项"
                continue
            try:
                applied[key] = self._coerce_web_value(key, raw_value)
            except ValueError as e:
                errors[key] = str(e)

        if not applied:
            detail = "；".join(f"{k}: {v}" for k, v in errors.items()) or "没有提交任何改动"
            return self._json_err(f"没有可应用的修改（{detail}）")

        # 「唱多久」是个复合操作：一个词展开成目标+上限+清覆盖，必须整体写入
        final: dict = {}
        for key, value in applied.items():
            if key == "singing_length_preset":
                final.update(self._preset_values(value))
            else:
                final[key] = value
        # 单独设群聊/私聊项，说明用户就是要两边不同：必须同时关掉统一模式，
        # 否则这些值写进去也会被忽略，界面看着改了、实际不生效。
        if self._unified_mode() and any(
            _is_per_kind_key(key) and _override_active(value)
            for key, value in applied.items()
        ):
            final["singing_unified_mode"] = False
        for key, value in final.items():
            self.config[key] = value
        persisted = await self._persist_config()
        rebuilt = await self._rebuild_engines()

        state = self._singing_state()
        state.update(
            {
                "ok": not errors,
                "applied": final,
                "errors": errors,
                "persisted": persisted,
                "rebuilt_engine": rebuilt,
            }
        )
        logger.info(
            "[情语] 面板修改已应用: %s（持久化=%s, 重建引擎=%s）",
            final,
            persisted,
            rebuilt or "无",
        )
        return self._json_ok(state)

    async def api_singing_reset(self, **kwargs):
        """把唱歌控制相关的配置恢复默认。

        必须把「预设」和「统一模式」一起恢复：只改秒数会让面板显示「自定义」，
        只改覆盖项会让统一模式仍停在关闭状态——那样「恢复默认」名不副实。
        """
        defaults = {
            # 预设会把目标、上限、裁剪、群聊/私聊覆盖一次性设回默认
            **self._preset_values("medium"),
            "singing_unified_mode": True,
            "singing_quiet_hours": "",
            "singing_quiet_hours_private": "",
            "singing_quiet_hours_group": "",
            "singing_quiet_hours_enable": False,
            "singing_clip_to_target": True,
            "singing_fill_short": False,
            "singing_chars_per_second": 0,
        }
        self.config.update(defaults)
        persisted = await self._persist_config()
        rebuilt = await self._rebuild_engines()
        state = self._singing_state()
        state.update(
            {
                "ok": True,
                "applied": defaults,
                "errors": {},
                "persisted": persisted,
                "rebuilt_engine": rebuilt,
            }
        )
        logger.info("[情语] 唱歌控制已恢复默认（持久化=%s）", persisted)
        return self._json_ok(state)

    async def _unify_singing(self) -> dict:
        """一键统一：打开统一模式、清掉覆盖与换音色的绑定。

        「一次清空」不够——之后任何一次 /tts_bind 或面板改动都能再分叉。
        所以这里同时把 singing_unified_mode 打开，让群聊/私聊的差异从结构上不可能存在。
        唯一保留的例外是「某个会话禁用 TTS」，那是开关，不是配置差异。
        """
        reset_values = {
            "singing_target_seconds_private": 0,
            "singing_target_seconds_group": 0,
            "singing_max_seconds_private": 0,
            "singing_max_seconds_group": 0,
            "singing_quiet_hours_private": "",
            "singing_quiet_hours_group": "",
            "singing_unified_mode": True,
        }
        cleared_overrides: list[str] = []
        for key, value in reset_values.items():
            current = self.config.get(key)
            if current not in (None, "", 0, "0") and current is not value:
                cleared_overrides.append(f"{key}={current}")
            self.config[key] = value

        cleared_bindings = 0
        kept_disabled: dict[str, str] = {}
        for session_id, name in self._session_bindings.items():
            if name == DISABLED_ENGINE:
                kept_disabled[session_id] = name
            else:
                cleared_bindings += 1
        self._session_bindings = kept_disabled
        self._save_bindings()

        persisted = await self._persist_config()
        rebuilt = await self._rebuild_engines()
        logger.info(
            "[情语] 已统一（统一模式=开）：清覆盖 %s 项、清换音色绑定 %s 个、保留禁用 %s 个"
            "（持久化=%s, 重建引擎=%s）",
            len(cleared_overrides),
            cleared_bindings,
            len(kept_disabled),
            persisted,
            rebuilt or "无",
        )
        return {
            "cleared_overrides": cleared_overrides,
            "cleared_bindings": cleared_bindings,
            "kept_disabled": len(kept_disabled),
            "unified_mode": True,
            "persisted": persisted,
            "rebuilt_engine": rebuilt,
            "global_voice": self._global_voice_info(),
            "voice_unified": True,
        }

    async def api_singing_unify(self, **kwargs):
        """一键统一所有会话的唱歌配置。"""
        result = await self._unify_singing()
        state = self._singing_state()
        state.update({"ok": True, **result})
        return self._json_ok(state)

    # ---------- 自检 ----------

    def _segmented_reply_state(self) -> tuple[bool, str]:
        """读 AstrBot 的分段回复设置，判断会不会把语音切碎。

        分段回复把一条 LLM 回复拆成多条消息发出去。TTS 是在单条消息的结果上触发的，
        所以一旦被拆开，就会变成「好几条短语音」而不是一整段。

        返回值：(是否开启, 说明文字)。读不到就返回 (False, "")，不瞎猜。
        """
        reader = getattr(self.context, "get_config", None)
        if not callable(reader):
            return False, ""
        try:
            cfg = reader()
        except Exception:  # noqa: BLE001
            return False, ""
        if not isinstance(cfg, dict):
            return False, ""

        # 配置里可能叫 segmented_reply / segmented / segmented_reply_settings，
        # 用宽松匹配找，找不到就认为没开。
        def dig(node, depth=0):
            if depth > 4 or not isinstance(node, dict):
                return None
            for key, value in node.items():
                low = str(key).lower()
                if "segment" in low:
                    if isinstance(value, bool):
                        return value
                    if isinstance(value, dict):
                        for inner in ("enable", "enabled", "on"):
                            if inner in value:
                                return bool(value[inner])
                        return None
                found = dig(value, depth + 1)
                if found is not None:
                    return found
            return None

        state = dig(cfg)
        if state is None:
            return False, ""
        return bool(state), "AstrBot 的「分段回复」已开启"

    def _self_check(self) -> tuple[list[str], bool]:
        """一次检查完所有依赖项，回答「为什么某个功能用不了」。

        只报事实与可执行的下一步，不做猜测：能读到的状态就说，读不到的说读不到。
        """
        lines: list[str] = []
        problems: list[str] = []
        lines.append(f"版本　 {PLUGIN_VERSION}")

        engines = self._list_engines()
        engine = self._engine
        if not engines:
            problems.append("没有任何引擎：到「插件 → 配置 → 引擎列表」添加一个再重载插件")
            lines.append(f"引擎　 0 个 {MARK_BAD}")
        elif engine is None:
            problems.append("引擎配置存在但实例没建起来：看后台日志里的 [情语] 报错行")
            lines.append(f"引擎　 {len(engines)} 个，全局实例未创建 {MARK_BAD}")
        else:
            lines.append(f"引擎　 {len(engines)} 个，全局 {engine.instance_name} {MARK_OK}")

        if engine is not None:
            if self._supports_singing(engine):
                lines.append(f"唱歌　 可用 {MARK_OK}")
            else:
                reason = getattr(engine, "SINGING_UNSUPPORTED_REASON", "当前模型不支持唱歌")
                problems.append(f"唱歌不可用：{reason}")
                lines.append(f"唱歌　 不可用 {MARK_BAD}（{reason}）")

        # 配置漂移：音色被重置时这里会亮红灯
        if self._config_drift:
            lines.append(f"配置　 有 {len(self._config_drift)} 项被重置 {MARK_BAD}")
            for item in self._config_drift[:4]:
                lines.append(f"  · {item}")
            problems.append("配置被重置过（常见于插件更新），发 /tts_restore 恢复")
        else:
            lines.append(f"配置　 与上次一致 {MARK_OK}")

        # 音色：直接显示实际生效值
        try:
            _v = getattr(self._engine, "voice", "")
            _sv = getattr(self._engine, "singing_voice", "")
            lines.append(
                f"音色　 朗读 {_v or '(未设)'} / 唱歌 {_sv or '跟随朗读'}"
            )
        except Exception:  # noqa: BLE001
            pass

        # 克隆音色：样本与当前选择，直接摆出来，免得「不知道咋调用」
        try:
            clone_cfg = next(
                (
                    c
                    for c in self._list_engines()
                    if _as_str(c.get("__template_key")) == "mimo_v2_5_voiceclone"
                ),
                None,
            )
            samples = [
                Path(str(p)).name
                for p in (self.config.get("voice_clone_samples") or [])
                if str(p).strip()
            ]
            if clone_cfg is None:
                lines.append(
                    f"克隆　 未添加 VoiceClone 引擎（样本 {len(samples)} 个）"
                )
            elif not samples:
                lines.append(f"克隆　 引擎已建但没上传样本 {MARK_BAD}")
                problems.append(
                    "VoiceClone 没有样本：到配置页「音色克隆样本」上传一段 10~30 秒的清晰人声"
                )
            else:
                want = _as_str(clone_cfg.get("voice_sample_name")).strip()
                actual = want or f"{samples[0]}（默认取第一个）"
                ok = any(Path(s).name == want for s in samples) if want else True
                lines.append(
                    f"克隆　 {len(samples)} 个样本，当前 {actual} "
                    f"{MARK_OK if ok else MARK_BAD}"
                )
                if not ok:
                    problems.append(
                        f"克隆样本名 '{want}' 不在已上传列表里，去配置页「音色克隆样本」核对文件名"
                    )
        except Exception:  # noqa: BLE001
            lines.append("克隆　 状态读取失败")

        # 人设：改了不生效是常见困惑，这里把「读到了什么」直接摆出来
        try:
            _pm = getattr(self.context, "persona_manager", None)
            if _pm is None:
                lines.append(f"人设　 平台未提供 persona_manager {MARK_BAD}")
            else:
                lines.append(
                    f"人设　 接口可用 {MARK_OK}（每次事件重新读取，不吃旧缓存）"
                )
        except Exception:  # noqa: BLE001
            lines.append("人设　 读取接口异常")

        # 单条语音
        single = bool(self.config.get("tts_single_message", True))
        lines.append(
            f"单条语音 {'开' if single else '关'} "
            f"{MARK_OK if single else MARK_BAD}"
            + ("" if single else "（会被分段回复拆成多段）")
        )

        # 分段回复会把一条回复拆成多条消息，TTS 只拿到其中一段，语音就被切碎。
        segmented, _seg_note = self._segmented_reply_state()
        if segmented:
            lines.append(f"分段　 已开启 {MARK_BAD}（语音会被切成多段）")
            problems.append(
                "AstrBot 开启了「分段回复」：一条回复被拆成多条消息，"
                "语音也会跟着变成好几段。想让语音连贯，"
                "去 AstrBot 配置里关掉「分段回复 → 启用分段回复」"
            )
        else:
            lines.append(f"分段　 未开启 {MARK_OK}")

        hook_prompt = bool(HOOK_REGISTRY.get("on_llm_request"))
        hook_web = bool(HOOK_REGISTRY.get("web_api"))
        lines.append(
            f"钩子　 提示词{'已注册' if hook_prompt else '未注册'} {MARK_OK if hook_prompt else MARK_BAD}"
            f" / 面板接口{'已注册' if hook_web else '未注册'} {MARK_OK if hook_web else MARK_BAD}"
        )
        if not hook_prompt:
            problems.append(
                "提示词钩子未注册：模型不会主动写长歌词，此时靠关键词兜底或 /tts_sing 指定歌词"
            )
        if not hook_web:
            problems.append(
                "面板接口未注册：WebUI 里「唱歌控制」页面的按钮点不动，改用 /tts_set 与 /tts_panel"
            )

        view = Path(__file__).resolve().parent / "views" / "singing" / "index.html"
        if view.exists():
            lines.append(f"视图　 views/singing 就位 {MARK_OK}（需该版本 AstrBot 支持插件页面）")
        else:
            problems.append("视图文件缺失：重新安装插件包，确认 views/singing 下三个文件都在")
            lines.append(f"视图　 缺失 {MARK_BAD}")

        required = ("enable", "engines", "singing_length_preset", "singing_unified_mode")
        missing = [key for key in required if key not in self.config]
        if missing:
            # 这些键代码里都带默认值，缺了不影响功能，所以只提示、不算问题
            lines.append(
                f"配置　 {len(missing)} 项未写入（{'、'.join(missing)}），已用默认值兜底"
            )
        else:
            lines.append(f"配置　 {len(self.config)} 项，关键项齐全 {MARK_OK}")

        lines.append(
            f"生效　 私聊 {self._singing_target_seconds('private')} 秒 / "
            f"群聊 {self._singing_target_seconds('group')} 秒 · "
            f"统一模式{'开' if self._unified_mode() else '关'}"
        )

        ok = not problems
        lines.append("")
        lines.append("结论　 " + (f"各项可用 {MARK_OK}" if ok else f"发现 {len(problems)} 个问题"))
        for index, item in enumerate(problems, 1):
            lines.append(f"　{index}. {item}")
        return lines, ok

    async def terminate(self):
        """插件卸载时释放引擎资源"""
        if self._engine:
            await self._engine.close()
        for engine in self._cached_engines.values():
            await engine.close()
        self._cached_engines.clear()
        logger.info("[情语] 引擎资源已释放")
