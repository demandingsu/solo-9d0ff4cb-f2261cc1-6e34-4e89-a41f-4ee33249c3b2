"""构造测试请求的小工具。"""

from __future__ import annotations

from app.models import CellSpec, Link, Node, PanelSpec, Position, SolveRequest


def node(nid: str, x: float, y: float, connector: bool = False) -> Node:
    return Node(
        id=nid, position=Position(x=x, y=y), is_connector=connector
    )


def link(a: str, b: str) -> Link:
    return Link(a=a, b=b)


def panel(
    pid: str,
    price: float,
    nodes: list[Node],
    links: list[Link] | None = None,
    point_ids: list[str] | None = None,
) -> PanelSpec:
    return PanelSpec(
        id=pid,
        price=price,
        nodes=nodes,
        links=links or [],
        point_ids=point_ids or [],
    )


def cell(
    r: int,
    c: int,
    must_replace: bool,
    candidates: list[str] | None = None,
    old_panel: str | None = None,
) -> CellSpec:
    return CellSpec(
        row=r,
        col=c,
        must_replace=must_replace,
        candidates=candidates or [],
        old_panel=old_panel,
    )


def request(
    rows: int,
    cols: int,
    panels: dict[str, PanelSpec],
    cells: list[CellSpec],
    entrance: tuple[float, float],
    must_reach: list[tuple[float, float]] | None = None,
) -> SolveRequest:
    return SolveRequest(
        rows=rows,
        cols=cols,
        panels=panels,
        cells=cells,
        entrance=Position(x=entrance[0], y=entrance[1]),
        must_reach=[Position(x=x, y=y) for x, y in (must_reach or [])],
    )
