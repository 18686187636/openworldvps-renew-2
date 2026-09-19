#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# v20-github: GitHub Actions 版（无头、Secret 配置、无交互暂停、失败非零退出）

import os
import re
import sys
import json
import random
import urllib.parse
import requests
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

try:
    import numpy as np
    import cv2
except ImportError:
    print("❌ 缺少依赖，请先运行：")
    print("   pip install playwright requests numpy opencv-python-headless")
    print("   playwright install chromium")
    sys.exit(1)


# ============================================================
# ⭐ GitHub Actions 配置：全部从 Secret / 环境变量读取
# ============================================================
CONFIG = {
    # 必填：Discord Token（GitHub Secret: DISCORD_TOKEN）
    "DISCORD_TOKEN": os.environ.get("DISCORD_TOKEN", "").strip(),

    # 可选：Telegram 通知（GitHub Secret: TG_BOT_TOKEN / TG_CHAT_ID，留空就不发）
    "TG_BOT_TOKEN":  os.environ.get("TG_BOT_TOKEN", "").strip(),
    "TG_CHAT_ID":    os.environ.get("TG_CHAT_ID", "").strip(),
    "ACCOUNT_NAME":  os.environ.get("ACCOUNT_NAME", "GitHub Actions"),

    # 站点
    "SITE_BASE": "https://openworld.eu.org",

    # 站点 Discord 服务器 id（OAuth authorize 请求用，抓包固定值）
    "DISCORD_GUILD_ID": "1525632757072658502",

    # CI 模式：无头、不暂停、截图目录在仓库内（供 Artifact 上传）
    "HEADLESS": True,
    "SCREENSHOT_DIR": "./screenshots",
    "RENEW_THRESHOLD_DAYS": 5,
}
# ============================================================


# 把配置应用到全局常量
DISCORD_TOKEN = CONFIG["DISCORD_TOKEN"]
DISCORD_GUILD_ID = CONFIG["DISCORD_GUILD_ID"]
TG_CHAT_ID    = CONFIG["TG_CHAT_ID"]
TG_BOT_TOKEN  = CONFIG["TG_BOT_TOKEN"]
ACCOUNT_NAME  = CONFIG["ACCOUNT_NAME"]
SITE_BASE     = CONFIG["SITE_BASE"]
HEADLESS      = CONFIG["HEADLESS"]
SCREENSHOT_DIR = CONFIG["SCREENSHOT_DIR"]
RENEW_THRESHOLD_DAYS = CONFIG["RENEW_THRESHOLD_DAYS"]

# 脚本所在目录
SCRIPT_DIR = Path(__file__).resolve().parent
if not os.path.isabs(SCREENSHOT_DIR):
    SCREENSHOT_DIR = str(SCRIPT_DIR / SCREENSHOT_DIR)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


SUPPORTED_KINDS = ("puzzle", "key", "rotate", "odd", "match")
MAX_SWITCH_PER_SESSION = 8
MAX_FAIL_PER_SESSION = 5

ALIGN_STRATEGIES = [
    ("none",  0),
    ("none", -1),
    ("none", +1),
    ("sub",   0),
    ("sub",  -1),
    ("sub",  +1),
    ("none", -2),
    ("none", +2),
    ("add",   0),
]

ROTATE_NCC_MIN = 0.35
ROTATE_NCC_SUBMIT = 0.50   # NCC 低于此值时认为方向不确定，直接切 alt 而非提交
ALIGN_STATE = {"counter": 0}


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
    "url": None, "meta": None, "frames": [], "last_resp": None,
    "sent": [], "closed": False, "fail_pending": False,
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
        WS_STATE["closed"] = False
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
                    if s == "fail" or s.startswith("failed:"):
                        WS_STATE["fail_pending"] = True
                    try:
                        m = json.loads(s)
                        if isinstance(m, dict) and m.get("id"):
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
        print(f"   📢 [本地] {message}")
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
            # guild_id/location_context 与真实浏览器请求一致（抓包验证）
            json={
                "guild_id": DISCORD_GUILD_ID,
                "permissions": "0", "authorize": True, "integration_type": 0,
                "location_context": {"guild_id": "10000", "channel_id": "10000",
                                     "channel_type": 10000},
            },
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
        "w": cx1 - cx0, "h": cy1 - cy0,
        "circ": circ, "nv": len(approx),
        "alpha_x0": cx0, "alpha_y0": cy0,
        "alpha_x1": cx1, "alpha_y1": cy1,
        "alpha_cx": (cx0 + cx1) / 2.0,
        "alpha_cy": (cy0 + cy1) / 2.0,
    }


def _bg_black_shapes(bg_gray):
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
        out.append({"bbox": (x, y, w, h), "circ": circ,
                    "nv": len(approx), "area": area})
    return out


def _solve_puzzle(bg_bytes, chip_bytes, meta, align_idx=0):
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
    x0 = max(0, ox - pad); y0 = max(0, oy - pad)
    x1 = min(W, ox + ow + pad); y1 = min(H, oy + oh + pad)
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

    all_scores = {}
    for angle in range(0, 360, 3):
        s, rot, rot_m, px, py = _score(angle)
        all_scores[angle] = s
        if s > best_score:
            best_score, best_angle = s, angle
            best_rot, best_rot_m = rot, rot_m
            best_px, best_py = px, py
    for da in (-2, -1, 1, 2):
        angle = (best_angle + da) % 360
        if angle not in all_scores:
            s, rot, rot_m, px, py = _score(angle)
            all_scores[angle] = s
        else:
            s = all_scores[angle]
            rot, rot_m, px, py = _score(angle)[1:]
        if s > best_score:
            best_score, best_angle = s, angle
            best_rot, best_rot_m = rot, rot_m
            best_px, best_py = px, py

    # 对称歧义消解：180° 对称图标会让 NCC 在 θ 与 θ+180 处出现几乎同高的双峰。
    # 此时单纯取最高峰可能选到"翻转"的错误朝向。用形状方向线索做裁决：
    # 比较 θ 与 θ+180 两峰的 NCC，取显著更高者；若几乎相等则保留原 best。
    alt = (best_angle + 180) % 360
    alt_score = all_scores.get(alt, _score(alt)[0])
    if alt_score > best_score + 0.02:
        s, rot, rot_m, px, py = _score(alt)
        best_score, best_angle = s, alt
        best_rot, best_rot_m = rot, rot_m
        best_px, best_py = px, py
        print(f"   🔀 对称消歧: 采用 {alt}° (ncc={alt_score:.3f}) 替代 {alt}±180 双峰")
    elif abs(alt_score - best_score) < 0.05:
        print(f"   🔀 180° 双峰接近 (ncc={best_score:.3f}/{alt_score:.3f})，保留 {best_angle}°")

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

    if best_score < ROTATE_NCC_SUBMIT:
        print(f"   ⚠️ NCC {best_score:.3f} 低于提交阈值 {ROTATE_NCC_SUBMIT}，放弃提交")
        return -1

    return (360 - best_angle) % 360


def _solve_odd(bg_bytes, meta):
    """odd: 图上 4 个图形中有一个异类，返回异类的 x 坐标。

    先按中心颜色聚类找颜色异类；若无，再按灰度 patch 与其余均值的
    NCC 找形状异类。已用抓包 4 帧验证全部命中。
    """
    bg = _decode(bg_bytes)
    if bg is None:
        raise RuntimeError("解码失败")
    rgb = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)

    items = meta.get("items") or []
    if len(items) < 2:
        raise RuntimeError("items 不足")

    # 采样每个 item 中心的平均 RGB
    colors = []
    for it in items:
        x, y = int(it["x"]), int(it["y"])
        patch = rgb[max(0, y - 3):y + 6, max(0, x - 3):x + 6]
        colors.append(patch.reshape(-1, 3).astype(np.float32).mean(axis=0))
    colors = np.array(colors)

    n = len(items)
    # 两两欧氏距离的均值，最大者即颜色异类候选
    dists = []
    for i in range(n):
        d = sum(float(np.linalg.norm(colors[i] - colors[j]))
                for j in range(n) if j != i)
        dists.append(d / (n - 1))
    ci = int(np.argmax(dists))
    if dists[ci] > float(np.mean(dists)) * 1.3:
        print(f"   🎨 odd 颜色异类: item#{ci} {items[ci]} "
              f"dist={dists[ci]:.1f} avg={np.mean(dists):.1f}")
        return int(items[ci]["x"])

    # 颜色无差异 → 形状异类: 每个 item 与其余 resize 后均值的 NCC
    gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
    patches = []
    for it in items:
        x, y, r = int(it["x"]), int(it["y"]), int(it.get("r") or 24)
        sub = gray[max(0, y - r):y + r, max(0, x - r):x + r]
        patches.append(sub.astype(np.float32))

    scores = []
    for i in range(n):
        others = [p for j, p in enumerate(patches) if j != i]
        h = min(p.shape[0] for p in others)
        w = min(p.shape[1] for p in others)
        tmpl = np.mean([cv2.resize(p, (w, h)) for p in others], axis=0)
        p = cv2.resize(patches[i], (w, h))
        av = tmpl - tmpl.mean()
        bv = p - p.mean()
        d = float(np.sqrt((av * av).sum() * (bv * bv).sum()))
        scores.append(float((av * bv).sum() / d) if d > 1e-6 else 0.0)
    si = int(np.argmin(scores))  # 最低 NCC = 最不同
    print(f"   🎨 odd 形状异类: item#{si} {items[si]} ncc={scores[si]:.3f}")
    return int(items[si]["x"])


def _solve_match(bg_bytes, meta):
    """match: 左右两列卡片，返回配对编码 matchMap[0]*100+[1]*10+[2]。

    用左右 item 的灰度 patch NCC 找最相似配对（每张左卡对应最像的右卡）。
    """
    bg = _decode(bg_bytes)
    if bg is None:
        raise RuntimeError("解码失败")
    rgb = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)

    left = meta.get("left") or []
    right = meta.get("right") or []
    if len(left) != 3 or len(right) != 3:
        raise RuntimeError("match left/right 需各 3 个")

    def _patch(it):
        x, y, r = int(it["x"]), int(it["y"]), int(it.get("r") or 24)
        sub = gray[max(0, y - r):y + r, max(0, x - r):x + r]
        return sub.astype(np.float32)

    lp = [_patch(it) for it in left]
    rp = [_patch(it) for it in right]
    h = min(p.shape[0] for p in lp + rp)
    w = min(p.shape[1] for p in lp + rp)
    lp = [cv2.resize(p, (w, h)) for p in lp]
    rp = [cv2.resize(p, (w, h)) for p in rp]

    def _ncc(a, b):
        av = a - a.mean()
        bv = b - b.mean()
        d = float(np.sqrt((av * av).sum() * (bv * bv).sum()))
        return float((av * bv).sum() / d) if d > 1e-6 else 0.0

    match_map = []
    used = set()
    for i in range(3):
        sims = [(_ncc(lp[i], rp[j]), j) for j in range(3)]
        sims.sort(reverse=True)
        for _, j in sims:
            if j not in used:
                used.add(j)
                match_map.append(j)
                break
        else:
            match_map.append(0)
    print(f"   🎨 match 配对: {match_map}")
    return match_map[0] * 100 + match_map[1] * 10 + match_map[2]


# ================= 交互 =================

def _box_pos_to_page(page, x, y):
    """把验证码图内坐标 (0..w, 0..h) 换算成页面绝对坐标。"""
    box = page.locator("#captcha_box_default")
    box.wait_for(state="visible", timeout=5000)
    bb = box.bounding_box()
    if not bb:
        raise RuntimeError("captcha box 不可见")
    meta_w = 300
    meta_h = 160
    px = bb["x"] + x / meta_w * bb["width"]
    py = bb["y"] + y / meta_h * bb["height"]
    return px, py


def _hover_to(page, tx, ty, steps=None):
    """鼠标带随机抖动自然移动到目标点。

    间隔 >16ms：前端 rec() 有 16ms 限流，间隔太短会被丢弃导致行为样本过少。
    """
    if steps is None:
        steps = random.randint(14, 24)
    # 从浏览器实际鼠标位置出发
    cur = page.evaluate(
        "() => [window.__owMouseX || window.innerWidth / 2, "
        "window.__owMouseY || window.innerHeight / 2]")
    cx, cy = cur[0], cur[1]
    for i in range(1, steps + 1):
        t = i / steps
        eased = 1 - (1 - t) ** 2
        x = cx + (tx - cx) * eased + random.uniform(-1.5, 1.5)
        y = cy + (ty - cy) * eased + random.uniform(-1.5, 1.5)
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(18, 38))
        # 记录当前位置，供下次衔接
        page.evaluate("([x, y]) => { window.__owMouseX = x; window.__owMouseY = y; }",
                      [x, y])
    page.mouse.move(tx + random.uniform(-0.5, 0.5), ty + random.uniform(-0.5, 0.5))
    page.wait_for_timeout(random.randint(60, 150))


def _click_captcha_point(page, x, y):
    """在验证码图 (x,y) 处产生 hover→click，供前端 rec() 记录行为轨迹。"""
    px, py = _box_pos_to_page(page, x, y)
    # 先在图上随意 hover 一下（产生行为样本），再移向目标点击
    box = page.locator("#captcha_box_default")
    bb = box.bounding_box()
    hx = bb["x"] + bb["width"] * random.uniform(0.2, 0.8)
    hy = bb["y"] + bb["height"] * random.uniform(0.2, 0.8)
    page.mouse.move(hx, hy)
    page.wait_for_timeout(random.randint(120, 260))
    _hover_to(page, px, py)
    page.mouse.down()
    page.wait_for_timeout(random.randint(40, 90))
    page.mouse.up()
    page.wait_for_timeout(random.randint(200, 400))


def _click_match_pairs(page, meta):
    """match: 依次点击左卡 i → 右卡 matchMap[i]，共 3 对。"""
    left = meta.get("left") or []
    right = meta.get("right") or []
    if len(left) != 3 or len(right) != 3:
        raise RuntimeError("match left/right 需各 3 个")

    match_map = _match_map_for_click(page, meta)

    # 每张卡片：先从一个"别处"的随机位置移过来（人不会从上一个卡片直接
    # 匀速滑过去），落到卡上再加 ±3~5px 随机偏移（人不会次次点精确中心），
    # 偶尔二次点击确认。
    box = page.locator("#captcha_box_default")
    bb = box.bounding_box()

    def _jitter_target(px, py):
        return px + random.uniform(-4, 4), py + random.uniform(-4, 4)

    for i in range(3):
        # 每对开始先在图内某处 hover 一下（产生行为样本 + 打破匀速感）
        hx = bb["x"] + bb["width"] * random.uniform(0.15, 0.85)
        hy = bb["y"] + bb["height"] * random.uniform(0.15, 0.85)
        page.mouse.move(hx, hy)
        page.wait_for_timeout(random.randint(150, 400))

        # 左卡
        lx, ly = _box_pos_to_page(page, left[i]["x"], left[i]["y"])
        lx, ly = _jitter_target(lx, ly)
        _hover_to(page, lx, ly)
        page.wait_for_timeout(random.randint(60, 180))
        page.mouse.down()
        page.wait_for_timeout(random.randint(50, 120))
        page.mouse.up()
        if random.random() < 0.35:
            # 偶尔二次轻点（人确认时的小习惯）
            page.wait_for_timeout(random.randint(60, 140))
            page.mouse.down()
            page.wait_for_timeout(random.randint(30, 70))
            page.mouse.up()
        # 左→右间隔大区间随机
        page.wait_for_timeout(random.randint(400, 1100))

        # 右卡
        rj = right[match_map[i]]
        rx, ry = _box_pos_to_page(page, rj["x"], rj["y"])
        rx, ry = _jitter_target(rx, ry)
        _hover_to(page, rx, ry)
        page.wait_for_timeout(random.randint(60, 180))
        page.mouse.down()
        page.wait_for_timeout(random.randint(50, 120))
        page.mouse.up()
        if random.random() < 0.35:
            page.wait_for_timeout(random.randint(60, 140))
            page.mouse.down()
            page.wait_for_timeout(random.randint(30, 70))
            page.mouse.up()
        # 每对之间更长、更随机的停顿
        page.wait_for_timeout(random.randint(500, 1300))

    page.wait_for_timeout(random.randint(600, 1100))


def _match_map_for_click(page, meta):
    """与 _solve_match 相同的配对计算（供点击用），从最近的验证码帧算。"""
    frames = WS_STATE.get("frames") or []
    if not frames:
        raise RuntimeError("无验证码帧")
    return _match_map_calc(frames[0], meta)


def _match_map_calc(bg_bytes, meta):
    """_solve_match 的配对核心（返回 match_map 列表而非编码）。"""
    bg = _decode(bg_bytes)
    if bg is None:
        raise RuntimeError("解码失败")
    rgb = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)

    left = meta.get("left") or []
    right = meta.get("right") or []

    def _patch(it):
        x, y, r = int(it["x"]), int(it["y"]), int(it.get("r") or 24)
        sub = gray[max(0, y - r):y + r, max(0, x - r):x + r]
        return sub.astype(np.float32)

    lp = [_patch(it) for it in left]
    rp = [_patch(it) for it in right]
    h = min(p.shape[0] for p in lp + rp)
    w = min(p.shape[1] for p in lp + rp)
    lp = [cv2.resize(p, (w, h)) for p in lp]
    rp = [cv2.resize(p, (w, h)) for p in rp]

    def _ncc(a, b):
        av = a - a.mean()
        bv = b - b.mean()
        d = float(np.sqrt((av * av).sum() * (bv * bv).sum()))
        return float((av * bv).sum() / d) if d > 1e-6 else 0.0

    match_map = []
    used = set()
    for i in range(3):
        sims = [(_ncc(lp[i], rp[j]), j) for j in range(3)]
        sims.sort(reverse=True)
        for _, j in sims:
            if j not in used:
                used.add(j)
                match_map.append(j)
                break
        else:
            match_map.append(0)
    return match_map


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
    dist = target_x - start_x

    page.mouse.move(start_x, y)
    page.wait_for_timeout(random.randint(80, 200))
    page.mouse.down()
    page.wait_for_timeout(random.randint(80, 180))

    # 真人拖动节奏不均匀：快启动 → 中途变慢/停顿 → 收尾微调。
    # 分 3 段：60% 距离快速推进，25% 中速并偶有微停顿，15% 缓慢逼近。
    seg_fracs = [0.60, 0.25, 0.15]
    seg_steps = [random.randint(14, 18), random.randint(8, 12), random.randint(8, 12)]
    cum = 0.0
    for si, (sf, ns) in enumerate(zip(seg_fracs, seg_steps)):
        seg_end = start_x + dist * (cum + sf)
        prev = start_x + dist * cum
        for i in range(1, ns + 1):
            t = i / ns
            eased = 1 - (1 - t) ** 2  # 各段内部再缓入
            x = prev + (seg_end - prev) * eased + random.uniform(-1.2, 1.2)
            yy = y + random.uniform(-1.5, 1.5)
            page.mouse.move(x, yy)
            base_dt = (24, 60) if si == 2 else ((40, 90) if si == 1 else (18, 38))
            page.wait_for_timeout(random.randint(*base_dt))
            # 中段偶尔停顿一下（模拟手抖 / 犹豫）
            if si == 1 and random.random() < 0.25:
                page.mouse.move(x + random.uniform(-1, 1), yy + random.uniform(-1, 1))
                page.wait_for_timeout(random.randint(60, 150))
        cum += sf

    # 收尾：轻微越过目标再回拉（更像人松手前的小调整）
    over = random.uniform(2, 5)
    page.mouse.move(target_x + over, y + random.uniform(-2, 2))
    page.wait_for_timeout(random.randint(40, 80))
    back = random.uniform(1, 3)
    page.mouse.move(target_x - back, y + random.uniform(-1, 1))
    page.wait_for_timeout(random.randint(30, 70))
    # 回拉后可能再有一次微回弹，最后才稳定
    if random.random() < 0.5:
        page.mouse.move(target_x - back + random.uniform(0.5, 1.5),
                        y + random.uniform(-0.5, 0.5))
        page.wait_for_timeout(random.randint(20, 50))
    page.mouse.move(target_x + random.uniform(-1, 1), y + random.uniform(-1, 1))
    page.wait_for_timeout(random.randint(30, 80))
    page.mouse.up()
    page.wait_for_timeout(random.randint(120, 300))


# ================= 一关处理 =================

def _handle_one_stage(page, meta, frames, tag="", align_idx=0):
    kind = meta.get("kind")
    alt = meta.get("alt")
    print(f"   🎯 kind={kind} nf={meta.get('nf')} "
          f"stage={meta.get('stage')}/{meta.get('stages')} alt={alt}")

    if kind not in SUPPORTED_KINDS:
        if alt in SUPPORTED_KINDS:
            try:
                btn = page.locator("#captcha_switch_default").first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    print(f"   🔁 切换类型: {kind} → {alt}")
                    return "switched"
            except Exception as e:
                print(f"   ⚠️ 切换失败: {e}")
        print(f"   ⚠️ {kind} 无法处理（alt={alt}），放弃会话")
        return False

    if kind in ("puzzle", "key"):
        if len(frames) < 2:
            print("   ⚠️ nf<2")
            return False
        try:
            value = _solve_puzzle(frames[0], frames[1], meta, align_idx=align_idx)
        except Exception as e:
            print(f"   ⚠️ puzzle 解算失败（{e}），尝试切换 {alt}")
            if alt in SUPPORTED_KINDS:
                try:
                    btn = page.locator("#captcha_switch_default").first
                    if btn.is_visible(timeout=1500):
                        btn.click()
                        print(f"   🔁 切换类型: {kind} → {alt}")
                        return "switched"
                except Exception as e2:
                    print(f"   ⚠️ 切换失败: {e2}")
            return False
        print(f"   🧩 value={value} vmax={meta.get('vmax')}")
        _drag_slider(page, value, int(meta.get("vmax") or 300))
        return True

    if kind == "rotate":
        if len(frames) < 2:
            print("   ⚠️ nf<2")
            return False
        try:
            value = _solve_rotate(frames[0], frames[1], meta, tag=tag)
            if value < 0:
                print(f"   ⚠️ rotate 求解失败（NCC 过低），尝试切换 {alt}")
                if alt in SUPPORTED_KINDS:
                    try:
                        btn = page.locator("#captcha_switch_default").first
                        if btn.is_visible(timeout=1500):
                            btn.click()
                            print(f"   🔁 切换类型: rotate → {alt}")
                            return "switched"
                    except Exception as e2:
                        print(f"   ⚠️ 切换失败: {e2}")
                return False
            print(f"   🎯 value={value} vmax={meta.get('vmax')}")
            _drag_slider(page, value, int(meta.get("vmax") or 359))
            return True
        except Exception as e:
            print(f"   ❌ rotate: {e}")
            return False

    if kind in ("odd", "match"):
        # 点击类: 滑块被隐藏，直接在验证码图上点击
        if len(frames) < 1:
            print("   ⚠️ nf<1")
            return False
        try:
            if kind == "odd":
                ans_x = _solve_odd(frames[0], meta)
                item = next(it for it in (meta.get("items") or [])
                            if int(it["x"]) == ans_x)
                print(f"   🖱️ odd 点击 ({item['x']},{item['y']}) ans={ans_x}")
                _click_captcha_point(page, item["x"], item["y"])
            else:
                ans = _solve_match(frames[0], meta)
                print(f"   🖱️ match ans={ans}")
                _click_match_pairs(page, meta)
            return True
        except Exception as e:
            print(f"   ❌ {kind}: {e}")
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
        if WS_STATE.get("closed"):
            print("   ⚠️ WS 已关闭，退出会话")
            return None

        if WS_STATE.get("fail_pending"):
            WS_STATE["fail_pending"] = False
            fail_count += 1
            print(f"   ⚠️ 答案被拒 (fail #{fail_count}/{MAX_FAIL_PER_SESSION})")
            if fail_count >= MAX_FAIL_PER_SESSION:
                print(f"   ⚠️ fail 次数达上限，退出会话")
                return None
            handled_fps.clear()
            WS_STATE["frames"] = []
            if WS_STATE.get("closed"):
                print("   ⚠️ fail 后 WS 已关闭，退出会话")
                return None
            page.wait_for_timeout(350)
            continue

        resp = WS_STATE["last_resp"]

        # rate 限流：服务端返回 "rate" 表示请求过频，本轮直接放弃
        if resp == "rate":
            print("   ⚠️ 收到 rate 限流，本轮不再尝试")
            return None

        if resp and resp.startswith("ok:"):
            token = resp[3:]
            print(f"   🎉 全部通过！token 长度={len(token)}")

            # 点 Confirm 前截图，记录当时页面状态
            save_screenshot(page, "confirm_before")

            # 点 Confirm 时不再吞异常谎报成功。按钮可能没渲染/被验证码层挡住，
            # 所以分级重试：等渲染 → 强制可见 → 点击，成功才进入天数确认。
            confirm_ok = False
            for sel in ("button.btn-primary:has-text('Confirm Renewal')",
                         "button:has-text('Confirm Renewal')",
                         "button:has-text('Confirm')"):
                try:
                    confirm = page.locator(sel).first
                    confirm.wait_for(state="visible", timeout=6000)
                    confirm.click(timeout=3000)
                    print(f"   ✅ 已点击 Confirm Renewal ({sel})")
                    confirm_ok = True
                    break
                except Exception as e:
                    print(f"   ⚠️ Confirm 选择器 {sel} 失败: {str(e)[:120]}")
            if not confirm_ok:
                # 都没点中：再宽等一次后强制点第一个匹配的（force 绕过被遮挡）
                try:
                    page.locator("button:has-text('Confirm')").first.click(
                        timeout=3000, force=True)
                    print("   ✅ 已强制点击 Confirm")
                    confirm_ok = True
                except Exception as e:
                    print(f"   ❌ Confirm 未能点击: {e}")

            # 点 Confirm 后立刻截图，看页面实际状态
            save_screenshot(page, "confirm_after")

            # 等这个 POST 真正完成。Confirm 是 <form method=POST> 的 submit 按钮，
            # 点下去浏览器会发 POST /vps/{id}/renew，成功后跳回带新天数的页面。
            # 之前的问题是：点完立刻 page.reload() 会取消/打断这个 POST，导致加天没落地。
            # 现在改为等 URL 变化或 body 出现成功提示，最多等 15s。
            page.wait_for_timeout(3000)
            try:
                page.wait_for_url(
                    re.compile(r"/vps/"),
                    timeout=15000,
                    wait_until="domcontentloaded")
            except Exception:
                pass
            wait_for_cloudflare(page)

            # 再截一张，看提交后页面
            save_screenshot(page, "confirm_result")

            # 服务端给 VPS 加天可能有延迟，轮询读天数（最多 3 次，间隔 8s 防 rate 限流）
            new_days = None
            for _ in range(3):
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
                    candidate = int(m.group(1))
                    print(f"   📊 刷新后剩余: {candidate} 天")
                    if candidate > initial_days:
                        new_days = candidate
                        break
                page.wait_for_timeout(8000)

            if new_days is not None:
                print(f"   ✅ 续期成功！{initial_days} → {new_days} 天")
                return True

            print(f"   ❌ 已拿到 token，Confirm={confirm_ok}，但多次刷新后"
                  f"仍未检测到天数增加，判定失败")
            return None

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

        fp = (meta["id"], meta.get("stage"), meta.get("nf"))
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

        if result == "switched":
            switch_count += 1
            if switch_count > MAX_SWITCH_PER_SESSION:
                print("   ⚠️ 切换次数过多，退出")
                return None
            page.wait_for_timeout(800)
            continue

        if not result:
            return None

        ALIGN_STATE["counter"] += 1
        page.wait_for_timeout(600)

    print(f"   ❌ 超时 {max_total}s")
    save_screenshot(page, f"renew_timeout_a{attempt}")
    return None


def try_renew_captcha(page, initial_days, max_attempts=6):
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


# ================= 配置检查 =================

def check_config():
    print("=" * 50)
    print("⚙️  配置检查")
    print("=" * 50)
    print(f"   DISCORD_TOKEN : {'✅ 已设置 (' + DISCORD_TOKEN[:12] + '...)' if DISCORD_TOKEN else '❌ 空'}")
    print(f"   TG 通知       : {'✅ 已启用' if (TG_BOT_TOKEN and TG_CHAT_ID) else '⚪ 未启用（跳过）'}")
    print(f"   SITE_BASE     : {SITE_BASE}")
    print(f"   运行模式      : {'✅ 无头' if HEADLESS else '❌ 有头'}")
    print(f"   截图目录      : {SCREENSHOT_DIR}")

    if not DISCORD_TOKEN:
        print()
        print("❌ 缺少 DISCORD_TOKEN")
        print("   请在 GitHub 仓库 → Settings → Secrets and variables → Actions")
        print("   添加 Secret：")
        print("     - DISCORD_TOKEN : 必填")
        print("     - TG_BOT_TOKEN  : 可选（Telegram 机器人 token）")
        print("     - TG_CHAT_ID    : 可选（Telegram 对话 id）")
        return False
    return True


# ================= main =================

def main() -> int:
    print("#" * 60)
    print("   Openworld VPS 自动续期 (v20-github GitHub Actions 版)")
    print("#" * 60)

    if not check_config():
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            proxy={
                "server": os.environ.get("CHROME_PROXY", ""),
            } if os.environ.get("CHROME_PROXY") else None,
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

        failures = 0
        try:
            if not login_with_discord_token(page, DISCORD_TOKEN):
                send_telegram_message("❌ 登录流程失败")
                return 1

            vps_list = get_vps_urls(page)
            if not vps_list:
                send_telegram_message("❌ 未找到 VPS")
                return 1

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
                    failures += 1
                    send_telegram_message(f"❌ 续期失败\n实例: {url}")

        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断 (Ctrl+C)")
            try:
                save_screenshot(page, "user_interrupt")
            except Exception:
                pass
            return 1
        except Exception as e:
            print(f"\n💥 异常: {e}")
            import traceback
            traceback.print_exc()
            try:
                save_screenshot(page, "uncaught_error")
            except Exception:
                pass
            send_telegram_message(f"❌ 异常: {str(e)[:200]}")
            return 1
        finally:
            browser.close()
            print("\n🏁 执行完毕")

    if failures:
        print(f"\n❌ 共 {failures} 台 VPS 续期失败")
        return 1
    print("\n✅ 全部 VPS 处理完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
