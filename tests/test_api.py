"""FastAPI 接口测试：校验、端到端样例与响应结构。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _post(payload: dict) -> dict:
    resp = client.post("/api/solve", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestValidation:
    def test_grid_too_large_rejected(self) -> None:
        resp = client.post(
            "/api/solve",
            json={"rows": 4, "cols": 4, "panels": {}, "cells": [],
                  "entrance": {"x": 0, "y": 0}},
        )
        assert resp.status_code == 422

    def test_connector_inside_panel_rejected(self) -> None:
        payload = {
            "rows": 1,
            "cols": 1,
            "entrance": {"x": 0.5, "y": 0},
            "panels": {
                "A": {
                    "id": "A",
                    "price": 10,
                    "nodes": [
                        {"id": "n", "position": {"x": 0.5, "y": 0.5},
                         "is_connector": True}
                    ],
                    "links": [],
                    "point_ids": [],
                }
            },
            "cells": [
                {"row": 0, "col": 0, "must_replace": True, "candidates": ["A"]}
            ],
        }
        resp = client.post("/api/solve", json=payload)
        assert resp.status_code == 422
        assert "边缘" in resp.text

    def test_three_candidates_rejected(self) -> None:
        payload = {
            "rows": 1,
            "cols": 1,
            "entrance": {"x": 0.5, "y": 0},
            "panels": {
                pid: {
                    "id": pid,
                    "price": 1,
                    "nodes": [
                        {"id": "t", "position": {"x": 0.5, "y": 0},
                         "is_connector": True}
                    ],
                    "links": [],
                    "point_ids": [],
                }
                for pid in ("A", "B", "C")
            },
            "cells": [
                {"row": 0, "col": 0, "must_replace": True,
                 "candidates": ["A", "B", "C"]}
            ],
        }
        resp = client.post("/api/solve", json=payload)
        assert resp.status_code == 422

    def test_must_replace_with_old_panel_rejected(self) -> None:
        payload = {
            "rows": 1,
            "cols": 1,
            "entrance": {"x": 0.5, "y": 0},
            "panels": {
                "A": {
                    "id": "A",
                    "price": 1,
                    "nodes": [
                        {"id": "t", "position": {"x": 0.5, "y": 0},
                         "is_connector": True}
                    ],
                    "links": [],
                    "point_ids": [],
                }
            },
            "cells": [
                {"row": 0, "col": 0, "must_replace": True,
                 "candidates": ["A"], "old_panel": "A"}
            ],
        }
        resp = client.post("/api/solve", json=payload)
        assert resp.status_code == 422
        assert "必换" in resp.text

    def test_entrance_not_on_boundary_rejected(self) -> None:
        payload = {
            "rows": 2,
            "cols": 2,
            "entrance": {"x": 0.5, "y": 0.5},
            "panels": {"A": {"id": "A", "price": 1,
                             "nodes": [
                                 {"id": "n", "position": {"x": 0.5, "y": 0.5},
                                  "is_connector": False}
                             ],
                             "links": [], "point_ids": []}},
            "cells": [
                {"row": r, "col": c, "must_replace": False, "old_panel": "A"}
                for r in range(2) for c in range(2)
            ],
        }
        resp = client.post("/api/solve", json=payload)
        assert resp.status_code == 422
        assert "外缘" in resp.text

    def test_duplicate_cell_rejected(self) -> None:
        one_cell = {
            "row": 0, "col": 0, "must_replace": False, "old_panel": "A"
        }
        payload = {
            "rows": 1,
            "cols": 2,
            "entrance": {"x": 0.5, "y": 0},
            "panels": {"A": {"id": "A", "price": 1,
                             "nodes": [
                                 {"id": "n", "position": {"x": 0.5, "y": 0.5},
                                  "is_connector": False}
                             ],
                             "links": [], "point_ids": []}},
            "cells": [one_cell, dict(one_cell)],
        }
        resp = client.post("/api/solve", json=payload)
        assert resp.status_code == 422
        assert "重复" in resp.text


class TestExamples:
    def test_unreachable_example(self) -> None:
        payload = json.loads((EXAMPLES / "unreachable_point.json").read_text())
        body = _post(payload)
        assert body["feasible"] is False
        assert body["reason"]
        assert "无法从入口" in body["reason"]
        assert body["plan"] is None
        # 必达展点仍回显，前端可定位。
        assert len(body["must_reach"]) == 2
        # 失败的完整组合也回显连通分析：第一个展点可达、第二个不可达。
        assert body["reachable_points"] == [{"x": 0.5, "y": 1.5}]
        assert body["unreachable_points"] == [{"x": 1.5, "y": 1.5}]

    def test_seam_mismatch_example(self) -> None:
        payload = json.loads((EXAMPLES / "seam_mismatch.json").read_text())
        body = _post(payload)
        assert body["feasible"] is False
        assert "接缝" in body["reason"]

    def test_feasible_example_structure_and_cost(self) -> None:
        payload = json.loads((EXAMPLES / "feasible_min_cost.json").read_text())
        body = _post(payload)
        assert body["feasible"] is True
        assert body["total_cost"] == 120 + 100 + 130  # 左下留旧板免费
        assert len(body["plan"]) == 4
        # 最便宜的标准板胜出，而不是 PREMIUM。
        tl = next(p for p in body["plan"] if p["row"] == 0 and p["col"] == 0)
        assert tl["panel_id"] == "P-TL-STD"
        assert tl["choice"] == "spare"
        bl = next(p for p in body["plan"] if p["row"] == 1 and p["col"] == 0)
        assert bl["choice"] == "old"
        assert bl["price"] == 0.0
        # 图面坐标：左上格的上缘接头应为 (0.5, 0)。
        top_node = next(n for n in tl["nodes"] if n["local_id"] == "n1")
        assert top_node["global_position"] == {"x": 0.5, "y": 0.0}
        assert top_node["is_connector"] is True
        # 所有必达展点均可达。
        assert len(body["reachable_points"]) == 2
        assert body["unreachable_points"] == []
        # 诊断信息存在且计数自洽。
        diag = body["diagnostics"]
        assert diag["complete_combinations_evaluated"] >= 1


class TestHealth:
    def test_health(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
