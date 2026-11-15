#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dual_color_wave 单元测试：python -m unittest discover tests -v"""

import math
import os
import sys
import tempfile
import unittest

import numpy as np
import trimesh

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dual_color_wave as dcw


class TestResampleContour(unittest.TestCase):
    def _square(self):
        # 逆时针单位正方形，起点 (1, 1)
        return np.array([[1, 1], [1, -1], [-1, -1], [-1, 1], [1, 1]], dtype=float)

    def test_sampling_count_and_arc_length(self):
        res = dcw.resample_contour(self._square(), step=0.1)
        self.assertIsNotNone(res)
        pts, normals, s, total = res
        self.assertGreaterEqual(len(pts), 16)
        self.assertAlmostEqual(total, 8.0, places=6)
        self.assertAlmostEqual(s[-1], total - 0.1, delta=0.11)

    def test_start_point_aligned_to_positive_x(self):
        res = dcw.resample_contour(self._square(), step=0.25, align_angle=0.0)
        pts = res[0]
        # 起点应被旋转到极角最接近 0 的顶点，即 (1, 1) 或 (1, -1)
        self.assertAlmostEqual(pts[0][0], 1.0, places=6)

    def test_normals_point_outward(self):
        res = dcw.resample_contour(self._square(), step=0.25)
        pts, normals = res[0], res[1]
        # 正方形中心在原点，所有法线应指向外侧（与顶点方向同号）
        dot = np.einsum("ij,ij->i", normals, pts)
        self.assertTrue(np.all(dot > 0))

    def test_cw_input_is_normalized(self):
        cw = self._square()[::-1]
        res = dcw.resample_contour(cw, step=0.25)
        pts, normals = res[0], res[1]
        dot = np.einsum("ij,ij->i", normals, pts)
        self.assertTrue(np.all(dot > 0))


class TestTexture(unittest.TestCase):
    def setUp(self):
        h, w = 16, 32
        yy, xx = np.mgrid[0:h, 0:w]
        self.tex = (xx / (w - 1)).astype(np.float32)   # u 方向线性渐变
        self.bounds = np.array([[0.0, 0.0, 0.0], [10.0, 10.0, 10.0]])

    def _pts(self, angle, z):
        return np.array([[5 + 4 * math.cos(angle), 5 + 4 * math.sin(angle), z]])

    def test_cylindrical_sampling_gradient(self):
        a0 = dcw.sample_texture(self.tex, self._pts(0.0, 5.0), self.bounds, "cylindrical")
        a1 = dcw.sample_texture(self.tex, self._pts(math.pi, 5.0), self.bounds, "cylindrical")
        self.assertAlmostEqual(float(a0[0]), 0.0, places=5)   # u=0  -> 纯黑
        # u=0.5 落在像素中心，量化误差约为 1/w
        self.assertAlmostEqual(float(a1[0]), 0.5, delta=2.0 / 32)

    def test_v_maps_bottom_to_top(self):
        lo = dcw.sample_texture(self.tex, self._pts(0, 0.0), self.bounds, "cylindrical")
        hi = dcw.sample_texture(self.tex, self._pts(0, 10.0), self.bounds, "cylindrical")
        self.assertLessEqual(float(lo[0]), float(hi[0]))

    def test_planar_sampling(self):
        pts = np.array([[0.0, 5.0, 5.0], [10.0, 5.0, 5.0]])
        got = dcw.sample_texture(self.tex, pts, self.bounds, "planar")
        self.assertAlmostEqual(float(got[0]), 0.0, places=5)
        self.assertAlmostEqual(float(got[1]), 1.0, places=5)

    def test_flip_u(self):
        pts = np.array([[0.0, 5.0, 5.0], [10.0, 5.0, 5.0]])
        got = dcw.sample_texture(self.tex, pts, self.bounds, "planar", flip_u=True)
        self.assertAlmostEqual(float(got[0]), 1.0, places=5)


class TestGenerate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dcw_test_")
        self.out = os.path.join(self.tmp.name, "out")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **overrides):
        params = dict(dcw.DEFAULT_PARAMS)
        params.update({"output": self.out, "demo_radius": 8.0, "demo_height": 8.0})
        params.update(overrides)
        return dcw.generate(params, log=lambda m: None)

    def test_demo_outputs_are_watertight(self):
        stats = self._run()
        self.assertIn("wave_white", stats["outputs"])
        self.assertIn("wave_black", stats["outputs"])
        for name, info in stats["outputs"].items():
            path = info["path"]
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(info["watertight"])
            self.assertTrue(info["winding_consistent"])
            mesh = trimesh.load(path, force="mesh")
            self.assertTrue(mesh.is_watertight)
            self.assertGreater(mesh.volume, 0.0)

    def test_layers_per_color_changes_blocks(self):
        a = self._run(layers_per_color=2)
        b = self._run(layers_per_color=4)
        self.assertEqual(a["model_height_mm"], b["model_height_mm"])
        self.assertEqual(b["color_blocks"], a["color_blocks"] // 2)

    def test_gamma_affects_amplitude_split(self):
        g1 = self._run(gamma=1.0)
        g2 = self._run(gamma=0.5)
        vw1 = g1["outputs"]["wave_white"]["volume_mm3"]
        vw2 = g2["outputs"]["wave_white"]["volume_mm3"]
        self.assertNotAlmostEqual(vw1, vw2, delta=1e-9)

    def test_planar_mapping_runs(self):
        stats = self._run(mapping="planar", planar_axis="x")
        self.assertTrue(stats["outputs"]["wave_white"]["watertight"])

    def test_bad_gamma_raises(self):
        with self.assertRaises(ValueError):
            self._run(gamma=0.0)

    def test_run_info_json_written(self):
        stats = self._run()
        path = os.path.join(self.out, "run_info.json")
        self.assertTrue(os.path.isfile(path))
        import json
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], dcw.__version__)
        self.assertEqual(data["color_blocks"], stats["color_blocks"])


if __name__ == "__main__":
    unittest.main()
