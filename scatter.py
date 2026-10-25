#!/usr/bin/env python3
"""A click-to-spread floating-window overview for Hyprland."""

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


def hypr(*args, json_result=False):
    result = subprocess.run(
        ["hyprctl", *(["-j"] if json_result else []), *args],
        check=True, text=True, capture_output=True, timeout=5,
    )
    return json.loads(result.stdout) if json_result else result.stdout


def batch(commands):
    if not commands:
        return
    result = hypr("--batch", ";".join("dispatch " + c for c in commands))
    if any(line.strip() != "ok" for line in result.splitlines() if line.strip()):
        raise RuntimeError(result.strip())


def contains(rect, point, padding=0):
    x, y, w, h = rect
    return (x - padding <= point[0] < x + w + padding
            and y - padding <= point[1] < y + h + padding)


def workarea(monitor):
    width, height = monitor["width"], monitor["height"]
    if monitor.get("transform", 0) % 2:
        width, height = height, width
    left, top, right, bottom = monitor["reserved"]
    return (monitor["x"] + left, monitor["y"] + top,
            int(width / monitor["scale"]) - left - right,
            int(height / monitor["scale"]) - top - bottom)


def grid(windows, area, gap=28):
    """Choose balanced rows by maximizing total aspect-preserving preview area."""
    if not windows:
        return []
    x, y, width, height = area
    count = len(windows)
    best = None
    for cols in range(1, count + 1):
        rows = math.ceil(count / cols)
        cw = (width - gap * (cols + 1)) / cols
        ch = (height - gap * (rows + 1)) / rows
        if cw <= 0 or ch <= 0:
            continue
        score = sum(w["size"][0] * w["size"][1] * min(
            cw / w["size"][0], ch / w["size"][1], 1.0) ** 2 for w in windows)
        if best is None or score > best[0]:
            best = score, cols, rows, cw, ch
    if best is None:
        raise RuntimeError("Not enough monitor space for the overview")
    _, cols, rows, cw, ch = best
    result = []
    for i, window in enumerate(windows):
        row, col = divmod(i, cols)
        row_count = min(cols, count - row * cols)
        offset = (width - (row_count * cw + (row_count - 1) * gap)) / 2
        ow, oh = window["size"]
        scale = min(cw / ow, ch / oh, 1.0)
        w, h = max(1, int(ow * scale)), max(1, int(oh * scale))
        result.append((int(x + offset + col * (cw + gap) + (cw - w) / 2),
                       int(y + gap + row * (ch + gap) + (ch - h) / 2), w, h))
    return result


def geometry(window, rect):
    x, y, w, h = rect
    address = "address:" + window["address"]
    return [f"resizewindowpixel exact {w} {h},{address}",
            f"movewindowpixel exact {x} {y},{address}"]


def visible_clients(clients, monitor):
    workspace = monitor["specialWorkspace"]["id"] or monitor["activeWorkspace"]["id"]
    return [c for c in clients if c.get("mapped") and not c.get("hidden")
            and c["monitor"] == monitor["id"]
            and (c["workspace"]["id"] == workspace or c.get("pinned"))]


def on_panel(layers, monitor, point):
    levels = layers.get(monitor["name"], {}).get("levels", {})
    noctalia_background = False
    for level in ("2", "3"):
        for layer in levels.get(level, []):
            if layer.get("namespace", "").startswith("noctalia-background-"):
                noctalia_background = True
                continue
            if layer.get("alpha", 1) > 0 and contains(
                (layer["x"], layer["y"], layer["w"], layer["h"]), point
            ):
                return True
    if noctalia_background:
        # Noctalia hosts panels in its full-screen background surface. While a
        # panel is open, that surface also consumes outside clicks to dismiss it.
        result = subprocess.run(
            ["qs", "-c", "noctalia-shell", "ipc", "call", "plugin:hypr-scatter", "panelOpen"],
            check=True, text=True, capture_output=True, timeout=5,
        )
        panel_open = json.loads(result.stdout)
        if not isinstance(panel_open, bool):
            raise ValueError("Noctalia panelOpen must return a boolean")
        return panel_open
    return False


class Scatter:
    def __init__(self, state_file):
        self.path = state_file
        self.press_path = state_file.with_suffix(".press")

    def save(self, state):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state))
        temp.replace(self.path)

    def restore(self, state, clients, selected=None):
        live = {c["address"]: c for c in clients}
        commands = []
        retained = []
        for window in state["windows"]:
            current = live.get(window["address"])
            if not current:
                continue
            # Do not drag windows back after an intentional workspace move.
            if current["workspace"]["id"] != window["workspace"]["id"]:
                continue
            if not current["floating"] or current.get("fullscreen"):
                continue
            commands.extend(geometry(window, [*window["at"], *window["size"]]))
            retained.append(window)
        # The saved clients order is bottom-to-top, independent of focus history.
        for window in retained:
            commands.append("alterzorder top,address:" + window["address"])
        # Cancellation restores stacking without focusing a window: focuswindow
        # warps the cursor into the target when cursor:no_warps is disabled.
        target = selected
        if target in {w["address"] for w in retained}:
            commands.extend(["focuswindow address:" + target,
                             "alterzorder top,address:" + target])
        batch(commands)
        self.path.unlink(missing_ok=True)

    def run(self, action):
        state = json.loads(self.path.read_text()) if self.path.exists() else None
        if action == "status":
            print(json.dumps({"active": state is not None,
                              "windows": len(state["windows"]) if state else 0}))
            return
        clients = hypr("clients", json_result=True)
        if action == "restore":
            if state:
                self.restore(state, clients)
            return
        if action == "click" and self.press_path.exists():
            blocked = self.press_path.read_text() == "blocked"
            self.press_path.unlink()
            if blocked:
                return
        monitors = hypr("monitors", json_result=True)
        cursor = hypr("cursorpos", json_result=True)
        point = cursor["x"], cursor["y"]
        monitor = next((m for m in monitors if contains(workarea(m), point)), None)
        if action == "press":
            # Fail closed if querying the shell fails; a release must not turn a
            # panel interaction into a desktop click after the panel disappears.
            self.press_path.write_text("blocked")
            if monitor is not None and not on_panel(hypr("layers", json_result=True), monitor, point):
                self.press_path.write_text("desktop")
            return
        if monitor is None:
            return
        visible = visible_clients(clients, monitor)
        if on_panel(hypr("layers", json_result=True), monitor, point):
            return
        if state:
            if time.monotonic() < state["ready_at"]:
                return
            if monitor["id"] != state["monitor"] or monitor["activeWorkspace"]["id"] != state["workspace"]:
                self.restore(state, clients)
                return
            # Query live geometry: clients can enforce their own minimum size.
            hit = [w for w in visible if contains((*w["at"], *w["size"]), point, 6)]
            self.restore(state, clients, hit[-1]["address"] if hit else None)
            return
        if action == "click" and any(contains((*w["at"], *w["size"]), point, 6) for w in visible):
            return
        # Real floating windows are rearranged; tiled layouts, groups and
        # fullscreen windows are deliberately left intact.
        windows = [w for w in visible if w["floating"] and not w.get("fullscreen")
                   and not w.get("grouped") and not w.get("pinned")]
        if len(windows) < 2:
            return
        state = {"windows": windows, "monitor": monitor["id"],
                 "workspace": monitor["activeWorkspace"]["id"],
                 "ready_at": time.monotonic() + 0.4}
        self.save(state)  # Write recovery information before touching any window.
        commands = []
        layout = sorted(windows, key=lambda w: (w["at"][1], w["at"][0], w["address"]))
        for window, rect in zip(layout, grid(layout, workarea(monitor))):
            commands.extend(geometry(window, rect))
        try:
            batch(commands)
        except Exception:
            self.restore(state, hypr("clients", json_result=True))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["press", "click", "toggle", "restore", "status"])
    args = parser.parse_args()
    runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "hypr-scatter"
    runtime.mkdir(mode=0o700, exist_ok=True)
    instance = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
    with (runtime / (instance + ".lock")).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Scatter(runtime / (instance + ".json")).run(args.action)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"hypr-scatter: {error}", file=sys.stderr)
        sys.exit(1)
