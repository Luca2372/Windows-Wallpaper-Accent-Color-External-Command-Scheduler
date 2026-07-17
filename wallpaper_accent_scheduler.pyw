"""Wallpaper & Accent Slider for Windows.

A small Windows desktop utility that changes the wallpaper and Windows accent
colour together. Wallpapers can be assigned to day and night schedules and may
optionally run a user-defined command after each change.

The application itself is not Lenovo-specific. Commands such as
``llt rgb set <profile>`` are optional integrations supplied by the user.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import shutil
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk
except ImportError:  # Pillow is optional; automatic colour extraction is disabled.
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]

try:
    import ctypes
    import ctypes.wintypes as wintypes
    import winreg
except Exception:  # Allows importing the module on non-Windows systems.
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]
    winreg = None  # type: ignore[assignment]


APP_NAME = "Windows Wallpaper & Accent Slider"
APP_SLUG = "WallpaperAccentScheduler"
CONFIG_VERSION = 3
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_SLUG
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_path() -> Path:
    return app_data_dir() / "last_config.json"


def instance_lock_path() -> Path:
    return app_data_dir() / "instance.lock"


def ipc_state_path() -> Path:
    return app_data_dir() / "ipc.json"


def preview_cache_dir() -> Path:
    """Return the cache used by the Windows thumbnail fallback."""
    path = app_data_dir() / "preview_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def subprocess_background_kwargs() -> dict:
    """Return Windows subprocess flags that suppress a console window."""
    if os.name != "nt":
        return {}
    try:
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"startupinfo": startup_info, "creationflags": flags}
    except Exception:
        return {}


def normalize_hex(value: str) -> str:
    value = (value or "").strip().removeprefix("#")
    if len(value) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in value):
        raise ValueError("Use a six-digit colour such as #4f8cff.")
    return f"#{value.lower()}"


def parse_time(value: str) -> Optional[dtime]:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except (TypeError, ValueError):
        return None


def in_time_range(now: dtime, start: dtime, end: dtime) -> bool:
    """Return whether *now* is inside a range, including ranges across midnight."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end



def windows_uses_dark_apps() -> bool:
    """Return the current Windows app-theme preference."""
    if os.name != "nt" or winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        return False


def apply_windows_title_bar_theme(root: tk.Tk, dark: bool) -> None:
    """Apply the matching Windows title-bar theme when supported."""
    if os.name != "nt" or ctypes is None:
        return
    try:
        root.update_idletasks()
        hwnd = root.winfo_id()
        enabled = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        for attribute in (20, 19):
            result = dwm.DwmSetWindowAttribute(
                hwnd,
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                break
    except Exception:
        pass


def path_key(value: str | Path) -> str:
    """Return a stable, case-insensitive key for comparing Windows paths."""
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def get_current_wallpaper() -> Optional[str]:
    """Read the wallpaper currently reported by Windows."""
    if os.name != "nt" or ctypes is None:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        success = ctypes.windll.user32.SystemParametersInfoW(
            0x0073,  # SPI_GETDESKWALLPAPER
            len(buffer),
            buffer,
            0,
        )
        if success and buffer.value:
            return buffer.value
    except Exception:
        pass
    return None


@dataclass
class ImageEntry:
    path: str
    colour: str = "#ffffff"
    enabled: bool = True
    in_day: bool = True
    in_night: bool = True
    command: str = ""

    def clone(self) -> "ImageEntry":
        return ImageEntry(
            path=self.path,
            colour=self.colour,
            enabled=self.enabled,
            in_day=self.in_day,
            in_night=self.in_night,
            command=self.command,
        )

    def compute_average_colour(self) -> None:
        if Image is None:
            return
        try:
            with Image.open(self.path) as image:
                image = image.convert("RGB")
                image.thumbnail((64, 64))
                pixels = list(image.getdata())
                if not pixels:
                    return
                red = sum(pixel[0] for pixel in pixels) // len(pixels)
                green = sum(pixel[1] for pixel in pixels) // len(pixels)
                blue = sum(pixel[2] for pixel in pixels) // len(pixels)
                self.colour = f"#{red:02x}{green:02x}{blue:02x}"
        except Exception:
            pass


def set_wallpaper(image_path: str) -> None:
    """Set the Windows desktop wallpaper using SystemParametersInfoW."""
    if os.name != "nt" or ctypes is None:
        return
    absolute_path = str(Path(image_path).resolve())
    success = ctypes.windll.user32.SystemParametersInfoW(
        20,  # SPI_SETDESKWALLPAPER
        0,
        absolute_path,
        0x01 | 0x02,  # SPIF_UPDATEINIFILE | SPIF_SENDWININICHANGE
    )
    if not success:
        raise OSError("Windows could not change the wallpaper.")


def set_accent_colour(colour: str) -> None:
    """Set the Windows accent colour through per-user registry values.

    Microsoft does not provide a stable public API for setting every Windows
    accent surface. These registry values are therefore best-effort and may
    behave differently between Windows releases.
    """
    if os.name != "nt" or ctypes is None or winreg is None:
        return

    colour = normalize_hex(colour)
    rgb = int(colour[1:], 16)
    red = (rgb >> 16) & 0xFF
    green = (rgb >> 8) & 0xFF
    blue = rgb & 0xFF

    accent_abgr = (0xFF << 24) | (blue << 16) | (green << 8) | red
    colorization_abgr = (0xC4 << 24) | (blue << 16) | (green << 8) | red
    explorer_colour = (blue << 16) | (green << 8) | red
    # Preserve the palette byte order used by the original app.
    palette = bytes([red, green, blue, 0xFF] * 8)

    targets = [
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent",
            {
                "AccentColor": explorer_colour,
                "AccentColorMenu": explorer_colour,
                "StartColorMenu": explorer_colour,
                "AccentPalette": palette,
            },
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\DWM",
            {
                "AccentColor": accent_abgr,
                "AccentColorInactive": accent_abgr,
                "ColorizationColor": colorization_abgr,
                "ColorizationAfterglow": colorization_abgr,
                "AutoColorization": 0,
                "ColorPrevalence": 1,
                "AccentPalette": palette,
            },
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            {"AutoColorization": 0, "ColorPrevalence": 1},
        ),
    ]

    for root, key_path, values in targets:
        with winreg.CreateKeyEx(root, key_path, 0, winreg.KEY_SET_VALUE) as key:
            for name, value in values.items():
                value_type = winreg.REG_BINARY if isinstance(value, bytes) else winreg.REG_DWORD
                winreg.SetValueEx(key, name, 0, value_type, value)

    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF,  # HWND_BROADCAST
        0x001A,  # WM_SETTINGCHANGE
        0,
        ctypes.c_wchar_p("ImmersiveColorSet"),
        0x0002,  # SMTO_ABORTIFHUNG
        2000,
        ctypes.byref(result),
    )

    try:
        subprocess.Popen(
            ["rundll32.exe", "user32.dll,UpdatePerUserSystemParameters"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **subprocess_background_kwargs(),
        )
    except Exception:
        pass


def run_external_command(command: str) -> None:
    """Run a user-supplied command without opening a console window."""
    if not command.strip():
        return
    subprocess.Popen(
        command,
        shell=False,
        cwd=str(Path.home()),
        **subprocess_background_kwargs(),
    )


class SingleInstanceLock:
    """Per-user process lock kept open for the lifetime of the primary instance."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.file = open(self.path, "r+b")
        except FileNotFoundError:
            self.file = open(self.path, "w+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            self.file.close()
            self.file = None
            return False

    def release(self) -> None:
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.file.close()
        finally:
            self.file = None


class IPCServer(threading.Thread):
    """Small localhost server used to control the already-running instance."""

    def __init__(self, event_queue: queue.Queue[tuple[str, object]]) -> None:
        super().__init__(daemon=True, name="IPCServer")
        self.event_queue = event_queue
        self.stop_event = threading.Event()
        self.server_socket: Optional[socket.socket] = None
        seed = f"{APP_SLUG}|{app_data_dir()}"
        self.token = hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def start_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(0.5)
        self.server_socket = server
        state = {"port": server.getsockname()[1], "token": self.token, "pid": os.getpid()}
        ipc_state_path().write_text(json.dumps(state), encoding="utf-8")
        self.start()

    def run(self) -> None:
        assert self.server_socket is not None
        while not self.stop_event.is_set():
            try:
                connection, _ = self.server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(1.0)
                try:
                    raw = connection.recv(4096)
                    payload = json.loads(raw.decode("utf-8"))
                    if payload.get("token") != self.token:
                        connection.sendall(b'{"ok": false}')
                        continue
                    command = str(payload.get("command", "")).upper()
                    if command in {"SHOW", "START", "STOP", "CHANGE_NOW", "TOGGLE_COMMANDS", "EXIT"}:
                        self.event_queue.put((command, None))
                        connection.sendall(b'{"ok": true}')
                    else:
                        connection.sendall(b'{"ok": false}')
                except Exception:
                    try:
                        connection.sendall(b'{"ok": false}')
                    except Exception:
                        pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except Exception:
                pass
        if self.is_alive():
            self.join(timeout=1.5)
        try:
            ipc_state_path().unlink(missing_ok=True)
        except Exception:
            pass


def send_to_existing_instance(command: str) -> bool:
    """Send a command to the primary instance, retrying briefly during startup."""
    for _ in range(12):
        try:
            state = json.loads(ipc_state_path().read_text(encoding="utf-8"))
            port = int(state["port"])
            token = str(state["token"])
            payload = json.dumps({"token": token, "command": command}).encode("utf-8")
            with socket.create_connection(("127.0.0.1", port), timeout=0.4) as client:
                client.sendall(payload)
                reply = client.recv(512)
                return bool(json.loads(reply.decode("utf-8")).get("ok"))
        except Exception:
            threading.Event().wait(0.08)
    return False


HOTKEY_KEYS = {
    "SPACE": 0x20,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
}
HOTKEY_MODIFIERS = {
    "ALT": 0x0001,
    "CTRL": 0x0002,
    "CONTROL": 0x0002,
    "SHIFT": 0x0004,
    "WIN": 0x0008,
    "WINDOWS": 0x0008,
}


def parse_hotkey(value: str) -> tuple[int, int]:
    """Parse strings such as ``Ctrl+Alt+W`` for RegisterHotKey."""
    tokens = [token.strip().upper() for token in value.split("+") if token.strip()]
    if len(tokens) < 2:
        raise ValueError("A shortcut must include a modifier and a key.")

    modifiers = 0x4000  # MOD_NOREPEAT
    key_token: Optional[str] = None
    for token in tokens:
        if token in HOTKEY_MODIFIERS:
            modifiers |= HOTKEY_MODIFIERS[token]
        elif key_token is None:
            key_token = token
        else:
            raise ValueError("A shortcut can contain only one non-modifier key.")

    if modifiers == 0x4000 or key_token is None:
        raise ValueError("A shortcut must include Ctrl, Alt, Shift or Win.")

    if len(key_token) == 1 and (key_token.isalpha() or key_token.isdigit()):
        virtual_key = ord(key_token)
    elif key_token.startswith("F") and key_token[1:].isdigit() and 1 <= int(key_token[1:]) <= 24:
        virtual_key = 0x70 + int(key_token[1:]) - 1
    elif key_token in HOTKEY_KEYS:
        virtual_key = HOTKEY_KEYS[key_token]
    else:
        raise ValueError(f"Unsupported key: {key_token}")
    return modifiers, virtual_key


class HotkeyManager(threading.Thread):
    """Register global Windows hotkeys in a dedicated message-loop thread."""

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012

    def __init__(
        self,
        shortcuts: dict[int, tuple[str, str]],
        event_queue: queue.Queue[tuple[str, object]],
    ) -> None:
        super().__init__(daemon=True, name="HotkeyManager")
        self.shortcuts = shortcuts
        self.event_queue = event_queue
        self.thread_id: Optional[int] = None
        self.ready = threading.Event()
        self.registration_errors: list[str] = []

    def run(self) -> None:
        if os.name != "nt" or ctypes is None or wintypes is None:
            self.ready.set()
            return

        class Point(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class Message(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", Point),
                ("lPrivate", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = int(kernel32.GetCurrentThreadId())

        registered_ids: list[int] = []
        for hotkey_id, (shortcut, _action) in self.shortcuts.items():
            if not shortcut.strip():
                continue
            try:
                modifiers, virtual_key = parse_hotkey(shortcut)
            except ValueError as error:
                self.registration_errors.append(f"{shortcut}: {error}")
                continue
            if user32.RegisterHotKey(None, hotkey_id, modifiers, virtual_key):
                registered_ids.append(hotkey_id)
            else:
                error_code = int(kernel32.GetLastError())
                self.registration_errors.append(
                    f"{shortcut}: unavailable or already used (Windows error {error_code})."
                )

        # Ensure this thread owns a Windows message queue before another
        # thread tries to stop it with PostThreadMessageW.
        message = Message()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self.ready.set()
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == self.WM_HOTKEY:
                    hotkey_id = int(message.wParam)
                    item = self.shortcuts.get(hotkey_id)
                    if item:
                        self.event_queue.put((item[1], None))
        finally:
            for hotkey_id in registered_ids:
                user32.UnregisterHotKey(None, hotkey_id)

    def stop(self) -> None:
        if os.name == "nt" and ctypes is not None and self.thread_id is not None:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self.thread_id, self.WM_QUIT, 0, 0
                )
            except Exception:
                pass
        if self.is_alive():
            self.join(timeout=1.5)


class Scheduler(threading.Thread):
    """Background wallpaper rotation worker."""

    def __init__(self, app: "WallpaperApp") -> None:
        super().__init__(daemon=True, name="WallpaperScheduler")
        self.app = app
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            image = self.app.select_next_image()
            if image is not None:
                self.app.apply_image(image, source="schedule")

            try:
                minutes = float(self.app.interval_minutes_str)
            except (TypeError, ValueError):
                minutes = 15.0
            seconds = max(1.0, minutes * 60.0)
            self.stop_event.wait(seconds)


class NotificationOverlay:
    """Bottom-centre on-screen notification similar to a compact Windows OSD."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.window: Optional[tk.Toplevel] = None
        self.hide_job: Optional[str] = None

    def show(self, title: str, detail: str = "", duration_ms: int = 2200) -> None:
        if self.window is None or not self.window.winfo_exists():
            window = tk.Toplevel(self.root)
            window.withdraw()
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            try:
                window.attributes("-alpha", 0.95)
            except tk.TclError:
                pass
            outer = tk.Frame(window, bg="#17191c", bd=1, relief="solid")
            outer.pack(fill="both", expand=True)
            self.title_label = tk.Label(
                outer,
                bg="#17191c",
                fg="#ffffff",
                font=("Segoe UI Semibold", 11),
                anchor="w",
            )
            self.title_label.pack(fill="x", padx=18, pady=(13, 2))
            self.detail_label = tk.Label(
                outer,
                bg="#17191c",
                fg="#b8bdc7",
                font=("Segoe UI", 9),
                anchor="w",
            )
            self.detail_label.pack(fill="x", padx=18, pady=(0, 13))
            self.window = window

        assert self.window is not None
        self.title_label.configure(text=title)
        self.detail_label.configure(text=detail)
        self.window.update_idletasks()

        width = max(360, min(560, self.window.winfo_reqwidth()))
        height = max(72, self.window.winfo_reqheight())
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = screen_height - height - 76
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.deiconify()
        self.window.lift()

        if self.hide_job is not None:
            try:
                self.root.after_cancel(self.hide_job)
            except Exception:
                pass
        self.hide_job = self.root.after(duration_ms, self.hide)

    def hide(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.withdraw()
        self.hide_job = None


class WallpaperApp:
    def __init__(self, root: tk.Tk, silent: bool = False) -> None:
        self.root = root
        self.silent = silent
        self.root.title(APP_NAME)
        self.root.geometry("1180x820")
        self.root.minsize(980, 680)
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

        self.state_lock = threading.RLock()
        self.change_lock = threading.Lock()
        self.images: list[ImageEntry] = []
        self.scheduler: Optional[Scheduler] = None
        self.hotkey_manager: Optional[HotkeyManager] = None
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.notification = NotificationOverlay(root)
        self.exiting = False
        self.loading_config = False
        self.current_config_path: Optional[Path] = None

        self.day_index = 0
        self.night_index = 0
        self.active_image_path: Optional[str] = None
        self.active_markers: dict[str, ttk.Label] = {}
        self.preview_references: list[object] = []
        self.tab_scroll_canvases: list[tk.Canvas] = []
        self.themes_scroll_canvas: Optional[tk.Canvas] = None
        self.automation_scroll_canvas: Optional[tk.Canvas] = None

        self.folder = tk.StringVar()
        self.day_start = tk.StringVar(value="08:00")
        self.day_end = tk.StringVar(value="20:00")
        self.night_start = tk.StringVar(value="20:00")
        self.night_end = tk.StringVar(value="08:00")
        self.interval = tk.StringVar(value="60")
        self.random_order = tk.BooleanVar(value=False)
        self.autostart = tk.BooleanVar(value=False)
        self.commands_enabled = tk.BooleanVar(value=True)
        self.notifications_enabled = tk.BooleanVar(value=True)
        self.hotkey_change = tk.StringVar(value="Ctrl+Alt+W")
        self.hotkey_commands = tk.StringVar(value="Ctrl+Alt+C")
        self.scheduler_enabled = False

        self.day_start_str = self.day_start.get()
        self.day_end_str = self.day_end.get()
        self.night_start_str = self.night_start.get()
        self.night_end_str = self.night_end.get()
        self.interval_minutes_str = self.interval.get()
        self.random_order_bool = self.random_order.get()
        self.commands_enabled_bool = self.commands_enabled.get()

        self.dark_mode = windows_uses_dark_apps()
        self.colours = self.make_colour_scheme(self.dark_mode)
        self.configure_style()
        self.build_ui()
        self.bind_variable_mirrors()
        apply_windows_title_bar_theme(self.root, self.dark_mode)
        self.root.after(80, self.process_events)

        try:
            self.autostart.set(self.get_autostart())
        except Exception:
            pass

    @staticmethod
    def make_colour_scheme(dark: bool) -> dict[str, str]:
        if dark:
            return {
                "background": "#14171a",
                "panel": "#1b1f23",
                "panel_alt": "#20252a",
                "foreground": "#f1f3f5",
                "muted": "#a6adb5",
                "border": "#343a40",
                "entry": "#252a2f",
                "button": "#262c31",
                "button_active": "#30373d",
                "success": "#38c878",
                "error": "#ff7070",
            }
        return {
            "background": "#f4f6f8",
            "panel": "#ffffff",
            "panel_alt": "#f7f9fb",
            "foreground": "#18202a",
            "muted": "#5d6875",
            "border": "#d7dde3",
            "entry": "#ffffff",
            "button": "#f7f9fb",
            "button_active": "#e8edf2",
            "success": "#1d9b5f",
            "error": "#b42318",
        }

    def configure_style(self) -> None:
        c = self.colours
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".",
            background=c["background"],
            foreground=c["foreground"],
            fieldbackground=c["entry"],
            bordercolor=c["border"],
            lightcolor=c["border"],
            darkcolor=c["border"],
            troughcolor=c["panel_alt"],
            arrowcolor=c["foreground"],
            font=("Segoe UI", 9),
        )
        style.configure("TFrame", background=c["background"])
        style.configure("Panel.TFrame", background=c["panel"])
        style.configure("TLabel", background=c["background"], foreground=c["foreground"])
        style.configure("Panel.TLabel", background=c["panel"], foreground=c["foreground"])
        style.configure(
            "Header.TLabel",
            background=c["background"],
            foreground=c["foreground"],
            font=("Segoe UI Semibold", 20),
        )
        style.configure(
            "Subtitle.TLabel",
            background=c["background"],
            foreground=c["muted"],
            font=("Segoe UI", 10),
        )
        style.configure(
            "PanelSubtitle.TLabel",
            background=c["panel"],
            foreground=c["muted"],
            font=("Segoe UI", 9),
        )
        style.configure(
            "Section.TLabelframe",
            background=c["background"],
            foreground=c["foreground"],
            bordercolor=c["border"],
            padding=12,
        )
        style.configure(
            "Section.TLabelframe.Label",
            background=c["background"],
            foreground=c["foreground"],
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "TButton",
            background=c["button"],
            foreground=c["foreground"],
            bordercolor=c["border"],
            padding=(10, 6),
        )
        style.map(
            "TButton",
            background=[("active", c["button_active"]), ("pressed", c["button_active"])],
            foreground=[("disabled", c["muted"])],
        )
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 9), padding=(14, 8))
        style.configure(
            "TEntry",
            fieldbackground=c["entry"],
            foreground=c["foreground"],
            insertcolor=c["foreground"],
            bordercolor=c["border"],
        )
        style.map("TEntry", fieldbackground=[("disabled", c["panel_alt"])])
        style.configure(
            "TCheckbutton",
            background=c["background"],
            foreground=c["foreground"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", c["background"])],
            foreground=[("disabled", c["muted"])],
        )
        style.configure(
            "Row.TCheckbutton",
            background=c["panel"],
            foreground=c["foreground"],
        )
        style.map("Row.TCheckbutton", background=[("active", c["panel"])])
        style.configure("Status.TLabel", font=("Segoe UI Semibold", 9))
        style.configure("PanelStatus.TLabel", background=c["panel"], font=("Segoe UI Semibold", 9))
        style.configure("TNotebook", background=c["background"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=c["button"],
            foreground=c["foreground"],
            padding=(16, 8),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", c["panel"]), ("active", c["button_active"])],
        )
        style.configure(
            "Vertical.TScrollbar",
            background=c["button"],
            troughcolor=c["panel"],
            bordercolor=c["border"],
            arrowcolor=c["foreground"],
        )
        self.root.configure(bg=c["background"])

    def build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=18)
        main.pack(fill="both", expand=True)

        # Global controls stay fixed below the tabs. Only the tab contents scroll.
        action_bar = ttk.Frame(main)
        action_bar.pack(side="bottom", fill="x", pady=(12, 0))
        self.status_label = ttk.Label(action_bar, text="Stopped", style="Status.TLabel")
        self.status_label.pack(side="left")
        ttk.Button(action_bar, text="Exit", command=self.exit_application).pack(side="right")
        ttk.Button(action_bar, text="Change now", command=self.change_now).pack(side="right", padx=(8, 0))
        ttk.Button(action_bar, text="Stop", command=self.stop_scheduler).pack(side="right", padx=(8, 0))
        ttk.Button(action_bar, text="Start", style="Primary.TButton", command=self.start_scheduler).pack(
            side="right", padx=(8, 0)
        )
        ttk.Separator(main, orient="horizontal").pack(side="bottom", fill="x")

        notebook = ttk.Notebook(main)
        notebook.pack(side="top", fill="both", expand=True)

        themes_tab = ttk.Frame(notebook)
        automation_tab = ttk.Frame(notebook)
        notebook.add(themes_tab, text="Themes")
        notebook.add(automation_tab, text="Automation")

        themes_content, self.themes_scroll_canvas = self.create_scrollable_tab(themes_tab)
        automation_content, self.automation_scroll_canvas = self.create_scrollable_tab(automation_tab)
        self.build_themes_tab(themes_content)
        self.build_automation_tab(automation_content)

        # One wheel handler manages both tab scrolling and the nested wallpaper list.
        self.root.bind_all("<MouseWheel>", self.on_global_mousewheel, add="+")

    def create_scrollable_tab(self, parent: ttk.Frame) -> tuple[ttk.Frame, tk.Canvas]:
        """Create a vertically scrollable tab while keeping its width responsive."""
        shell = ttk.Frame(parent)
        shell.pack(fill="both", expand=True)

        canvas = tk.Canvas(
            shell,
            highlightthickness=0,
            bg=self.colours["background"],
            bd=0,
            yscrollincrement=24,
        )
        scrollbar = ttk.Scrollbar(
            shell,
            orient="vertical",
            command=canvas.yview,
            style="Vertical.TScrollbar",
        )
        content = ttk.Frame(canvas, padding=14)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content.bind(
            "<Configure>",
            lambda _event, c=canvas: c.configure(scrollregion=c.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event, c=canvas, item=window_id: c.itemconfigure(item, width=event.width),
        )
        self.tab_scroll_canvases.append(canvas)
        return content, canvas

    @staticmethod
    def widget_is_inside(widget: Optional[tk.Misc], ancestor: tk.Misc) -> bool:
        """Return whether *widget* is the ancestor itself or one of its descendants."""
        current = widget
        while current is not None:
            if current == ancestor:
                return True
            try:
                parent_name = current.winfo_parent()
                if not parent_name:
                    break
                current = current._nametowidget(parent_name)
            except (KeyError, tk.TclError):
                break
        return False

    @staticmethod
    def canvas_can_scroll(canvas: tk.Canvas, units: int) -> bool:
        try:
            first, last = canvas.yview()
        except tk.TclError:
            return False
        if last - first >= 0.999:
            return False
        return first > 0.001 if units < 0 else last < 0.999

    def on_global_mousewheel(self, event) -> Optional[str]:
        """Scroll the list under the pointer, then bubble to its containing tab at an edge."""
        if not event.delta:
            return None
        units = int(-event.delta / 120)
        if units == 0:
            units = -1 if event.delta > 0 else 1

        try:
            pointed_widget = self.root.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            pointed_widget = None

        if (
            hasattr(self, "wallpaper_canvas")
            and self.widget_is_inside(pointed_widget, self.wallpaper_canvas)
            and self.canvas_can_scroll(self.wallpaper_canvas, units)
        ):
            self.wallpaper_canvas.yview_scroll(units, "units")
            return "break"

        for canvas in self.tab_scroll_canvases:
            if self.widget_is_inside(pointed_widget, canvas) and self.canvas_can_scroll(canvas, units):
                canvas.yview_scroll(units, "units")
                return "break"
        return None

    def build_themes_tab(self, parent: ttk.Frame) -> None:
        folder_box = ttk.LabelFrame(parent, text="Wallpaper folder", style="Section.TLabelframe")
        folder_box.pack(fill="x", pady=(0, 12))
        folder_box.columnconfigure(0, weight=1)
        ttk.Entry(folder_box, textvariable=self.folder).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(folder_box, text="Browse", command=self.browse_folder).grid(row=0, column=1)

        ranges = ttk.LabelFrame(parent, text="Time ranges", style="Section.TLabelframe")
        ranges.pack(fill="x", pady=(0, 12))
        for column in range(6):
            ranges.columnconfigure(column, weight=1 if column in {1, 3, 5} else 0)

        ttk.Label(ranges, text="Day starts").grid(row=0, column=0, sticky="w")
        ttk.Entry(ranges, textvariable=self.day_start, width=9).grid(row=0, column=1, sticky="w", padx=(6, 24))
        ttk.Label(ranges, text="Day ends").grid(row=0, column=2, sticky="w")
        ttk.Entry(ranges, textvariable=self.day_end, width=9).grid(row=0, column=3, sticky="w", padx=(6, 24))
        ttk.Label(ranges, text="Change every (minutes)").grid(row=0, column=4, sticky="w")
        ttk.Entry(ranges, textvariable=self.interval, width=10).grid(row=0, column=5, sticky="w", padx=(6, 0))

        ttk.Label(ranges, text="Night starts").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(ranges, textvariable=self.night_start, width=9).grid(
            row=1, column=1, sticky="w", padx=(6, 24), pady=(10, 0)
        )
        ttk.Label(ranges, text="Night ends").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(ranges, textvariable=self.night_end, width=9).grid(
            row=1, column=3, sticky="w", padx=(6, 24), pady=(10, 0)
        )
        ttk.Checkbutton(ranges, text="Random order", variable=self.random_order).grid(
            row=1, column=4, columnspan=2, sticky="w", pady=(10, 0)
        )

        self.build_wallpapers_section(parent)

        config_box = ttk.LabelFrame(parent, text="Configuration", style="Section.TLabelframe")
        config_box.pack(fill="x")
        ttk.Button(config_box, text="Load configuration", command=self.load_config).pack(side="left")
        ttk.Button(config_box, text="Save configuration", command=self.save_config).pack(side="left", padx=(8, 0))
        ttk.Label(
            config_box,
            text="The last configuration is restored automatically.",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=16)

    def build_wallpapers_section(self, parent: ttk.Frame) -> None:
        wallpaper_box = ttk.LabelFrame(parent, text="Wallpapers", style="Section.TLabelframe")
        wallpaper_box.pack(fill="both", expand=True, pady=(0, 12))

        toolbar = ttk.Frame(wallpaper_box)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(
            toolbar,
            text="Only selected wallpapers are used. Commands run after the matching wallpaper is applied.",
            style="Subtitle.TLabel",
        ).pack(side="left")
        ttk.Button(toolbar, text="Reload folder", command=self.reload_folder).pack(side="right")
        ttk.Button(toolbar, text="Add images", command=self.add_images).pack(side="right", padx=(0, 8))

        container = ttk.Frame(wallpaper_box, style="Panel.TFrame")
        container.pack(fill="both", expand=True)

        self.wallpaper_canvas = tk.Canvas(
            container,
            highlightthickness=1,
            highlightbackground=self.colours["border"],
            bg=self.colours["panel"],
            bd=0,
            height=360,
        )
        scrollbar = ttk.Scrollbar(
            container,
            orient="vertical",
            command=self.wallpaper_canvas.yview,
            style="Vertical.TScrollbar",
        )
        self.image_grid = ttk.Frame(self.wallpaper_canvas, style="Panel.TFrame", padding=(8, 4))
        self.image_window_id = self.wallpaper_canvas.create_window(
            (0, 0), window=self.image_grid, anchor="nw"
        )
        self.wallpaper_canvas.configure(yscrollcommand=scrollbar.set)
        self.wallpaper_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.image_grid.bind(
            "<Configure>",
            lambda _event: self.wallpaper_canvas.configure(scrollregion=self.wallpaper_canvas.bbox("all")),
        )
        self.wallpaper_canvas.bind(
            "<Configure>",
            lambda event: self.wallpaper_canvas.itemconfigure(self.image_window_id, width=event.width),
        )
        column_settings = {
            0: (54, 0),
            1: (46, 0),
            2: (80, 0),
            3: (250, 3),
            4: (68, 0),
            5: (100, 0),
            6: (54, 0),
            7: (58, 0),
            8: (320, 4),
        }
        for column, (minimum, weight) in column_settings.items():
            self.image_grid.columnconfigure(column, minsize=minimum, weight=weight)

        headings = [
            ("Active", 0),
            ("Use", 1),
            ("Preview", 2),
            ("File", 3),
            ("Colour", 4),
            ("Hex", 5),
            ("Day", 6),
            ("Night", 7),
            ("External command", 8),
        ]
        for text, column in headings:
            ttk.Label(self.image_grid, text=text, style="PanelStatus.TLabel").grid(
                row=0, column=column, sticky="w", padx=5, pady=(5, 8)
            )
        ttk.Separator(self.image_grid, orient="horizontal").grid(
            row=1, column=0, columnspan=9, sticky="ew"
        )

        self.empty_images_label = ttk.Label(
            self.image_grid,
            text="Choose a folder or add images to begin.",
            style="PanelSubtitle.TLabel",
            padding=18,
        )
        self.empty_images_label.grid(row=2, column=0, columnspan=9, sticky="w")

    def build_automation_tab(self, parent: ttk.Frame) -> None:
        startup_box = ttk.LabelFrame(parent, text="Background operation", style="Section.TLabelframe")
        startup_box.pack(fill="x", pady=(0, 12))
        ttk.Checkbutton(
            startup_box,
            text="Start with Windows",
            variable=self.autostart,
            command=self.on_toggle_autostart,
        ).pack(anchor="w")
        ttk.Label(
            startup_box,
            text="Closing the window hides it while the scheduler is running. Use Exit to terminate the process.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(5, 0))

        command_box = ttk.LabelFrame(parent, text="External commands", style="Section.TLabelframe")
        command_box.pack(fill="x", pady=(0, 12))
        ttk.Checkbutton(
            command_box,
            text="Run external commands when the wallpaper changes",
            variable=self.commands_enabled,
            command=self.on_commands_checkbox,
        ).pack(anchor="w")
        ttk.Label(
            command_box,
            text="Disable this temporarily to keep keyboard lighting off while wallpaper and accent changes continue.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(5, 0))

        shortcut_box = ttk.LabelFrame(parent, text="Global shortcuts", style="Section.TLabelframe")
        shortcut_box.pack(fill="x", pady=(0, 12))
        shortcut_box.columnconfigure(1, weight=1)
        ttk.Label(shortcut_box, text="Change wallpaper").grid(row=0, column=0, sticky="w")
        ttk.Entry(shortcut_box, textvariable=self.hotkey_change).grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Label(shortcut_box, text="Toggle external commands").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(shortcut_box, textvariable=self.hotkey_commands).grid(
            row=1, column=1, sticky="ew", padx=10, pady=(10, 0)
        )
        ttk.Button(shortcut_box, text="Apply shortcuts", command=self.apply_hotkeys).grid(
            row=0, column=2, rowspan=2, sticky="ns"
        )
        ttk.Label(
            shortcut_box,
            text="Format: Ctrl+Alt+W. Supported keys: A-Z, 0-9, F1-F24, arrows, Space, Home, End, PageUp and PageDown.",
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        notification_box = ttk.LabelFrame(parent, text="On-screen display", style="Section.TLabelframe")
        notification_box.pack(fill="x")
        ttk.Checkbutton(
            notification_box,
            text="Show a bottom-centre notification after actions",
            variable=self.notifications_enabled,
            command=self.save_last_config_safely,
        ).pack(anchor="w")

    def bind_variable_mirrors(self) -> None:
        bindings: list[tuple[tk.Variable, str, Callable[[], object]]] = [
            (self.day_start, "day_start_str", self.day_start.get),
            (self.day_end, "day_end_str", self.day_end.get),
            (self.night_start, "night_start_str", self.night_start.get),
            (self.night_end, "night_end_str", self.night_end.get),
            (self.interval, "interval_minutes_str", self.interval.get),
            (self.random_order, "random_order_bool", self.random_order.get),
            (self.commands_enabled, "commands_enabled_bool", self.commands_enabled.get),
        ]
        for variable, attribute, getter in bindings:
            variable.trace_add("write", lambda *_args, a=attribute, g=getter: setattr(self, a, g()))

    def process_events(self) -> None:
        if self.exiting:
            return
        while True:
            try:
                action, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if action == "SHOW":
                self.show_window()
            elif action == "START":
                self.start_scheduler()
            elif action == "STOP":
                self.stop_scheduler()
            elif action == "CHANGE_NOW":
                self.change_now(quiet=True)
            elif action == "TOGGLE_COMMANDS":
                self.toggle_commands()
            elif action == "IMAGE_APPLIED":
                data = payload if isinstance(payload, dict) else {}
                image_path = str(data.get("path", ""))
                if image_path:
                    self.set_active_image(image_path)
                self.show_notification("Wallpaper changed", str(data.get("detail", "")))
            elif action == "NOTIFY_ERROR":
                self.show_notification("Action failed", str(payload))
            elif action == "EXIT":
                self.exit_application()
        if not self.exiting:
            self.root.after(80, self.process_events)

    def show_window(self) -> None:
        self.silent = False
        self.root.deiconify()
        try:
            self.root.state("normal")
        except tk.TclError:
            pass
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(180, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.update_status()

    def on_window_close(self) -> None:
        if self.scheduler and self.scheduler.is_alive():
            self.root.withdraw()
            self.show_notification("Still running", "Use Exit to stop the background process.")
        else:
            self.exit_application()

    def show_notification(self, title: str, detail: str = "") -> None:
        if self.notifications_enabled.get() and not self.exiting:
            self.notification.show(title, detail)

    def has_active_configuration(self) -> bool:
        return bool(self.images or self.folder.get().strip())

    def confirm_folder_change(self) -> bool:
        if not self.has_active_configuration():
            return True
        answer = messagebox.askyesnocancel(
            APP_NAME,
            "Save the current configuration before changing the wallpaper folder?",
            detail="Yes saves it first, No changes folder without creating a configuration file, and Cancel keeps the current folder.",
        )
        if answer is None:
            return False
        if answer:
            return self.save_current_configuration(show_confirmation=False)
        return True

    def browse_folder(self) -> None:
        initial = self.folder.get().strip() or str(Path.home())
        directory = filedialog.askdirectory(title="Select wallpaper folder", initialdir=initial)
        if not directory:
            return
        if self.folder.get().strip() and path_key(directory) == path_key(self.folder.get().strip()):
            return
        if not self.confirm_folder_change():
            return
        self.folder.set(directory)
        self.current_config_path = None
        self.load_images(directory, preserve=False)
        self.save_last_config_safely()

    def reload_folder(self) -> None:
        directory = self.folder.get().strip()
        if not directory:
            messagebox.showwarning(APP_NAME, "Choose a wallpaper folder first.")
            return
        self.load_images(directory, preserve=True)
        self.save_last_config_safely()

    def add_images(self) -> None:
        directory_text = self.folder.get().strip()
        if not directory_text or not Path(directory_text).is_dir():
            directory_text = filedialog.askdirectory(title="Choose the destination wallpaper folder")
            if not directory_text:
                return
            if self.has_active_configuration() and not self.confirm_folder_change():
                return
            self.folder.set(directory_text)
            self.current_config_path = None
            self.load_images(directory_text, preserve=False)

        selected = filedialog.askopenfilenames(
            title="Add images to the wallpaper folder",
            filetypes=[
                ("Supported images", "*.jpg *.jpeg *.png *.bmp *.gif *.webp"),
                ("All files", "*.*"),
            ],
        )
        if not selected:
            return

        destination_folder = Path(directory_text)
        added = 0
        skipped = 0
        for source_text in selected:
            source = Path(source_text)
            if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
                skipped += 1
                continue
            try:
                destination = destination_folder / source.name
                if path_key(source) != path_key(destination):
                    destination = self.unique_destination(destination)
                    shutil.copy2(source, destination)
                added += 1
            except Exception:
                skipped += 1

        self.load_images(str(destination_folder), preserve=True)
        self.save_last_config_safely()
        if skipped:
            messagebox.showwarning(
                APP_NAME,
                f"Added {added} image(s). {skipped} file(s) could not be added.",
            )

    @staticmethod
    def unique_destination(destination: Path) -> Path:
        if not destination.exists():
            return destination
        counter = 2
        while True:
            candidate = destination.with_name(f"{destination.stem} ({counter}){destination.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def load_images(
        self,
        directory: str,
        preserve: bool = True,
        configured_entries: Optional[list[ImageEntry]] = None,
    ) -> None:
        folder_path = Path(directory)
        if not folder_path.is_dir():
            if not self.silent:
                messagebox.showerror(APP_NAME, "The selected wallpaper folder does not exist.")
            return

        source_entries = configured_entries
        if source_entries is None and preserve:
            source_entries = self.get_images_snapshot()
        existing = {path_key(image.path): image for image in (source_entries or [])}

        entries: list[ImageEntry] = []
        for file_path in sorted(folder_path.iterdir(), key=lambda item: item.name.lower()):
            if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            old = existing.get(path_key(file_path))
            if old is not None:
                old.path = str(file_path)
                entries.append(old)
            else:
                entry = ImageEntry(path=str(file_path))
                entry.compute_average_colour()
                entries.append(entry)

        with self.state_lock:
            self.images = entries
            self.day_index = 0
            self.night_index = 0
        if self.active_image_path and path_key(self.active_image_path) not in {path_key(i.path) for i in entries}:
            self.active_image_path = None
        self.rebuild_image_rows()
        self.update_status()

    def rebuild_image_rows(self) -> None:
        for child in self.image_grid.winfo_children():
            info = child.grid_info()
            if info and int(info.get("row", 0)) >= 2:
                child.destroy()
        self.active_markers.clear()
        self.preview_references.clear()

        if not self.images:
            self.empty_images_label = ttk.Label(
                self.image_grid,
                text="No supported images were found.",
                style="PanelSubtitle.TLabel",
                padding=18,
            )
            self.empty_images_label.grid(row=2, column=0, columnspan=9, sticky="w")
            return

        for index, image in enumerate(self.images):
            grid_row = 2 + index * 2
            self.add_image_row(image, grid_row)
            ttk.Separator(self.image_grid, orient="horizontal").grid(
                row=grid_row + 1, column=0, columnspan=9, sticky="ew", pady=(3, 0)
            )
        self.refresh_active_markers()

    def create_preview(self, image_path: str) -> Optional[object]:
        # Prefer Pillow, then native Tk, then a Windows thumbnail fallback.
        if Image is not None and ImageTk is not None:
            try:
                with Image.open(image_path) as source:
                    source.seek(0)
                    preview = source.convert("RGBA")
                    resampling = getattr(Image, "Resampling", Image)
                    preview.thumbnail((72, 44), resampling.LANCZOS)
                    background = Image.new("RGBA", (72, 44), self.colours["panel_alt"])
                    x = (72 - preview.width) // 2
                    y = (44 - preview.height) // 2
                    background.alpha_composite(preview, (x, y))
                    return ImageTk.PhotoImage(
                        background.convert("RGB"),
                        master=self.root,
                    )
            except Exception:
                pass

        # PNG and GIF can often be displayed directly even without Pillow.
        direct = self.create_tk_native_preview(image_path)
        if direct is not None:
            return direct

        # JPEG is common for wallpapers. On Windows, create and cache a PNG
        # thumbnail using the built-in System.Drawing stack when Pillow is absent.
        return self.create_windows_cached_preview(image_path)

    def create_tk_native_preview(self, image_path: str) -> Optional[object]:
        try:
            source = tk.PhotoImage(file=image_path, master=self.root)
            factor = max(
                1,
                (source.width() + 71) // 72,
                (source.height() + 43) // 44,
            )
            return source.subsample(factor, factor) if factor > 1 else source
        except Exception:
            return None

    def create_windows_cached_preview(self, image_path: str) -> Optional[object]:
        if os.name != "nt":
            return None
        try:
            source_path = Path(image_path)
            stat = source_path.stat()
            cache_key = hashlib.sha1(
                f"{path_key(source_path)}|{stat.st_mtime_ns}|{stat.st_size}|72x44|{self.colours['panel_alt']}".encode("utf-8")
            ).hexdigest()
            target_path = preview_cache_dir() / f"{cache_key}.png"

            if not target_path.exists():
                colour = self.colours["panel_alt"].lstrip("#")
                red = int(colour[0:2], 16)
                green = int(colour[2:4], 16)
                blue = int(colour[4:6], 16)
                script = f'''
Add-Type -AssemblyName System.Drawing
$source = [System.Drawing.Image]::FromFile($env:WAS_PREVIEW_SOURCE)
$bitmap = New-Object System.Drawing.Bitmap 72, 44
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {{
    $graphics.Clear([System.Drawing.Color]::FromArgb({red}, {green}, {blue}))
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $scale = [Math]::Min(72.0 / $source.Width, 44.0 / $source.Height)
    $width = [Math]::Max(1, [int][Math]::Round($source.Width * $scale))
    $height = [Math]::Max(1, [int][Math]::Round($source.Height * $scale))
    $x = [int]((72 - $width) / 2)
    $y = [int]((44 - $height) / 2)
    $graphics.DrawImage($source, $x, $y, $width, $height)
    $bitmap.Save($env:WAS_PREVIEW_TARGET, [System.Drawing.Imaging.ImageFormat]::Png)
}} finally {{
    $graphics.Dispose()
    $bitmap.Dispose()
    $source.Dispose()
}}
'''
                environment = os.environ.copy()
                environment["WAS_PREVIEW_SOURCE"] = str(source_path)
                environment["WAS_PREVIEW_TARGET"] = str(target_path)
                completed = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        script,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=environment,
                    **subprocess_background_kwargs(),
                )
                if completed.returncode != 0 or not target_path.exists():
                    return None

            return tk.PhotoImage(file=str(target_path), master=self.root)
        except Exception:
            return None

    def add_image_row(self, image: ImageEntry, grid_row: int) -> None:
        key = path_key(image.path)
        marker = ttk.Label(
            self.image_grid,
            text="",
            style="Panel.TLabel",
            font=("Segoe UI Symbol", 14),
            anchor="center",
        )
        marker.grid(row=grid_row, column=0, padx=5, pady=6, sticky="nsew")
        self.active_markers[key] = marker

        enabled_variable = tk.BooleanVar(value=image.enabled)

        def update_enabled() -> None:
            with self.state_lock:
                image.enabled = enabled_variable.get()
            self.save_last_config_safely()
            self.update_status()

        ttk.Checkbutton(
            self.image_grid,
            variable=enabled_variable,
            command=update_enabled,
            style="Row.TCheckbutton",
        ).grid(row=grid_row, column=1, padx=5, pady=6)

        preview = self.create_preview(image.path)
        if preview is not None:
            self.preview_references.append(preview)
            preview_label = ttk.Label(self.image_grid, image=preview, style="Panel.TLabel")
            preview_label.image = preview  # Keep a widget-local Tk image reference.
            preview_label.grid(row=grid_row, column=2, padx=5, pady=6, sticky="w")
        else:
            ttk.Label(
                self.image_grid,
                text="No preview",
                style="PanelSubtitle.TLabel",
                anchor="center",
            ).grid(row=grid_row, column=2, padx=5, pady=6, sticky="ew")

        ttk.Label(
            self.image_grid,
            text=Path(image.path).name,
            style="Panel.TLabel",
            anchor="w",
        ).grid(row=grid_row, column=3, padx=5, pady=6, sticky="ew")

        hex_variable = tk.StringVar(value=image.colour)
        colour_button = tk.Button(
            self.image_grid,
            bg=image.colour,
            activebackground=image.colour,
            width=4,
            height=1,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.colours["border"],
            command=lambda: self.choose_colour(image, colour_button, hex_variable),
        )
        colour_button.grid(row=grid_row, column=4, padx=5, pady=6)

        hex_entry = ttk.Entry(self.image_grid, textvariable=hex_variable, width=10)
        hex_entry.grid(row=grid_row, column=5, sticky="ew", padx=5, pady=6)

        def apply_hex(_event=None) -> None:
            try:
                colour = normalize_hex(hex_variable.get())
            except ValueError:
                try:
                    hex_entry.configure(foreground=self.colours["error"])
                except tk.TclError:
                    pass
                return
            with self.state_lock:
                image.colour = colour
            colour_button.configure(bg=colour, activebackground=colour)
            hex_variable.set(colour)
            try:
                hex_entry.configure(foreground=self.colours["foreground"])
            except tk.TclError:
                pass
            self.save_last_config_safely()

        hex_entry.bind("<Return>", apply_hex)
        hex_entry.bind("<FocusOut>", apply_hex)

        day_variable = tk.BooleanVar(value=image.in_day)
        night_variable = tk.BooleanVar(value=image.in_night)

        def update_day() -> None:
            with self.state_lock:
                image.in_day = day_variable.get()
            self.save_last_config_safely()

        def update_night() -> None:
            with self.state_lock:
                image.in_night = night_variable.get()
            self.save_last_config_safely()

        ttk.Checkbutton(
            self.image_grid,
            variable=day_variable,
            command=update_day,
            style="Row.TCheckbutton",
        ).grid(row=grid_row, column=6, padx=5, pady=6)
        ttk.Checkbutton(
            self.image_grid,
            variable=night_variable,
            command=update_night,
            style="Row.TCheckbutton",
        ).grid(row=grid_row, column=7, padx=5, pady=6)

        command_variable = tk.StringVar(value=image.command)
        command_entry = ttk.Entry(self.image_grid, textvariable=command_variable)
        command_entry.grid(row=grid_row, column=8, sticky="ew", padx=5, pady=6)

        def update_command(_event=None) -> None:
            with self.state_lock:
                image.command = command_variable.get().strip()
            self.save_last_config_safely()

        command_entry.bind("<FocusOut>", update_command)
        command_entry.bind("<Return>", update_command)

    def choose_colour(self, image: ImageEntry, button: tk.Button, variable: tk.StringVar) -> None:
        selected = colorchooser.askcolor(color=image.colour, title="Select accent colour")[1]
        if selected:
            selected = normalize_hex(selected)
            with self.state_lock:
                image.colour = selected
            button.configure(bg=selected, activebackground=selected)
            variable.set(selected)
            self.save_last_config_safely()

    def set_active_image(self, image_path: str) -> None:
        self.active_image_path = image_path
        self.refresh_active_markers()
        self.save_last_config_safely()

    def refresh_active_markers(self) -> None:
        active_key = path_key(self.active_image_path) if self.active_image_path else None
        for key, marker in self.active_markers.items():
            marker.configure(
                text="●" if key == active_key else "",
                foreground=self.colours["success"],
            )

    def validate_schedule(self) -> Optional[str]:
        enabled_images = [image for image in self.get_images_snapshot() if image.enabled]
        if not enabled_images:
            return "Select at least one wallpaper."
        if not all(
            [
                parse_time(self.day_start.get()),
                parse_time(self.day_end.get()),
                parse_time(self.night_start.get()),
                parse_time(self.night_end.get()),
            ]
        ):
            return "Use HH:MM values for all time fields."
        try:
            interval = float(self.interval.get())
            if interval <= 0:
                raise ValueError
        except ValueError:
            return "The interval must be a number greater than zero."
        return None

    def get_images_snapshot(self) -> list[ImageEntry]:
        with self.state_lock:
            return [image.clone() for image in self.images]

    def select_next_image(self) -> Optional[ImageEntry]:
        images = [image for image in self.get_images_snapshot() if image.enabled]
        if not images:
            return None

        day_start = parse_time(self.day_start_str)
        day_end = parse_time(self.day_end_str)
        night_start = parse_time(self.night_start_str)
        night_end = parse_time(self.night_end_str)
        if not all([day_start, day_end, night_start, night_end]):
            return None

        now = datetime.now().time()
        day_images = [image for image in images if image.in_day]
        night_images = [image for image in images if image.in_night]

        if in_time_range(now, day_start, day_end):
            active = day_images
            period = "day"
        elif in_time_range(now, night_start, night_end):
            active = night_images
            period = "night"
        else:
            active = day_images or night_images
            period = "day" if day_images else "night"

        if not active:
            return None
        if self.random_order_bool:
            return random.choice(active)

        with self.state_lock:
            if period == "day":
                image = active[self.day_index % len(active)]
                self.day_index += 1
            else:
                image = active[self.night_index % len(active)]
                self.night_index += 1
        return image

    def apply_image(self, image: ImageEntry, source: str) -> None:
        with self.change_lock:
            try:
                if not Path(image.path).is_file():
                    raise FileNotFoundError(f"Wallpaper not found: {image.path}")
                set_wallpaper(image.path)
                set_accent_colour(image.colour)
                command_ran = False
                if self.commands_enabled_bool and image.command.strip():
                    run_external_command(image.command)
                    command_ran = True
                detail = f"{Path(image.path).name}  •  {image.colour.upper()}"
                if image.command.strip() and not self.commands_enabled_bool:
                    detail += "  •  command skipped"
                elif command_ran:
                    detail += "  •  command executed"
                self.event_queue.put(
                    (
                        "IMAGE_APPLIED",
                        {"source": source, "detail": detail, "path": image.path},
                    )
                )
            except Exception as error:
                self.event_queue.put(("NOTIFY_ERROR", str(error)))

    def change_now(self, quiet: bool = False) -> None:
        error = self.validate_schedule()
        if error:
            if quiet:
                self.show_notification("Wallpaper not changed", error)
            else:
                messagebox.showwarning(APP_NAME, error)
            return
        image = self.select_next_image()
        if image is None:
            text = "No selected wallpaper is enabled for the current time range."
            if quiet:
                self.show_notification("Wallpaper not changed", text)
            else:
                messagebox.showwarning(APP_NAME, text)
            return
        threading.Thread(
            target=self.apply_image,
            args=(image, "shortcut" if quiet else "manual"),
            daemon=True,
            name="WallpaperChange",
        ).start()

    def start_scheduler(self, notify: bool = True) -> None:
        error = self.validate_schedule()
        if error:
            if not self.silent:
                messagebox.showwarning(APP_NAME, error)
            return
        if self.scheduler and self.scheduler.is_alive():
            self.scheduler_enabled = True
            self.update_status()
            if notify and not self.silent:
                self.show_notification("Scheduler already running")
            return

        self.scheduler_enabled = True
        self.save_last_config_safely()
        self.scheduler = Scheduler(self)
        self.scheduler.start()
        self.update_status()
        if notify and not self.silent:
            self.show_notification("Scheduler started", "Wallpaper rotation is active in the background.")

    def stop_scheduler(self, notify: bool = True) -> None:
        scheduler = self.scheduler
        if scheduler is not None:
            scheduler.stop()
            scheduler.join(timeout=2.0)
        self.scheduler = None
        self.scheduler_enabled = False
        self.save_last_config_safely()
        self.update_status()
        if notify and not self.silent:
            self.show_notification("Scheduler stopped", "Automatic wallpaper changes are disabled.")

    def update_status(self) -> None:
        running = bool(self.scheduler and self.scheduler.is_alive())
        command_state = "commands on" if self.commands_enabled.get() else "commands off"
        selected = sum(1 for image in self.images if image.enabled)
        self.status_label.configure(
            text=f"{'Running' if running else 'Stopped'}  •  {command_state}  •  {selected}/{len(self.images)} selected"
        )

    def on_commands_checkbox(self) -> None:
        self.commands_enabled_bool = self.commands_enabled.get()
        self.save_last_config_safely()
        self.update_status()

    def toggle_commands(self) -> None:
        enabled = not self.commands_enabled.get()
        self.commands_enabled.set(enabled)
        self.commands_enabled_bool = enabled
        self.save_last_config_safely()
        self.update_status()
        self.show_notification(
            "External commands enabled" if enabled else "External commands disabled",
            "Wallpaper and accent changes remain active.",
        )

    def apply_hotkeys(self, notify: bool = True) -> None:
        shortcuts = {
            1: (self.hotkey_change.get().strip(), "CHANGE_NOW"),
            2: (self.hotkey_commands.get().strip(), "TOGGLE_COMMANDS"),
        }
        for shortcut, _action in shortcuts.values():
            if shortcut:
                try:
                    parse_hotkey(shortcut)
                except ValueError as error:
                    if not self.silent:
                        messagebox.showerror(APP_NAME, f"Invalid shortcut '{shortcut}':\n{error}")
                    return

        if self.hotkey_manager is not None:
            self.hotkey_manager.stop()
        self.hotkey_manager = HotkeyManager(shortcuts, self.event_queue)
        self.hotkey_manager.start()
        self.hotkey_manager.ready.wait(timeout=1.5)
        self.save_last_config_safely()

        if self.hotkey_manager.registration_errors:
            details = "\n".join(self.hotkey_manager.registration_errors)
            if not self.silent:
                messagebox.showwarning(APP_NAME, f"Some shortcuts could not be registered:\n\n{details}")
        elif notify and not self.silent:
            self.show_notification("Shortcuts applied", "Global shortcuts are active while the app is running.")

    def autostart_value_name(self) -> str:
        return APP_SLUG

    def build_autostart_command(self) -> str:
        executable = sys.executable or "python"
        if getattr(sys, "frozen", False):
            return f'"{executable}" --auto-start --background'
        if executable.lower().endswith("python.exe"):
            pythonw = executable[:-10] + "pythonw.exe"
            if Path(pythonw).exists():
                executable = pythonw
        script = str(Path(__file__).resolve())
        return f'"{executable}" "{script}" --auto-start --background'

    def get_autostart(self) -> bool:
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            ) as key:
                winreg.QueryValueEx(key, self.autostart_value_name())
                return True
        except Exception:
            return False

    def set_autostart(self, enabled: bool) -> None:
        if winreg is None:
            return
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    self.autostart_value_name(),
                    0,
                    winreg.REG_SZ,
                    self.build_autostart_command(),
                )
            else:
                try:
                    winreg.DeleteValue(key, self.autostart_value_name())
                except FileNotFoundError:
                    pass

    def on_toggle_autostart(self) -> None:
        try:
            self.set_autostart(self.autostart.get())
        except Exception as error:
            self.autostart.set(self.get_autostart())
            messagebox.showerror(APP_NAME, f"Could not update Windows startup:\n{error}")

    def build_config_dict(self) -> dict:
        with self.state_lock:
            images = [
                {
                    "path": image.path,
                    "colour": image.colour,
                    "enabled": image.enabled,
                    "in_day": image.in_day,
                    "in_night": image.in_night,
                    "command": image.command,
                }
                for image in self.images
            ]
        return {
            "version": CONFIG_VERSION,
            "folder": self.folder.get(),
            "day_start": self.day_start.get(),
            "day_end": self.day_end.get(),
            "night_start": self.night_start.get(),
            "night_end": self.night_end.get(),
            "interval": self.interval.get(),
            "random_order": self.random_order.get(),
            "scheduler_enabled": self.scheduler_enabled,
            "commands_enabled": self.commands_enabled.get(),
            "notifications_enabled": self.notifications_enabled.get(),
            "hotkey_change": self.hotkey_change.get(),
            "hotkey_commands": self.hotkey_commands.get(),
            "active_image_path": self.active_image_path or "",
            "images": images,
        }

    def save_config_to_path(self, file_path: Path) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = file_path.with_suffix(file_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.build_config_dict(), indent=2), encoding="utf-8")
        temporary.replace(file_path)

    def save_last_config_safely(self) -> None:
        if self.loading_config:
            return
        try:
            self.save_config_to_path(default_config_path())
        except Exception:
            pass

    def load_config_from_path(self, file_path: Path) -> bool:
        try:
            config = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return False

        self.loading_config = True
        try:
            self.folder.set(str(config.get("folder", "")))
            self.day_start.set(str(config.get("day_start", "08:00")))
            self.day_end.set(str(config.get("day_end", "20:00")))
            self.night_start.set(str(config.get("night_start", "20:00")))
            self.night_end.set(str(config.get("night_end", "08:00")))
            self.interval.set(str(config.get("interval", "60")))
            self.random_order.set(bool(config.get("random_order", False)))
            self.scheduler_enabled = bool(config.get("scheduler_enabled", False))
            self.commands_enabled.set(bool(config.get("commands_enabled", True)))
            self.notifications_enabled.set(bool(config.get("notifications_enabled", True)))
            self.hotkey_change.set(str(config.get("hotkey_change", "Ctrl+Alt+W")))
            self.hotkey_commands.set(str(config.get("hotkey_commands", "Ctrl+Alt+C")))

            entries: list[ImageEntry] = []
            for raw in config.get("images", []):
                try:
                    colour = normalize_hex(str(raw.get("colour", "#ffffff")))
                except ValueError:
                    colour = "#ffffff"
                entries.append(
                    ImageEntry(
                        path=str(raw.get("path", "")),
                        colour=colour,
                        enabled=bool(raw.get("enabled", True)),
                        in_day=bool(raw.get("in_day", True)),
                        in_night=bool(raw.get("in_night", True)),
                        command=str(raw.get("command", "")),
                    )
                )

            configured_active = str(config.get("active_image_path", "")).strip() or None
            current_wallpaper = get_current_wallpaper()
            self.active_image_path = current_wallpaper or configured_active
            folder = self.folder.get().strip()
            if folder and Path(folder).is_dir():
                self.load_images(folder, preserve=False, configured_entries=entries)
            else:
                with self.state_lock:
                    self.images = entries
                    self.day_index = 0
                    self.night_index = 0
                self.rebuild_image_rows()
            self.update_status()
        finally:
            self.loading_config = False
        return True

    def load_last_config(self) -> bool:
        loaded = self.load_config_from_path(default_config_path())
        if loaded:
            self.current_config_path = None
        return loaded

    def save_current_configuration(self, show_confirmation: bool = True) -> bool:
        if self.current_config_path is None:
            return self.save_config(show_confirmation=show_confirmation)
        try:
            self.save_config_to_path(self.current_config_path)
            self.save_last_config_safely()
            if show_confirmation:
                messagebox.showinfo(APP_NAME, "Configuration saved.")
            return True
        except Exception as error:
            messagebox.showerror(APP_NAME, f"Could not save the configuration:\n{error}")
            return False

    def save_config(self, show_confirmation: bool = True) -> bool:
        file_name = filedialog.asksaveasfilename(
            title="Save configuration",
            defaultextension=".json",
            filetypes=[("JSON configuration", "*.json")],
        )
        if not file_name:
            return False
        try:
            target = Path(file_name)
            self.save_config_to_path(target)
            self.current_config_path = target
            self.save_last_config_safely()
            if show_confirmation:
                messagebox.showinfo(APP_NAME, "Configuration saved.")
            return True
        except Exception as error:
            messagebox.showerror(APP_NAME, f"Could not save the configuration:\n{error}")
            return False

    def load_config(self) -> None:
        file_name = filedialog.askopenfilename(
            title="Load configuration",
            filetypes=[("JSON configuration", "*.json"), ("All files", "*.*")],
        )
        if not file_name:
            return
        target = Path(file_name)
        if not self.load_config_from_path(target):
            messagebox.showerror(APP_NAME, "Could not load the configuration.")
            return
        self.current_config_path = target
        self.save_last_config_safely()
        self.apply_hotkeys(notify=False)
        messagebox.showinfo(APP_NAME, "Configuration loaded.")

    def exit_application(self) -> None:
        if self.exiting:
            return
        self.exiting = True
        if self.scheduler is not None:
            self.scheduler.stop()
            self.scheduler.join(timeout=2.0)
            self.scheduler = None
        if self.hotkey_manager is not None:
            self.hotkey_manager.stop()
            self.hotkey_manager = None
        self.save_last_config_safely()
        self.root.destroy()


def main() -> None:
    background = "--background" in sys.argv
    auto_start = "--auto-start" in sys.argv

    instance_lock = SingleInstanceLock(instance_lock_path())
    if not instance_lock.acquire():
        if not background and not auto_start:
            send_to_existing_instance("SHOW")
        return

    root: Optional[tk.Tk] = None
    ipc_server: Optional[IPCServer] = None
    try:
        root = tk.Tk()
        app = WallpaperApp(root, silent=background or auto_start)
        ipc_server = IPCServer(app.event_queue)
        ipc_server.start_server()

        app.load_last_config()
        app.apply_hotkeys(notify=False)

        if background:
            root.withdraw()

        if auto_start and app.scheduler_enabled:
            app.start_scheduler(notify=False)
        elif not background:
            app.show_window()

        root.mainloop()
    finally:
        if ipc_server is not None:
            ipc_server.stop()
        instance_lock.release()


if __name__ == "__main__":
    main()
