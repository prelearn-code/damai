# AGENTS.md — 本项目对 AI 的硬性限制

本文件是 AI 在本仓库内工作的行为约束。优先级高于一般性建议；与本文件冲突的修改一律不做，除非用户明确、逐条地推翻本文件。

## 项目是什么

大麦网演唱会抢票自动化（Android 真机 + uiautomator2 + ADB）。主程序只执行：
**演唱会详情页 → 票档处理 → 确认购买 → 立即提交 → 支付宝收银台**，检测到支付宝成为前台 App 后立即退出。

安全红线（永不可违反）：
- 绝不点击支付宝的"付款/确认支付"按钮，程序不校验金额；
- 程序不返回、不搜索、不整理页面，每次执行前由用户手动把手机停在目标演唱会详情页；
- 不自动下单前必须确认用户意图，`--stop-before-submit` 之外一律不碰真实支付。

## 限制 1：演唱会 profile JSON 的命名规则（强制）

`profiles/` 下每个演唱会一个 JSON 配置文件，命名必须遵循：

```
{城市拼音}_{艺人拼音}_{YYYY-MM-DD}_{HH-MM}_mode{模式号}.json
```

- 城市拼音、艺人拼音：全小写，多音节用下划线 `_` 连接；
- `YYYY-MM-DD`：开售日期；
- `HH-MM`：开售时间，**用短横线 `-` 分隔，不是冒号**；
- `mode{模式号}`：`mode1` / `mode2` / `mode3`。

已存在的合法示例：

| 文件 | 含义 |
| --- | --- |
| `linyi_li_ronghao_2026-08-18_13-00_mode1.json` | 临沂站·李荣浩·2026-08-18 13:00 开售·模式1 |
| `suzhou_wang_lihong_2026-08-12_15-33_mode1.json` | 苏州站·王力宏·2026-08-12 15:33 开售·模式1 |

仓库里的旧式命名（`li_ronghao_urumqi.json`、`lin_zhixuan_beijing.json` 等）属于历史遗留，**新增演唱会一律用上述新规则命名，不得沿用旧式命名，不得重命名已有文件**。

## 限制 2：新增演唱会的默认抢票逻辑（强制）

用户新增一个演唱会详情页时，除非用户主动提出改变（例如明确说要模式 2/模式 3、不同的倒计时策略、不同的票价），否则默认生成与现有"倒计时到模式 1 的完整执行"完全相同的逻辑：

1. profile 字段沿用新格式模板（见下）；
2. 运行参数固定为 `--selection-mode 1 --wait-for-sale --fast --sale-time "<开售时间>"`；
3. 行为 = 现有 `wait_for_sale_fast()` + `select_mode_1()` 完整链路，不得简化、不得换成别的检测方式。

"用户不主动提出来，就默认不变"——不允许 AI 自行发挥、猜测或"优化"这条默认链路。

### 新格式 profile 模板（新增演唱会照抄，只改字段值）

```json
{
  "artist": "艺人中文名",
  "event_keyword": "城市站",
  "selection_mode": 1,
  "target_price": 目标票价,
  "screen_size": [1440, 3200],
  "timeout": 30,
  "queue_timeout": 180,
  "artifacts": "artifacts/{城市-艺人-YYYY-MM-DD-HH-MM-modeN}",
  "sale_wait": {
    "timeout": 1800,
    "refresh_interval": 0.5,
    "refresh_settle": 0.25,
    "refresh_start": [720, 500],
    "refresh_end": [720, 1800],
    "refresh_duration_ms": 180,
    "selector": {
      "resource_id": "cn.damai:id/id_project_count_down_layout",
      "confirm_count": 2,
      "poll_interval_s": 0.01,
      "refresh_interval_s": 10,
      "refresh_stop_at_s": 2.0,
      "t_fallback_ms": 150
    }
  },
  "calibrated_points": {
    "purchase": [898, 3082]
  }
}
```

> `calibrated_points.purchase` 是"购票入口"的屏幕坐标，必须在目标真机（1440×3200）上实际校准，不得凭经验乱填；其他坐标组按需校准。

### "倒计时到模式 1"完整执行链路（默认行为，勿改动）

1. 详情页 `wait_for_sale_fast()`：先确认倒计时节点 `cn.damai:id/id_project_count_down_layout` 稳定存在（连续 3 次命中，防止误判），再轮询其消失——连续 `confirm_count`（默认 2）次消失即视为开售；`--sale-time` 提供定时器兜底（开售时间 + profile 的 `t_fallback_ms`，代码默认 300ms，当前 profile 配置 150ms）；等待期间每 `refresh_interval_s`（默认 10s）下拉刷新一次，开售前 2s 停止刷新。
2. 开售 → 立即点击 `calibrated_points.purchase`。
3. 票档页（模式 1 已预约、预约票价有票）：轮询 `cn.damai:id/btn_buy_view` 且 `clickable=true`，出现即点击"确认"。
4. 确认购买页：轮询文本为"立即提交"且 `clickable=true` 的节点，出现即点击。
5. 支付宝收银台：模式 1 专用重试循环（点"继续"→ 点"立即提交"→ 点"去支付"）直到支付宝成为前台 App；到达即结束，**绝不点击付款**。

## 目录结构

- `scripts/damai_checkout.py` — 主抢票脚本（详情页→支付宝收银台）
- `scripts/measure_price_ready.py`、`scripts/open_event_smoke_test.py`、`scripts/phone_probe.py`、`scripts/test_confirm_button_latency.py` — 辅助/调试脚本
- `profiles/*.json` — 演唱会配置（见限制 1）
- `artifacts/` — 真机截图与 UI XML（已被 .gitignore 忽略）
- `PHONE_DEBUG.md` — 真机调试说明（环境、设备序列号、三种模式用法）

## 环境与运行

- 设备序列号：`8595251f`，校准分辨率 `1440×3200`
- Python 环境：`.venv-phone`（手机侧）/ `.venv`（电脑侧），用 `requirements-phone.txt`
- ADB：`E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe`
- 运行方式（倒计时到模式 1 的标准命令）：

```powershell
.\.venv-phone\Scripts\python.exe scripts\damai_checkout.py `
  --profile profiles\linyi_li_ronghao_2026-08-18_13-00_mode1.json `
  --serial 8595251f `
  --selection-mode 1 `
  --wait-for-sale --fast `
  --sale-time "2026-08-18 13:00:00"
```

## 修改代码时的注意

- 坐标全部以 1440×3200 校准，任何坐标相关改动必须保留屏幕尺寸校验（`screen_size` 不一致即报错）；
- 修改 `wait_for_sale_fast` / `select_mode_1` 等默认链路时，视为修改"限制 2"的默认行为，必须先向用户确认；
- 运行结果 JSON 中 `sale_trigger` 为 `signal` 或 `timer`，是排查抢票是否触发的重要字段；
- 新增/修改 profile 后如需校验，跑 `open_event_smoke_test.py` 或按 PHONE_DEBUG.md 的真机流程验证。
