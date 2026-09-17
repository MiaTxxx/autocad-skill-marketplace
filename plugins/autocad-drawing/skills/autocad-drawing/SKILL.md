---
name: autocad-drawing
description: '用 COM/ActiveX 驱动本机正在运行的完整版 AutoCAD，按参数化规格绘制、修改与读取二维图形，交付 dwg 并回读校验几何。用于按给定尺寸画零件/平面图、批量或重复绘图、把自然语言描述转成图纸、从图纸里测量核对尺寸。不用于 AutoCAD LT、BricsCAD/ZWCAD 等非 AutoCAD 平台或纯 dwg 格式转换。'
---

# AutoCAD 参数化绘图

模型只把需求翻译成规格 JSON；几何与算式由 Python 算；落图由 COM 执行；画得对不对由回读数据判定。任何"应该画上了"的结论都不算完成。

## 1. 适用范围

| 用户请求 | 处理范围 |
|---|---|
| 按尺寸画新图 | 写规格 → 校验 → 落图 → 回读；交付 dwg 与实测值 |
| 改已有图（增/删/移/换尺寸） | 先快照现状，再改规格重画，或定点修补后回读；不得静默重排已有图元 |
| 测量/核对图中几何 | 只读快照报实测值，不落图 |
| 自然语言描述转图 | 先经 nl2spec 出规格，落图前核对关键尺寸 |
| 导出 PNG/PDF | 走 `PLOT`/`EXPORTPDF` 命令行；按第 8 节谨慎处理异步命令 |
| AutoCAD LT、BricsCAD、ZWCAD、纯格式转换 | 不适用：LT 无 ActiveX 接口，其他平台 ProgID 不同，需另建方案 |

尺寸缺失或互相矛盾（如"边长 6、圆角 4"）时先问，不猜；可逆的样式选择（图层名、中心线伸出量、圆整顺序）自行决定并在交付里说明。

## 2. 硬规则

1. **模型只产出规格 JSON**，禁止让模型生成可执行代码（LISP/Python/脚本）再执行——那是不可校验、失败无声的执行面。
2. **算术在 Python 里做**（bulge、坐标、弧长、面积），不要求模型做几何计算。
3. **落图后必须回读**：逐图元比对 `Length`/`Area`/`Radius`/`Center`/`Layer`，并检查有无多余图元。
4. **两条独立算路互证**外形尺寸（几何积分 vs 闭式公式），不一致即停。
5. **不擅自清空模型空间、不擅自覆盖已有 dwg**：必须显式 `--clear` / `--force`。
6. **交付里报的数必须是回读实测值**，不是期望值；两者都要列出。
7. 编辑正在打开的图纸前确认 CAD 侧没有未保存改动（`doc.Saved`），避免覆盖用户工作。

## 3. 文件与位置

本 skill 已**全局安装**在 `%USERPROFILE%\.omp\agent\skills\AutoCAD\`（omp 用户级 native provider），任何项目、任何会话都可用；本文件内的所有路径一律相对本文件解析，因此装在哪儿都不影响运行：

| 位置 | 内容 |
|---|---|
| [`scripts/cad_ai.py`](scripts/cad_ai.py) | 核心：规格校验 → 几何编译 → COM 落图 → 回读校验 |
| [`scripts/nl2spec.py`](scripts/nl2spec.py) | 自然语言 → 规格 JSON（OpenAI 兼容接口） |
| [`references/autocad-com-notes.md`](references/autocad-com-notes.md) | COM 绑定细节、属性表、错误码、版本差异、实测锚点 |

规格 `spec_*.json` 与交付 dwg 放在**当前项目**目录；命令可在项目目录里跑（规格给相对路径），也可给绝对路径。

**分享给别人 / 装到别的机器**：用同目录的 marketplace 仓库（`autocad-skill-marketplace`，含 `.omp-plugin/marketplace.json` 与本 skill 的完整副本）。对方两种装法任选：

```
/marketplace add <本地路径 或 github owner/repo>
/marketplace install autocad-drawing@autocad-skill-marketplace
```
或直接把本目录整体拷到对方的 `%USERPROFILE%\.omp\agent\skills\AutoCAD\`。

前置条件：Windows + **完整版** AutoCAD（含 ActiveX 自动化）+ Python + `pip install pywin32`（验证：`python -c "import win32com.client"`）。对方的 AutoCAD 版本不需要相同，但 LT 不可用。

## 4. 执行流程

0. 首次使用或遇到 COM 报错时，先读 [COM 实操笔记](references/autocad-com-notes.md) 对应章节。
1. 取规格：手写 spec JSON，或
   `python scripts/nl2spec.py --text "<中文描述>" --out spec.json`
   （需环境变量 `CAD_LLM_API_KEY`，可选 `CAD_LLM_BASE_URL`/`CAD_LLM_MODEL`；输出不通过校验会回喂错误重试，仍失败则退出）。
2. 预检（不碰 CAD）：`python scripts/cad_ai.py --spec spec.json --dry-run`
   输出语义提示、图元清单、两条算路自检。规格报错在此处修完。
3. 落图：`python scripts/cad_ai.py --spec spec.json --clear`
   可选 `--force`（覆盖同名 dwg）、`--launch`（CAD 未运行时由脚本拉起）、`--dwg <路径>`。
4. 复核：`--verify-only` 只回读不落图（可对任何已画好的图用）。
5. 视觉确认（可选）：窗口截图或 computer-use 看图形是否完整、居中、无多余标注。
6. 交付：dwg 路径 + 回读结果 + 关键实测值。

退出码：`0` 通过｜`2` 规格非法（未碰 CAD）｜`3` CAD 侧错误（连不上/空间非空/文件已存在）｜`4` 回读不匹配。

## 5. 规格字段

顶层 `part` 必需，`output` / `comment` 可选。未知字段一律拒绝。

| 字段 | 含义与约束 |
|---|---|
| `part.outline` | `{type:"rounded_rect", w, h, r, center:[x,y]}`；`r ∈ [0, min(w,h)/2]`，`r=0` 即直角矩形 |
| `part.holes[]` | `diameter`；`origin:"outline_fillet_centers"`（与圆角同心，可用 `inset:[dx,dy]` 覆盖默认 `[r,r]`）或 `origin:"centers"` + `centers:[[x,y],…]` |
| `part.polygons[]` | `type:"regular"`，`sides≥3`，`across_flats` 与 `circumradius` **二选一**，`vertex_up`（默认 true），`center` |
| `part.circles[]` | `diameter`，`center`（同心得写两条） |
| `part.centerlines` | `axis_extent` 水平/垂直轴线**半长**；`diagonals:true` + `diagonal_extent` 45° 斜中心线半长；取值须落在合理区间，超界即拒 |
| `output.dwg` | 交付路径；相对路径按当前工作目录解析 |

默认图层：`外形轮廓`（洋红 ACI 6）、`中心线`（ACI 7 + CENTER 线型）、其余进 `0` 层。单位 mm，原点默认在零件中心，Y 向上。

## 6. 验收判据

| 检查 | 通过条件 |
|---|---|
| 规格 | `validate_spec` 无异常；未知字段/越界/二义尺寸被拒且**未连 CAD** |
| 几何 | 几何积分结果 == 闭式公式结果 |
| 落图 | 回读 `n/n` 全部匹配，无多余图元，数量一致 |
| 反向 | 故意删一个图元后 `--verify-only` 必须 `FAIL` 且退出码 `4`（静默通过即校验器失效） |
| 视觉 | 图形完整、居中、无用户未要求的标注 |

只报"画完了"而不给回读表格，视为未完成。

## 7. 高频坑（详见笔记）

- CAD 没启动时 `GetActiveObject` 抛 `com_error (-2147221021)`——这是没有进程可附着，不是代码错；用 `--launch` 或先手动开 CAD。
- 必须 `gencache.EnsureDispatch`；晚绑定下 `ModelSpace` 遍历与属性读取会不一致。
- `ms.Item(i)` 静态返回 `IAcadEntity` 基类，取 `Length/Radius/Center` 要 `CastTo("IAcadLWPolyline"/"IAcadCircle"/"IAcadLine")`。
- 遍历用索引循环，清空用**倒序**删除；不要 `for e in ms`。
- `AddLightWeightPolyline` 必须传 `VARIANT(VT_ARRAY|VT_R8)` 扁平数组；圆弧段用 `SetBulge(i, tan(θ/4))`（90° 弧 = `tan22.5°`）。
- `ZoomExtents()` / `ActiveViewport` 在 2026 上不刷新画面，改用 `SendCommand("_.ZOOM\n_W\n…")` + `Regen(1)`。
- 校验按**几何配对**，不是按索引配对——索引错位会产生假通过。

## 8. 能力边界

- `SendCommand` 是异步 fire-and-forget，失败只往命令行打文本，**不能据其判定成功**；仅用于没有 COM 等价物的操作（`-HATCH`、`-BOUNDARY`、`PEDIT`、`PLOT`），且落图后仍须回读验证。
- 不覆盖：三维实体、块参照/属性块、标注对象与尺寸驱动约束、图层过滤与布局视口设置。
- 不做：代替用户决定缺失尺寸、在 LT 上工作、声称 DRC/工艺合格。
- 扩展新图元需同步四处：`validate_spec` / `compile_spec` / `snapshot` / `_match`（配套 `_want_text`、`_act_text`），任一处漏改都会让校验失去意义。

## 9. 实测锚点（回归对照）

| 对象 | 期望回读值 |
|---|---|
| 40×40、R4 圆角闭合轮廓 | `Length=153.1327`、`Area=1586.2655`、`Closed=True` |
| 对边距 20 的正六边形 | `Length=69.2820`、`Area=346.4102` |

本机环境：AutoCAD 2026（`app.Name="AutoCAD"`，`app.Version="25.1s (LMS Tech)"`）+ Python 3.14 + pywin32。