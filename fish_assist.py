import argparse
import ctypes
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import keyboard
import mss
import numpy as np
import pyautogui

try:
    import pydirectinput
except ImportError:
    pydirectinput = None


CONFIG_PATH = Path(__file__).with_name("fish_assist_config.json")

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
INPUT_MOUSE = 0
VK_LBUTTON = 0x01
VK_XBUTTON1 = 0x05
BOBBER_REEL_COOLDOWN_S = 1.5
BOBBER_TO_MINIGAME_DELAY_S = 0.8
BOBBER_REEL_CLICK_COUNT = 5
POST_CATCH_DELAY_S = 5.0
MODE_CATCH = "catch"
MODE_BOBBER = "bobber"
MODE_FISHER = "fisher"


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class InputUnion(ctypes.Union):
    _fields_ = [("mi", MouseInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", InputUnion)]


@dataclass
class Config:
    region_left: int = 0
    region_top: int = 0
    region_width: int = 0
    region_height: int = 0
    bobber_left: int = 0
    bobber_top: int = 0
    bobber_width: int = 0
    bobber_height: int = 0
    cast_x: int = 0
    cast_y: int = 0
    click_x: int = 0
    click_y: int = 0
    deadzone_px: int = 18
    click_interval_s: float = 0.08
    loop_sleep_s: float = 0.01
    min_bar_area: int = 1200
    min_fish_area: int = 80
    bar_y_tolerance_px: int = 45
    click_duration_s: float = 0.025


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)
    defaults = asdict(Config())
    config = Config(**{**defaults, **{key: value for key, value in data.items() if key in defaults}})
    if (not config.click_x or not config.click_y) and config.region_width > 0 and config.region_height > 0:
        config.click_x = config.region_left + config.region_width // 2
        config.click_y = config.region_top + config.region_height // 2
        save_config(config)
    return config


def save_config(config: Config) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(asdict(config), file, indent=2)


def is_left_mouse_pressed() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def wait_for_calibration_point(prompt: str):
    print(prompt)
    while is_left_mouse_pressed():
        time.sleep(0.03)

    while True:
        if keyboard.is_pressed("f8") or is_left_mouse_pressed():
            position = pyautogui.position()
            while is_left_mouse_pressed() or keyboard.is_pressed("f8"):
                time.sleep(0.03)
            return position
        time.sleep(0.03)


def select_region(config: Config) -> Config:
    left, top = wait_for_calibration_point("Move your mouse to the TOP-LEFT of the fishing UI, then left-click or press F8.")
    print(f"Top-left saved: {left}, {top}")

    time.sleep(0.4)
    right, bottom = wait_for_calibration_point("Move your mouse to the BOTTOM-RIGHT of the fishing UI, then left-click or press F8.")
    print(f"Bottom-right saved: {right}, {bottom}")

    config.region_left = min(left, right)
    config.region_top = min(top, bottom)
    config.region_width = abs(right - left)
    config.region_height = abs(bottom - top)
    config.click_x = config.region_left + config.region_width // 2
    config.click_y = config.region_top + config.region_height // 2
    save_config(config)
    print(f"Saved region to {CONFIG_PATH}")
    print(f"Clicks will be sent to: {config.click_x}, {config.click_y}")
    return config


def select_bobber_region(config: Config) -> Config:
    left, top = wait_for_calibration_point("Move your mouse to the TOP-LEFT of the bobber/Reel prompt, then left-click or press F8.")
    print(f"Bobber top-left saved: {left}, {top}")

    time.sleep(0.4)
    right, bottom = wait_for_calibration_point("Move your mouse to the BOTTOM-RIGHT of the bobber/Reel prompt, then left-click or press F8.")
    print(f"Bobber bottom-right saved: {right}, {bottom}")

    config.bobber_left = min(left, right)
    config.bobber_top = min(top, bottom)
    config.bobber_width = abs(right - left)
    config.bobber_height = abs(bottom - top)
    save_config(config)
    print(f"Saved bobber region to {CONFIG_PATH}")
    print(f"Bobber preview size: {config.bobber_width}x{config.bobber_height}")
    return config


def select_cast_point(config: Config) -> Config:
    x, y = wait_for_calibration_point("Hover the Cast/Start button, then left-click or press F8.")
    config.cast_x = x
    config.cast_y = y
    save_config(config)
    print(f"Saved Cast click point: {x}, {y}")
    return config


def detect_state(frame_bgr: np.ndarray, config: Config):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

    # The control bar in the reference image is a saturated cyan/blue rectangle.
    bar_mask = cv2.inRange(hsv, np.array([95, 220, 200]), np.array([103, 255, 255]))
    bar_mask = cv2.morphologyEx(bar_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(bar_mask, 8)
    bar = None
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        x = int(stats[index, cv2.CC_STAT_LEFT])
        y = int(stats[index, cv2.CC_STAT_TOP])
        w = int(stats[index, cv2.CC_STAT_WIDTH])
        h = int(stats[index, cv2.CC_STAT_HEIGHT])
        if area < config.min_bar_area:
            continue
        if w < 50 or w > frame_bgr.shape[1] * 0.75:
            continue
        if h < 30 or h > 90:
            continue
        cx, cy = centroids[index]
        candidate = {
            "area": area,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "cx": float(cx),
            "cy": float(cy),
        }
        if bar is None or candidate["h"] > bar["h"]:
            bar = candidate
    if bar is None:
        return None

    y_min = max(0, int(bar["y"] - config.bar_y_tolerance_px))
    y_max = min(frame_bgr.shape[0], int(bar["y"] + bar["h"] + config.bar_y_tolerance_px))
    lane = frame_bgr[y_min:y_max, :]

    gray = cv2.cvtColor(lane, cv2.COLOR_BGR2GRAY)
    fish_mask = cv2.inRange(gray, 0, 70)

    # Exclude dark panel/progress-bar edges by keeping fish-sized components near the control lane.
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(fish_mask, 8)
    fish = None
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        x = int(stats[index, cv2.CC_STAT_LEFT])
        y = int(stats[index, cv2.CC_STAT_TOP])
        w = int(stats[index, cv2.CC_STAT_WIDTH])
        h = int(stats[index, cv2.CC_STAT_HEIGHT])
        if area < config.min_fish_area or area > 4000:
            continue
        if h < 10 or w < 10 or w > 90 or h > 70:
            continue
        cx, cy = centroids[index]
        candidate = {
            "area": area,
            "x": x,
            "y": y + y_min,
            "w": w,
            "h": h,
            "cx": float(cx),
            "cy": float(cy + y_min),
        }
        distance_to_bar = abs(candidate["cy"] - bar["cy"])
        if fish is None or distance_to_bar < fish["distance_to_bar"]:
            fish = {**candidate, "distance_to_bar": distance_to_bar}

    if fish is None:
        return None

    bar_left = bar["x"]
    bar_right = bar["x"] + bar["w"]
    fish_x = fish["cx"]
    deadzone = config.deadzone_px

    if fish_x < bar_left + deadzone:
        action = "release"
    elif fish_x > bar_right - deadzone:
        action = "click"
    else:
        bar_center = bar["cx"]
        action = "click" if fish_x > bar_center + deadzone else "release"

    return {"bar": bar, "fish": fish, "action": action}


def detect_caught(frame_bgr: np.ndarray) -> bool:
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    green_mask = cv2.inRange(hsv, np.array([45, 80, 120]), np.array([85, 255, 255]))
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(green_mask, 8)
    letters = 0
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        x = int(stats[index, cv2.CC_STAT_LEFT])
        y = int(stats[index, cv2.CC_STAT_TOP])
        w = int(stats[index, cv2.CC_STAT_WIDTH])
        h = int(stats[index, cv2.CC_STAT_HEIGHT])
        if 100 <= area <= 1200 and 6 <= w <= 40 and 18 <= h <= 45:
            letters += 1
    return letters >= 4


def draw_preview(frame: np.ndarray, state, enabled: bool) -> np.ndarray:
    output = frame.copy()
    if state:
        bar = state["bar"]
        fish = state["fish"]
        cv2.rectangle(output, (bar["x"], bar["y"]), (bar["x"] + bar["w"], bar["y"] + bar["h"]), (0, 255, 255), 2)
        cv2.rectangle(output, (fish["x"], fish["y"]), (fish["x"] + fish["w"], fish["y"] + fish["h"]), (0, 0, 255), 2)
        label = state["action"] if enabled else "paused"
        cv2.putText(output, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    else:
        label = "not detected" if enabled else "paused"
        cv2.putText(output, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 80, 255), 2)
    return output


def detect_red_bobber(frame_bgr: np.ndarray):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    lower_red = cv2.inRange(hsv, np.array([0, 80, 80]), np.array([12, 255, 255]))
    upper_red = cv2.inRange(hsv, np.array([160, 60, 55]), np.array([180, 255, 255]))
    mask = cv2.bitwise_or(lower_red, upper_red)

    # Ignore lower UI buttons and text; the red bobber is in the water area.
    mask[int(frame_bgr.shape[0] * 0.72) :, :] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        x = int(stats[index, cv2.CC_STAT_LEFT])
        y = int(stats[index, cv2.CC_STAT_TOP])
        w = int(stats[index, cv2.CC_STAT_WIDTH])
        h = int(stats[index, cv2.CC_STAT_HEIGHT])
        if area < 20 or area > 1200:
            continue
        if y < frame_bgr.shape[0] * 0.04:
            continue
        if w < 4 or h < 4 or w > 90 or h > 55:
            continue
        aspect = w / max(1, h)
        if aspect < 0.9 or aspect > 2.8:
            continue
        context_x1 = max(0, x - 35)
        context_y1 = max(0, y - 35)
        context_x2 = min(frame_bgr.shape[1], x + w + 35)
        context_y2 = min(frame_bgr.shape[0], y + h + 35)
        context = hsv[context_y1:context_y2, context_x1:context_x2]
        blue_context = cv2.inRange(context, np.array([90, 50, 80]), np.array([115, 255, 255]))
        if cv2.countNonZero(blue_context) / max(1, blue_context.size) < 0.14:
            continue

        line_x1 = max(0, x - 80)
        line_y1 = max(0, y - 90)
        line_x2 = min(frame_bgr.shape[1], x + w + 80)
        line_y2 = min(frame_bgr.shape[0], y + h + 70)
        line_context = hsv[line_y1:line_y2, line_x1:line_x2]
        line_mask = cv2.inRange(line_context, np.array([0, 0, 45]), np.array([180, 95, 185]))
        if cv2.countNonZero(line_mask) < 15:
            continue
        cx, cy = centroids[index]
        candidate = {"area": area, "x": x, "y": y, "w": w, "h": h, "cx": int(cx), "cy": int(cy)}
        if best is None or candidate["area"] > best["area"]:
            best = candidate
    return best


def find_bobber_reel_button(frame_bgr: np.ndarray):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    cyan_mask = cv2.inRange(hsv, np.array([85, 120, 120]), np.array([100, 255, 255]))

    # Only search the bobber prompt button area. This keeps it separate from minigame logic.
    cyan_mask[: int(frame_bgr.shape[0] * 0.68), :] = 0
    cyan_mask[:, int(frame_bgr.shape[1] * 0.58) :] = 0
    cyan_mask = cv2.morphologyEx(cyan_mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))

    count, labels, stats, centroids = cv2.connectedComponentsWithStats(cyan_mask, 8)
    best = None
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        x = int(stats[index, cv2.CC_STAT_LEFT])
        y = int(stats[index, cv2.CC_STAT_TOP])
        w = int(stats[index, cv2.CC_STAT_WIDTH])
        h = int(stats[index, cv2.CC_STAT_HEIGHT])
        if area < 4000 or w < 100 or h < 30:
            continue
        cx, cy = centroids[index]
        candidate = {"area": area, "x": x, "y": y, "w": w, "h": h, "cx": int(cx), "cy": int(cy)}
        if best is None or candidate["area"] > best["area"]:
            best = candidate
    return best


def detect_bobber_prompt_text(frame_bgr: np.ndarray):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    white_mask = cv2.inRange(hsv, np.array([0, 0, 190]), np.array([180, 90, 255]))

    y1 = int(frame_bgr.shape[0] * 0.62)
    y2 = int(frame_bgr.shape[0] * 0.94)
    text_band = white_mask[y1:y2, :]
    white_pixels = cv2.countNonZero(text_band)
    ratio = white_pixels / max(1, text_band.size)

    if ratio < 0.035:
        return None
    return {"x": 0, "y": y1, "w": frame_bgr.shape[1], "h": y2 - y1, "ratio": float(ratio)}


def draw_bobber_preview(frame_bgr: np.ndarray, enabled: bool, message: str) -> np.ndarray:
    output = frame_bgr.copy()
    red_bobber = detect_red_bobber(output)
    reel_button = find_bobber_reel_button(output)
    prompt_text = detect_bobber_prompt_text(output)

    if prompt_text:
        cv2.rectangle(
            output,
            (prompt_text["x"], prompt_text["y"]),
            (prompt_text["x"] + prompt_text["w"] - 1, prompt_text["y"] + prompt_text["h"]),
            (255, 255, 255),
            2,
        )
        cv2.putText(output, "bobber prompt", (12, max(58, prompt_text["y"] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    if red_bobber:
        cv2.rectangle(
            output,
            (red_bobber["x"], red_bobber["y"]),
            (red_bobber["x"] + red_bobber["w"], red_bobber["y"] + red_bobber["h"]),
            (0, 0, 255),
            2,
        )
        cv2.putText(output, "red bobber", (red_bobber["x"], max(24, red_bobber["y"] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    if reel_button:
        cv2.rectangle(
            output,
            (reel_button["x"], reel_button["y"]),
            (reel_button["x"] + reel_button["w"], reel_button["y"] + reel_button["h"]),
            (255, 255, 0),
            2,
        )
        cv2.drawMarker(output, (reel_button["cx"], reel_button["cy"]), (255, 255, 0), cv2.MARKER_CROSS, 28, 2)
        cv2.putText(output, f"Reel click {reel_button['cx']},{reel_button['cy']}", (reel_button["x"], max(24, reel_button["y"] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

    label = message if enabled else "paused"
    cv2.putText(output, label, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

    max_width = 1100
    if output.shape[1] > max_width:
        scale = max_width / output.shape[1]
        output = cv2.resize(output, (max_width, int(output.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    return output


def force_screen_click(x: int, y: int, duration_s: float) -> None:
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.18)
    if pydirectinput is None:
        raise SystemExit("pydirectinput is not installed. Run: python -m pip install -r requirements.txt")
    pydirectinput.click(x=x, y=y)


def click_bobber_reel(reel_button: dict, tracker: dict, clicker: str, config: Config, screen_left: int, screen_top: int, message_prefix: str, verbose: bool) -> str:
    now = time.monotonic()
    click_x = screen_left + reel_button["cx"]
    click_y = screen_top + reel_button["cy"] - max(0, int(reel_button.get("h", 0) * 0.12))
    clicked_points = click_spread(click_x, click_y, config.click_duration_s)
    tracker["armed"] = False
    tracker["last_click"] = now
    tracker["done_until"] = now + BOBBER_TO_MINIGAME_DELAY_S
    tracker["complete"] = True
    message = f"{message_prefix}; reel clicked {BOBBER_REEL_CLICK_COUNT}x at " + " ".join(clicked_points)
    if verbose:
        print(message)
    return message


def bobber_reel_fallback(frame_bgr: np.ndarray) -> dict:
    return {
        "x": 0,
        "y": 0,
        "w": 0,
        "h": 0,
        "cx": int(frame_bgr.shape[1] * 0.25),
        "cy": int(frame_bgr.shape[0] * 0.90),
    }


def handle_bobber_reel(frame_bgr: np.ndarray, tracker: dict, clicker: str, config: Config, screen_left: int, screen_top: int, verbose: bool) -> str:
    now = time.monotonic()
    red_bobber = detect_red_bobber(frame_bgr)
    prompt_text = detect_bobber_prompt_text(frame_bgr)
    reel_button = find_bobber_reel_button(frame_bgr)
    if reel_button:
        tracker["last_reel_button"] = reel_button

    if tracker.get("complete"):
        if not prompt_text:
            tracker["complete"] = False
            return "bobber reset"
        return "bobber complete"

    if red_bobber:
        tracker["armed"] = True
        tracker["last_seen"] = now
        return "bobber visible"

    if now - tracker.get("last_click", 0.0) < BOBBER_REEL_COOLDOWN_S:
        return "bobber cooldown"

    if tracker.get("armed") and now - tracker.get("last_seen", 0.0) < 0.12:
        return "bobber disappearing"

    if tracker.get("armed"):
        reel_button = reel_button or tracker.get("last_reel_button") or bobber_reel_fallback(frame_bgr)
        return click_bobber_reel(reel_button, tracker, clicker, config, screen_left, screen_top, "bobber gone", verbose)

    if prompt_text:
        return "bobber waiting for red"
    return "bobber waiting"


def get_monitor(config: Config):
    if config.region_width <= 0 or config.region_height <= 0:
        raise SystemExit("No capture region set. Run: python fish_assist.py --calibrate")
    return {
        "left": config.region_left,
        "top": config.region_top,
        "width": config.region_width,
        "height": config.region_height,
    }


def get_bobber_monitor(config: Config, sct: mss.MSS):
    if config.bobber_width > 0 and config.bobber_height > 0:
        return {
            "left": config.bobber_left,
            "top": config.bobber_top,
            "width": config.bobber_width,
            "height": config.bobber_height,
        }
    return sct.monitors[0]


def send_input_click(duration_s: float) -> None:
    extra = ctypes.c_ulong(0)
    down = Input(
        type=INPUT_MOUSE,
        ii=InputUnion(mi=MouseInput(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, ctypes.pointer(extra))),
    )
    up = Input(
        type=INPUT_MOUSE,
        ii=InputUnion(mi=MouseInput(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, ctypes.pointer(extra))),
    )
    ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(down))
    time.sleep(duration_s)
    ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(up))


def click_point(clicker: str, x: int, y: int, duration_s: float) -> None:
    if clicker == "directinput":
        if pydirectinput is None:
            raise SystemExit("pydirectinput is not installed. Run: python -m pip install -r requirements.txt")
        pydirectinput.click(x=x, y=y)
    elif clicker == "sendinput":
        ctypes.windll.user32.SetCursorPos(x, y)
        send_input_click(duration_s)
    elif clicker == "win32":
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(duration_s)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    else:
        pyautogui.moveTo(x, y)
        pyautogui.mouseDown()
        time.sleep(duration_s)
        pyautogui.mouseUp()


def click_current_position(clicker: str, duration_s: float) -> None:
    if clicker == "directinput":
        if pydirectinput is None:
            raise SystemExit("pydirectinput is not installed. Run: python -m pip install -r requirements.txt")
        pydirectinput.click()
    elif clicker == "sendinput":
        send_input_click(duration_s)
    elif clicker == "win32":
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(duration_s)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    else:
        pyautogui.mouseDown()
        time.sleep(duration_s)
        pyautogui.mouseUp()


def click_mouse(clicker: str, config: Config) -> None:
    click_current_position(clicker, config.click_duration_s)


def is_mouse4_pressed() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_XBUTTON1) & 0x8000)


def click_spread(center_x: int, center_y: int, duration_s: float) -> list[str]:
    offsets = [0, 12, -12, 24, -24]
    clicked_points = []
    for offset in offsets:
        x = center_x + offset
        force_screen_click(x, center_y, duration_s)
        clicked_points.append(f"{x},{center_y}")
        time.sleep(0.12)
    return clicked_points


def click_cast(config: Config) -> str:
    if not config.cast_x or not config.cast_y:
        return "Cast click point is not calibrated. Use Calibrate -> Start Cast."
    points = click_spread(config.cast_x, config.cast_y, config.click_duration_s)
    return "cast clicked 5x at " + " ".join(points)


class FishAssistGui:
    def __init__(self) -> None:
        self.config = load_config()
        self.root = tk.Tk()
        self.root.title("Fishing Assist")
        self.root.configure(bg="white")
        self.root.resizable(False, False)

        self.status_queue = queue.Queue()
        self.bot_thread = None
        self.stop_event = None
        self.phase = "cast"
        self.enabled = False
        self.mode = MODE_CATCH

        self._build()
        self.root.bind_all("<z>", lambda event: self.set_mode(MODE_CATCH))
        self.root.bind_all("<x>", lambda event: self.set_mode(MODE_BOBBER))
        self.root.bind_all("<c>", lambda event: self.set_mode(MODE_FISHER))
        try:
            keyboard.add_hotkey("z", lambda: self.root.after(0, self.set_mode, MODE_CATCH))
            keyboard.add_hotkey("x", lambda: self.root.after(0, self.set_mode, MODE_BOBBER))
            keyboard.add_hotkey("c", lambda: self.root.after(0, self.set_mode, MODE_FISHER))
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll_status)

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)

        title = tk.Label(self.root, text="Fishing Assist", bg="white", fg="#111111", font=("Segoe UI", 18, "bold"))
        title.grid(row=0, column=0, padx=18, pady=(16, 6), sticky="w")

        phases = tk.Frame(self.root, bg="white")
        phases.grid(row=1, column=0, padx=18, pady=8, sticky="ew")

        self.cast_phase = self._phase_label(phases, "Cast")
        self.cast_phase.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        arrow = tk.Label(phases, text="->", bg="white", fg="#555555", font=("Segoe UI", 14, "bold"))
        arrow.grid(row=0, column=1, padx=4)
        self.bobber_phase = self._phase_label(phases, "Bobber")
        self.bobber_phase.grid(row=0, column=2, padx=8, sticky="ew")
        arrow2 = tk.Label(phases, text="->", bg="white", fg="#555555", font=("Segoe UI", 14, "bold"))
        arrow2.grid(row=0, column=3, padx=4)
        self.minigame_phase = self._phase_label(phases, "Minigame")
        self.minigame_phase.grid(row=0, column=4, padx=(8, 0), sticky="ew")

        controls = tk.Frame(self.root, bg="white")
        controls.grid(row=2, column=0, padx=18, pady=10, sticky="ew")

        self.start_button = tk.Button(controls, text="Start", command=self.start_bot, width=12, bg="#111111", fg="white", relief="flat", padx=8, pady=7)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = tk.Button(controls, text="Stop", command=self.stop_bot, width=12, bg="#eeeeee", fg="#111111", relief="flat", padx=8, pady=7, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=8)
        calibrate_button = tk.Button(controls, text="Calibrate", command=self.open_calibrate_menu, width=12, bg="#eeeeee", fg="#111111", relief="flat", padx=8, pady=7)
        calibrate_button.grid(row=0, column=2, padx=(8, 0))

        modes = tk.Frame(self.root, bg="white")
        modes.grid(row=3, column=0, padx=18, pady=(0, 10), sticky="ew")
        self.catch_button = tk.Button(modes, text="Catch (Z)", command=lambda: self.set_mode(MODE_CATCH), width=12, bg="#111111", fg="white", relief="flat", padx=8, pady=6)
        self.catch_button.grid(row=0, column=0, padx=(0, 8))
        self.bobber_button = tk.Button(modes, text="Bobber (X)", command=lambda: self.set_mode(MODE_BOBBER), width=12, bg="#eeeeee", fg="#111111", relief="flat", padx=8, pady=6)
        self.bobber_button.grid(row=0, column=1, padx=8)
        self.fisher_button = tk.Button(modes, text="Fisher (C)", command=lambda: self.set_mode(MODE_FISHER), width=12, bg="#eeeeee", fg="#111111", relief="flat", padx=8, pady=6)
        self.fisher_button.grid(row=0, column=2, padx=(8, 0))

        self.status_label = tk.Label(self.root, text="Paused. Press Start, then Mouse4 to enable.", bg="white", fg="#333333", font=("Segoe UI", 10), anchor="w")
        self.status_label.grid(row=4, column=0, padx=18, pady=(6, 2), sticky="ew")

        self.region_label = tk.Label(self.root, text=self.region_text(), bg="white", fg="#666666", font=("Segoe UI", 9), justify="left", anchor="w")
        self.region_label.grid(row=5, column=0, padx=18, pady=(0, 16), sticky="ew")

        self.set_phase("cast", False, "Paused. Press Start, then Mouse4 to enable.")

    def _phase_label(self, parent, text: str):
        return tk.Label(parent, text=text, bg="#e5e5e5", fg="#111111", font=("Segoe UI", 13, "bold"), width=14, padx=12, pady=12)

    def region_text(self) -> str:
        return (
            f"Fishing: {self.config.region_left},{self.config.region_top} "
            f"{self.config.region_width}x{self.config.region_height}\n"
            f"Bobber: {self.config.bobber_left},{self.config.bobber_top} "
            f"{self.config.bobber_width}x{self.config.bobber_height}\n"
            f"Cast: {self.config.cast_x},{self.config.cast_y}"
        )

    def set_phase(self, phase: str, enabled: bool, message: str) -> None:
        active = "#24a148"
        inactive = "#e5e5e5"
        self.cast_phase.configure(bg=active if enabled and phase == "cast" else inactive, fg="white" if enabled and phase == "cast" else "#111111")
        self.bobber_phase.configure(bg=active if enabled and phase == "bobber" else inactive, fg="white" if enabled and phase == "bobber" else "#111111")
        self.minigame_phase.configure(bg=active if enabled and phase == "minigame" else inactive, fg="white" if enabled and phase == "minigame" else "#111111")
        state = "Enabled" if enabled else "Paused"
        self.status_label.configure(text=f"{state}: {message}")

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        active = "#111111"
        inactive = "#eeeeee"
        for button, button_mode in (
            (self.catch_button, MODE_CATCH),
            (self.bobber_button, MODE_BOBBER),
            (self.fisher_button, MODE_FISHER),
        ):
            selected = mode == button_mode
            button.configure(bg=active if selected else inactive, fg="white" if selected else "#111111")
        self.status_queue.put({"phase": self.phase, "enabled": self.enabled, "message": f"Mode: {mode.title()}", "mode": mode})

    def start_bot(self) -> None:
        if self.bot_thread and self.bot_thread.is_alive():
            return

        self.config = load_config()
        self.stop_event = threading.Event()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_label.configure(text="Starting previews. Press Mouse4 to enable clicking.")

        def status_callback(status: dict) -> None:
            self.status_queue.put(status)

        def mode_getter() -> str:
            return self.mode

        def target() -> None:
            try:
                run_bot(self.config, True, "sendinput", False, True, self.stop_event, status_callback, mode_getter)
            except Exception as error:
                self.status_queue.put({"phase": self.phase, "enabled": False, "message": f"Stopped: {error}"})
            finally:
                self.status_queue.put({"phase": self.phase, "enabled": False, "message": "Bot stopped", "stopped": True})

        self.bot_thread = threading.Thread(target=target, daemon=True)
        self.bot_thread.start()

    def stop_bot(self) -> None:
        if self.stop_event:
            self.stop_event.set()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.set_phase(self.phase, False, "Stopping...")

    def poll_status(self) -> None:
        latest = None
        while True:
            try:
                latest = self.status_queue.get_nowait()
            except queue.Empty:
                break

        if latest:
            self.phase = latest.get("phase", self.phase)
            self.enabled = latest.get("enabled", False)
            self.set_phase(self.phase, self.enabled, latest.get("message", ""))
            if latest.get("refresh_config"):
                self.config = load_config()
                self.region_label.configure(text=self.region_text())
            if latest.get("stopped"):
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")

        self.root.after(100, self.poll_status)

    def open_calibrate_menu(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Calibrate")
        window.configure(bg="white")
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()

        label = tk.Label(window, text="Choose what to calibrate", bg="white", fg="#111111", font=("Segoe UI", 12, "bold"))
        label.grid(row=0, column=0, columnspan=3, padx=18, pady=(16, 10))

        fishing = tk.Button(window, text="Fishing", command=lambda: self.start_calibration(window, "fishing"), width=14, bg="#eeeeee", relief="flat", pady=8)
        fishing.grid(row=1, column=0, padx=(18, 8), pady=(0, 16))
        bobber = tk.Button(window, text="Bobber", command=lambda: self.start_calibration(window, "bobber"), width=14, bg="#eeeeee", relief="flat", pady=8)
        bobber.grid(row=1, column=1, padx=8, pady=(0, 16))
        cast = tk.Button(window, text="Start Cast", command=lambda: self.start_calibration(window, "cast"), width=14, bg="#eeeeee", relief="flat", pady=8)
        cast.grid(row=1, column=2, padx=(8, 18), pady=(0, 16))

    def start_calibration(self, window, mode: str) -> None:
        window.destroy()
        self.stop_bot()
        if mode == "cast":
            message = "Hover the Cast/Start button where it is clickable, then left-click. F8 also works."
            status = "Calibrating Start Cast. Click the Cast button point."
        else:
            message = "Move to the top-left corner and left-click, then move to the bottom-right corner and left-click. F8 also works."
            status = f"Calibrating {mode}. Left-click both corners."
        messagebox.showinfo("Calibration", message)
        self.status_label.configure(text=status)

        def target() -> None:
            try:
                config = load_config()
                if mode == "bobber":
                    self.config = select_bobber_region(config)
                elif mode == "cast":
                    self.config = select_cast_point(config)
                else:
                    self.config = select_region(config)
                self.status_queue.put({
                    "phase": mode if mode in ("bobber", "cast") else "minigame",
                    "enabled": False,
                    "message": f"{mode.title()} calibration saved",
                    "refresh_config": True,
                })
            except Exception as error:
                self.status_queue.put({"phase": self.phase, "enabled": False, "message": f"Calibration failed: {error}"})

        threading.Thread(target=target, daemon=True).start()

    def close(self) -> None:
        self.stop_bot()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_gui() -> None:
    FishAssistGui().run()


def run_bot(config: Config, preview: bool, clicker: str, verbose: bool, bobber: bool, stop_event=None, status_callback=None, mode_getter=None) -> None:
    monitor = get_monitor(config)
    last_click = 0.0
    last_log = 0.0
    last_bobber_check = 0.0
    bobber_tracker = {"armed": False, "last_seen": 0.0, "last_click": 0.0, "done_until": 0.0, "complete": False}
    bobber_message = "bobber waiting" if bobber else "bobber off"
    bobber_preview_frame = None
    active_phase = "cast" if bobber else "minigame"
    last_minigame_seen = 0.0
    cast_clicked = False
    cast_next_at = 0.0
    current_mode = MODE_CATCH if bobber else MODE_FISHER

    print("Press Mouse4 to start/pause. Press F5 or ESC to quit.")
    enabled = False
    mouse4_was_pressed = False

    def publish(phase: str, running: bool, message: str) -> None:
        if status_callback:
            status_callback({"phase": phase, "enabled": running, "message": message})

    publish("cast" if bobber else "minigame", enabled, "paused")

    def reset_for_mode(mode: str, now: float) -> None:
        nonlocal active_phase, bobber_tracker, bobber_message, cast_clicked, cast_next_at, last_minigame_seen
        bobber_tracker = {"armed": False, "last_seen": 0.0, "last_click": 0.0, "done_until": 0.0, "complete": False}
        bobber_message = "bobber waiting"
        cast_clicked = False
        cast_next_at = now
        last_minigame_seen = 0.0
        if mode == MODE_CATCH:
            active_phase = "cast"
        elif mode == MODE_BOBBER:
            active_phase = "bobber"
        else:
            active_phase = "minigame"

    with mss.MSS() as sct:
        while not keyboard.is_pressed("esc") and not keyboard.is_pressed("f5"):
            if stop_event is not None and stop_event.is_set():
                break

            mouse4_pressed = is_mouse4_pressed()
            if mouse4_pressed and not mouse4_was_pressed:
                enabled = not enabled
                print("enabled - press Mouse4 again to pause" if enabled else "paused")
                publish(active_phase, enabled, "enabled" if enabled else "paused")
                time.sleep(0.35)
            mouse4_was_pressed = mouse4_pressed

            now = time.monotonic()
            requested_mode = mode_getter() if mode_getter else (MODE_CATCH if bobber else MODE_FISHER)
            if requested_mode != current_mode:
                current_mode = requested_mode
                reset_for_mode(current_mode, now)
                publish(active_phase, enabled, f"Mode: {current_mode.title()}")

            state = None
            frame = None

            if current_mode == MODE_CATCH and active_phase == "cast":
                if enabled and not cast_clicked and now >= cast_next_at:
                    if config.cast_x and config.cast_y:
                        bobber_message = click_cast(config)
                        cast_clicked = True
                        active_phase = "bobber"
                        bobber_tracker = {"armed": False, "last_seen": 0.0, "last_click": 0.0, "done_until": 0.0, "complete": False}
                        bobber_message = "bobber waiting"
                    else:
                        bobber_message = "calibrate Start Cast"

            if current_mode in (MODE_CATCH, MODE_BOBBER) and active_phase == "bobber" and (enabled or preview) and now - last_bobber_check >= 0.08:
                bobber_monitor = get_bobber_monitor(config, sct)
                screen_shot = sct.grab(bobber_monitor)
                screen_frame = cv2.cvtColor(np.array(screen_shot), cv2.COLOR_BGRA2BGR)
                bobber_preview_frame = screen_frame
                if enabled:
                    bobber_message = handle_bobber_reel(
                        screen_frame,
                        bobber_tracker,
                        clicker,
                        config,
                        bobber_monitor["left"],
                        bobber_monitor["top"],
                        verbose,
                    )
                else:
                    red_bobber = detect_red_bobber(screen_frame)
                    reel_button = find_bobber_reel_button(screen_frame)
                    prompt_text = detect_bobber_prompt_text(screen_frame)
                    if red_bobber:
                        bobber_message = "bobber visible"
                    elif prompt_text and reel_button:
                        bobber_message = "bobber prompt; reel found"
                    elif prompt_text:
                        bobber_message = "bobber prompt"
                    elif reel_button:
                        bobber_message = "bobber not visible; reel found"
                    else:
                        bobber_message = "bobber waiting"
                last_bobber_check = now

                if bobber_tracker.get("complete"):
                    if current_mode == MODE_CATCH:
                        active_phase = "minigame"
                        last_minigame_seen = time.monotonic()
                    else:
                        bobber_tracker = {"armed": False, "last_seen": 0.0, "last_click": 0.0, "done_until": 0.0, "complete": False}
                        bobber_message = "bobber waiting"

            if active_phase == "minigame":
                shot = sct.grab(monitor)
                frame = cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
                if current_mode == MODE_CATCH and detect_caught(frame):
                    active_phase = "cast"
                    cast_clicked = False
                    cast_next_at = now + POST_CATCH_DELAY_S
                    bobber_tracker = {"armed": False, "last_seen": 0.0, "last_click": 0.0, "done_until": 0.0, "complete": False}
                    bobber_message = "caught; waiting 5s"
                    state = None
                    last_minigame_seen = 0.0
                else:
                    state = detect_state(frame, config)
                    if state:
                        last_minigame_seen = now
                    elif current_mode == MODE_CATCH and enabled and last_minigame_seen and now - last_minigame_seen > 2.0:
                        active_phase = "cast"
                        cast_clicked = False
                        cast_next_at = now + POST_CATCH_DELAY_S
                        bobber_tracker = {"armed": False, "last_seen": 0.0, "last_click": 0.0, "done_until": 0.0, "complete": False}
                        bobber_message = "minigame gone; waiting 5s"

            if verbose and now - last_log >= 0.25:
                minigame_message = "minigame paused" if active_phase in ("cast", "bobber") else state["action"] if state else "not detected"
                print(f"{minigame_message}; {bobber_message}" if bobber else minigame_message)
                last_log = now

            if active_phase == "cast":
                phase_message = "clicking Cast" if cast_clicked else "waiting to click Cast"
            elif active_phase == "bobber":
                phase_message = bobber_message
            else:
                phase_message = f"Fishing detected: {state['action']}" if state else "minigame scanning"
            publish(active_phase, enabled, phase_message)

            if enabled and active_phase == "minigame" and state and state["action"] == "click" and now - last_click >= config.click_interval_s:
                click_mouse(clicker, config)
                last_click = now

            if preview:
                if frame is not None and active_phase == "minigame":
                    cv2.imshow("fish assist preview", draw_preview(frame, state, enabled))
                if bobber and bobber_preview_frame is not None:
                    cv2.imshow("bobber preview", draw_bobber_preview(bobber_preview_frame, enabled, bobber_message))
                if cv2.waitKey(1) & 0xFF == 27:
                    break

            time.sleep(config.loop_sleep_s)

    cv2.destroyAllWindows()


def main() -> None:
    parser = argparse.ArgumentParser(description="Screen-based fishing assist for cyan-bar fishing minigames.")
    parser.add_argument("--gui", action="store_true", help="Open the simple control panel.")
    parser.add_argument("--calibrate", action="store_true", help="Set the screen region to watch.")
    parser.add_argument("--calibrate-bobber", action="store_true", help="Set the bobber/Reel prompt region to watch.")
    parser.add_argument("--calibrate-cast", action="store_true", help="Set the Cast/Start click point.")
    parser.add_argument("--preview", action="store_true", help="Show detected fish/bar boxes.")
    parser.add_argument("--deadzone", type=int, help="Pixels of tolerance around bar edges/center.")
    parser.add_argument("--clicker", choices=("pyautogui", "directinput", "win32", "sendinput"), default="sendinput", help="Mouse click backend.")
    parser.add_argument("--click-interval", type=float, help="Seconds between repeated clicks.")
    parser.add_argument("--verbose", action="store_true", help="Print detected action while running.")
    parser.add_argument("--test-click", action="store_true", help="Click once immediately, then exit.")
    parser.add_argument("--test-click-delay", type=float, default=3.0, help="Seconds to wait before --test-click fires.")
    parser.add_argument("--click-duration", type=float, help="Seconds to hold each click down.")
    parser.add_argument("--click-x", type=int, help="Screen X coordinate to click.")
    parser.add_argument("--click-y", type=int, help="Screen Y coordinate to click.")
    parser.add_argument("--bobber", action="store_true", help="Also watch for the red bobber and click Reel after it disappears.")
    parser.add_argument("--fast", action="store_true", help="Shortcut for the common preview/sendinput/0.08s settings.")
    args = parser.parse_args()

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    if args.gui:
        run_gui()
        return

    if args.fast:
        args.preview = True
        args.verbose = True
        args.clicker = "sendinput"
        args.click_duration = args.click_duration if args.click_duration is not None else 0.08
        args.click_interval = args.click_interval if args.click_interval is not None else 0.08

    if args.clicker == "directinput" and pydirectinput is not None:
        pydirectinput.PAUSE = 0

    config = load_config()
    if args.deadzone is not None:
        config.deadzone_px = args.deadzone
        save_config(config)
    if args.click_duration is not None:
        config.click_duration_s = args.click_duration
        save_config(config)
    if args.click_interval is not None:
        config.click_interval_s = args.click_interval
        save_config(config)
    if args.click_x is not None:
        config.click_x = args.click_x
        save_config(config)
    if args.click_y is not None:
        config.click_y = args.click_y
        save_config(config)

    if args.calibrate:
        config = select_region(config)
        return
    if args.calibrate_bobber:
        config = select_bobber_region(config)
        return
    if args.calibrate_cast:
        config = select_cast_point(config)
        return

    if args.test_click:
        print(f"Test click in {args.test_click_delay:.1f}s. Focus Roblox now.")
        time.sleep(args.test_click_delay)
        click_mouse(args.clicker, config)
        return

    run_bot(config, args.preview, args.clicker, args.verbose, args.bobber)


if __name__ == "__main__":
    pyautogui.PAUSE = 0
    main()
