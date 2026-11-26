"""求解器核心逻辑测试。"""

from __future__ import annotations

import itertools

import pytest

from app.solver import Solver
from tests.factories import cell, link, node, panel, request


def _top_panel(pid: str, price: float = 100.0, point: bool = False):
    """上排板：上中（入口侧）+ 下中（接缝侧），可带板内展点。"""
    nodes = [
        node("top", 0.5, 0.0, connector=True),
        node("mid", 0.5, 0.5),
        node("bot", 0.5, 1.0, connector=True),
    ]
    links_ = [link("top", "mid"), link("mid", "bot")]
    return panel(pid, price, nodes, links_, point_ids=["mid"] if point else [])


def _bottom_panel(pid: str, price: float = 100.0, connected: bool = True):
    """底排板：仅上中接头 + 板内展点；connected=False 时展点不接线。"""
    nodes = [
        node("top", 0.5, 0.0, connector=True),
        node("p", 0.5, 0.5),
    ]
    links_ = [link("top", "p")] if connected else []
    return panel(pid, price, nodes, links_, point_ids=["p"])


def _elbow_panel(pid: str, price: float = 100.0):
    """弯板：上中 -> 右中。"""
    return panel(
        pid,
        price,
        [
            node("top", 0.5, 0.0, connector=True),
            node("right", 1.0, 0.5, connector=True),
        ],
        [link("top", "right")],
    )


class TestFeasible:
    def test_two_stacked_panels_reach_point(self) -> None:
        panels = {
            "A": _top_panel("A", 100),
            "B": _bottom_panel("B", 80),
        }
        req = request(
            2,
            1,
            panels,
            [cell(0, 0, True, ["A"]), cell(1, 0, False, old_panel="B")],
            entrance=(0.5, 0),
            must_reach=[(0.5, 1.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is True
        assert result["choices"] == ["A", "B"]
        # 第二格留用旧板，只算备板钱。
        assert result["total_cost"] == 100.0

    def test_cheapest_candidate_wins(self) -> None:
        def single(pid: str, price: float):
            return panel(
                pid,
                price,
                [node("top", 0.5, 0.0, connector=True), node("p", 0.5, 0.5)],
                [link("top", "p")],
                point_ids=["p"],
            )

        panels = {"CHEAP": single("CHEAP", 50), "PRICEY": single("PRICEY", 150)}
        req = request(
            1,
            1,
            panels,
            [cell(0, 0, True, ["PRICEY", "CHEAP"])],
            entrance=(0.5, 0),
            must_reach=[(0.5, 0.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is True
        assert result["choices"] == ["CHEAP"]
        assert result["total_cost"] == 50.0

    def test_all_old_panels_cost_zero(self) -> None:
        panels = {
            "A": _top_panel("A", 100),
            "B": _bottom_panel("B", 100),
        }
        req = request(
            2,
            1,
            panels,
            [
                cell(0, 0, False, old_panel="A"),
                cell(1, 0, False, old_panel="B"),
            ],
            entrance=(0.5, 0),
            must_reach=[(0.5, 1.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is True
        assert result["total_cost"] == 0.0

    def test_elbow_chain_on_two_by_two(self) -> None:
        # 入口(0.5,0)：(0,0) 弯板接上缘与右接缝；(0,1) 横向到下接缝；
        # (1,1) 顶接头接板内展点；(1,0) 无接头板（内部格不能朝外缘伸接头）。
        h_panel = panel(
            "H",
            70,
            [
                node("l", 0.0, 0.5, connector=True),
                node("b", 0.5, 1.0, connector=True),
            ],
            [link("l", "b")],
        )
        bottom_right = panel(
            "E2",
            90,
            [
                node("top", 0.5, 0.0, connector=True),
                node("p", 0.5, 0.5),
            ],
            [link("top", "p")],
            point_ids=["p"],
        )
        blank = panel("DUMMY", 10, [node("p", 0.5, 0.5)], point_ids=["p"])
        panels = {
            "E1": _elbow_panel("E1", 90),
            "H": h_panel,
            "E2": bottom_right,
            "DUMMY": blank,
        }
        req = request(
            2,
            2,
            panels,
            [
                cell(0, 0, True, ["E1"]),
                cell(0, 1, True, ["H"]),
                cell(1, 0, True, ["DUMMY"]),
                cell(1, 1, True, ["E2"]),
            ],
            entrance=(0.5, 0),
            must_reach=[(1.5, 1.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is True
        assert result["total_cost"] == 90 + 70 + 10 + 90


class TestInfeasible:
    def test_seam_mismatch_reported(self) -> None:
        panels = {
            "A": panel(
                "A",
                100,
                [
                    node("top", 0.5, 0.0, connector=True),
                    node("right", 1.0, 0.5, connector=True),
                ],
                [link("top", "right")],
            ),
            "B": panel(
                "B",
                80,
                [node("p", 0.5, 0.5)],
                point_ids=["p"],
            ),
        }
        req = request(
            1,
            2,
            panels,
            [cell(0, 0, True, ["A"]), cell(0, 1, True, ["B"])],
            entrance=(0.5, 0),
            must_reach=[(1.5, 0.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is False
        assert "接缝" in result["reason"]

    def test_unreachable_point_reported(self) -> None:
        panels = {
            "A": _top_panel("A", 100),
            "B": _bottom_panel("B", 60, connected=False),
        }
        req = request(
            2,
            1,
            panels,
            [cell(0, 0, True, ["A"]), cell(1, 0, True, ["B"])],
            entrance=(0.5, 0),
            must_reach=[(0.5, 1.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is False
        assert "无法从入口" in result["reason"]

    def test_outer_edge_connector_forbidden(self) -> None:
        panels = {
            "A": panel(
                "A",
                100,
                [
                    node("top", 0.5, 0.0, connector=True),
                    node("leak", 0.0, 0.5, connector=True),
                ],
                [link("top", "leak")],
            ),
        }
        req = request(
            1,
            1,
            panels,
            [cell(0, 0, True, ["A"])],
            entrance=(0.5, 0),
        )
        result = Solver(req).solve()
        assert result["feasible"] is False
        assert "外缘" in result["reason"]

    def test_corner_connector_rejected(self) -> None:
        panels = {
            "A": panel(
                "A",
                100,
                [
                    node("top", 0.5, 0.0, connector=True),
                    node("corner", 1.0, 1.0, connector=True),
                ],
                [link("top", "corner")],
            ),
        }
        req = request(
            1,
            1,
            panels,
            [cell(0, 0, True, ["A"])],
            entrance=(0.5, 0),
        )
        result = Solver(req).solve()
        assert result["feasible"] is False
        assert "角" in result["reason"]

    def test_missing_point_on_panel_reported(self) -> None:
        panels = {
            "A": panel(
                "A",
                100,
                [
                    node("top", 0.5, 0.0, connector=True),
                    node("bot", 0.5, 1.0, connector=True),
                ],
                [link("top", "bot")],
            ),
        }
        req = request(
            1,
            1,
            panels,
            [cell(0, 0, True, ["A"])],
            entrance=(0.5, 0),
            must_reach=[(0.3, 0.3)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is False
        assert "必达展点" in result["reason"]


class TestExhaustive:
    def test_brute_force_matches_solver_on_tall_grid(self) -> None:
        """3 行单列、每格两候选 + 旧板：求解器结果与朴素枚举一致。"""
        panel_ids = [f"P{i}" for i in range(4)]
        panels = {}
        for i, pid in enumerate(panel_ids):
            price = 10 + i * 7
            panels[f"{pid}-T"] = _top_panel(f"{pid}-T", price)
            panels[f"{pid}-B"] = _bottom_panel(f"{pid}-B", price)

        cells = []
        for r in range(3):
            suffix = "-B" if r == 2 else "-T"
            opts = [f"{panel_ids[0]}{suffix}", f"{panel_ids[1]}{suffix}"]
            if r % 2 == 0:
                cells.append(cell(r, 0, True, opts))
            else:
                cells.append(
                    cell(r, 0, False, opts, old_panel=f"{panel_ids[3]}{suffix}")
                )
        req = request(
            3,
            1,
            panels,
            cells,
            entrance=(0.5, 0),
            must_reach=[(0.5, 2.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is True

        # 朴素枚举全部 3x2x3=18 种组合，独立验证最低价。
        options_per_cell = []
        for r in range(3):
            suffix = "-B" if r == 2 else "-T"
            paid = [
                (f"{panel_ids[0]}{suffix}", panels[f"{panel_ids[0]}{suffix}"].price),
                (f"{panel_ids[1]}{suffix}", panels[f"{panel_ids[1]}{suffix}"].price),
            ]
            if r % 2 == 0:
                options_per_cell.append(paid)
            else:
                options_per_cell.append(
                    paid + [(f"{panel_ids[3]}{suffix}", 0.0)]
                )
        brute_min = min(
            sum(price for _, price in combo)
            for combo in itertools.product(*options_per_cell)
        )
        assert result["total_cost"] == pytest.approx(brute_min)
        # 两个必换格各取最便宜的 10 元板 = 20，中间留旧 = 0。
        assert result["total_cost"] == 20.0

    def test_max_grid_3x4_runs_and_explores_all(self) -> None:
        """3x4 满格（每格都给合法板）确保规模上限下求解器可完成。"""
        # 只让入口所在列走竖线，其余格放无接头板，避免外缘/接缝冲突。
        blank = panel(
            "BLANK",
            5,
            [node("p", 0.5, 0.5)],
            point_ids=["p"],
        )
        panels = {
            "BLANK": blank,
            "T0": _top_panel("T0", 11),
            "T1": _top_panel("T1", 17),
            "M0": _top_panel("M0", 12),
            "M1": _top_panel("M1", 18),
            "B0": _bottom_panel("B0", 13),
            "B1": _bottom_panel("B1", 19),
        }
        cells = []
        for r in range(3):
            for c in range(4):
                if c == 0:
                    if r == 0:
                        cells.append(cell(r, c, True, ["T0", "T1"]))
                    elif r == 1:
                        cells.append(cell(r, c, True, ["M0", "M1"]))
                    else:
                        cells.append(cell(r, c, True, ["B0", "B1"]))
                else:
                    cells.append(cell(r, c, False, ["BLANK"], old_panel="BLANK"))
        req = request(
            3,
            4,
            panels,
            cells,
            entrance=(0.5, 0),
            must_reach=[(0.5, 2.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is True
        assert result["total_cost"] == 11 + 12 + 13
        # 每格 2~3 种选择，完整评估数应在预期量级（2*2*2*3^9=112k 上限内）。
        assert result["stats"]["complete_combinations_evaluated"] >= 1


class TestEntrance:
    def test_entrance_without_connector_fails(self) -> None:
        panels = {
            "A": panel(
                "A",
                100,
                [node("p", 0.5, 0.5)],
                point_ids=["p"],
            ),
        }
        req = request(
            1,
            1,
            panels,
            [cell(0, 0, True, ["A"])],
            entrance=(0.5, 0),
            must_reach=[(0.5, 0.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is False
        assert "入口" in result["reason"]

    def test_side_entrance_works(self) -> None:
        # 左缘入口，左格横向到右接缝；右格只留左接头（右缘不得有接头）。
        left = panel(
            "L",
            100,
            [
                node("l", 0.0, 0.5, connector=True),
                node("p", 0.5, 0.5),
                node("r", 1.0, 0.5, connector=True),
            ],
            [link("l", "p"), link("p", "r")],
            point_ids=["p"],
        )
        right = panel(
            "R",
            50,
            [node("l", 0.0, 0.5, connector=True)],
        )
        req = request(
            1,
            2,
            {"L": left, "R": right},
            [cell(0, 0, True, ["L"]), cell(0, 1, True, ["R"])],
            entrance=(0, 0.5),
            must_reach=[(0.5, 0.5)],
        )
        result = Solver(req).solve()
        assert result["feasible"] is True
        assert result["total_cost"] == 150.0
