# 物品变化 Log 校验工具 · V2 技术设计文档

| 项目 | 内容 |
|---|---|
| 文档版本 | v2.0 |
| 日期 | 2026-10-23 |
| 状态 | 设计稿 |
| 定位 | 物品改动校验 / 测试提效 |

---

## 1. 背景与目标

### 1.1 问题

策划修改物品配置（新增/删除/修改材料ID）后，需要验证改动是否正确落地。目前验证方式：人工打开柜子 → 肉眼对比 → 记录结果。效率低、易遗漏。

### 1.2 目标

1. 录入一批材料变化的 ID（改动清单）
2. 从**本地 log 文件**和际数据
3. 自动化模拟点击柜子触发 log 输出
4. 将采集到的 ID 与改动清单比对
5. 输出一份测试报告，标记 通过/缺失/多余/异常

### 1.3 非目标

- 不做通用自动化测试框架
- 不做 log 实时流式处理
- 不做跨版本回归（首期只管当次改动）

---

## 2. 整体架构

```
┌─────────────────────────────────────────────┐
│              用户交互层（Agent / CLI）         │
│  录入改动 ID 列表 → 触发校验流程 → 查看报告   │
└──────────┬──────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────┐
│              调度层（主控脚本）                │
│  按顺序/并行调度下面的子脚本                  │
└──┬───────┬───────┬───────┬───────┬──────────┘
   │       │       │       │       │
   ▼       ▼       ▼       ▼       ▼
 录入ID  本地log  服务端log  模拟点击  比对+报告
```

### 2.1 模块划分

| 模块 | 脚本 | 职责 |
|---|---|---|
| 改动录入 | `input_change_ids.py` | 录入/读取本次改动的物品ID列表 |
| 本地log采集 | `collect_local_log.py` | 从本地log文件提取物品相关记录 |
| 服务端log采集 | `collect_server_log.py` | 从服务端HTTP接口拉取log |
| 自动化点击 | `auto_click.py` | ADB坐标点击 或 GM指令触发柜子交互 |
| ID比对 | `compare_ids.py` | 将采集ID与改动ID比对，生成差异 |
| 报告生成 | `generate_report.py` | 输出可读的测试报告（txt/md） |
| 主控 | `main.py` | 串联上述所有模块的入口脚本 |

---

## 3. 执行流程

### 3.1 脚本模式（当前阶段）

```
1. 运行 main.py
2. 输入改动ID列表（手动输入 或 从文件读取）
3. 执行自动化点击 → 触发客户端打开柜子 → 产生log
4. 采集本地log（解析log文件中的物品ID）
5. 采集服务端log（HTTP请求获取物品ID）
6. 比对：改动ID vs 本地log ID vs 服务端log ID
7. 生成报告 → 保存到 reports/log_check_v2/
```

### 3.2 Skill模式（后续阶段）

```
用户 → Agent对话："帮我检查这批物品改动"
  ↓
Agent 调用 skill：
  1. 让用户粘贴/上传改动ID列表
  2. 调 auto_click.py 模拟点击
  3. 调 collect_local_log.py + collect_server_log.py 采集
  4. 调 compare_ids.py 比对
  5. 调 generate_report.py 输出
  6. 将报告内容返回给用户
```

---

## 4. 自动化点击方案

### 4.1 方案A：ADB坐标点击（推荐）

```bash
# 点击屏幕坐标 (x, y)
adb shell input tap 500 800

# 滑动
adb shell input swipe 500 1000 500 500

# 截图验证
adb shell screencap /sdcard/screen.png
adb pull /sdcard/screen.png ./screen.png
```

流程：
1. 提前录制柜子坐标（截图标注）
2. 将坐标存入配置文件 `click_coords.json`
3. 脚本按配置依次执行 `adb shell input tap`
4. 每次点击后等待固定时间（如2秒）让log产生

### 4.2 方案B：GM指令触发

如果打开柜子有对应的GM/debug指令，直接发指令即可，无需ADB。
比如：`open_cabinet <cabinet_id>` 之类。

**优先用方案B，没有对应指令时用方案A。**

### 4.3 坐标配置示例

```json
{
  "cabinets": [
    {"name": "柜子1", "x": 500, "y": 800, "wait_ms": 2000},
    {"name": "柜子2", "x": 700, "y": 800, "wait_ms": 2000}
  ],
  "back_button": {"x": 100, "y": 100, "wait_ms": 1000}
}
```

---

## 5. Log 采集

### 5.1 本地 log

- 读取本地log文件（路径可配置）
- 按时间窗口过滤（只取本次操作时间段内的log）
- 用正则提取物品ID相关行
- 输出：`{timestamp, item_id, action, detail}`

### 5.2 服务端 log

- HTTP GET/POST 请求服务端log接口
- 传参：玩家ID、时间范围、log类型
- 解析返回的JSON
- 输出：同上格式

---

## 6. 比对逻辑

```
输入：
  - change_ids: Set[str]     # 改动清单
  - local_ids: Set[str]      # 本地log采集到的ID
  - server_ids: Set[str]     # 服务端log采集到的ID

比对：
  matched     = change_ids & local_ids & server_ids    # 三方一致 → 通过
  missing     = change_ids - (local_ids | server_ids)  # 改了但log里没有 → 缺失
  extra       = (local_ids | server_ids) - change_ids  # log有但不在改动里 → 多余
  mismatch    = local_ids ^ server_ids                 # 本地和服务端不一致 → 异常
```

---

## 7. 报告格式

```markdown
# 物品改动校验报告

- 检测时间: 2026-10-23 18:00:00
- 改动ID数量: 15
- 本地log采集ID数量: 14
- 服务端log采集ID数量: 14

## 结果摘要

| 状态 | 数量 |
|------|------|
| ✅ 通过 | 13 |
| ❌ 缺失 | 2 |
| ⚠️ 多余 | 0 |
| 🔴 不一致 | 1 |

## 详细列表

### ✅ 通过 (13)
- ID_001, ID_002, ...

### ❌ 缺失 (2)
- ID_014: 改动清单有，本地log无，服务端log无
- ID_015: 改动清单有，本地log无，服务端log有

### 🔴 不一致 (1)
- ID_010: 本地log有，服务端log无
```

---

## 8. 配置文件

`scripts/log_check_v2/config.json`

```json
{
  "local_log_path": "C:/game/logs/client.log",
  "server_log_url": "http://xxx.xxx.xxx/api/logs",
  "server_log_token": "",
  "player_id": "",
  "adb_device": "",
  "click_coords_file": "click_coords.json",
  "report_output_dir": "../../reports/log_check_v2/",
  "log_time_window_seconds": 60,
  "item_id_pattern": "item_id[=:]\\s*(\\d+)"
}
```

---

## 9. 目录结构

```
物品check/
├── docs/
│   └── v2/
│       └── material_change_log_check_v2.md    # 本文档
├── scripts/
│   └── log_check_v2/
│       ├── main.py                  # 主控入口
│       ├── input_change_ids.py      # 改动ID录入
│       ├── collect_local_log.py     # 本地log采集
│       ├── collect_server_log.py    # 服务端log采集
│       ├── auto_click.py            # 自动化点击
│       ├── compare_ids.py           # ID比对
│       ├── generate_report.py       # 报告生成
│       ├── config.json              # 配置文件
│       └── click_coords.json        # 点击坐标配置
└── reports/
    └── log_check_v2/                # 报告输出目录
```

---

## 10. 里程碑

| 阶段 | 内容 | 预计 |
|------|------|------|
| P1 | 脚本框架搭建 + 改动ID录入 + 本地log采集 | 1周 |
| P2 | 服务端log采集 + 比对逻辑 + 报告生成 | 1周 |
| P3 | 自动化点击（ADB/GM） | 1周 |
| P4 | 联调 + 合并为skill | 1周 |

---

## 11. 风险

| 风险 | 对策 |
|------|------|
| 柜子坐标不固定（分辨率变化） | 配置文件 + 截图校准脚本 |
| 服务端log接口不可用 | 降级为只用本地log |
| log格式变动 | 正则模式可配置 |
| ADB连接不稳定 | 重试机制 + 超时检测 |
