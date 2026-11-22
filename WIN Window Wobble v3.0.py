"""
Window Wobble - "wobbly windows" voor Windows 11 (zoals Compiz op Linux)

Sleep een willekeurig venster: het buigt en zwabbert als gelei en wiebelt na.
Het programma draait op de achtergrond met een icoontje bij de klok.

Installeren (eenmalig):   pip install numpy opencv-python pystray pillow
Starten:                  python window_wobble.py
                          (het programma maakt zichzelf los van het cmd-venster,
                           dat mag je daarna gewoon sluiten)

Afsluiten: rechtsklik op het icoontje bij de klok -> Afsluiten.
Windows 11 verstopt nieuwe icoontjes soms achter het pijltje ^ naast de klok.

Hoe het werkt (kort):
  Windows laat niet toe dat je een echt venster vervormt. Daarom maakt het
  programma tijdens het slepen een foto van het venster, maakt het echte
  venster tijdelijk onzichtbaar en tekent die foto vervormd in een
  doorzichtige overlay. Zodra het wiebelen uitgewiebeld is, komt het echte
  venster weer tevoorschijn. Tijdens het slepen is de inhoud dus even een
  foto (een video in het venster loopt dan niet door).
"""

import atexit
import importlib.util
import json
import math
import os
import queue
import subprocess
import sys
import time
import traceback

# ----------------------------------------------------------------------------
# 1. Controleren of alle packages er zijn (nog in het zichtbare cmd-venster)
# ----------------------------------------------------------------------------
_missing = [name for name, mod in (("numpy", "numpy"), ("opencv-python", "cv2"),
                                   ("pystray", "pystray"), ("pillow", "PIL"))
            if importlib.util.find_spec(mod) is None]
if _missing:
    print("Installeer eerst de benodigde packages:\n")
    print("    pip install numpy opencv-python pystray pillow\n")
    print("Ontbrekend:", ", ".join(_missing))
    input("\nDruk op Enter om af te sluiten...")
    raise SystemExit(1)

if sys.platform != "win32":
    raise SystemExit("Dit programma werkt alleen op Windows.")

# ----------------------------------------------------------------------------
# 2. Losmaken van het cmd-venster: opnieuw starten met pythonw (geen venster)
# ----------------------------------------------------------------------------
if (os.path.basename(sys.executable).lower() != "pythonw.exe"
        and "--child" not in sys.argv):
    _pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.exists(_pyw):
        subprocess.Popen(
            [_pyw, os.path.abspath(__file__), "--child"],
            creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            close_fds=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("Window Wobble draait nu op de achtergrond (icoontje bij de klok).")
        print("Je kunt dit venster sluiten.")
        time.sleep(1.5)
        raise SystemExit(0)

# ----------------------------------------------------------------------------
# 3. Vanaf hier draait alles in het achtergrondproces
# ----------------------------------------------------------------------------
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
import pystray
from PIL import Image, ImageDraw

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "WindowWobble")
os.makedirs(APP_DIR, exist_ok=True)

# Zonder cmd-venster bestaat er geen scherm voor foutmeldingen: log naar een bestand
if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(APP_DIR, "log.txt"), "a", buffering=1, encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log


# ============================================================================
#  VEERFYSICA EN VERVORMEN VAN HET BEELD
# ============================================================================

class WobbleSim:
    """
    Een raster van punten dat als gelei aan het venster hangt.
    Punten dicht bij de plek waar je het venster vastpakt volgen strak,
    punten verder weg lopen achter en veren door. Dat geeft het buigen.
    """

    SUBSTEP = 1.0 / 240.0
    COARSE = 4  # de vervormingskaart wordt op 1/4 resolutie berekend

    def __init__(self, w, h, margin, grab_local, percent):
        self.m = margin
        self.W = w + 2 * margin
        self.H = h + 2 * margin

        self.nx = int(min(12, max(4, round(self.W / 90))))
        self.ny = int(min(12, max(4, round(self.H / 90))))

        # Rustposities van de rasterpunten (midden van elke cel), canvas-lokaal
        xs = (np.arange(self.nx) + 0.5) * self.W / self.nx
        ys = (np.arange(self.ny) + 0.5) * self.H / self.ny
        gx, gy = np.meshgrid(xs, ys)

        # Sterkere veer bij het aangrijppunt (de cursor)
        d2 = (gx - grab_local[0]) ** 2 + (gy - grab_local[1]) ** 2
        sigma = 0.3 * max(w, h) + 30.0
        self.weight = (1.0 + 5.0 * np.exp(-d2 / (2.0 * sigma ** 2)))[..., None]

        self.Q = np.zeros((self.ny, self.nx, 2))  # verplaatsing van de punten
        self.V = np.zeros((self.ny, self.nx, 2))  # snelheid van de punten
        self.limit = max(20.0, margin - 6.0)
        self.set_percent(percent)

        # Voorberekend coördinatenraster voor de grove kaart
        self.cw = int(math.ceil(self.W / self.COARSE))
        self.ch = int(math.ceil(self.H / self.COARSE))
        self.fx = self.W / self.cw
        self.fy = self.H / self.ch
        px = (np.arange(self.cw) + 0.5) * self.fx - 0.5
        py = (np.arange(self.ch) + 0.5) * self.fy - 0.5
        PX, PY = np.meshgrid(px, py)
        self.P0 = np.dstack([PX, PY]).astype(np.float32)

    def set_percent(self, percent):
        t = max(0.0, min(1.0, percent / 100.0))
        self.k = 2500.0 * (1.0 - t) ** 2 + 50.0  # veerstijfheid (lager = slapper)
        zeta = 0.75 - 0.60 * t                   # demping (lager = meer nawiebelen)
        self.c = 2.0 * zeta * math.sqrt(self.k)
        self.kn = 0.6 * self.k                   # koppeling tussen buurpunten

    def step(self, dt, delta):
        """delta = hoeveel het echte venster is verschoven sinds het begin."""
        n = max(1, int(math.ceil(dt / self.SUBSTEP)))
        h = dt / n
        for _ in range(n):
            D = self.Q - delta
            Dp = np.pad(D, ((1, 1), (1, 1), (0, 0)), mode="edge")
            lap = (Dp[:-2, 1:-1] + Dp[2:, 1:-1] + Dp[1:-1, :-2] + Dp[1:-1, 2:] - 4.0 * D)
            a = -self.k * self.weight * D - self.c * self.V + self.kn * lap
            self.V += a * h
            self.Q += self.V * h
        # Niet verder laten uitslaan dan de marge van de overlay
        D = np.clip(self.Q - delta, -self.limit, self.limit)
        self.Q = D + delta

    def settled(self, delta):
        D = self.Q - delta
        return float(np.abs(D).max()) < 0.5 and float(np.abs(self.V).max()) < 3.0

    def render(self, src, delta):
        """src: BGRA-beeld van het canvas. Geeft het vervormde BGRA-beeld terug."""
        D = (self.Q - delta).astype(np.float32)
        Dc = cv2.resize(D, (self.cw, self.ch), interpolation=cv2.INTER_CUBIC)

        # Omgekeerde kaart: voor elke doelpixel p zoeken we de bronpixel s
        # met s + D(s) = p. Twee vaste-punt-iteraties op grove resolutie.
        S = self.P0
        for _ in range(2):
            u = ((S[..., 0] + 0.5) / self.fx - 0.5).astype(np.float32)
            v = ((S[..., 1] + 0.5) / self.fy - 0.5).astype(np.float32)
            Ds = cv2.remap(Dc, u, v, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            S = self.P0 - Ds

        Sfull = cv2.resize(S, (self.W, self.H), interpolation=cv2.INTER_LINEAR)
        return cv2.remap(src, Sfull, None, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))


# ============================================================================
#  WINDOWS API
# ============================================================================

import ctypes
from ctypes import wintypes

# Pixelcoördinaten kloppen ook op schermen met schaling (125%, 150%, ...)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- constanten ---
VK_LBUTTON = 0x01
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_POPUP = 0x80000000
LWA_ALPHA = 0x02
ULW_ALPHA = 0x02
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
SW_SHOWNOACTIVATE = 4
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
DWMWA_EXTENDED_FRAME_BOUNDS = 9
RDW_INVALIDATE = 0x0001
RDW_ALLCHILDREN = 0x0080
RDW_FRAME = 0x0400
ERROR_ALREADY_EXISTS = 183

# Vensters die we nooit laten wobbelen (bureaublad, taakbalk, menu's, ...)
IGNORED_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "#32768", "tooltips_class32", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "TaskListThumbnailWnd",
}
# Voor deze vensters werkt "doorzichtig maken" niet; daar verbergen we ze via een lege regio
REGION_HIDE_CLASSES = {
    "ApplicationFrameWindow", "Windows.UI.Core.CoreWindow", "WinUIDesktopWin32WindowClass",
}

MAX_CANVAS_PIXELS = 6_000_000
MARGIN = 140


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte),
    ]


def _proto(func, restype, argtypes):
    func.restype = restype
    func.argtypes = argtypes


# user32
_proto(user32.GetDC, wintypes.HDC, [wintypes.HWND])
_proto(user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, wintypes.HDC])
_proto(user32.GetAsyncKeyState, ctypes.c_short, [ctypes.c_int])
_proto(user32.GetCursorPos, wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)])
_proto(user32.WindowFromPoint, wintypes.HWND, [wintypes.POINT])
_proto(user32.GetAncestor, wintypes.HWND, [wintypes.HWND, wintypes.UINT])
_proto(user32.GetWindowRect, wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)])
_proto(user32.IsWindow, wintypes.BOOL, [wintypes.HWND])
_proto(user32.IsWindowVisible, wintypes.BOOL, [wintypes.HWND])
_proto(user32.IsIconic, wintypes.BOOL, [wintypes.HWND])
_proto(user32.IsZoomed, wintypes.BOOL, [wintypes.HWND])
_proto(user32.GetWindowThreadProcessId, wintypes.DWORD, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)])
_proto(user32.GetClassNameW, ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int])
_GetWindowLongPtr = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
_SetWindowLongPtr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
_proto(_GetWindowLongPtr, ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int])
_proto(_SetWindowLongPtr, ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t])
_proto(user32.SetLayeredWindowAttributes, wintypes.BOOL,
       [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD])
_proto(user32.GetLayeredWindowAttributes, wintypes.BOOL,
       [wintypes.HWND, ctypes.POINTER(wintypes.COLORREF),
        ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD)])
_proto(user32.UpdateLayeredWindow, wintypes.BOOL,
       [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD])
_proto(user32.CreateWindowExW, wintypes.HWND,
       [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID])
_proto(user32.DestroyWindow, wintypes.BOOL, [wintypes.HWND])
_proto(user32.ShowWindow, wintypes.BOOL, [wintypes.HWND, ctypes.c_int])
_proto(user32.RedrawWindow, wintypes.BOOL,
       [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT])
_proto(user32.GetWindowRgn, ctypes.c_int, [wintypes.HWND, wintypes.HRGN])
_proto(user32.SetWindowRgn, ctypes.c_int, [wintypes.HWND, wintypes.HRGN, wintypes.BOOL])
_proto(user32.MessageBoxW, ctypes.c_int,
       [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT])

# gdi32
_proto(gdi32.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC])
_proto(gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int])
_proto(gdi32.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ])
_proto(gdi32.BitBlt, wintypes.BOOL,
       [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD])
_proto(gdi32.GetDIBits, ctypes.c_int,
       [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT])
_proto(gdi32.DeleteObject, wintypes.BOOL, [wintypes.HGDIOBJ])
_proto(gdi32.DeleteDC, wintypes.BOOL, [wintypes.HDC])
_proto(gdi32.CreateDIBSection, wintypes.HBITMAP,
       [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD])
_proto(gdi32.CreateRectRgn, wintypes.HRGN,
       [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int])

# dwmapi
_proto(dwmapi.DwmGetWindowAttribute, ctypes.c_long,
       [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD])

# kernel32
_proto(kernel32.CreateMutexW, wintypes.HANDLE,
       [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR])


def _bmi(width, height_topdown):
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height_topdown  # negatief = bovenaan beginnen
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    return bmi


def grab_screen(left, top, w, h):
    """Maakt een screenshot van een gebied. Geeft een BGRA numpy-array."""
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbm = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, hbm)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, left, top, SRCCOPY | CAPTUREBLT)
    gdi32.SelectObject(hdc_mem, old)  # bitmap mag niet geselecteerd zijn voor GetDIBits
    buf = (ctypes.c_ubyte * (w * h * 4))()
    bmi = _bmi(w, h)
    gdi32.GetDIBits(hdc_screen, hbm, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()
    arr[..., 3] = 255
    return arr


def window_rect(hwnd):
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def frame_bounds(hwnd):
    """Zichtbare rand van het venster (zonder de onzichtbare schaduwrand)."""
    r = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r))
    if hr == 0:
        return r.left, r.top, r.right, r.bottom
    return window_rect(hwnd)


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


# ---------- het echte venster tijdelijk onzichtbaar maken ----------

def _hide_alpha(hwnd):
    ex = _GetWindowLongPtr(hwnd, GWL_EXSTYLE)
    was_layered = bool(ex & WS_EX_LAYERED)
    old_key, old_alpha, old_flags = 0, 255, LWA_ALPHA
    if was_layered:
        key = wintypes.COLORREF(0)
        alpha = ctypes.c_ubyte(255)
        flags = wintypes.DWORD(0)
        if not user32.GetLayeredWindowAttributes(
                hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)):
            return None
        old_key, old_alpha, old_flags = key.value, alpha.value, flags.value
    else:
        _SetWindowLongPtr(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
    if not user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA):
        if not was_layered:
            _SetWindowLongPtr(hwnd, GWL_EXSTYLE, ex)
        return None
    return ("alpha", was_layered, old_key, old_alpha, old_flags)


def _hide_region(hwnd):
    """Verbergt een venster door er een lege 'regio' aan te geven."""
    old = gdi32.CreateRectRgn(0, 0, 0, 0)
    had_region = user32.GetWindowRgn(hwnd, old) != 0
    empty = gdi32.CreateRectRgn(0, 0, 0, 0)
    if not user32.SetWindowRgn(hwnd, empty, True):
        gdi32.DeleteObject(empty)
        gdi32.DeleteObject(old)
        return None
    if not had_region:
        gdi32.DeleteObject(old)
        old = None
    return ("region", old)


def hide_window(hwnd):
    """Geeft info terug om het venster later te herstellen, of None als het niet lukt."""
    if class_name(hwnd) not in REGION_HIDE_CLASSES:
        info = _hide_alpha(hwnd)
        if info:
            return info
    return _hide_region(hwnd)


def show_window(hwnd, info):
    if not hwnd or not user32.IsWindow(hwnd):
        if info[0] == "region" and info[1]:
            gdi32.DeleteObject(info[1])
        return
    if info[0] == "alpha":
        _, was_layered, old_key, old_alpha, old_flags = info
        if was_layered:
            user32.SetLayeredWindowAttributes(hwnd, old_key, old_alpha, old_flags)
        else:
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            ex = _GetWindowLongPtr(hwnd, GWL_EXSTYLE)
            _SetWindowLongPtr(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
    else:
        user32.SetWindowRgn(hwnd, info[1], True)  # None = geen regio (zoals voorheen)
    user32.RedrawWindow(hwnd, None, None, RDW_INVALIDATE | RDW_FRAME | RDW_ALLCHILDREN)


class Overlay:
    """Een doorzichtig, klikdoorlatend venster dat het vervormde beeld toont."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
            WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            "Static", "", WS_POPUP, 0, 0, 1, 1, None, None, None, None)
        if not self.hwnd:
            raise OSError("Kon overlay niet maken")
        self.hdc_screen = user32.GetDC(None)
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_screen)
        bits = ctypes.c_void_p()
        bmi = _bmi(w, h)
        self.hbm = gdi32.CreateDIBSection(
            self.hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not self.hbm or not bits.value:
            raise OSError("Kon bitmap niet maken")
        self.bits = bits.value
        self.old = gdi32.SelectObject(self.hdc_mem, self.hbm)
        self.blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self.size = wintypes.SIZE(w, h)
        self.src_pt = wintypes.POINT(0, 0)
        self.shown = False

    def update(self, frame, x, y):
        ctypes.memmove(self.bits, frame.ctypes.data, frame.nbytes)
        dst = wintypes.POINT(int(x), int(y))
        user32.UpdateLayeredWindow(
            self.hwnd, self.hdc_screen, ctypes.byref(dst), ctypes.byref(self.size),
            self.hdc_mem, ctypes.byref(self.src_pt), 0, ctypes.byref(self.blend), ULW_ALPHA)
        if not self.shown:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.shown = True

    def destroy(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.hdc_mem:
            gdi32.SelectObject(self.hdc_mem, self.old)
            gdi32.DeleteObject(self.hbm)
            gdi32.DeleteDC(self.hdc_mem)
            self.hdc_mem = None
        if self.hdc_screen:
            user32.ReleaseDC(None, self.hdc_screen)
            self.hdc_screen = None


# ============================================================================
#  SLEPEN HERKENNEN EN HET EFFECT AANSTUREN
# ============================================================================

class DragWobbler:
    """
    Werkt bij ELK venster: we kijken niet naar de titelbalk, maar naar het feit dat
    het venster meebeweegt met de muis terwijl de linkermuisknop ingedrukt is
    (en niet van grootte verandert).
    """

    IDLE, ARMED, ACTIVE, WAIT_RELEASE = range(4)

    def __init__(self, get_percent):
        self.get_percent = get_percent
        self.state = self.IDLE
        self.prev_down = False
        self.hwnd = None
        self.hidden_info = None
        self.overlay = None
        self.sim = None
        self.src = None

    def tick(self):
        try:
            self._tick()
        except Exception:
            traceback.print_exc()
            self.shutdown()

    def _tick(self):
        down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        rising = down and not self.prev_down
        self.prev_down = down
        pct = self.get_percent()

        if self.state == self.IDLE:
            if rising and pct > 0:
                self._try_arm()
        elif self.state == self.ARMED:
            self._check_armed(down, pct)
        elif self.state == self.WAIT_RELEASE:
            if not down:
                self.state = self.IDLE
        elif self.state == self.ACTIVE:
            self._update(down, pct)

    # ---------- muisknop ingedrukt: welk venster zit eronder? ----------
    @staticmethod
    def _cursor():
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _try_arm(self):
        x, y = self._cursor()
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return
        root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        if pid.value == os.getpid():
            return
        if not user32.IsWindowVisible(root) or user32.IsIconic(root) or user32.IsZoomed(root):
            return
        if class_name(root) in IGNORED_CLASSES:
            return
        rect = window_rect(root)
        if rect is None:
            return

        self.hwnd = root
        self.start_rect = rect
        self.press_pt = (x, y)
        self.state = self.ARMED

    # ---------- beweegt het venster mee met de muis? ----------
    def _check_armed(self, down, pct):
        if not down:
            self.state = self.IDLE
            return
        if pct <= 0 or not user32.IsWindow(self.hwnd):
            self.state = self.WAIT_RELEASE
            return
        rect = window_rect(self.hwnd)
        if rect is None:
            self.state = self.IDLE
            return

        s = self.start_rect
        # Van grootte veranderd (formaat aanpassen, uit snap halen): geen effect
        if (rect[2] - rect[0], rect[3] - rect[1]) != (s[2] - s[0], s[3] - s[1]):
            self.state = self.WAIT_RELEASE
            return

        wdx, wdy = rect[0] - s[0], rect[1] - s[1]
        if abs(wdx) + abs(wdy) < 3:
            return  # nog niet bewogen: blijf wachten

        cx, cy = self._cursor()
        cdx, cdy = cx - self.press_pt[0], cy - self.press_pt[1]
        if abs(cdx) + abs(cdy) >= 3 and (wdx * cdx + wdy * cdy) > 0:
            self._activate(rect)       # venster volgt de muis: het wordt gesleept
        else:
            self.state = self.WAIT_RELEASE  # venster bewoog zonder de muis: negeren

    # ---------- effect starten ----------
    def _activate(self, rect):
        fl, ft, fr, fb = frame_bounds(self.hwnd) or rect
        w, h = fr - fl, fb - ft
        W, H = w + 2 * MARGIN, h + 2 * MARGIN
        if w < 60 or h < 40 or W * H > MAX_CANVAS_PIXELS:
            self.state = self.WAIT_RELEASE
            return

        snap = grab_screen(fl, ft, w, h)
        src = np.zeros((H, W, 4), dtype=np.uint8)
        src[MARGIN:MARGIN + h, MARGIN:MARGIN + w] = snap

        overlay = None
        try:
            overlay = Overlay(W, H)
            overlay.update(src, fl - MARGIN, ft - MARGIN)  # eerst de foto tonen...
            info = hide_window(self.hwnd)                   # ...dan het echte venster verbergen
            if info is None:
                overlay.destroy()
                self.state = self.WAIT_RELEASE
                return
        except Exception:
            if overlay:
                overlay.destroy()
            self.state = self.WAIT_RELEASE
            raise

        cx, cy = self._cursor()
        grab_local = (cx - (fl - MARGIN), cy - (ft - MARGIN))
        self.sim = WobbleSim(w, h, MARGIN, grab_local, self.get_percent())
        self.src = src
        self.overlay = overlay
        self.hidden_info = info
        self.origin0 = (fl, ft)
        self.wr0 = (rect[0], rect[1])
        self.size0 = (rect[2] - rect[0], rect[3] - rect[1])
        self.released = False
        self.last_time = time.perf_counter()
        self.state = self.ACTIVE

    # ---------- elke frame tijdens het wiebelen ----------
    def _update(self, down, pct):
        now = time.perf_counter()
        dt = min(max(now - self.last_time, 0.001), 0.05)
        self.last_time = now

        if not user32.IsWindow(self.hwnd) or pct <= 0:
            self._finish()
            return
        if not down and not self.released:
            self.released = True
        elif down and self.released:  # opnieuw vastgepakt tijdens uitwiebelen
            self._finish()
            return

        rect = window_rect(self.hwnd)
        if rect is None or (rect[2] - rect[0], rect[3] - rect[1]) != self.size0:
            self._finish()  # venster is van grootte veranderd (bijv. snappen)
            return

        delta = np.array([rect[0] - self.wr0[0], rect[1] - self.wr0[1]], dtype=np.float64)
        self.sim.set_percent(pct)
        self.sim.step(dt, delta)
        frame = self.sim.render(self.src, delta)
        self.overlay.update(
            frame,
            self.origin0[0] + delta[0] - MARGIN,
            self.origin0[1] + delta[1] - MARGIN)

        if self.released and self.sim.settled(delta):
            self._finish()

    def _finish(self):
        if self.hidden_info is not None:
            show_window(self.hwnd, self.hidden_info)
            self.hidden_info = None
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        self.sim = None
        self.src = None
        self.state = self.WAIT_RELEASE if self.prev_down else self.IDLE

    def shutdown(self):
        """Zet alles terug zoals het was. Veilig om vaker aan te roepen."""
        try:
            self._finish()
        except Exception:
            traceback.print_exc()
        self.state = self.IDLE


# ============================================================================
#  INSTELLINGEN, ICOONTJE BIJ DE KLOK EN HET BESTURINGSVENSTER
# ============================================================================

class State:
    """Instellingen; wordt onthouden tussen keren dat je het programma start."""

    PATH = os.path.join(APP_DIR, "settings.json")

    def __init__(self):
        self.enabled = True
        self.percent = 50
        try:
            with open(self.PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.enabled = bool(data.get("enabled", True))
            self.percent = max(0, min(100, int(data.get("percent", 50))))
        except Exception:
            pass

    def save(self):
        try:
            with open(self.PATH, "w", encoding="utf-8") as f:
                json.dump({"enabled": self.enabled, "percent": self.percent}, f)
        except Exception:
            pass

    def effective_percent(self):
        return self.percent if self.enabled else 0


def make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((5, 9, 59, 55), radius=9, fill=(30, 136, 229, 255))
    d.rounded_rectangle((5, 9, 59, 22), radius=9, fill=(21, 101, 192, 255))
    d.line([(12 + i, 40 + 6 * math.sin(i / 5.0)) for i in range(0, 41)],
           fill=(255, 255, 255, 255), width=4)
    return img


class App:
    def __init__(self, root):
        self.root = root
        self.state = State()
        self.cmds = queue.Queue()  # opdrachten vanuit het icoontje (andere thread)
        self._syncing = False
        self._told_about_tray = False

        self.wobbler = DragWobbler(self.state.effective_percent)
        atexit.register(self.wobbler.shutdown)

        root.title("Window Wobble")
        root.geometry("370x290")
        root.resizable(False, False)

        self.enabled_var = tk.BooleanVar(value=self.state.enabled)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Window Wobble",
                  font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Sleep een venster en voel de gelei.",
                  foreground="#666").pack(anchor="w", pady=(0, 12))

        ttk.Checkbutton(frame, text="Wobbly windows aan", variable=self.enabled_var,
                        command=self._on_toggle).pack(anchor="w")

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(14, 0))
        ttk.Label(row, text="Intensiteit:").pack(side="left")
        self.percent_label = ttk.Label(row, text="50%", width=5, anchor="e")
        self.percent_label.pack(side="right")

        self.slider = ttk.Scale(frame, from_=0, to=100, orient="horizontal",
                                command=self._on_slider)
        self.slider.pack(fill="x", pady=(4, 0))
        self.slider.bind("<ButtonRelease-1>", lambda e: self.state.save())

        ttk.Label(frame, text="0% = uit, 100% = maximaal zwabberen",
                  foreground="#888").pack(anchor="w", pady=(2, 0))
        ttk.Label(frame,
                  text="Als je dit venster sluit, draait het programma verder\n"
                       "op de achtergrond (icoontje bij de klok).",
                  foreground="#888", justify="left").pack(anchor="w", pady=(10, 0))
        ttk.Button(frame, text="Helemaal afsluiten", command=self.quit).pack(
            anchor="w", pady=(10, 0))

        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._sync_ui()

        self.icon = pystray.Icon("WindowWobble", make_tray_image(),
                                 "Window Wobble", self._build_menu())
        self.icon.run_detached()
        self._loop()

    # ---------- icoontje bij de klok ----------
    def _build_menu(self):
        def make_pct(p):
            def cb(icon, item):
                self.cmds.put(("percent", p))
            return cb

        def make_checked(p):
            def chk(item):
                return self.state.percent == p
            return chk

        pct_items = [pystray.MenuItem(f"{p}%", make_pct(p), checked=make_checked(p), radio=True)
                     for p in (10, 25, 50, 75, 100)]
        return pystray.Menu(
            pystray.MenuItem("Instellingen openen",
                             lambda icon, item: self.cmds.put(("open",)), default=True),
            pystray.MenuItem("Wobbly windows aan",
                             lambda icon, item: self.cmds.put(("toggle",)),
                             checked=lambda item: self.state.enabled),
            pystray.MenuItem("Intensiteit", pystray.Menu(*pct_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Afsluiten", lambda icon, item: self.cmds.put(("quit",))),
        )

    def _handle_cmd(self, cmd):
        kind = cmd[0]
        if kind == "open":
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        elif kind == "toggle":
            self.state.enabled = not self.state.enabled
            if not self.state.enabled:
                self.wobbler.shutdown()
            self.state.save()
        elif kind == "percent":
            self.state.percent = cmd[1]
            self.state.enabled = True
            self.state.save()
        elif kind == "quit":
            self.quit()
            return
        self._sync_ui()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    # ---------- besturingsvenster ----------
    def _sync_ui(self):
        self._syncing = True
        self.enabled_var.set(self.state.enabled)
        self.slider.set(self.state.percent)
        self.percent_label.config(text=f"{self.state.percent}%")
        self._syncing = False

    def _on_slider(self, value):
        if self._syncing:
            return
        self.state.percent = int(float(value))
        self.percent_label.config(text=f"{self.state.percent}%")

    def _on_toggle(self):
        self.state.enabled = bool(self.enabled_var.get())
        if not self.state.enabled:
            self.wobbler.shutdown()
        self.state.save()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def hide_to_tray(self):
        self.root.withdraw()
        self.state.save()
        if not self._told_about_tray:
            self._told_about_tray = True
            try:
                self.icon.notify("Window Wobble draait verder op de achtergrond.\n"
                                 "Rechtsklik op dit icoontje om af te sluiten.",
                                 "Window Wobble")
            except Exception:
                pass

    def quit(self):
        self.wobbler.shutdown()
        self.state.save()
        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.destroy()

    def _loop(self):
        try:
            while True:
                self._handle_cmd(self.cmds.get_nowait())
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.wobbler.tick()
        try:
            self.root.after(8, self._loop)
        except tk.TclError:
            pass


def _already_running():
    """Voorkomt dat het programma twee keer tegelijk draait."""
    handle = kernel32.CreateMutexW(None, False, "Local\\WindowWobbleSingleInstance")
    err = ctypes.get_last_error()
    _already_running.handle = handle  # vasthouden zolang het proces leeft
    return err == ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if _already_running():
        user32.MessageBoxW(
            None,
            "Window Wobble draait al op de achtergrond.\n"
            "Zoek het icoontje bij de klok (misschien achter het pijltje ^).",
            "Window Wobble", 0x40)
        raise SystemExit(0)
    root = tk.Tk()
    App(root)
    root.mainloop()
