# -*- coding: utf-8 -*-
"""
CAD-AI 流水线核心: 结构化规格(JSON) -> 校验 -> 确定性几何编译 -> AutoCAD COM -> 回读校验

与"让 AI 生成 LISP 代码文本再转发执行"的区别:
  1. 模型只填参数(JSON), 不产出可执行代码 -> 没有代码注入面, 也没有"LISP 报错只打一行红字"的静默失败
  2. 几何与算式全部在 Python 里算, 不依赖模型做算术
  3. 落图后逐图元回读 Length/Area/Radius/Center/Layer, 与编译期期望值比对 -> 画没画上由数据说话
  4. 校验分两层: 硬错误(拒绝执行) 与 语义提示(如六边形内切圆是否与中心圆重合)
  5. 默认不覆盖已有图元、不覆盖已有文件, 需显式 --clear / --force
"""
import argparse
import json
import math
import os
import sys

TOL = 1e-6
DEFAULT_LAYERS = {"外形轮廓": {"color": 6}, "中心线": {"color": 7, "linetype": "CENTER"}}
MAGENTA_LAYER = "外形轮廓"


class SpecError(Exception):
    """规格非法 -> 直接拒绝执行, 不碰 CAD"""


# ============ 1. 规格校验 ============

def _num(v, where):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SpecError("%s 必须是数字, 收到 %r" % (where, v))
    return float(v)


def _pos(v, where):
    x = _num(v, where)
    if x <= 0:
        raise SpecError("%s 必须 > 0, 收到 %r" % (where, v))
    return x


def _pt(v, where):
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise SpecError("%s 必须是 [x, y], 收到 %r" % (where, v))
    return [_num(v[0], where + "[0]"), _num(v[1], where + "[1]")]


def _keys(d, allowed, where):
    if not isinstance(d, dict):
        raise SpecError("%s 必须是对象, 收到 %r" % (where, d))
    for k in d:
        if k not in allowed:
            raise SpecError("%s 出现未知字段 %r (允许: %s)" % (where, k, "/".join(sorted(allowed))))


def validate_spec(spec):
    """硬校验(出错即拒绝) + 语义提示, 返回提示列表"""
    _keys(spec, {"part", "output", "comment"}, "顶层")
    part = spec.get("part")
    _keys(part, {"outline", "holes", "polygons", "circles", "centerlines", "comment"}, "part")

    out = part.get("outline")
    if out is None:
        raise SpecError("part.outline 必需")
    _keys(out, {"type", "w", "h", "r", "center"}, "part.outline")
    if out.get("type", "rounded_rect") != "rounded_rect":
        raise SpecError("part.outline.type 只支持 rounded_rect")
    w = _pos(out.get("w"), "part.outline.w")
    h = _pos(out.get("h"), "part.outline.h")
    r = _num(out.get("r", 0), "part.outline.r")
    if not 0 <= r <= min(w, h) / 2.0:
        raise SpecError("part.outline.r 必须落在 [0, min(w,h)/2] = [0, %.4g]" % (min(w, h) / 2.0))
    _pt(out.get("center", [0, 0]), "part.outline.center")

    for i, hole in enumerate(part.get("holes", [])):
        where = "part.holes[%d]" % i
        _keys(hole, {"diameter", "origin", "inset", "centers", "layer"}, where)
        d = _pos(hole.get("diameter"), where + ".diameter")
        origin = hole.get("origin", "outline_fillet_centers")
        if origin not in ("outline_fillet_centers", "centers"):
            raise SpecError("%s.origin 只支持 outline_fillet_centers / centers" % where)
        if origin == "outline_fillet_centers":
            ins = _pt(hole.get("inset", [r, r]), where + ".inset")
            if ins[0] > w / 2.0 or ins[1] > h / 2.0:
                raise SpecError("%s.inset 超出板面范围" % where)
            if d / 2.0 > ins[0] or d / 2.0 > ins[1]:
                pass  # 允许孔边越出外形(本作业即如此), 由提示说明
        else:
            cs = hole.get("centers")
            if not isinstance(cs, list) or not cs:
                raise SpecError("%s.centers 必须是非空数组" % where)
            for j, c in enumerate(cs):
                _pt(c, "%s.centers[%d]" % (where, j))

    for i, poly in enumerate(part.get("polygons", [])):
        where = "part.polygons[%d]" % i
        _keys(poly, {"type", "sides", "across_flats", "circumradius", "center",
                     "vertex_up", "layer"}, where)
        if poly.get("type", "regular") != "regular":
            raise SpecError("%s.type 只支持 regular" % where)
        n = poly.get("sides")
        if not isinstance(n, int) or isinstance(n, bool) or n < 3:
            raise SpecError("%s.sides 必须是 >= 3 的整数" % where)
        if ("across_flats" in poly) == ("circumradius" in poly):
            raise SpecError("%s 必须且只能给 across_flats 或 circumradius 之一" % where)
        if "across_flats" in poly:
            _pos(poly["across_flats"], where + ".across_flats")
        else:
            _pos(poly["circumradius"], where + ".circumradius")
        _pt(poly.get("center", [0, 0]), where + ".center")

    for i, c in enumerate(part.get("circles", [])):
        where = "part.circles[%d]" % i
        _keys(c, {"diameter", "center", "layer"}, where)
        _pos(c.get("diameter"), where + ".diameter")
        _pt(c.get("center", [0, 0]), where + ".center")

    cl = part.get("centerlines", {})
    _keys(cl, {"axis_extent", "diagonals", "diagonal_extent", "layer"}, "part.centerlines")
    half_diag = math.hypot(w, h) / 2.0
    if cl:
        # axis_extent / diagonal_extent 都是"半长"(从中点到端点); 超出板面尺寸范围即判非法,
        # 这是模型最容易搞错的地方(把总长当半长), 交给回喂重试去修
        ext = float(cl["axis_extent"]) if "axis_extent" in cl else max(w, h) / 2.0 + 6.0
        lo, hi = max(w, h) / 2.0, max(w, h)
        if not lo <= ext <= hi:
            raise SpecError("part.centerlines.axis_extent 是轴线半长, 应落在 [%.4g, %.4g], 收到 %g"
                            % (lo, hi, ext))
        if cl.get("diagonals"):
            de = float(cl["diagonal_extent"]) if "diagonal_extent" in cl else half_diag + 4.0
            lo, hi = half_diag, max(w, h)
            if not lo <= de <= hi:
                raise SpecError("part.centerlines.diagonal_extent 是斜中心线半长, 应落在 "
                                "[%.4g, %.4g], 收到 %g" % (lo, hi, de))

    if "output" in spec:
        _keys(spec["output"], {"dwg"}, "output")

    # ---- 语义提示(不拦截执行) ----
    notes = []
    circles_d = [float(c["diameter"]) for c in part.get("circles", [])]
    for i, poly in enumerate(part.get("polygons", [])):
        n, af = poly["sides"], None
        if "across_flats" in poly:
            af = float(poly["across_flats"])
        else:
            af = 2.0 * float(poly["circumradius"]) * math.cos(math.pi / n)
        hit = [d for d in circles_d if abs(d - af) < 1e-9]
        if hit:
            notes.append("polygons[%d] 内切圆 Ø%g 与中心圆 Ø%g 重合 -> 六边形与该圆相切" % (i, af, hit[0]))
        elif circles_d:
            notes.append("polygons[%d] 内切圆 Ø%.4g 与中心圆 %s 不重合(不相切)"
                         % (i, af, "/".join("Ø%g" % d for d in circles_d)))
    for i, hole in enumerate(part.get("holes", [])):
        if hole.get("origin", "outline_fillet_centers") == "outline_fillet_centers":
            ins = hole.get("inset", [r, r])
            if float(hole["diameter"]) / 2.0 > min(ins):
                notes.append("holes[%d] Ø%g 的孔边越出外形轮廓(圆心离边 %g, 半径 %g)"
                             % (i, float(hole["diameter"]), min(ins), float(hole["diameter"]) / 2.0))
    return notes


# ============ 2. 几何编译(全确定性, 不用模型算数) ============

def _rounded_rect_pts(w, h, r, cx, cy):
    hw, hh = w / 2.0, h / 2.0
    bulge = math.tan(math.radians(90) / 4.0)   # 90° 圆弧的 bulge
    if r <= 0:
        return [[cx - hw, cy - hh, 0.0], [cx + hw, cy - hh, 0.0],
                [cx + hw, cy + hh, 0.0], [cx - hw, cy + hh, 0.0]]
    return [[cx - hw + r, cy - hh, 0.0], [cx + hw - r, cy - hh, bulge],
            [cx + hw, cy - hh + r, 0.0], [cx + hw, cy + hh - r, bulge],
            [cx + hw - r, cy + hh, 0.0], [cx - hw + r, cy + hh, bulge],
            [cx - hw, cy + hh - r, 0.0], [cx - hw, cy - hh + r, bulge]]


def pl_length(pts, closed=True):
    """独立于 CAD 的弧长积分: 直线段 + 圆弧段(bulge)"""
    n, total = len(pts), 0.0
    for i in range(n if closed else n - 1):
        x1, y1, b = pts[i]
        x2, y2, _ = pts[(i + 1) % n]
        chord = math.hypot(x2 - x1, y2 - y1)
        if abs(b) < 1e-12:
            total += chord
        else:
            theta = 4.0 * math.atan(b)                      # 圆心角
            total += chord * theta / (2.0 * math.sin(theta / 2.0))
    return total


def pl_area(pts, closed=True):
    """独立于 CAD 的面积: 鞋带公式 + 圆弧弓形面积(带符号)"""
    n, a = len(pts), 0.0
    for i in range(n if closed else n - 1):
        x1, y1, _ = pts[i]
        x2, y2, _ = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    a *= 0.5
    for i in range(n if closed else n - 1):
        x1, y1, b = pts[i]
        x2, y2, _ = pts[(i + 1) % n]
        if abs(b) < 1e-12:
            continue
        chord = math.hypot(x2 - x1, y2 - y1)
        theta = 4.0 * math.atan(b)
        rad = chord / (2.0 * math.sin(abs(theta) / 2.0))
        seg = 0.5 * rad * rad * (abs(theta) - math.sin(abs(theta)))
        a += seg if b > 0 else -seg
    return abs(a)


def compile_spec(spec, layers=None):
    """规格 -> 图元清单(含期望值)"""
    layers = DEFAULT_LAYERS if layers is None else layers
    part = spec["part"]
    ents = []

    out = part["outline"]
    w, h, r = float(out["w"]), float(out["h"]), float(out.get("r", 0))
    cx, cy = out.get("center", [0, 0])
    pts = _rounded_rect_pts(w, h, r, float(cx), float(cy))
    ents.append({"op": "lwpolyline", "pts": pts, "closed": True,
                 "layer": MAGENTA_LAYER, "tag": "外形轮廓",
                 "expect": {"length": pl_length(pts), "area": pl_area(pts)}})

    for i, hole in enumerate(part.get("holes", [])):
        d = float(hole["diameter"])
        if hole.get("origin", "outline_fillet_centers") == "outline_fillet_centers":
            ins = hole.get("inset", [r, r])
            cs = [[float(cx) - w / 2.0 + ins[0], float(cy) - h / 2.0 + ins[1]],
                  [float(cx) - w / 2.0 + ins[0], float(cy) + h / 2.0 - ins[1]],
                  [float(cx) + w / 2.0 - ins[0], float(cy) + h / 2.0 - ins[1]],
                  [float(cx) + w / 2.0 - ins[0], float(cy) - h / 2.0 + ins[1]]]
        else:
            cs = [[float(c[0]), float(c[1])] for c in hole["centers"]]
        for c in cs:
            ents.append({"op": "circle", "center": c, "radius": d / 2.0,
                         "layer": hole.get("layer", "0"), "tag": "holes[%d]" % i,
                         "expect": {"radius": d / 2.0, "center": c}})

    for i, poly in enumerate(part.get("polygons", [])):
        n = int(poly["sides"])
        pcx, pcy = poly.get("center", [0, 0])
        if "across_flats" in poly:
            big_r = float(poly["across_flats"]) / 2.0 / math.cos(math.pi / n)
        else:
            big_r = float(poly["circumradius"])
        base = 90.0 if poly.get("vertex_up", True) else 90.0 + 180.0 / n
        pts = [[float(pcx) + big_r * math.cos(math.radians(base + 360.0 * k / n)),
                float(pcy) + big_r * math.sin(math.radians(base + 360.0 * k / n)), 0.0]
               for k in range(n)]
        ents.append({"op": "lwpolyline", "pts": pts, "closed": True,
                     "layer": poly.get("layer", "0"), "tag": "polygons[%d]" % i,
                     "expect": {"length": pl_length(pts), "area": pl_area(pts)}})

    for i, c in enumerate(part.get("circles", [])):
        ccx, ccy = c.get("center", [0, 0])
        rad = float(c["diameter"]) / 2.0
        ents.append({"op": "circle", "center": [float(ccx), float(ccy)], "radius": rad,
                     "layer": c.get("layer", "0"), "tag": "circles[%d]" % i,
                     "expect": {"radius": rad, "center": [float(ccx), float(ccy)]}})

    cl = part.get("centerlines")
    if cl:
        ext = float(cl.get("axis_extent", max(w, h) / 2.0 + 6))
        layer = cl.get("layer", "中心线")
        for tag, a, b in (("axis_x", (float(cx) - ext, float(cy)), (float(cx) + ext, float(cy))),
                          ("axis_y", (float(cx), float(cy) - ext), (float(cx), float(cy) + ext))):
            ents.append({"op": "line", "a": list(a), "b": list(b), "layer": layer, "tag": tag,
                         "expect": {"length": math.dist(a, b)}})
        if cl.get("diagonals"):
            de = float(cl.get("diagonal_extent", min(math.hypot(w, h) / 2.0 + 4.0, max(w, h))))
            for tag, a, b in (("diag_1", (float(cx) - de, float(cy) - de), (float(cx) + de, float(cy) + de)),
                              ("diag_2", (float(cx) + de, float(cy) - de), (float(cx) - de, float(cy) + de))):
                ents.append({"op": "line", "a": list(a), "b": list(b), "layer": layer, "tag": tag,
                             "expect": {"length": math.dist(a, b)}})
    return ents


# ============ 3. AutoCAD COM 落图 ============

def _variant(flat):
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in flat])


def _cast(obj, interface):
    """ms.Item(i) 声明返回 IAcadEntity(基类), 取 Length/Radius/Center 需按类型库转接口;
    动态绑定与类型库绑定两种模式下都能用。"""
    from win32com.client import CastTo
    try:
        return CastTo(obj, interface)
    except Exception:
        return obj


ERR_NOT_RUNNING = -2147221021   # 操作无法使用: 没有可附着的 CAD 进程
ERR_BUSY = -2147418111          # 被呼叫方拒绝接收呼叫: CAD 正忙(交互命令等待输入等)


def _hr(e):
    hr = getattr(e, "hresult", None)
    if hr is not None:
        return hr
    args = getattr(e, "args", None)
    return args[0] if args and isinstance(args[0], int) else None


def is_busy(e):
    """CAD 拒绝调用(而不是没在运行) —— 典型原因: 有交互命令正在等待输入。"""
    return _hr(e) == ERR_BUSY


def attach(launch=False, attempts=3, delay=1.5):
    """附着到运行中的 AutoCAD。CAD 正忙于交互命令时会被拒绝, 故做有界重试;
    重试仍失败则给出可诊断的原因, 而不是抛裸的 com_error。"""
    import time
    import win32com.client as wc
    last = None
    for attempt in range(1, attempts + 1):
        try:
            app = wc.GetActiveObject("AutoCAD.Application")
            try:                           # 统一走类型库, 避免 gen_py 缓存有无导致行为不一致
                from win32com.client import gencache
                app = gencache.EnsureDispatch(app)
            except Exception:
                pass
            app.ActiveDocument.ModelSpace.Count      # 真正握手一次, 暴露"忙"的拒绝
            return app
        except Exception as e:
            last = e
            if is_busy(e) and attempt < attempts:
                time.sleep(delay)
                continue
            break

    if is_busy(last):
        raise SpecError(
            "AutoCAD 正忙, 拒绝 ActiveX 调用 (%s)。通常是 CAD 里有命令正在执行或等待输入"
            "(夹点编辑、选择窗口、尺寸输入等)。请在 CAD 中按 Esc 结束当前命令后重试; "
            "不要强杀进程, 以免丢失未保存的编辑。" % last)
    if _hr(last) == ERR_NOT_RUNNING and launch:
        try:
            app = wc.Dispatch("AutoCAD.Application")
            try:
                from win32com.client import gencache
                app = gencache.EnsureDispatch(app)
            except Exception:
                pass
            app.ActiveDocument.ModelSpace.Count      # 同样握手一次
            return app
        except Exception as e:
            raise SpecError("拉起 AutoCAD 失败: %s" % e)
    if _hr(last) == ERR_NOT_RUNNING:
        raise SpecError(
            "连不上正在运行的 AutoCAD(%s)。先启动 CAD, 或加 --launch 由脚本拉起。"
            "注意: AutoCAD LT 没有 ActiveX/COM 接口。" % last)
    raise SpecError("附着 AutoCAD 失败: %s" % last)


def ensure_layers(doc, layers):
    for name, cfg in layers.items():
        try:
            lay = doc.Layers.Item(name)
        except Exception:
            lay = doc.Layers.Add(name)
        lay.color = cfg["color"]
        if cfg.get("linetype"):
            try:
                doc.Linetypes.Load(cfg["linetype"], "acad.lin")
            except Exception:
                pass
            try:
                lay.Linetype = cfg["linetype"]
            except Exception:
                pass


def draw(app, ents, layers=None, clear=False):
    layers = DEFAULT_LAYERS if layers is None else layers
    doc = app.ActiveDocument
    ms = doc.ModelSpace
    if ms.Count and not clear:
        raise SpecError("模型空间已有 %d 个图元; 要清空重画请显式加 --clear" % ms.Count)
    if clear:
        for i in range(ms.Count - 1, -1, -1):
            ms.Item(i).Delete()
    ensure_layers(doc, layers)
    try:
        doc.SetVariable("UCSICON", 0)
    except Exception:
        pass

    for e in ents:
        if e["op"] == "lwpolyline":
            obj = ms.AddLightWeightPolyline(_variant([c for p in e["pts"] for c in p[:2]]))
            for i, p in enumerate(e["pts"]):
                if p[2]:
                    obj.SetBulge(i, p[2])
            obj.Closed = bool(e["closed"])
        elif e["op"] == "circle":
            obj = ms.AddCircle(_variant(e["center"] + [0.0]), e["radius"])
        elif e["op"] == "line":
            obj = ms.AddLine(_variant(e["a"] + [0.0]), _variant(e["b"] + [0.0]))
        else:
            raise SpecError("未知图元类型 %r" % e["op"])
        obj.Layer = e["layer"]
        obj.Update()
    return doc


def fit_view(doc, ents, margin=3.0):
    xs, ys = [], []
    for e in ents:
        if e["op"] == "lwpolyline":
            xs += [p[0] for p in e["pts"]]
            ys += [p[1] for p in e["pts"]]
        elif e["op"] == "circle":
            xs += [e["center"][0] - e["radius"], e["center"][0] + e["radius"]]
            ys += [e["center"][1] - e["radius"], e["center"][1] + e["radius"]]
        else:
            xs += [e["a"][0], e["b"][0]]
            ys += [e["a"][1], e["b"][1]]
    try:                                   # COM 的 ZoomExtents 在 2026 上不刷新, 用命令行 ZOOM
        doc.SendCommand("_.ZOOM\n_W\n%.4f,%.4f\n%.4f,%.4f\n" % (
            min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin))
        doc.Regen(1)
    except Exception:
        pass


# ============ 4. 回读校验(按图元实际类型取接口, 按几何匹配, 不看顺序) ============

def _near(a, b, tol=TOL):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def snapshot(ms):
    """把 CAD 里现有图元读成与期望同构的字典列表"""
    out = []
    for i in range(ms.Count):
        o = ms.Item(i)
        name = o.EntityName
        if name == "AcDbCircle":
            c = _cast(o, "IAcadCircle")
            out.append({"op": "circle", "layer": c.Layer, "radius": float(c.Radius),
                        "center": [float(c.Center[0]), float(c.Center[1])]})
        elif name in ("AcDbPolyline", "AcDb2dPolyline"):
            p = _cast(o, "IAcadLWPolyline")
            co = list(p.Coordinates)
            out.append({"op": "lwpolyline", "layer": p.Layer, "length": float(p.Length),
                        "area": float(p.Area), "closed": bool(p.Closed),
                        "pts": [[co[j], co[j + 1]] for j in range(0, len(co) - 1, 2)]})
        elif name == "AcDbLine":
            ln = _cast(o, "IAcadLine")
            sp, ep = ln.StartPoint, ln.EndPoint
            out.append({"op": "line", "layer": ln.Layer, "length": float(ln.Length),
                        "a": [float(sp[0]), float(sp[1])], "b": [float(ep[0]), float(ep[1])]})
        else:
            out.append({"op": name, "layer": o.Layer, "unsupported": True})
    return out


def _match(exp, act, tol=TOL):
    """期望图元与实际图元是否同一条(类型 + 图层 + 几何)"""
    if exp["op"] != act["op"] or exp["layer"] != act["layer"]:
        return False
    if exp["op"] == "circle":
        return (_near(exp["expect"]["radius"], act["radius"], tol)
                and _near(exp["expect"]["center"][0], act["center"][0], tol)
                and _near(exp["expect"]["center"][1], act["center"][1], tol))
    if exp["op"] == "line":
        return (_near(exp["expect"]["length"], act["length"], tol)
                and _near(exp["a"][0], act["a"][0], tol) and _near(exp["a"][1], act["a"][1], tol)
                and _near(exp["b"][0], act["b"][0], tol) and _near(exp["b"][1], act["b"][1], tol))
    return (_near(exp["expect"]["length"], act["length"], tol)
            and _near(exp["expect"]["area"], act["area"], 1e-6)
            and act["closed"] == exp["closed"] and len(act["pts"]) == len(exp["pts"]))


def _want_text(e):
    x = e["expect"]
    if e["op"] == "circle":
        return "R=%.4f C=(%.4f,%.4f)" % (x["radius"], x["center"][0], x["center"][1])
    if e["op"] == "line":
        return "L=%.4f" % x["length"]
    return "Length=%.4f Area=%.4f Closed=%s" % (x["length"], x["area"], e["closed"])


def _act_text(a):
    if a.get("unsupported"):
        return "%s(非本流水线图元)" % a["op"]
    if a["op"] == "circle":
        return "R=%.4f C=(%.4f,%.4f)" % (a["radius"], a["center"][0], a["center"][1])
    if a["op"] == "line":
        return "L=%.4f" % a["length"]
    return "Length=%.4f Area=%.4f Closed=%s" % (a["length"], a["area"], a["closed"])


def verify(snap, ents, tol=TOL):
    """期望图元集合 vs 实际图元集合: 一一配对, 多余/缺失都算失败"""
    rows, ok, used = [], True, set()
    for e in ents:
        hit = next((j for j, a in enumerate(snap) if j not in used and _match(e, a, tol)), None)
        if hit is None:
            rows.append((e["tag"], _want_text(e), "在图中找不到", False))
            ok = False
        else:
            used.add(hit)
            rows.append((e["tag"], _want_text(e), _act_text(snap[hit]), True))
    extra = [snap[j] for j in range(len(snap)) if j not in used]
    if len(snap) != len(ents):
        rows.append(("图元数量", "期望 %d" % len(ents), "实际 %d" % len(snap), not extra))
    for a in extra:
        rows.append(("多余图元", "-", _act_text(a), False))
        ok = False
    return ok, rows


def magenta_length(snap, ents, tol=TOL):
    """作业要的那个数: 洋红图层的闭合轮廓周长"""
    for e in ents:
        if e["layer"] == MAGENTA_LAYER and e["op"] == "lwpolyline":
            for a in snap:
                if _match(e, a, tol):
                    return a["length"]
    return None


# ============ 5. CLI ============

def main(argv=None):
    ap = argparse.ArgumentParser(description="结构化规格 -> AutoCAD 落图 + 回读校验")
    ap.add_argument("--spec", required=True, help="规格 JSON 文件")
    ap.add_argument("--dwg", help="另存路径(默认用 spec.output.dwg)")
    ap.add_argument("--clear", action="store_true", help="允许清空模型空间后重画")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的 dwg 文件")
    ap.add_argument("--launch", action="store_true", help="CAD 未运行时由脚本拉起")
    ap.add_argument("--dry-run", action="store_true", help="只校验+编译自检, 不连 CAD")
    ap.add_argument("--verify-only", action="store_true", help="不落图, 只回读校验当前模型空间")
    args = ap.parse_args(argv)

    try:
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
    except Exception as e:
        print("[FAIL] 规格文件读取失败: %s" % e)
        return 2

    try:
        notes = validate_spec(spec)
        ents = compile_spec(spec)
    except SpecError as e:
        print("[FAIL] 规格非法: %s" % e)
        return 2

    print("== 规格校验 ==")
    for n in notes:
        print("  [NOTE] %s" % n)
    print("  图元 %d 个: %s" % (len(ents), ", ".join(e["tag"] for e in ents)))

    outline = ents[0]
    w = float(spec["part"]["outline"]["w"])
    h = float(spec["part"]["outline"]["h"])
    r = float(spec["part"]["outline"].get("r", 0))
    formula = 2 * (w - 2 * r) + 2 * (h - 2 * r) + (2 * math.pi * r if r else 0)
    print("== 编译自检(两条独立算路) ==")
    print("  几何积分: 周长=%.4f 面积=%.4f" % (outline["expect"]["length"], outline["expect"]["area"]))
    print("  闭式公式: 2(w-2r)+2(h-2r)+2*pi*r = %.4f" % formula)
    if not _near(outline["expect"]["length"], formula):
        print("[FAIL] 两条算路不一致")
        return 2
    print("  [OK] 一致")

    if args.dry_run:
        print("== dry-run: 未连接 CAD ==")
        return 0

    try:
        app = attach(args.launch)
    except SpecError as e:
        print("[FAIL] %s" % e)
        return 3

    if not args.verify_only:
        try:
            doc = draw(app, ents, clear=args.clear)
        except SpecError as e:
            print("[FAIL] %s" % e)
            return 3
        fit_view(doc, ents)
        target = args.dwg or spec.get("output", {}).get("dwg")
        if target:
            target = os.path.abspath(target)
            cur = getattr(doc, "FullName", "") or ""
            if os.path.exists(target) and os.path.abspath(cur).lower() != target.lower() \
                    and not args.force:
                print("[FAIL] %s 已存在; 要覆盖请加 --force" % target)
                return 3
            if os.path.abspath(cur).lower() == target.lower():
                doc.Save()
            else:
                doc.SaveAs(target)
            print("== 已保存 ==\n  %s" % target)

    doc = app.ActiveDocument
    snap = snapshot(doc.ModelSpace)
    ok, rows = verify(snap, ents)
    print("== 回读校验(期望 vs CAD 实际) ==")
    width = max(len(r[0]) for r in rows) + 2
    print("  %-*s %-34s %s" % (width, "图元", "期望", "CAD 实测"))
    for tag, want, detail, good in rows:
        print("  [%s] %-*s %-34s %s" % ("OK" if good else "FAIL", width, tag, want, detail))
    val = magenta_length(snap, ents)
    exp_len = ents[0]["expect"]["length"]
    print("== 结论 ==")
    print("  洋红色线条周长: 期望 %.4f mm | CAD 实测 %s"
          % (exp_len, "%.4f mm" % val if val is not None else "未找到洋红轮廓"))
    print("  [%s] 回读校验 %d/%d 项匹配"
          % ("PASS" if ok else "FAIL", sum(1 for r in rows if r[3]), len(rows)))
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
