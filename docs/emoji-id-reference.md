# 表情 ID 参考

表情回应在协议端的标识（**表情 ID**，见 CONTEXT.md）有两套编号并存。本文件是
配置与排查时的查表参考；全量目录在协议端运行时侧，见文末。

## 两套编号体系

| 体系 | 形态 | 示例 | 归一后 |
|---|---|---|---|
| QQ 小黄脸 | 1~3 位纯数字编号 | `4`、`76` | 原样 |
| Unicode emoji | 码点十进制（≥4 位数字） | 👍 → `128077` | 原样 |
| emoji 字符（配置输入） | 字符本身 | `👍` | 取首码点 `128077` |

归一规则见 `reactions.py` 的 `normalize_emoji_id`：纯数字原样保留；emoji 字符取
首个码点。协议端发送侧用 `code.length > 3` 判类型（≤3 位当 QQ 小黄脸、更长当
Unicode 码点），归一正好抹平「上报给字符、发送要码点」的不对称。

## QQ 小黄脸编号边界

编号语义没有公开权威静态表，以下边界来自 SnowLuma（`arcwsl:~/github/REFERENCE/snowluma`）
`packages/protocol/src/sys-face-store.ts` 的 `faceWireFor`：

- `face_id < 260`：classic 小黄脸
- `260 <= face_id`：small（新式表情）
- 超级表情：目录条目带 `ani_sticker_pack_id` / `ani_sticker_id` /
  `ani_sticker_type` 字段，不是普通编号

本插件**不内置** QQ 小黄脸编号→语义的映射（不臆造私有编号语义），配置反义对
时如需私有编号，请从协议端全量目录（下文）查证后填入 `extra_pairs`。

## 内置反义映射的编号对照

内置反义映射（`reactions.py` 的 `DEFAULT_OPPOSITE_PAIRS`）全部使用 Unicode
emoji，归一后编号如下：

| 表情 | 码点 | 表情 | 码点 |
|---|---|---|---|
| 👍 | 128077 | 👎 | 128078 |
| ❤ | 10084 | 💔 | 128148 |
| 😂 | 128514 | 😭 | 128557 |
| 😄 | 128516 | 😡 | 128545 |
| ✅ | 9989 | ❌ | 10060 |
| 🔥 | 128293 | ❄ | 10052 |
| ⬆ | 11014 | ⬇ | 11015 |
| 😍 | 128525 | 🤢 | 129314 |
| 😈 | 128520 | 😇 | 128519 |
| 🤮 | 129326 | 😋 | 128523 |
| 🥶 | 129398 | 🥵 | 129397 |
| ☀ | 9728 | 🌧 | 127783 |
| 🚀 | 128640 | 🐢 | 128034 |
| 😊 | 128522 | 😠 | 128544 |
| 🎉 | 127881 | 😢 | 128546 |
| ➕ | 10133 | ➖ | 10134 |

（码点可在 Python 里复核：`str(ord("👍"))`。）

## 全量目录获取

QQ 系统表情目录在协议端运行时从 QQ 服务器拉取并落盘。SnowLuma 暴露
`fetch_sys_faces` OneBot 动作（`packages/onebot/src/actions/system-face.ts`）：

```
POST http://<协议端地址>/fetch_sys_faces
{"refresh": false}
```

返回 `packs: [{ pack_name, emojis: [{ q_sid, q_des, em_code, q_cid, is_super, ... }] }]`。
同机部署时目录快照在协议端容器的 `data/sys-face-catalog.json`。`q_sid` 是目录
标识（部分为 Unicode 表情字符串）、`q_des` 是表情描述——这是查证私有编号语义
的权威来源。
