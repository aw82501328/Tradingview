# -*- coding: utf-8 -*-
"""
对齐测试：用 Python 版 chan_core 独立计算笔/买卖点，与 JS 导出的 baseline.json 对比。
验证 chan_core.py 与 chan_core.js 在同一输入下逐函数等价。

用法：
    python vnpy/align_test.py
"""
import json
import os
import sys

# Windows 控制台默认 GBK，强制 UTF-8 输出（中文注释/日志）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import chan_core as core

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "baseline.json")

PERIODS = ["D", "240", "60", "15", "3"]
ATR_FILTER = 0.5

EPS = 1e-9  # 浮点容差（时间戳为整数秒，价格用该容差）


def approx(a, b, eps=EPS):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= eps


def compute_bis(rawBarsByRes, refBarsByRes):
    """复现 chan-bi 的笔计算流程。"""
    allBis = {}
    prevBis = None
    for i, res in enumerate(PERIODS):
        rawBars = rawBarsByRes[res]
        merged = core.mergeBars(rawBars)
        fractals = core.findFractals(merged)
        atr = core.calcATR(rawBars, 14)
        macdArr = core.calcMACD(rawBars)
        # 区间套强制对齐：把上一层笔端点作为锁定端点传入 buildBi
        lockedPivots = core.lockedPivotsOf(prevBis)
        bis = core.buildBi(fractals, merged, atr, macdArr, lockedPivots)
        threshold = atr * ATR_FILTER
        bis = [b for b in bis if b["span"] >= threshold]
        drawBis = bis
        drawBis = core.extendLastBi(drawBis, rawBars)
        lowerRes = core.lowerResOf(res)
        if lowerRes and res in refBarsByRes:
            refBars = refBarsByRes[res]
            drawBis = core.calibrateBiTimes(drawBis, rawBars, refBars, core.intervalSecOf(res))
        # 区间套强制对齐：把本级别笔拐点对齐到紧邻上级笔拐点
        if prevBis:
            drawBis = core.alignBiToUpper(drawBis, prevBis, core.intervalSecOf(PERIODS[i - 1]))
        allBis[res] = drawBis
        prevBis = drawBis
    return allBis


def compute_buysell(allBis, rawBarsByRes, fromTs):
    """复现 mark-buy-sell 的买卖点计算流程（含跨周期共振颜色判定）。"""
    upperBis = None
    buySellByRes = {}
    marksByRes = {}
    periodMarks = {}
    RED = "#F23645"
    GREEN = "#089981"
    UPPER_CLASS = {"1买", "2买", "3买", "1卖", "2卖", "3卖", "类2买", "类2卖"}

    for pi, res in enumerate(PERIODS):
        curBis = [b for b in allBis[res] if b["endTime"] >= fromTs]
        rawBars = rawBarsByRes[res]
        atr = core.calcATR(rawBars, 14)
        macdArr = core.calcMACD(rawBars)
        buyPts = core.findBuyPoints(curBis, upperBis, macdArr, core.intervalSecOf(res))
        sellPts = core.findSellPoints(curBis, upperBis, macdArr, core.intervalSecOf(res))

        # 一买锚定（除日线外）
        anchoredBuyPts = buyPts
        firstBuyPts = [p for p in buyPts if p["type"] == "1买"]
        if len(firstBuyPts) > 0 and pi > 0:
            anchoredMarks = []
            seenMarkPos = set()
            for cand in firstBuyPts:
                anchored = core.anchorFirstBuy(cand, upperBis)
                if anchored is not None:
                    snapTime = core.snapToOwnBar(anchored["price"], anchored["time"], rawBars)
                    if snapTime in seenMarkPos:
                        continue
                    seenMarkPos.add(snapTime)
                    anchoredMarks.append({"type": "1买", "time": snapTime, "price": anchored["price"]})
            anchoredBuyPts = [p for p in buyPts if p["type"] != "1买"] + anchoredMarks

        marks = []
        offset = max(atr * 0.5, 0.05)
        for p in anchoredBuyPts:
            t = core.snapToOwnBar(p["price"], p["time"], rawBars)
            marks.append({"label": p["type"], "time": t, "price": p["price"] - offset, "rawTime": p["time"], "rawPrice": p["price"]})
        for p in sellPts:
            t = core.snapToOwnBar(p["price"], p["time"], rawBars)
            marks.append({"label": p["type"], "time": t, "price": p["price"] + offset, "rawTime": p["time"], "rawPrice": p["price"]})

        # 跨周期共振
        upperRes = PERIODS[pi - 1] if pi > 0 else None
        upperMarks = periodMarks.get(upperRes) if upperRes else None
        price_tol = max(atr * 0.2, 0.05)
        for mk in marks:
            mk["color"] = RED
            if (mk["label"] == "1买" or mk["label"] == "1卖") and upperMarks:
                for um in upperMarks:
                    if um["label"] not in UPPER_CLASS:
                        continue
                    if abs(mk["rawTime"] - um["rawTime"]) <= 60 and abs(mk["rawPrice"] - um["rawPrice"]) <= price_tol:
                        mk["color"] = GREEN
                        break

        buySellByRes[res] = {"buyPts": anchoredBuyPts, "sellPts": sellPts}
        marksByRes[res] = marks
        periodMarks[res] = marks
        upperBis = curBis
    return buySellByRes, marksByRes


def compare_bi(a, b):
    """比较单笔。返回差异描述列表（空表示一致）。"""
    diffs = []
    keys = ["type", "startTime", "endTime", "startPrice", "endPrice", "span", "rawCount", "gapLocked", "macdCross"]
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if k == "type":
            if va != vb:
                diffs.append(f"{k}: {va} != {vb}")
        elif k == "rawCount" or isinstance(va, bool) or isinstance(vb, bool):
            if va != vb:
                diffs.append(f"{k}: {va} != {vb}")
        else:
            if not approx(va, vb):
                diffs.append(f"{k}: {va} != {vb}")
    return diffs


def compare_point(a, b):
    """比较单个买卖点。"""
    diffs = []
    if a["type"] != b["type"]:
        diffs.append(f"type: {a['type']} != {b['type']}")
    if not approx(a["time"], b["time"]):
        diffs.append(f"time: {a['time']} != {b['time']}")
    if not approx(a["price"], b["price"]):
        diffs.append(f"price: {a['price']} != {b['price']}")
    return diffs


def compare_mark(a, b):
    """比较单个标记（含颜色）。"""
    diffs = []
    for k in ("label", "color"):
        if a.get(k) != b.get(k):
            diffs.append(f"{k}: {a.get(k)} != {b.get(k)}")
    for k in ("time", "price", "rawTime", "rawPrice"):
        if not approx(a.get(k), b.get(k)):
            diffs.append(f"{k}: {a.get(k)} != {b.get(k)}")
    return diffs


def main():
    with open(BASE, "r", encoding="utf-8") as f:
        base = json.load(f)

    fromTs = base["fromTs"]
    rawBarsByRes = base["rawBars"]
    refBarsByRes = base["refBars"]
    bisRef = base["bis"]
    buySellRef = base["buySell"]
    marksRef = base["marks"]

    pyBis = compute_bis(rawBarsByRes, refBarsByRes)
    pyBuySell, pyMarks = compute_buysell(pyBis, rawBarsByRes, fromTs)

    total_bi_diff = 0
    total_pt_diff = 0
    total_mark_diff = 0

    # ---- 对比笔 ----
    print("===== 笔对比 =====")
    for res in PERIODS:
        a = bisRef[res]
        b = pyBis[res]
        n_diff = 0
        if len(a) != len(b):
            print(f"[{res}] 笔数量不一致: JS={len(a)} PY={len(b)}")
            n_diff += abs(len(a) - len(b))
        for i in range(min(len(a), len(b))):
            d = compare_bi(a[i], b[i])
            if d:
                n_diff += 1
                if n_diff <= 5:
                    print(f"[{res}] 笔#{i} 差异: {d} | JS={a[i]}")
        if n_diff == 0:
            print(f"[{res}] 笔完全一致 ({len(a)} 笔)")
        total_bi_diff += n_diff

    # ---- 对比买卖点 ----
    print("\n===== 买卖点对比 =====")
    for res in PERIODS:
        aB = buySellRef[res]["buyPts"]
        bB = pyBuySell[res]["buyPts"]
        aS = buySellRef[res]["sellPts"]
        bS = pyBuySell[res]["sellPts"]
        n_diff = 0
        if len(aB) != len(bB):
            print(f"[{res}] 买点数量不一致: JS={len(aB)} PY={len(bB)}")
            n_diff += abs(len(aB) - len(bB))
        if len(aS) != len(bS):
            print(f"[{res}] 卖点数量不一致: JS={len(aS)} PY={len(bS)}")
            n_diff += abs(len(aS) - len(bS))
        for i in range(min(len(aB), len(bB))):
            d = compare_point(aB[i], bB[i])
            if d:
                n_diff += 1
                if n_diff <= 5:
                    print(f"[{res}] 买点#{i} 差异: {d} | JS={aB[i]}")
        for i in range(min(len(aS), len(bS))):
            d = compare_point(aS[i], bS[i])
            if d:
                n_diff += 1
                if n_diff <= 5:
                    print(f"[{res}] 卖点#{i} 差异: {d} | JS={aS[i]}")
        if n_diff == 0:
            print(f"[{res}] 买卖点完全一致 (买{len(aB)}/卖{len(aS)})")
        total_pt_diff += n_diff

    # ---- 对比标记（含共振颜色） ----
    print("\n===== 标记对比（含共振颜色） =====")
    for res in PERIODS:
        a = marksRef[res]
        b = pyMarks[res]
        n_diff = 0
        if len(a) != len(b):
            print(f"[{res}] 标记数量不一致: JS={len(a)} PY={len(b)}")
            n_diff += abs(len(a) - len(b))
        for i in range(min(len(a), len(b))):
            d = compare_mark(a[i], b[i])
            if d:
                n_diff += 1
                if n_diff <= 5:
                    print(f"[{res}] 标记#{i} 差异: {d} | JS={a[i]}")
        green_js = sum(1 for m in a if m.get("color") == "#089981")
        green_py = sum(1 for m in b if m.get("color") == "#089981")
        if n_diff == 0:
            print(f"[{res}] 标记完全一致 ({len(a)} 个, 绿色 {green_js} 个)")
        else:
            print(f"[{res}] 绿色数 JS={green_js} PY={green_py}")
        total_mark_diff += n_diff

    print("\n===== 汇总 =====")
    print(f"笔差异总数: {total_bi_diff}")
    print(f"买卖点差异总数: {total_pt_diff}")
    print(f"标记差异总数: {total_mark_diff}")
    if total_bi_diff == 0 and total_pt_diff == 0 and total_mark_diff == 0:
        print("✅ 对齐通过：Python 与 JS 输出完全一致（含共振颜色）")
    else:
        print("❌ 存在差异，需修正 chan_core.py")


if __name__ == "__main__":
    main()
