#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dual_color_wave.py - 双色波浪混色 3D 打印模型生成器

把任意封闭 3D 模型 + 一张灰度图，转换成一对可直接拖进 Bambu Studio 的
STL（wave_white.stl / wave_black.stl）。侧面按层交替挤出两种颜色，
黑白两色的轮廓用「相位相差半个周期」的正弦波沿外法线调制，
波峰外凸的幅度由灰度图控制，从远处看就混合出灰阶图像。

核心管线：
  1. 按「色段」(layers_per_color 层) 取一个水平截面轮廓
  2. 等弧长重采样 + 起点按极角对齐（保证波峰沿同一条母线排列）
  3. 柱面 / 平面 UV 映射，采样灰度 I
  4. 正弦法向调制 + 可选相位递进 (twist) / 波峰下垂 (droop)
  5. 厚壁扫掠生成 watertight 环形棱柱，按颜色合并导出 binary STL

用法：
  python dual_color_wave.py --demo                 # 10 秒验证：自动生成测试圆柱 + 测试灰度图
  python dual_color_wave.py --model a.stl --image a.png --mapping planar
  python dual_color_wave.py --help                # 全部参数
  python dual_color_wave.py --gui                 # 打开本地网页界面
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Callable

import numpy as np
import trimesh
from shapely.geometry import Polygon, LineString
from shapely.ops import polygonize, unary_union
from PIL import Image, ImageDraw, ImageFont

__version__ = "1.0"

DEFAULT_PARAMS = {
    "model": None,                 # STL/OBJ/3MF；None = 自动生成测试圆柱
    "image": None,                 # 灰度图；None = 自动生成测试图
    "output": "output_wave",
    "demo_radius": 20.0,
    "demo_height": 20.0,
    "layer_height": 0.2,
    "layers_per_color": 2,
    "wavelength": 1.6,
    "max_amplitude": 0.42,
    "min_amplitude": 0.05,
    "wall_thickness": 0.85,
    "sample_step": 0.2,
    "mapping": "cylindrical",      # cylindrical | planar
    "planar_axis": "x",            # planar 模式下 u 由 x 还是 y 决定
    "angle_offset": 0.0,           # 柱面贴图旋转角，单位「度」
    "flip_u": False,
    "flip_v": False,
    "twist": 0.0,                  # 每个色段相位递进量（弧度），0=竖直编织，0.3 左右=斜纹
    "droop": 0.0,                  # 波峰沿 Z 下垂量 (mm)，0=关闭
    "overlap": 0.02,               # 相邻色段的 Z 向搭接量 (mm)
    "preview": True,
}

MM_PER_INCH = 1.0


# ---------------------------------------------------------------- 基础工具

def _say(log: Callable[[str], None] | None, msg: str) -> None:
    (log or print)(msg)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def load_grayscale_image(path: str) -> np.ndarray:
    """加载图片并归一化到 [0,1]，0=黑 1=白。翻转 Y 使 v=0 对应模型底部。"""
    img = Image.open(path).convert("L")
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return np.flipud(arr)


def make_demo_image(path: str, width: int = 1024, height: int = 512) -> str:
    """生成一张适合柱面映射的测试灰度图：横向渐变 + 反相圆 + 斜条纹。"""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    u = xx / (width - 1)
    v = yy / (height - 1)

    grad = u.copy()
    grad += 0.10 * np.sin(u * 34.0)                       # 细密竖条
    band = np.clip((v - 0.28) / 0.44, 0.0, 1.0)          # 中部横向带
    img = grad * (0.35 + 0.65 * band)

    # 一个反相大圆，便于在圆柱侧面看到清晰的黑白边界
    cx, cy, r = width * 0.68, height * 0.52, height * 0.34
    circle = ((xx - cx) ** 2 + (yy - cy) ** 2) < r ** 2
    img[circle] = 1.0 - img[circle]

    # 上下各留一条纯黑 / 纯白参考带
    img[: int(height * 0.08), :] = 0.0
    img[int(height * 0.92):, :] = 1.0

    arr = np.clip(img * 255.0, 0, 255).astype(np.uint8)
    out = Image.fromarray(arr)
    draw = ImageDraw.Draw(out)
    draw.text((16, height // 2 - 6), "DUAL COLOR WAVE  |  BLACK <--> WHITE", fill=200)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    out.save(path)
    return path


def make_demo_model(path: str, radius: float = 20.0, height: float = 20.0) -> str:
    """没有输入模型时，生成一个标准测试圆柱（杯/笔筒）。"""
    mesh = trimesh.creation.cylinder(radius=radius, height=height, sections=192)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    mesh.export(path, file_type="stl")
    return path


def resample_contour(coords: np.ndarray, step: float = 0.2,
                     align_angle: float = 0.0):
    """
    闭合 2D 轮廓 -> 等弧长重采样点 + 单位外法线 + 累计弧长。

    关键：把起点统一旋转到指定极角（默认 +X 轴），否则不同层的波峰
    会随机错位，打印出来是乱纹而不是竖直编织。全程 numpy 向量化。
    """
    pts = np.asarray(coords, dtype=np.float64)
    if np.linalg.norm(pts[0] - pts[-1]) < 1e-9:
        pts = pts[:-1]
    if len(pts) < 3:
        return None

    # 统一为逆时针（逆时针时外法线 = (ty, -tx)）
    if not Polygon(pts).exterior.is_ccw:
        pts = pts[::-1].copy()

    center = pts.mean(axis=0)
    theta = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])
    delta = np.abs(np.angle(np.exp(1j * (theta - align_angle))))
    start = int(np.argmin(delta))
    pts = np.roll(pts, -start, axis=0)

    seg = np.roll(pts, -1, axis=0) - pts
    seg_len = np.linalg.norm(seg, axis=1)
    cum = np.concatenate(([0.0], np.cumsum(seg_len)))
    total = float(cum[-1])
    if total < 1e-6:
        return None

    s = np.arange(0.0, total, max(step, 1e-4), dtype=np.float64)
    if len(s) < 16:
        s = np.linspace(0.0, total, 16, endpoint=False)
    idx = np.searchsorted(cum, s, side="right") - 1
    idx = np.clip(idx, 0, len(seg) - 1)
    t = (s - cum[idx]) / np.maximum(seg_len[idx], 1e-12)
    sampled = pts[idx] + seg[idx] * t[:, None]

    tang = np.roll(sampled, -1, axis=0) - np.roll(sampled, 1, axis=0)
    tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-12)
    normals = np.column_stack([tang[:, 1], -tang[:, 0]])
    return sampled, normals, s, total


def polygons_from_path2d(planar) -> list:
    """
    不依赖 networkx 的截面闭合兜底：把 Path2D 里的离散线段用 shapely
    polygonize 组装成多边形。trimesh 5.x 的 polygons_full 需要 networkx，
    这里保证只装 numpy/trimesh/shapely/pillow 也能正常出图。
    """
    lines = []
    for entity in planar.entities:
        try:
            pts = np.asarray(entity.discrete(planar.vertices), dtype=np.float64)
        except Exception:
            continue
        if len(pts) >= 2 and np.linalg.norm(pts[0] - pts[-1]) > 1e-9:
            lines.append(LineString(pts))
    if not lines:
        return []
    try:
        merged = unary_union(lines)
        return [p for p in polygonize(merged) if p.area > 1e-6]
    except Exception:
        return []


def sample_texture(tex: np.ndarray, pts3d: np.ndarray, bounds: np.ndarray,
                   mapping: str, angle_offset_deg: float = 0.0,
                   planar_axis: str = "x", flip_u: bool = False,
                   flip_v: bool = False) -> np.ndarray:
    """把 3D 顶点映射到灰度图，返回每点的灰度 I in [0,1]。"""
    h, w = tex.shape
    xyz = np.asarray(pts3d, dtype=np.float64)
    x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    lo, hi = bounds[0], bounds[1]

    v = (z - lo[2]) / max(hi[2] - lo[2], 1e-6)

    if mapping == "planar":
        axis = x if planar_axis == "x" else y
        k = 0 if planar_axis == "x" else 1
        u = (axis - lo[k]) / max(hi[k] - lo[k], 1e-6)
    else:
        cx = (lo[0] + hi[0]) / 2.0
        cy = (lo[1] + hi[1]) / 2.0
        ang = np.arctan2(y - cy, x - cx) + math.radians(angle_offset_deg)
        u = (ang % (2.0 * math.pi)) / (2.0 * math.pi)

    if flip_u:
        u = 1.0 - u
    if flip_v:
        v = 1.0 - v

    px = np.clip((u * w).astype(np.int64), 0, w - 1)
    py = np.clip((v * h).astype(np.int64), 0, h - 1)
    return tex[py, px].astype(np.float64)


# ---------------------------------------------------------------- 网格构建

def build_segment_mesh(outer_xy: np.ndarray, inner_xy: np.ndarray,
                       z_bot: float, z_top: float,
                       z_shift: np.ndarray | None = None) -> trimesh.Trimesh:
    """
    把一个色段构建成 watertight 的厚壁环形棱柱。
    4 圈顶点：底外 / 底内 / 顶外 / 顶内；4 组面：外壁 / 内壁 / 顶盖 / 底盖。
    与「每层单独封顶」相比，段内没有内部面，切片软件不会生成多余的实心顶底层。
    """
    n = len(outer_xy)
    if z_shift is None:
        z_shift = np.zeros(n, dtype=np.float64)

    v_bo = np.column_stack([outer_xy, np.full(n, z_bot) + z_shift])
    v_bi = np.column_stack([inner_xy, np.full(n, z_bot)])
    v_to = np.column_stack([outer_xy, np.full(n, z_top) + z_shift])
    v_ti = np.column_stack([inner_xy, np.full(n, z_top)])
    vertices = np.vstack([v_bo, v_bi, v_to, v_ti])

    i = np.arange(n)
    j = np.roll(i, -1)
    bo, bo_j = i, j
    bi, bi_j = n + i, n + j
    to, to_j = 2 * n + i, 2 * n + j
    ti, ti_j = 3 * n + i, 3 * n + j

    faces = np.vstack([
        np.column_stack([bo, bo_j, to_j]),      # 外壁
        np.column_stack([bo, to_j, to]),
        np.column_stack([bi, ti_j, bi_j]),      # 内壁
        np.column_stack([bi, ti, ti_j]),
        np.column_stack([to, to_j, ti_j]),      # 顶盖
        np.column_stack([to, ti_j, ti]),
        np.column_stack([bo, bi_j, bo_j]),      # 底盖
        np.column_stack([bo, bi, bi_j]),
    ]).astype(np.int64)

    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


# ---------------------------------------------------------------- 主流程

def generate(params: dict | None = None, log: Callable[[str], None] | None = None) -> dict:
    """执行完整管线，返回统计信息 dict。"""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    say = lambda m: _say(log, m)
    t0 = time.time()

    out_dir = os.path.abspath(str(p["output"]))
    os.makedirs(out_dir, exist_ok=True)

    # ---- 1. 模型 -----------------------------------------------------------
    if p["model"]:
        say(f"[1/5] 加载模型: {p['model']}")
        mesh = trimesh.load(str(p["model"]), force="mesh")
    else:
        demo_path = os.path.join(out_dir, "demo_cylinder.stl")
        make_demo_model(demo_path, float(p["demo_radius"]), float(p["demo_height"]))
        say(f"[1/5] 未指定模型，已生成测试圆柱: {demo_path}")
        mesh = trimesh.load(demo_path, force="mesh")

    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate([g for g in mesh.geometry.values()])

    bounds = mesh.bounds.copy()
    centre = (bounds[0][:2] + bounds[1][:2]) / 2.0
    mesh.apply_translation([-centre[0], -centre[1], -bounds[0][2]])
    bounds = mesh.bounds.copy()
    height = float(bounds[1][2] - bounds[0][2])
    say(f"      模型 {bounds[1][0]-bounds[0][0]:.1f} x "
        f"{bounds[1][1]-bounds[0][1]:.1f} x {height:.1f} mm, "
        f"watertight={mesh.is_watertight}")
    if not mesh.is_watertight:
        say("      注意：输入模型不是完全流形，切片轮廓可能不闭合；建议先做修复。")

    # ---- 2. 灰度图 ---------------------------------------------------------
    if p["image"]:
        say(f"[2/5] 加载灰度图: {p['image']}")
        tex = load_grayscale_image(str(p["image"]))
    else:
        demo_img = os.path.join(out_dir, "demo_gray.png")
        make_demo_image(demo_img)
        say(f"[2/5] 未指定图片，已生成测试灰度图: {demo_img}")
        tex = load_grayscale_image(demo_img)
    say(f"      纹理 {tex.shape[1]} x {tex.shape[0]} px, 映射模式={p['mapping']}")

    # ---- 3. 分色段切片 + 波浪调制 -------------------------------------------
    lh = float(p["layer_height"])
    per_color = max(1, int(p["layers_per_color"]))
    n_layers = max(1, int(math.floor(height / lh + 1e-6)))
    n_blocks = int(math.ceil(n_layers / per_color))
    say(f"[3/5] 切片：{n_layers} 层 / {n_blocks} 个色段 "
        f"(每 {per_color} 层换一次色，预计换料 {n_blocks - 1} 次)")
    if n_blocks - 1 > 60:
        say("      提示：换料次数偏多，AMS 单次换料约 1~2 分钟，建议加大 "
            "layers_per_color 或降低模型高度。")

    lam = max(float(p["wavelength"]), 1e-3)
    a_max = float(p["max_amplitude"])
    a_min = float(p["min_amplitude"])
    wall = float(p["wall_thickness"])
    step = float(p["sample_step"])
    twist = float(p["twist"])
    droop = float(p["droop"])
    overlap = max(0.0, float(p["overlap"]))

    white_parts, black_parts, records = [], [], []
    skipped = 0
    warned_polygon = False

    for b in range(n_blocks):
        z0 = b * per_color * lh
        z1 = min(z0 + per_color * lh, height)
        if z1 <= z0:
            break
        zc = (z0 + z1) / 2.0

        section = mesh.section(plane_origin=[0, 0, zc], plane_normal=[0, 0, 1])
        if section is None:
            skipped += 1
            continue
        # trimesh 5.x 用 to_2D，老版本用 to_planar
        to_2d = getattr(section, "to_2D", None) or getattr(section, "to_planar")
        planar, to_3d = to_2d()
        try:
            polys = list(planar.polygons_full)
        except Exception as exc:
            if not warned_polygon:
                say(f"      polygons_full 不可用 ({exc})，已自动改用 shapely 闭合轮廓")
                warned_polygon = True
            polys = polygons_from_path2d(planar)

        is_white = (b % 2 == 0)
        weight_of = (lambda I: I) if is_white else (lambda I: 1.0 - I)
        phase = (0.0 if is_white else math.pi) + twist * b
        zb = z0 - (overlap if b > 0 else 0.0)
        zt = z1 + (overlap if b < n_blocks - 1 else 0.0)

        for poly in polys:
            if poly.is_empty or poly.area < 1e-6:
                continue
            c2d = np.asarray(poly.exterior.coords, dtype=np.float64)
            if len(c2d) < 4:
                continue
            hom = np.column_stack([c2d, np.zeros(len(c2d)), np.ones(len(c2d))])
            world = (to_3d @ hom.T).T[:, :3]

            res = resample_contour(world[:, :2], step=step)
            if res is None:
                continue
            pts_xy, normals, arc_s, perimeter = res
            n = len(pts_xy)
            pts3d = np.column_stack([pts_xy, np.full(n, zc)])

            I = sample_texture(
                tex, pts3d, bounds, str(p["mapping"]),
                float(p["angle_offset"]), str(p["planar_axis"]),
                bool(p["flip_u"]), bool(p["flip_v"]),
            )
            weight = np.clip(weight_of(I), 0.0, 1.0)
            wave = 0.5 * (1.0 + np.sin(2.0 * math.pi * arc_s / lam + phase))
            disp = a_min + (a_max - a_min) * weight * wave

            outer = pts_xy + normals * disp[:, None]
            inner = pts_xy - normals * wall
            z_shift = -droop * weight * wave

            seg = build_segment_mesh(outer, inner, zb, zt, z_shift)
            (white_parts if is_white else black_parts).append(seg)
            records.append({
                "block": b, "white": is_white, "z0": z0, "z1": z1,
                "s": arc_s, "disp": disp, "outer": outer,
                "perimeter": perimeter,
            })

    if not white_parts and not black_parts:
        raise RuntimeError(f"没有生成任何轮廓（跳过 {skipped} 个空截面）。"
                           "请检查模型是否为封闭实体、层高是否过大。")
    if skipped:
        say(f"      跳过 {skipped} 个空截面")

    # ---- 4. 合并导出 -------------------------------------------------------
    say("[4/5] 合并网格并导出 binary STL ...")
    result = {}
    for name, parts in (("wave_white", white_parts), ("wave_black", black_parts)):
        if not parts:
            continue
        combined = trimesh.util.concatenate(parts) if len(parts) > 1 else parts[0]
        path = os.path.join(out_dir, name + ".stl")
        combined.export(path, file_type="stl")
        size_mb = os.path.getsize(path) / 1024.0 / 1024.0
        result[name] = {
            "path": path,
            "faces": int(len(combined.faces)),
            "parts": len(parts),
            "watertight": bool(combined.is_watertight),
            "winding_consistent": bool(combined.is_winding_consistent),
            "volume_mm3": float(combined.volume),
            "size_mb": round(size_mb, 2),
        }
        say(f"      {name}.stl  {len(combined.faces):,} 面  {size_mb:.2f} MB  "
            f"watertight={combined.is_watertight}  "
            f"winding={combined.is_winding_consistent}  "
            f"体积={combined.volume:,.0f} mm³")

    if not all(v["watertight"] and v["winding_consistent"] for v in result.values()):
        say("      警告：存在非流形或绕向不一致的网格，建议减小 sample_step "
            "或改用更规整的输入模型。")

    # ---- 5. 预览 + 参数记录 -------------------------------------------------
    preview_path = None
    if p["preview"] and records:
        preview_path = os.path.join(out_dir, "preview_wave.png")
        try:
            make_preview(preview_path, records, bounds, height, p)
            say(f"[5/5] 波形预览图: {preview_path}")
        except Exception as exc:
            say(f"[5/5] 预览图生成失败（不影响 STL）: {exc}")
            preview_path = None

    stats = {
        "version": __version__,
        "params": {k: (str(v) if isinstance(v, (np.floating, np.integer)) else v)
                   for k, v in p.items()},
        "model_height_mm": height,
        "layers": n_layers,
        "color_blocks": n_blocks,
        "filament_changes": n_blocks - 1,
        "contours": len(records),
        "elapsed_sec": round(time.time() - t0, 2),
        "outputs": result,
        "preview": preview_path,
        "output_dir": out_dir,
    }
    with open(os.path.join(out_dir, "run_info.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2)

    say(f"完成！耗时 {stats['elapsed_sec']} s。"
        "把两个 STL 一起拖入 Bambu Studio，选择「作为单个对象 / 多个零件加载」，"
        "再按零件分别指定耗材 1 和耗材 2。")
    return stats


# ---------------------------------------------------------------- 预览图

def make_preview(path: str, records: list, bounds: np.ndarray,
                 height: float, p: dict) -> str:
    """左：弧长×高度的波形展开图；右：相邻黑白两段的俯视轮廓叠加。"""
    W, H = 1240, 700
    img = Image.new("RGB", (W, H), (250, 250, 248))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    d.text((30, 18), "DUAL COLOR WAVE  /  unwrapped wave + top view", fill=(35, 38, 45))
    d.text((30, 34), f"lambda={p['wavelength']}mm  Amax={p['max_amplitude']}mm  "
                     f"h={p['layer_height']}mm  layers/color={p['layers_per_color']}  "
                     f"mapping={p['mapping']}  twist={p['twist']}", fill=(105, 110, 120))

    # 面板框
    px0, py0, pw, ph = 30, 62, 700, 600
    d.rectangle([px0, py0, px0 + pw, py0 + ph], outline=(210, 214, 220), width=1)
    s_range = max(r["s"][-1] for r in records)         # 展开整整一圈，能看到完整灰阶分布
    amp_draw = 2.5                                     # 振幅放大 2.5 倍便于观察
    X = lambda s: px0 + 12 + s / max(s_range, 1e-6) * (pw - 24)
    Y = lambda z: py0 + ph - 12 - z / max(height, 1e-6) * (ph - 24)

    for rec in records:
        s, disp = rec["s"], rec["disp"]
        mask = s <= s_range
        ss, dd = s[mask], disp[mask]
        if len(ss) < 2:
            continue
        base_y = Y(rec["z0"])
        top = [(X(float(a)), base_y - float(b) * amp_draw * (ph - 24) / max(height, 1e-6))
               for a, b in zip(ss, dd)]
        poly = [(X(float(ss[0])), base_y)] + top + [(X(float(ss[-1])), base_y)]
        fill = (238, 240, 244) if rec["white"] else (48, 52, 60)
        line = (150, 155, 165) if rec["white"] else (20, 22, 26)
        d.polygon(poly, fill=fill)
        d.line(top, fill=line, width=1)
    d.text((px0 + 12, py0 + ph - 22), "s (arc length) ->     amplitude shown x2.5",
           fill=(120, 125, 135), font=font)
    d.text((px0 + 12, py0 + 8), "UNWRAPPED SIDE VIEW", fill=(120, 125, 135), font=font)

    # 俯视
    qx0, qy0, qw, qh = 755, 62, 455, 600
    d.rectangle([qx0, qy0, qx0 + qw, qy0 + qh], outline=(210, 214, 220), width=1)
    d.text((qx0 + 12, qy0 + 8), "TOP VIEW (one white + one black segment)",
           fill=(120, 125, 135), font=font)

    mid = len(records) // 2
    pair = []
    for rec in records[mid:]:
        if rec["white"] and not any(r["white"] for r in pair):
            pair.append(rec)
        if (not rec["white"]) and not any(not r["white"] for r in pair):
            pair.append(rec)
        if len(pair) == 2:
            break
    if len(pair) < 2 and len(records) >= 2:
        pair = records[:2]

    blue, orange = (47, 111, 235), (232, 120, 40)
    if pair:
        all_pts = np.vstack([r["outer"] for r in pair])
        span = max(float(np.ptp(all_pts[:, 0])), float(np.ptp(all_pts[:, 1])), 1e-6)
        scale = min(qw - 70, 290) / span
        cx = qx0 + qw / 2
        cy = qy0 + 30 + 290 / 2
        ox = (bounds[0][0] + bounds[1][0]) / 2.0
        oy = (bounds[0][1] + bounds[1][1]) / 2.0
        for rec in pair:
            pts = rec["outer"]
            xy = [(float(cx + (a - ox) * scale), float(cy - (b - oy) * scale))
                  for a, b in pts]
            d.line(xy + [xy[0]], fill=blue if rec["white"] else orange, width=2)
        d.line([(cx - 12, cy), (cx + 12, cy)], fill=(200, 80, 80), width=1)
        d.line([(cx, cy - 12), (cx, cy + 12)], fill=(200, 80, 80), width=1)

        # 局部放大：同一段弧长上，白 / 黑两段的振幅曲线相差半个周期
        ax0, ay0, aw, ah = qx0 + 20, qy0 + 370, qw - 40, 210
        d.text((qx0 + 12, qy0 + 348), "AMPLITUDE PROFILE (12 mm window at mid-grey)",
               fill=(120, 125, 135), font=font)
        d.rectangle([ax0, ay0, ax0 + aw, ay0 + ah], outline=(224, 228, 234), width=1)
        lo_s = float(pair[0]["perimeter"]) * 0.45       # 取灰度接近中值的那段弧
        for rec in pair:
            s, disp = rec["s"], rec["disp"]
            mask = (s >= lo_s) & (s <= lo_s + 12.0)
            ss, dd = s[mask], disp[mask]
            if len(ss) < 2:
                continue
            curve = [(float(ax0 + (a - lo_s) / 12.0 * aw),
                      float(ay0 + ah - min(b / 0.5, 1.0) * (ah - 12)))
                     for a, b in zip(ss, dd)]
            d.line(curve, fill=blue if rec["white"] else orange, width=2)
        d.text((ax0 + 6, ay0 + 6), "0.5 mm", fill=(140, 145, 155), font=font)
        d.text((qx0 + 12, qy0 + qh - 24), "blue = white segment   /   orange = black segment",
               fill=(120, 125, 135), font=font)

    img.save(path)
    return path


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="dual_color_wave.py",
        description="双色波浪混色 3D 打印模型生成器（灰度图 -> white/black STL）",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "示例:\n"
            "  python dual_color_wave.py --demo\n"
            "  python dual_color_wave.py --model vase.stl --image portrait.png\n"
            "  python dual_color_wave.py --model board.stl --image logo.png --mapping planar\n"
            "  python dual_color_wave.py --demo --wavelength 1.2 --max-amplitude 0.5 --twist 0.3\n"
            "  python dual_color_wave.py --gui\n"
        ),
    )
    ap.add_argument("--model", help="输入 3D 模型 (STL/OBJ/3MF)。省略则自动生成测试圆柱")
    ap.add_argument("--image", help="输入灰度图 (png/jpg)。省略则自动生成测试图")
    ap.add_argument("--output", default="output_wave", help="输出目录 (默认 output_wave)")
    ap.add_argument("--demo-radius", type=float, default=20.0, help="测试圆柱半径 mm (默认 20)")
    ap.add_argument("--demo-height", type=float, default=20.0, help="测试圆柱高度 mm (默认 20)")
    ap.add_argument("--layer-height", type=float, default=0.2, help="层高 mm (默认 0.2)")
    ap.add_argument("--layers-per-color", type=int, default=2,
                    help="每几层换一次色 (默认 2；H2D 双喷嘴可用 1)")
    ap.add_argument("--wavelength", type=float, default=1.6, help="波浪周期 mm (推荐 1.2~2.0)")
    ap.add_argument("--max-amplitude", type=float, default=0.42,
                    help="最大外凸振幅 mm (推荐 0.35~0.50，略大于半个喷嘴直径)")
    ap.add_argument("--min-amplitude", type=float, default=0.05, help="基底偏移 mm (默认 0.05)")
    ap.add_argument("--wall-thickness", type=float, default=0.85,
                    help="实体壁厚 mm (默认 0.85，需 >= 2 倍线宽)")
    ap.add_argument("--sample-step", type=float, default=0.2, help="轮廓采样步长 mm (默认 0.2)")
    ap.add_argument("--mapping", choices=["cylindrical", "planar"], default="cylindrical",
                    help="贴图映射：柱面(花瓶/雕像) 或 平面(画板/浮雕)，默认 cylindrical")
    ap.add_argument("--planar-axis", choices=["x", "y"], default="x",
                    help="平面模式下由哪个水平轴决定 u (默认 x)")
    ap.add_argument("--angle-offset", type=float, default=0.0, help="柱面贴图旋转角度 (度)")
    ap.add_argument("--flip-u", action="store_true", help="水平镜像贴图")
    ap.add_argument("--flip-v", action="store_true", help="垂直镜像贴图")
    ap.add_argument("--twist", type=float, default=0.0,
                    help="每色段相位递进 (弧度)，0=竖直编织，0.2~0.5=斜纹")
    ap.add_argument("--droop", type=float, default=0.0, help="波峰沿 Z 下垂量 mm (默认 0)")
    ap.add_argument("--overlap", type=float, default=0.02, help="相邻色段 Z 搭接 mm (默认 0.02)")
    ap.add_argument("--no-preview", action="store_true", help="不生成波形预览图")
    ap.add_argument("--demo", action="store_true", help="等价于省略 --model/--image，直接跑测试用例")
    ap.add_argument("--gui", action="store_true", help="启动本地网页界面（浏览器表单，无需 tkinter）")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.gui:
        try:
            from wave_gui import launch
        except ImportError:
            print("找不到 wave_gui.py，请把它和 dual_color_wave.py 放在同一目录。")
            return 1
        launch()
        return 0

    params = {
        "model": None if args.demo else args.model,
        "image": None if args.demo else args.image,
        "output": args.output,
        "demo_radius": args.demo_radius,
        "demo_height": args.demo_height,
        "layer_height": args.layer_height,
        "layers_per_color": args.layers_per_color,
        "wavelength": args.wavelength,
        "max_amplitude": args.max_amplitude,
        "min_amplitude": args.min_amplitude,
        "wall_thickness": args.wall_thickness,
        "sample_step": args.sample_step,
        "mapping": args.mapping,
        "planar_axis": args.planar_axis,
        "angle_offset": args.angle_offset,
        "flip_u": args.flip_u,
        "flip_v": args.flip_v,
        "twist": args.twist,
        "droop": args.droop,
        "overlap": args.overlap,
        "preview": not args.no_preview,
    }
    try:
        generate(params)
    except Exception as exc:
        print(f"运行失败: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
