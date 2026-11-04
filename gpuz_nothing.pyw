"""
GPU-Z monitor, Nothing-style.
Reads GPU-Z's shared memory, so GPU-Z must be running (it can stay minimized).
Keys:  T = always on top   F11 = fullscreen   Esc = leave fullscreen
Resize the window freely; the tiles re-flow and the dot-matrix numbers scale.
"""
import ctypes, math, struct, sys, time
import tkinter as tk
from tkinter import font as tkfont

# ---- settings ---------------------------------------------------------------
REFRESH_MS = 500
TOPMOST_AT_START = False
GHOST_DOTS = False            # True = faint unlit dots behind the numbers
BG, CARD, EDGE = "#000000", "#0c0c0c", "#212121"
FG, DIM, FAINT, RED = "#f2f2f2", "#7a7a7a", "#2a2a2a", "#d71921"
BLUE, YELLOW = "#4f9bff", "#f0c24b"
# temperature colors: below 60 blue, 60-75 yellow, 75 and above red
TEMP_BANDS = ((60, BLUE), (75, YELLOW), (float("inf"), RED))
UI_FONTS = ("Bahnschrift SemiLight", "Bahnschrift", "Segoe UI")

# PerfCap bit names, lowest bit first. Treat this as best-effort; the raw
# number is shown on the tile so you can check it against GPU-Z.
PERFCAP_BITS = ["IDLE", "PWR", "THRM", "REL", "OP", "UTIL"]
PERFCAP_INFO = {"IDLE": "GPU idle", "PWR": "Power limit", "THRM": "Thermal limit",
                "REL": "Reliability voltage limit", "OP": "Max operating voltage",
                "UTIL": "Low utilization", "?": "Unknown limit"}

# label, sensor-name prefixes, decimals, meter, color bands
#   meter: ("max", N) fixed scale | ("auto", floor) scales to session peak | ("ram", 0)
TILES = [
    ("GPU clock",    ("GPU Clock",),          0, ("auto", 2000), None),
    ("Memory clock", ("Memory Clock",),       0, ("auto", 2000), None),
    ("GPU temp",     ("GPU Temperature",),    0, ("max", 100),   TEMP_BANDS),
    ("GPU load",     ("GPU Load",),           0, ("max", 100),   None),
    ("Board power",  ("Board Power Draw",),   1, ("auto", 60),   None),
    ("Chip power",   ("GPU Chip Power Draw",), 1, ("auto", 50),  None),
    ("Voltage",      ("GPU Voltage",),        3, ("max", 1.2),   None),
    ("PCIe TX",      ("PCIe TX",),            2, ("auto", 1),    None),
    ("CPU temp",     ("CPU Temperature",),    0, ("max", 100),   TEMP_BANDS),
    ("System RAM",   ("System Memory Used",), 1, ("ram", 0),     None),
    ("PerfCap",      ("PerfCap Reason",),     0, None,           None),
]

# ---- GPU-Z shared memory ------------------------------------------------------
# struct (pack 1): u32 version, i32 busy, u32 lastUpdate,
#   128 x {key[256], value[256]} wchar,
#   128 x {name[256], unit[8] wchar, u32 digits, f64 value}
K32 = ctypes.windll.kernel32 if sys.platform == "win32" else None
if K32:
    K32.OpenFileMappingW.restype = ctypes.c_void_p
    K32.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    K32.MapViewOfFile.restype = ctypes.c_void_p
    K32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_size_t]
    K32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    K32.CloseHandle.argtypes = [ctypes.c_void_p]

BASE = 12 + 128 * 1024
LAYOUTS = [(BASE, 540, 532), (BASE + 4, 544, 536)]   # packed, then 8-byte aligned
MAP_SIZE = BASE + 128 * 540
_handle = _view = None


def _open():
    global _handle, _view
    if _view:
        return _view
    if not K32:
        return None
    _handle = K32.OpenFileMappingW(4, False, "GPUZShMem")
    if _handle:
        _view = K32.MapViewOfFile(_handle, 4, 0, 0, 0)
    return _view


def _close():
    global _handle, _view
    if _view:
        K32.UnmapViewOfFile(_view)
    if _handle:
        K32.CloseHandle(_handle)
    _handle = _view = None


def _text(b):
    return b.decode("utf-16-le", "ignore").split("\x00")[0].strip()


def parse(raw, base, size, voff):
    rows = []
    for i in range(128):
        o = base + i * size
        if o + size > len(raw):
            break
        name = _text(raw[o:o + 512])
        if not name:
            break
        unit = _text(raw[o + 512:o + 528])
        val = struct.unpack_from("<d", raw, o + voff)[0]
        rows.append((name, unit, val if math.isfinite(val) else None))
    return rows


def read_gpuz():
    """-> ({name: (unit, value)}, lastUpdateTick) or None"""
    try:
        v = _open()
        if not v:
            return None
        raw = ctypes.string_at(v, MAP_SIZE)
        tick = struct.unpack_from("<I", raw, 8)[0]
        for layout in LAYOUTS:
            rows = parse(raw, *layout)
            if rows:
                return {r[0]: (r[1], r[2]) for r in rows}, tick
    except Exception:
        _close()
    return None


class MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32)] + \
               [(n, ctypes.c_uint64) for n in ("tp", "ap", "tpf", "apf", "tv", "av", "aev")]


def total_ram_mb():
    try:
        m = MEMSTAT()
        m.dwLength = ctypes.sizeof(m)
        K32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.tp / 1048576
    except Exception:
        return 0


# ---- 5x7 dot-matrix glyphs ------------------------------------------------------
_G = {
    "0": ".###.|#...#|#..##|#.#.#|##..#|#...#|.###.",
    "1": "..#..|.##..|..#..|..#..|..#..|..#..|.###.",
    "2": ".###.|#...#|....#|...#.|..#..|.#...|#####",
    "3": "#####|...#.|..#..|...#.|....#|#...#|.###.",
    "4": "...#.|..##.|.#.#.|#..#.|#####|...#.|...#.",
    "5": "#####|#....|####.|....#|....#|#...#|.###.",
    "6": "..##.|.#...|#....|####.|#...#|#...#|.###.",
    "7": "#####|....#|...#.|..#..|.#...|.#...|.#...",
    "8": ".###.|#...#|#...#|.###.|#...#|#...#|.###.",
    "9": ".###.|#...#|#...#|.####|....#|...#.|.##..",
    "A": ".###.|#...#|#...#|#####|#...#|#...#|#...#",
    "B": "####.|#...#|#...#|####.|#...#|#...#|####.",
    "C": ".###.|#...#|#....|#....|#....|#...#|.###.",
    "D": "####.|#...#|#...#|#...#|#...#|#...#|####.",
    "E": "#####|#....|#....|####.|#....|#....|#####",
    "F": "#####|#....|#....|####.|#....|#....|#....",
    "G": ".###.|#...#|#....|#.###|#...#|#...#|.####",
    "H": "#...#|#...#|#...#|#####|#...#|#...#|#...#",
    "I": ".###.|..#..|..#..|..#..|..#..|..#..|.###.",
    "J": "..###|...#.|...#.|...#.|...#.|#..#.|.##..",
    "K": "#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#",
    "L": "#....|#....|#....|#....|#....|#....|#####",
    "M": "#...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#",
    "N": "#...#|##..#|#.#.#|#..##|#...#|#...#|#...#",
    "O": ".###.|#...#|#...#|#...#|#...#|#...#|.###.",
    "P": "####.|#...#|#...#|####.|#....|#....|#....",
    "Q": ".###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#",
    "R": "####.|#...#|#...#|####.|#.#..|#..#.|#...#",
    "S": ".####|#....|#....|.###.|....#|....#|####.",
    "T": "#####|..#..|..#..|..#..|..#..|..#..|..#..",
    "U": "#...#|#...#|#...#|#...#|#...#|#...#|.###.",
    "V": "#...#|#...#|#...#|#...#|#...#|.#.#.|..#..",
    "W": "#...#|#...#|#...#|#.#.#|#.#.#|##.##|#...#",
    "X": "#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#",
    "Y": "#...#|#...#|.#.#.|..#..|..#..|..#..|..#..",
    "Z": "#####|....#|...#.|..#..|.#...|#....|#####",
    ".": ".|.|.|.|.|.|#",
    "-": ".....|.....|.....|#####|.....|.....|.....",
    "+": ".....|..#..|..#..|#####|..#..|..#..|.....",
    " ": "...|...|...|...|...|...|...",
}
GLYPHS = {k: v.split("|") for k, v in _G.items()}


def dot_cols(text):
    return sum(len(GLYPHS.get(c, GLYPHS[" "])[0]) for c in text) + max(len(text) - 1, 0)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---- app ------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        super().__init__()
        self.S = self.winfo_fpixels("1i") / 96
        S = self.S
        fams = set(tkfont.families())
        self.ui = next((f for f in UI_FONTS if f in fams), "TkDefaultFont")

        self.title("GPU Monitor")
        self.configure(bg=BG)
        self.geometry(f"{int(900 * S)}x{int(540 * S)}")
        self.minsize(int(300 * S), int(220 * S))
        self.attributes("-topmost", TOPMOST_AT_START)
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)

        self.data, self.state = {}, "Waiting for GPU-Z"
        self.peak, self.ram_mb = {}, total_ram_mb()
        self.last_tick, self.last_change = None, time.monotonic()
        self._redraw_job = None

        self.cv.bind("<Configure>", lambda e: self.schedule_redraw())
        self.bind("<t>", self.toggle_top)
        self.bind("<F11>", lambda e: self.attributes("-fullscreen", not self.attributes("-fullscreen")))
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        self.dark_titlebar()
        self.poll()

    # window chrome
    def dark_titlebar(self):
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dw = ctypes.windll.dwmapi.DwmSetWindowAttribute
            for attr, val in ((20, 1), (35, 0x000000), (36, 0xFFFFFF), (34, 0x181818)):
                dw(hwnd, attr, ctypes.byref(ctypes.c_int(val)), 4)
        except Exception:
            pass

    def toggle_top(self, _=None):
        self.attributes("-topmost", not self.attributes("-topmost"))

    # data
    def poll(self):
        res = read_gpuz()
        now = time.monotonic()
        if res is None:
            self.data, self.state = {}, "Waiting for GPU-Z"
        else:
            self.data, tick = res
            if tick != self.last_tick or tick == 0:
                self.last_tick, self.last_change = tick, now
            if tick and now - self.last_change > 12:
                self.state = "Stale, reconnecting"
                _close()
            elif not any(self.find(t[1]) for t in TILES):
                self.state = "Connected, no matching sensors"
            else:
                self.state = "Live"
        self.redraw()
        self.after(REFRESH_MS, self.poll)

    def find(self, prefixes):
        for name, rec in self.data.items():
            low = name.lower()
            if any(low.startswith(p.lower()) for p in prefixes):
                return rec
        return None

    # drawing
    def schedule_redraw(self):
        if self._redraw_job is None:
            self._redraw_job = self.after(15, self.redraw)

    def dots(self, text, x, y, p, color):
        cv = self.cv
        p = max(int(p), 2)
        d = max(2, round(p * 0.7))
        off = (p - d) // 2
        x, y = round(x), round(y)
        for ch in text:
            g = GLYPHS.get(ch, GLYPHS[" "])
            for ry, row in enumerate(g):
                for rx, c in enumerate(row):
                    if c == "#" or GHOST_DOTS:
                        px, py = x + rx * p + off, y + ry * p + off
                        cv.create_oval(px, py, px + d, py + d, outline="",
                                       fill=color if c == "#" else "#141414")
            x += (len(g[0]) + 1) * p

    def rounded(self, x0, y0, x1, y1, r):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        self.cv.create_polygon(pts, smooth=True, fill=CARD, outline=EDGE)

    def redraw(self):
        self._redraw_job = None
        cv, S = self.cv, self.S
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 50 or H < 50:
            return
        gap = clamp(min(W, H) * 0.022, 6 * S, 16 * S)

        # header
        hh = clamp(H * 0.09, 30 * S, 48 * S)
        live = self.state == "Live"
        dr = hh * 0.11
        cv.create_oval(gap + 4, hh / 2 - dr, gap + 4 + 2 * dr, hh / 2 + dr,
                       fill=RED if live else FAINT, outline="")
        p = max(int(hh * 0.07), 2)
        self.dots("GPU MONITOR", gap + 4 + 2 * dr + hh * 0.3, hh / 2 - 3.5 * p, p, FG)
        cv.create_text(W - gap - 2, hh / 2, text=self.state, anchor="e", fill=DIM,
                       font=(self.ui, -int(clamp(hh * 0.32, 9 * S, 14 * S))))

        # grid: choose the column count whose tiles are closest to a 1.4:1 shape
        n = len(TILES)
        top = hh
        aw, ah = W, H - top
        best = None
        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            tw, th = (aw - gap * (cols + 1)) / cols, (ah - gap * (rows + 1)) / rows
            if tw <= 0 or th <= 0:
                continue
            score = abs(math.log(tw / th / 1.4)) + 0.05 * (cols * rows - n)
            if best is None or score < best[0]:
                best = (score, cols, rows)
        if not best:
            return
        _, cols, rows = best
        th = (ah - gap * (rows + 1)) / rows
        for i, spec in enumerate(TILES):
            r, c = divmod(i, cols)
            count = min(cols, n - r * cols)         # last row stretches to fill
            tw = (aw - gap * (count + 1)) / count
            x0 = gap + c * (tw + gap)
            y0 = top + gap + r * (th + gap)
            self.tile(x0, y0, x0 + tw, y0 + th, spec)

    def tile(self, x0, y0, x1, y1, spec):
        label, prefixes, dec, meter, bands = spec
        cv, S = self.cv, self.S
        w, h = x1 - x0, y1 - y0
        m = min(w, h)
        pad = clamp(m * 0.1, 7 * S, 20 * S)
        self.rounded(x0, y0, x1, y1, clamp(m * 0.14, 7 * S, 22 * S))

        rec = self.find(prefixes)
        if rec and rec[1] is None:
            rec = None
        text, unit, color, frac, desc = "-", "", FAINT, None, ""
        if rec:
            unit, val = rec
            unit = unit.replace("%%", "%")
            if label == "PerfCap":
                text, color, unit, desc = self.perfcap(int(val))
            else:
                if label == "System RAM" and unit.upper() == "MB":
                    mb, val, unit = val, val / 1024, "GB"
                else:
                    mb = val
                text, color = f"{val:.{dec}f}", FG
                if bands:
                    color = next(c for lim, c in bands if val < lim)
                kind, arg = meter
                if kind == "max":
                    frac = val / arg
                elif kind == "auto":
                    pk = self.peak[label] = max(self.peak.get(label, arg), val)
                    frac = val / pk
                elif kind == "ram" and self.ram_mb:
                    frac = mb / self.ram_mb

        # label and unit
        lf = int(clamp(h * 0.085, 9 * S, 16 * S))
        fit = lambda f: tkfont.Font(family=self.ui, size=-f).measure(label + "   " + unit)
        while lf > 8 and fit(lf) > w - 2 * pad:
            lf -= 1
        cv.create_text(x0 + pad, y0 + pad, text=label, anchor="nw", fill=DIM, font=(self.ui, -lf))
        if unit and fit(lf) <= w - 2 * pad:
            cv.create_text(x1 - pad, y0 + pad, text=unit, anchor="ne", fill=DIM, font=(self.ui, -lf))

        # meter along the bottom
        rw = w - 2 * pad
        bottom = y1 - pad
        if meter:
            nd = max(10, int(rw // clamp(m * 0.085, 7 * S, 14 * S)))
            step = rw / nd
            rad = clamp(step * 0.26, 1.2, 4 * S)
            lit = round(clamp(frac or 0, 0, 1) * nd)
            my = y1 - pad - rad
            for k in range(nd):
                cx = x0 + pad + (k + 0.5) * step
                cv.create_oval(cx - rad, my - rad, cx + rad, my + rad, outline="",
                               fill=color if k < lit else FAINT)
            bottom = my - rad - pad * 0.9

        # short explanation line (PerfCap)
        if desc:
            df = lf
            while df > 8 and tkfont.Font(family=self.ui, size=-df).measure(desc) > rw:
                df -= 1
            cv.create_text(x0 + pad, y1 - pad, text=desc, anchor="sw", fill=DIM,
                           font=(self.ui, -df))
            bottom = y1 - pad - df * 1.7

        # dot-matrix number, as large as the free area allows
        ntop = y0 + pad + lf * 1.5
        rh = max(bottom - ntop, 8)
        p = max(int(min(rw / dot_cols(text), rh / 7)), 2)
        self.dots(text, x0 + pad, ntop + (rh - 7 * p) / 2, p, color)

    @staticmethod
    def perfcap(v):
        """-> (dot text, color, code label, description)"""
        code = f"code {v}"
        if v == 0:
            return "NONE", "#4a4a4a", code, "No limit active"
        names = [PERFCAP_BITS[b] if b < len(PERFCAP_BITS) else "?" for b in range(16) if v >> b & 1]
        active = [x for x in names if x != "IDLE"]
        if not active:
            return "IDLE", DIM, code, PERFCAP_INFO["IDLE"]
        color = RED if "THRM" in active else FG          # only heat is a warning
        return "+".join(active), color, code, ", ".join(PERFCAP_INFO[x] for x in active)


if __name__ == "__main__":
    App().mainloop()
