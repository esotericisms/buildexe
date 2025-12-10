"""
overlay_screenshots_buffer_gpt5_tinybox_fixed.py (Discord webhook, GPT-5 Responses API)

Hotkeys (global, work from any app):
 - Ctrl+Alt+X  => Capture fullscreen screenshot (buffered in memory)
 - Ctrl+Alt+S  => Send all buffered screenshots to GPT-5, show preview in overlay, then clear buffer
 - Ctrl+Alt+O  => Toggle overlay show/hide

Local (when overlay focused):
 - Esc         => Quit
 - Drag with left mouse to move the box

Requires:
 - pip install pygame pillow requests
"""

import os, sys, time, json, base64, threading, ctypes, io
from ctypes import wintypes
import pygame
from PIL import ImageGrab
import requests

# ---------------------- Config (kept inline, as requested) ----------------------
OPENAI_API_KEY = "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"
OPENAI_MODEL   = "gpt-5"  # API alias for "gpt-5-thinking" system card
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1424560766421569547/nIS3MkNlDg_tWtRvGMYMzP_yPKk2C5x5609LckkdWycvKMGfqSkBIB-cyv7cFv9ZKMlw"

OPENAI_API_URL = "https://api.openai.com/v1/responses"  # Responses API

# Tiny discreet box defaults (fixed-height, width-only auto-resize)
START_W, START_H = 20, 15
BG = (211, 211, 211)     # light gray
FG = (20, 20, 20)        # dark text
PADDING = 2
FIXED_FONT_PT = 11       # fits inside 15px height
MIN_W = 20
FIXED_H = START_H

# ---------------------- Windows work area (for max width clamp) ----------------------
user32 = ctypes.windll.user32
SPI_GETWORKAREA = 0x0030
work_rect = wintypes.RECT()
ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work_rect), 0)
MAX_W = (work_rect.right - work_rect.left) - 8

# ---------------------- Init + position ----------------------
start_x = work_rect.left + 4
start_y = work_rect.bottom - FIXED_H - 4
os.environ["SDL_VIDEO_WINDOW_POS"] = f"{start_x},{start_y}"

pygame.init()
screen = pygame.display.set_mode((START_W, FIXED_H), pygame.NOFRAME)
pygame.display.set_caption("")
clock = pygame.time.Clock()
hwnd = pygame.display.get_wm_info().get('window')

# Always-on-top & constants
HWND_TOPMOST   = -1
SWP_NOSIZE     = 0x0001
SWP_NOMOVE     = 0x0002
SWP_SHOWWINDOW = 0x0040
SWP_NOACTIVATE = 0x0010
SW_HIDE        = 0
SW_SHOW        = 5
SW_RESTORE     = 9

ctypes.windll.user32.SetWindowPos(
    hwnd, HWND_TOPMOST, start_x, start_y, 0, 0, SWP_NOSIZE | SWP_SHOWWINDOW
)

# --- Extra Win32 constants used to hide from taskbar ---
GWL_EXSTYLE      = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW  = 0x00040000

def hide_from_taskbar(hwnd):
    """Make the given HWND a TOOLWINDOW (no taskbar) and remove APPWINDOW."""
    try:
        if not hwnd:
            return
        # Select correct Get/Set functions for 32/64-bit
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            GetEx = ctypes.windll.user32.GetWindowLongPtrW
            SetEx = ctypes.windll.user32.SetWindowLongPtrW
        else:
            GetEx = ctypes.windll.user32.GetWindowLongW
            SetEx = ctypes.windll.user32.SetWindowLongW

        ex_style = GetEx(hwnd, GWL_EXSTYLE)
        # Add TOOLWINDOW, remove APPWINDOW
        new_style = (ex_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        SetEx(hwnd, GWL_EXSTYLE, new_style)

        # Force the window manager to notice the change and keep topmost
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        )
    except Exception as e:
        # don't crash the UI if this fails
        print("[HIDE TASKBAR ERR]", e)

# enforce toolwindow style so the overlay never gets a taskbar button
hide_from_taskbar(hwnd)

overlay_visible = True
status_text = ""  # blank on open

# ---------------------- Helpers: window position/size ----------------------
def get_win_pos():
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top

def get_window_size():
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top

def set_window_size_width(new_w):
    """Resize ONLY width (clamped), keep height & position."""
    global screen, hwnd
    new_w = max(MIN_W, min(int(new_w), MAX_W))
    x, y = get_win_pos()
    screen = pygame.display.set_mode((new_w, FIXED_H), pygame.NOFRAME)
    screen.fill(BG); pygame.display.update()
    hwnd = pygame.display.get_wm_info().get('window')
    ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_SHOWWINDOW)
    hide_from_taskbar(hwnd)

# ---------------------- Font helper (fixed-size) ----------------------
BASE_FONT_NAME = None  # default pygame font
fixed_font = pygame.font.Font(BASE_FONT_NAME, FIXED_FONT_PT)

# ---------------------- Thread-safe UI event IDs ----------------------
EVT_STATUS   = pygame.USEREVENT + 1  # set status text
EVT_RESULT   = pygame.USEREVENT + 2  # set final result text (e.g., API response)
EVT_ERROR    = pygame.USEREVENT + 3  # show "err"

def post_status(msg: str):
    pygame.event.post(pygame.event.Event(EVT_STATUS, {"text": msg or ""}))

def post_result(msg: str):
    pygame.event.post(pygame.event.Event(EVT_RESULT, {"text": msg or ""}))

def post_error():
    pygame.event.post(pygame.event.Event(EVT_ERROR, {}))

# ---------------------- Drawing / overlay behavior ----------------------
def draw_overlay():
    if not overlay_visible:
        return
    surf = pygame.display.get_surface()
    if surf is None:
        return
    surf.fill(BG)
    text = (status_text or "")
    if len(text) > 300:
        text = text[:300] + "…"
    text_surf = fixed_font.render(text, True, FG)
    tw, th = text_surf.get_size()
    desired_w = max(MIN_W, tw + 2 * PADDING)
    cur_w, _ = get_window_size()
    if desired_w != cur_w:
        set_window_size_width(desired_w)
        surf = pygame.display.get_surface()
        if surf is None:
            return
    max_h = max(2, surf.get_height() - 2 * PADDING)
    x = PADDING
    y = PADDING + (max_h - th) // 2
    surf.blit(text_surf, (x, y))
    pygame.display.update()

def show_overlay():
    global overlay_visible, hwnd
    overlay_visible = True
    hwnd = pygame.display.get_wm_info().get('window')
    hide_from_taskbar(hwnd)
    ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
    rect = wintypes.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, rect.left, rect.top, 0, 0, SWP_NOSIZE | SWP_SHOWWINDOW)
    try: ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception: pass
    hide_from_taskbar(hwnd)
    draw_overlay()

def hide_overlay():
    global overlay_visible
    overlay_visible = False
    ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)

def toggle_overlay():
    hide_overlay() if overlay_visible else show_overlay()

def set_status(msg: str):
    global status_text
    status_text = msg or ""

draw_overlay()

# ---------------------- Dragging (frameless window) ----------------------
dragging = False
drag_offset = (0, 0)

def move_window_to(x, y):
    ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_SHOWWINDOW)

# ---------------------- In-memory screenshots ----------------------
screenshots_buffer = []

def capture_screenshot_to_buffer():
    """Capture screenshot to base64 (PNG) and store in memory."""
    try:
        if not overlay_visible:
            show_overlay()
        img = ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        screenshots_buffer.append(b64)
        n = len(screenshots_buffer)
        post_status(f"{n}/{n}")
    except Exception as e:
        post_error()
        print("[CAPTURE ERR]", e)

# ---------------------- Discord Webhook Function ----------------------
def send_to_discord(screenshot_data):
    """Send screenshot to Discord webhook"""
    try:
        if not DISCORD_WEBHOOK_URL:
            return False
        image_bytes = base64.b64decode(screenshot_data)
        files = {'file': ('screenshot.png', image_bytes, 'image/png')}
        data = {'content': 'New Screenshot:'}
        response = requests.post(DISCORD_WEBHOOK_URL, files=files, data=data, timeout=30)
        return response.status_code in (200, 204)
    except Exception as e:
        print(f"[DISCORD ERR] {e}")
        return False

# ---------------------- Minimal, reliable text extractor for Responses API ----------------------
def _just_text(res_json):
    """
    Return ONLY the model's text across common Responses API shapes.
    If nothing textual is found, return "".
    """
    if not isinstance(res_json, dict):
        return ""
    # A) Convenience field
    t = res_json.get("output_text")
    if isinstance(t, str) and t.strip():
        return t.strip()
    # B) Structured 'output' array
    out = res_json.get("output")
    if isinstance(out, list):
        parts = []
        for item in out:
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                        if isinstance(c.get("text"), str):
                            parts.append(c["text"])
        if parts:
            return "\n".join(parts).strip()
    # C) Chat-style fallback
    choices = res_json.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"].strip()
    # D) Last-ditch: top-level 'content'
    c = res_json.get("content")
    if isinstance(c, str) and c.strip():
        return c.strip()
    return ""

# ---------------------- Responses API worker (GPT-5, multimodal) ----------------------
def worker_send_images(copy_of_images):
    """
    Runs in a background thread. Do NOT call pygame here.
    Posts UI events back to the main thread.
    """
    try:
        post_status("loading...")

        # Optional: send screenshots to Discord
        for screenshot_data in copy_of_images:
            send_to_discord(screenshot_data)

        if not OPENAI_API_KEY:
            print("[API ERR] Missing OPENAI_API_KEY")
            post_error()
            return

        # Prompts
        system_prompt = (
            "You are an expert academic assistant that analyzes educational content. "
            "Format your answers appropriately based on the question type."
        )

        # Prompting: keep concise instructions — you can adjust for verification if desired
        user_text = (
            "Analyze these screenshots and provide answers to all academic content.\n\n"
            "MULTIPLE CHOICE QUESTIONS:\n"
            "- Answer every MCQ in order from top to bottom\n"
            "- Use format: 1:B 2:D 3:C 4:E (one line only)\n"
            "- Only include question numbers and letters\n"
            "- No explanations for MCQs\n\n"
            "FREE RESPONSE QUESTIONS:\n"
            "- Provide well-reasoned and correct answers\n"
            "AUTOMATIC DETECTION:\n"
            "- If you see MCQs, use the one-line format\n"
            "- If you see FRQs, provide full answers\n"
            "- If you see both, handle MCQs first then FRQs"
        )

        # Build Responses API "input" (multimodal): input_text + input_image parts
        input_parts = [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user",   "content": [{"type": "input_text", "text": user_text}]}
        ]

        # Append each screenshot as an input_image (data URL)
        for b64 in copy_of_images:
            input_parts[-1]["content"].append({
                "type": "input_image",
                "image_url": f"data:image/png;base64,{b64}"
            })

        # NOTE: use gpt-5 (API alias for gpt-5-thinking) with medium reasoning
        payload = {
            "model": OPENAI_MODEL,
            "input": input_parts,
            # medium token budget for reasonably detailed answers; raise as needed for longer FRQs
            "max_output_tokens": 64000,
            # GPT-5 uses 'reasoning' and 'text' controls; temperature/top_p are not supported for GPT-5 family.
            "reasoning": {"effort": "medium"},
            "text": {"verbosity": "medium"}
        }

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }

        # robust request with a larger timeout (GPT-5 reasoning can take longer)
        resp = requests.post(OPENAI_API_URL, headers=headers, data=json.dumps(payload), timeout=300)

        # If server errored, show body raw (preview) and bail
        if resp.status_code != 200:
            body = resp.text or ""
            post_result(f"[{resp.status_code}] " + (" ".join(body.split())[:300] or "error"))
            return

        res_json = resp.json()
        text_out = _just_text(res_json)

        if not text_out:
            # Log payload head for debugging (console only)
            try:
                print("[RESP DEBUG] No text extracted; raw payload head:",
                      (json.dumps(res_json, ensure_ascii=False)[:2000]))
            except Exception:
                print("[RESP DEBUG] No text extracted; raw payload (non-serializable)")
            post_result("no text from model")
            return

        # Show compact preview in the tiny overlay (no file writes, no clipboard)
        preview = " ".join(text_out.split())[:300]
        post_result(preview if preview else "got response ✓")

    except Exception as e:
        print("[WORKER EXC]", e)
        post_error()

# ---------------------- Global hotkeys (Windows) ----------------------
WM_HOTKEY   = 0x0312
MOD_ALT     = 0x0001
MOD_CONTROL = 0x0002
VK_X        = 0x58
VK_S        = 0x53
VK_O        = 0x4F

HOTKEY_ID_CAPTURE  = 1001
HOTKEY_ID_SEND     = 1002
HOTKEY_ID_TOGGLE   = 1003

def hotkey_thread():
    if not user32.RegisterHotKey(None, HOTKEY_ID_CAPTURE, MOD_CONTROL | MOD_ALT, VK_X):
        print("[WARN] Hotkey Ctrl+Alt+X failed")
    if not user32.RegisterHotKey(None, HOTKEY_ID_SEND, MOD_CONTROL | MOD_ALT, VK_S):
        print("[WARN] Hotkey Ctrl+Alt+S failed")
    if not user32.RegisterHotKey(None, HOTKEY_ID_TOGGLE, MOD_CONTROL | MOD_ALT, VK_O):
        print("[WARN] Hotkey Ctrl+Alt+O failed")

    msg = wintypes.MSG()
    while True:
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r == 0: break
        if msg.message == WM_HOTKEY:
            if msg.wParam == HOTKEY_ID_CAPTURE:
                pygame.event.post(pygame.event.Event(pygame.USEREVENT, {"action":"capture"}))
            elif msg.wParam == HOTKEY_ID_SEND:
                pygame.event.post(pygame.event.Event(pygame.USEREVENT, {"action":"send"}))
            elif msg.wParam == HOTKEY_ID_TOGGLE:
                pygame.event.post(pygame.event.Event(pygame.USEREVENT, {"action":"toggle"}))

threading.Thread(target=hotkey_thread, daemon=True).start()

print("Overlay running.")
print("Ctrl+Alt+X=capture • Ctrl+Alt+S=send • Ctrl+Alt+O=toggle • Esc=quit")

# ---------------------- Main loop (ALL UI here) ----------------------
while True:
    for event in pygame.event.get():
        # Only Esc quits
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            user32.PostQuitMessage(0); pygame.quit(); sys.exit()

        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            dragging = True
            mx, my = pygame.mouse.get_pos()
            win_x, win_y = get_win_pos()
            drag_offset = (mx + win_x, my + win_y)

        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            dragging = False

        elif event.type == pygame.MOUSEMOTION and dragging:
            mx, my = pygame.mouse.get_pos()
            new_x = drag_offset[0] - mx
            new_y = drag_offset[1] - my
            move_window_to(new_x, new_y)

        elif event.type == pygame.USEREVENT:
            act = event.dict.get("action")
            if act == "capture":
                capture_screenshot_to_buffer()
            elif act == "send":
                imgs = list(screenshots_buffer)
                screenshots_buffer.clear()
                threading.Thread(target=worker_send_images, args=(imgs,), daemon=True).start()
            elif act == "toggle":
                toggle_overlay()
                if overlay_visible:
                    draw_overlay()

        elif event.type == EVT_STATUS:
            set_status(event.text)
        elif event.type == EVT_RESULT:
            set_status(event.text)
        elif event.type == EVT_ERROR:
            set_status("err")

    if overlay_visible:
        draw_overlay()
        clock.tick(30)
    else:
        time.sleep(0.03)


