"""FastAPI 入口：一次算完接口、分批续算接口与 /docs 文档。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .models import (
    BatchPlan,
    BatchRequest,
    BatchResponse,
    ResumeRequest,
    SolveRequest,
    SolveResponse,
)
from .reporting import build_plan, reachable_points
from .solver import Solver

app = FastAPI(
    title="触摸导览图换板试算服务",
    version="1.1.0",
    description=(
        "录入 3 行 4 列以内的导览图、必换位置、指定入口与必达展点，"
        "在满足接缝接头逐一对齐、外缘仅留入口、入口可达全部必达展点的前提下，"
        "求更换费最低的逐格用板方案。\n\n"
        "**一次算完**：`POST /api/solve`。\n\n"
        "**分批续算**（适合边算边看）：先用原试算输入调用 "
        "`POST /api/batch/start` 发起，拿到 `search_id`；"
        "之后反复调用 `POST /api/batch/{search_id}` 续算，"
        "每次传入本批最多试放次数。被淘汰的候选也计数，"
        "每批返回实际次数、当前最低价与用板方案、待查分支的费用下界。"
        "状态区分 `searching`（仍在试算）、`optimal`（已证最低价）、"
        "`infeasible`（已证无解）。\n\n"
        "演示每批只试一个选择：两个接口的请求示例中 `max_attempts` 均为 1，"
        "发起后把返回的 `search_id` 填入续算路径即可逐步查看。"
    ),
)

# 同一服务进程内的搜索登记表；进程重启后编号失效。
_searches: dict[str, _SearchState] = {}
_searches_lock = threading.Lock()
_SEARCH_TTL_SECONDS = 3600.0
_MAX_SEARCHES = 512


@dataclass
class _SearchState:
    solver: Solver
    batch_index: int
    attempted_total: int
    last_used_at: float
    lock: threading.Lock = field(default_factory=threading.Lock)


def _prune_registry(now: float) -> None:
    stale = [
        sid
        for sid, st in _searches.items()
        if now - st.last_used_at > _SEARCH_TTL_SECONDS
    ]
    for sid in stale:
        _searches.pop(sid, None)
    if len(_searches) > _MAX_SEARCHES:
        oldest = sorted(_searches, key=lambda sid: _searches[sid].last_used_at)
        for sid in oldest[: len(_searches) - _MAX_SEARCHES]:
            _searches.pop(sid, None)


def _batch_payload(state: _SearchState, search_id: str, used: int) -> BatchResponse:
    solver = state.solver
    state.batch_index += 1
    state.attempted_total += used

    done = solver.done
    if done:
        status = "optimal" if solver.best_choices is not None else "infeasible"
    else:
        status = "searching"

    best_plan = None
    if solver.best_choices is not None:
        best_plan = BatchPlan(
            total_cost=round(float(solver.best_cost or 0.0), 2),
            plan=build_plan(solver.req, solver.best_choices),
        )

    return BatchResponse(
        search_id=search_id,
        batch_index=state.batch_index,
        status=status,
        attempts_used=used,
        pending_branches=0 if done else solver.pending_branches,
        attempted_total=state.attempted_total,
        current_min_cost=(
            round(float(solver.best_cost), 2)
            if solver.best_cost is not None
            else None
        ),
        best_plan=best_plan,
        cost_lower_bound=None if done else solver.lower_bound(),
        reason=(
            solver.result()["reason"]
            if done and solver.best_choices is None
            else None
        ),
        entrance=solver.req.entrance,
        must_reach=list(solver.req.must_reach),
    )


def _get_state(search_id: str) -> _SearchState:
    with _searches_lock:
        _prune_registry(time.monotonic())
        state = _searches.get(search_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"搜索编号 {search_id} 不存在或已过期；"
                "请重新调用 /api/batch/start 发起（编号仅在当前服务进程内有效）"
            ),
        )
    return state


def _advance(state: _SearchState, search_id: str, max_attempts: int) -> BatchResponse:
    """在锁内推进一批，避免同一编号的并发续算把栈状态打乱。

    锁按搜索编号分配，不同搜索可并行推进。
    """
    with state.lock:
        used = state.solver.run_batch(max_attempts)
        state.last_used_at = time.monotonic()
        return _batch_payload(state, search_id, used)


@app.get("/health", tags=["系统"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/solve",
    response_model=SolveResponse,
    tags=["换板试算"],
    summary="一次算完：求最低价方案",
)
def solve(req: SolveRequest) -> Any:
    solver = Solver(req)
    result = solver.solve()

    if not result["feasible"]:
        reach, unreach = [], []
        # 若存在走到叶节点的完整组合（如仅因展点不可达而失败），
        # 回显其连通分析，便于前端在图面上标注。
        if result.get("last_leaf_choices") is not None:
            reach, unreach = reachable_points(req, result["last_leaf_choices"])
        return SolveResponse(
            feasible=False,
            reason=result["reason"],
            entrance=req.entrance,
            must_reach=list(req.must_reach),
            reachable_points=reach,
            unreachable_points=unreach,
            diagnostics=result["stats"],
        )

    choices = result["choices"]
    reach, unreach = reachable_points(req, choices)
    return SolveResponse(
        feasible=True,
        total_cost=round(float(result["total_cost"]), 2),
        entrance=req.entrance,
        must_reach=list(req.must_reach),
        reachable_points=reach,
        unreachable_points=unreach,
        plan=build_plan(req, choices),
        diagnostics=result["stats"],
    )


@app.post(
    "/api/batch/start",
    response_model=BatchResponse,
    tags=["分批续算"],
    summary="发起分批搜索（同时跑完第一批）",
    description=(
        "请求体为原试算输入再加 `max_attempts`（本批最多试放次数）。"
        "返回 `search_id`，后续用 `POST /api/batch/{search_id}` 续算；"
        "每个候选分支只会在某一批里试一次。"
        "示例中 `max_attempts=1`，即发起批也只试一个选择。"
    ),
)
def batch_start(req: BatchRequest) -> BatchResponse:
    solver = Solver(req)
    search_id = uuid.uuid4().hex
    state = _SearchState(
        solver=solver,
        batch_index=0,
        attempted_total=0,
        last_used_at=time.monotonic(),
    )
    with _searches_lock:
        _prune_registry(state.last_used_at)
        _searches[search_id] = state
    return _advance(state, search_id, req.max_attempts)


@app.post(
    "/api/batch/{search_id}",
    response_model=BatchResponse,
    tags=["分批续算"],
    summary="凭编号续算一批",
    description=(
        "沿用发起时的试算输入，从上次停下的位置继续；"
        "已经试过的候选不会重试。每批最多试放 `max_attempts` 次，"
        "返回本批实际次数。示例中 `max_attempts=1`，即每批只试一个选择。"
    ),
)
def batch_resume(search_id: str, req: ResumeRequest) -> BatchResponse:
    state = _get_state(search_id)
    return _advance(state, search_id, req.max_attempts)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception) -> JSONResponse:
    """兜底，保证异常时返回结构化 JSON 而不是裸 500。"""
    return JSONResponse(
        status_code=500,
        content={"feasible": False, "reason": f"服务器内部错误: {exc!r}"},
    )
