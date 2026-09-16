#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# v18: 修复对齐策略顺序 / 切换消耗索引 / fail 竞态 / rotate 质量门

import os
import re
import sys
import json
import random
import urllib.parse
import requests
import time
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

try:
    import numpy as np
    import cv2
    from numpy.lib.stride_tricks import sliding_window_view
except ImportError:
    print("❌ 缺少依赖：pip install numpy opencv-python-headless")
    sys.exit(1)


# ================= 配置区 =================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "")
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "")
ACCOUNT_NAME  = os.environ.get("ACCOUNT_NAME", "未命名账号")
SITE_BASE     = "https://openworld.eu.org"
RENEW_THRESHOLD_DAYS = 5
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", ".")

PREFERRED_KINDS = ("puzzle", "key", "rotate", "odd")
MAX_SWITCH_PER_SESSION = 8
MAX_FAIL_PER_SESSION = 5

# ⭐ v18: 把 "sub" 放到最前（物理上更正确）
#   px_mode: "none" = shape_cx - alpha_cx
#            "sub"  = shape_cx - alpha_cx - px
#            "add"  = shape_cx - alpha_cx + px
ALIGN_STRATEGIES = [
    ("sub",  0),
    ("sub", +1),
    ("sub", -1),
    ("none", 0),
    ("none", +1),
    ("none", -1),
    ("sub", +2),
    ("sub", -2),
    ("add",  0),
    ("none", +2),
    ("none", -2),
]

# ⭐ v18: rotate NCC 阈值，低于此值直接放弃
ROTATE_NCC_MIN = 0.35

ALIGN_STATE = {"counter": 0}
# ==========================================

os.makedirs(SCREENSHOT_DIR, exist_ok=True)


STEALTH_JS = r"""
(function() {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => false }); } catch(e) {}
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [{name:'PDF Viewer'},{name:'Chrome PDF Viewer'},{name:'Chromium PDF Viewer'}]
    });
  } catch(e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en','zh-CN'] }); } catch(e) {}
  try {
    if (!window.chrome) window.chrome = { runtime:{}, loadTimes:function(){}, csi:function(){}, app:{} };
  } catch(e) {}
  ['__selenium_unwrapped','__webdriver_evaluate','__selenium_evaluate','__driver_evaluate',
   '__fxdriver_evaluate','_Selenium_IDE_Recorder','__webdriver_script_fn','__webdriver_script_func',
   '__webdriver_script_url','__driver_script_fn','__driver_script_url','_phantom','__nightmare',
   'callPhantom','domAutomation','domAutomationController'].forEach(function(k){
    try { delete window[k]; } catch(e) {}
  });
  try { for (var k in window) if (k.indexOf('$cdc_')===0) { try{delete window[k];}catch(e){} } } catch(e) {}
  try { delete document.$cdc_asdjflasutopfhvcZLmcfl_; } catch(e) {}
})();
"""


# ================= WebSocket 状态 =================
WS_STATE = {
    "url": None,
    "meta": None,
    "frames": [],
    "last_resp": None,
    "sent": [],
    "closed": False,
    "fail_pending": False,   # ⭐ v18
}


def _reset_ws_state():
    WS_STATE.update({"meta": None, "frames": [], "last_resp": None,
                     "sent": [], "closed": False, "fail_pending": False})


def _reset_align_pick():
    ALIGN_STATE["counter"] = 0


def _install_ws_hook(page):
    def on_ws(ws):
        if "openworld.eu.org" not in ws.url:
            return
        WS_STATE["url"] = ws.url
        print(f"   🔌 WebSocket: {ws.url}")

        def on_sent(payload):
            try:
                if isinstance(payload, (bytes, bytearray)):
                    WS_STATE["sent"].append(f"[bin:{len(payload)}]")
                else:
                    s = str(payload)
                    WS_STATE["sent"].append(s[:200])
                    print(f"   ➡️ {s[:140]}")
            except Exception:
                pass

        def on_recv(payload):
            try:
                if isinstance(payload, (bytes, bytearray)):
                    WS_STATE["frames"].append(bytes(payload))
                else:
                    s = str(payload)
                    WS_STATE["last_resp"] = s
                    print(f"   ⬅️ {s[:180]}")
                    # ⭐ v18: 在这里就标记失败，避免后续被新 challenge 覆盖
                    if s == "fail" or s.startswith("failed:"):
                        WS_STATE["fail_pending"] = True
                    try:
                        m = json.loads(s)
                        if isinstance(m, dict) and m.get("id") and m.get("nf"):
                            WS_STATE["meta"] = m
                            WS_STATE["frames"] = []
                    except Exception:
                        pass
            except Exception:
                pass

        def on_close():
            WS_STATE["closed"] = True
            print("   🔒 WebSocket 关闭")

        ws.on("framesent", on_sent)
        ws.on("framereceived", on_recv)
        ws.on("close", on_close)

    page.on("websocket", on_ws)


# ================= 工具 =================

def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": f"👤 {ACCOUNT_NAME}\n{message}"},
            timeout=10,
        )
        print("✅ Telegram 已发送" if r.status_code == 200 else f"❌ TG {r.status_code}")
    except Exception as e:
        print(f"❌ TG 异常: {e}")


def save_screenshot(page, name):
    try:
        page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"))
        print(f"   📸 {name}.png")
    except Exception as e:
        print(f"   ⚠️ 截图: {e}")


def dump_page_debug(page, name):
    save_screenshot(page, name)
    try:
        with open(os.path.join(SCREENSHOT_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def wait_for_cloudflare(page, timeout=15):
    inds = ["verify you are human", "just a moment", "checking your browser",
            "cf-browser-verification", "challenge-platform"]
    start = time.time()
    while time.time() - start < timeout:
        try:
            if not any(i in page.content().lower() for i in inds):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ================= Discord OAuth 登录 =================

def login_with_discord_token(page, dc_token: str) -> bool:
    print("=" * 50)
    print("🔑 Discord OAuth 登录")
    print("=" * 50)

    try:
        page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"   ⚠️ 首页: {e}")

    try:
        page.goto(f"{SITE_BASE}/login", wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"   ⚠️ 登录页: {e}")

    def _click(sels, desc=""):
        for sel in sels:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.click()
                    print(f"   🔘 {desc}: {sel}")
                    return True
            except Exception:
                continue
        return False

    if "discord.com" not in page.url:
        clicked = _click(["#clerk-signin", "button[id='clerk-signin']",
                          "button:has-text('Sign in with Clerk')",
                          "[data-clerk-component] button"], "Clerk 入口")
        if clicked:
            page.wait_for_timeout(4000)
            d_sels = ["button:has-text('Continue with Discord')",
                      "button.cl-socialButtonsBlockButton",
                      "button[data-localization-key='socialButtonsBlockButton']",
                      "button:has-text('Discord')",
                      "a:has-text('Continue with Discord')"]
            _click(d_sels, "Clerk 内 Discord")
            try:
                page.wait_for_url(re.compile(r"discord\.com"), timeout=20000,
                                  wait_until="domcontentloaded")
                print("   ✅ 已跳到 Discord")
            except Exception:
                pass
        if "discord.com" not in page.url and not clicked:
            _click(["button:has-text('Sign in with Discord')",
                    "a:has-text('Sign in with Discord')",
                    "button:has-text('Continue with Discord')",
                    "button:has-text('Discord')",
                    "a:has-text('Discord')"], "通用入口")

    if "discord.com" not in page.url:
        for _ in range(15):
            page.wait_for_timeout(1000)
            if "discord.com" in page.url:
                break
        if "discord.com" not in page.url:
            print(f"   ❌ 无法跳转 Discord: {page.url}")
            save_screenshot(page, "login_failed_no_discord")
            return False

    oauth_url = page.url
    if "discord.com/login" in oauth_url and "redirect_to=" in oauth_url:
        p = urllib.parse.urlparse(oauth_url)
        q = urllib.parse.parse_qs(p.query)
        rt = q.get("redirect_to", [""])[0]
        if rt:
            oauth_url = ("https://discord.com" + rt) if rt.startswith("/") else rt

    p = urllib.parse.urlparse(oauth_url)
    q = urllib.parse.parse_qs(p.query)
    client_id     = q.get("client_id", [""])[0]
    redirect_uri  = q.get("redirect_uri", [""])[0]
    scope         = q.get("scope", ["identify email"])[0]
    state         = q.get("state", [""])[0]
    response_type = q.get("response_type", ["code"])[0]

    if not client_id or not redirect_uri:
        print("   ❌ OAuth 参数解析失败")
        save_screenshot(page, "login_failed_parse")
        return False

    api_p = urllib.parse.urlencode({
        "client_id": client_id, "response_type": response_type,
        "redirect_uri": redirect_uri, "scope": scope, "state": state,
    })
    try:
        r = requests.post(
            f"https://discord.com/api/v9/oauth2/authorize?{api_p}",
            headers={
                "accept": "*/*",
                "authorization": dc_token.strip(),
                "content-type": "application/json",
                "origin": "https://discord.com",
                "referer": f"https://discord.com/oauth2/authorize?{api_p}",
                "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/130.0.0.0 Safari/537.36"),
            },
            json={"permissions": "0", "authorize": True, "integration_type": 0},
            timeout=20,
        )
        print(f"   API: {r.status_code}")
        if r.status_code in (401, 403):
            print("   ❌ Discord Token 失效")
            return False
        if r.status_code != 200:
            print(f"   ❌ {r.text[:200]}")
            return False
        location = r.json().get("location", "")
    except Exception as e:
        print(f"   ❌ API 异常: {e}")
        return False

    if not location:
        return False

    try:
        page.goto(location, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(5000)
    wait_for_cloudflare(page)

    if "/login" in page.url and "discord" not in page.url:
        page.wait_for_timeout(5000)
        if "/login" in page.url:
            print(f"   ❌ 登录失败: {page.url}")
            save_screenshot(page, "login_callback_stuck")
            return False

    print(f"   ✅ 登录成功: {page.url}")
    return True


# ================= 求解器 =================

def _decode(b):
    return cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_UNCHANGED)


def _chip_shape(chip):
    """从 chip 的 alpha 提取形状描述 + alpha 内容几何。"""
    alpha = chip[:, :, 3]
    ys, xs = np.where(alpha > 200)
    if len(xs) == 0:
        return None
    cx0, cx1 = int(xs.min()), int(xs.max()) + 1
    cy0, cy1 = int(ys.min()), int(ys.max()) + 1
    a = alpha[cy0:cy1, cx0:cx1]
    _, th = cv2.threshold(a, 200, 255, cv2.THRESH_BINARY)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(cnt)
    peri = cv2.arcLength(cnt, True)
    circ = 4 * np.pi * area / (peri * peri) if peri > 0 else 0
    approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)
    return {
        "w": cx1 - cx0,
        "h": cy1 - cy0,
        "circ": circ,
        "nv": len(approx),
        "alpha_x0": cx0,
        "alpha_y0": cy0,
        "alpha_x1": cx1,
        "alpha_y1": cy1,
        "alpha_cx": (cx0 + cx1) / 2.0,
        "alpha_cy": (cy0 + cy1) / 2.0,
    }


def _bg_black_shapes(bg_gray):
    """找 bg 上所有黑色连通域。"""
    _, th = cv2.threshold(bg_gray, 40, 255, cv2.THRESH_BINARY_INV)
    kernel = np.ones((3, 3), np.uint8)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    out = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = cv2.contourArea(cnt)
        if w < 40 or h < 40 or area < 1500:
            continue
        peri = cv2.arcLength(cnt, True)
        if peri < 1:
            continue
        circ = 4 * np.pi * area / (peri * peri)
        approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)
        out.append({
            "bbox": (x, y, w, h),
            "circ": circ,
            "nv": len(approx),
            "area": area,
        })
    return out


def _solve_puzzle(bg_bytes, chip_bytes, meta, align_idx=0):
    """
    puzzle/key：chip 拖到形状中心。
    对齐公式：value = shape_cx - alpha_cx - px * mode + offset
    """
    bg = _decode(bg_bytes)
    chip = _decode(chip_bytes)
    if bg is None or chip is None:
        raise RuntimeError("解码失败")
    bg_rgb  = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
    bg_gray = cv2.cvtColor(bg_rgb, cv2.COLOR_BGR2GRAY)

    if chip.ndim != 3 or chip.shape[2] != 4:
        raise RuntimeError("chip 缺少 alpha")

    img_h, img_w = chip.shape[:2]
    chip_info = _chip_shape(chip)
    if chip_info is None:
        raise RuntimeError("chip 轮廓为空")
    chip_w, chip_h = chip_info["w"], chip_info["h"]
    chip_circ, chip_nv = chip_info["circ"], chip_info["nv"]
    alpha_cx = chip_info["alpha_cx"]
    print(f"   📐 chip img: {img_w}x{img_h}  alpha bbox: {chip_w}x{chip_h} "
          f"circ={chip_circ:.2f} nv={chip_nv} alpha_cx={alpha_cx:.1f}")

    candidates = _bg_black_shapes(bg_gray)
    if not candidates:
        raise RuntimeError("bg 上未找到黑色形状")
    print(f"   🔍 bg 上检测到 {len(candidates)} 个形状:")

    scored = []
    for c in candidates:
        bw, bh = c["bbox"][2], c["bbox"][3]
        size_score = 1.0 - min(1.0, abs(bw - chip_w) + abs(bh - chip_h)) / 100.0

        if chip_nv in (3, 4, 5, 6, 7, 8):
            shape_score = 1.0 if c["nv"] == chip_nv else 0.2
        elif chip_circ > 0.75:
            shape_score = 1.0 if c["circ"] > 0.75 else 0.2
        else:
            shape_score = max(0.0, 1.0 - abs(c["circ"] - chip_circ))

        score = 0.7 * shape_score + 0.3 * size_score
        scored.append((score, c))
        print(f"      bbox={c['bbox']} nv={c['nv']} circ={c['circ']:.2f} "
              f"shape={shape_score:.2f} size={size_score:.2f} total={score:.2f}")

    scored.sort(reverse=True, key=lambda x: x[0])
    best_score, best = scored[0]
    if best_score < 0.5:
        raise RuntimeError(f"形状匹配度过低 ({best_score:.2f})")

    x, y, w, h = best["bbox"]
    shape_cx = x + w / 2.0

    px = int(meta.get("px") or 0)
    pw = int(meta.get("pw") or img_w)
    vmax = int(meta.get("vmax") or 300)

    mode, offset = ALIGN_STRATEGIES[align_idx % len(ALIGN_STRATEGIES)]
    base = shape_cx - alpha_cx
    if mode == "sub":
        base -= px
    elif mode == "add":
        base += px
    value = int(round(base + offset))
    value = max(0, min(vmax, value))

    print(f"   🎯 bbox=({x},{y},{w},{h}) score={best_score:.2f} "
          f"shape_cx={shape_cx:.1f} alpha_cx={alpha_cx:.1f} px={px} pw={pw} "
          f"strategy=#{align_idx % len(ALIGN_STRATEGIES)}({mode},{offset:+d}) → value={value}")

    try:
        vis = bg_rgb.copy()
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.imwrite(os.path.join(SCREENSHOT_DIR,
                                 f"puzzle_align_{meta['id'][:6]}_a{align_idx}.png"), vis)
    except Exception as e:
        print(f"   ⚠️ 可视化: {e}")

    return value


def _solve_rotate(bg_bytes, chip_bytes, meta, tag=""):
    """
    rotate：chip 固定在 (ox,oy,ow,oh) 绕中心旋转。
    返回: 角度（>=0），或 -1 表示放弃（NCC 太低）。
    """
    bg = _decode(bg_bytes)
    chip = _decode(chip_bytes)
    if bg is None or chip is None:
        return -1
    bg_rgb = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
    bg_gray = cv2.cvtColor(bg_rgb, cv2.COLOR_BGR2GRAY)
    H, W = bg_gray.shape

    ox, oy = int(meta["ox"]), int(meta["oy"])
    ow, oh = int(meta["ow"]), int(meta["oh"])

    pad = 30
    x0 = max(0, ox - pad)
    y0 = max(0, oy - pad)
    x1 = min(W, ox + ow + pad)
    y1 = min(H, oy + oh + pad)
    target = bg_gray[y0:y1, x0:x1]
    th, tw = target.shape

    if chip.ndim == 3 and chip.shape[2] == 4:
        chip_rgb = chip[:, :, :3]
        chip_alpha = chip[:, :, 3]
    else:
        chip_rgb = chip[:, :, :3] if chip.ndim == 3 else cv2.cvtColor(chip, cv2.COLOR_GRAY2BGR)
        cg = cv2.cvtColor(chip_rgb, cv2.COLOR_BGR2GRAY)
        _, chip_alpha = cv2.threshold(cg, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    if chip_alpha.max() > 100:
        ys, xs = np.where(chip_alpha > 100)
        if len(xs) > 0:
            cx0, cx1 = int(xs.min()), int(xs.max()) + 1
            cy0, cy1 = int(ys.min()), int(ys.max()) + 1
            chip_gray = cv2.cvtColor(chip_rgb[cy0:cy1, cx0:cx1], cv2.COLOR_BGR2GRAY)
            chip_mask = chip_alpha[cy0:cy1, cx0:cx1]
        else:
            chip_gray = cv2.cvtColor(chip_rgb, cv2.COLOR_BGR2GRAY)
            chip_mask = chip_alpha
    else:
        chip_gray = cv2.cvtColor(chip_rgb, cv2.COLOR_BGR2GRAY)
        chip_mask = chip_alpha

    ch, cw = chip_gray.shape
    ccx, ccy = cw / 2.0, ch / 2.0
    tgt_cx = (ox + ow / 2.0) - x0
    tgt_cy = (oy + oh / 2.0) - y0

    def _score(angle):
        M = cv2.getRotationMatrix2D((ccx, ccy), angle, 1.0)
        cos_a, sin_a = abs(M[0, 0]), abs(M[0, 1])
        nw = int(np.ceil(ch * sin_a + cw * cos_a))
        nh = int(np.ceil(ch * cos_a + cw * sin_a))
        M[0, 2] += nw / 2.0 - ccx
        M[1, 2] += nh / 2.0 - ccy
        rot = cv2.warpAffine(chip_gray, M, (nw, nh),
                             flags=cv2.INTER_LINEAR, borderValue=0)
        rot_m = cv2.warpAffine(chip_mask, M, (nw, nh),
                               flags=cv2.INTER_NEAREST, borderValue=0)
        px = int(round(tgt_cx - nw / 2.0))
        py = int(round(tgt_cy - nh / 2.0))
        tx0, ty0 = max(0, px), max(0, py)
        tx1, ty1 = min(tw, px + nw), min(th, py + nh)
        if tx1 <= tx0 or ty1 <= ty0:
            return -1.0, rot, rot_m, px, py
        rx0, ry0 = tx0 - px, ty0 - py
        rx1 = rx0 + (tx1 - tx0); ry1 = ry0 + (ty1 - ty0)
        a = target[ty0:ty1, tx0:tx1].astype(np.float32)
        b = rot[ry0:ry1, rx0:rx1].astype(np.float32)
        m = rot_m[ry0:ry1, rx0:rx1] > 0
        if m.sum() < 200:
            return -1.0, rot, rot_m, px, py
        av = a[m] - a[m].mean()
        bv = b[m] - b[m].mean()
        d = float(np.sqrt((av * av).sum()) * np.sqrt((bv * bv).sum()))
        if d < 1e-6:
            return -1.0, rot, rot_m, px, py
        return float((av * bv).sum() / d), rot, rot_m, px, py

    best_angle, best_score = 0, -1.0
    best_rot = best_rot_m = None
    best_px = best_py = 0

    for angle in range(0, 360, 3):
        s, rot, rot_m, px, py = _score(angle)
        if s > best_score:
            best_score, best_angle = s, angle
            best_rot, best_rot_m = rot, rot_m
            best_px, best_py = px, py
    for da in (-2, -1, 1, 2):
        angle = (best_angle + da) % 360
        s, rot, rot_m, px, py = _score(angle)
        if s > best_score:
            best_score, best_angle = s, angle
            best_rot, best_rot_m = rot, rot_m
            best_px, best_py = px, py

    print(f"   🎯 rotate: 逆时针={best_angle}° ncc={best_score:.3f}")

    try:
        if best_rot is not None:
            vis = cv2.cvtColor(target, cv2.COLOR_GRAY2BGR)
            h2, w2 = best_rot.shape
            tx0, ty0 = max(0, best_px), max(0, best_py)
            tx1, ty1 = min(tw, best_px + w2), min(th, best_py + h2)
            rx0, ry0 = tx0 - best_px, ty0 - best_py
            rx1 = rx0 + (tx1 - tx0); ry1 = ry0 + (ty1 - ty0)
            m3 = (best_rot_m[ry0:ry1, rx0:rx1] > 0)
            sub = vis[ty0:ty1, tx0:tx1]
            sub[m3] = (sub[m3] * 0.5 +
                       np.array([0, 0, 255]) * 0.5).astype(np.uint8)
            cv2.imwrite(os.path.join(SCREENSHOT_DIR,
                                     f"rotate_vis_{tag}.png"), vis)
    except Exception as e:
        print(f"   ⚠️ 可视化: {e}")

    # ⭐ v18: NCC 质量门
    if best_score < ROTATE_NCC_MIN:
        print(f"   ⚠️ NCC 低于阈值 {ROTATE_NCC_MIN}，放弃本次提交")
        return -1

    return (360 - best_angle) % 360


# ================= 交互 =================

def _drag_slider(page, value, vmax):
    track = page.locator("#captcha_track_default")
    track.wait_for(state="visible", timeout=5000)
    box = track.bounding_box()
    if not box:
        raise RuntimeError("track 不可见")

    hw = page.evaluate("""() => {
        const h = document.getElementById('captcha_handle_default');
        return h ? h.offsetWidth : 24;
    }""") or 24

    frac = max(0.0, min(1.0, value / max(1, vmax)))
    usable = max(1.0, box["width"] - hw)
    target_x = box["x"] + hw / 2 + frac * usable
    start_x = box["x"] + hw / 2
    y = box["y"] + box["height"] / 2

    page.mouse.move(start_x, y)
    page.wait_for_timeout(random.randint(80, 200))
    page.mouse.down()
    page.wait_for_timeout(random.randint(60, 120))

    steps = random.randint(18, 28)
    for i in range(1, steps + 1):
        t = i / steps
        eased = 1 - (1 - t) ** 2
        x = start_x + (target_x - start_x) * eased
        page.mouse.move(x, y + random.uniform(-1.5, 1.5))
        page.wait_for_timeout(random.randint(10, 25))

    over = random.uniform(3, 6)
    page.mouse.move(target_x + over, y + random.uniform(-2, 2))
    page.wait_for_timeout(random.randint(50, 90))
    page.mouse.move(target_x - random.uniform(1, 3), y + random.uniform(-1, 1))
    page.wait_for_timeout(random.randint(40, 80))
    page.mouse.move(target_x + random.uniform(-1, 1), y + random.uniform(-1, 1))
    page.wait_for_timeout(random.randint(30, 60))
    page.mouse.up()


# ================= 一关处理 =================

def _handle_one_stage(page, meta, frames, tag="", align_idx=0):
    """
    True       -> 已提交
    "switched" -> 已切换类型
    False      -> 无法处理
    """
    kind = meta.get("kind")
    alt = meta.get("alt")
    print(f"   🎯 kind={kind} nf={meta.get('nf')} "
          f"stage={meta.get('stage')}/{meta.get('stages')} alt={alt}")

    if kind not in PREFERRED_KINDS:
        if alt in PREFERRED_KINDS:
            try:
                btn = page.locator("#captcha_switch_default").first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    print(f"   🔁 切换类型: {kind} → {alt}")
                    return "switched"
            except Exception as e:
                print(f"   ⚠️ 切换失败: {e}")
        print(f"   ⚠️ {kind} 无法处理，放弃会话")
        return False

    if kind in ("puzzle", "key"):
        if len(frames) < 2:
            print("   ⚠️ nf<2")
            return False
        try:
            value = _solve_puzzle(frames[0], frames[1], meta, align_idx=align_idx)
            print(f"   🧩 value={value} vmax={meta.get('vmax')}")
            _drag_slider(page, value, int(meta.get("vmax") or 300))
            return True
        except Exception as e:
            print(f"   ❌ puzzle: {e}")
            return False

    if kind == "rotate":
        if len(frames) < 2:
            print("   ⚠️ nf<2")
            return False
        try:
            value = _solve_rotate(frames[0], frames[1], meta, tag=tag)
            if value < 0:
                print("   ⚠️ rotate 求解失败，放弃本次提交")
                return False
            print(f"   🎯 value={value} vmax={meta.get('vmax')}")
            _drag_slider(page, value, int(meta.get("vmax") or 359))
            return True
        except Exception as e:
            print(f"   ❌ rotate: {e}")
            return False

    if kind == "odd":
        print("   ⚠️ odd 类型暂不处理，放弃会话")
        return False

    print(f"   ⚠️ 未支持 kind: {kind}")
    return False


# ================= 多 stage 会话 =================

def _try_renew_session(page, attempt, initial_days):
    print(f"\n   {'='*40}\n   🔄 第 {attempt} 次会话\n   {'='*40}")

    try:
        btn = page.locator("button:has-text('Renew free')").first
        btn.wait_for(state="visible", timeout=8000)
        btn.click()
        print("   ✅ 已点击 Renew free")
    except Exception as e:
        print(f"   ❌ 未找到续期按钮: {e}")
        return None

    ok = False
    for _ in range(80):
        if WS_STATE["meta"] and WS_STATE["meta"].get("id"):
            ok = True
            break
        page.wait_for_timeout(200)
    if not ok:
        print(f"   ❌ 无 meta，last_resp={WS_STATE['last_resp']!r}")
        dump_page_debug(page, f"no_meta_{attempt}")
        return None

    handled_fps = set()
    switch_count = 0
    fail_count = 0
    last_action = time.time()
    start = time.time()
    max_total = 300

    while time.time() - start < max_total:
        # ⭐ v18: 先处理 fail_pending，避免被新 challenge 覆盖后漏检
        if WS_STATE.get("fail_pending"):
            WS_STATE["fail_pending"] = False
            fail_count += 1
            print(f"   ⚠️ 答案被拒 (fail #{fail_count}/{MAX_FAIL_PER_SESSION})")
            if fail_count >= MAX_FAIL_PER_SESSION:
                print(f"   ⚠️ fail 次数达上限，退出会话")
                return None
            handled_fps.clear()
            # ⭐ v18: 不再清空 meta——服务器可能已发来新 challenge
            # 只清 frames，让新 challenge 的 frames 重新收集
            WS_STATE["frames"] = []
            page.wait_for_timeout(350)
            continue

        resp = WS_STATE["last_resp"]

        # ---- 成功 ----
        if resp and resp.startswith("ok:"):
            token = resp[3:]
            print(f"   🎉 全部通过！token 长度={len(token)}")
            try:
                confirm = page.locator("button:has-text('Confirm Renewal')").first
                confirm.wait_for(state="visible", timeout=5000)
                confirm.click()
                print("   ✅ 已点击 Confirm Renewal")
            except Exception as e:
                print(f"   ⚠️ Confirm: {e}")

            page.wait_for_timeout(4000)
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            wait_for_cloudflare(page)
            page.wait_for_timeout(2000)

            try:
                text = page.locator("body").inner_text()
            except Exception:
                text = ""
            m = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", text)
            if m:
                new_days = int(m.group(1))
                print(f"   📊 刷新后剩余: {new_days} 天")
                if new_days > initial_days:
                    print(f"   ✅ 续期成功！{initial_days} → {new_days} 天")
                    return True
                print("   ❌ 未增加")
                return None
            print("   ⚠️ 无法解析天数")
            return None

        # ---- 服务端拒绝 ----
        if resp in ("burned", "blocked", "rate"):
            print(f"   ❌ 服务端拒绝: {resp}")
            return None
        if resp and resp.startswith("bot:"):
            print(f"   ❌ bot: {resp}")
            return None

        meta = WS_STATE["meta"]
        if not meta or not meta.get("id"):
            page.wait_for_timeout(300)
            continue

        fp = (meta["id"], meta.get("py"), meta.get("px"),
              meta.get("ox"), meta.get("oy"))
        if fp in handled_fps:
            page.wait_for_timeout(300)
            if time.time() - last_action > 60:
                print("   ⚠️ 60s 无新状态，退出")
                return None
            continue

        nf = int(meta.get("nf") or 1)
        if len(WS_STATE["frames"]) < nf:
            page.wait_for_timeout(300)
            continue

        frames = list(WS_STATE["frames"])[:nf]
        handled_fps.add(fp)
        last_action = time.time()

        tag = f"a{attempt}_{meta['id'][:6]}"
        for i, fb in enumerate(frames):
            try:
                with open(os.path.join(
                    SCREENSHOT_DIR,
                    f"captcha_{tag}_{meta.get('kind')}_f{i}.png"), "wb") as f:
                    f.write(fb)
            except Exception:
                pass

        align_idx = ALIGN_STATE["counter"]

        result = _handle_one_stage(page, meta, frames,
                                   tag=tag, align_idx=align_idx)

        # ⭐ v18: 切换不消耗策略索引
        if result == "switched":
            switch_count += 1
            if switch_count > MAX_SWITCH_PER_SESSION:
                print("   ⚠️ 切换次数过多，退出")
                return None
            page.wait_for_timeout(800)
            continue

        if not result:
            return None

        # ⭐ v18: 只有真正提交答案后才递增策略索引
        ALIGN_STATE["counter"] += 1
        page.wait_for_timeout(600)

    print(f"   ❌ 超时 {max_total}s")
    save_screenshot(page, f"renew_timeout_a{attempt}")
    return None


def try_renew_captcha(page, initial_days, max_attempts=4):
    for attempt in range(1, max_attempts + 1):
        _reset_align_pick()
        try:
            r = _try_renew_session(page, attempt, initial_days)
        except Exception as e:
            print(f"   ❌ 第 {attempt} 次会话异常: {e}")
            import traceback
            traceback.print_exc()
            r = None

        if r is True:
            return True

        if attempt < max_attempts:
            _reset_ws_state()
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
                page.reload(wait_until="domcontentloaded", timeout=30000)
                wait_for_cloudflare(page)
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"   ⚠️ 刷新: {e}")

    print(f"   ❌ {max_attempts} 次会话均失败")
    return False


# ================= VPS 列表 =================

def get_vps_urls(page):
    def extract():
        found = []
        try:
            for link in page.locator("a[href*='/vps/']").all():
                href = link.get_attribute("href") or ""
                if not href:
                    continue
                full = urllib.parse.urljoin(SITE_BASE, href)
                path = urllib.parse.urlparse(full).path.rstrip('/')
                parts = [p for p in path.split('/') if p]
                if len(parts) == 2 and parts[0] == "vps" and \
                   parts[1] not in ("list", "new", "create"):
                    if full not in found:
                        found.append(full)
        except Exception:
            pass
        return found

    print("\n🔍 识别 VPS 实例...")
    urls = extract()
    if not urls:
        try:
            page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
            wait_for_cloudflare(page)
            page.wait_for_timeout(3000)
            urls = extract()
        except Exception:
            pass
    if urls:
        print(f"   ✅ {len(urls)} 个:")
        for u in urls:
            print(f"      - {u}")
    else:
        print("   ❌ 未找到")
    return urls


# ================= main =================

def main():
    print("#" * 50)
    print("   Openworld VPS 自动续期 (v18 - sub 优先 + fail_pending + rotate 阈值)")
    print("#" * 50)

    if not DISCORD_TOKEN:
        print("❌ 未设置 DISCORD_TOKEN")
        sys.exit(1)

    headless = os.environ.get("HEADLESS", "true").lower() == "true"
    print(f"🖥️  {'无头' if headless else '有头'}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            timezone_id="America/New_York",
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()
        _install_ws_hook(page)

        try:
            if not login_with_discord_token(page, DISCORD_TOKEN):
                send_telegram_message("❌ 登录流程失败")
                return

            vps_list = get_vps_urls(page)
            if not vps_list:
                send_telegram_message("❌ 未找到 VPS")
                return

            for idx, url in enumerate(vps_list, 1):
                print(f"\n{'=' * 50}")
                print(f"📌 [{idx}/{len(vps_list)}] {url}")
                print(f"{'=' * 50}")

                _reset_ws_state()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"⚠️ 加载: {e}")
                wait_for_cloudflare(page)
                page.wait_for_timeout(3000)

                if "/login" in page.url:
                    print("❌ 被重定向")
                    send_telegram_message("❌ 登录后仍被重定向")
                    break

                try:
                    text = page.locator("body").inner_text()
                except Exception:
                    text = ""

                if "404" in page.title() or "Page Not Found" in page.title():
                    print("❌ 404")
                    continue

                print("✅ 到达 VPS 页面")
                save_screenshot(page, f"vps_page_loaded_{idx}")

                m = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", text)
                if m:
                    days = int(m.group(1))
                    print(f"🔍 剩余: {days} 天")
                    if days > RENEW_THRESHOLD_DAYS:
                        print("⏳ 跳过续期")
                        send_telegram_message(
                            f"ℹ️ 无需续期\n实例: {url}\n剩余: {days} 天")
                        continue
                    print(f"⚠️ {days} ≤ {RENEW_THRESHOLD_DAYS}，开始续期")
                else:
                    print("⚠️ 未解析天数，强制尝试")
                    days = 0

                print(f"\n{'=' * 50}\n🔄 验证码续期\n{'=' * 50}")
                ok = try_renew_captcha(page, initial_days=days)

                if ok:
                    expiry = datetime.now(timezone(timedelta(hours=8))) + \
                             timedelta(days=6)
                    es = expiry.strftime("%Y-%m-%d %H:%M:%S") + " (GMT+8)"
                    print(f"✅ 续期成功，至: {es}")
                    send_telegram_message(
                        f"✅ 续期成功！\n实例: {url}\n续期至: {es}")
                else:
                    print("❌ 续期失败")
                    send_telegram_message(f"❌ 续期失败\n实例: {url}")

        except Exception as e:
            print(f"\n💥 异常: {e}")
            import traceback
            traceback.print_exc()
            try:
                save_screenshot(page, "uncaught_error")
            except Exception:
                pass
            send_telegram_message(f"❌ 异常: {str(e)[:200]}")

        finally:
            browser.close()
            print("\n🏁 执行完毕")


if __name__ == "__main__":
    main()
