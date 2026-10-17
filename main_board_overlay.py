"""Native transparent overlay for legal move hints on the live board."""
from __future__ import annotations


class MainBoardHintOverlay:
    """A separate Windows top-level window aligned to a browser-board rectangle."""

    KEY_COLOR = "#010203"

    def __init__(self, parent):
        import tkinter as tk

        self.tk = tk
        self.alive = True
        self.supported = False
        self.enabled = True
        self.rect = None
        self.hint = None
        self.bottom_side = "white"
        self.color = "#facc15"
        self.step = 8
        self.job = None

        self.window = tk.Toplevel(parent)
        self.window.title("Main Board Hint Overlay")
        self.window.overrideredirect(True)
        self.window.configure(background=self.KEY_COLOR)
        self.window.attributes("-topmost", True)
        self.window.geometry("1x1+0+0")
        try:
            self.window.wm_attributes("-transparentcolor", self.KEY_COLOR)
            self.supported = True
        except tk.TclError:
            self.supported = False
        self.canvas = tk.Canvas(
            self.window, background=self.KEY_COLOR, highlightthickness=0, bd=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.window.withdraw()

    def _make_click_through(self):
        if not self.supported:
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = wintypes.HWND(self.window.winfo_id())
            get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_style.argtypes = [wintypes.HWND, ctypes.c_int]
            get_style.restype = ctypes.c_ssize_t
            set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            set_style.restype = ctypes.c_ssize_t
            style = get_style(hwnd, -20)  # GWL_EXSTYLE
            style |= 0x00000020  # WS_EX_TRANSPARENT: pass mouse clicks through
            style |= 0x00080000  # WS_EX_LAYERED: color-key transparency
            style |= 0x00000080  # WS_EX_TOOLWINDOW: omit taskbar button
            style |= 0x08000000  # WS_EX_NOACTIVATE: keep browser focus
            set_style(hwnd, -20, style)

            # Explicitly tell Windows which exact RGB color is transparent.
            # Tk can accept -transparentcolor without reliably applying the
            # native color key on every Windows/Tk build; without this call,
            # the full overlay rectangle can appear black over the board.
            set_layered = user32.SetLayeredWindowAttributes
            set_layered.argtypes = [
                wintypes.HWND, ctypes.c_ulong, ctypes.c_ubyte, ctypes.c_ulong
            ]
            set_layered.restype = wintypes.BOOL
            color_key = 0x00030201  # COLORREF for #010203 (RGB byte order)
            if not set_layered(hwnd, color_key, 0, 0x00000001):  # LWA_COLORKEY
                raise OSError("Could not enable the overlay transparency color key")

            user32.SetWindowPos(
                hwnd, wintypes.HWND(-1), 0, 0, 0, 0,
                0x0002 | 0x0001 | 0x0010 | 0x0040,
            )
        except Exception:
            # Do not show a visual overlay that could intercept board input.
            self.supported = False
            try:
                self.window.withdraw()
            except self.tk.TclError:
                pass

    def update_geometry(self, rect, bottom_side="white"):
        if not self.alive:
            return
        self.bottom_side = "black" if str(bottom_side).lower() == "black" else "white"
        if not isinstance(rect, dict):
            self.rect = None
            self._sync()
            return
        try:
            result = {
                "left": int(round(float(rect["left"]))),
                "top": int(round(float(rect["top"]))),
                "width": int(round(float(rect["width"]))),
                "height": int(round(float(rect["height"]))),
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            self.rect = None
            self._sync()
            return
        if (
            result["width"] < 80 or result["height"] < 80
            or abs(result["width"] - result["height"]) > max(12, result["width"] * 0.12)
        ):
            self.rect = None
            self._sync()
            return
        self.rect = result
        self._sync()

    def set_hint(self, hint, bottom_side="white", color="#facc15"):
        self.hint = dict(hint) if isinstance(hint, dict) else None
        self.bottom_side = "black" if str(bottom_side).lower() == "black" else "white"
        self.color = color
        self.step = 0
        self._cancel_animation()
        self._sync()

    def clear(self):
        self.hint = None
        self._cancel_animation()
        self._sync()

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self._sync()

    def _sync(self):
        if not self.alive:
            return
        if not (self.supported and self.enabled and self.hint and self.rect):
            try:
                self.window.withdraw()
            except self.tk.TclError:
                self.alive = False
            return
        try:
            r = self.rect
            self.window.geometry(f"{r['width']}x{r['height']}+{r['left']}+{r['top']}")
            self.window.deiconify()
            self.window.attributes("-topmost", True)
            self.window.update_idletasks()
            self._make_click_through()
            self._draw()
            if self.step < 8:
                self._schedule_animation()
        except self.tk.TclError:
            self.alive = False

    def _cancel_animation(self):
        if self.job is not None:
            try:
                self.window.after_cancel(self.job)
            except (self.tk.TclError, ValueError):
                pass
            self.job = None

    def _schedule_animation(self):
        if not self.alive or not self.enabled or not self.hint or self.job is not None:
            return
        try:
            self.job = self.window.after(12, self._animate)
        except self.tk.TclError:
            self.alive = False

    def _animate(self):
        self.job = None
        if not self.alive or not self.enabled or not self.hint:
            return
        self.step = min(8, self.step + 1)
        self._draw()
        if self.step < 8:
            self._schedule_animation()

    def _draw(self):
        if not self.alive or not self.supported or not self.hint or not self.rect:
            return
        try:
            self.canvas.delete("all")
            width = max(1, self.rect["width"])
            height = max(1, self.rect["height"])
            cw, ch = width / 8.0, height / 8.0

            def center(square):
                col, row = int(square) % 8, 7 - int(square) // 8
                if self.bottom_side == "black":
                    col, row = 7 - col, 7 - row
                return (col + 0.5) * cw, (row + 0.5) * ch, col, row

            start = center(self.hint["from_square"])
            target = center(self.hint["to_square"])
            for point in (start, target):
                col, row = point[2], point[3]
                self.canvas.create_rectangle(
                    col * cw + 2, row * ch + 2,
                    (col + 1) * cw - 2, (row + 1) * ch - 2,
                    outline=self.color, width=max(3, int(min(cw, ch) * 0.055)),
                )
            progress = self.step / 8.0
            end = (
                start[0] + (target[0] - start[0]) * progress,
                start[1] + (target[1] - start[1]) * progress,
            )
            line_width = max(5, int(min(cw, ch) * 0.10))
            arrow_shape = (
                max(12, int(cw * 0.30)),
                max(14, int(ch * 0.32)),
                max(5, int(cw * 0.12)),
            )
            self.canvas.create_line(
                start[0], start[1], end[0], end[1],
                fill="#111827", width=line_width + 3, capstyle="round",
            )
            self.canvas.create_line(
                start[0], start[1], end[0], end[1],
                fill=self.color, width=line_width, capstyle="round",
                arrow="last" if progress >= 1.0 else "none",
                arrowshape=arrow_shape,
            )
        except self.tk.TclError:
            self.alive = False

    def shutdown(self):
        if not self.alive:
            return
        self.alive = False
        self._cancel_animation()
        try:
            self.window.destroy()
        except self.tk.TclError:
            pass
