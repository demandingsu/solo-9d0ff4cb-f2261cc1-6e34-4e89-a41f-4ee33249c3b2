"""方案与连通性报告工具：一次算完与分批续算共用。"""

from __future__ import annotations

from .models import CellPlan, GlobalLink, GlobalNode, Position, SolveRequest
from .solver import COORD_QUANTUM, _q, _RollbackDSU


def round_pos(x: float, y: float) -> Position:
    return Position(x=round(x, COORD_QUANTUM), y=round(y, COORD_QUANTUM))


def build_plan(req: SolveRequest, choices: list[str]) -> list[CellPlan]:
    """把逐格板编号展开成带图面坐标与费用的方案。"""
    plan: list[CellPlan] = []
    for r in range(req.rows):
        for c in range(req.cols):
            idx = r * req.cols + c
            pid = choices[idx]
            cell = next(
                cell for cell in req.cells if cell.row == r and cell.col == c
            )
            panel = req.panels[pid]
            is_old = (not cell.must_replace) and pid == cell.old_panel
            nodes = [
                GlobalNode(
                    cell_row=r,
                    cell_col=c,
                    local_id=n.id,
                    global_position=round_pos(c + n.position.x, r + n.position.y),
                    is_connector=n.is_connector,
                    is_point=n.id in panel.point_ids,
                )
                for n in panel.nodes
            ]
            links = [GlobalLink(a_local_id=lk.a, b_local_id=lk.b) for lk in panel.links]
            plan.append(
                CellPlan(
                    row=r,
                    col=c,
                    panel_id=pid,
                    choice="old" if is_old else "spare",
                    price=0.0 if is_old else panel.price,
                    nodes=nodes,
                    links=links,
                    point_ids=list(panel.point_ids),
                )
            )
    return plan


def reachable_points(req: SolveRequest, choices: list[str]) -> tuple[list, list]:
    """对给定完整方案跑一次连通分析，返回可达/不可达必达展点。"""
    dsu = _RollbackDSU()
    coord_buckets: dict[tuple, list[tuple]] = {}

    for r in range(req.rows):
        for c in range(req.cols):
            panel = req.panels[choices[r * req.cols + c]]
            for n in panel.nodes:
                dsu.add((r, c, n.id))
            for lk in panel.links:
                dsu.union((r, c, lk.a), (r, c, lk.b))
            for n in panel.nodes:
                if not n.is_connector:
                    continue
                lx, ly = _q(n.position.x), _q(n.position.y)
                gkey = (_q(c + lx), _q(r + ly))
                coord_buckets.setdefault(gkey, []).append((r, c, n.id))
    for bucket in coord_buckets.values():
        first = bucket[0]
        for other in bucket[1:]:
            dsu.union(first, other)

    # 入口节点。
    ex, ey = _q(req.entrance.x), _q(req.entrance.y)
    ent_roots = {dsu.find(k) for k in coord_buckets.get((ex, ey), [])}

    reachable, unreachable = [], []
    for p in req.must_reach:
        r = min(max(int(p.y), 0), req.rows - 1)
        c = min(max(int(p.x), 0), req.cols - 1)
        lx, ly = _q(p.x - c), _q(p.y - r)
        panel = req.panels[choices[r * req.cols + c]]
        node_key = None
        for n in panel.nodes:
            if n.id in panel.point_ids and (
                _q(n.position.x), _q(n.position.y)
            ) == (lx, ly):
                node_key = (r, c, n.id)
                break
        if node_key is not None and dsu.find(node_key) in ent_roots:
            reachable.append(round_pos(p.x, p.y))
        else:
            unreachable.append(round_pos(p.x, p.y))
    return reachable, unreachable
