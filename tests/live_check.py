"""Opt-in live round-trip check. Temporarily rearranges floating windows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scatter


def main():
    root = Path(__file__).resolve().parents[1]
    state_path = Path(os.environ["XDG_RUNTIME_DIR"]) / "hypr-scatter" / (
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] + ".json")
    if state_path.exists():
        raise RuntimeError("Restore the existing overview before running this check")
    before = scatter.hypr("clients", json_result=True)
    original_focus = scatter.hypr("activewindow", json_result=True).get("address")
    try:
        subprocess.run([sys.executable, str(root / "scatter.py"), "toggle"], check=True)
        time.sleep(0.8)
        if not state_path.exists():
            raise RuntimeError("Overview did not open: cursor must be in a monitor work area with at least two floating windows")
        state = json.loads(state_path.read_text())
        addresses = {w["address"] for w in state["windows"]}
        after = {w["address"]: w for w in scatter.hypr("clients", json_result=True)}
        rects = [(*after[a]["at"], *after[a]["size"]) for a in addresses]
        for i, (x, y, w, h) in enumerate(rects):
            for xx, yy, ww, hh in rects[i + 1:]:
                if not (x + w <= xx or xx + ww <= x or y + h <= yy or yy + hh <= y):
                    raise RuntimeError("Live windows overlap (possibly an application minimum-size constraint)")
        selected = state["windows"][0]["address"]
        scatter.Scatter(state_path).restore(state, list(after.values()), selected)
        time.sleep(0.8)
        assert scatter.hypr("activewindow", json_result=True)["address"] == selected
        restored = {w["address"]: w for w in scatter.hypr("clients", json_result=True)}
        for old in before:
            if old["address"] in addresses:
                new = restored[old["address"]]
                assert old["at"] == new["at"], (old["address"], "position", old["at"], new["at"])
                assert old["size"] == new["size"], (old["address"], "size", old["size"], new["size"])
        print(f"PASS: {len(addresses)} live windows spread without overlap, exact geometry restored, selection focused and raised")
    finally:
        subprocess.run([sys.executable, str(root / "scatter.py"), "restore"], check=True)
        if original_focus:
            scatter.batch(["focuswindow address:" + original_focus,
                           "alterzorder top,address:" + original_focus])


if __name__ == "__main__":
    main()
