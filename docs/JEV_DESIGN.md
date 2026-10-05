# Chat_PLUS System One（Jev）决策接入设计文档

> 所属：Chat_PLUS Jev fork 集成任务
> 开发副本：`_local_deploy/plugins/astrbot_plugin_group_chat_plus/`
> 协议：阿里云百炼 TokenPlan System One（TypeSafe 协议）｜设计文档即本文件
> 本文所有行号均取自实际代码（2026-10-04 定稿版本），非推测。

---

## 1. 概述

在 Chat_PLUS 的「普通消息插话」与「沉默主动开话题」两条链路上接入阿里云百炼
`decision-model-preview`（TypeSafe System One 协议，下称 System One），用极速决策模型
返回的 `join` 概率**替代（replace）或复核（hybrid）**原有的随机概率门，并把
`mode`（提问/分享/附和吐槽）结果作为一句克制的发言方式建议注入回复生成。

默认 `enable_systemone_decision=false` + `jev_mode=off` + API Key 留空，
三者任一未满足即完全跳过决策链，插件行为与改造前**完全一致**；任何失败/超时一律
返回「未决策」并降级回旧概率逻辑，不会让机器人哑掉、刷屏或抛异常。

---

## 2. 接入点清单（实际改动的文件与行号）

### 2.1 新增文件

| 文件 | 规模 | 作用 |
|---|---|---|
| `_local_deploy/plugins/astrbot_plugin_group_chat_plus/utils/systemone_judge.py` | 1125 行（新建） | System One 决策客户端：HTTP 调用、响应解析、限频保护、每群覆盖配置、降级、日志 |
| `_local_deploy/plugins/astrbot_plugin_group_chat_plus/tests/test_systemone_judge.py` | 868 行（新建） | 不依赖 AstrBot 的单元测试（67 用例） |
| `docs/JEV_DESIGN.md` | 本文档 | 设计文档 |

### 2.2 `main.py`（14509 → 14703 行，纯新增为主）

| 行号 | 位置 | 作用 |
|---|---|---|
| 272 | `from .utils import (...)` | 导入 `SystemOneJudge` |
| 1136–1163 | `__init__` 配置提取区 | 新增 20 个实例属性（`self.enable_systemone_decision` … `self.jev_per_group_config_path`），仅读取配置，不做任何网络请求 |
| 2906–2912 | `__init__` 初始化区（主动对话初始化之后） | `SystemOneJudge.configure(self._build_systemone_config(), str(data_dir))`；启用且填了 Key 时调用 `ensure_group_config_file()` 生成带 BOM 的每群覆盖空模板 |
| 3467–3490 | 新增方法 `_build_systemone_config()` | 把 20 个配置项汇总成一个 dict 交给 `SystemOneJudge`（放在 `_build_cooldown_config` 之后，风格一致） |
| 8272–8274 | `_generate_and_send_reply` 发送成功后 | `SystemOneJudge.record_bot_reply(str(chat_id))`（仅在 `is_group_active` 且非重复拦截时），用于「机器人刚说过话」冷却 |
| 11153–11159 | `on_llm_request` 情绪提示之后 | 读取 `event.get_extra("_group_chat_plus_jev_hint")` 并追加到 `req.system_prompt` 末尾（与现有 mood_hint 同机制，**不触碰用户手写的 extra prompt**） |
| 11202 | `on_llm_request` 收尾清理块 | 追加清理 `_group_chat_plus_jev_hint`，防止 event 复用污染 |
| 14571–14581 | `_check_probability()` 末尾、原「随机判断」之前 | 插话决策接入点：`await self._systemone_probability_gate(...)`，返回值非 None 直接 return，否则继续走原 `random.random()` 逻辑 |
| 14599–14668 | 新增方法 `_systemone_probability_gate()` | 决策门本体：off 判断 → hybrid 先跑概率门 → 收集最近消息 → `judge_join` → 结果映射/降级/注入 mode 提示 |
| 14670–14687 | 新增方法 `_build_systemone_current_message()` | 把当前事件转成 `{sender_name, sender_id, content, timestamp}` |
| 14689–14703 | 新增方法 `_inject_systemone_mode_hint()` | 把 `mode` 转成提示写入 `event.set_extra("_group_chat_plus_jev_hint", hint)` |

> 未改动任何既有函数签名、未重构既有逻辑；`_check_probability` 只在末尾插入了 11 行。

### 2.3 `utils/proactive_chat_manager.py`（5765 → 5835 行）

| 行号 | 位置 | 作用 |
|---|---|---|
| 40 | 模块导入区 | `from .systemone_judge import SystemOneJudge` |
| 996–1001 | `record_proactive_reply()` 开头 | 主动发言真正发出后调用 `record_bot_reply()`，更新 System One 冷却（未介入的群零开销直接跳过） |
| 2432–2478 | 新增类方法 `apply_systemone_proactive_judge()` | 沉默开话题复核：把 `chat_key` 解析成 `chat_id`，走 `judge_proactive`，返回 `(是否继续触发, 原因)`；异常/未决策一律放行 |
| 3052–3063 | `start_background_task()` 后台循环（`should_trigger_proactive_chat` 之后） | 仅在随机概率已通过（`should_trigger=True`）时调用上面的复核；返回 False 则本轮不触发，也**不会**命中「未通过概率筛选」重置计时器的分支 |

> `should_trigger_proactive_chat()` 的函数签名与内部逻辑**未改**（仍是同步 classmethod），
> 复核逻辑放在其调用点，避免改动既有签名。

### 2.4 `utils/__init__.py`

| 行号 | 作用 |
|---|---|
| 72 | `from .systemone_judge import SystemOneJudge`（与现有模块导出风格一致） |
| 138 | `__all__` 增加 `"SystemOneJudge"` |

### 2.5 `_conf_schema.json`（2214 → 2345 行）

| 行号 | 作用 |
|---|---|
| 2214–2344 | 新增「System One 决策」配置区，共 21 个键（1 个分区标题 + 20 个配置项），中文 description/hint，与现网 schema 风格一致（4 空格缩进；`jev_mode` 使用 `options` 下拉，插件自带 Web 面板 `web/static/js/config-editor.js:260` 支持该字段） |
| — | 该文件原本无 BOM、LF 行尾，改动后保持不变（校验：首字节 `{`） |

### 2.6 运行态职责划分

- `SystemOneJudge` 使用**类变量单例**（与 `ProactiveChatManager` / `ProbabilityManager` 同风格），
  由 `main.py` 在插件 `__init__` 时注入配置；运行期不做热重载配置（改配置需重载插件），
  但**每群覆盖 JSON 会按 mtime + 30 秒节流自动重读**。
- 关键方法（`utils/systemone_judge.py` 行号）：`configure`(211)、`is_active`(312)、
  `is_group_active`(323)、`get_group_config_path`(330)、`load_group_overrides`(352)、
  `get_group_jev_mode`(410)、`ensure_group_config_file`(454)、`_check_guard`(493)、
  `note_join`(522)、`record_bot_reply`(537)、`judge_join`(561)、`judge_proactive`(583)、
  `_judge`(604)、`_post_json`(695)、`_parse_response`(762)、`build_mode_hint`(922)、
  `collect_recent_messages`(932)。

---

## 3. 数据流（文字版）

### 3.1 普通消息插话（main.py `_check_probability` → `_generate_and_send_reply`）

```
群消息到达
  └─ 拟人阈值 / 冷却 / 待决策上下文等既有前置过滤（未改动）
      └─ _check_probability()：既有概率修正（注意力/拟人/疲劳/表情包/密度/质量/@全员临时提升/硬边界）
          └─ 【14571】_systemone_probability_gate()          ← 新增
              ├─ is_private 或 !is_active() → None（直接走旧逻辑）
              ├─ get_group_jev_mode(群号) == "off" → None（直接走旧逻辑）
              ├─ replace：直接调 judge_join
              └─ hybrid：先 random.random() < 概率门，未过 → False（不调服务）
                   └─ SystemOneJudge.judge_join()
                       ├─ 保护：消息长度 < jev_min_message_length → None（未决策）
                       ├─ 保护：每小时上限 / 最小间隔 / 机器人刚说话冷却 → 判定不插话（guard）
                       ├─ 组装 state：一句话人设 + 最近 8~15 条群消息（每条截 60 字，含发送者与时间）
                       ├─ POST 决策端点（Bearer Key，超时 4s）
                       ├─ 解析 join(noul) / topic_relevance(score) / mode(choice)
                       └─ join_probability >= 阈值 ? 通过并 note_join : 不插话
              ├─ 通过 + mode 有结果 → _inject_systemone_mode_hint() → event.extra
              └─ 返回 True / False / None
          ├─ None → 落回原 `roll = random.random()` 随机概率门（旧行为）
          └─ True → 继续后续读空气 / 等待窗口 / 回复生成
              └─ _generate_and_send_reply() → ReplyHandler.generate_reply()
                  └─ on_llm_request()：mood_hint 之后追加 jev_hint 到 system_prompt 末尾
                      （[系统信息-发言方式建议: …]，不修改用户手写 extra prompt）
                  └─ 发送成功后【8272】SystemOneJudge.record_bot_reply(chat_id)
```

### 3.2 沉默主动开话题（utils/proactive_chat_manager.py）

```
后台任务（每 proactive_check_interval 秒）
  └─ 群沉默 > 阈值 且 随机 proactive_probability 通过（should_trigger_proactive_chat，未改动）
      └─ 【3052】apply_systemone_proactive_judge(chat_key, plugin_instance)   ← 新增
          ├─ !is_active() → 放行（True, "未启用 System One 决策"）
          ├─ 私聊 → 放行
          ├─ get_group_jev_mode(群号) == "off" → 放行
          ├─ 收集最近消息 → SystemOneJudge.judge_proactive()
          │    ├─ 未决策（失败/超时/保护以外的跳过）→ 放行（沿用原概率结果）
          │    ├─ join=True → 放行，并记入 proactive 额度
          │    └─ join=False（含限频保护拦截）→ 拦截本次开场
          └─ 异常 → 放行（绝不打断后台任务）
      └─ should_trigger 仍为 True → trigger_proactive_chat()（既有流程）
          └─ 消息发送成功 → record_proactive_reply()【996】→ record_bot_reply()
```

---

## 4. 配置清单（共 21 键，抄自 `_conf_schema.json` 2214–2344 行）

| # | 键名 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| 0 | `_systemone_section_header` | string | `--- System One 决策区 ---` | 分区标题（仅用于 Web 面板分组显示） |
| 1 | `enable_systemone_decision` | bool | `false` | 总开关；关闭后完全跳过决策链 |
| 2 | `systemone_api_key` | string | `""` | 阿里云百炼 API Key；留空时模块自我禁用并打日志提示 |
| 3 | `systemone_base_url` | string | `""`（空值由代码回退为默认端点） | 决策端点，默认 `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone` |
| 4 | `systemone_model` | string | `""`（空值回退 `decision-model-preview`） | 决策模型名 |
| 5 | `systemone_timeout` | float | `4.0` | 请求超时（秒），代码夹紧到 0.5~30 |
| 6 | `jev_mode` | string | `"off"`（options: off/replace/hybrid） | 介入模式：off 不介入；replace 替代概率门；hybrid 概率门通过后复核 |
| 7 | `jev_join_threshold` | float | `0.6` | join 概率阈值，`P ≥ 阈值` 判定插话（代码夹紧 0~1） |
| 8 | `jev_max_joins_per_hour` | int | `8` | 每群每小时主动插话上限，0=不限（代码夹紧 0~1000） |
| 9 | `jev_min_join_interval_sec` | int | `120` | 两次插话最小间隔（秒），0=不限（代码夹紧 0~86400） |
| 10 | `jev_bot_cooldown_sec` | int | `90` | 机器人刚发言后的冷却（秒），0=不限（代码夹紧 0~86400） |
| 11 | `jev_min_message_length` | int | `6` | 消息短于该字数则跳过本次决策，0=不跳过（代码夹紧 0~200） |
| 12 | `jev_state_max_messages` | int | `12` | state 中携带的最近群消息条数（代码夹紧 8~15） |
| 13 | `jev_message_max_chars` | int | `60` | 单条消息截断字数（代码夹紧 10~500） |
| 14 | `jev_persona_summary` | text | `""`（空值用代码内置默认摘要） | 送给模型的一句话人设摘要 |
| 15 | `jev_enable_topic_relevance` | bool | `true` | 是否附带 `topic_relevance`(score 4 级) 问题（仅用于日志观察） |
| 16 | `jev_enable_mode_choice` | bool | `true` | 是否附带 `mode`(choice) 问题 |
| 17 | `jev_inject_mode_hint` | bool | `true` | 是否把 mode 结果作为提示注入回复生成 |
| 18 | `jev_proactive_judge` | bool | `true` | 沉默开话题链路是否走 System One 复核 |
| 19 | `jev_log_enabled` | bool | `true` | 是否输出每次决策日志（含 join 概率/阈值/相关度/方式/延迟） |
| 20 | `jev_per_group_config_path` | string | `plugin_data/astrbot_plugin_group_chat_plus/systemone_group_config.json` | 每群覆盖 JSON 路径（绝对路径或相对 AstrBot 数据目录） |

> 代码内置默认值（`utils/systemone_judge.py` 顶部常量）：端点 `DEFAULT_BASE_URL`、
> 模型 `DEFAULT_MODEL=decision-model-preview`、人设摘要 `DEFAULT_PERSONA_SUMMARY`、
> 每群配置相对路径 `DEFAULT_GROUP_CONFIG_REL`；`jev_mode` 非法值一律归一化为 `off`。
> 配置注入后不写回插件配置，Key 仅存在于插件配置 JSON（不进代码、不进 git）。

---

## 5. 每群独立配置

### 5.1 路径解析

`SystemOneJudge.get_group_config_path()`（utils/systemone_judge.py:330）：

1. 配置值为绝对路径 → 直接使用；
2. 相对路径且首段是 `plugin_data` → 以 AstrBot 数据根目录为基准
   （`StarTools.get_data_dir()` 通常返回 `<data>/plugin_data/astrbot_plugin_group_chat_plus`，
   取其 `parent.parent` 作为 `<data>`）；
3. 其它相对路径 → 以插件数据目录为基准；
4. 未注入 `plugin_data_dir` 时回退到进程当前目录。

默认解析结果（容器内）：`/AstrBot/data/plugin_data/astrbot_plugin_group_chat_plus/systemone_group_config.json`。

### 5.2 文件格式（UTF-8 **BOM**）

```json
{
    "879646332": {
        "join_threshold": 0.75,
        "max_joins_per_hour": 4,
        "min_join_interval_sec": 300,
        "jev_mode": "hybrid",
        "enable": true
    },
    "123456789": {
        "enable": false
    }
}
```

| 键 | 类型 | 含义 |
|---|---|---|
| `join_threshold` | float | 该群插话阈值，覆盖全局 `jev_join_threshold` |
| `max_joins_per_hour` | int | 该群每小时上限，覆盖全局 |
| `min_join_interval_sec` | int | 该群两次插话最小间隔，覆盖全局 |
| `jev_mode` | string | 该群介入模式（off/replace/hybrid），覆盖全局 |
| `enable` | bool | 设为 `false` 等价于该群 `jev_mode=off` |

- **未列出的群**：全部走全局默认值（`get_group_setting` 未命中即返回全局值）。
- 读写均使用 `utf-8-sig`，容忍并保留 BOM；插件启动时若「总开关开启且 Key 非空」
  会自动生成一个 `{}` 空模板（带 BOM），**已存在的文件不会被覆盖**。
- 加载机制：`load_group_overrides()`（352 行）按 **文件 mtime 变化 + 30 秒节流** 重读；
  文件不存在不算错误（保持空覆盖）；**文件损坏只记 warning 并回退全局默认，绝不抛异常**。
- 生效延迟：修改文件后约 30 秒内生效，无需重启插件。

---

## 6. 降级矩阵

### 6.1 `decided=False`（返回「未决策」）的全部情况 → 调用方降级

| 情况 | 判定位置 | replace 模式行为 | hybrid 模式行为 | 沉默开话题行为 |
|---|---|---|---|---|
| 总开关关闭 / 未配置 | `is_active()` | 旧随机概率门 | 旧随机概率门 | 放行（沿用概率结果） |
| `systemone_api_key` 为空 | `is_active()` | 旧随机概率门 | 旧随机概率门 | 放行 |
| 该群 `jev_mode=off` / `enable=false` | `get_group_jev_mode` | 旧随机概率门 | 旧随机概率门 | 放行 |
| 消息短于 `jev_min_message_length` | `_judge` 保护 1 | 旧随机概率门 | **视为概率门已通过**（概率门已过才调用） | 放行 |
| HTTP 非 200（401/500 等） | `_post_json` | 旧随机概率门 | 视为概率门已通过 | 放行 |
| 请求超时（默认 4s） | `_post_json` | 旧随机概率门 | 视为概率门已通过 | 放行 |
| 网络异常 / 缺少 aiohttp | `_post_json` | 旧随机概率门 | 视为概率门已通过 | 放行 |
| 响应非 JSON / 含 `error` / 缺 `answers` / 缺合法 `join` | `_post_json`/`_parse_response` | 旧随机概率门 | 视为概率门已通过 | 放行 |
| 决策模块内部任何未预期异常 | `_judge` 兜底 / 两个接入点的 try-except | 旧随机概率门 | 视为概率门已通过 | 放行 |
| `jev_proactive_judge=false` | `judge_proactive` | — | — | 放行 |

### 6.2 `decided=True`（明确判定）

| 情况 | 行为 |
|---|---|
| `join_probability >= 该群阈值` | 插话/开场；记入该群额度（`note_join`）；命中 mode 时注入发言方式建议 |
| `join_probability < 该群阈值` | 本次不插话/不开场；**不占用**每小时额度 |
| 触发保护（每小时上限 / 最小间隔 / 机器人刚说话冷却） | 判定不插话（`source="guard"`），**不调用决策服务**，日志打印拦截原因 |
| 保护拦截（proactive 链路） | 本轮主动开场被拦截，不触发 LLM 生成 |

### 6.3 保护开关的「关闭」语义

| 参数 | 0 的含义 |
|---|---|
| `max_joins_per_hour` | 0 = 不限制（额度保护关闭） |
| `min_join_interval_sec` | 0 = 不限制 |
| `bot_cooldown_sec` | 0 = 不限制 |
| `min_message_length` | 0 = 不跳过（短消息也送决策） |

### 6.4 插话链路与 @ 消息的关系

`@消息`、触发关键词、符合条件的新成员入群消息**不走概率门**（`main.py`
`_check_probability_before_processing` 直接跳过），因此**完全不经过 System One**，
保持原有秒回行为。

---

## 7. 回滚方法

### 7.1 改动前备份对照表（本地开发备份，未随仓库分发）

| 备份路径 | 对应文件 | 说明 |
|---|---|---|
| 本地备份 `main.py` | 插件 `main.py` | 改动前版本（MD5 已核对） |
| 本地备份 `_conf_schema.json` | 插件 `_conf_schema.json` | 改动前版本 |
| 本地备份 `utils/proactive_chat_manager.py` | 插件同名文件 | 改动前版本 |
| 本地备份 `utils/__init__.py` | 插件同名文件 | 改动前版本 |
| （无备份） | `utils/systemone_judge.py`、`tests/` | **新增文件**，删除即还原 |

### 7.2 三种回滚方式

**A. 配置级软回滚（最快，无需改代码/重启后可立即恢复旧行为）**

```
enable_systemone_decision = false      # 或
jev_mode = "off"                        # 或把 systemone_api_key 清空
```

任一即可让决策链短路，回到纯随机概率模式（模块仍加载，但不发起请求）。
⚠️ 若已经写了每群覆盖 JSON，还需把其中各群的 `jev_mode` 改为 `off`（或 `enable: false`），
否则这些群仍会按覆盖配置继续介入。

**B. 单文件回滚（开发副本 → 容器）**

```bash
cd /Users/czy/Documents/AstrBot
P=_local_deploy/plugins/astrbot_plugin_group_chat_plus
cp <本地备份>/main.py "$P/main.py"
cp <本地备份>/_conf_schema.json "$P/_conf_schema.json"
cp <本地备份>/proactive_chat_manager.py "$P/utils/proactive_chat_manager.py"
cp <本地备份>/__init__.py "$P/utils/__init__.py"
rm -f "$P/utils/systemone_judge.py"          # 新增模块，按需删除
docker cp "$P" astrbot:/AstrBot/data/plugins/
docker restart astrbot
```

**C. 整包回滚（部署脚本产出的备份）**

```bash
bash <本地部署脚本>/rollback_chatplus.sh              # 回滚到最近一次备份
bash <本地部署脚本>/rollback_chatplus.sh deploy_YYYYmmdd_HHMMSS   # 指定备份
```

脚本逻辑：删除容器内插件目录 → `docker cp` 还原 `plugin_original` → 还原
`config_original.json` → `docker restart astrbot` → 轮询 Dashboard(6185) 就绪。
（容器内 `__pycache__` 会被清理，避免旧字节码干扰。）

> 回滚后建议确认：插件加载无报错（`docker logs astrbot`）、
> `astrbot_plugin_group_chat_plus_config.json` 的用户手写 extra prompt 字段内容未变。

---

## 8. 测试说明

### 8.1 运行方式（不依赖 AstrBot 运行环境，也不需要真实网络）

```bash
cd /Users/czy/Documents/AstrBot/_local_deploy/plugins/astrbot_plugin_group_chat_plus
python3 -m unittest tests.test_systemone_judge -v          # 本文档定稿实测命令
python3 -m unittest discover -s tests -v                   # 等价（自动发现）
python3 -m pytest tests/ -v                                # 若环境装了 pytest 亦可
```

实测结果（Python 3.13.12，宿主机）：**Ran 67 tests … OK**（约 0.1s）。
测试通过 `importlib` 以文件方式加载 `utils/systemone_judge.py`，并用
`SystemOneJudge._http_post_hook`（伪 HTTP）与 `SystemOneJudge.clock`（伪时间）
注入替身，因此**不导入 astrbot、不发起真实请求、不写仓库外的文件**
（每群配置文件写在 `tempfile.TemporaryDirectory()` 内）。

### 8.2 覆盖点分类（8 个测试类，67 用例）

| 测试类 | 用例数 | 覆盖内容 |
|---|---|---|
| `TestEnableSwitch` | 6 | 总开关 / Key 为空自我禁用 / jev_mode=off / 非法模式归一化 / 配置夹紧（禁用时零 HTTP 调用） |
| `TestResponseParsing` | 13 | noul/score/choice 解析、阈值边界（含等号）、概率夹紧、布尔与裸数值容错、缺 answers/缺 join/错误体/非法 JSON → 未决策 |
| `TestFailureDegradation` | 7 | HTTP 401/500、超时、网络异常、未预期异常一律返回 None 不抛异常；proactive 同样降级；请求体/Header/超时参数断言 |
| `TestRateLimits` | 9 | 每小时上限（含窗口滑出、0=不限）、最小间隔（含 0=不限、到期恢复）、机器人刚说话冷却、被拒决策不占额度、群间隔离、插话与开场额度独立 |
| `TestMessageGuards` | 5 | 太短消息跳过（当前消息 / 最近缓存消息）、够长则调用、0=不跳过、无消息也能决策 |
| `TestPerGroupConfig` | 11 | 默认路径解析、绝对路径、**BOM 文件解析**、阈值/模式/额度覆盖、`enable=false`、未列出群回退全局、损坏文件回退、模板生成（带 BOM）、mtime+节流刷新 |
| `TestStateAndQuestions` | 12 | state 结构、消息条数上限（≤15）、单条截断、当前消息入列、人设覆盖；questions 三问/单问结构（score 4 级、choice 含 other）；proactive 问题文案；mode 提示映射；normalize/extract 辅助函数边界 |
| `TestBotReplyRecording` | 4 | 禁用时零副作用、`note_join` 只记额度、`record_bot_reply` 只记发言时间、从插件缓存收集最近消息 |

### 8.3 未覆盖（需部署后人工/联调验证）

- `main.py` / `proactive_chat_manager.py` 的接入点（依赖 AstrBot 运行环境，无法离线单测）——
  由 Lead 在任务C 部署后用 `docker logs` 观察 `🧠 [SystemOne]` 日志验证；其中
  `_systemone_probability_gate` 内部逻辑已在 `tests` 中以等价路径覆盖（判断/阈值/降级）。
- 真实 System One 端点的连通性与鉴权（依赖线上 Key）——由 Lead 在部署阶段实测。

---

## 附：本文档自查清单

- [x] 接入点行号全部由 `grep`/`sed` 从实际代码复核（main.py 14703 行版本）
- [x] 21 个配置键的名称/类型/默认值逐条对照 `_conf_schema.json` 2214–2344 行
- [x] 降级矩阵每一行对应代码中的实际分支（`is_active`/`get_group_jev_mode`/`_check_guard`/`_post_json`/`_parse_response`/`_judge` 兜底）
- [x] 测试数字为实际运行结果（67 用例 OK）
- [x] 未描述任何代码中不存在的功能

---

## 9. WebUI 集成（Lead 增补，2026-10-04）

插件自带 Web 面板（1451 端口）的配置流程图由 `web/static/js/flow-data.js` 手工定义节点图。
System One 已接入该图，改完 `docker cp` 即生效（静态文件逐请求读盘），浏览器硬刷新可见：

### 9.1 新增节点（2 个）+ 跨链（1 条）

| 位置 | 节点 | 说明 |
|---|---|---|
| 主流水线 · 概率判定系统阶段 · 概率硬限→随机判定之间 | `systemone-judge`（🧠 System One 决策） | 携带 19 个配置键（总开关/Key/端点/模式/阈值/限频/state/方式提示/日志/每群路径），与代码执行顺序一致：概率门通过后、随机判定之前 |
| 主动对话流水线 · 概率与决策阶段 · 失败冷却之后 | `systemone-proactive`（🧠 System One 开场复核） | 携带 `jev_proactive_judge` 开关，描述里说明需先启用主流水线节点 |
| crossLinks | `systemone-judge → systemone-proactive`（决策客户端共用） | 与「内容过滤共用」等既有跨链同风格 |

两节点 key 无重叠（`jev_proactive_judge` 独占主动对话节点），避免触发共用配置标记逻辑。

### 9.2 渲染链路（全部已验证）

- 节点点击 → ConfigEditor 按 `_conf_schema.json` 类型生成控件：
  bool→开关（enable_systemone_decision 等）、int/float→数字框（阈值/限频）、
  string+options→下拉（jev_mode: off/replace/hybrid）、string→文本框（Key/端点）、
  text→文本域（jev_persona_summary）；hint 与「默认: …」自动显示
- 保存 → `PUT /api/config` → schema 校验（21 键全部在 schema 中）→
  `utf-8-sig` 写回配置（BOM 保留）→ 面板「🔄 重启插件」后生效
- 搜索：节点名/描述/配置描述全部进入流程图搜索索引（输入「System One」「jev」可命中）

### 9.3 验证记录

- `node --check` 语法通过；Node 无头加载 `FlowData.init()` 图完整性校验：
  61 节点 / 13 阶段 / 8 跨链，所有 next/nextStage/crossLink 指针可解析，
  链路顺序 hard-limit→systemone-judge→random-roll 与 proactive-failure→systemone-proactive 正确
- 容器内文件 MD5 与开发副本一致（23db1d94…）；面板 1451 HTTP 200
- 备份：本地 `flow-data.js.bak`
