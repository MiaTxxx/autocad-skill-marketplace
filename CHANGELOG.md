# Changelog

本仓库记录 `autocad-drawing` skill 的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

## [1.0.0] - 2026-09-17

首个可用版本：把「AI 只说参数、代码负责正确」的 AutoCAD 绘图能力打包成可安装 skill。

### 新增

- `skills/autocad-drawing/SKILL.md`：作业规范 —— 适用范围、7 条硬规则、执行流程、规格字段表、验收判据、高频坑、能力边界。
- `skills/autocad-drawing/scripts/cad_ai.py`：规格校验 → 确定性几何编译 → COM 落图 → 回读校验；退出码 `0/2/3/4` 分级。
- `skills/autocad-drawing/scripts/nl2spec.py`：自然语言 → 规格 JSON（OpenAI 兼容接口），输出经硬校验，不合格回喂错误重试。
- `skills/autocad-drawing/references/autocad-com-notes.md`：COM/ActiveX 实测笔记 —— 绑定、接口转换、属性表、错误码、视图、保存、平台差异。
- 校验采用**两条独立算路**互证外形尺寸（几何积分 vs 闭式公式）；回读按**几何配对**而非索引配对。
- marketplace 目录（`.claude-plugin/marketplace.json`）：插件 `source: "./"`，用 `skills` 字段声明根目录下的 skill。
- `sync.ps1`：从全局安装目录单向刷新仓库副本，避免两份手工漂移。

### 修复

- **CAD 忙时拒绝调用**：识别 `-2147418111`（`RPC_E_CALL_REJECTED`）并给出"请在 CAD 中按 Esc 结束当前命令"的可操作提示，而非抛裸错误码。
- **视图适配触发 WBLOCK 模态框导致挂死**：`SendCommand("_.ZOOM\n_W\n…")` 中的 `_W` 若未被 ZOOM 接住会被解析为命令别名 **W = WBLOCK**，弹出模态框并挂起脚本。改为只用完整关键字 `Extents`，并在发送前用 `CMDACTIVE` 守卫（实测 `SetVariable("VIEWSIZE")` 不可用）。
- **CAD 停在开始页（无任何图纸）**：`ActiveDocument` 抛 `-2145320900`，原先被误报为附着失败。握手改用 `Documents.Count`，新增 `ensure_document()`（已有则用 → 目标存在则打开 → 否则新建）。
- **关闭/新建/打开图纸后的瞬时忙窗口**：`RPC_E_CALL_REJECTED` 会打到任意 COM 调用，原先只有 `attach()` 有重试。抽出 `retry_com()` 覆盖全部 COM 边界；落图中途被拒则丢弃半成品整轮重画，避免重复实体。

### 重构

- 按主流技能仓库布局（对齐 `anthropics/skills`）把 skill 移到根目录 `skills/<name>/`，删除多余的 `plugins/<plugin>/skills/` 包装。
- 移除 `.omp-plugin/marketplace.json`，统一由 `.claude-plugin/marketplace.json` 提供目录（omp 会自动回退读取），避免两份 catalog 漂移。

### 文档

- README：安装（marketplace / 拷贝两种）、用法、仓库结构、维护与发布流程。
- README：记录"`omp plugin install` 复用缓存、升级不生效"的坑与清除方法，并给出 sha256 哈希核对手段。
- 回归锚点改用中性示例（25×25 R5、对边距 12 六边形），数值为**本机 CAD 实测回读**值。

### 已知限制

- 不覆盖：三维实体、块参照/属性块、标注对象与尺寸驱动约束、图层过滤与布局视口。
- `SendCommand` 异步且失败无声，仅用于 COM 没有等价物的命令（`-HATCH`、`BOUNDARY`、`PEDIT`、`PLOT`）。
- **AutoCAD LT 无 ActiveX 接口**，不适用；其他 CAD 平台需按其对象模型改写落图/快照两层。
