# Tasks: repeat-and-rebrand

> 细则：seam = 本条任务的测试落点（公共接口边界）；验收 = 可独立验证的标准。
> 实现约定见 `design.md`（D1–D8），行为契约见 `specs/message-repeat/spec.md`（19 场景）。

## 1. 纯逻辑层 `repeats.py`

- [x] 1.1 实现宽松归一函数 `normalize_repeat_text`
  - seam：`repeats.normalize_repeat_text`（模块导出，纯函数）
  - 验收：` 草 `→`草`；`HHH` 与 `hhh` 归一结果相同；`哈  哈`→`哈 哈`（连续空白折叠）；空串与纯空白输入不崩溃；对应单测全绿
- [x] 1.2 实现消息资格判定 `extract_repeat_candidate`
  - seam：`repeats.extract_repeat_candidate`（模块导出；输入序列化 SessionMessage dict，输出候选（group_id、sender_qq、原始文本）或 None）
  - 验收：仅含 text 段且 trim 非空产出候选；含 @/at、reply、image、face 段返回 None；`is_notify=True` 返回 None；文本以 `/` 开头返回 None；超 `max_text_length` 返回 None；`sender_qq == self_qq` 返回 None；无 group_info（私聊）返回 None；结构异常（非 dict、缺字段）返回 None 不抛异常；对应单测全绿
- [x] 1.3 实现 `RepeatState` 链状态机
  - seam：`repeats.RepeatState.record`（公开同步方法，无 await 点，单次调用内完成窗口清理→计数→达标判定→资格消耗）
  - 验收：`distinct_senders=true` 时 A/B/C 三发送者达标、A 连发三条计数仍 1；`distinct_senders=false` 时逐条计数、A 三条即达标；窗口滚动后早期条目失效（`time.time()` 传参，不传 0.0）；达标即写终结标记，TTL 内 D/E 再发同文本不返回决策资格（沉默口径同样终结）；`chain_ttl_seconds` 过期后新一轮 A/B/C 可再达标；链数超 `max_chains` LRU 淘汰最旧；达标时返回链内最后一条原始文本（非归一产物）；对应单测全绿
- [x] 1.4 实现三出口决策函数 `decide_repeat`
  - seam：`repeats.decide_repeat`（模块导出，纯函数，`roll` 与 `choice` 可注入）
  - 验收：`roll < probability` → 复读出口（携带原始文本）；`probability ≤ roll < probability + interrupt_probability` → 打断出口（池内随机一条，不携带复读文本）；其余 → 沉默出口；三出口互斥；`interrupt_texts` 为空列表时回落内置默认池不抛异常；对应单测全绿

## 2. 配置层

- [x] 2.1 `config.py` 新增 `RepeatSection` 与 `scope.ignore_senders`
  - seam：`SendingEmojisConfig` 构造与字段默认值（类名于任务 4.1 统一改为 `RepeaterConfig`）
  - 验收：`repeat` 节字段与默认值同 design D6 表逐项一致（enabled=true、window_seconds=120、threshold=3、distinct_senders=true、probability=0.6、interrupt_probability=0.15、interrupt_texts 五条默认、chain_ttl_seconds=1800、cooldown_seconds=120、max_text_length=200、max_chains=2000）；`scope.ignore_senders` 默认空列表；各字段带中文 UI hint；默认值单测全绿
- [x] 2.2 `config.toml` 同步默认值
  - seam：`config.toml` 与 `config.py` 的一致性断言（沿用 `tests/test_config.py` 既有模式）
  - 验收：toml 每项与 `config.py` default 逐字段一致；一致性单测全绿

## 3. 插件接入

- [x] 3.1 扩展 fake SDK 的 `ctx.send.text` 替身
  - seam：`tests/fake_maibot_sdk.py` 的 fake `ctx.send`（记录调用供断言）
  - 验收：替身记录每次 `(text, stream_id)` 调用；既有测试套件零回归
- [x] 3.2 `plugin.py` 新挂 observe 模式 `before_process` hook handler
  - seam：插件类 hook handler 方法（插件级测试直接调用，模拟主程序 observe 分发）
  - 验收：`plugin.enabled=false` 或 `repeat.enabled=false` 时直接返回不进链；黑名单群/`ignore_senders` 名内发送者不进链；三人接龙第三次调用消耗资格并掷骰；冷却期内达标不消耗资格不掷骰，冷却结束后新发送者加入可触发；冷却进入时机为复读/打断出口命中（沉默不冷却，发送成败不影响已进入的冷却）；解析失败打 debug 不打 warning；对应单测全绿
- [x] 3.3 实现复读/打断发送编排
  - seam：fake `ctx.send.text` 的调用记录
  - 验收：复读出口发送链内最后一条原始文本（含原始空白，非归一产物）；打断出口只发池内一条文本、不附带复读文本；沉默出口零调用；发送经 `behavior.delay_min/max` 随机延迟后执行（可注入 sleep 断言）；stream_id 复用 `_get_stream_id` 缓存；`ctx.send.text` 异常或失败记 warning 不抛不重试；对应单测全绿
- [x] 3.4 更新 `_manifest.json`
  - seam：`_manifest.json` 文件内容断言（沿用既有 manifest 断言模式）
  - 验收：id=`arcdent.repeater-plugin`、name=「复读插件」、description 覆盖双行为；capabilities 保留既有项并追加 `before_process` 观测声明；JSON 语法合法；断言用例全绿
- [x] 3.5 插件级边界用例补全
  - seam：同 3.2/3.3（组合场景）
  - 验收：复读冷却不影响贴表情冷却（刚复读完，表情上报照常走表情自身冷却判定）；`repeat.enabled=false` 时表情跟风照常触发；「断言不触发」用例概率拉满（`probability`/`interrupt_probability` 置 0）；时间戳全部 `time.time()`；全量套件零回归

## 4. 改名与文档

- [x] 4.1 类名与 docstring 全量改名
  - seam：`create_plugin()` 返回类型 + 全量测试套件
  - 验收：`SendingEmojisPlugin`→`RepeaterPlugin`、`SendingEmojisConfig`→`RepeaterConfig`；`plugin.py`/`config.py`/`reactions.py` 模块 docstring 与 `tests/` 内引用同步；`grep -rn "SendingEmojis" --include="*.py" --include="*.toml" --include="*.json"` 仅剩 openspec 工件与 CHANGELOG 历史条目；pytest 全绿
- [x] 4.2 重写 README 与项目规则文档
  - seam：`README.md` / `AGENTS.md` / `CHANGELOG.md` 文件本身
  - 验收：README 含双功能定位、安装配置（含 `repeat` 节与 `ignore_senders` 说明、自定义命令前缀盲区提示）、迁移段落（旧 id 卸载→新 id 安装、仓库改名步骤）；AGENTS.md 结构补 `repeats.py` 与复读规则条目（术语、`set` 铁律对等的复读铁律）；CHANGELOG 记 BREAKING 条目
- [x] 4.3 全量验证与部署交接
  - seam：验证命令输出
  - 验收：`uv run --with pytest --with aiohttp pytest tests/ -q` 零失败；`uv run --with pyright --with aiohttp pyright` 生产代码零错误；`ruff check` 仅存四类既有有意偏离（BLE001/N999/SIM103/SIM117），无新增；用户手动操作清单（GitHub 仓库改名、MaiBot 容器目录迁移）已在 README 迁移段落可循
