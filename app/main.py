"""FastAPI 入口：换板试算接口与 /docs 文档。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .models import (
    CellPlan,
    ChosenPanel,
    GlobalLink,
    GlobalNode,
    Position,
    SolveRequest,
    SolveResponse,
)
from .solver import COORD_QUANTUM, Solver

app = FastAPI(
    title="触摸导览图换板试算服务",
    version="1.0.0",
    description=(
        "录入 3 行 4 列以内的导览图、必换位置、指定入口与必达展点，"
        "在满足接缝接头逐一对齐、外缘仅留入口、入口可达全部必达展点的前提下，"
        "求更换费最低的逐格用板方案。\n\n"
        "在本页下方 `/api/solve` 中可直接用示例请求试算。"
    ),
)


def _round_pos(x: float, y: float) -> Position:
    return Position(x=round(x, COORD_QUANTUM), y=round(y, COORD_QUANTUM))


def _build_plan(req: SolveRequest, choices: list[str]) -> list[CellPlan]:
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
                    global_position=_round_pos(c + n.position.x, r + n.position.y),
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


def _reachable_points(req: SolveRequest, choices: list[str]) -> tuple[list, list]:
    """对成功方案再跑一次连通分析，返回可达/不可达必达展点。"""
    # 复用求解器内部 DSU：重新构造并一次性放置全部板。
    from .solver import _RollbackDSU, _q  # noqa: WPS437

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
            reachable.append(_round_pos(p.x, p.y))
        else:
            unreachable.append(_round_pos(p.x, p.y))
    return reachable, unreachable


@app.get("/health", tags=["系统"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/solve", response_model=SolveResponse, tags=["换板试算"])
def solve(req: SolveRequest) -> Any:
    solver = Solver(req)
    result = solver.solve()

    if not result["feasible"]:
        reachable, unreachable = [], []
        # 若存在走到叶节点的完整组合（如仅因展点不可达而失败），
        # 回显其连通分析，便于前端在图面上标注。
        if result.get("last_leaf_choices") is not None:
            reachable, unreachable = _reachable_points(
                req, result["last_leaf_choices"]
            )
        return SolveResponse(
            feasible=False,
            reason=result["reason"],
            entrance=req.entrance,
            must_reach=list(req.must_reach),
            reachable_points=reachable,
            unreachable_points=unreachable,
            diagnostics=result["stats"],
        )

    choices = result["choices"]
    plan = _build_plan(req, choices)
    reachable, unreachable = _reachable_points(req, choices)
    return SolveResponse(
        feasible=True,
        total_cost=round(float(result["total_cost"]), 2),
        entrance=req.entrance,
        must_reach=list(req.must_reach),
        reachable_points=reachable,
        unreachable_points=unreachable,
        plan=plan,
        diagnostics=result["stats"],
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception) -> JSONResponse:
    """兜底，保证异常时返回结构化 JSON 而不是裸 500。"""
    return JSONResponse(
        status_code=500,
        content={"feasible": False, "reason": f"服务器内部错误: {exc!r}"},
    )
