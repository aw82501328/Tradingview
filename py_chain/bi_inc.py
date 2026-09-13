# -*- coding: utf-8 -*-
"""30S 等大周期数据的增量笔构建器（与引擎批量口径一致，尾部续算）。

动机：深度回补后 30S 可达 10^5~10^6 根K线，分型尾部每次变化都全量重跑
buildBi+fixBiExtremes 是 O(全窗口)，随回测窗口平方级放大。本构建器把
阶段一（同型合并）/阶段二（回溯替换）改为尾部续算：

- 阶段一：seq 与各元素的分型 run 起始索引增量维护。分型只在尾部变化
  （updateFractalsTail 只动 mergedIdx ≥ n-2 的元素），从「首个受影响的
  run」起重折叠（确定性折叠 → 前缀结果不变）；
- 阶段二：结果用不可变栈 (elem, prev, depth) 表示（chan_core._stkCons），
  每个 seq 位置留栈头快照——旧节点永不改动，快照天然有效；分型变化后从
  受影响位置的前一快照重放 biStep（规则体与 buildBi 共用，单一算法源）；
- 阶段三+端点修正：结果栈与上次构建共享前缀节点（不可变），bis 前缀按
  引用复用，仅重建尾部 _TAIL 个新对；fixBiExtremes 只作用于尾部新笔
  （含边界前一笔的复制重修正）。跳空判定用逐对差值表（gapDiffs）只扫
  查询区间，原始K线数用前缀和（rawCounter）O(1) 查询。

口径说明：已冻结前缀上的历史跳空判定沿用**冻结时的 ATR**（批量口径每次
用最新 ATR 重判全部分型），极罕见情况（跳空幅度恰在阈值附近且 ATR 已漂移）
下个别笔边界与全量重建不同——与引擎已有的增量 wick 漂移同类，重同步
（BacktestEngine._resync_bis → invalidate + 批量重建）后严格归零。
"""

from .chan_core import (BiBuildCtx, biSeqStep, biStep, biPair, fixBiExtremes,
                        _stkLen, biListFromHead, countRaw)

# 尾部重建的笔数裕量：覆盖阶段二最深 3 层回看 + 单次 update 的热区分型数。
# 单次 _advance_cut（fine=3m）约并入 6 根 30S bar → 热区 seq 元素 ≤ ~8，
# 结果净增长 ≤ 8 ≪ _TAIL-3；更大批量（监控补数据）由 lens 边界动态收紧。
_TAIL = 24


class BiIncBuilder:
    """单周期增量笔构建器。用法：每次分型尾部变化时 update(...)，返回当前笔列表
    （同一列表对象原地更新尾部；消费方跨调用持有的旧 dict 不被改动——尾部重建
    恒为新 dict，冻结前缀按引用共享且不再修改）。resync 后 invalidate()。"""

    def __init__(self, res):
        self.res = str(res)
        self._stale = True
        self._fracs = None      # 分型列表对象（重同步会整体换对象 → 身份失效）
        self._merged = None     # 合并块列表对象（同上；_mergeStep 原地追加）
        self._seq = []          # 阶段一序列（分型 dict 引用）
        self._seq_start = []    # seq[i] 的 run 起始分型索引（run=同型分型段）
        self._heads = []        # heads[i] = 处理完 seq[:i+1] 的不可变栈头
        self._lens = []         # lens[i] = heads[i] 深度（冻结边界判定用）
        self._bis = []          # 缓存笔列表（尾部原地拼接；前缀 dict 按引用共享）
        self._gap_diffs = []    # 相邻块对 p → (nextLow-curHigh, curLow-nextHigh)
        self._gap_final = 0     # [0:_gap_final) 已固化（块 p+1 不再是末块）
        self._cum = [0]         # cum[i] = blocks[:i] 的 _rawCount 和
        self._cum_final = 1     # cum 有效下标数（0.._cum_final-1）

    # ---------------- 对外接口 ----------------

    def invalidate(self):
        """批量重同步后调用：下一次 update 走全量重建（严格等于 batch 口径）。"""
        self._stale = True

    def update(self, fractals, merged, macd, atr):
        """分型尾部变化后重建笔列表；返回笔列表（self._bis，同一对象）。"""
        if self._stale or fractals is not self._fracs or merged is not self._merged:
            return self._full_rebuild(fractals, merged, macd, atr)
        self._sync_gap_cum(len(merged))
        if len(fractals) < 2:
            self._bis = []
            return self._bis
        # 1) 分型热区：updateFractalsTail 只保留/新增 mergedIdx ≥ n-2 的尾部元素，
        #    d = 首个热区分型索引（此前的前缀 dict 与索引均已冻结）
        n2 = len(merged) - 2
        d = len(fractals)
        while d > 0 and fractals[d - 1]["mergedIdx"] >= n2:
            d -= 1
        # 2) 阶段一尾部重折叠：丢弃 run 起点在热区的 seq 元素；run 可能跨越 d 的
        #    末元素（起点 < d）一并重折叠（确定性折叠，重算结果与首折一致）
        k = len(self._seq_start)
        while k > 0 and self._seq_start[k - 1] >= d:
            k -= 1
        if k > 0:
            k -= 1
        fold_from = self._seq_start[k] if k < len(self._seq_start) else d
        del self._seq[k:]
        del self._seq_start[k:]
        for fi in range(fold_from, len(fractals)):
            f = fractals[fi]
            before = len(self._seq)
            biSeqStep(self._seq, f)
            if len(self._seq) > before:
                self._seq_start.append(fi)
        # 3) 阶段二重放：从受影响位置的前一快照起 biStep（规则体与批量共用）
        del self._heads[k:]
        del self._lens[k:]
        head = self._heads[k - 1] if k > 0 else None
        ctx = BiBuildCtx(merged, atr, macd, fractals=fractals,
                         gapDiffs=self._gap_diffs, rawCounter=self.count_raw)
        for kk in self._seq[k:]:
            head = biStep(ctx, head, kk)
            self._heads.append(head)
            self._lens.append(_stkLen(head))
        # 4) 阶段三 + 端点修正：冻结前缀按引用复用，仅重建尾部
        self._splice_bis(head, merged, ctx, k)
        return self._bis

    # ---------------- 内部 ----------------

    def count_raw(self, a, b):
        """(a, b] 区间原始K线数；前缀和 O(1)，涉及未固化末块时回退逐块累加。"""
        if b + 1 < self._cum_final:
            return self._cum[b + 1] - self._cum[a + 1]
        return countRaw(self._merged, a, b)

    def _full_rebuild(self, fractals, merged, macd, atr):
        self._fracs = fractals
        self._merged = merged
        self._gap_diffs = []
        self._gap_final = 0
        self._cum = [0]
        self._cum_final = 1
        self._sync_gap_cum(len(merged))
        self._seq = []
        self._seq_start = []
        self._heads = []
        self._lens = []
        for fi, f in enumerate(fractals):
            before = len(self._seq)
            biSeqStep(self._seq, f)
            if len(self._seq) > before:
                self._seq_start.append(fi)
        ctx = BiBuildCtx(merged, atr, macd, fractals=fractals,
                         gapDiffs=self._gap_diffs, rawCounter=self.count_raw)
        head = None
        for kk in self._seq:
            head = biStep(ctx, head, kk)
            self._heads.append(head)
            self._lens.append(_stkLen(head))
        result = biListFromHead(head)
        self._bis = [biPair(result[i], result[i + 1], merged, ctx)
                     for i in range(len(result) - 1)]
        fixBiExtremes(self._bis, merged, count_raw=self.count_raw)
        self._stale = False
        return self._bis

    def _sync_gap_cum(self, m, force=False):
        """同步逐对跳空差值与 _rawCount 前缀和。块 p 一旦不是末块（p ≤ m-2）即固化；
        末对 (m-2, m-1) 与末块计数随末块包含合并变化，每次重算（临时值）。"""
        merged = self._merged
        for p in range(self._gap_final, max(self._gap_final, m - 2)):
            a, b = merged[p], merged[p + 1]
            up = b.get("rawLow", b["low"]) - a.get("rawHigh", a["high"])
            dn = a.get("rawLow", a["low"]) - b.get("rawHigh", b["high"])
            if p < len(self._gap_diffs):
                self._gap_diffs[p] = (up, dn)
            else:
                self._gap_diffs.append((up, dn))
        self._gap_final = max(self._gap_final, m - 2 if m >= 2 else 0)
        if m >= 2:
            p = m - 2
            a, b = merged[p], merged[p + 1]
            up = b.get("rawLow", b["low"]) - a.get("rawHigh", a["high"])
            dn = a.get("rawLow", a["low"]) - b.get("rawHigh", b["high"])
            if p < len(self._gap_diffs):
                self._gap_diffs[p] = (up, dn)
            else:
                self._gap_diffs.append((up, dn))
        # cum 有效到 m-1（cum[m-1] 含块 m-2，已固化）
        for i in range(self._cum_final, m):
            self._cum.append(self._cum[-1] + merged[i - 1]["_rawCount"])
        self._cum_final = max(self._cum_final, m)

    def _splice_bis(self, head, merged, ctx, k):
        """阶段三尾部拼接 + 端点修正（只作用于尾部；冻结前缀按引用复用）。

        fixBiExtremes 从左到右：fix(bis[j]) 读 bis[j+1] 的**原始** endIdx 定扫描上界，
        命中时同步改写 bis[j+1] 的起点。因此重建区从 keep_bis-2 起全部用**新鲜原始
        biPair**（保证前驱 fix 重放时以原始值为输入、确定性再触发起点改写）；重建区
        首笔的起点从缓存回填（保留更早一轮 fix(bis[keep_bis-3]) 已写入的改写——其
        输入节点已冻结，值稳定）。"""
        total = _stkLen(head)
        if total < 2:
            self._bis = []
            return
        # 冻结边界：规则最深回看 3 层 → 重放起点 k 之前 lens-3 深度的节点不可再变；
        # 同时保留 _TAIL 裕量的尾部重建区
        boundary = (self._lens[k - 1] - 3) if k > 0 else 0
        keep_nodes = max(0, min(total - _TAIL, boundary))
        # 收集尾部节点（0 起算的第 keep_nodes-2..total-1 个；_stkCons 深度从 1 起算）
        tail = []
        nd = head
        while nd is not None and nd[2] > keep_nodes - 2:
            tail.append(nd[0])
            nd = nd[1]
        tail.reverse()
        raw_pairs = []
        if nd is not None and tail:
            raw_pairs.append((nd[0], tail[0]))       # 位置 keep_bis-2（接缝首笔）
        for i in range(len(tail) - 1):
            raw_pairs.append((tail[i], tail[i + 1]))
        region = [biPair(a, b, merged, ctx) for a, b in raw_pairs]
        seam = max(0, keep_nodes - 3)
        if seam < len(self._bis) and region:
            # 回填接缝首笔起点（含 fix(bis[seam-1]) 的历史改写），其余字段保持原始值
            old = self._bis[seam]
            cur = region[0]
            for f in ("startIdx", "startTime", "startPrice"):
                cur[f] = old[f]
            cur["rawCount"] = ctx.countRawBetween(cur["startIdx"], cur["endIdx"])
            cur["span"] = (cur["endPrice"] - cur["startPrice"]) if cur["type"] == "up" \
                else (cur["startPrice"] - cur["endPrice"])
        fixBiExtremes(region, merged, count_raw=self.count_raw)
        self._bis[seam:] = region
