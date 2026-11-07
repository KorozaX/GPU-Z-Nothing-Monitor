"""
GPU-Z Nothing Monitor
A dark, dot-matrix dashboard fed by GPU-Z's shared memory. GPU-Z must be running
(it can stay minimized). Tested on NVIDIA only.

Keys:  T = always on top   F11 = fullscreen   Esc = leave fullscreen
       D = save a sensor dump (gpuz_dump.txt) and open its folder

Settings live in %APPDATA%\\GPUZ-Nothing-Monitor\\ :
  config.json  tiles, thresholds, refresh rate (created on first run, safe to edit)
  state.json   window size, position, always-on-top (managed by the app)
"""
import ctypes, json, math, os, re, struct, sys, time
import tkinter as tk
from tkinter import font as tkfont

APP_NAME = "GPUZ-Nothing-Monitor"

# ---- look ---------------------------------------------------------------------
TOPMOST_AT_START = False
BG, CARD, EDGE = "#000000", "#0c0c0c", "#212121"
FG, DIM, FAINT, RED = "#f2f2f2", "#7a7a7a", "#2a2a2a", "#d71921"
BLUE, YELLOW = "#4f9bff", "#f0c24b"
UI_FONTS = ("Bahnschrift SemiLight", "Bahnschrift", "Segoe UI")

# ---- default config (copied to config.json on first run) ----------------------
# sensors: GPU-Z sensor-name prefixes, first match wins.
# kind:    "number" (default) | "memory" (MB shown as GB) | "perfcap"
# meter:   {"type": "max", "max": N} | {"type": "auto", "floor": N}
#          | {"type": "ram"} | {"type": "vram"}   (auto = scales to the session peak)
# bands:   "temp" colors the value with temp_bands
DEFAULT_TILES = [
    {"label": "GPU clock", "sensors": ["GPU Clock"], "meter": {"type": "auto", "floor": 1500}},
    {"label": "Memory clock", "sensors": ["Memory Clock"], "meter": {"type": "auto", "floor": 1500}},
    {"label": "GPU temp", "sensors": ["GPU Temperature"], "meter": {"type": "max", "max": 100},
     "bands": "temp"},
    {"label": "GPU load", "sensors": ["GPU Load"], "meter": {"type": "max", "max": 100}},
    {"label": "VRAM", "sensors": ["Memory Used (Dedicated)", "Memory Used"], "decimals": 1,
     "kind": "memory", "meter": {"type": "vram"}},
    {"label": "Board power", "sensors": ["Board Power Draw"], "decimals": 1,
     "meter": {"type": "auto", "floor": 50}},
    {"pair": [{"label": "Chip power", "sensors": ["GPU Chip Power Draw"], "decimals": 1},
              {"label": "Voltage", "sensors": ["GPU Voltage"], "decimals": 2}]},
    {"label": "PCIe TX", "sensors": ["PCIe TX"], "decimals": 2, "meter": {"type": "auto", "floor": 1}},
    {"label": "CPU temp", "sensors": ["CPU Temperature"], "meter": {"type": "max", "max": 100},
     "bands": "temp"},
    {"label": "System RAM", "sensors": ["System Memory Used"], "decimals": 1, "kind": "memory",
     "meter": {"type": "ram"}},
    {"label": "PerfCap", "sensors": ["PerfCap Reason"], "kind": "perfcap"},
]
DEFAULT_CONFIG = {
    "refresh_ms": 500,
    "ghost_dots": False,                 # faint unlit dots behind the numbers
    "vram_total_mb": None,               # null = detect from GPU-Z; or set e.g. 6144
    "temp_bands": [[60, BLUE], [75, YELLOW], [1000, RED]],   # below 60 / 60-75 / 75+
    "tiles": DEFAULT_TILES,
}

# PerfCap: GPU-Z publishes a bit mask. Idle = 16 is reported by users reading the
# shared memory; the other bit positions follow the same order GPU-Z lists its
# reasons in and are not officially documented. The raw code is shown on the tile.
PERFCAP_BITS = {0: ("PWR", "Power limit"), 1: ("THRM", "Thermal limit"),
                2: ("VREL", "Reliability voltage limit"), 3: ("VOP", "Max operating voltage"),
                4: ("IDLE", "GPU idle")}


# ---- settings files -------------------------------------------------------------
def data_dir():
    d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def load_json(name):
    try:
        with open(os.path.join(data_dir(), name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(name, obj):
    try:
        with open(os.path.join(data_dir(), name), "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
    except OSError:
        pass


def norm_tile(t):
    if "pair" in t:
        a, b = t["pair"]
        return {"pair": [norm_tile(a), norm_tile(b)]}
    return {"label": str(t["label"]), "sensors": [str(s) for s in t["sensors"]],
            "decimals": int(t.get("decimals", 0)), "kind": t.get("kind", "number"),
            "meter": t.get("meter"), "bands": t.get("bands")}


def _finish(cfg):
    cfg["tiles"] = [norm_tile(t) for t in cfg["tiles"]]
    cfg["temp_bands"] = [(float(lim), str(col)) for lim, col in cfg["temp_bands"]]
    cfg["refresh_ms"] = max(100, int(cfg["refresh_ms"]))
    return cfg


def load_config():
    """-> (config dict, error text). Falls back to defaults if config.json is broken."""
    cfg, err = json.loads(json.dumps(DEFAULT_CONFIG)), ""
    path = os.path.join(data_dir(), "config.json")
    if not os.path.exists(path):
        save_json("config.json", DEFAULT_CONFIG)
    else:
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
        except Exception as e:
            err = f"config.json unreadable ({e.__class__.__name__}), using defaults"
    try:
        return _finish(cfg), err
    except Exception as e:
        err = f"config.json has an invalid entry ({e.__class__.__name__}), using defaults"
        return _finish(json.loads(json.dumps(DEFAULT_CONFIG))), err


# ---- GPU-Z shared memory ---------------------------------------------------------
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


def parse_info(raw):
    info = {}
    for i in range(128):
        o = 12 + i * 1024
        key = _text(raw[o:o + 512])
        if not key:
            break
        info[key] = _text(raw[o + 512:o + 1024])
    return info


def read_gpuz():
    """-> ({sensor: (unit, value)}, lastUpdateTick, {info key: value}) or None"""
    try:
        v = _open()
        if not v:
            return None
        raw = ctypes.string_at(v, MAP_SIZE)
        tick = struct.unpack_from("<I", raw, 8)[0]
        for layout in LAYOUTS:
            rows = parse(raw, *layout)
            if rows:
                return {r[0]: (r[1], r[2]) for r in rows}, tick, parse_info(raw)
    except Exception:
        _close()
    return None


# ---- VRAM size from GPU-Z's static info ---------------------------------------------
_EXACT = {"memsize", "memorysize", "vramsize", "videomemory", "memtotal", "memorytotal",
          "totalmemory", "vram", "memorycapacity", "memcapacity"}
_SKIP = ("shared", "system", "dynamic", "bus", "clock", "type", "vendor", "used", "free",
         "bandwidth", "width")


def parse_size_mb(text):
    m = re.search(r"(\d[\d.,]*)\s*(TB|GB|MB|KB)?", text, re.I)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").upper() or ("MB" if n >= 128 else "GB")
    mb = n * {"KB": 1 / 1024, "MB": 1, "GB": 1024, "TB": 1048576}[unit]
    return mb if 128 <= mb <= 1048576 else None


def find_vram_total(info):
    loose = []
    for key, val in info.items():
        nk = re.sub(r"[^a-z]", "", key.lower())
        mb = parse_size_mb(val)
        if not mb:
            continue
        if nk in _EXACT:
            return mb
        if "mem" in nk and "size" in nk and not any(w in nk for w in _SKIP):
            loose.append(mb)
    return loose[0] if loose else None


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


# ---- 5x7 dot-matrix glyphs ------------------------------------------------------------
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
    "?": ".###.|#...#|....#|...#.|..#..|.....|..#..",
    " ": "...|...|...|...|...|...|...",
}
GLYPHS = {k: v.split("|") for k, v in _G.items()}


def dot_cols(text):
    return sum(len(GLYPHS.get(c, GLYPHS[" "])[0]) for c in text) + max(len(text) - 1, 0)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---- app ------------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        super().__init__()
        self.S = S = self.winfo_fpixels("1i") / 96
        fams = set(tkfont.families())
        self.ui = next((f for f in UI_FONTS if f in fams), "TkDefaultFont")

        self.cfg, self.notice = load_config()
        self.tiles, self.bands = self.cfg["tiles"], self.cfg["temp_bands"]
        self.ghost = bool(self.cfg["ghost_dots"])
        self.vram_total = self.cfg["vram_total_mb"]
        st = load_json("state.json")
        self.top = bool(st.get("topmost", TOPMOST_AT_START))

        self.title("GPU Monitor")
        self.configure(bg=BG)
        self.geometry(self.fit_geometry(st.get("geometry")) or f"{int(900 * S)}x{int(540 * S)}")
        self.minsize(int(300 * S), int(220 * S))
        self.attributes("-topmost", self.top)
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.tag_bind("pin", "<Button-1>", self.toggle_top)
        self.cv.tag_bind("pin", "<Enter>", lambda e: self.cv.config(cursor="hand2"))
        self.cv.tag_bind("pin", "<Leave>", lambda e: self.cv.config(cursor=""))

        self.data, self.info, self.state = {}, {}, "Waiting for GPU-Z"
        self.peak, self.ram_mb, self.flash = {}, total_ram_mb(), ("", 0)
        self.last_tick, self.last_change = None, time.monotonic()
        self._redraw_job = self._save_job = None

        self.cv.bind("<Configure>", self.on_configure)
        self.bind("<t>", self.toggle_top)
        self.bind("<d>", lambda e: self.dump())
        self.bind("<F11>", lambda e: self.attributes("-fullscreen", not self.attributes("-fullscreen")))
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.dark_titlebar()
        self.poll()

    # ---- window chrome and saved state
    def dark_titlebar(self):
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dw = ctypes.windll.dwmapi.DwmSetWindowAttribute
            for attr, val in ((20, 1), (35, 0x000000), (36, 0xFFFFFF), (34, 0x181818)):
                dw(hwnd, attr, ctypes.byref(ctypes.c_int(val)), 4)
        except Exception:
            pass

    def virtual_screen(self):
        try:
            u = ctypes.windll.user32
            return [u.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
        except Exception:
            return [0, 0, self.winfo_screenwidth(), self.winfo_screenheight()]

    def fit_geometry(self, geo):
        """Saved 'WxH+X+Y' -> usable geometry, dropping the position if it is off-screen."""
        try:
            w, h, x, y = (int(n) for n in re.findall(r"-?\d+", geo))
        except Exception:
            return None
        if w < 200 or h < 150:
            return None
        vx, vy, vw, vh = self.virtual_screen()
        if x + 80 > vx and x < vx + vw - 80 and vy - 10 <= y < vy + vh - 60:
            return f"{w}x{h}+{x}+{y}"
        return f"{w}x{h}"

    def save_state(self):
        st = load_json("state.json")
        st["topmost"] = self.top
        try:
            if self.state_is_normal():
                st["geometry"] = self.geometry()
        except tk.TclError:
            pass
        save_json("state.json", st)

    def state_is_normal(self):
        return self.wm_state() == "normal" and not self.attributes("-fullscreen")

    def on_configure(self, _=None):
        self.schedule_redraw()
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(1500, self.save_state)

    def on_close(self):
        self.save_state()
        self.destroy()

    def toggle_top(self, _=None):
        self.top = not self.top
        self.attributes("-topmost", self.top)
        self.save_state()
        self.redraw()

    # ---- data
    def poll(self):
        res = read_gpuz()
        now = time.monotonic()
        if res is None:
            self.data, self.info, self.state = {}, {}, "Waiting for GPU-Z"
        else:
            self.data, tick, self.info = res
            if self.cfg["vram_total_mb"] is None:
                self.vram_total = find_vram_total(self.info)
            if tick != self.last_tick or tick == 0:
                self.last_tick, self.last_change = tick, now
            if tick and now - self.last_change > 12:
                self.state = "Stale, reconnecting"
                _close()
            elif not self.visible_tiles():
                self.state = "Connected, no matching sensors"
            else:
                self.state = "Live"
        self.redraw()
        self.after(self.cfg["refresh_ms"], self.poll)

    def find(self, prefixes):
        for p in prefixes:                      # earlier prefixes take priority
            for name, rec in self.data.items():
                if name.lower().startswith(p.lower()):
                    return rec
        return None

    def visible_tiles(self):
        """Tiles whose sensor GPU-Z actually reports; the rest are hidden."""
        out = []
        for t in self.tiles:
            if "pair" in t:
                have = [s for s in t["pair"] if self.find(s["sensors"]) is not None]
                if len(have) == 2:
                    out.append(t)
                elif have:
                    out.append(have[0])
            elif self.find(t["sensors"]) is not None:
                out.append(t)
        return out

    def dump(self):
        flat = [s for t in self.tiles for s in (t["pair"] if "pair" in t else [t])]
        lines = [f"{APP_NAME} dump", time.ctime(),
                 f"python {sys.version.split()[0]} ({struct.calcsize('P') * 8}-bit)",
                 f"state: {self.state}",
                 f"detected VRAM total (MB): {find_vram_total(self.info)}", "",
                 "[GPU-Z info records]"]
        lines += [f"{k} = {v}" for k, v in self.info.items()]
        lines += ["", "[sensors: name | unit | value]"]
        lines += [f"{n} | {u} | {v}" for n, (u, v) in self.data.items()]
        lines += ["", "[tiles]"]
        lines += [f"{s['label']}: {'found' if self.find(s['sensors']) is not None else 'MISSING'}"
                  for s in flat]
        path = os.path.join(data_dir(), "gpuz_dump.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self.flash = ("Saved gpuz_dump.txt", time.monotonic() + 4)
        except OSError:
            self.flash = ("Could not save dump", time.monotonic() + 4)
        try:
            os.startfile(data_dir())
        except Exception:
            pass
        self.redraw()

    # ---- drawing helpers
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
                    if c == "#" or self.ghost:
                        px, py = x + rx * p + off, y + ry * p + off
                        cv.create_oval(px, py, px + d, py + d, outline="",
                                       fill=color if c == "#" else "#141414")
            x += (len(g[0]) + 1) * p

    def rounded(self, x0, y0, x1, y1, r, fill=CARD, outline=EDGE, tags=()):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        self.cv.create_polygon(pts, smooth=True, fill=fill, outline=outline, tags=tags)

    def message(self, top, title, sub):
        cv, S = self.cv, self.S
        W, H = cv.winfo_width(), cv.winfo_height()
        cy = top + (H - top) / 2
        tf = int(clamp(H * 0.05, 14 * S, 26 * S))
        cv.create_text(W / 2, cy - tf * 0.4, text=title, fill=FG, font=(self.ui, -tf))
        cv.create_text(W / 2, cy + tf * 0.9, text=sub, fill=DIM,
                       font=(self.ui, -int(clamp(tf * 0.6, 10 * S, 15 * S))), width=W * 0.8,
                       justify="center")

    def redraw(self):
        self._redraw_job = None
        cv, S = self.cv, self.S
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 50 or H < 50:
            return
        gap = clamp(min(W, H) * 0.022, 6 * S, 16 * S)

        # header: live dot, title, status, always-on-top button
        hh = clamp(H * 0.09, 30 * S, 48 * S)
        live = self.state == "Live"
        dr = hh * 0.11
        cv.create_oval(gap + 4, hh / 2 - dr, gap + 4 + 2 * dr, hh / 2 + dr,
                       fill=RED if live else FAINT, outline="")
        p = max(int(hh * 0.07), 2)
        self.dots("GPU MONITOR", gap + 4 + 2 * dr + hh * 0.3, hh / 2 - 3.5 * p, p, FG)

        bpx = int(clamp(hh * 0.3, 9 * S, 13 * S))
        bw = tkfont.Font(family=self.ui, size=-bpx).measure("On top") + 22 * S
        bh = hh * 0.56
        bx1, by0 = W - gap - 2, hh / 2 - bh / 2
        self.rounded(bx1 - bw, by0, bx1, by0 + bh, bh / 2, fill=FG if self.top else BG,
                     outline=FG if self.top else "#3a3a3a", tags="pin")
        cv.create_text(bx1 - bw / 2, hh / 2, text="On top", fill=BG if self.top else DIM,
                       font=(self.ui, -bpx), tags="pin")
        status = self.flash[0] if time.monotonic() < self.flash[1] else self.state
        if self.notice and status == "Live":
            status += "  ·  " + self.notice
        cv.create_text(bx1 - bw - 14 * S, hh / 2, text=status, anchor="e", fill=DIM,
                       font=(self.ui, -bpx))

        vis = self.visible_tiles()
        if not vis:
            if self.state == "Waiting for GPU-Z":
                self.message(hh, "Waiting for GPU-Z", "Start GPU-Z and leave it running. It can stay minimized.")
            else:
                self.message(hh, "No known sensors found",
                             "Press D to save a sensor dump, then open an issue with that file.")
            return

        # grid: choose the column count whose tiles are closest to a 1.4:1 shape
        n = len(vis)
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
        for i, spec in enumerate(vis):
            r, c = divmod(i, cols)
            count = min(cols, n - r * cols)         # last row stretches to fill
            tw = (aw - gap * (count + 1)) / count
            x0 = gap + c * (tw + gap)
            y0 = top + gap + r * (th + gap)
            self.tile(x0, y0, x0 + tw, y0 + th, spec)

    # ---- tiles
    def reading(self, spec):
        """-> (dot text, unit, color, meter fraction, description)"""
        text, unit, color, frac, desc = "-", "", FAINT, None, ""
        rec = self.find(spec["sensors"])
        if rec and rec[1] is not None:
            unit, val = rec
            unit = unit.replace("%%", "%")
            if spec["kind"] == "perfcap":
                text, color, unit, desc = self.perfcap(int(val))
            else:
                mb = None
                if spec["kind"] == "memory":
                    u = unit.upper()
                    if u == "MB":
                        mb, val, unit = val, val / 1024, "GB"
                    elif u == "GB":
                        mb = val * 1024
                text, color = f"{val:.{spec['decimals']}f}", FG
                if spec["bands"]:
                    color = next((c for lim, c in self.bands if val < lim), self.bands[-1][1])
                m, label = spec["meter"], spec["label"]
                kind = m.get("type") if m else None
                if kind == "max":
                    frac = val / float(m["max"])
                elif kind == "auto":
                    pk = self.peak[label] = max(self.peak.get(label, float(m.get("floor", 1))), val)
                    frac = val / pk
                elif kind == "ram" and mb and self.ram_mb:
                    frac = mb / self.ram_mb
                elif kind == "vram" and mb:
                    if self.vram_total:
                        frac = mb / self.vram_total
                    else:                       # size unknown: scale to the session peak
                        pk = self.peak[label] = max(self.peak.get(label, 1024.0), mb)
                        frac = mb / pk
        return text, unit, color, frac, desc

    def tile(self, x0, y0, x1, y1, spec):
        S = self.S
        w, h = x1 - x0, y1 - y0
        m = min(w, h)
        pad = clamp(m * 0.1, 7 * S, 20 * S)
        self.rounded(x0, y0, x1, y1, clamp(m * 0.14, 7 * S, 22 * S))
        if "pair" not in spec:
            self.cell(x0, y0, x1, y1, spec, pad)
            return
        a, b = spec["pair"]                     # two readings sharing one card
        xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
        opts = []                               # side by side vs stacked: keep the bigger numbers
        for side in (True, False):
            ra, rb = ((x0, y0, xm, y1), (xm, y0, x1, y1)) if side else \
                     ((x0, y0, x1, ym), (x0, ym, x1, y1))
            pd = pad if side else pad * 0.6
            pa, pb = self.plan(*ra, a, pd, 0.15), self.plan(*rb, b, pd, 0.15)
            opts.append((min(pa[1], pb[1]), side, ra, rb, pd, min(pa[0], pb[0])))
        p, side, ra, rb, pd, lf = max(opts, key=lambda o: (o[0], o[1]))
        if side:
            self.cv.create_line(xm, y0 + pad, xm, y1 - pad, fill=EDGE)
        else:
            self.cv.create_line(x0 + pad, ym, x1 - pad, ym, fill=EDGE)
        self.cell(*ra, a, pd, False, 0.15, lf, p)        # same sizes in both halves
        self.cell(*rb, b, pd, False, 0.15, lf, p)

    def plan(self, x0, y0, x1, y1, spec, pad, lfk):
        """Label font and dot pitch a meter-less cell would use."""
        S = self.S
        text, unit = self.reading(spec)[:2]
        w, h = x1 - x0, y1 - y0
        lf = int(clamp(h * lfk, 9 * S, 16 * S))
        while lf > 8 and tkfont.Font(family=self.ui, size=-lf).measure(
                spec["label"] + "   " + unit) > w - 2 * pad:
            lf -= 1
        rh = max(h - 2 * pad - lf * 1.5, 8)
        return lf, max(int(min((w - 2 * pad) / dot_cols(text), rh / 7)), 2)

    def cell(self, x0, y0, x1, y1, spec, pad, meter_on=True, lfk=0.085, lf_force=None, p_force=None):
        cv, S = self.cv, self.S
        label = spec["label"]
        text, unit, color, frac, desc = self.reading(spec)
        w, h = x1 - x0, y1 - y0
        m = min(w, h)

        # label and unit, shrunk so they never collide
        lf = int(clamp(h * lfk, 9 * S, 16 * S))
        fit = lambda f: tkfont.Font(family=self.ui, size=-f).measure(label + "   " + unit)
        while lf > 8 and fit(lf) > w - 2 * pad:
            lf -= 1
        lf = lf_force or lf
        cv.create_text(x0 + pad, y0 + pad, text=label, anchor="nw", fill=DIM, font=(self.ui, -lf))
        if unit and fit(lf) <= w - 2 * pad:
            cv.create_text(x1 - pad, y0 + pad, text=unit, anchor="ne", fill=DIM, font=(self.ui, -lf))

        # meter along the bottom
        rw = w - 2 * pad
        bottom = y1 - pad
        if spec["meter"] and meter_on:
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
            cv.create_text(x0 + pad, y1 - pad, text=desc, anchor="sw", fill=DIM, font=(self.ui, -df))
            bottom = y1 - pad - df * 1.7

        # dot-matrix number, as large as the free area allows
        ntop = y0 + pad + lf * 1.5
        rh = max(bottom - ntop, 8)
        p = p_force or max(int(min(rw / dot_cols(text), rh / 7)), 2)
        self.dots(text, x0 + pad, ntop + (rh - 7 * p) / 2, p, color)

    @staticmethod
    def perfcap(v):
        """-> (dot text, color, code label, description). Only heat is shown in red."""
        code = f"code {v}"
        if v == 0:
            return "NONE", "#4a4a4a", code, "No limit active"
        found = [PERFCAP_BITS.get(b, ("?", "Unknown flag")) for b in range(16) if v >> b & 1]
        active = [f for f in found if f[0] != "IDLE"]
        if not active:
            return "IDLE", DIM, code, "GPU idle"
        color = RED if any(f[0] == "THRM" for f in active) else FG
        return "+".join(f[0] for f in active), color, code, ", ".join(f[1] for f in active)


if __name__ == "__main__":
    App().mainloop()
