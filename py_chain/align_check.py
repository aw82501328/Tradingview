# -*- coding: utf-8 -*-
"""py↔JS 笔结构对拍（SPEC_divergence_chanset.md 块1 回归）

同一份K线数据双侧重建笔序列，逐周期逐笔 diff，目标 0 差异：
  - py 侧：与 backtest.build_bis 同口径（markWickBars→mergeBars→findFractals→
    buildBi(…,None,sec>=3600,lowerContext)→fixBiExtremes→extendLastBi(trimmed)）
  - JS 侧：.cursor/skills/chan-core/scripts/rebuild_bis.js（复用图表算法源 chan_core.js）

用法：
    python -m py_chain.align_check                       # 默认 bars_all_tf.json 全周期
    python -m py_chain.align_check path/to/bars.json --only 15,60 --verbose 10
退出码：0 = 全部一致；1 = 存在差异；2 = 环境错误。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

REBUILD_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          ".cursor", "skills", "chan-core", "scripts", "rebuild_bis.js")

# 参与逐字段比对的笔字段（与 JS 笔对象字段约定一致）
FIELDS = ["type", "startIdx", "endIdx", "startTime", "endTime",
          "startPrice", "endPrice", "rawCount", "span", "gapLocked", "macdCross"]


def py_rebuild(bl, res, lower_bars=None):
    """py 侧重建（与 backtest.build_bis 完全同口径）。"""
    from .chan_core import (
        mergeBars, findFractals, markWickBars, buildBi, fixBiExtremes,
        extendLastBi, calcATR, calcMACD, intervalSecOf, makeBiLowerContext,
    )
    trimmed = markWickBars(bl)
    merged = mergeBars(trimmed)
    fractals = findFractals(merged)
    atr = calcATR(bl, 14)
    macd = calcMACD(bl)
    bis = buildBi(fractals, merged, atr, macd, None, intervalSecOf(res) >= 3600, makeBiLowerContext(res, lower_bars))
    bis = fixBiExtremes(bis, merged) or bis
    bis = extendLastBi(bis, trimmed)
    return bis


def js_rebuild(data, only=None):
    """JS 侧重建：把（已按时间排序的）数据写入临时文件，调 node rebuild_bis.js。"""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        tmp = f.name
    try:
        cmd = ["node", REBUILD_JS, tmp]
        if only:
            cmd.append(",".join(only))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"node rebuild_bis.js 失败: {r.stderr.strip()[:500]}")
        return json.loads(r.stdout)
    finally:
        os.unlink(tmp)


def fmt_ts(ts):
    from .chan_core import fmtT
    return fmtT(ts) if ts is not None else "?"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="py↔JS 笔结构对拍")
    ap.add_argument("bars", nargs="?", default="bars_all_tf.json", help="K线 JSON 文件路径")
    ap.add_argument("--only", default=None, help="只对拍指定周期，逗号分隔（如 15,60）")
    ap.add_argument("--verbose", type=int, default=5, help="每周期最多展示的差异条数")
    args = ap.parse_args(argv)

    with open(args.bars, encoding="utf-8") as f:
        data = json.load(f)
    # 双侧输入一致化：按时间排序
    data = {res: sorted(bars, key=lambda b: b["time"]) for res, bars in data.items()
            if isinstance(bars, list) and len(bars) >= 6}
    periods = [p.strip() for p in args.only.split(",") if p.strip()] if args.only else list(data.keys())

    try:
        js_bis = js_rebuild(data, periods)
    except Exception as e:
        print(f"ERROR: {e}")
        return 2

    total_diff = 0
    for res in periods:
        py_bis = py_rebuild(data[res], res, data.get("15"))
        js = js_bis.get(res) or []
        diffs = []
        for i in range(max(len(py_bis), len(js))):
            a = py_bis[i] if i < len(py_bis) else None
            b = js[i] if i < len(js) else None
            if a is None or b is None:
                diffs.append((i, "<笔数差异>",
                              f"py={'缺失' if a is None else fmt_ts(a['startTime']) + '~' + fmt_ts(a['endTime'])}",
                              f"js={'缺失' if b is None else fmt_ts(b['startTime']) + '~' + fmt_ts(b['endTime'])}"))
                continue
            for fld in FIELDS:
                if a.get(fld) != b.get(fld):
                    diffs.append((i, fld, a.get(fld), b.get(fld)))
        status = "OK" if not diffs else f"DIFF {len(diffs)} 处"
        print(f"[{res:>4}] py {len(py_bis)} 笔 / js {len(js)} 笔  → {status}")
        for i, fld, pv, jv in diffs[: args.verbose]:
            print(f"    #{i:>3} {fld:<12} py={pv!r:<28} js={jv!r}")
        total_diff += len(diffs)
    print(f"\n合计差异: {total_diff} 处（{'✓ 对拍通过' if total_diff == 0 else '✗ 需修复或白名单'}）")
    return 0 if total_diff == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
