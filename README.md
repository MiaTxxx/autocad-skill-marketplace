# autocad-skill-marketplace

把「AI 只说参数，代码负责正确」的 AutoCAD 绘图能力打包成可安装的 omp / Claude Code skill。

**核心主张**：模型输出结构化规格 JSON，几何与算式由 Python 计算，落图走 COM/ActiveX，落图后逐图元回读 `Length`/`Area`/`Radius`/`Center`/`Layer` 与期望值比对。不允许"让模型生成 LISP 脚本再执行"——那是不可校验、失败无声的执行面。

## 安装

### 方式一：marketplace（推荐，可升级/可卸载）

```
/marketplace add <本仓库路径 或 GitHub 的 owner/repo>
/marketplace install autocad-drawing@autocad-skill-marketplace
```

CLI 等价写法：

```bash
omp plugin marketplace add owner/repo
omp plugin install autocad-drawing@autocad-skill-marketplace
```

### 方式二：直接拷成用户级 skill

把 `plugins/autocad-drawing/skills/autocad-drawing/` 整个复制到 `%USERPROFILE%\.omp\agent\skills\AutoCAD\`，任何项目即刻可用（skill 发现规则：`<skills-root>/<name>/SKILL.md`，逐级不递归）。

装好后新开一个会话，`skill://autocad-drawing` 与 `/skill:autocad-drawing` 即可用。

## 前置条件

| 项 | 要求 |
|---|---|
| 系统 | Windows |
| CAD | **完整版** AutoCAD（含 ActiveX 自动化）；**LT 不可用** |
| Python | 3.x + `pip install pywin32` |

验证：`python -c "import win32com.client"`。对方 AutoCAD 版本不必与本机相同，程序按 ProgID `AutoCAD.Application` 附着到正在运行的实例。

## 用法

```
/skill:autocad-drawing 画一块 40×40 板，四角 R4 圆角，四角 Ø4 孔与圆角同心，中心正六边形内切圆 Ø20，中心 Ø20 与 Ø10 圆
```

或直接用脚本（在项目目录里跑，规格给绝对路径亦可）：

```bash
python scripts/nl2spec.py --text "<中文描述>" --out spec.json   # 自然语言 → 规格(需 CAD_LLM_API_KEY)
python scripts/cad_ai.py --spec spec.json --dry-run            # 预检, 不碰 CAD
python scripts/cad_ai.py --spec spec.json --clear              # 落图并保存
python scripts/cad_ai.py --spec spec.json --verify-only        # 只回读校验
```

退出码：`0` 通过｜`2` 规格非法（未碰 CAD）｜`3` CAD 侧错误｜`4` 回读不匹配。

`nl2spec.py` 通过环境变量接入任意 OpenAI 兼容接口：`CAD_LLM_BASE_URL`、`CAD_LLM_API_KEY`、`CAD_LLM_MODEL`。模型输出会被硬校验，不合格则把错误回喂重试。

## 仓库结构

```
.omp-plugin/marketplace.json          omp 目录
.claude-plugin/marketplace.json       Claude Code 兼容目录（同一份内容）
plugins/autocad-drawing/
  skills/autocad-drawing/
    SKILL.md                          作业规范：硬规则/流程/验收判据/坑
    scripts/cad_ai.py                 校验 → 编译 → 落图 → 回读
    scripts/nl2spec.py                自然语言 → 规格 JSON
    references/autocad-com-notes.md   COM 实测笔记：绑定、属性、错误码、锚点
sync.ps1                              从全局安装目录刷新本仓库副本
```

## 维护：改完怎么同步

`plugins/autocad-drawing/skills/autocad-drawing/` 是**产物副本**，真正的编辑对象是全局安装目录。改完跑一次：

```powershell
powershell -ExecutionPolicy Bypass -File sync.ps1
```

## 发布到 GitHub 让别人用

```bash
git init && git add -A && git commit -m "autocad-drawing skill"
git remote add origin git@github.com:<你的账号>/autocad-skill-marketplace.git
git push -u origin main
```

之后把 `owner/repo` 发给别人，对方 `/marketplace add owner/repo` 即可。

## 边界

不覆盖：三维实体、块/属性块、标注对象与参数化约束、布局视口。`SendCommand` 是异步且失败无声，仅用于 COM 没有等价物的命令（`-HATCH`、`BOUNDARY`、`PEDIT`、`PLOT`），且仍需回读验证。