# -*- coding: utf-8 -*-
"""
自然语言 -> 结构化规格(JSON)。模型只负责"把话说成参数", 不产出任何可执行代码。

对接任意 OpenAI 兼容接口, 用环境变量配置:
  CAD_LLM_BASE_URL  默认 https://api.openai.com/v1
  CAD_LLM_API_KEY   必填
  CAD_LLM_MODEL     默认 gpt-4o-mini

用法:
  python nl2spec.py --text "40x40板, R4圆角, 四角4个Ø4孔与圆角同心, 中心正六边形内切圆Ø20,
                             中心Ø20与Ø10两个圆, 画中心线" --out spec.json
模型输出会被 cad_ai.validate_spec 硬校验; 不合格则回喂错误重试一次。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import cad_ai

SCHEMA_DOC = """{
  "part": {
    "outline":  {"type":"rounded_rect", "w":40, "h":40, "r":4, "center":[0,0]},
    "holes":    [{"diameter":4, "origin":"outline_fillet_centers"}],
    "polygons": [{"type":"regular","sides":6,"across_flats":20,"vertex_up":true,"center":[0,0]}],
    "circles":  [{"diameter":20,"center":[0,0]}, {"diameter":10,"center":[0,0]}],
    "centerlines": {"axis_extent": 26, "diagonals":true, "diagonal_extent":29}
  },
  "output": {"dwg": "C:/path/to/out.dwg"},
  "comment": "自由文本"
}"""

SYSTEM = """你是 CAD 参数解析器, 把用户的中文描述翻译成同构的 JSON 规格。
规则:
1. 只输出 JSON 本身, 不要解释、不要 Markdown 代码块、不要产生任何 LISP 或程序代码。
2. 严格只用下面出现的字段名, 不得新增字段(未知字段会被校验器拒绝):
%s
3. 尺寸一律用毫米, 直径写 diameter, 半径写 r; 坐标写 [x, y]。
4. 描述里没提到的可选字段直接省略, 不要猜造。
5. 孔位用 origin 表达: "与圆角同心" -> "outline_fillet_centers"; 给了坐标 -> "centers" + centers 数组。
6. 正多边形: 给对边距用 across_flats, 给外接圆半径用 circumradius, 二者只能有一个。
7. centerlines.axis_extent 与 diagonal_extent 都是"半长"(从中点到端点, 单位 mm), 不是总长;
   中心线应比轮廓略长(超出量几毫米量级), 不要给成远超板面尺寸的数。""" % SCHEMA_DOC


def chat(text, base_url, api_key, model, timeout=120):
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": text}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def extract_json(text):
    """模型偶尔会包 Markdown 围栏或加解释, 这里剥出第一个完整 JSON 对象"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    start = s.find("{")
    if start < 0:
        raise ValueError("模型输出里没有 JSON 对象")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(s[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("JSON 花括号不配对")


def nl_to_spec(text, base_url=None, api_key=None, model=None, attempts=2):
    base_url = base_url or os.environ.get("CAD_LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = api_key or os.environ.get("CAD_LLM_API_KEY", "")
    model = model or os.environ.get("CAD_LLM_MODEL", "gpt-4o-mini")
    if not api_key:
        raise SystemExit("[FAIL] 未配置 CAD_LLM_API_KEY (环境变量), 无法调用模型")

    prompt, last = text, None
    for attempt in range(1, attempts + 1):
        raw = chat(prompt, base_url, api_key, model)
        try:
            spec = extract_json(raw)
            notes = cad_ai.validate_spec(spec)       # 硬校验 + 语义提示
            ents = cad_ai.compile_spec(spec)         # 能编译成图元才算真的可用
            return spec, notes, ents
        except Exception as e:
            last = e
            print("[WARN] 第 %d 次输出未通过校验: %s -> 回喂错误重试" % (attempt, e),
                  file=sys.stderr)
            prompt = ("你上一次的输出无法通过校验: %s\n"
                      "原始输入:\n%s\n请重新只输出修正后的 JSON。" % (e, text))
    raise SystemExit("[FAIL] 模型输出 %d 次都未通过校验: %s" % (attempts, last))


def main(argv=None):
    ap = argparse.ArgumentParser(description="自然语言 -> CAD 规格 JSON")
    ap.add_argument("--text", help="自然语言描述")
    ap.add_argument("--file", help="从文件读取描述")
    ap.add_argument("--out", default="spec.json", help="输出规格文件路径")
    ap.add_argument("--show", action="store_true", help="把 JSON 打到 stdout")
    args = ap.parse_args(argv)

    text = args.text
    if not text and args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    if not text:
        text = sys.stdin.read()
    if not text.strip():
        raise SystemExit("[FAIL] 没有输入描述")

    spec, notes, ents = nl_to_spec(text)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    print("[OK] 生成规格 %s: 图元 %d 个" % (args.out, len(ents)))
    for n in notes:
        print("  [NOTE] %s" % n)
    if args.show:
        print(json.dumps(spec, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())