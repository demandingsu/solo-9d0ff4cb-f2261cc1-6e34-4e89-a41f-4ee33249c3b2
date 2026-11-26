"""换板组合求解器。

思路
====
每格的可选板至多 3 种（2 个付费备板 + 1 个免费旧板），网格至多 3x4=12 格，
组合上限 3**12 = 531441，用深度优先回溯即可。回溯过程中做增量剪枝：

1. 接缝对齐：拼某格时，它上、左两侧的接头必须与已拼格逐一对齐
   （同一接缝位置双方都有接头，或双方都没有）。
2. 外缘约束：图外缘除指定入口外不允许出现任何接头。
3. 展点归属：该格若含必达展点，所选板必须在对应位置有展点节点。
4. 连通性：用可回滚并查集（DSU）增量合并板内连线与接缝接头，
   完整拼好后检查每个必达展点是否与入口同一连通分量。
5. 费用下界剪枝：当前累计费用已不低于已知最优解时直接剪枝。
"""

from __future__ import annotations

from typing import Optional

from .models import EPS, SolveRequest

COORD_QUANTUM = 9


def _q(v: float) -> float:
    """坐标量化，避免浮点误差让本该重合的接头对不上。"""
    return round(float(v), COORD_QUANTUM)


class _RollbackDSU:
    """支持回滚的并查集，按节点全局坐标键登记。"""

    def __init__(self) -> None:
        self.parent: dict[object, object] = {}
        self.size: dict[object, int] = {}
        self.history: list[tuple[object, object, int]] = []

    def add(self, key: object) -> None:
        if key not in self.parent:
            self.parent[key] = key
            self.size[key] = 1

    def find(self, key: object) -> object:
        # 不做路径压缩，保证回滚简单可靠；规模很小，链长可接受。
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        return root

    def union(self, a: object, b: object) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.history.append((rb, ra, self.size[ra]))
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True

    def checkpoint(self) -> int:
        return len(self.history)

    def rollback(self, cp: int) -> None:
        while len(self.history) > cp:
            child, parent, old_size = self.history.pop()
            self.parent[child] = child
            self.size[parent] = old_size


class _Reason:
    """记录剪枝原因（保留最深层，即最接近完整方案的失败原因）。"""

    def __init__(self) -> None:
        self.depth: int = -1
        self.text: Optional[str] = None

    def note(self, depth: int, text: str) -> None:
        if depth > self.depth:
            self.depth = depth
            self.text = text


class Solver:
    def __init__(self, req: SolveRequest) -> None:
        self.req = req
        self.rows, self.cols = req.rows, req.cols
        self.cell_at: dict[tuple[int, int], object] = {
            (c.row, c.col): c for c in req.cells
        }
        self.dsu = _RollbackDSU()
        self.best_cost: Optional[float] = None
        self.best_choices: Optional[list[str]] = None
        # 最后一个走到叶节点的完整组合（可能因不可达而失败），
        # 用于不可行响应里回显可达/不可达展点。
        self.last_leaf_choices: Optional[list[str]] = None
        self.reason = _Reason()
        self.stats = {
            "partial_combinations_visited": 0,
            "complete_combinations_evaluated": 0,
            "pruned_by_cost_bound": 0,
            "pruned_by_seam_mismatch": 0,
            "pruned_by_outer_edge": 0,
            "pruned_by_missing_point": 0,
            "pruned_by_unreachable": 0,
        }
        # 每个必达展点归属的格子与应匹配的局部坐标。
        self.points_by_cell: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for p in req.must_reach:
            r, c, lx, ly = self._global_to_cell(p.x, p.y)
            self.points_by_cell.setdefault((r, c), []).append(
                (_q(lx), _q(ly))
            )
        self.entrance_cell, self.entrance_local = self._entrance_target()

    # ------------------------------------------------------------------ 坐标
    def _global_to_cell(self, gx: float, gy: float) -> tuple[int, int, float, float]:
        # 坐标已在模型层保证落在图面内；int 向零取整即所在格（接缝线上归左下规则）。
        c = min(max(int(gx), 0), self.cols - 1)
        r = min(max(int(gy), 0), self.rows - 1)
        return r, c, gx - c, gy - r

    def _entrance_target(self) -> tuple[tuple[int, int], tuple[float, float]]:
        """入口落在外缘哪一格、对应哪个局部坐标。"""
        x, y = self.req.entrance.x, self.req.entrance.y
        if abs(y) <= EPS:  # 上缘
            r, c = 0, min(max(int(x), 0), self.cols - 1)
        elif abs(y - self.rows) <= EPS:  # 下缘
            r, c = self.rows - 1, min(max(int(x), 0), self.cols - 1)
        elif abs(x) <= EPS:  # 左缘
            r, c = min(max(int(y), 0), self.rows - 1), 0
        else:  # 右缘（入口合法性已在模型层校验）
            r, c = min(max(int(y), 0), self.rows - 1), self.cols - 1
        lx, ly = _q(x - c), _q(y - r)
        return (r, c), (lx, ly)

    def _node_key(self, r: int, c: int, local_id: str) -> tuple[int, int, str]:
        return (r, c, local_id)

    def _global_key(self, r: int, c: int, lx: float, ly: float) -> tuple:
        return (_q(c + lx), _q(r + ly))

    # ------------------------------------------------------------ 板级工具
    def _panel_connectors(self, panel) -> dict[tuple[float, float], str]:
        """局部坐标 -> 接头节点 id。"""
        out: dict[tuple[float, float], str] = {}
        for n in panel.nodes:
            if n.is_connector:
                out[(_q(n.position.x), _q(n.position.y))] = n.id
        return out

    def _panel_point_positions(self, panel) -> set[tuple[float, float]]:
        pts = set()
        for n in panel.nodes:
            if n.id in panel.point_ids:
                pts.add((_q(n.position.x), _q(n.position.y)))
        return pts

    def _options_for(self, cell) -> list[tuple[str, float, str]]:
        """返回 (panel_id, price, 来源)，按 id 排序保证同价时结果确定。"""
        opts: list[tuple[str, float, str]] = []
        for pid in sorted(cell.candidates):
            opts.append((pid, float(self.req.panels[pid].price), "spare"))
        if not cell.must_replace and cell.old_panel is not None:
            opts.append((cell.old_panel, 0.0, "old"))
        return opts

    # ------------------------------------------------------------ 约束检查
    def _check_points(
        self, r: int, c: int, panel
    ) -> Optional[str]:
        needed = self.points_by_cell.get((r, c), [])
        if not needed:
            return None
        have = self._panel_point_positions(panel)
        for lx, ly in needed:
            if (lx, ly) not in have:
                return (
                    f"格子({r},{c}) 的必达展点局部坐标({lx},{ly}) "
                    f"在板 {panel.id} 上不存在"
                )
        return None

    def _check_edges(
        self, r: int, c: int, panel, connectors: dict[tuple[float, float], str]
    ) -> Optional[str]:
        """外缘约束 + 与上/左已拼格的接缝逐一对齐。"""
        ent = (_q(self.req.entrance.x), _q(self.req.entrance.y))
        for (lx, ly), nid in connectors.items():
            on_top = abs(ly) <= EPS
            on_bottom = abs(ly - 1) <= EPS
            on_left = abs(lx) <= EPS
            on_right = abs(lx - 1) <= EPS
            if sum((on_top, on_bottom, on_left, on_right)) > 1:
                return (
                    f"板 {panel.id} 的接头 {nid} 位于格角部 {lx,ly}，"
                    "本服务仅支持边中接头，不支持角接头"
                )

            # 1) 图外缘：只允许指定入口那一个接头。
            outer_side = (
                (on_top and r == 0)
                or (on_bottom and r == self.rows - 1)
                or (on_left and c == 0)
                or (on_right and c == self.cols - 1)
            )
            if outer_side:
                gkey = self._global_key(r, c, lx, ly)
                if gkey != ent:
                    return (
                        f"板 {panel.id} 在格子({r},{c}) 的接头 {nid} "
                        f"位于图外缘 {gkey}，该处不是指定入口"
                    )
                # 入口接头还必须落在入口归属格的对应局部位置。
                if (r, c) != self.entrance_cell or (lx, ly) != self.entrance_local:
                    return (
                        f"板 {panel.id} 的接头 {nid} 坐标与指定入口不一致"
                    )

            # 2) 与上方格子的横向接缝。
            if on_top and r > 0:
                up = self._placed_connectors.get((r - 1, c))
                assert up is not None
                if (lx, _q(1.0)) not in up:
                    return (
                        f"横向接缝（行 {r - 1}/{r}，列 {c}）处，"
                        f"下侧板 {panel.id} 有接头 {nid} 而上方板无对应接头"
                    )
            # 3) 与左方格子的纵向接缝。
            if on_left and c > 0:
                left = self._placed_connectors.get((r, c - 1))
                assert left is not None
                if (_q(1.0), ly) not in left:
                    return (
                        f"纵向接缝（行 {r}，列 {c - 1}/{c}）处，"
                        f"右侧板 {panel.id} 有接头 {nid} 而左侧板无对应接头"
                    )

        # 反向检查：上/左板朝本接缝伸出的接头，本板也必须有。
        if r > 0:
            up = self._placed_connectors[(r - 1, c)]
            for lx, ly in up:
                if abs(ly - 1) <= EPS and (lx, _q(0.0)) not in connectors:
                    return (
                        f"横向接缝（行 {r - 1}/{r}，列 {c}）处，"
                        "上方板有接头而下侧板无对应接头"
                    )
        if c > 0:
            left = self._placed_connectors[(r, c - 1)]
            for lx, ly in left:
                if abs(lx - 1) <= EPS and (_q(0.0), ly) not in connectors:
                    return (
                        f"纵向接缝（行 {r}，列 {c - 1}/{c}）处，"
                        "左侧板有接头而右侧板无对应接头"
                    )
        return None

    def _place(self, r: int, c: int, panel, connectors) -> None:
        """登记节点、合并板内连线与接缝接头（调用方负责回滚）。

        所有接头（含角部）都按全局坐标登记；同一坐标下不同格的节点
        自动并为同一连通分量，这样十字接缝/角接缝也能正确贯通。
        """
        for n in panel.nodes:
            self.dsu.add(self._node_key(r, c, n.id))
        for link in panel.links:
            self.dsu.union(
                self._node_key(r, c, link.a),
                self._node_key(r, c, link.b),
            )
        for (lx, ly), nid in connectors.items():
            gkey = self._global_key(r, c, lx, ly)
            bucket = self._coord_to_node.setdefault(gkey, [])
            me = self._node_key(r, c, nid)
            for other in bucket:
                self.dsu.union(other, me)
            bucket.append(me)
        self._placed_connectors[(r, c)] = connectors

    # ------------------------------------------------------------------ 回溯
    def solve(self) -> dict:
        self._placed_connectors: dict[tuple[int, int], dict] = {}
        self._coord_to_node: dict[tuple, list] = {}
        order = [
            (r, c)
            for r in range(self.rows)
            for c in range(self.cols)
        ]
        self._dfs(order, 0, [], 0.0)

        if self.best_choices is None:
            return {
                "feasible": False,
                "reason": self.reason.text or "所有用板组合均不满足约束",
                "stats": self.stats,
                "last_leaf_choices": self.last_leaf_choices,
            }
        return {
            "feasible": True,
            "choices": self.best_choices,
            "total_cost": self.best_cost,
            "stats": self.stats,
        }

    def _dfs(
        self,
        order: list[tuple[int, int]],
        idx: int,
        chosen: list[str],
        spent: float,
    ) -> None:
        if self.best_cost is not None and spent >= self.best_cost - EPS:
            self.stats["pruned_by_cost_bound"] += 1
            return
        if idx == len(order):
            self.stats["complete_combinations_evaluated"] += 1
            self._evaluate_leaf(chosen, spent)
            return

        r, c = order[idx]
        cell = self.cell_at[(r, c)]
        self.stats["partial_combinations_visited"] += 1

        for pid, price, _src in self._options_for(cell):
            if spent + price >= (self.best_cost or float("inf")) - EPS:
                self.stats["pruned_by_cost_bound"] += 1
                continue
            panel = self.req.panels[pid]
            connectors = self._panel_connectors(panel)

            point_err = self._check_points(r, c, panel)
            if point_err:
                self.stats["pruned_by_missing_point"] += 1
                self.reason.note(idx, point_err)
                continue

            edge_err = self._check_edges(r, c, panel, connectors)
            if edge_err:
                if "外缘" in edge_err or "入口" in edge_err:
                    self.stats["pruned_by_outer_edge"] += 1
                else:
                    self.stats["pruned_by_seam_mismatch"] += 1
                self.reason.note(idx, edge_err)
                continue

            cp = self.dsu.checkpoint()
            self._place(r, c, panel, connectors)
            chosen.append(pid)
            self._dfs(order, idx + 1, chosen, spent + price)
            chosen.pop()
            del self._placed_connectors[(r, c)]
            # 清理本格登记的全部全局坐标接头（含角部）。
            for lx, ly in connectors:
                gkey = self._global_key(r, c, lx, ly)
                bucket = self._coord_to_node.get(gkey)
                if bucket is None:
                    continue
                bucket = [k for k in bucket if (k[0], k[1]) != (r, c)]
                if bucket:
                    self._coord_to_node[gkey] = bucket
                else:
                    del self._coord_to_node[gkey]
            self.dsu.rollback(cp)

    def _evaluate_leaf(self, chosen: list[str], spent: float) -> None:
        """完整方案的入口-展点连通性检查。"""
        self.last_leaf_choices = list(chosen)
        # 找到入口接头所在的 DSU 节点。
        r, c = self.entrance_cell
        lx, ly = self.entrance_local
        panel = self.req.panels[chosen[self._index(r, c)]]
        ent_node = None
        for n in panel.nodes:
            if n.is_connector and (
                _q(n.position.x), _q(n.position.y)
            ) == (lx, ly):
                ent_node = self._node_key(r, c, n.id)
                break
        if ent_node is None:
            self.stats["pruned_by_unreachable"] += 1
            self.reason.note(
                len(chosen),
                f"入口格({r},{c}) 所选板 {panel.id} 在入口位置没有接头",
            )
            return

        root = self.dsu.find(ent_node)
        for p in self.req.must_reach:
            pr, pc, plx, ply = self._global_to_cell(p.x, p.y)
            ppanel = self.req.panels[chosen[self._index(pr, pc)]]
            target = None
            for n in ppanel.nodes:
                if n.id in ppanel.point_ids and (
                    _q(n.position.x), _q(n.position.y)
                ) == (_q(plx), _q(ply)):
                    target = self._node_key(pr, pc, n.id)
                    break
            if target is None or self.dsu.find(target) != root:
                self.stats["pruned_by_unreachable"] += 1
                self.reason.note(
                    len(chosen),
                    f"必达展点({p.x},{p.y}) 无法从入口沿凸线到达",
                )
                return

        if self.best_cost is None or spent < self.best_cost - EPS:
            self.best_cost = spent
            self.best_choices = list(chosen)

    def _index(self, r: int, c: int) -> int:
        return r * self.cols + c
