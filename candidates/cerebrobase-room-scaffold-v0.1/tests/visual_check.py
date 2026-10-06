"""F (visual). Screenshots of the login, admin room, admin entry and pilot room at 320px and desktop width, with a
horizontal-overflow check (document scrollWidth <= viewport width) and keyboard focus check. Requires the Python
`playwright` package and a Chromium build; if unavailable this prints VISUAL_CHECK=UNRUN and exits 3 (never PASS).

usage: python tests/visual_check.py --out <NEW dir> [--chromium <executable>]
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--chromium")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"VISUAL_CHECK": "UNRUN", "reason": "python playwright not installed",
                          "command": "pip install playwright && python tests/visual_check.py --out <dir>"}))
        return 3
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    base = out / "_server"
    (base / "public").mkdir(parents=True)
    cfg = {"config_schema": "cerebrobase.config/v1", "environment": "DEV", "build_id": "SOURCE_TREE",
           "db_path": str(base / "data" / "db" / "rooms.sqlite3"), "private_data_root": str(base / "data" / "private"),
           "public_assets_root": str(base / "public"), "runtime_root": str(base / "run"), "bind_host": "127.0.0.1",
           "port": port, "public_origin": f"http://127.0.0.1:{port}", "auth_mode": "SYNTHETIC",
           "cookie_secure": False, "capabilities": {"tools": {"fixture.echo": True}}, "min_free_bytes": 1024}
    cp = base / "dev.json"
    cp.write_text(json.dumps(cfg))
    py = [sys.executable, "-B", "-m", "cerebrobase"]
    for cmd in (["preflight", "--config", str(cp), "--phase", "pre-migrate"], ["migrate", "--config", str(cp)],
                ["seed", "--config", str(cp)]):
        subprocess.run(py + cmd, cwd=ROOT, check=True, capture_output=True)
    proc = subprocess.Popen(py + ["serve", "--config", str(cp)], cwd=ROOT, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    origin = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(origin + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    results = []
    try:
        with sync_playwright() as pw:
            kw = {"executable_path": a.chromium} if a.chromium else {}
            browser = pw.chromium.launch(**kw)
            for width, height, label in ((320, 720, "320"), (1280, 900, "desktop")):
                for ident, pages in (("fixture-andreas-admin", ["/rom", "/admin"]),
                                     ("fixture-marianne-pilot", ["/rom"])):
                    ctx = browser.new_context(viewport={"width": width, "height": height})
                    page = ctx.new_page()
                    page.goto(origin + "/logg-inn")
                    if ident == "fixture-andreas-admin":
                        shot = out / f"login-{label}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        results.append(_measure(page, "login", label, shot))
                    page.click(f'button[value="{ident}"]')
                    page.wait_for_load_state()
                    for path in pages:
                        page.goto(origin + path)
                        name = ("admin-room" if path == "/rom" else "admin-entry") if "andreas" in ident else "pilot-room"
                        shot = out / f"{name}-{label}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        results.append(_measure(page, name, label, shot))
                    ctx.close()
            browser.close()
    finally:
        subprocess.run(py + ["stop", "--config", str(cp)], cwd=ROOT, capture_output=True)
        proc.wait(timeout=20)
        shutil.rmtree(base, ignore_errors=True)
    ok = all(r["no_horizontal_overflow"] and r["focus_visible_target"] for r in results)
    rec = {"VISUAL_CHECK": "PASS" if ok else "FAIL", "results": results}
    (out / "visual_check.json").write_text(json.dumps(rec, indent=1))
    print(json.dumps(rec, indent=1))
    return 0 if ok else 1


def _measure(page, name, label, shot):
    dims = page.evaluate("() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,"
                         " h: document.documentElement.scrollHeight})")
    page.keyboard.press("Tab")
    focused = page.evaluate("() => document.activeElement ? document.activeElement.tagName : null")
    return {"page": name, "viewport": label, "scroll_width": dims["sw"], "client_width": dims["cw"],
            "no_horizontal_overflow": dims["sw"] <= dims["cw"], "first_tab_focus": focused,
            "focus_visible_target": focused in ("A", "BUTTON", "INPUT"), "screenshot": shot.name}


if __name__ == "__main__":
    sys.exit(main())
