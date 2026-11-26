"""请求/响应数据模型与入参校验。

坐标约定
========
* 备板定义使用**格内局部坐标**：每格是 [0, 1] x [0, 1] 的正方形，
  原点在格子左上角，x 向右、y 向下，因此同一备板可在多格复用。
* 图面（全局）坐标：位于第 r 行、第 c 列（均从 0 起）的格子中，
  局部坐标 (x, y) 的节点对应全局坐标 (c + x, r + y)。
* 凸线接头（connector）必须位于备板边缘，即 x 或 y 恰为 0/1。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

MAX_ROWS = 3
MAX_COLS = 4
MAX_CANDIDATES = 2
EPS = 1e-9

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _load_example(name: str) -> dict:
    return json.loads((_EXAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


class Position(BaseModel):
    """二维坐标（格内为局部坐标，请求里的入口/展点为图面坐标）。"""

    x: float
    y: float


class Node(BaseModel):
    """板上的一个节点：凸线接头或板内展点（也可以只是连线转折点）。"""

    id: str = Field(..., min_length=1, description="板内唯一节点编号")
    position: Position
    is_connector: bool = Field(
        ..., description="True 表示凸线接头（位于板边缘），False 为板内节点"
    )


class Link(BaseModel):
    """板内一条凸线，连接同一块板上的两个节点。"""

    a: str = Field(..., min_length=1)
    b: str = Field(..., min_length=1)


class PanelSpec(BaseModel):
    """一种可选备板的完整规格（含报价）。"""

    id: str = Field(..., min_length=1)
    price: float = Field(..., ge=0, description="更换该备板的报价（元）")
    nodes: list[Node] = Field(..., min_length=1)
    links: list[Link] = Field(default_factory=list)
    point_ids: list[str] = Field(
        default_factory=list, description="作为展点的节点编号"
    )

    @model_validator(mode="after")
    def _check_panel(self) -> "PanelSpec":
        node_ids = [n.id for n in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError(f"备板 {self.id} 存在重复节点编号")
        node_set = set(node_ids)
        for node in self.nodes:
            x, y = node.position.x, node.position.y
            if not (-EPS <= x <= 1 + EPS and -EPS <= y <= 1 + EPS):
                raise ValueError(
                    f"备板 {self.id} 节点 {node.id} 局部坐标超出 [0,1] 范围"
                )
            if node.is_connector:
                on_edge = (
                    abs(x) <= EPS
                    or abs(x - 1) <= EPS
                    or abs(y) <= EPS
                    or abs(y - 1) <= EPS
                )
                if not on_edge:
                    raise ValueError(
                        f"备板 {self.id} 的接头 {node.id} 不在板边缘"
                        "（x 或 y 应为 0 或 1）"
                    )
        for link in self.links:
            if link.a not in node_set or link.b not in node_set:
                raise ValueError(
                    f"备板 {self.id} 的连线 {link.a}-{link.b} 引用了不存在的节点"
                )
            if link.a == link.b:
                raise ValueError(f"备板 {self.id} 存在自环线 {link.a}")
        for pid in self.point_ids:
            if pid not in node_set:
                raise ValueError(
                    f"备板 {self.id} 的展点 {pid} 不是已定义节点"
                )
        return self


class CellSpec(BaseModel):
    """一个格子的换板约束。"""

    row: int = Field(..., ge=0)
    col: int = Field(..., ge=0)
    must_replace: bool = Field(..., description="True=必换位置，只能选备板")
    candidates: list[str] = Field(
        default_factory=list,
        description="带报价备板编号，每格至多两种",
    )
    old_panel: Optional[str] = Field(
        default=None, description="留用旧板编号（非必换格可用，免费）"
    )

    @model_validator(mode="after")
    def _check_cell(self) -> "CellSpec":
        if len(self.candidates) > MAX_CANDIDATES:
            raise ValueError(
                f"格子({self.row},{self.col}) 备板数量超过 {MAX_CANDIDATES} 种"
            )
        if len(self.candidates) != len(set(self.candidates)):
            raise ValueError(f"格子({self.row},{self.col}) 备板编号重复")
        if self.must_replace and self.old_panel is not None:
            raise ValueError(
                f"格子({self.row},{self.col}) 是必换位置，不能指定旧板"
            )
        if not self.must_replace and not self.old_panel and not self.candidates:
            raise ValueError(
                f"格子({self.row},{self.col}) 既没有可留用旧板，也没有备板可选"
            )
        return self


class SolveRequest(BaseModel):
    """换板试算请求。"""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "可行：最低价换板方案（2x2）",
                    "value": _load_example("feasible_min_cost"),
                },
                {
                    "summary": "不可行：接缝对齐但展点不可达（2x2）",
                    "value": _load_example("unreachable_point"),
                },
                {
                    "summary": "不可行：接缝接头错位（1x2）",
                    "value": _load_example("seam_mismatch"),
                },
            ]
        }
    }

    rows: int = Field(..., ge=1, le=MAX_ROWS)
    cols: int = Field(..., ge=1, le=MAX_COLS)
    panels: dict[str, PanelSpec]
    cells: list[CellSpec]
    entrance: Position = Field(
        ..., description="指定入口的图面坐标，须落在图外缘并与某接头重合"
    )
    must_reach: list[Position] = Field(
        default_factory=list, description="必达展点的图面坐标"
    )

    @model_validator(mode="after")
    def _check_request(self) -> "SolveRequest":
        if not self.panels:
            raise ValueError("至少要定义一种板")
        if len(self.cells) != self.rows * self.cols:
            raise ValueError(
                f"格子数量应为 {self.rows * self.cols}（{self.rows} 行 x "
                f"{self.cols} 列），实际 {len(self.cells)}"
            )
        seen: set[tuple[int, int]] = set()
        for cell in self.cells:
            if cell.row >= self.rows or cell.col >= self.cols:
                raise ValueError(
                    f"格子({cell.row},{cell.col}) 超出 {self.rows}x{self.cols} 范围"
                )
            if (cell.row, cell.col) in seen:
                raise ValueError(f"格子({cell.row},{cell.col}) 重复录入")
            seen.add((cell.row, cell.col))
            refs = list(cell.candidates)
            if cell.old_panel is not None:
                refs.append(cell.old_panel)
            for pid in refs:
                if pid not in self.panels:
                    raise ValueError(
                        f"格子({cell.row},{cell.col}) 引用了未定义的板 {pid}"
                    )
            if cell.must_replace and not cell.candidates:
                raise ValueError(
                    f"必换格子({cell.row},{cell.col}) 没有任何备板可选"
                )
        self._check_entrance_on_boundary()
        self._check_must_reach_inside()
        return self

    def _check_entrance_on_boundary(self) -> None:
        w, h = self.cols, self.rows
        x, y = self.entrance.x, self.entrance.y
        in_x = -EPS <= x <= w + EPS
        in_y = -EPS <= y <= h + EPS
        if not (in_x and in_y):
            raise ValueError("入口坐标超出图面范围")
        on_outer = (
            abs(x) <= EPS
            or abs(x - w) <= EPS
            or abs(y) <= EPS
            or abs(y - h) <= EPS
        )
        if not on_outer:
            raise ValueError("入口必须位于图的外缘（x 或 y 恰为 0 或图宽/高）")

    def _check_must_reach_inside(self) -> None:
        w, h = self.cols, self.rows
        for p in self.must_reach:
            if not (
                -EPS <= p.x <= w + EPS and -EPS <= p.y <= h + EPS
            ):
                raise ValueError(f"必达展点({p.x},{p.y}) 超出图面范围")


class ChosenPanel(BaseModel):
    """逐格用板方案中的一格结果。"""

    row: int
    col: int
    panel_id: str
    choice: Literal["spare", "old"] = Field(
        ..., description="spare=更换备板（付费），old=留用旧板（免费）"
    )
    price: float


class GlobalNode(BaseModel):
    cell_row: int
    cell_col: int
    local_id: str
    global_position: Position
    is_connector: bool
    is_point: bool


class GlobalLink(BaseModel):
    a_local_id: str
    b_local_id: str


class CellPlan(ChosenPanel):
    nodes: list[GlobalNode]
    links: list[GlobalLink]
    point_ids: list[str]


class SolveResponse(BaseModel):
    feasible: bool
    total_cost: Optional[float] = None
    reason: Optional[str] = Field(
        default=None, description="不可行时的首要原因"
    )
    entrance: Position
    must_reach: list[Position]
    reachable_points: list[Position] = Field(
        default_factory=list, description="从入口沿凸线实际可达的必达展点"
    )
    unreachable_points: list[Position] = Field(default_factory=list)
    plan: Optional[list[CellPlan]] = None
    diagnostics: dict[str, int] = Field(
        default_factory=dict,
        description="求解过程统计：完整组合数、各类剪枝次数等",
    )
