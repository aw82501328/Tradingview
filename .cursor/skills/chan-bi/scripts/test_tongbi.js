/**
 * 同笔后处理纯函数单测：buildTongBiGroups（同笔分组）与 unionIntervalVisibility（并集可见范围）。
 *
 * chan_bi.js 是 CDP 脚本（require 即连 TradingView），无法直接导入；此处按源码标记
 * 提取文件顶层的纯函数段（intervalVisibility 起至「主流程」止），配合真实 chan-core
 * 的 isSameAsUpperBi / intervalSecOf 运行——判定口径与买卖点「同笔例外」共用同一实现，
 * 此处只验证分组/链式合并/排序与可见范围并集逻辑。
 *
 * 运行：node test_tongbi.js（scripts 目录内；无需 CDP 在线）
 */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "chan_bi.js"), "utf8");
const start = src.indexOf("function intervalVisibility");
const end = src.indexOf("// 主流程");
if (start < 0 || end < 0 || end <= start) {
  console.error("提取失败：chan_bi.js 纯函数段标记缺失（intervalVisibility / 主流程）");
  process.exit(1);
}
const core = require("../../chan-core/scripts/chan_core.js");
const factory = new Function(
  "intervalSecOf", "isSameAsUpperBi", "COMPUTE_ONLY",
  src.slice(start, end) + "\n; return { intervalVisibility, buildTongBiGroups, unionIntervalVisibility };"
);
const { intervalVisibility, buildTongBiGroups, unionIntervalVisibility } =
  factory(core.intervalSecOf, core.isSameAsUpperBi, new Set(["30S"]));

let fail = 0;
const check = (name, cond, extra) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${cond ? "" : "  " + (extra || "")}`);
  if (!cond) fail++;
};

const b = (ty, t0, t1, p0, p1) => ({ type: ty, startTime: t0, endTime: t1, startPrice: p0, endPrice: p1 });

// ---------- buildTongBiGroups ----------
const LADDER = ["D", "240", "60", "15", "3"];

// 1) 基本对：60 与 15 完全同笔（四字段重合）
const bi60 = [b("down", 1000, 5000, 4400, 4300), b("up", 5000, 9000, 4300, 4450)];
const bi15 = [b("down", 1000, 5000, 4400, 4300), b("up", 5000, 5200, 4300, 4320), b("down", 5200, 9000, 4320, 4290)];
{
  const g = buildTongBiGroups({ "60": bi60, "15": bi15 }, LADDER);
  check("基本对 60=15 成组", g.length === 1 && g[0].members.length === 2 &&
    g[0].members[0].res === "60" && g[0].members[1].res === "15", JSON.stringify(g));
}

// 2) 无重叠 → 0 组
{
  const g = buildTongBiGroups({ "60": bi60, "15": bi15.map(x => ({ ...x, startTime: x.startTime + 100000, endTime: x.endTime + 100000 })) }, LADDER);
  check("无重叠 0 组", g.length === 0, JSON.stringify(g));
}

// 3) 容差：低级别 1 根 bar（15m=900s）内偏移算同笔，2 根不算
{
  const g1 = buildTongBiGroups({ "60": bi60, "15": [b("down", 1000 + 900, 5000 - 900, 4400, 4300)] }, LADDER);
  check("时间偏移 ≤1 根15m bar 判同笔", g1.length === 1, JSON.stringify(g1));
  const g2 = buildTongBiGroups({ "60": bi60, "15": [b("down", 1000 + 1800, 5000, 4400, 4300)] }, LADDER);
  check("时间偏移 2 根 bar 不判同笔", g2.length === 0, JSON.stringify(g2));
}

// 4) 链式合并：D=240=60=15=3 → 一组五员、大到小
const biD = [b("down", 1000, 5000, 4400, 4300)];
const bi240 = [b("down", 1000, 5000, 4400, 4300)];
const bi3 = [b("down", 1000, 5000, 4400, 4300)];
{
  const g = buildTongBiGroups({ "D": biD, "240": bi240, "60": bi60, "15": bi15, "3": bi3 }, LADDER);
  check("链式合并成一组五员", g.length === 1 && g[0].members.length === 5,
    JSON.stringify(g.map(x => x.members.map(m => m.res))));
  check("链式组成员大到小", g.length === 1 && g[0].members.map(m => m.res).join(",") === "D,240,60,15,3",
    JSON.stringify(g.map(x => x.members.map(m => m.res))));
}

// 5) 方向不同不算
{
  const g = buildTongBiGroups({ "60": bi60, "15": [b("up", 1000, 5000, 4400, 4300)] }, LADDER);
  check("方向不同不判同笔", g.length === 0, JSON.stringify(g));
}

// 6) 30S 参与 ladder 时被跳过（COMPUTE_ONLY 只算不画）
{
  const g = buildTongBiGroups({ "3": bi3, "30S": [b("down", 1000, 5000, 4400, 4300)] }, ["3", "30S"]);
  check("30S（只算不画）不参与配对", g.length === 0, JSON.stringify(g));
}

// 7) 组间排序：60 的 down 笔只与 15 同笔（一组 top=60），60 的 up 笔再与 240 同笔
//    （一组 top=240）→ 按最大周期升序：60 组在前 240 组在后（后处理先重建小周期组）
{
  const g = buildTongBiGroups({
    "240": [b("up", 20000, 30000, 4300, 4500)],
    "60": [b("down", 1000, 5000, 4400, 4300), b("up", 20000, 30000, 4300, 4500)],
    "15": [b("down", 1000, 5000, 4400, 4300), b("up", 20000, 30000, 4300, 4500)],
  }, LADDER);
  check("多组按最大周期升序", g.length === 2 && g[0].members[0].res === "60" && g[1].members[0].res === "240",
    JSON.stringify(g.map(x => x.members.map(m => m.res))));
}

// 8) 局部重跑视角：本次只有 15 的数据 → 0 组（配对需要相邻两级都有数据）
{
  const g = buildTongBiGroups({ "15": bi15 }, LADDER);
  check("单周期数据 0 组", g.length === 0, JSON.stringify(g));
}

// ---------- unionIntervalVisibility ----------
{
  const u = unionIntervalVisibility(["15", "60"]);
  check("并集 15+60 = 分钟3..15 + 小时1",
    u.minutes === true && u.minutesFrom === 3 && u.minutesTo === 15 &&
    u.hours === true && u.hoursFrom === 1 && u.hoursTo === 1 &&
    u.seconds === false && u.days === false, JSON.stringify(u));
}
{
  const u = unionIntervalVisibility(["3", "15", "60"]);
  check("并集 3+15+60 = 秒30 + 分钟3..15 + 小时1",
    u.seconds === true && u.secondsFrom === 30 && u.secondsTo === 30 &&
    u.minutesFrom === 3 && u.minutesTo === 15 && u.hours === true, JSON.stringify(u));
}
{
  const u = unionIntervalVisibility(["240", "D"]);
  check("并集 240+D = 小时1..24 + 日1（关闭成员的模板默认值不污染范围）",
    u.hours === true && u.hoursFrom === 1 && u.hoursTo === 24 &&
    u.days === true && u.daysFrom === 1 && u.daysTo === 1 &&
    u.minutes === false, JSON.stringify(u));
}
{
  const u = unionIntervalVisibility(["15"]);
  const ref = intervalVisibility("15");
  check("单成员并集 = 原配置(15)",
    JSON.stringify(u) === JSON.stringify(ref), JSON.stringify(u));
}

console.log(fail === 0 ? "\n全部通过" : `\n${fail} 项失败`);
process.exit(fail === 0 ? 0 : 1);
