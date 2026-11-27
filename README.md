# 触摸导览图换板试算服务

馆内触摸导览图部分板磨损，需要在满足物理拼接与导览连通要求的前提下，
求**更换费最低**的逐格用板方案。本服务用 FastAPI 实现，容器化启动，
并在 `/docs` 提供可直接试算的交互文档和样例。

除一次算完外，还支持**分批续算**：馆员可以先分几批试算，看到目前能用的
板和当前最低价，再决定是否继续把剩余分支查完。

## 业务规则

1. 图面最多 **3 行 4 列**（共 12 格），逐格录入。
2. 每格最多 **2 种带报价备板**；非必换格还可以**免费留用旧板**，
   必换位置只能选备板。
3. 每块板用格内局部坐标 `[0,1] × [0,1]` 描述：
   - **凸线接头**（`is_connector=true`）必须在板边缘（x 或 y 为 0/1）；
   - 板内**连线**连接同板两个节点；
   - **展点**是标记为 `point_ids` 的节点。
4. 拼接约束（求解器强制保证）：
   - 接缝两侧的接头**逐一对齐**：同一接缝位置两侧都有、或都没有；
   - 图外缘除**指定入口**外不允许出现任何接头；
   - 从入口沿凸线（板内连线 + 接缝接头贯通）必须能走到**每个必达展点**。
5. 找到可行方案时返回逐格用板（备板/旧板、各自费用）与总价；
   找不到时 `feasible=false` 并给出首要失败原因。

坐标换算：第 `r` 行第 `c` 列格内局部坐标 `(x,y)` 对应
图面全局坐标 `(c+x, r+y)`，响应里的节点坐标均为图面坐标。

## 启动（Docker Compose）

```bash
docker compose up --build
```

- 交互文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 试算接口：`POST /api/solve`（一次算完）
- 分批搜索：`POST /api/batch/start` 发起，`POST /api/batch/{search_id}` 续算

在 `/docs` 展开 `POST /api/solve`，点 **Try it out**，请求体右侧可直接
选择三个内置样例（可行最低价、接缝对齐但展点不可达、接缝错位），
点 **Execute** 即可看到结构化结果。

## 本地开发（无 Docker 时）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# 自测
pip install pytest httpx
pytest -q
```

## 请求示例

```json
{
  "rows": 1,
  "cols": 2,
  "entrance": { "x": 0.5, "y": 0 },
  "must_reach": [{ "x": 1.5, "y": 0.5 }],
  "panels": {
    "P-A": {
      "id": "P-A",
      "price": 100,
      "nodes": [
        { "id": "top", "position": {"x": 0.5, "y": 0}, "is_connector": true },
        { "id": "right", "position": {"x": 1, "y": 0.5}, "is_connector": true },
        { "id": "p", "position": {"x": 0.3, "y": 0.7}, "is_connector": false }
      ],
      "links": [{"a": "top", "b": "right"}, {"a": "right", "b": "p"}],
      "point_ids": ["p"]
    }
  },
  "cells": [
    {"row": 0, "col": 0, "must_replace": true, "candidates": ["P-A"]},
    {"row": 0, "col": 1, "must_replace": true, "candidates": []}
  ]
}
```

## 响应说明

| 字段 | 含义 |
| --- | --- |
| `feasible` | 是否存在可行方案 |
| `total_cost` | 可行时的最低更换总价（旧板免费，不计价） |
| `reason` | 不可行时的首要原因（接缝错位/外缘违规/展点缺失/不可达等） |
| `plan[].choice` | `spare`=付费备板，`old`=留用旧板（price=0） |
| `plan[].nodes[].global_position` | 节点的图面坐标，接头/展点均有标记 |
| `reachable_points` / `unreachable_points` | 必达展点的连通结果 |
| `diagnostics` | 求解过程统计（访问组合数、各类剪枝次数） |

## 分批续算

一次算完适合“给个结论”；分批续算适合“先看看目前有哪些能用板，再决定
要不要找下去”。两次调用都沿用原试算输入（接缝、入口、展点规则完全一致）。

1. **发起**：`POST /api/batch/start`，请求体 = 原试算输入 + `max_attempts`
   （本批最多试放次数）。返回里带 `search_id`。
2. **续算**：`POST /api/batch/{search_id}`，请求体只有 `max_attempts`。
   沿用发起时的输入，从上次停下的位置继续。

计数与续算规则：

- **试放**指给某一格试一个候选板；被剪枝淘汰的候选也计 1 次。
- 每批返回 `attempts_used` 为本批**实际**次数（`0 ≤ attempts_used ≤ max_attempts`）。
- **续算时每个候选分支只试一次**：已试过的候选（无论成败）在同一分支内不会重试。
- `search_id` 只在同一服务进程内有效，进程重启后失效（调用会返回 404）。

状态与报价：

| `status` | 含义 |
| --- | --- |
| `searching` | 仍在试算，还有待查分支 |
| `optimal` | 已证最低价：所有分支查完，`current_min_cost` 即最低价 |
| `infeasible` | 已证无解：所有分支查完均不可行，`reason` 给首要原因 |

| 字段 | 含义 |
| --- | --- |
| `current_min_cost` | 找到可行方案时，当前已知最低价（未找到为 `null`） |
| `best_plan` | 找到可行方案时，当前最低价对应的逐格用板方案 |
| `cost_lower_bound` | 仍有待查分支时，各分支费用下界中的最低值；搜索结束为 `null`（只针对待查分支，可高于已找到的最低价） |
| `pending_branches` | 本批结束后仍待查的候选分支数 |
| `attempted_total` | 自发起以来累计试放次数 |

每个分支的费用下界 = 已选板费用 + 下一块待试板费用 + 其后各格可能的最低
费用（旧板按 0 计），因此至少计入已选板与下一块待试板的费用。
`cost_lower_bound` 只描述**待查分支**：若此前已找到方案，它可能高于
`current_min_cost`（剩余分支若都更贵，说明已找到的很可能是最低价，
但只有 `status=optimal` 才是证明）。

`/docs` 中两个分批接口的示例都设了 `max_attempts=1`：在
`/api/batch/start` 点 Execute 拿到 `search_id` 后，把它填进
`/api/batch/{search_id}` 的路径参数反复 Execute，即可演示“每批只试一个选择”
地逐步推进。可行样例需要 8 批，不可行样例 4 批。

## 求解算法

每格至多 3 个选项（2 备板 + 1 旧板），12 格组合上限 3¹² ≈ 53 万，
采用按格深度优先回溯，并在回溯中增量剪枝：

1. **展点归属剪枝**：所选板在该格必达展点位置必须有对应展点节点；
2. **外缘剪枝**：外缘接头只能出现在指定入口；
3. **接缝对齐剪枝**：与上方、左方已拼格的接头逐一对齐（双向检查）；
4. **连通性增量维护**：可回滚并查集（DSU）合并板内连线与同坐标接缝接头，
   叶节点检查入口与全部必达展点是否同一连通分量；
5. **费用下界剪枝**：当前累计费用不低于已知最优解时立即剪枝，
   保证返回的是最低价方案。

实测最坏对抗场景（3×4、每格 2 候选、失败只能在叶节点暴露，约 2 千个
完整方案评估）耗时约 0.3 秒，满足交互式试算。

分批续算把同一套深度优先回溯改成显式栈：每“试放”一个格子的一个候选就是
一次 attempt，`run_batch(max_attempts)` 推满指定次数即停，栈、可回滚并查集、
已选板都留在实例里，续算从栈顶的下一个候选继续。因此分批与一次算完的
搜索结果完全一致（用板方案与最低价逐格相同），批次大小只影响何时停下。

## 目录结构

```
app/            FastAPI 应用（models 模型校验 / solver 求解器 / reporting 方案展开 / main 接口）
tests/          pytest 自测（37 个用例，含分批续算与结果对照）
examples/       /docs 内置的三个试算样例
Dockerfile, docker-compose.yml, requirements.txt
```

## 已知边界

- 接头只支持边中位置，落在格角点（如 (1,1)）的接头会被显式拒绝；
- 板不支持旋转/翻转，朝向以录入坐标为准；
- 同一坐标位置每块板只允许一个接头节点；
- 分批搜索编号只存在当前进程内存中，单进程有效；`docker compose` 默认单
  副本，勿加多 worker 或多副本后依赖跨进程续算。
