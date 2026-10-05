"""
System One 决策客户端单元测试 - test_systemone_judge

不依赖 AstrBot 运行环境：直接以文件方式加载 utils/systemone_judge.py，
并通过 SystemOneJudge._http_post_hook / clock 注入伪实现（不发起真实网络请求）。

运行方式（插件目录下）：
    python3 -m unittest discover -s tests -v
    python3 -m pytest tests/ -v        # 若环境安装了 pytest 亦可

覆盖范围：
1. 响应解析（noul / score / choice、异常结构、错误体、非法 JSON）
2. 阈值判定（P ≥ 阈值 才插话）
3. 限频保护（每小时上限 / 最小间隔 / 机器人刚说话冷却 / 太短消息跳过）
4. 降级（禁用、API Key 空、HTTP 错误、超时、网络异常 → 一律「未决策」且不抛异常）
5. 每群覆盖配置（UTF-8 BOM 文件、缺失群回退全局、文件损坏回退）
6. state / questions 构建与 mode 提示

作者: czyczy23（Jev fork，基于 Him666233/astrbot_plugin_group_chat_plus）
版本: V1.0.0
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "utils" / "systemone_judge.py"

spec = importlib.util.spec_from_file_location("systemone_judge_under_test", MODULE_PATH)
systemone_judge = importlib.util.module_from_spec(spec)
sys.modules["systemone_judge_under_test"] = systemone_judge
spec.loader.exec_module(systemone_judge)

SystemOneJudge = systemone_judge.SystemOneJudge
JudgeResult = systemone_judge.JudgeResult


def build_response(join=0.96, mode="question", relevance=2.12):
    """构造与线上实测一致的 System One 响应体。"""
    answers = {
        "join": {"type": "noul", "noul": join},
    }
    if relevance is not None:
        answers["topic_relevance"] = {
            "type": "score",
            "score": relevance,
            "confidence": 0.71,
            "legend": {"0": "完全无关", "1": "稍微相关", "2": "比较相关", "3": "高度相关"},
        }
    if mode is not None:
        answers["mode"] = {
            "type": "choice",
            "choice": mode,
            "confidence": 0.56,
            "probabilities": {mode: 0.67},
        }
    return {
        "model": "decision-model-preview",
        "request_id": "test-request",
        "answers": answers,
        "usage": {"input_tokens": 183},
        "latency_ms": 64.1,
    }


BASE_CONFIG = {
    "enable_systemone_decision": True,
    "systemone_api_key": "sk-sp-unit-test-key",
    "systemone_base_url": "https://example.invalid/systemone",
    "systemone_model": "decision-model-preview",
    "systemone_timeout": 4.0,
    "jev_mode": "replace",
    "jev_join_threshold": 0.6,
    "jev_max_joins_per_hour": 8,
    "jev_min_join_interval_sec": 120,
    "jev_bot_cooldown_sec": 90,
    "jev_min_message_length": 6,
    "jev_state_max_messages": 12,
    "jev_message_max_chars": 60,
    "jev_enable_topic_relevance": True,
    "jev_enable_mode_choice": True,
    "jev_inject_mode_hint": True,
    "jev_proactive_judge": True,
    "jev_log_enabled": False,
    "jev_persona_summary": "",
    "jev_per_group_config_path": "",
}


class JudgeTestCase(unittest.IsolatedAsyncioTestCase):
    """公共夹具：注入时间与 HTTP 伪实现，隔离每个用例的运行态。"""

    def setUp(self):
        self.now = 1_000_000.0
        self.calls = []
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        SystemOneJudge.clock = lambda: self.now
        SystemOneJudge._http_post_hook = None
        SystemOneJudge.reset_runtime_state()

    def configure(self, **overrides):
        config = dict(BASE_CONFIG)
        config["jev_per_group_config_path"] = str(
            Path(self.tmpdir.name) / "group_config.json"
        )
        config.update(overrides)
        SystemOneJudge.configure(config)
        return config

    def mock_http(self, body=None, status=200, raw_text=None):
        """注册伪 HTTP 钩子：记录调用参数并返回指定响应。"""
        text = raw_text if raw_text is not None else json.dumps(
            body if body is not None else build_response(), ensure_ascii=False
        )

        async def hook(url, payload, headers, timeout):
            self.calls.append(
                {
                    "url": url,
                    "payload": payload,
                    "headers": headers,
                    "timeout": timeout,
                }
            )
            return status, text

        SystemOneJudge._http_post_hook = hook

    def mock_http_error(self, exc):
        async def hook(url, payload, headers, timeout):
            self.calls.append({"url": url, "payload": payload})
            raise exc

        SystemOneJudge._http_post_hook = hook

    @staticmethod
    def messages(count=3, text="有没有人一起去漫展啊", sender="群友A"):
        return [
            {
                "sender_name": f"{sender}{index}",
                "content": f"{text}{index}",
                "timestamp": 1_700_000_000 + index,
            }
            for index in range(count)
        ]

    def write_group_config(self, data, bom=True):
        path = Path(SystemOneJudge.get_group_config_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        encoding = "utf-8-sig" if bom else "utf-8"
        with open(path, "w", encoding=encoding) as fp:
            if isinstance(data, str):
                fp.write(data)
            else:
                fp.write(json.dumps(data, ensure_ascii=False, indent=4))
        return path


# ========== 1. 开关与自我禁用 ==========


class TestEnableSwitch(JudgeTestCase):
    async def test_disabled_by_default(self):
        self.configure(enable_systemone_decision=False, jev_mode="off")
        self.assertFalse(SystemOneJudge.is_active())
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)
        self.assertEqual(self.calls, [], "禁用时不应发起任何 HTTP 请求")

    async def test_disabled_when_api_key_empty(self):
        self.configure(systemone_api_key="")
        self.assertFalse(SystemOneJudge.is_active())
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])

    async def test_disabled_when_jev_mode_off(self):
        self.configure(jev_mode="off")
        # 全局 off：模块已配置但任何群都不会真正介入
        self.assertFalse(SystemOneJudge.is_group_active("10001"))
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)
        self.assertEqual(self.calls, [], "jev_mode=off 时不应发起任何 HTTP 请求")

    async def test_active_when_all_conditions_met(self):
        self.configure()
        self.assertTrue(SystemOneJudge.is_active())
        self.assertTrue(SystemOneJudge.is_group_active("10001"))

    async def test_invalid_jev_mode_normalized_to_off(self):
        self.configure(jev_mode="REPLACE")
        self.assertEqual(SystemOneJudge._jev_mode, "replace")
        self.configure(jev_mode="whatever")
        self.assertEqual(SystemOneJudge._jev_mode, "off")
        self.mock_http()
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])

    async def test_config_values_are_clamped(self):
        self.configure(
            systemone_timeout="not-a-number",
            jev_join_threshold=5.0,
            jev_max_joins_per_hour=-10,
            jev_state_max_messages=999,
            jev_message_max_chars=1,
            jev_min_join_interval_sec=None,
        )
        self.assertEqual(SystemOneJudge._timeout, 4.0)
        self.assertEqual(SystemOneJudge._join_threshold, 1.0)
        self.assertEqual(SystemOneJudge._max_joins_per_hour, 0)
        self.assertEqual(SystemOneJudge._state_max_messages, 15)
        self.assertEqual(SystemOneJudge._message_max_chars, 10)
        self.assertEqual(SystemOneJudge._min_join_interval_sec, 120)


# ========== 2. 响应解析与阈值 ==========


class TestResponseParsing(JudgeTestCase):
    async def test_join_pass_parses_all_fields(self):
        self.configure()
        self.mock_http()
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNotNone(result)
        self.assertTrue(result.decided)
        self.assertTrue(result.join)
        self.assertAlmostEqual(result.join_probability, 0.96)
        self.assertAlmostEqual(result.relevance, 2.12)
        self.assertEqual(result.mode, "question")
        self.assertEqual(result.source, "systemone")
        self.assertAlmostEqual(result.latency_ms, 64.1)
        self.assertEqual(len(self.calls), 1)

    async def test_below_threshold_is_decided_but_rejected(self):
        self.configure()
        self.mock_http(build_response(join=0.41))
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNotNone(result)
        self.assertTrue(result.decided)
        self.assertFalse(result.join)
        self.assertEqual(result.source, "systemone")

    async def test_threshold_boundary_is_inclusive(self):
        self.configure(jev_join_threshold=0.6)
        self.mock_http(build_response(join=0.6))
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertTrue(result.join)

    async def test_probabilities_are_clamped(self):
        self.configure()
        self.mock_http({"answers": {"join": {"type": "noul", "noul": 1.5}}})
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertEqual(result.join_probability, 1.0)

        SystemOneJudge.reset_runtime_state()
        self.mock_http({"answers": {"join": {"type": "noul", "noul": -3}}})
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertEqual(result.join_probability, 0.0)

    async def test_join_as_plain_number_is_tolerated(self):
        self.configure()
        self.mock_http({"answers": {"join": 0.88}})
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertTrue(result.join)

    async def test_join_as_bool_is_tolerated(self):
        self.configure()
        self.mock_http({"answers": {"join": True}})
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertTrue(result.join)
        self.assertEqual(result.join_probability, 1.0)

    async def test_missing_answers_degrades(self):
        self.configure()
        self.mock_http({"model": "decision-model-preview"})
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_missing_join_degrades(self):
        self.configure()
        self.mock_http({"answers": {"mode": {"type": "choice", "choice": "share"}}})
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_mode_synonyms_and_unknown(self):
        self.configure()
        for raw, expected in (
            ("share", "share"),
            ("React", "react"),
            ("提问", "question"),
            ("totally-new-label", "other"),
        ):
            SystemOneJudge.reset_runtime_state()
            self.mock_http(build_response(mode=raw, join=0.9))
            result = await SystemOneJudge.judge_join("10001", self.messages())
            self.assertEqual(result.mode, expected, raw)

    async def test_missing_relevance_and_mode_are_none(self):
        self.configure()
        self.mock_http(build_response(mode=None, relevance=None))
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertTrue(result.join)
        self.assertIsNone(result.mode)
        self.assertIsNone(result.relevance)

    async def test_error_body_degrades(self):
        self.configure()
        self.mock_http({"error": {"code": "invalid_parameter_error", "message": "x"}})
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_invalid_json_degrades(self):
        self.configure()
        self.mock_http(raw_text="{not-json")
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_partial_dict_without_join_degrades(self):
        self.configure()
        self.mock_http({"answers": {"join": {"type": "noul"}, "mode": {}}})
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)


# ========== 3. 失败 / 超时降级 ==========


class TestFailureDegradation(JudgeTestCase):
    async def test_http_500_degrades(self):
        self.configure()
        self.mock_http(status=500, raw_text="internal error")
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_http_401_degrades(self):
        self.configure()
        self.mock_http(status=401, raw_text='{"error":{"code":"invalid_api_key"}}')
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_timeout_degrades(self):
        self.configure()
        self.mock_http_error(asyncio.TimeoutError())
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_network_exception_degrades(self):
        self.configure()
        self.mock_http_error(OSError("connection reset"))
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_unexpected_exception_degrade_instead_of_raise(self):
        self.configure()
        self.mock_http_error(RuntimeError("boom"))
        # 不允许把异常抛给调用方（否则主流程可能哑掉）
        result = await SystemOneJudge.judge_join("10001", self.messages())
        self.assertIsNone(result)

    async def test_proactive_failure_degrades(self):
        self.configure()
        self.mock_http_error(asyncio.TimeoutError())
        result = await SystemOneJudge.judge_proactive("10001", self.messages())
        self.assertIsNone(result)

    async def test_request_payload_and_headers(self):
        self.configure(systemone_timeout=3.5)
        self.mock_http()
        await SystemOneJudge.judge_join("10001", self.messages())
        call = self.calls[0]
        self.assertEqual(call["url"], "https://example.invalid/systemone")
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-sp-unit-test-key")
        self.assertEqual(call["timeout"], 3.5)
        self.assertEqual(call["payload"]["model"], "decision-model-preview")


# ========== 4. 限频 / 冷却保护 ==========


class TestRateLimits(JudgeTestCase):
    async def test_hourly_cap_blocks_and_does_not_call_api(self):
        self.configure(jev_max_joins_per_hour=2, jev_min_join_interval_sec=0)
        self.mock_http()
        first = await SystemOneJudge.judge_join("20001", self.messages())
        second = await SystemOneJudge.judge_join("20001", self.messages())
        third = await SystemOneJudge.judge_join("20001", self.messages())
        self.assertTrue(first.join)
        self.assertTrue(second.join)
        self.assertFalse(third.join)
        self.assertEqual(third.source, "guard")
        self.assertIn("上限", third.reason)
        self.assertEqual(len(self.calls), 2, "被保护拦截时不应调用决策服务")

    async def test_hourly_cap_window_expires(self):
        self.configure(jev_max_joins_per_hour=1, jev_min_join_interval_sec=0)
        self.mock_http()
        await SystemOneJudge.judge_join("20002", self.messages())
        blocked = await SystemOneJudge.judge_join("20002", self.messages())
        self.assertFalse(blocked.join)
        self.now += 3601  # 1 小时窗口滑出
        allowed = await SystemOneJudge.judge_join("20002", self.messages())
        self.assertTrue(allowed.join)

    async def test_min_interval_blocks_second_join(self):
        self.configure(jev_max_joins_per_hour=10, jev_min_join_interval_sec=120)
        self.mock_http()
        first = await SystemOneJudge.judge_join("20003", self.messages())
        second = await SystemOneJudge.judge_join("20003", self.messages())
        self.assertTrue(first.join)
        self.assertFalse(second.join)
        self.assertEqual(second.source, "guard")
        self.assertIn("不足120秒", second.reason)
        self.now += 121
        third = await SystemOneJudge.judge_join("20003", self.messages())
        self.assertTrue(third.join)

    async def test_min_interval_zero_disables_guard(self):
        self.configure(
            jev_min_join_interval_sec=0,
            jev_max_joins_per_hour=10,
            jev_bot_cooldown_sec=0,
        )
        self.mock_http()
        first = await SystemOneJudge.judge_join("20004", self.messages())
        second = await SystemOneJudge.judge_join("20004", self.messages())
        self.assertTrue(first.join)
        self.assertTrue(second.join)

    async def test_bot_reply_cooldown(self):
        self.configure(jev_min_join_interval_sec=0, jev_bot_cooldown_sec=90)
        self.mock_http()
        SystemOneJudge.record_bot_reply("20005")
        blocked = await SystemOneJudge.judge_join("20005", self.messages())
        self.assertFalse(blocked.join)
        self.assertIn("冷却", blocked.reason)
        self.assertEqual(self.calls, [])
        self.now += 91
        allowed = await SystemOneJudge.judge_join("20005", self.messages())
        self.assertTrue(allowed.join)

    async def test_rejected_decision_does_not_consume_quota(self):
        self.configure(jev_max_joins_per_hour=1, jev_min_join_interval_sec=0)
        self.mock_http(build_response(join=0.1))
        rejected = await SystemOneJudge.judge_join("20006", self.messages())
        self.assertFalse(rejected.join)
        self.mock_http(build_response(join=0.9))
        accepted = await SystemOneJudge.judge_join("20006", self.messages())
        self.assertTrue(accepted.join, "未通过阈值的决策不应占用每小时额度")

    async def test_hourly_cap_zero_means_unlimited(self):
        self.configure(
            jev_max_joins_per_hour=0,
            jev_min_join_interval_sec=0,
            jev_bot_cooldown_sec=0,
        )
        self.mock_http()
        for _ in range(12):
            result = await SystemOneJudge.judge_join("20007", self.messages())
            self.assertTrue(result.join)

    async def test_join_and_proactive_quotas_are_independent(self):
        self.configure(
            jev_max_joins_per_hour=1,
            jev_min_join_interval_sec=0,
            jev_bot_cooldown_sec=0,
        )
        self.mock_http()
        join_result = await SystemOneJudge.judge_join("20008", self.messages())
        proactive_result = await SystemOneJudge.judge_proactive(
            "20008", self.messages()
        )
        self.assertTrue(join_result.join)
        self.assertTrue(proactive_result.join)
        self.assertEqual(
            len(self.calls),
            2,
            "普通插话与主动开场的额度应分别统计，互不抢占",
        )

    async def test_groups_are_isolated(self):
        self.configure(jev_max_joins_per_hour=1, jev_min_join_interval_sec=0)
        self.mock_http()
        first = await SystemOneJudge.judge_join("30001", self.messages())
        other_group = await SystemOneJudge.judge_join("30002", self.messages())
        self.assertTrue(first.join)
        self.assertTrue(other_group.join)


# ========== 5. 消息长度保护 ==========


class TestMessageGuards(JudgeTestCase):
    async def test_short_current_message_skips_api(self):
        self.configure(jev_min_message_length=6)
        self.mock_http()
        result = await SystemOneJudge.judge_join(
            "40001", [], current_message={"sender_name": "A", "content": "嗯"}
        )
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])

    async def test_short_last_cached_message_skips_api(self):
        self.configure(jev_min_message_length=6)
        self.mock_http()
        messages = [{"sender_name": "A", "content": "哈哈哈"}]
        result = await SystemOneJudge.judge_join("40002", messages)
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])

    async def test_long_current_message_calls_api(self):
        self.configure(jev_min_message_length=6)
        self.mock_http()
        result = await SystemOneJudge.judge_join(
            "40003",
            [],
            current_message={"sender_name": "A", "content": "有没有人今晚一起听演唱会"},
        )
        self.assertTrue(result.join)
        self.assertEqual(len(self.calls), 1)

    async def test_min_length_zero_disables_guard(self):
        self.configure(jev_min_message_length=0)
        self.mock_http()
        result = await SystemOneJudge.judge_join(
            "40004", [], current_message={"sender_name": "A", "content": "嗯"}
        )
        self.assertTrue(result.join)

    async def test_no_message_at_all_still_decides(self):
        self.configure()
        self.mock_http()
        result = await SystemOneJudge.judge_join("40005", [])
        self.assertTrue(result.join)


# ========== 6. 每群覆盖配置 ==========


class TestPerGroupConfig(JudgeTestCase):
    async def test_default_path_resolution(self):
        self.configure(
            jev_per_group_config_path=(
                "plugin_data/astrbot_plugin_group_chat_plus/systemone_group_config.json"
            )
        )
        SystemOneJudge._plugin_data_dir = (
            "/AstrBot/data/plugin_data/astrbot_plugin_group_chat_plus"
        )
        self.assertEqual(
            str(SystemOneJudge.get_group_config_path()),
            "/AstrBot/data/plugin_data/astrbot_plugin_group_chat_plus/systemone_group_config.json",
        )

    def test_absolute_path_is_used_as_is(self):
        self.configure()
        self.assertTrue(
            str(SystemOneJudge.get_group_config_path()).startswith(self.tmpdir.name)
        )

    def test_bom_file_is_parsed(self):
        self.configure()
        path = self.write_group_config({"123456": {"join_threshold": 0.9}})
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "测试文件必须带 UTF-8 BOM")
        SystemOneJudge.load_group_overrides(force=True)
        self.assertAlmostEqual(
            SystemOneJudge.get_group_threshold("123456"), 0.9, places=6
        )

    async def test_group_threshold_override_auto_reload(self):
        self.configure(jev_join_threshold=0.6)
        self.write_group_config(
            {"123456": {"join_threshold": 0.95, "max_joins_per_hour": 50}}
        )
        # 不调用 force：走 30 秒节流后的自动重读路径
        self.now += 31
        self.mock_http(build_response(join=0.7))

        strict = await SystemOneJudge.judge_join("123456", self.messages())
        self.assertFalse(strict.join, "该群阈值 0.95，0.7 不应通过")

        self.mock_http(build_response(join=0.7))
        loose = await SystemOneJudge.judge_join("654321", self.messages())
        self.assertTrue(loose.join, "未列出的群应使用全局阈值 0.6")

    async def test_group_jev_mode_override(self):
        self.configure(jev_mode="off")
        self.write_group_config({"123456": {"jev_mode": "replace"}})
        SystemOneJudge.load_group_overrides(force=True)
        self.mock_http()
        enabled = await SystemOneJudge.judge_join("123456", self.messages())
        self.assertIsNotNone(enabled)
        other = await SystemOneJudge.judge_join("999999", self.messages())
        self.assertIsNone(other, "全局 jev_mode=off 时未列出的群不参与决策")

    async def test_group_enable_false_disables_group(self):
        self.configure(jev_mode="replace")
        self.write_group_config({"123456": {"enable": False}})
        SystemOneJudge.load_group_overrides(force=True)
        self.mock_http()
        result = await SystemOneJudge.judge_join("123456", self.messages())
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])
        self.assertEqual(SystemOneJudge.get_group_jev_mode("123456"), "off")

    async def test_group_limits_override(self):
        self.configure(jev_max_joins_per_hour=8, jev_min_join_interval_sec=0)
        self.write_group_config({"123456": {"max_joins_per_hour": 1}})
        SystemOneJudge.load_group_overrides(force=True)
        self.mock_http()
        first = await SystemOneJudge.judge_join("123456", self.messages())
        second = await SystemOneJudge.judge_join("123456", self.messages())
        self.assertTrue(first.join)
        self.assertFalse(second.join)
        self.assertIn("上限", second.reason)

    async def test_broken_file_falls_back_to_global(self):
        self.configure()
        self.write_group_config("{ this is not json ")
        SystemOneJudge.load_group_overrides(force=True)
        self.assertEqual(SystemOneJudge._group_overrides, {})
        self.mock_http()
        result = await SystemOneJudge.judge_join("123456", self.messages())
        self.assertTrue(result.join, "损坏的覆盖文件不应影响全局默认决策")

    async def test_missing_file_is_not_an_error(self):
        self.configure()
        SystemOneJudge.load_group_overrides(force=True)
        self.assertEqual(SystemOneJudge._group_overrides, {})

    async def test_ensure_group_config_file_writes_bom(self):
        self.configure()
        path = SystemOneJudge.ensure_group_config_file(force=True)
        self.assertIsNotNone(path)
        raw = Path(path).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(json.loads(raw.decode("utf-8-sig")), {})

    async def test_group_config_reload_is_throttled(self):
        self.configure()
        self.write_group_config({"123456": {"join_threshold": 0.9}})
        self.assertIsNone(SystemOneJudge._group_overrides.get("123456"))
        self.now += 31  # 越过 configure 时的首次检查时间，允许自动重读
        SystemOneJudge.load_group_overrides()  # 首次读取
        self.assertIn("123456", SystemOneJudge._group_overrides)
        # 30 秒内即使文件变化也不重读
        self.write_group_config({"123456": {"join_threshold": 0.1}})
        SystemOneJudge.load_group_overrides()
        self.assertAlmostEqual(
            SystemOneJudge.get_group_threshold("123456"), 0.9, places=6
        )
        self.now += 31
        SystemOneJudge.load_group_overrides()
        self.assertAlmostEqual(
            SystemOneJudge.get_group_threshold("123456"), 0.1, places=6
        )


# ========== 7. state / questions / 提示 ==========


class TestStateAndQuestions(JudgeTestCase):
    async def test_state_payload_structure(self):
        self.configure(jev_state_max_messages=10, jev_message_max_chars=20)
        self.mock_http()
        messages = [
            {"sender_name": f"用户{i}", "content": "长" * 100, "timestamp": 1_700_000_000}
            for i in range(30)
        ]
        await SystemOneJudge.judge_join("50001", messages)
        state = self.calls[0]["payload"]["state"]
        self.assertEqual(state["group_id"], "50001")
        self.assertIn("persona", state)
        self.assertLessEqual(len(state["recent_messages"]), 10)
        for line in state["recent_messages"]:
            content = line.split("：")[-1]
            self.assertLessEqual(len(content), 20)
            self.assertNotIn("长" * 21, line)

    async def test_state_includes_current_message(self):
        self.configure()
        self.mock_http()
        await SystemOneJudge.judge_join(
            "50002",
            [{"sender_name": "A", "content": "上一句闲聊内容"}],
            current_message={"sender_name": "B", "content": "这条是当前消息内容"},
        )
        lines = self.calls[0]["payload"]["state"]["recent_messages"]
        self.assertEqual(len(lines), 2)
        self.assertIn("这条是当前消息内容", lines[-1])

    async def test_state_caps_message_count_to_15(self):
        self.configure(jev_state_max_messages=15)
        self.mock_http()
        await SystemOneJudge.judge_join("50003", self.messages(count=40))
        lines = self.calls[0]["payload"]["state"]["recent_messages"]
        self.assertLessEqual(len(lines), 15)

    async def test_persona_summary_override(self):
        self.configure(jev_persona_summary="测试用一句话人设")
        self.mock_http()
        await SystemOneJudge.judge_join("50004", self.messages())
        self.assertEqual(
            self.calls[0]["payload"]["state"]["persona"], "测试用一句话人设"
        )

    async def test_questions_join_only_when_extras_disabled(self):
        self.configure(
            jev_enable_topic_relevance=False, jev_enable_mode_choice=False
        )
        self.mock_http()
        await SystemOneJudge.judge_join("50005", self.messages())
        questions = self.calls[0]["payload"]["questions"]
        self.assertEqual(list(questions.keys()), ["join"])
        self.assertEqual(questions["join"]["type"], "noul")
        self.assertIn("criteria", questions["join"])

    async def test_questions_full_set(self):
        self.configure()
        self.mock_http()
        await SystemOneJudge.judge_join("50006", self.messages())
        questions = self.calls[0]["payload"]["questions"]
        self.assertEqual(set(questions.keys()), {"join", "topic_relevance", "mode"})
        self.assertEqual(questions["join"]["type"], "noul")
        self.assertEqual(questions["topic_relevance"]["type"], "score")
        self.assertEqual(len(questions["topic_relevance"]["criteria"]), 4)
        self.assertEqual(questions["mode"]["type"], "choice")
        self.assertIn("other", questions["mode"]["criteria"])

    async def test_proactive_questions(self):
        self.configure()
        self.mock_http()
        await SystemOneJudge.judge_proactive("50007", self.messages())
        questions = self.calls[0]["payload"]["questions"]
        self.assertIn("join", questions)
        self.assertIn("开场", questions["join"]["instructions"])
        self.assertEqual(questions["mode"]["type"], "choice")

    async def test_proactive_judge_can_be_disabled(self):
        self.configure(jev_proactive_judge=False)
        self.mock_http()
        result = await SystemOneJudge.judge_proactive("50008", self.messages())
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])

    def test_build_mode_hint(self):
        for mode in ("question", "share", "react"):
            hint = SystemOneJudge.build_mode_hint(mode)
            self.assertTrue(hint)
            self.assertIn("发言方式建议", hint)
        for mode in ("other", None, "", "unknown"):
            self.assertEqual(SystemOneJudge.build_mode_hint(mode), "")

    def test_normalize_message_variants(self):
        self.assertEqual(
            systemone_judge.normalize_message("  你好  ", 60), "你好"
        )
        self.assertEqual(
            systemone_judge.normalize_message(
                {"sender_name": "小明", "content": "吃饭了吗", "timestamp": 1_700_000_000},
                60,
            ).split("：")[-1],
            "吃饭了吗",
        )
        # 毫秒时间戳不应报错
        line = systemone_judge.normalize_message(
            {"sender_name": "小明", "content": "hi", "timestamp": 1_700_000_000_000},
            60,
        )
        self.assertIn("小明", line)
        # 无 sender_name 时用 sender_id 兜底
        self.assertIn(
            "用户12345",
            systemone_judge.normalize_message(
                {"sender_id": "12345", "content": "在吗"}, 60
            ),
        )
        self.assertEqual(systemone_judge.normalize_message(None, 60), "")
        self.assertEqual(systemone_judge.normalize_message({}, 60), "")
        self.assertEqual(
            systemone_judge.normalize_message({"content": "   \n  "}, 60), ""
        )

    def test_extract_helpers_edge_cases(self):
        self.assertIsNone(systemone_judge.extract_probability(None))
        self.assertIsNone(systemone_judge.extract_probability({}))
        self.assertIsNone(systemone_judge.extract_probability("abc"))
        self.assertEqual(systemone_judge.extract_probability(0.5), 0.5)
        self.assertIsNone(systemone_judge.extract_score(None))
        self.assertIsNone(systemone_judge.extract_score({"no_score": 1}))
        self.assertEqual(systemone_judge.extract_score({"score": 2.12}), 2.12)
        self.assertIsNone(systemone_judge.extract_choice(123))
        self.assertEqual(systemone_judge.extract_choice({"choice": "SHARE"}), "share")
        self.assertIsNone(systemone_judge.extract_confidence({"confidence": "x"}))
        self.assertEqual(systemone_judge.extract_confidence({"confidence": 0.5}), 0.5)
        self.assertEqual(systemone_judge.parse_latency({"latency_ms": 12.5}, 99.0), 12.5)
        self.assertEqual(systemone_judge.parse_latency({}, 99.0), 99.0)

    def test_normalize_messages_filters_and_limits(self):
        lines = systemone_judge.normalize_messages(
            [
                {"sender_name": "A", "content": "一"},
                {"sender_name": "B", "content": ""},
                "纯字符串消息",
            ],
            limit=2,
            max_chars=60,
        )
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("纯字符串消息" in line for line in lines))


# ========== 8. 机器人发言记录 ==========


class TestBotReplyRecording(JudgeTestCase):
    async def test_record_bot_reply_is_noop_when_disabled(self):
        self.configure(enable_systemone_decision=False, jev_mode="off")
        SystemOneJudge.record_bot_reply("60001")
        self.assertEqual(SystemOneJudge._group_state, {})

    async def test_note_join_updates_bucket_only(self):
        self.configure()
        SystemOneJudge.note_join("60002", "join")
        state = SystemOneJudge._group_state["60002"]
        self.assertEqual(len(state["join"]), 1)
        self.assertEqual(
            state["last_speak"], 0.0, "note_join 只记决策额度，不冒充真实发言时间"
        )

    async def test_record_bot_reply_updates_last_speak(self):
        self.configure()
        SystemOneJudge.record_bot_reply("60003")
        state = SystemOneJudge._group_state["60003"]
        self.assertEqual(state["last_speak"], self.now)
        self.assertEqual(state["join"], [])

    async def test_collect_recent_messages_from_plugin_cache(self):
        class FakePlugin:
            pending_messages_cache = {
                "70001": [
                    {"sender_name": "A", "content": f"消息{index}"} for index in range(20)
                ],
                "70002": "not-a-list",
            }

        collected = SystemOneJudge.collect_recent_messages(FakePlugin(), "70001")
        self.assertEqual(len(collected), SystemOneJudge._state_max_messages)
        self.assertEqual(collected[-1]["content"], "消息19")
        self.assertEqual(SystemOneJudge.collect_recent_messages(FakePlugin(), "70002"), [])
        self.assertEqual(SystemOneJudge.collect_recent_messages(None, "70001"), [])
        self.assertEqual(
            SystemOneJudge.collect_recent_messages(FakePlugin(), "70001", limit=3)[-1][
                "content"
            ],
            "消息19",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
