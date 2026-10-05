"""
System One 决策客户端 - SystemOneJudge

Chat_PLUS Jev fork 新增模块：接入 System One（Jev 式）极速决策模型做
「是否加入话题主动发言」的判断，用于替代 / 复核 Chat_PLUS 原有的随机概率门。
支持两类接口（协议同构，配置 systemone_provider 切换）：
- aliyun    阿里云百炼 TokenPlan 官方接口（decision-model-preview）
- openrouter OpenRouter System One 接口（typesafe/jev-1.13 等模型）

设计要点：
1. 极速：aiohttp 直连 HTTPS，默认 4 秒超时；失败 / 超时 / 解析异常一律返回「未决策」None，
   由调用方降级回旧概率逻辑，绝不抛异常、绝不哑掉机器人。
2. 安全：内置限频保护——每群每小时主动插话上限、两次插话最小间隔、机器人刚说过话冷却、
   消息太短跳过、全局开关；API Key 留空时模块自我禁用并输出日志提示。
3. 可配：全局参数来自插件配置项（_conf_schema.json 的「System One 决策」区），
   支持每群覆盖 JSON（默认 plugin_data/astrbot_plugin_group_chat_plus/systemone_group_config.json）。
4. 零侵入：enable_systemone_decision=False 或 jev_mode=off 时模块完全禁用，插件行为与旧版一致。

请求：POST {base_url}  {"model": ..., "state": {...}, "questions": {...}}
响应：{"answers": {"join": {"type":"noul","noul":0.96}, ...}, "usage": ..., "latency_ms": ...}

作者: czyczy23（Jev fork，基于 Him666233/astrbot_plugin_group_chat_plus）
版本: V1.0.0
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:  # pragma: no cover - AstrBot 运行环境
    from astrbot import logger
except Exception:  # pragma: no cover - 脱离 AstrBot 环境（单元测试）时的兜底
    logger = logging.getLogger("astrbot_plugin_group_chat_plus.systemone_judge")

try:  # aiohttp 在 AstrBot 容器内可用；测试环境可注入伪实现
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None


# ========== 常量 ==========

# 接口提供商：systemone_provider 配置项的合法取值
PROVIDER_ALIYUN = "aliyun"
PROVIDER_OPENROUTER = "openrouter"

# 各提供商的默认端点与模型（systemone_base_url / systemone_model 留空时使用）。
# 两者的 System One 请求/响应协议同构（{"model","state","questions"} → {"answers":{...}}），
# 切换提供商只需改 provider 并换对应 API Key。
PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    PROVIDER_ALIYUN: {
        "base_url": (
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode"
            "/v1/systemone"
        ),
        "model": "decision-model-preview",
    },
    PROVIDER_OPENROUTER: {
        "base_url": "https://openrouter.ai/api/v1/systemone",
        "model": "typesafe/jev-1.13",
    },
}

DEFAULT_BASE_URL = PROVIDER_DEFAULTS[PROVIDER_ALIYUN]["base_url"]
DEFAULT_MODEL = PROVIDER_DEFAULTS[PROVIDER_ALIYUN]["model"]
DEFAULT_GROUP_CONFIG_REL = (
    "plugin_data/astrbot_plugin_group_chat_plus/systemone_group_config.json"
)
DEFAULT_PERSONA_SUMMARY = (
    "一个活泼但克制的群聊 Bot，喜欢音乐、美食和日常闲聊，"
    "话不多但自然，不刷屏、不啰嗦（建议在配置中按你的 Bot 人设改写）"
)

# jev_mode 取值
JEV_MODE_OFF = "off"
JEV_MODE_REPLACE = "replace"
JEV_MODE_HYBRID = "hybrid"
JEV_MODES = (JEV_MODE_OFF, JEV_MODE_REPLACE, JEV_MODE_HYBRID)

# 每群覆盖文件的重读间隔（秒）
GROUP_CONFIG_RELOAD_INTERVAL = 30.0

# mode choice 到回复提示的映射（克制：只给一句方式建议，不写内容模板）
MODE_HINTS = {
    "question": "可以用提问的方式接话：顺着当前话题向群友自然提一个小问题，不要求别人一定回答。",
    "share": "可以分享一点自己的相关经历或看法，简短自然，不要像在念稿。",
    "react": "只需要简短附和或吐槽一句，不要展开成长篇。",
}


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    """把配置值矫正为 [low, high] 区间内的 float，异常时返回默认值。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return max(low, min(high, result))


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """把配置值矫正为 [low, high] 区间内的 int，异常时返回默认值。"""
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, result))


def normalize_jev_mode(value: Any) -> str:
    """把 jev_mode 配置值规范化为 off / replace / hybrid（非法值按 off 处理）。"""
    text = str(value or "").strip().lower()
    if text in JEV_MODES:
        return text
    return JEV_MODE_OFF


class JudgeResult:
    """一次 System One 决策的结果。

    decided=False 表示「未决策」（禁用 / 未调用 / 失败 / 超时 / 消息太短），
    调用方必须降级到旧概率逻辑；decided=True 且 join=False 表示明确判定不插话。
    """

    __slots__ = (
        "decided",
        "join",
        "join_probability",
        "relevance",
        "mode",
        "reason",
        "source",
        "latency_ms",
        "confidence",
    )

    def __init__(
        self,
        decided: bool,
        join: bool,
        join_probability: Optional[float] = None,
        relevance: Optional[float] = None,
        mode: Optional[str] = None,
        reason: str = "",
        source: str = "",
        latency_ms: Optional[float] = None,
        confidence: Optional[float] = None,
    ):
        self.decided = bool(decided)
        self.join = bool(join)
        self.join_probability = join_probability
        self.relevance = relevance
        self.mode = mode
        self.reason = reason
        self.source = source
        self.latency_ms = latency_ms
        self.confidence = confidence

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        return (
            f"JudgeResult(decided={self.decided}, join={self.join}, "
            f"p={self.join_probability}, mode={self.mode}, source={self.source}, "
            f"reason={self.reason!r})"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decided": self.decided,
            "join": self.join,
            "join_probability": self.join_probability,
            "relevance": self.relevance,
            "mode": self.mode,
            "reason": self.reason,
            "source": self.source,
            "latency_ms": self.latency_ms,
            "confidence": self.confidence,
        }


class SystemOneJudge:
    """System One 决策客户端（类方法 + 类变量单例，风格与插件其他管理器一致）。"""

    # ========== 配置参数（由 main.py 的 SystemOneJudge.configure 注入） ==========
    _configured: bool = False
    _enable: bool = False
    _api_key: str = ""
    _base_url: str = DEFAULT_BASE_URL
    _model: str = DEFAULT_MODEL
    _provider: str = PROVIDER_ALIYUN
    _timeout: float = 4.0
    _jev_mode: str = JEV_MODE_OFF
    _join_threshold: float = 0.6
    _max_joins_per_hour: int = 8
    _min_join_interval_sec: int = 120
    _bot_cooldown_sec: int = 90
    _min_message_length: int = 6
    _state_max_messages: int = 12
    _message_max_chars: int = 60
    _enable_topic_relevance: bool = True
    _enable_mode_choice: bool = True
    _inject_mode_hint: bool = True
    _proactive_judge: bool = True
    _log_enabled: bool = True
    _persona_summary: str = DEFAULT_PERSONA_SUMMARY
    _group_config_path: str = DEFAULT_GROUP_CONFIG_REL
    _plugin_data_dir: str = ""

    # ========== 运行态 ==========
    # 每群覆盖配置 {群号: {...}}，来自 group_config_path（utf-8-sig，带 BOM）
    _group_overrides: Dict[str, dict] = {}
    _group_overrides_mtime: float = -1.0
    _group_overrides_checked_at: float = 0.0
    # 每群限频/冷却状态 {群号: {"join": [ts...], "proactive": [ts...], "last_speak": ts}}
    _group_state: Dict[str, dict] = {}
    _lock = threading.Lock()

    # ========== 测试注入点 ==========
    # 时间函数（测试可替换）；签名 () -> float
    clock: Callable[[], float] = staticmethod(time.time)
    # HTTP 钩子（测试可替换）；签名 async (url, payload, headers, timeout) -> (status, text)
    _http_post_hook: Optional[Callable] = None

    # ========== 初始化 ==========

    @classmethod
    def configure(cls, config: Optional[dict] = None, plugin_data_dir: str = "") -> None:
        """读取插件配置（由 main.py 在插件初始化时调用）。

        Args:
            config: 插件配置字典（AstrBotConfig 或普通 dict）
            plugin_data_dir: 插件数据目录（StarTools.get_data_dir()）
        """
        config = config or {}
        cls._plugin_data_dir = str(plugin_data_dir or "")

        cls._enable = bool(config.get("enable_systemone_decision", False))
        cls._api_key = str(config.get("systemone_api_key", "") or "").strip()

        # 接口提供商（aliyun=官方 TokenPlan / openrouter=OpenRouter System One）
        provider = str(config.get("systemone_provider", "") or "").strip().lower()
        if provider not in PROVIDER_DEFAULTS:
            provider = PROVIDER_ALIYUN
        cls._provider = provider

        base_url = str(config.get("systemone_base_url", "") or "").strip()
        cls._base_url = base_url or PROVIDER_DEFAULTS[provider]["base_url"]
        model = str(config.get("systemone_model", "") or "").strip()
        cls._model = model or PROVIDER_DEFAULTS[provider]["model"]

        cls._timeout = _clamp_float(config.get("systemone_timeout", 4), 4.0, 0.5, 30.0)
        cls._jev_mode = normalize_jev_mode(config.get("jev_mode", JEV_MODE_OFF))
        cls._join_threshold = _clamp_float(
            config.get("jev_join_threshold", 0.6), 0.6, 0.0, 1.0
        )
        cls._max_joins_per_hour = _clamp_int(
            config.get("jev_max_joins_per_hour", 8), 8, 0, 1000
        )
        cls._min_join_interval_sec = _clamp_int(
            config.get("jev_min_join_interval_sec", 120), 120, 0, 86400
        )
        cls._bot_cooldown_sec = _clamp_int(
            config.get("jev_bot_cooldown_sec", 90), 90, 0, 86400
        )
        cls._min_message_length = _clamp_int(
            config.get("jev_min_message_length", 6), 6, 0, 200
        )
        cls._state_max_messages = _clamp_int(
            config.get("jev_state_max_messages", 12), 12, 8, 15
        )
        cls._message_max_chars = _clamp_int(
            config.get("jev_message_max_chars", 60), 60, 10, 500
        )
        cls._enable_topic_relevance = bool(
            config.get("jev_enable_topic_relevance", True)
        )
        cls._enable_mode_choice = bool(config.get("jev_enable_mode_choice", True))
        cls._inject_mode_hint = bool(config.get("jev_inject_mode_hint", True))
        cls._proactive_judge = bool(config.get("jev_proactive_judge", True))
        cls._log_enabled = bool(config.get("jev_log_enabled", True))

        persona_summary = str(
            config.get("jev_persona_summary", "") or ""
        ).strip()
        cls._persona_summary = persona_summary or DEFAULT_PERSONA_SUMMARY

        group_path = str(config.get("jev_per_group_config_path", "") or "").strip()
        cls._group_config_path = group_path or DEFAULT_GROUP_CONFIG_REL

        cls._configured = True
        # 配置（重）加载后清空运行态，避免旧配置的限频状态残留
        with cls._lock:
            cls._group_state = {}
        cls._group_overrides = {}
        cls._group_overrides_mtime = -1.0
        cls._group_overrides_checked_at = 0.0
        cls.load_group_overrides(force=True)

        cls._log_startup_status()

    @classmethod
    def _log_startup_status(cls) -> None:
        """输出初始化状态；API Key 留空等自我禁用情况给出明确日志提示。"""
        if not cls._enable:
            logger.info(
                "🧠 [SystemOne] 决策模块未启用（enable_systemone_decision=false），"
                "插件保持原有随机概率行为"
            )
            return
        if not cls._api_key:
            logger.warning(
                "⚠️ [SystemOne] 决策模块已自我禁用：systemone_api_key 为空。"
                "请在插件配置「System One 决策」区填写阿里云百炼 API Key 后重启插件"
            )
            return
        if cls._jev_mode == JEV_MODE_OFF:
            logger.info(
                "🧠 [SystemOne] 决策模块已配置，但全局 jev_mode=off"
                "（仅每群覆盖 JSON 中显式设置了 jev_mode 的群会介入决策）"
            )
            return
        logger.info(
            f"🧠 [SystemOne] 决策模块已启用：provider={cls._provider}, "
            f"model={cls._model}, endpoint={cls._base_url}, "
            f"jev_mode={cls._jev_mode}, 阈值={cls._join_threshold:.2f}, "
            f"每小时上限={cls._max_joins_per_hour}(0=不限), "
            f"最小间隔={cls._min_join_interval_sec}s, 超时={cls._timeout:.1f}s"
        )
        if cls._group_overrides:
            logger.info(
                f"🧠 [SystemOne] 已加载每群覆盖配置 {len(cls._group_overrides)} 项"
            )

    @classmethod
    def is_active(cls) -> bool:
        """模块是否已具备决策条件（全局开关 + API Key）。

        注意：具体某个群是否参与决策还要看 get_group_jev_mode()——每群可在覆盖
        JSON 中单独开关（enable=false 或 jev_mode=off）。未启用 / Key 为空 /
        全局 jev_mode=off 且该群无覆盖时，_judge() 都会直接返回「未决策」，
        不会发起任何网络请求。
        """
        return bool(cls._configured and cls._enable and cls._api_key)

    @classmethod
    def is_group_active(cls, group_id: Any) -> bool:
        """某个群当前是否会真正走 System One 决策。"""
        return cls.is_active() and cls.get_group_jev_mode(group_id) != JEV_MODE_OFF

    # ========== 每群覆盖配置 ==========

    @classmethod
    def get_group_config_path(cls) -> Optional[Path]:
        """解析每群覆盖 JSON 的路径（支持绝对路径与相对 AstrBot 数据目录的相对路径）。"""
        raw = str(cls._group_config_path or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path
        if not cls._plugin_data_dir:
            return Path.cwd() / path
        plugin_data = Path(cls._plugin_data_dir)
        # plugin_data_dir 形如 <AstrBot data>/plugin_data/<插件名>
        data_root = (
            plugin_data.parent.parent
            if plugin_data.parent.name == "plugin_data"
            else plugin_data
        )
        first_part = path.parts[0] if path.parts else ""
        base = data_root if first_part == "plugin_data" else plugin_data
        return base / path

    @classmethod
    def load_group_overrides(cls, force: bool = False) -> Dict[str, dict]:
        """读取每群覆盖配置（utf-8-sig，容忍 BOM；带 mtime 缓存与 30 秒节流）。

        文件不存在 / 解析失败时保持「空覆盖」（即全部走全局默认），绝不抛异常。
        """
        now = cls.clock()
        if not force and (now - cls._group_overrides_checked_at) < GROUP_CONFIG_RELOAD_INTERVAL:
            return cls._group_overrides
        cls._group_overrides_checked_at = now

        path = cls.get_group_config_path()
        if path is None:
            return cls._group_overrides
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # 文件不存在：不是错误，保持空覆盖
            cls._group_overrides = {}
            cls._group_overrides_mtime = -1.0
            return cls._group_overrides

        if not force and mtime == cls._group_overrides_mtime:
            return cls._group_overrides

        try:
            with open(path, "r", encoding="utf-8-sig") as fp:
                data = json.load(fp)
            if not isinstance(data, dict):
                raise ValueError("顶层结构必须是 JSON 对象 {群号: {...}}")
            cleaned: Dict[str, dict] = {}
            for group_id, item in data.items():
                if isinstance(item, dict):
                    cleaned[str(group_id)] = item
            cls._group_overrides = cleaned
            cls._group_overrides_mtime = mtime
            if cls._log_enabled:
                logger.info(
                    f"🧠 [SystemOne] 每群覆盖配置已加载：{path}（{len(cleaned)} 项）"
                )
        except Exception as e:
            logger.warning(
                f"⚠️ [SystemOne] 每群覆盖配置读取失败，已回退为全局默认：{path} ({e})"
            )
            cls._group_overrides = {}
            cls._group_overrides_mtime = -1.0
        return cls._group_overrides

    @classmethod
    def get_group_setting(cls, group_id: Any, key: str, default: Any) -> Any:
        """获取某群的参数：优先每群覆盖，其次全局默认。"""
        overrides = cls._group_overrides
        if overrides:
            item = overrides.get(str(group_id))
            if isinstance(item, dict) and key in item:
                return item.get(key)
        return default

    @classmethod
    def get_group_jev_mode(cls, group_id: Any) -> str:
        """获取某群生效的 jev_mode（每群可单独设置；enable=false 等价于 off）。"""
        overrides = cls._group_overrides
        if overrides:
            item = overrides.get(str(group_id))
            if isinstance(item, dict):
                if item.get("enable") is False:
                    return JEV_MODE_OFF
                if "jev_mode" in item:
                    return normalize_jev_mode(item.get("jev_mode"))
        return cls._jev_mode

    @classmethod
    def get_group_threshold(cls, group_id: Any) -> float:
        return _clamp_float(
            cls.get_group_setting(group_id, "join_threshold", cls._join_threshold),
            cls._join_threshold,
            0.0,
            1.0,
        )

    @classmethod
    def get_group_max_joins_per_hour(cls, group_id: Any) -> int:
        return _clamp_int(
            cls.get_group_setting(
                group_id, "max_joins_per_hour", cls._max_joins_per_hour
            ),
            cls._max_joins_per_hour,
            0,
            1000,
        )

    @classmethod
    def get_group_min_join_interval(cls, group_id: Any) -> int:
        return _clamp_int(
            cls.get_group_setting(
                group_id, "min_join_interval_sec", cls._min_join_interval_sec
            ),
            cls._min_join_interval_sec,
            0,
            86400,
        )

    @classmethod
    def ensure_group_config_file(cls, force: bool = False) -> Optional[Path]:
        """确保每群覆盖文件存在（写入带 UTF-8 BOM 的空对象模板）。

        仅在模块真正启用时创建；写失败只记日志，不影响主流程。
        """
        if not force and not cls.is_active():
            return None
        path = cls.get_group_config_path()
        if path is None:
            return None
        try:
            if path.exists() and not force:
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            if force or not path.exists():
                with open(path, "w", encoding="utf-8-sig") as fp:
                    fp.write(json.dumps({}, ensure_ascii=False, indent=4))
                    fp.write("\n")
                logger.info(
                    f"🧠 [SystemOne] 已创建每群覆盖配置模板（UTF-8 BOM）：{path}"
                )
            return path
        except Exception as e:
            logger.warning(f"⚠️ [SystemOne] 创建每群覆盖配置模板失败：{path} ({e})")
            return None

    # ========== 限频 / 冷却保护 ==========

    @classmethod
    def _get_state(cls, group_id: Any) -> dict:
        """获取（并初始化）某群的运行态。调用方需自行加锁或接受 GIL 下的简单读写。"""
        key = str(group_id)
        state = cls._group_state.get(key)
        if state is None:
            state = {"join": [], "proactive": [], "last_speak": 0.0}
            cls._group_state[key] = state
        return state

    @classmethod
    def _check_guard(cls, group_id: Any, kind: str) -> Optional[str]:
        """检查限频 / 冷却保护；返回拦截原因，None 表示放行。"""
        now = cls.clock()
        cutoff = now - 3600.0
        with cls._lock:
            state = cls._get_state(group_id)
            bucket = state.setdefault(kind, [])
            # 清理 1 小时前的记录（同时限制内存增长）
            bucket[:] = [ts for ts in bucket if ts > cutoff]

            max_per_hour = cls.get_group_max_joins_per_hour(group_id)
            if max_per_hour > 0 and len(bucket) >= max_per_hour:
                return f"本群每小时主动插话已达上限({max_per_hour}次)"

            min_interval = cls.get_group_min_join_interval(group_id)
            if min_interval > 0 and bucket:
                remaining = int(min_interval - (now - max(bucket)))
                if remaining > 0:
                    return f"距离上次主动插话不足{min_interval}秒（剩余{remaining}秒）"

            if cls._bot_cooldown_sec > 0 and state.get("last_speak", 0.0) > 0:
                remaining = int(
                    cls._bot_cooldown_sec - (now - float(state["last_speak"]))
                )
                if remaining > 0:
                    return f"机器人刚发过言，冷却中（剩余{remaining}秒）"
        return None

    @classmethod
    def note_join(cls, group_id: Any, kind: str = "join") -> None:
        """记录一次「已决定插话」（用于每小时的决策额度与最小间隔统计）。

        说明：这里只记决策额度，真实发言时间由 record_bot_reply() 单独记录，
        两者语义不同——决策通过但回复生成失败时，不应占用「刚说过话」冷却。
        """
        try:
            now = cls.clock()
            with cls._lock:
                state = cls._get_state(group_id)
                state.setdefault(kind, []).append(now)
        except Exception:  # pragma: no cover - 记账失败不影响决策
            pass

    @classmethod
    def record_bot_reply(cls, group_id: Any) -> None:
        """记录机器人实际发言时间（用于「刚说过话」冷却）。

        未介入决策的群直接跳过，保证 jev_mode=off 时零行为变化、零内存增长。
        """
        try:
            if not cls.is_group_active(group_id):
                return
            now = cls.clock()
            with cls._lock:
                state = cls._get_state(group_id)
                state["last_speak"] = now
        except Exception:  # pragma: no cover
            pass

    @classmethod
    def reset_runtime_state(cls) -> None:
        """清空运行态（测试 / 热重载用）。"""
        with cls._lock:
            cls._group_state = {}

    # ========== 决策入口 ==========

    @classmethod
    async def judge_join(
        cls,
        group_id: Any,
        messages: Optional[List[Any]] = None,
        current_message: Optional[dict] = None,
        persona_summary: Optional[str] = None,
    ) -> Optional[JudgeResult]:
        """普通消息插话判断：是否应该主动加入当前话题。

        Returns:
            JudgeResult（decided=True 时 join 表示判定结果）；
            None 表示「未决策」，调用方必须降级到旧概率逻辑。
        """
        return await cls._judge(
            group_id,
            messages=messages,
            current_message=current_message,
            persona_summary=persona_summary,
            kind="join",
        )

    @classmethod
    async def judge_proactive(
        cls,
        group_id: Any,
        messages: Optional[List[Any]] = None,
        persona_summary: Optional[str] = None,
    ) -> Optional[JudgeResult]:
        """沉默开话题判断：当前是否值得主动开场。

        返回语义同 judge_join；None 表示未决策（降级到旧随机概率）。
        """
        if not cls._proactive_judge:
            return None
        return await cls._judge(
            group_id,
            messages=messages,
            current_message=None,
            persona_summary=persona_summary,
            kind="proactive",
        )

    @classmethod
    async def _judge(
        cls,
        group_id: Any,
        messages: Optional[List[Any]],
        current_message: Optional[dict],
        persona_summary: Optional[str],
        kind: str,
    ) -> Optional[JudgeResult]:
        """决策主流程；任何异常都在这里被吞掉并降级为「未决策」。"""
        try:
            if not cls.is_active():
                return None

            cls.load_group_overrides()
            group_key = str(group_id)
            if cls.get_group_jev_mode(group_key) == JEV_MODE_OFF:
                return None

            # 保护 1：消息太短不浪费一次决策（返回未决策 → 调用方走旧概率逻辑）
            latest_text = cls._extract_text(current_message)
            if not latest_text and messages:
                latest_text = cls._extract_text(messages[-1])
            if cls._min_message_length > 0 and latest_text:
                if len(latest_text.strip()) < cls._min_message_length:
                    if cls._log_enabled:
                        logger.info(
                            f"🧠 [SystemOne] 群{group_key} 跳过本次决策："
                            f"消息过短（{len(latest_text.strip())}<{cls._min_message_length}字）"
                        )
                    return None

            # 保护 2/3/4：每小时上限 / 最小间隔 / 机器人刚说话冷却
            blocked_reason = cls._check_guard(group_key, kind)
            if blocked_reason:
                if cls._log_enabled:
                    logger.info(
                        f"🛡️ [SystemOne] 群{group_key} 触发保护，本次不插话：{blocked_reason}"
                    )
                return JudgeResult(
                    decided=True,
                    join=False,
                    reason=blocked_reason,
                    source="guard",
                )

            state = cls._build_state(group_key, messages, current_message, persona_summary)
            questions = cls._build_questions(kind)
            payload = {"model": cls._model, "state": state, "questions": questions}

            start = cls.clock()
            data = await cls._post_json(payload)
            latency_ms = max(0.0, (cls.clock() - start) * 1000.0)
            if data is None:
                return None

            parsed = cls._parse_response(data)
            if parsed is None:
                return None

            join_prob = parsed["join"]
            threshold = cls.get_group_threshold(group_key)
            passed = join_prob >= threshold
            if passed:
                cls.note_join(group_key, kind)

            result = JudgeResult(
                decided=True,
                join=passed,
                join_probability=join_prob,
                relevance=parsed.get("relevance"),
                mode=parsed.get("mode"),
                reason="通过阈值" if passed else "未达阈值",
                source="systemone",
                latency_ms=parse_latency(data, latency_ms),
                confidence=parsed.get("confidence"),
            )
            cls._log_decision(group_key, kind, result, threshold)
            return result
        except Exception as e:  # 兜底：任何异常都降级，绝不外抛
            try:
                logger.warning(
                    f"⚠️ [SystemOne] 决策过程异常，已降级到旧概率逻辑: "
                    f"{type(e).__name__}: {e}"
                )
            except Exception:  # pragma: no cover
                pass
            return None

    # ========== HTTP ==========

    @classmethod
    async def _post_json(cls, payload: dict) -> Optional[dict]:
        """POST 到 System One 端点；失败 / 超时 / 非 200 返回 None（由调用方降级）。"""
        headers = {
            "Authorization": f"Bearer {cls._api_key}",
            "Content-Type": "application/json",
        }
        if cls._provider == PROVIDER_OPENROUTER:
            # OpenRouter 归属头（可选）：在 openrouter.ai 排行榜中标注调用来源
            headers.setdefault(
                "HTTP-Referer", "https://github.com/czyczy23/astrbot_plugin_jev"
            )
            headers.setdefault("X-Title", "astrbot_plugin_jev")
        timeout = float(cls._timeout)
        try:
            if cls._http_post_hook is not None:
                status, text = await cls._http_post_hook(
                    cls._base_url, payload, headers, timeout
                )
            else:
                if aiohttp is None:
                    logger.warning(
                        "⚠️ [SystemOne] 当前环境缺少 aiohttp，决策模块降级到旧概率逻辑"
                    )
                    return None
                client_timeout = aiohttp.ClientTimeout(total=timeout)
                async with aiohttp.ClientSession(timeout=client_timeout) as session:
                    async with session.post(
                        cls._base_url, json=payload, headers=headers
                    ) as resp:
                        status = resp.status
                        text = await resp.text()
        except asyncio.TimeoutError:
            logger.warning(
                f"⚠️ [SystemOne] 决策请求超时（{timeout:.1f}s），已降级到旧概率逻辑"
            )
            return None
        except Exception as e:
            logger.warning(
                f"⚠️ [SystemOne] 决策请求失败（{type(e).__name__}: {e}），"
                "已降级到旧概率逻辑"
            )
            return None

        if int(status) != 200:
            snippet = str(text or "")[:200].replace("\n", " ")
            logger.warning(
                f"⚠️ [SystemOne] 决策服务返回 HTTP {status}: {snippet}"
                "（已降级到旧概率逻辑）"
            )
            return None

        try:
            data = json.loads(text)
        except Exception as e:
            logger.warning(
                f"⚠️ [SystemOne] 决策响应不是合法 JSON（{e}），已降级到旧概率逻辑"
            )
            return None

        if isinstance(data, dict) and data.get("error"):
            logger.warning(
                f"⚠️ [SystemOne] 决策服务返回错误: {str(data.get('error'))[:200]}"
                "（已降级到旧概率逻辑）"
            )
            return None
        if not isinstance(data, dict):
            logger.warning("⚠️ [SystemOne] 决策响应结构异常，已降级到旧概率逻辑")
            return None
        return data

    # ========== 解析 ==========

    @classmethod
    def _parse_response(cls, data: dict) -> Optional[dict]:
        """解析 System One 响应；join 缺失 / 非法时返回 None（未决策）。"""
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            logger.warning("⚠️ [SystemOne] 响应缺少 answers 字段，已降级到旧概率逻辑")
            return None

        join_prob = extract_probability(answers.get("join"))
        if join_prob is None:
            logger.warning(
                "⚠️ [SystemOne] 响应缺少合法的 join 概率，已降级到旧概率逻辑"
            )
            return None

        mode_block = answers.get("mode")
        return {
            "join": join_prob,
            "relevance": extract_score(answers.get("topic_relevance")),
            "mode": extract_choice(mode_block),
            "confidence": extract_confidence(mode_block),
        }

    # ========== state / questions 构建 ==========

    @classmethod
    def _build_state(
        cls,
        group_id: str,
        messages: Optional[List[Any]],
        current_message: Optional[dict],
        persona_summary: Optional[str],
    ) -> dict:
        """构建送给模型的 state：一句话人设 + 最近若干条群消息（每条截断）。"""
        recent = normalize_messages(
            messages or [], cls._state_max_messages, cls._message_max_chars
        )
        current_line = normalize_message(current_message, cls._message_max_chars)
        if current_line:
            recent.append(current_line)
            recent = recent[-cls._state_max_messages :]
        return {
            "persona": (persona_summary or cls._persona_summary),
            "group_id": str(group_id),
            "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "recent_messages": recent,
        }

    @classmethod
    def _build_questions(cls, kind: str) -> dict:
        """构建 2~3 个问题：join(noul，必须) + topic_relevance(score 4级) + mode(choice)。"""
        if kind == "proactive":
            questions: Dict[str, dict] = {
                "join": {
                    "type": "noul",
                    "instructions": (
                        "群里已经安静了一段时间，机器人正在考虑主动开口开启一个新话题。"
                        "现在是否值得主动开场？（有自然、不尴尬的话题切入点，"
                        "且开口不会打扰到群友时为是；如果只是硬找话说则为否）"
                    ),
                    "criteria": {
                        "true": "值得主动开场",
                        "false": "不值得，保持安静更好",
                    },
                }
            }
            if cls._enable_mode_choice:
                questions["mode"] = {
                    "type": "choice",
                    "instructions": "若主动开口，用哪种方式最自然？",
                    "criteria": {
                        "question": "抛出一个轻松的问题引发讨论",
                        "share": "分享自己的近况或兴趣",
                        "react": "自言自语式地感慨或吐槽",
                        "other": "其它",
                    },
                }
            return questions

        questions = {
            "join": {
                "type": "noul",
                "instructions": (
                    "群友正在聊天，机器人是否应该主动插话加入当前话题？"
                    "（话题与其人设兴趣（见 state.persona）相关、有人在征求意见"
                    "或气氛适合接话时为是；话题已经聊完、插话会显得突兀时为否）"
                ),
                "criteria": {
                    "true": "应该插话",
                    "false": "不该插话，继续看着就好",
                },
            }
        }
        if cls._enable_topic_relevance:
            questions["topic_relevance"] = {
                "type": "score",
                "instructions": (
                    "当前话题与该机器人人设兴趣（见 state.persona）"
                    "的相关程度"
                ),
                "criteria": ["完全无关", "稍微相关", "比较相关", "高度相关"],
            }
        if cls._enable_mode_choice:
            questions["mode"] = {
                "type": "choice",
                "instructions": "若机器人插话，应以何种方式？",
                "criteria": {
                    "question": "向群友提问互动",
                    "share": "分享自己的相关经历",
                    "react": "简短附和或吐槽",
                    "other": "其它",
                },
            }
        return questions

    @classmethod
    def _extract_text(cls, message: Any) -> str:
        """从消息 dict / 字符串里取出文本（失败返回空串）。"""
        if message is None:
            return ""
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            for key in ("content", "text", "message", "raw_message"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    # ========== 日志 / 提示 ==========

    @classmethod
    def _log_decision(
        cls, group_id: str, kind: str, result: JudgeResult, threshold: float
    ) -> None:
        if not cls._log_enabled:
            return
        try:
            label = "沉默开场" if kind == "proactive" else "插话"
            prob = result.join_probability
            prob_text = f"{prob:.2f}" if isinstance(prob, float) else "?"
            latency_text = (
                f"{result.latency_ms:.0f}ms"
                if isinstance(result.latency_ms, (int, float))
                else "?"
            )
            extra = []
            if result.relevance is not None:
                extra.append(f"相关度={result.relevance:.2f}")
            if result.mode:
                extra.append(f"方式={result.mode}")
            extra_text = (" | " + " ".join(extra)) if extra else ""
            decision_text = "通过" if result.join else "不插话"
            logger.info(
                f"🧠 [SystemOne] 群{group_id} {label}判断: {decision_text} "
                f"join={prob_text} (阈值{threshold:.2f}){extra_text} | 延迟={latency_text}"
            )
        except Exception:  # pragma: no cover - 日志失败不影响主流程
            pass

    @classmethod
    def build_mode_hint(cls, mode: Optional[str]) -> str:
        """把 mode choice 结果转成一句克制的回复方式建议（空串表示不注入）。"""
        hint = MODE_HINTS.get(str(mode or "").strip().lower(), "")
        if not hint:
            return ""
        return f"[系统信息-发言方式建议: {hint}（保持人格与口语风格，不要解释这条建议）]"

    # ========== 外部辅助 ==========

    @classmethod
    def collect_recent_messages(
        cls, plugin_instance: Any, chat_id: Any, limit: Optional[int] = None
    ) -> List[Any]:
        """从插件消息缓存里取最近消息，供 state 构建使用（失败返回空列表）。"""
        try:
            cache = getattr(plugin_instance, "pending_messages_cache", None)
            if not isinstance(cache, dict):
                return []
            raw = cache.get(str(chat_id))
            if not raw:
                return []
            items = [item for item in list(raw) if isinstance(item, dict)]
            effective_limit = cls._state_max_messages if limit is None else limit
            if effective_limit and effective_limit > 0:
                items = items[-int(effective_limit) :]
            return items
        except Exception:
            return []


# ========== 模块级解析工具（便于单测） ==========


def extract_probability(value: Any) -> Optional[float]:
    """从 noul 响应块中提取 P(yes)，规范化到 [0, 1]；无法解析返回 None。"""
    if isinstance(value, dict):
        raw = None
        for key in ("noul", "probability", "yes", "value", "score"):
            if key in value:
                raw = value.get(key)
                break
        if raw is None:
            return None
    else:
        raw = value
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return max(0.0, min(1.0, result))


def extract_score(value: Any) -> Optional[float]:
    """从 score 响应块中提取概率加权期望值；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("score", "value", "level", "index"):
            if key in value:
                value = value.get(key)
                break
        else:
            return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:
        return None
    return result


def extract_choice(value: Any) -> Optional[str]:
    """从 choice 响应块中提取选项名并归一化为 question/share/react/other。"""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("choice")
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if not key:
        return None
    mapping = {
        "question": "question",
        "ask": "question",
        "提问": "question",
        "share": "share",
        "分享": "share",
        "react": "react",
        "reaction": "react",
        "agree": "react",
        "附和": "react",
        "吐槽": "react",
        "other": "other",
        "其它": "other",
        "其他": "other",
    }
    return mapping.get(key, "other")


def extract_confidence(value: Any) -> Optional[float]:
    """提取 choice / score 响应块的 confidence（缺省返回 None）。"""
    if not isinstance(value, dict):
        return None
    try:
        result = float(value.get("confidence"))
    except (TypeError, ValueError):
        return None
    if result != result:
        return None
    return result


def parse_latency(data: dict, fallback_ms: float) -> float:
    """优先使用服务端 latency_ms，缺失时用本地测得值。"""
    try:
        if isinstance(data, dict):
            server_latency = float(data.get("latency_ms"))
            if server_latency == server_latency and server_latency >= 0:
                return server_latency
    except (TypeError, ValueError):
        pass
    return max(0.0, float(fallback_ms))


def normalize_message(message: Any, max_chars: int = 60) -> str:
    """把单条消息规范化为「发送者（时间）：内容」字符串（内容按 max_chars 截断）。"""
    if message is None:
        return ""
    if isinstance(message, str):
        text = message.strip()
        return text[:max_chars] if max_chars > 0 else text
    if not isinstance(message, dict):
        return ""

    text = ""
    for key in ("content", "text", "message", "raw_message"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    if not text:
        return ""
    text = " ".join(text.split())
    if max_chars > 0:
        text = text[:max_chars]

    sender = ""
    for key in ("sender_name", "sender", "nickname", "user_name"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            sender = value.strip()
            break
    if not sender:
        sender_id = message.get("sender_id") or message.get("user_id")
        if sender_id not in (None, ""):
            sender = f"用户{sender_id}"

    time_text = ""
    raw_ts = message.get("timestamp")
    if raw_ts is None:
        raw_ts = message.get("message_timestamp")
    if raw_ts is None:
        raw_ts = message.get("time")
    try:
        if raw_ts is not None:
            ts = float(raw_ts)
            if ts > 1e11:  # 毫秒时间戳
                ts = ts / 1000.0
            if ts > 0:
                time_text = datetime.fromtimestamp(ts).strftime("%H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        time_text = ""

    if sender and time_text:
        return f"{sender}（{time_text}）：{text}"
    if sender:
        return f"{sender}：{text}"
    if time_text:
        return f"（{time_text}）{text}"
    return text


def normalize_messages(
    messages: Any, limit: int = 12, max_chars: int = 60
) -> List[str]:
    """规范化消息列表：过滤空行、只保留最近 limit 条。"""
    if not messages:
        return []
    if not isinstance(messages, (list, tuple)):
        messages = [messages]
    lines: List[str] = []
    for item in messages:
        line = normalize_message(item, max_chars)
        if line:
            lines.append(line)
    if limit and limit > 0:
        lines = lines[-int(limit) :]
    return lines
