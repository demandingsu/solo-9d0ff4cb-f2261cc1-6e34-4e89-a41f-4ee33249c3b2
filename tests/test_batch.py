"""分批续算测试：计数、续算状态、下界与一次算完结果一致性。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.solver import Solver
from tests.factories import cell, link, node, panel, request

client = TestClient(app)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def _start(payload: dict, max_attempts: int = 1) -> dict:
    resp = client.post(
        "/api/batch/start", json={**payload, "max_attempts": max_attempts}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _resume(search_id: str, max_attempts: int = 1) -> dict:
    resp = client.post(
        f"/api/batch/{search_id}", json={"max_attempts": max_attempts}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _drain(payload: dict, max_attempts: int = 1) -> list[dict]:
    """每批只试 max_attempts 个选择，直到搜索结束；返回每批响应。"""
    body = _start(payload, max_attempts)
    batches = [body]
    while body["status"] == "searching":
        body = _resume(body["search_id"], max_attempts)
        batches.append(body)
    return batches


class TestBatchAccounting:
    def test_each_batch_attempts_within_limit_and_total_matches(self) -> None:
        payload = _load("feasible_min_cost")
        batches = _drain(payload, max_attempts=2)
        for b in batches:
            assert 0 <= b["attempts_used"] <= 2
        # 累计次数等于各批之和。
        assert sum(b["attempts_used"] for b in batches) == batches[-1][
            "attempted_total"
        ]
        # 同一批号连续递增。
        assert [b["batch_index"] for b in batches] == list(
            range(1, len(batches) + 1)
        )

    def test_pruned_choices_are_counted(self) -> None:
        # 不可行样例：所有候选最终都被剪枝，但每试放一次都要计数。
        payload = _load("unreachable_point")
        batches = _drain(payload, max_attempts=1)
        total = batches[-1]["attempted_total"]
        assert total >= 1
        # 每批只试放 1 次，累计次数等于批数。
        assert total == len(batches)
        assert batches[-1]["status"] == "infeasible"

    def test_each_candidate_tried_once_across_batches(self) -> None:
        """同一层的同一候选不会因分批停下而被重复尝试。

        注：回溯进入另一前缀时会在新层对象里重新试该格候选，那是不同分支，
        不算重复；这里按层对象追踪每个候选下标只走一次、且单调递增。
        """
        from app.models import SolveRequest

        payload = _load("feasible_min_cost")
        solver = Solver(SolveRequest(**payload))
        seen_per_frame: dict[int, list[int]] = {}
        kept: list[object] = []  # 持有引用，避免 id 复用
        original = solver._try_next_option

        def spy() -> None:
            frame = solver._stack[-1]
            if id(frame) not in seen_per_frame:
                seen_per_frame[id(frame)] = []
                kept.append(frame)
            seen_per_frame[id(frame)].append(frame.next_option)
            original()

        solver._try_next_option = spy  # type: ignore[method-assign]
        while not solver.done:
            solver.run_batch(1)
        for options in seen_per_frame.values():
            assert len(options) == len(set(options)), options
            assert options == sorted(options)
        assert sum(len(v) for v in seen_per_frame.values()) == solver.total_attempts

    def test_total_attempts_independent_of_batch_size(self) -> None:
        payload = _load("feasible_min_cost")
        totals = set()
        for size in (1, 2, 3, 10_000):
            solution = Solver(
                __import__("app.models", fromlist=["SolveRequest"]).SolveRequest(
                    **payload
                )
            )
            while not solution.done:
                solution.run_batch(size)
            totals.add(solution.total_attempts)
            assert solution.best_cost == 120 + 100 + 130
        assert len(totals) == 1, totals

    def test_resume_after_finish_uses_zero_attempts(self) -> None:
        body = _start(_load("feasible_min_cost"), max_attempts=10_000)
        assert body["status"] == "optimal"
        again = _resume(body["search_id"], 5)
        assert again["attempts_used"] == 0
        assert again["status"] == "optimal"
        assert again["attempted_total"] == body["attempted_total"]


class TestBatchStatus:
    def test_feasible_example_reaches_optimal_with_min_cost(self) -> None:
        payload = _load("feasible_min_cost")
        one_shot = client.post("/api/solve", json=payload).json()
        batches = _drain(payload, max_attempts=1)
        final = batches[-1]
        assert final["status"] == "optimal"
        assert final["pending_branches"] == 0
        assert final["cost_lower_bound"] is None
        assert final["current_min_cost"] == one_shot["total_cost"]
        assert final["best_plan"]["total_cost"] == one_shot["total_cost"]
        # 用板方案逐格一致。
        got = {(p["row"], p["col"]): p["panel_id"] for p in final["best_plan"]["plan"]}
        want = {(p["row"], p["col"]): p["panel_id"] for p in one_shot["plan"]}
        assert got == want

    def test_unreachable_example_reaches_infeasible(self) -> None:
        payload = _load("unreachable_point")
        one_shot = client.post("/api/solve", json=payload).json()
        final = _drain(payload, max_attempts=1)[-1]
        assert final["status"] == "infeasible"
        assert final["current_min_cost"] is None
        assert final["best_plan"] is None
        assert final["cost_lower_bound"] is None
        assert final["reason"] == one_shot["reason"]

    def test_searching_reports_bound_not_lower_than_known_optimum(self) -> None:
        payload = _load("feasible_min_cost")
        one_shot = client.post("/api/solve", json=payload).json()["total_cost"]
        body = _start(payload, max_attempts=1)
        saw_searching = False
        while body["status"] == "searching":
            saw_searching = True
            assert body["pending_branches"] > 0
            assert body["cost_lower_bound"] is not None
            # 尚未找到任何方案时，全部解都在待查分支里，
            # 故待查分支的最低下界不会高于真实最低价。
            if body["current_min_cost"] is None:
                assert body["cost_lower_bound"] <= one_shot + 1e-9
            body = _resume(body["search_id"], 1)
        assert saw_searching, "1 次一批应至少经历一个 searching 批次"
        assert body["status"] == "optimal"

    def test_lower_bound_includes_next_panel_price(self) -> None:
        """最早批次的下界至少含下一块待试板的价格。"""
        payload = _load("feasible_min_cost")
        body = _start(payload, max_attempts=1)
        # 候选按 id 排序，左上格先试 P-TL-PREMIUM（180）；
        # 仍在待查的最廉价分支是 P-TL-STD 一条，下界应含其 120：
        # 120 + P-TR 100 + P-BL(旧) 0 + P-BR 130。
        assert body["attempts_used"] == 1
        assert body["current_min_cost"] is None
        assert body["cost_lower_bound"] == 120 + 100 + 0 + 130

    def test_lower_bound_is_none_once_finished(self) -> None:
        for name in ("feasible_min_cost", "unreachable_point", "seam_mismatch"):
            final = _drain(_load(name), max_attempts=1)[-1]
            assert final["status"] in ("optimal", "infeasible")
            assert final["cost_lower_bound"] is None
            assert final["pending_branches"] == 0


class TestBatchValidation:
    def test_unknown_search_id_404(self) -> None:
        resp = client.post("/api/batch/deadbeef", json={"max_attempts": 1})
        assert resp.status_code == 404
        assert "不存在" in resp.text

    def test_zero_attempts_rejected(self) -> None:
        payload = {**_load("feasible_min_cost"), "max_attempts": 0}
        resp = client.post("/api/batch/start", json=payload)
        assert resp.status_code == 422

    def test_original_validation_still_applies(self) -> None:
        payload = dict(_load("feasible_min_cost"))
        payload["max_attempts"] = 1
        payload["rows"] = 4
        resp = client.post("/api/batch/start", json=payload)
        assert resp.status_code == 422


class TestBatchParity:
    """分批续算与一次算完在多种输入上的最优价/无解结论必须一致。"""

    def _randomish_request(self, spec: int):
        top = panel(
            "TOP",
            [60, 100][spec % 2],
            [
                node("t", 0.5, 0.0, connector=True),
                node("b", 0.5, 1.0, connector=True),
                node("p", 0.5, 0.5),
            ],
            [link("t", "p"), link("p", "b")],
        )
        alt = panel(
            "ALT",
            [45, 80][spec % 2],
            [node("t", 0.5, 0.0, connector=True), node("b", 0.5, 1.0, connector=True)],
            [link("t", "b")],
        )
        bot = panel(
            "BOT",
            [30, 70][spec % 2],
            [
                node("t", 0.5, 0.0, connector=True),
                node("p", 0.5, 0.5),
                node("r", 1.0, 0.5, connector=True),
            ],
            [link("t", "p")],
            point_ids=["p"],
        )
        bot_bad = panel(
            "BOTBAD",
            10,
            [node("t", 0.5, 0.0, connector=True), node("p", 0.5, 0.5)],
            [],
            point_ids=["p"],
        )
        h = panel(
            "H",
            40,
            [node("l", 0.0, 0.5, connector=True), node("b", 0.5, 1.0, connector=True)],
            [link("l", "b")],
        )
        blank = panel("BLANK", 5, [node("p", 0.5, 0.5)], point_ids=["p"])
        return request(
            2,
            2,
            {"TOP": top, "ALT": alt, "BOT": bot, "BOTBAD": bot_bad,
             "H": h, "BLANK": blank},
            [
                cell(0, 0, True, ["TOP", "ALT"]),
                cell(0, 1, True, ["H"]),
                (
                    cell(1, 0, False, ["BOT"], old_panel="BLANK")
                    if spec % 3 != 0
                    else cell(1, 0, True, ["BOT"])
                ),
                cell(1, 1, True, ["BOT", "BOTBAD"]),
            ],
            entrance=(0.5, 0),
            must_reach=[(1.5, 1.5)],
        )

    def test_parity_on_several_inputs(self) -> None:
        for spec in range(6):
            for batch_size in (1, 2, 3):
                req = self._randomish_request(spec)
                payload = req.model_dump()
                one_shot = client.post("/api/solve", json=payload).json()
                batches = _drain(payload, max_attempts=batch_size)
                final = batches[-1]
                if one_shot["feasible"]:
                    assert final["status"] == "optimal", (spec, batch_size)
                    assert final["current_min_cost"] == one_shot["total_cost"], (
                        spec, batch_size
                    )
                    assert final["attempted_total"] >= 1
                else:
                    assert final["status"] == "infeasible", (spec, batch_size)
                    assert final["reason"] == one_shot["reason"]
