#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wave_gui.py - dual_color_wave 的本地网页界面（只用 Python 标准库，不需要 tkinter）

    python wave_gui.py
    python dual_color_wave.py --gui

启动后会自动在浏览器打开 http://127.0.0.1:<端口>/，
选择模型 / 灰度图、调参数、点「生成」，日志和波形预览实时显示。
服务只监听 127.0.0.1，不对外网开放。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dual_color_wave import DEFAULT_PARAMS, generate

STATE = {
    "running": False,
    "logs": [],
    "result": None,
    "error": None,
    "out_dir": None,
}
LOCK = threading.Lock()
ALLOWED_FILES = {
    "preview_wave.png", "demo_gray.png", "demo_cylinder.stl",
    "wave_white.stl", "wave_black.stl", "run_info.json",
}


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>双色波浪混色 · 模型生成器</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --line:#e4e7ec; --ink:#1f2430; --sub:#6b7280;
          --acc:#2f6feb; --acc2:#eef3ff; --ok:#16845b; --err:#c0392b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  .wrap { max-width:1180px; margin:0 auto; padding:28px 22px 60px; }
  h1 { font-size:22px; margin:0 0 4px; letter-spacing:.2px; }
  .lead { color:var(--sub); margin:0 0 22px; font-size:13px; }
  .grid { display:grid; grid-template-columns:minmax(360px,1fr) minmax(360px,1fr); gap:18px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:18px 20px; box-shadow:0 1px 2px rgba(16,24,40,.03); }
  h2 { font-size:14px; margin:0 0 14px; padding-bottom:10px; border-bottom:1px solid var(--line); }
  label { display:block; font-size:12px; color:var(--sub); margin:10px 0 4px; }
  input[type=text], input[type=number], select {
      width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px;
      background:#fff; color:var(--ink); font:inherit; outline:none; }
  input:focus, select:focus { border-color:var(--acc); box-shadow:0 0 0 3px var(--acc2); }
  input[type=file] { width:100%; font-size:12px; color:var(--sub); }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
  .checks { display:flex; gap:18px; flex-wrap:wrap; margin-top:12px; }
  .checks label { display:flex; align-items:center; gap:6px; margin:0; color:var(--ink); font-size:13px; }
  .hint { font-size:11px; color:var(--sub); margin-top:5px; }
  button { background:var(--acc); color:#fff; border:0; border-radius:9px; padding:11px 18px;
           font:inherit; font-weight:600; cursor:pointer; margin-top:18px; }
  button:hover { background:#245ccb; }
  button:disabled { opacity:.55; cursor:not-allowed; }
  .btn2 { background:#eef1f6; color:var(--ink); font-weight:500; padding:8px 12px; margin:0; }
  pre { background:#f8f9fb; border:1px solid var(--line); border-radius:9px; padding:12px;
        height:210px; overflow:auto; font:12px/1.5 Consolas,"Courier New",monospace;
        white-space:pre-wrap; color:#333a48; }
  .kpis { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:12px; }
  .kpi { background:var(--acc2); border-radius:9px; padding:8px 12px; min-width:96px; }
  .kpi b { display:block; font-size:17px; }
  .kpi span { font-size:11px; color:var(--sub); }
  .status { font-size:12px; margin-top:10px; color:var(--sub); }
  .ok { color:var(--ok); } .err { color:var(--err); }
  img { max-width:100%; border:1px solid var(--line); border-radius:9px; display:block; }
  a { color:var(--acc); }
  .foot { color:var(--sub); font-size:11px; margin-top:26px; }
  .steps { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:18px; }
  .step { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .step b { display:block; margin-bottom:5px; font-size:13px; }
  .step span { color:var(--sub); font-size:12px; }
  .num { display:inline-flex; width:22px; height:22px; border-radius:50%; background:var(--acc);
         color:#fff; align-items:center; justify-content:center; font-size:12px; margin-right:7px; }
  .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:18px; }
  .toolbar button { margin-top:0; }
  .toolbar .btn2 { padding:10px 14px; }
  .demo-note { background:var(--acc2); border-radius:9px; padding:9px 12px; font-size:12px;
               color:#245ccb; margin-top:14px; }
  @media (max-width:820px) { .grid { grid-template-columns:1fr; } .steps { grid-template-columns:1fr 1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <h1>双色波浪混色 · 模型生成器</h1>
  <p class="lead">灰度图 → 交替挤出白 / 黑波浪轮廓 → 两个 STL（Bambu Studio 多零件对象）</p>

  <div class="steps">
    <div class="step"><b><span class="num">1</span>准备素材</b>
      <span>一个封闭 3D 模型（STL/OBJ）+ 一张黑白图案。没有素材就点「一键测试」。</span></div>
    <div class="step"><b><span class="num">2</span>选映射模式</b>
      <span>圆柱 / 花瓶 / 雕像用柱面；方形画板 / 浮雕用平面。</span></div>
    <div class="step"><b><span class="num">3</span>开始生成</b>
      <span>右侧看日志、波形预览，并下载 wave_white.stl 与 wave_black.stl。</span></div>
    <div class="step"><b><span class="num">4</span>切片打印</b>
      <span>两个 STL 一起拖进 Bambu Studio → 单个多零件对象 → 分别指定白 / 黑耗材。</span></div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>输入与输出</h2>
      <form id="form">
        <label>3D 模型文件（STL / OBJ / 3MF，留空自动生成测试圆柱）</label>
        <input type="file" name="model_file" id="model_file">
        <label>或直接填写模型绝对路径</label>
        <input type="text" name="model" placeholder="例如 E:\models\vase.stl">

        <label>灰度图（留空自动生成测试图）</label>
        <input type="file" name="image_file" id="image_file">
        <label>或直接填写图片绝对路径</label>
        <input type="text" name="image" placeholder="例如 E:\images\portrait.png">

        <label>输出目录</label>
        <input type="text" name="output" value="output_wave">

        <div class="row">
          <div><label>测试圆柱半径 mm</label><input type="number" step="any" name="demo_radius" value="20"></div>
          <div><label>测试圆柱高度 mm</label><input type="number" step="any" name="demo_height" value="20"></div>
        </div>

        <h2 style="margin-top:22px">贴图映射</h2>
        <div class="row">
          <div><label>映射模式</label>
            <select name="mapping">
              <option value="cylindrical">cylindrical 柱面（花瓶 / 雕像）</option>
              <option value="planar">planar 平面（画板 / 浮雕）</option>
            </select></div>
          <div><label>平面模式 u 轴</label>
            <select name="planar_axis"><option value="x">X</option><option value="y">Y</option></select></div>
        </div>
        <div class="row">
          <div><label>柱面旋转角（度）</label><input type="number" step="any" name="angle_offset" value="0"></div>
          <div><label>相位递进 twist（弧度）</label><input type="number" step="any" name="twist" value="0"></div>
        </div>
        <div class="checks">
          <label><input type="checkbox" name="flip_u"> 水平镜像</label>
          <label><input type="checkbox" name="flip_v"> 垂直镜像</label>
          <label><input type="checkbox" name="demo_check"> 强制使用内置测试模型/图</label>
        </div>

        <h2 style="margin-top:22px">打印工艺参数</h2>
        <div class="row3">
          <div><label>层高 mm</label><input type="number" step="any" name="layer_height" value="0.2"></div>
          <div><label>每色层数</label><input type="number" step="1" name="layers_per_color" value="2"></div>
          <div><label>波长 mm</label><input type="number" step="any" name="wavelength" value="1.6"></div>
        </div>
        <div class="row3">
          <div><label>最大振幅 mm</label><input type="number" step="any" name="max_amplitude" value="0.42"></div>
          <div><label>基底振幅 mm</label><input type="number" step="any" name="min_amplitude" value="0.05"></div>
          <div><label>壁厚 mm</label><input type="number" step="any" name="wall_thickness" value="0.85"></div>
        </div>
        <div class="row3">
          <div><label>采样步长 mm</label><input type="number" step="any" name="sample_step" value="0.2"></div>
          <div><label>波峰下垂 mm</label><input type="number" step="any" name="droop" value="0"></div>
          <div><label>色段搭接 mm</label><input type="number" step="any" name="overlap" value="0.02"></div>
        </div>
        <div class="checks"><label><input type="checkbox" name="preview" checked> 生成波形预览图</label></div>

        <div class="toolbar">
          <button type="submit" id="run">开始生成</button>
          <button type="button" class="btn2" id="demo_btn">一键测试（内置模型+图案）</button>
          <button type="button" class="btn2" id="open_btn">打开输出目录</button>
          <button type="button" class="btn2" id="clear">清空日志</button>
        </div>
        <div class="status" id="status">待命</div>
      </form>
      <div class="demo-note">第一次用？直接点「一键测试」：自动生成测试圆柱和测试图案，约 1 秒出结果。</div>
      <p class="foot">提示：模型必须接近封闭实体；换料次数 = 色段数 − 1，AMS 单次换料约 1~2 分钟。</p>
    </div>

    <div class="card">
      <h2>运行日志</h2>
      <pre id="log">等待任务…</pre>
      <div id="kpis" class="kpis"></div>
      <div id="links" class="status"></div>
      <h2 style="margin-top:20px">波形预览</h2>
      <div id="preview"><span class="hint">生成后显示</span></div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let busy = false;

async function startGeneration() {
  if (busy) return;
  busy = true; $('run').disabled = true; $('status').textContent = '正在生成…';
  $('status').className = 'status';
  try {
    const r = await fetch('/run', { method:'POST', body:new FormData($('form')) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '提交失败');
    poll();
  } catch (err) {
    $('status').textContent = '提交失败：' + err.message;
    $('status').className = 'status err';
    busy = false; $('run').disabled = false;
  }
}

$('form').addEventListener('submit', e => { e.preventDefault(); startGeneration(); });

$('demo_btn').onclick = () => {
  document.querySelector('[name=demo_check]').checked = true;
  document.querySelector('[name=output]').value = 'output_demo';
  document.querySelector('[name=demo_height]').value = '12';
  $('log').textContent = '';
  startGeneration();
};

$('open_btn').onclick = async () => {
  const output = document.querySelector('[name=output]').value || 'output_wave';
  try {
    const r = await fetch('/open', { method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify({output}) });
    const j = await r.json();
    $('status').textContent = j.ok ? ('已打开输出目录：' + j.path)
                                   : ('打开失败：' + (j.error || ''));
  } catch (e) { $('status').textContent = '打开失败：' + e.message; }
};

$('clear').onclick = () => { $('log').textContent = ''; };

function poll() {
  const timer = setInterval(async () => {
    try {
      const j = await (await fetch('/status')).json();
      $('log').textContent = j.logs.join('\n') || '等待任务…';
      $('log').scrollTop = $('log').scrollHeight;
      if (!j.running) {
        clearInterval(timer); busy = false; $('run').disabled = false;
        if (j.error) {
          $('status').textContent = '失败：' + j.error;
          $('status').className = 'status err';
        } else {
          $('status').textContent = '完成 ✓';
          $('status').className = 'status ok';
          render(j.result);
        }
      }
    } catch (e) { /* 网络瞬断，下轮继续 */ }
  }, 600);
}

function render(s) {
  if (!s) return;
  $('kpis').innerHTML = [
    ['层数', s.layers], ['色段', s.color_blocks], ['换料次数', s.filament_changes],
    ['轮廓数', s.contours], ['耗时', s.elapsed_sec + ' s']
  ].map(([k,v]) => `<div class="kpi"><b>${v}</b><span>${k}</span></div>`).join('');
  const rows = Object.entries(s.outputs || {}).map(([n, o]) =>
    `<div><a href="/file?name=${n}.stl" download>${n}.stl</a> · ${o.faces.toLocaleString()} 面 · ${o.size_mb} MB · watertight=${o.watertight}</div>`).join('');
  $('links').innerHTML = rows + `<div style="margin-top:6px">输出目录：${s.output_dir}</div>`;
  if (s.preview) {
    $('preview').innerHTML = `<img src="/file?name=preview_wave.png&t=${Date.now()}">`;
  }
}

// 刷新页面后恢复上一次的日志与结果；若任务仍在跑则继续轮询
window.addEventListener('load', async () => {
  try {
    const j = await (await fetch('/status')).json();
    if (j.logs && j.logs.length) {
      $('log').textContent = j.logs.join('\n');
      $('log').scrollTop = $('log').scrollHeight;
    }
    if (j.running) {
      busy = true; $('run').disabled = true;
      $('status').textContent = '正在生成…';
      poll();
    } else if (j.result) {
      $('status').textContent = '完成 ✓';
      $('status').className = 'status ok';
      render(j.result);
    } else if (j.error) {
      $('status').textContent = '失败：' + j.error;
      $('status').className = 'status err';
    }
  } catch (e) { /* 忽略 */ }
});
</script>
</body>
</html>
"""


def _parse_multipart(handler):
    """
    解析 multipart/form-data，返回 (字段 dict, 上传文件 dict)。
    直接按 bytes 边界切分，避免 email 模块把二进制 STL/PNG 当文本解码而损坏数据。
    """
    ctype = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(length)
    fields, files = {}, {}
    if "boundary=" not in ctype:
        fields = {k: v[-1] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        return fields, files

    boundary = ctype.split("boundary=", 1)[1].split(";", 1)[0].strip().strip('"')
    delim = b"--" + boundary.encode("ascii", "ignore")
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"--"):                       # 结束标记 --boundary--
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        head_txt = head.decode("utf-8", "replace")
        m_name = re.search(r'name="([^"]*)"', head_txt)
        if not m_name:
            continue
        name = m_name.group(1)
        m_file = re.search(r'filename="([^"]*)"', head_txt)
        if m_file is not None:
            fname = os.path.basename(m_file.group(1).replace("\\", "/"))
            if fname and data:
                files[name] = (fname, data)
        else:
            fields[name] = data.decode("utf-8", "replace").strip()
    return fields, files


def _coerce(fields):
    p = dict(DEFAULT_PARAMS)
    floats = ["demo_radius", "demo_height", "layer_height", "wavelength",
              "max_amplitude", "min_amplitude", "wall_thickness", "sample_step",
              "angle_offset", "twist", "droop", "overlap"]
    ints = ["layers_per_color"]
    strs = ["model", "image", "output", "mapping", "planar_axis"]
    for k in floats:
        if k in fields and fields[k] != "":
            p[k] = float(fields[k])
    for k in ints:
        if k in fields and fields[k] != "":
            p[k] = int(float(fields[k]))
    for k in strs:
        if k in fields:
            p[k] = fields[k].strip() or None
    p["flip_u"] = "flip_u" in fields
    p["flip_v"] = "flip_v" in fields
    p["preview"] = "preview" in fields
    demo = "demo_check" in fields
    if demo:
        p["model"] = None
        p["image"] = None
    return p


def _job(params):
    def log(msg):
        with LOCK:
            STATE["logs"].append(str(msg))

    try:
        res = generate(params, log=log)
        with LOCK:
            STATE["result"] = res
            STATE["out_dir"] = res.get("output_dir")
    except Exception as exc:
        with LOCK:
            STATE["error"] = f"{type(exc).__name__}: {exc}"
            STATE["logs"].append(traceback.format_exc())
    finally:
        with LOCK:
            STATE["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, data, ctype="application/json; charset=utf-8"):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if u.path == "/status":
            with LOCK:
                snap = {k: (list(v) if isinstance(v, list) else v)
                        for k, v in STATE.items()}
            self._send(200, json.dumps(snap, ensure_ascii=False))
            return
        if u.path == "/file":
            name = parse_qs(u.query).get("name", [""])[0]
            with LOCK:
                out_dir = STATE["out_dir"]
            if not out_dir or name not in ALLOWED_FILES:
                self._send(404, json.dumps({"error": "not found"}))
                return
            path = os.path.realpath(os.path.join(out_dir, name))
            if not path.startswith(os.path.realpath(out_dir) + os.sep) or not os.path.isfile(path):
                self._send(404, json.dumps({"error": "not found"}))
                return
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path == "/open":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                with LOCK:
                    state_dir = STATE["out_dir"]
                out = payload.get("output") or state_dir or "output_wave"
                path = os.path.abspath(str(out))
                os.makedirs(path, exist_ok=True)
                if os.name == "nt":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
                self._send(200, json.dumps({"ok": True, "path": path}, ensure_ascii=False))
            except Exception as exc:
                self._send(400, json.dumps({"ok": False, "error": str(exc)}))
            return
        if self.path != "/run":
            self._send(404, json.dumps({"error": "not found"}))
            return
        with LOCK:
            if STATE["running"]:
                self._send(409, json.dumps({"ok": False, "error": "已有任务正在运行"}))
                return
        try:
            fields, files = _parse_multipart(self)
            params = _coerce(fields)
            out_dir = os.path.abspath(params["output"] or "output_wave")
            os.makedirs(os.path.join(out_dir, "uploads"), exist_ok=True)
            if "model_file" in files and files["model_file"][0]:
                name, data = files["model_file"]
                path = os.path.join(out_dir, "uploads", name)
                with open(path, "wb") as fh:
                    fh.write(data)
                params["model"] = path
            if "image_file" in files and files["image_file"][0]:
                name, data = files["image_file"]
                path = os.path.join(out_dir, "uploads", name)
                with open(path, "wb") as fh:
                    fh.write(data)
                params["image"] = path
            with LOCK:
                STATE.update(running=True, logs=[], result=None, error=None,
                             out_dir=out_dir)
            threading.Thread(target=_job, args=(params,), daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
        except Exception as exc:
            self._send(400, json.dumps({"ok": False, "error": str(exc)}))


def launch(open_browser: bool = True, port: int = 8765) -> None:
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:                                   # 端口被占用则换随机端口
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print("双色波浪工具 UI 已启动:", url)
    print("在浏览器里上传模型和图案即可使用；按 Ctrl+C 退出。")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="双色波浪混色工具的本地 UI 界面")
    ap.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()
    launch(open_browser=not args.no_browser, port=args.port)
