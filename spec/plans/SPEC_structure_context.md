# 预期笔与够笔运行笔统一规则

状态：已实现。适用 Python 回测／回放／实时及 JS 图表分析。

## 阶段与数据边界

- 已有下跌笔的当前最低端点出现普通底分型（右肩已收盘）后，立即建立预期上涨段。卖侧镜像。预期段立即参与下级买卖点、方向及中枢归属，不要求强分型。
- 自起点所在合并块起满5块（包含起点）为够笔运行中；顶端尚可延伸。正式反向分型及成笔规则通过后，原始笔列表中的真实笔接替该段，不重复追加同一段。
- 预期段不写回画笔缓存。原始笔和计算用结构视图分开；活动端点不能生成或锚定已确认的一类点。一类点原强分型要求继续有效。
- 判断仅消费决策时刻已收盘前缀。极值时间 `endTime` 与覆盖边界 `coverageEnd` 分开：末段覆盖到决策时刻，极值之后的下级回调仍属于该段。创新低／新高否定旧预期后重新判断，不使用旧分型。

## 核心接口

两端 `chan_core` 提供同构接口：

- `mergedSegmentCount(merged, startTime, barSec)`：按合并块 `_firstTime` 至末根原始K收盘范围定位校准端点；包含起点块。输入必须为当前已收盘合并前缀，禁止先合并未来历史再截尾。
- `buildStructureContext(bis, bars, barSec, tCut, merged, fractals, lowerContext)`：返回 `confirmedBis`、结构 `bis`、`current`、`merged`、`cutoff`。当前段携带 `phase`（confirmed/expected/running）、`mergedCount`、`enough`、`coverageEnd`；追加预期段标记 `_forming`。明确给出截止时刻且原始数据含未来时，按截止前缀重建；60分钟保留15分钟补充确认上下文。
- `structurePeriods`：批量准备结构视图。Python可复用引擎增量合并／分型及工作缓存；输入闭合水位、极值、阶段和合并块变化使相关缓存失效，覆盖边界每拍推进。历史修正／重同步清空缓存。
- `confirmedStructureBis` 与 `pointEligibleBis`：前者排除追加预期段，供一类锚定；后者排除自身尚不够笔的预期段，供下级二／三类点及中枢识别。作为上级容器时，预期段无需够笔即可参与。

## 计数与消费

- 进场预期、检测周期门槛、低级别背驰候选段和方向相位均按合并K线块数判断。`expectBiMinBars`、`realtime_min_bars` 保持各自参数作用范围，默认5。`expectBiEnough` 控制预期段进场，不关闭结构预期。
- 角度强弱继续采用幅度／原始K线数量，参数单位与默认值不变。
- Python与JS方向相位一致，说明保留具体买卖点类型和预期阶段。观望状态仍为 `dir=None/null`，附原因并双向放行。
- 所有调用者传递结构视图，不在各模块单独创造“日线上涨笔”。JS画笔缓存保留原始笔；分析消费者临时构造结构视图。

## 固定验收

`OANDA:XAUUSD 2026-08-06 19:51`：日线上涨运行段13块（含底部块）；4小时二买3996.055、类二买4019.24。当时4小时顶部已出现预期回调，说明为 **4小时类2买回调中（预期段）**，方向观望。

夹具：`py_chain/fixtures/structure_xauusd_20260806.json.gz`。测试：`python -m unittest py_chain.test_structure_context`（含 Node 对照）。历史信号不追溯改写，使用相同参数离线重跑对比信号时间、数量、成交及盈亏。
