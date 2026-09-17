# AutoCAD COM/ActiveX 实操笔记

本机实测环境：AutoCAD 2026（`app.Name="AutoCAD"`、`app.Version="25.1s (LMS Tech)"`）、Python 3.14、pywin32。以下均为实际跑通或实际报错得到的行为，不是文档推演。

## 1. 连接与绑定

```python
import win32com.client as wc
from win32com.client import gencache

app = gencache.EnsureDispatch(wc.GetActiveObject("AutoCAD.Application"))  # 附着到已运行的 CAD
app = gencache.EnsureDispatch(wc.Dispatch("AutoCAD.Application"))         # 没有实例时新建一个
```

- CAD 未运行时 `GetActiveObject` 抛 `pywintypes.com_error (-2147221021, '操作无法使用')`。这是"没有进程可附着"，不是代码错误；此时改用 `Dispatch` 会新拉起一个 CAD 进程。
- **CAD 正忙时会拒绝调用**：`com_error (-2147418111, '被呼叫方拒绝接收呼叫。')`。这不是"没连上"——进程在、窗口 `Responding=True`、也没有模态对话框，而是 **CAD 里有交互命令正在执行或等待输入**（夹点编辑、选择窗口、命令行等待点/数值）。判别与处置：
  - 脚本侧：`attach()` 做有界重试并把这个 hr 单独识别（`ERR_BUSY`），失败时给出"请在 CAD 中按 Esc 结束当前命令"的提示，而不是抛裸错误码；
  - 人工侧：枚举 CAD 顶层窗口确认无模态框，再截图看命令行/夹点状态；
  - **不要强杀 CAD 进程**，会丢用户未保存的编辑。
- 一律用 `gencache.EnsureDispatch`。晚绑定（`wc.Dispatch` 不经 gencache）下 `ModelSpace` 的遍历与属性读取行为不一致，曾直接导致取不到属性。
- `app.Visible = True` 让新实例可见；`app.Quit()` 可关闭。
- 中文文件名与中文路径可用（`SaveAs(r"…\example.dwg")` 实测成功）。Python 以 `-X utf8` 运行可避免控制台打印中文报编码错。
- ProgID：`AutoCAD.Application`（另有带年份后缀的 `AutoCAD.Application.25` 等）。其他平台如 BricsCAD（`BricscadApp.AcadApplication`）、ZWCAD（`ZWCAD.Application`）ProgID 不同，未验证。

## 2. 取属性必须转接口

模型的类型库把 `ModelSpace.Item(i)` 声明为返回 `IAcadEntity`（基类），基类上**没有** `Length`/`Radius`/`Center`，直接取会 `AttributeError`。按图元实际类型转：

```python
from win32com.client import CastTo
obj = CastTo(ms.Item(i), "IAcadLWPolyline")   # 或 "IAcadCircle" / "IAcadLine"
```

`EntityName` 判类型：`AcDbPolyline`（轻量多段线）、`AcDbCircle`、`AcDbLine`；重多段线为 `AcDb2dPolyline`。

## 3. 遍历与删除

- 遍历：`for i in range(ms.Count): obj = ms.Item(i)`。不要 `for e in ms`。
- 清空：**倒序**删除，边删边改索引会跳过对象。
  ```python
  for i in range(ms.Count - 1, -1, -1):
      ms.Item(i).Delete()
  ```

## 4. 可读属性（回读校验就靠这些）

| 图元 | 可读 | 说明 |
|---|---|---|
| `AcDbPolyline` | `Length`、`Area`、`Closed`、`Coordinates`、`Layer` | 圆弧段计入长度与面积，可直接与解析计算值比对 |
| `AcDbCircle` | `Radius`、`Center`、`Layer` | `Center` 是三元组 |
| `AcDbLine` | `StartPoint`、`EndPoint`、`Length`、`Layer` | 端点用于校验位置，不只是长度 |

## 5. 创建图元

```python
import pythoncom
from win32com.client import VARIANT

def arr(*floats):
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in floats])

pl = ms.AddLightWeightPolyline(arr(x1, y1, x2, y2, ...))   # 扁平数组，不是 [[x,y],…]
pl.SetBulge(i, math.tan(math.radians(90) / 4))             # 第 i 段为圆弧；bulge = tan(圆心角/4)
pl.Closed = True
pl.Layer = "外形轮廓"
pl.Update()

c = ms.AddCircle(arr(0.0, 0.0, 0.0), 10.0)                 # 圆心三元组 + 半径
ln = ms.AddLine(arr(0.0, 0.0, 0.0), arr(10.0, 0.0, 0.0))
```

- bulge 定义：`b = tan(θ/4)`，θ 为圆心角；逆时针为正。90° 圆弧 `= tan 22.5° ≈ 0.414214`。
- 直接传 Python 列表进 `AddLightWeightPolyline` 会失败，必须 `VARIANT(VT_ARRAY|VT_R8)`。

## 6. 图层与线型

```python
doc.Layers.Add("外形轮廓").color = 6            # ACI 色号：6=洋红, 7=白/黑
doc.Linetypes.Load("CENTER", "acad.lin")        # 线型要先加载，否则赋值抛错
doc.Layers.Item("中心线").Linetype = "CENTER"
```

## 7. 视图

- `doc.SetVariable("UCSICON", 0)` 关闭 UCS 图标（保存进文件的视图状态）。
- `app.ZoomExtents()`、通过 `doc.ActiveViewport` 设置 `Center`/`Height`、`doc.SetVariable("VIEWSIZE", …)` 在 2026 上都不能可靠改变画面（前者不重绘，后者直接报"设置系统变量时出错"）。
- 有且只有命令行能用，但**只能用完整关键字**：
  ```python
  if int(doc.GetVariable("CMDACTIVE")) == 0:        # 有命令在执行就不要塞输入
      doc.SendCommand("_.ZOOM\nExtents\n")
      doc.Regen(1)
  ```
- **实测事故（务必记住）**：用缩写 `_.ZOOM\n_W\n…` 时，若 ZOOM 没接住这段输入，`W` 会在命令行被解析为命令别名 **W = WBLOCK（写块）**，弹出**模态对话框**，此后 CAD 主窗口 `enabled=False`、所有 ActiveX 调用被拒绝/挂起，脚本卡死（不是报错退出）。处置：向该对话框窗口 `PostMessage(hDlg, WM_CLOSE, 0, 0)` 等价于"取消"；**不要**轻易点"确定"（会真写块文件）。同理 `_E` = ERASE 别名。凡是往命令行送字符，都要用完整关键字（`Extents`/`Window`/`All`），不用单字母缩写。

## 8. 保存与文件

- 就地保存 `doc.Save()`；另存 `doc.SaveAs(path)`，之后 `doc.Name` / `doc.FullName` 更新为新名。
- 打开状态下会生成同名 `.dwl`/`.dwl2` 锁文件，属正常，不要删。
- AutoCAD 会另生成 `.bak` 备份；交付前可清理，但别删用户其它备份。
- 判断图纸是否有未保存改动用 `doc.Saved`。

## 9. SendCommand 的语义与适用边界

- 文档级命令，行尾加 `\n`；英文命令名前面加下划线（`_.ZOOM`）以避开本地化。
- **异步、无返回值**，失败只在命令行打一行文本 → 不能用来判定成败，也不能据此报"成功"。
- 只对没有 COM 等价物的操作使用：`-HATCH`、`-BOUNDARY`、`PEDIT`、`PLOT`/`EXPORTPDF` 等；用完后仍须回读模型空间验证。

## 10. 实测锚点（回归对照）

| 对象 | 回读值 |
|---|---|
| 40×40、R4 圆角、闭合轮廓 | `Length=153.1327`、`Area=1586.2655`、`Closed=True` |
| 对边距 20 的正六边形 | `Length=69.2820`、`Area=346.4102` |
| 解析校验 | 矩形圆角轮廓 `2(w−2r)+2(h−2r)+2πr`；`40×40 R4` 代入 = `153.1327`；面积 `w·h−(4−π)r²` = `1586.2655` |

## 11. 平台/版本差异

- **AutoCAD LT 没有 ActiveX 自动化接口**，本方法在 LT 上不可用（依 Autodesk 文档，未在本机验证 LT）。
- AutoCAD 完整版自 R14 起提供 ActiveX（`acadauto.chm` / `AutoCAD ActiveX Reference`）。
- 若要在其他 CAD 上复刻本方案，需要按其 ProgID 与对象模型改写 `cad_ai.py` 的落图与快照层；规格、校验、编译三层与平台无关，可直接复用。