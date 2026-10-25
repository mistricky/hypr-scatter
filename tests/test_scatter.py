import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scatter


def window(address, x, y, size=(900, 600), history=0):
    return dict(address=address, at=[x, y], size=list(size), floating=True,
                mapped=True, hidden=False, workspace={"id": 2}, monitor=1,
                focusHistoryID=history, grouped=[], fullscreen=0, pinned=False)


class GridTests(unittest.TestCase):
    def test_grid_is_bounded_and_disjoint_for_scaled_rotated_monitors(self):
        for transform in (0, 1):
            monitor = dict(width=3840, height=2160, scale=1.5, transform=transform,
                           x=-2560, y=100, reserved=[38, 20, 0, 0])
            area = scatter.workarea(monitor)
            for count in range(1, 25):
                windows = [window(str(i), 0, 0, (400 + i * 63, 400 + i * 23)) for i in range(count)]
                rects = scatter.grid(windows, area)
                for i, (x, y, w, h) in enumerate(rects):
                    self.assertGreaterEqual(x, area[0])
                    self.assertGreaterEqual(y, area[1])
                    self.assertLessEqual(x + w, area[0] + area[2])
                    self.assertLessEqual(y + h, area[1] + area[3])
                    for xx, yy, ww, hh in rects[i + 1:]:
                        self.assertTrue(x + w <= xx or xx + ww <= x or y + h <= yy or yy + hh <= y)


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.engine = scatter.Scatter(self.path)
        self.windows = [window("0x1", 200, 200, history=1), window("0x2", 300, 300)]
        self.monitor = dict(id=1, name="DP-8", width=2560, height=1440,
                            scale=1, x=0, y=0, reserved=[38, 0, 0, 0],
                            activeWorkspace={"id": 2}, specialWorkspace={"id": 0})
        self.point = {"x": 2500, "y": 1300}
        self.layers = {}
        self.calls = []
        self.addCleanup(patch.stopall)
        patch("scatter.hypr", side_effect=self.ipc).start()
        patch("scatter.batch", side_effect=lambda c: self.calls.extend(c)).start()

    def ipc(self, command, **kwargs):
        return {"clients": self.windows, "monitors": [self.monitor],
                "cursorpos": self.point, "layers": self.layers,
                "activewindow": self.windows[-1] if self.windows else {}}[command]

    def test_blank_click_spreads_and_selection_restores_then_raises(self):
        original = copy.deepcopy(self.windows)
        self.engine.run("click")
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.calls), 4)
        state = json.loads(self.path.read_text())
        state["ready_at"] = 0
        self.engine.save(state)
        for w, rect in zip(self.windows, scatter.grid(self.windows, scatter.workarea(self.monitor))):
            w["at"], w["size"] = list(rect[:2]), list(rect[2:])
        self.point = dict(zip(("x", "y"), [v + 10 for v in self.windows[0]["at"]]))
        self.calls.clear()
        self.engine.run("click")
        self.assertFalse(self.path.exists())
        for w in original:
            for command in scatter.geometry(w, [*w["at"], *w["size"]]):
                self.assertIn(command, self.calls)
        self.assertEqual(self.calls[-2:], ["focuswindow address:0x1", "alterzorder top,address:0x1"])

    def test_normal_window_and_panel_clicks_do_not_spread(self):
        self.point = {"x": 400, "y": 400}
        self.engine.run("click")
        self.assertFalse(self.path.exists())
        self.point = {"x": 2500, "y": 1300}
        self.layers = {"DP-8": {"levels": {"3": [dict(x=2400, y=1200, w=160, h=240)]}}}
        self.engine.run("click")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])

    def test_cancellation_preserves_cursor_when_focusing_would_warp_it(self):
        def dispatch(commands):
            self.calls.extend(commands)
            # Model Hyprland's default cursor warp on explicit focuswindow.
            if any(c.startswith("focuswindow ") for c in commands):
                self.point = {"x": 750, "y": 600}

        for action in ("click", "restore"):
            with self.subTest(action=action):
                self.engine.run("toggle")
                state = json.loads(self.path.read_text())
                state["ready_at"] = 0
                self.engine.save(state)
                cursor = self.point.copy()
                self.calls.clear()
                with patch("scatter.batch", side_effect=dispatch):
                    self.engine.run(action)
                self.assertEqual(self.point, cursor)
                self.assertFalse(self.path.exists())
                self.assertEqual(self.calls[-2:], [
                    "alterzorder top,address:0x1", "alterzorder top,address:0x2"])

    def test_closed_or_moved_window_is_not_restored(self):
        self.engine.run("toggle")
        self.windows.pop()
        self.windows[0]["workspace"]["id"] = 3
        self.calls.clear()
        self.engine.run("restore")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.path.exists())

    def test_dispatch_failure_keeps_recovery_state(self):
        self.engine.run("toggle")
        with patch("scatter.batch", side_effect=RuntimeError("IPC failed")):
            with self.assertRaises(RuntimeError):
                self.engine.run("restore")
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
