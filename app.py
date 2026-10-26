import copy
import json
import math
import os
import queue
import random
import shutil
import sys
import threading
import time
import tkinter as tk
import uuid
import winreg
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from PIL import Image, ImageDraw, ImageOps, ImageTk, UnidentifiedImageError
from update_client import check_for_update, download_release_asset, repository_is_configured
from update_config import APP_VERSION

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("APPDATA", str(BASE))) / "UsagiJindo"
DATA_FILE = DATA_DIR / "progress.json"
BACKUP_FILE = DATA_DIR / "progress.json.bak"
CHARACTER_DIR = DATA_DIR / "characters"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
KEY = "#ff00ff"
CREAM = "#fffaf0"
INK = "#42332f"
PURPLE = "#8065ec"
TRACK_BG = "#fffaf0"
LOAD_WARNING = ""
LOAD_SAVE_BLOCKED = False
LOAD_RECOVERY_FILE = None
USE_CLASS_IMAGE = object()
AUTO_START_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTO_START_VALUE = "JindoWidget"


def configure_dpi_awareness():
    """Ask Windows for per-monitor DPI coordinates before Tk is initialized."""
    if os.name != "nt":
        return
    try:
        import ctypes
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass

def empty_image_placeholder():
    """Draw a quiet add-image placeholder without bundled character artwork."""
    image = Image.new("RGBA", (112, 124), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((30, 28, 82, 96), radius=12,
                           fill=CREAM, outline="#ded8d0", width=3)
    draw.line((56, 48, 56, 76), fill="#c5bcb1", width=5)
    draw.line((42, 62, 70, 62), fill="#c5bcb1", width=5)
    return image.resize((56, 62), Image.Resampling.LANCZOS)


def load_character_image(path, max_size=(56, 62)):
    path = Path(path)
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("이미지 파일은 20MB 이하만 사용할 수 있어요.")
    with Image.open(path) as source:
        if source.width * source.height > MAX_IMAGE_PIXELS:
            raise ValueError("이미지가 너무 커요. 가로·세로 크기를 줄인 뒤 다시 선택해 주세요.")
        source.seek(0)
        source.load()
        image = ImageOps.exif_transpose(source).convert("RGBA")
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    return image


def calculate_race_positions(progress_values, start, length, max_gap=72):
    """Map progress to the track, adding only enough spacing to avoid overlap."""
    count = len(progress_values)
    if not count:
        return {}
    length = max(0.0, float(length))
    gap = min(float(max_gap), length / max(1, count - 1))
    order = sorted(range(count), key=lambda idx: (progress_values[idx], idx))
    desired = [float(start) + length * max(0, min(100, progress_values[idx])) / 100
               for idx in order]
    positions = desired[:]
    # Forward and backward passes resolve only collisions, preserving the ideal
    # percentage position whenever the available spacing permits it.
    for i in range(1, count):
        positions[i] = max(positions[i], positions[i-1] + gap)
    positions[-1] = min(positions[-1], float(start) + length)
    for i in range(count - 2, -1, -1):
        positions[i] = min(positions[i], positions[i+1] - gap)
    if positions[0] < start:
        shift = float(start) - positions[0]
        positions = [min(float(start) + length, x + shift) for x in positions]
        for i in range(count - 2, -1, -1):
            positions[i] = min(positions[i], positions[i+1] - gap)
    end = float(start) + length
    return {idx: max(float(start), min(end, positions[rank]))
            for rank, idx in enumerate(order)}

def display_image_name(path):
    name = Path(path).name
    return name.split("_", 1)[1] if "_" in name else name

def taskbar_top(screen_height):
    """Return the top edge of the Windows taskbar, or a safe bottom fallback."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowRect.restype = wintypes.BOOL
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        rect = wintypes.RECT()
        if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            if 0 < rect.top < screen_height:
                return rect.top
    except (AttributeError, OSError):
        pass
    return max(0, screen_height - 48)

def fresh_data():
    plan = [{"name": "1차시", "items": [
        {"text": "공기환경"}, {"text": "소리환경"}, {"text": "빛환경"}, {"text": "포스터 그리기"}
    ]}]
    classes = [{"name": f"{n}반", "image": n - 5, "custom_image": None,
                "progress": [[False] * len(plan[0]["items"])], "memo": ""}
               for n in range(5, 11)]
    return {"classes": classes, "plan": plan, "lesson": {}, "float_motion": False,
            "character_scale": 100, "show_progress_labels": True}

def normalize_data(data):
    if not isinstance(data, dict) or not isinstance(data.get("classes"), list):
        raise ValueError("저장 데이터 구조가 올바르지 않습니다.")
    if not data["classes"]:
        raise ValueError("저장된 반 정보가 없습니다.")
    if any(not isinstance(cls, dict) for cls in data["classes"]):
        raise ValueError("반 정보 형식이 올바르지 않습니다.")
    if "plan" not in data:
        # Upgrade earlier files by merging the existing lesson/activity names into
        # one shared plan while keeping each class's completion marks independent.
        plan = []
        for cls in data["classes"]:
            for lesson_idx, old_lesson in enumerate(cls.get("lessons", [])):
                if not isinstance(old_lesson, dict):
                    continue
                while len(plan) <= lesson_idx:
                    plan.append({"name": old_lesson.get("name", f"{lesson_idx+1}차시"), "items": []})
                if not plan[lesson_idx]["items"] and old_lesson.get("name"):
                    plan[lesson_idx]["name"] = old_lesson["name"]
                known = {item["text"] for item in plan[lesson_idx]["items"]}
                for old_item in old_lesson.get("items", []):
                    if isinstance(old_item, dict) and old_item.get("text") not in known:
                        plan[lesson_idx]["items"].append({"text": old_item.get("text", "활동")})
                        known.add(old_item.get("text"))
        data["plan"] = plan or [{"name": "1차시", "items": []}]
        for cls in data["classes"]:
            old_lessons = cls.get("lessons", [])
            cls["progress"] = []
            for lesson_idx, lesson in enumerate(data["plan"]):
                old_items = old_lessons[lesson_idx].get("items", []) if lesson_idx < len(old_lessons) else []
                old_done = {item.get("text"): bool(item.get("done"))
                            for item in old_items if isinstance(item, dict)}
                cls["progress"].append([old_done.get(item["text"], False) for item in lesson["items"]])
    elif not isinstance(data.get("plan"), list):
        raise ValueError("진도 계획 형식이 올바르지 않습니다.")
    for lesson in data["plan"]:
        if not isinstance(lesson, dict):
            raise ValueError("차시 정보 형식이 올바르지 않습니다.")
        lesson.setdefault("name", "차시")
        if not isinstance(lesson.get("items", []), list):
            raise ValueError("활동 목록 형식이 올바르지 않습니다.")
        lesson["items"] = [{"text": item if isinstance(item, str) else item.get("text", "활동")}
                           for item in lesson.get("items", [])]
    if not data["plan"]:
        data["plan"] = [{"name": "1차시", "items": []}]
    for idx, cls in enumerate(data["classes"]):
        cls.setdefault("name", f"{idx+1}반")
        cls.setdefault("image", idx % 6)
        cls.setdefault("custom_image", None)
        cls.setdefault("memo", "")
        existing = cls.get("progress", [])
        if not isinstance(existing, list):
            existing = []
        normalized = []
        for lesson_idx, lesson in enumerate(data["plan"]):
            old_marks = existing[lesson_idx] if lesson_idx < len(existing) else []
            if not isinstance(old_marks, list):
                old_marks = []
            normalized.append([bool(old_marks[i]) if i < len(old_marks) else False
                               for i in range(len(lesson["items"]))])
        cls["progress"] = normalized
        cls.pop("lessons", None)
    if not isinstance(data.get("lesson"), dict):
        data["lesson"] = {}
    data["float_motion"] = bool(data.get("float_motion", False))
    data["show_progress_labels"] = bool(data.get("show_progress_labels", True))
    try:
        data["character_scale"] = max(50, min(150, int(data.get("character_scale", 100))))
    except (TypeError, ValueError):
        data["character_scale"] = 100
    return data


def preserve_recovery_copy():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    recovery = DATA_DIR / f"progress.recovery-{stamp}.json"
    counter = 1
    while recovery.exists():
        recovery = DATA_DIR / f"progress.recovery-{stamp}-{counter}.json"
        counter += 1
    shutil.copy2(DATA_FILE, recovery)
    return recovery


def load_data():
    global LOAD_WARNING, LOAD_SAVE_BLOCKED, LOAD_RECOVERY_FILE
    LOAD_WARNING = ""
    LOAD_SAVE_BLOCKED = False
    LOAD_RECOVERY_FILE = None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    primary_error = None
    if DATA_FILE.exists():
        try:
            return normalize_data(json.loads(DATA_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            primary_error = exc
            try:
                recovery = preserve_recovery_copy()
                LOAD_RECOVERY_FILE = recovery
                LOAD_WARNING = f"저장 파일을 읽지 못해 원본을 보존했어요.\n복구용 파일: {recovery}"
            except OSError as copy_error:
                LOAD_SAVE_BLOCKED = True
                LOAD_WARNING = (f"저장 파일을 읽지 못했고 원본 백업도 만들 수 없어요.\n"
                                f"저장 위치의 권한과 여유 공간을 확인해 주세요.\n{copy_error}")
    if BACKUP_FILE.exists():
        try:
            backup = normalize_data(json.loads(BACKUP_FILE.read_text(encoding="utf-8")))
            if primary_error is None:
                LOAD_WARNING = "기본 저장 파일이 없어 최근 자동 백업을 복구했어요."
            elif LOAD_RECOVERY_FILE:
                LOAD_WARNING = ("기본 저장 파일을 읽지 못해 최근 자동 백업을 복구했어요.\n"
                                f"손상된 파일은 여기 보존했어요: {LOAD_RECOVERY_FILE}")
            else:
                LOAD_WARNING = ("기본 저장 파일을 읽지 못해 최근 자동 백업을 불러왔지만, "
                                "원본 보존 파일을 만들지 못했어요.\n"
                                "저장 위치의 권한과 여유 공간을 확인해 주세요.")
            LOAD_SAVE_BLOCKED = False
            return backup
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
            pass
    if primary_error is not None:
        if not LOAD_WARNING:
            LOAD_WARNING = f"저장 파일을 읽을 수 없어 새 데이터를 준비했어요.\n{primary_error}"
        else:
            LOAD_WARNING += "\n유효한 자동 백업이 없어 새 데이터를 준비했어요."
    return fresh_data()


def atomic_save_data(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOAD_SAVE_BLOCKED:
        raise OSError("원본 저장 파일을 보존하지 못해 덮어쓰기를 막았습니다.")
    if DATA_FILE.exists():
        try:
            normalize_data(json.loads(DATA_FILE.read_text(encoding="utf-8")))
        except OSError:
            # If the current file cannot be inspected, do not overwrite it.
            raise
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            if LOAD_RECOVERY_FILE is None or not LOAD_RECOVERY_FILE.exists():
                preserve_recovery_copy()
        else:
            backup_tmp = BACKUP_FILE.with_name(f"{BACKUP_FILE.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copy2(DATA_FILE, backup_tmp)
                os.replace(backup_tmp, BACKUP_FILE)
            finally:
                if backup_tmp.exists():
                    backup_tmp.unlink()
    temp = DATA_FILE.with_name(f"{DATA_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, DATA_FILE)
    finally:
        if temp.exists():
            temp.unlink()

class Mascot:
    def __init__(self, app, idx, cls):
        self.app, self.idx, self.cls = app, idx, cls
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.configure(bg=KEY)
        self.win.wm_attributes("-transparentcolor", KEY)
        # Keep the desktop character layer below regular application windows.
        self.win.wm_attributes("-topmost", False)
        self.win.geometry("+0+0")
        self.frame = tk.Frame(self.win, bg=KEY)
        self.frame.pack()
        self.name_label = tk.Label(self.frame, text=cls["name"], bg="#ffffff", fg=INK,
                                   font=("Malgun Gothic", 8, "bold"), padx=5, pady=1,
                                   cursor="hand2", highlightthickness=1, highlightbackground="#eadfd4")
        self.name_label.pack(pady=(0, 2))
        self.image_label = tk.Label(self.frame, bg=KEY, bd=0, cursor="hand2")
        self.image_label.pack()
        for widget in (self.win, self.frame, self.image_label, self.name_label):
            widget.bind("<ButtonPress-1>", self.drag_start)
            widget.bind("<B1-Motion>", self.drag_move)
            widget.bind("<ButtonRelease-1>", self.click_release)
            widget.bind("<Button-3>", self.context_menu)
        self.anchor_y = app.track_top - 82
        self.target_x = self.progress_x()
        self.x = self.target_x
        self.y = self.anchor_y
        self.phase = random.random() * 6.28
        self.dragging = False
        self.update_image()
        self.place()

    def update_image(self, scale=None, image_path=USE_CLASS_IMAGE):
        if scale is None:
            scale = self.app.data.get("character_scale", 100)
        scale = max(50, min(150, int(scale))) / 100
        target_size = (round(56 * scale), round(62 * scale))
        if image_path is USE_CLASS_IMAGE:
            image_path = self.cls.get("custom_image")
        self.has_character_image = False
        if image_path:
            try:
                img = load_character_image(image_path, max_size=target_size)
                self.has_character_image = True
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                self.app.note_image_issue(self.cls["name"], exc)
                img = empty_image_placeholder()
        else:
            img = empty_image_placeholder()
        if not image_path or not self.has_character_image:
            img = img.resize(target_size, Image.Resampling.LANCZOS)
        # Tk color-key windows cannot blend partial alpha with the desktop:
        # partial pixels blend with KEY first and leave a second dark fringe.
        # Match alpha to the window's on/off transparency after resizing. Keep
        # every RGB value, including the illustration's original black linework.
        alpha = img.getchannel("A")
        img.putalpha(alpha.point([0] * 128 + [255] * 128))
        self.photo = ImageTk.PhotoImage(img)
        self.character_width, self.character_height = img.size
        self.image_label.configure(image=self.photo)
        self.update_name_label()
        if hasattr(self, "anchor_y"):
            self.anchor_y = max(0, self.app.track_top - self.character_height - 20)
            if not getattr(self, "dragging", False):
                self.y = self.anchor_y
                self.place()

    def update_name_label(self):
        if self.app.data.get("show_progress_labels", True):
            label_text = f"{self.cls['name']}  {self.app.completion(self.idx)}%"
        else:
            label_text = self.cls["name"]
        self.name_label.configure(text=label_text)
        if not self.name_label.winfo_manager():
            self.name_label.pack(before=self.image_label, pady=(0, 2))

    def progress_x(self):
        return self.app.race_positions()[self.idx]

    def pick_target(self):
        return (self.progress_x(), self.anchor_y)

    def place(self):
        self.win.geometry(f"+{int(self.x)}+{int(self.y)}")

    def animate(self):
        if not self.win.winfo_exists():
            return
        if not self.dragging:
            self.target_x, self.target_y = self.pick_target()
            if self.app.data.get("float_motion", False):
                self.target_y += 2.2 * math.sin(time.monotonic() * 1.2 + self.phase)
            dx, dy = self.target_x - self.x, self.target_y - self.y
            distance = max((dx*dx + dy*dy) ** 0.5, 1)
            speed = min(1.4, distance)
            self.x += dx / distance * speed
            self.y += dy / distance * speed
            self.place()
        self.win.after(35, self.animate)

    def drag_start(self, event):
        self.dragging = True
        self.drag_origin = (event.x_root, event.y_root, self.x, self.y)
        self.moved = False

    def drag_move(self, event):
        if not hasattr(self, "drag_origin"):
            return
        ox, oy, x, y = self.drag_origin
        if abs(event.x_root-ox) + abs(event.y_root-oy) > 5:
            self.moved = True
        self.x = max(0, min(self.app.width-self.win.winfo_width(), x + event.x_root-ox))
        self.y = max(0, min(max(0, self.app.height-64), y + event.y_root-oy))
        self.target_x, self.target_y = self.x, self.y
        self.place()
        self.app.root.after(900, self.resume_walk)

    def resume_walk(self):
        self.dragging = False
        self.target_x, self.target_y = self.pick_target()

    def click_release(self, _event):
        if not getattr(self, "moved", False):
            if self.has_character_image:
                self.open_progress()
            else:
                self.choose_image()
        else:
            self.app.root.after(900, self.resume_walk)

    def choose_image(self):
        self.dragging = False
        path = filedialog.askopenfilename(
            parent=self.win, title="캐릭터 이미지 선택",
            filetypes=[("이미지 파일", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                       ("모든 파일", "*.*")])
        if not path:
            return
        try:
            self.cls["custom_image"] = self.app.import_character(path)
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            messagebox.showerror("이미지 열기", f"이 이미지는 사용할 수 없어요.\n{exc}", parent=self.win)
            return
        self.update_image()
        self.app.save()

    def open_progress(self, _event=None):
        self.dragging = False
        self.app.show_progress(self.idx)

    def context_menu(self, event):
        menu = tk.Menu(self.win, tearoff=False)
        menu.add_command(label="반 · 캐릭터 설정", command=self.app.open_class_settings)
        menu.add_command(label="수업 계획 설정", command=self.app.open_plan_settings)
        menu.add_separator()
        auto_start_var = tk.BooleanVar(value=self.app.is_auto_start_enabled())
        menu.add_checkbutton(
            label="자동 실행", variable=auto_start_var,
            state="normal" if self.app.auto_start_supported() else "disabled",
            command=lambda var=auto_start_var: self.app.toggle_auto_start(bool(var.get())))
        menu.add_command(label="업데이트 확인", command=self.app.check_for_updates)
        menu.add_command(label="전체 종료", command=self.app.close)
        menu.tk_popup(event.x_root, event.y_root)

class ClassSettings:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("반 설정")
        self.win.configure(bg=CREAM)
        self.win.wm_attributes("-topmost", True)
        window_width = max(1, min(460, app.width - 24))
        window_height = max(1, min(520, app.height - 48))
        x = max(0, (app.width - window_width) // 2)
        y = max(0, (app.height - window_height) // 2)
        self.win.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.win.minsize(min(380, window_width), min(320, window_height))
        self.count = tk.StringVar(value=str(len(app.data["classes"])))
        self.motion = tk.StringVar(value="float" if app.data.get("float_motion", False) else "still")
        self.character_scale = tk.IntVar(value=app.data.get("character_scale", 100))
        self.show_progress_labels = tk.BooleanVar(value=app.data.get("show_progress_labels", True))
        self.original_float_motion = bool(app.data.get("float_motion", False))
        self.original_show_progress_labels = bool(self.show_progress_labels.get())

        top = tk.Frame(self.win, bg=CREAM, padx=12, pady=10)
        top.pack(fill="x")
        tk.Label(top, text="수업하는 반", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 10, "bold")).pack(side="left")
        tk.Spinbox(top, from_=1, to=20, width=5, textvariable=self.count,
                   font=("Malgun Gothic", 10)).pack(side="left", padx=8)
        tk.Button(top, text="반 개수 적용", command=self.resize,
                  bg="#f0ebff", fg=PURPLE, relief="flat", padx=8).pack(side="left")
        tk.Label(
            self.win,
            text="이미지는 ‘이미지 선택’을 눌러 지정하세요. 반 삭제는 위쪽 반 개수를 줄이고 적용하세요.",
            bg=CREAM, fg="#82736b", font=("Malgun Gothic", 8), anchor="w",
            justify="left", wraplength=window_width - 24
        ).pack(fill="x", padx=12)

        motion_box = tk.Frame(self.win, bg=CREAM, padx=12)
        motion_box.pack(fill="x", pady=(4, 2))
        tk.Label(motion_box, text="전체 캐릭터 대기 동작", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 9, "bold")).pack(side="left")
        tk.Radiobutton(motion_box, text="살짝 둥둥", variable=self.motion, value="float",
                       bg=CREAM, activebackground=CREAM, fg=INK,
                       font=("Malgun Gothic", 9), command=self.preview_motion).pack(side="left", padx=(8, 0))
        tk.Radiobutton(motion_box, text="멈춤", variable=self.motion, value="still",
                       bg=CREAM, activebackground=CREAM, fg=INK,
                       font=("Malgun Gothic", 9), command=self.preview_motion).pack(side="left", padx=(4, 0))
        tk.Label(self.win, text="모든 반에 함께 적용돼요. 진도율에 따른 트랙 이동은 유지됩니다.", bg=CREAM,
                 fg="#82736b", font=("Malgun Gothic", 8), anchor="w").pack(fill="x", padx=12)

        size_box = tk.Frame(self.win, bg=CREAM, padx=12, pady=2)
        size_box.pack(fill="x")
        tk.Label(size_box, text="캐릭터 크기", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 9, "bold")).pack(side="left")
        tk.Scale(size_box, from_=50, to=150, resolution=10, orient="horizontal",
                 showvalue=False, length=150, variable=self.character_scale,
                 command=self.preview_character_size, bg=CREAM, highlightthickness=0,
                 troughcolor="#e8e0d9").pack(side="left", padx=8)
        self.character_scale_label = tk.Label(size_box,
            text=f"{self.character_scale.get()}%", bg=CREAM, fg="#82736b",
            font=("Malgun Gothic", 9), width=5)
        self.character_scale_label.pack(side="left")
        tk.Checkbutton(self.win, text="캐릭터 위에 진도율 표시",
                       variable=self.show_progress_labels, command=self.preview_progress_labels,
                       bg=CREAM, activebackground=CREAM, fg=INK,
                       font=("Malgun Gothic", 9), anchor="w").pack(fill="x", padx=12, pady=(2, 4))

        footer = tk.Frame(self.win, bg=CREAM, padx=12, pady=8)
        footer.pack(side="bottom", fill="x")
        tk.Button(footer, text="닫기", command=self.cancel, bg="white", fg=INK,
                  relief="flat", padx=10).pack(side="right")
        tk.Button(footer, text="저장", command=self.save, bg=PURPLE, fg="white",
                  relief="flat", padx=12).pack(side="right", padx=6)

        body = tk.Frame(self.win, bg=CREAM)
        self.scroll_body = body
        body.pack(fill="both", expand=True, padx=12, pady=8)
        self.canvas = tk.Canvas(body, bg=CREAM, bd=0, highlightthickness=0)
        scrollbar = tk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.win.bind("<MouseWheel>", self.on_settings_mousewheel, add="+")
        self.win.bind("<Button-4>", lambda event: self.scroll_settings_rows(event, -1), add="+")
        self.win.bind("<Button-5>", lambda event: self.scroll_settings_rows(event, 1), add="+")
        self.rows = tk.Frame(self.canvas, bg=CREAM)
        self.rows_id = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", self.schedule_rows_canvas_sync)
        self.canvas.bind("<Configure>", self.on_rows_canvas_configure)
        self.vars = []
        self.image_labels = []
        self.row_frames = []
        self.render_rows()
        self.win.after_idle(self.sync_rows_canvas)

        self.win.protocol("WM_DELETE_WINDOW", self.cancel)

    def schedule_rows_canvas_sync(self, _event=None):
        try:
            self.win.after_idle(self.sync_rows_canvas)
        except tk.TclError:
            pass

    def on_rows_canvas_configure(self, event):
        self.canvas.itemconfigure(self.rows_id, width=max(1, event.width))
        self.schedule_rows_canvas_sync()

    def sync_rows_canvas(self):
        if not self.win.winfo_exists():
            return
        self.rows.update_idletasks()
        width = max(1, self.canvas.winfo_width())
        view_height = max(1, self.canvas.winfo_height())
        content_height = max(1, self.rows.winfo_reqheight())
        total_height = max(view_height, content_height)
        self.canvas.itemconfigure(self.rows_id, width=width, height=total_height)
        self.canvas.configure(scrollregion=(0, 0, width, total_height))

    def scroll_settings_rows(self, event, units):
        widget = event.widget
        while widget is not None and widget is not self.scroll_body:
            widget = getattr(widget, "master", None)
        if widget is None:
            return
        self.canvas.yview_scroll(units, "units")
        return "break"

    def on_settings_mousewheel(self, event):
        delta = getattr(event, "delta", 0)
        if not delta:
            return
        units = -int(delta / 120)
        if units == 0:
            units = -1 if delta > 0 else 1
        return self.scroll_settings_rows(event, units)

    def preview_character_size(self, value):
        self.character_scale_label.configure(text=f"{int(float(value))}%")
        scale = max(50, min(150, int(float(value))))
        for idx, mascot in enumerate(self.app.mascots):
            if idx < len(self.vars):
                mascot.update_image(scale=scale, image_path=self.vars[idx][1].get() or None)
            else:
                mascot.update_image(scale=scale)
        self.app.refresh_track()

    def preview_motion(self):
        self.app.data["float_motion"] = self.motion.get() == "float"

    def preview_progress_labels(self):
        self.app.data["show_progress_labels"] = bool(self.show_progress_labels.get())
        self.app.refresh_track()

    def cancel(self):
        scale = self.app.data.get("character_scale", 100)
        self.app.data["float_motion"] = self.original_float_motion
        self.app.data["show_progress_labels"] = self.original_show_progress_labels
        for mascot in self.app.mascots:
            mascot.update_image(scale=scale)
            mascot.update_name_label()
        self.app.refresh_track()
        self.win.destroy()

    def render_rows(self):
        for child in self.rows.winfo_children():
            child.destroy()
        self.vars = []
        self.image_labels = []
        self.row_frames = []
        for idx, cls in enumerate(self.app.data["classes"]):
            row = tk.Frame(self.rows, bg="white", padx=6, pady=5,
                           highlightthickness=1, highlightbackground="#eadfd4")
            row.pack(fill="x", pady=3)
            row.grid_columnconfigure(2, weight=1)
            name_var = tk.StringVar(value=cls["name"])
            image_var = tk.StringVar(value=cls.get("custom_image") or "")
            tk.Label(row, text=f"{idx+1}.", bg="white", fg=INK,
                     font=("Malgun Gothic", 9, "bold"), width=3).grid(row=0, column=0, padx=(0, 3))
            tk.Entry(row, textvariable=name_var, width=8, font=("Malgun Gothic", 9)).grid(
                row=0, column=1, sticky="ew", padx=(0, 6))
            image_name = display_image_name(image_var.get()) if image_var.get() else "이미지 없음"
            image_label = tk.Label(row, text=image_name, bg="white", fg="#82736b",
                                   font=("Malgun Gothic", 8), width=11, anchor="w")
            image_label.grid(row=0, column=2, sticky="ew", padx=(0, 3))
            choose_button = tk.Button(row, text="이미지 선택", bg="#f0ebff", fg=PURPLE,
                                      relief="flat", padx=6,
                                      command=lambda var=image_var, row_idx=idx: self.choose_image(var, row_idx))
            choose_button.grid(row=0, column=3, padx=2)
            clear_button = tk.Button(row, text="이미지 삭제", bg="#f0ebff", fg=PURPLE,
                                     relief="flat", padx=6,
                                     command=lambda var=image_var, label=image_label, row_idx=idx:
                                     self.reset_image(var, label, row_idx))
            clear_button.grid(row=0, column=4, padx=(2, 0))
            self.vars.append((name_var, image_var))
            self.image_labels.append(image_label)
            self.row_frames.append(row)

    def choose_image(self, image_var, row_idx):
        path = filedialog.askopenfilename(
            parent=self.win, title="캐릭터 이미지 선택",
            filetypes=[("이미지 파일", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                       ("모든 파일", "*.*")])
        if path:
            self.apply_image_path(path, row_idx)

    def apply_image_path(self, path, row_idx):
        try:
            imported = self.app.import_character(path)
            self.vars[row_idx][1].set(imported)
            self.image_labels[row_idx].configure(text=display_image_name(imported))
            if row_idx < len(self.app.mascots):
                self.app.mascots[row_idx].update_image(
                    scale=self.character_scale.get(), image_path=imported)
                self.app.refresh_track()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            messagebox.showerror("이미지 열기", f"이 이미지는 사용할 수 없어요.\n{exc}", parent=self.win)

    def reset_image(self, image_var, image_label, row_idx):
        image_var.set("")
        image_label.configure(text="이미지 없음")
        if row_idx < len(self.app.mascots):
            self.app.mascots[row_idx].update_image(
                scale=self.character_scale.get(), image_path=None)
            self.app.refresh_track()

    def resize(self):
        try:
            count = int(self.count.get())
        except ValueError:
            messagebox.showerror("반 개수", "1에서 20 사이의 숫자를 입력해 주세요.", parent=self.win)
            return
        if not 1 <= count <= 20:
            messagebox.showerror("반 개수", "반은 1개부터 20개까지 설정할 수 있어요.", parent=self.win)
            return
        previous = [(name.get(), image.get()) for name, image in self.vars]
        if self.app.resize_classes(count, self.win):
            self.count.set(str(count))
            self.render_rows()
            for idx, values in enumerate(previous[:len(self.vars)]):
                self.vars[idx][0].set(values[0])
                self.vars[idx][1].set(values[1])
        else:
            self.count.set(str(len(self.app.data["classes"])))

    def save(self):
        self.app.clear_undo()
        self.app.data["float_motion"] = self.motion.get() == "float"
        self.app.data["character_scale"] = max(50, min(150, self.character_scale.get()))
        self.app.data["show_progress_labels"] = bool(self.show_progress_labels.get())
        for idx, cls in enumerate(self.app.data["classes"]):
            if idx >= len(self.vars):
                break
            name_var, image_var = self.vars[idx]
            name = name_var.get().strip()
            if name:
                cls["name"] = name
            cls["custom_image"] = image_var.get() or None
            self.app.mascots[idx].update_image()
        self.app.refresh_track()
        if not self.app.save():
            return
        self.win.destroy()


class PlanSettings:
    def __init__(self, app):
        self.app = app
        self.plan = copy.deepcopy(app.data["plan"])
        self.progress = [copy.deepcopy(cls["progress"]) for cls in app.data["classes"]]
        self.lesson_selection = copy.deepcopy(app.data.get("lesson", {}))
        self.win = tk.Toplevel(app.root)
        self.win.title("수업 계획 설정")
        self.win.configure(bg=CREAM)
        self.win.wm_attributes("-topmost", False)
        window_width = max(1, min(600, app.width - 24))
        window_height = max(1, min(650, app.height - 48))
        x = max(0, (app.width - window_width) // 2)
        y = max(0, (app.height - window_height) // 2)
        self.win.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.win.minsize(min(430, window_width), min(360, window_height))

        header = tk.Frame(self.win, bg=CREAM, padx=14, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="수업 계획 설정", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 12, "bold")).pack(anchor="w")
        tk.Label(header, text="여기서 저장한 차시와 활동은 모든 반에 함께 적용돼요.",
                 bg=CREAM, fg="#82736b", font=("Malgun Gothic", 8)).pack(anchor="w", pady=(2, 0))

        footer = tk.Frame(self.win, bg=CREAM, padx=12, pady=8)
        footer.pack(side="bottom", fill="x")
        tk.Button(footer, text="닫기", command=self.cancel, bg="white", fg=INK,
                  relief="flat", padx=10).pack(side="right")
        tk.Button(footer, text="저장", command=self.save, bg=PURPLE, fg="white",
                  relief="flat", padx=12).pack(side="right", padx=6)

        body = tk.Frame(self.win, bg=CREAM)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.canvas = tk.Canvas(body, bg=CREAM, bd=0, highlightthickness=0)
        scrollbar = tk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=CREAM)
        self.content_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self.sync_scrollregion)
        self.canvas.bind("<Configure>", self.resize_content)
        self.win.bind("<MouseWheel>", self.on_mousewheel, add="+")
        self.render()
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        self.win.lift()

    def sync_scrollregion(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def resize_content(self, event):
        self.canvas.itemconfigure(self.content_id, width=max(1, event.width))

    def on_mousewheel(self, event):
        delta = getattr(event, "delta", 0)
        if not delta:
            return
        units = -int(delta / 120)
        if units == 0:
            units = -1 if delta > 0 else 1
        self.canvas.yview_scroll(units, "units")
        return "break"

    def render(self):
        position = self.canvas.yview()[0] if self.content.winfo_children() else 0.0
        for child in self.content.winfo_children():
            child.destroy()

        for lesson_idx, lesson in enumerate(self.plan):
            card = tk.Frame(self.content, bg="white", padx=8, pady=7,
                            highlightthickness=1, highlightbackground="#eadfd4")
            card.pack(fill="x", pady=4)
            header = tk.Frame(card, bg="white")
            header.pack(fill="x")
            tk.Label(header, text=f"{lesson_idx + 1}. {lesson['name']}", bg="white", fg=INK,
                     font=("Malgun Gothic", 10, "bold"), anchor="w").pack(
                         side="left", fill="x", expand=True)
            tk.Button(header, text="↑", bg="white", fg=INK, relief="flat", padx=4,
                      state="normal" if lesson_idx > 0 else "disabled",
                      command=lambda idx=lesson_idx: self.move_lesson(idx, -1)).pack(side="left")
            tk.Button(header, text="↓", bg="white", fg=INK, relief="flat", padx=4,
                      state="normal" if lesson_idx < len(self.plan) - 1 else "disabled",
                      command=lambda idx=lesson_idx: self.move_lesson(idx, 1)).pack(side="left")
            tk.Button(header, text="수정", bg="#f0ebff", fg=PURPLE, relief="flat", padx=6,
                      command=lambda idx=lesson_idx: self.edit_lesson(idx)).pack(side="left", padx=2)
            tk.Button(header, text="삭제", bg="white", fg="#82736b", relief="flat", padx=6,
                      state="normal" if len(self.plan) > 1 else "disabled",
                      command=lambda idx=lesson_idx: self.delete_lesson(idx)).pack(side="left")

            if lesson["items"]:
                for item_idx, item in enumerate(lesson["items"]):
                    row = tk.Frame(card, bg="#fffdf9", padx=5, pady=4)
                    row.pack(fill="x", pady=(5 if item_idx == 0 else 2, 0))
                    tk.Label(row, text=f"{item_idx + 1}.", bg="#fffdf9", fg="#82736b",
                             font=("Malgun Gothic", 8), width=3).pack(side="left")
                    tk.Label(row, text=item["text"], bg="#fffdf9", fg=INK,
                             font=("Malgun Gothic", 9), anchor="w", justify="left",
                             wraplength=280).pack(side="left", fill="x", expand=True)
                    tk.Button(row, text="↑", bg="#fffdf9", fg=INK, relief="flat", padx=3,
                              state="normal" if item_idx > 0 else "disabled",
                              command=lambda li=lesson_idx, ii=item_idx:
                              self.move_item(li, ii, -1)).pack(side="left")
                    tk.Button(row, text="↓", bg="#fffdf9", fg=INK, relief="flat", padx=3,
                              state="normal" if item_idx < len(lesson["items"]) - 1 else "disabled",
                              command=lambda li=lesson_idx, ii=item_idx:
                              self.move_item(li, ii, 1)).pack(side="left")
                    tk.Button(row, text="수정", bg="#f0ebff", fg=PURPLE, relief="flat", padx=5,
                              command=lambda li=lesson_idx, ii=item_idx:
                              self.edit_item(li, ii)).pack(side="left", padx=2)
                    tk.Button(row, text="삭제", bg="white", fg="#82736b", relief="flat", padx=5,
                              command=lambda li=lesson_idx, ii=item_idx:
                              self.delete_item(li, ii)).pack(side="left")
            else:
                tk.Label(card, text="아직 활동이 없어요.", bg="white", fg="#a3968d",
                         font=("Malgun Gothic", 8)).pack(anchor="w", padx=5, pady=(6, 0))

            tk.Button(card, text="+ 활동 추가", bg="white", fg=PURPLE, relief="flat",
                      command=lambda idx=lesson_idx: self.add_item(idx)).pack(anchor="w", pady=(6, 0))

        tk.Button(self.content, text="+ 차시 추가", bg="#f0ebff", fg=PURPLE, relief="flat",
                  padx=12, pady=5, command=self.add_lesson).pack(anchor="w", pady=(5, 10))
        self.win.after_idle(lambda: self.canvas.yview_moveto(position))

    def add_lesson(self):
        name = simpledialog.askstring("차시 추가", "차시 이름을 입력해 주세요.",
                                      initialvalue=f"{len(self.plan) + 1}차시", parent=self.win)
        if name and name.strip():
            self.plan.append({"name": name.strip(), "items": []})
            for marks in self.progress:
                marks.append([])
            self.render()

    def edit_lesson(self, lesson_idx):
        lesson = self.plan[lesson_idx]
        name = simpledialog.askstring("차시 이름 수정", "차시 이름을 입력해 주세요.",
                                      initialvalue=lesson["name"], parent=self.win)
        if name is None:
            return
        if not name.strip():
            messagebox.showinfo("차시 이름 수정", "이름을 입력해 주세요.", parent=self.win)
            return
        lesson["name"] = name.strip()
        self.render()

    def move_lesson(self, lesson_idx, offset):
        target = lesson_idx + offset
        if not 0 <= target < len(self.plan):
            return
        self.plan[lesson_idx], self.plan[target] = self.plan[target], self.plan[lesson_idx]
        for marks in self.progress:
            marks[lesson_idx], marks[target] = marks[target], marks[lesson_idx]
        for key, selected_idx in list(self.lesson_selection.items()):
            if selected_idx == lesson_idx:
                self.lesson_selection[key] = target
            elif selected_idx == target:
                self.lesson_selection[key] = lesson_idx
        self.render()

    def delete_lesson(self, lesson_idx):
        if len(self.plan) <= 1:
            return
        lesson_name = self.plan[lesson_idx]["name"]
        if not messagebox.askyesno(
                "차시 삭제",
                f"모든 반에서 ‘{lesson_name}’ 차시와 그 안의 활동·완료 기록이 삭제됩니다.\n계속할까요?",
                parent=self.win):
            return
        self.plan.pop(lesson_idx)
        for marks in self.progress:
            if lesson_idx < len(marks):
                marks.pop(lesson_idx)
        for key, old_index in list(self.lesson_selection.items()):
            if old_index > lesson_idx:
                self.lesson_selection[key] = old_index - 1
            elif old_index == lesson_idx:
                self.lesson_selection[key] = min(lesson_idx, len(self.plan) - 1)
        self.render()

    def add_item(self, lesson_idx):
        text = simpledialog.askstring("활동 추가", "활동 내용을 입력해 주세요.", parent=self.win)
        if text and text.strip():
            self.plan[lesson_idx]["items"].append({"text": text.strip()})
            for marks in self.progress:
                marks[lesson_idx].append(False)
            self.render()

    def edit_item(self, lesson_idx, item_idx):
        item = self.plan[lesson_idx]["items"][item_idx]
        text = simpledialog.askstring("활동 수정", "활동 내용을 입력해 주세요.",
                                      initialvalue=item["text"], parent=self.win)
        if text is None:
            return
        if not text.strip():
            messagebox.showinfo("활동 수정", "내용을 입력해 주세요.", parent=self.win)
            return
        item["text"] = text.strip()
        self.render()

    def move_item(self, lesson_idx, item_idx, offset):
        target = item_idx + offset
        items = self.plan[lesson_idx]["items"]
        if not 0 <= target < len(items):
            return
        items[item_idx], items[target] = items[target], items[item_idx]
        for marks in self.progress:
            lesson_marks = marks[lesson_idx]
            lesson_marks[item_idx], lesson_marks[target] = lesson_marks[target], lesson_marks[item_idx]
        self.render()

    def delete_item(self, lesson_idx, item_idx):
        item_text = self.plan[lesson_idx]["items"][item_idx]["text"]
        if not messagebox.askyesno(
                "활동 삭제",
                f"모든 반에서 ‘{item_text}’ 활동과 완료 기록이 삭제됩니다.\n계속할까요?",
                parent=self.win):
            return
        self.plan[lesson_idx]["items"].pop(item_idx)
        for marks in self.progress:
            if lesson_idx < len(marks) and item_idx < len(marks[lesson_idx]):
                marks[lesson_idx].pop(item_idx)
        self.render()

    def save(self):
        previous_plan = self.app.data["plan"]
        previous_lesson = self.app.data.get("lesson", {})
        previous_progress = [cls["progress"] for cls in self.app.data["classes"]]
        self.app.set_undo()
        self.app.data["plan"] = copy.deepcopy(self.plan)
        self.app.data["lesson"] = copy.deepcopy(self.lesson_selection)
        for idx, cls in enumerate(self.app.data["classes"]):
            if idx < len(self.progress):
                cls["progress"] = copy.deepcopy(self.progress[idx])
            else:
                cls["progress"] = [[False] * len(lesson["items"]) for lesson in self.plan]
        self.app.refresh_track()
        if not self.app.save():
            self.app.data["plan"] = previous_plan
            self.app.data["lesson"] = previous_lesson
            for cls, marks in zip(self.app.data["classes"], previous_progress):
                cls["progress"] = marks
            self.app.clear_undo()
            self.app.refresh_track()
            return
        if self.app.bubble and self.app.bubble.win.winfo_exists():
            self.app.bubble.refresh()
        self.close_window()

    def cancel(self):
        self.close_window()

    def close_window(self):
        self.app.plan_settings = None
        if self.win.winfo_exists():
            self.win.destroy()


class ProgressBubble:
    def __init__(self, app, idx):
        self.app, self.idx = app, idx
        self.cls = app.data["classes"][idx]
        self.win = tk.Toplevel(app.root)
        # Keep Tk from mapping a new bubble at the default (0, 0) before its
        # final size and position have been measured.
        self.win.withdraw()
        self.win.overrideredirect(True)
        # Match the desktop widget layer: let regular apps, including browsers,
        # cover the progress bubble when they are activated.
        self.win.wm_attributes("-topmost", False)
        self.win.configure(bg=CREAM, highlightbackground="#eadfd4", highlightthickness=1)
        self.win.bind("<Escape>", lambda _e: self.close())
        self.win.bind("<Control-z>", lambda _e: self.app.undo())
        self.create_footer()
        self.refresh()

    def create_footer(self):
        self.footer = tk.Frame(self.win, bg=CREAM, padx=12, pady=8)
        self.footer.pack(side="bottom", fill="x")
        tk.Button(self.footer, text="＋ 활동", command=self.add_item, bg="#f0ebff", fg=PURPLE,
                  relief="flat", padx=8).pack(side="left", padx=(0, 4))
        tk.Button(self.footer, text="＋ 차시", command=self.add_lesson, bg="white", fg=INK,
                  relief="flat", padx=8).pack(side="left")
        self.save_status_label = tk.Label(self.footer, text="저장됨", bg=CREAM, fg="#82736b",
                                          font=("Malgun Gothic", 8), anchor="w")
        self.save_status_label.pack(side="right", padx=(8, 0))
        self.undo_button = tk.Button(self.footer, text="↶", command=self.app.undo,
                                     bg=CREAM, fg=INK, relief="flat", width=2,
                                     font=("Segoe UI Symbol", 10), takefocus=False,
                                     state="normal" if self.app.undo_snapshot else "disabled")
        self.undo_button.pack(side="right", padx=(4, 0))

    def current_lesson(self):
        lessons = self.app.data["plan"]
        index = min(max(self.app.data["lesson"].get(str(self.idx), 0), 0), len(lessons)-1)
        self.app.data["lesson"][str(self.idx)] = index
        return lessons[index]

    def current_lesson_index(self):
        self.current_lesson()
        return self.app.data["lesson"][str(self.idx)]

    def current_items_with_progress(self):
        lesson_index = self.current_lesson_index()
        marks = self.cls["progress"][lesson_index]
        return [{"text": item["text"], "done": marks[i]} for i, item in enumerate(self.app.data["plan"][lesson_index]["items"])]

    def update_lesson_summary(self):
        items = self.current_items_with_progress()
        done = sum(1 for item in items if item["done"])
        percent = round(done * 100 / len(items)) if items else 0
        if getattr(self, "lesson_summary_label", None) and self.lesson_summary_label.winfo_exists():
            self.lesson_summary_label.configure(
                text=f"이번 차시 {done}/{len(items)}  ·  {percent}% 완료")

    def refresh(self):
        was_visible = bool(self.win.winfo_viewable())
        if not was_visible:
            self.win.withdraw()
        self.capture_memo()
        if getattr(self, "viewport", None) and self.viewport.winfo_exists():
            self.viewport.destroy()
        self.memo_text = None
        lesson = self.current_lesson()
        lesson_items = self.current_items_with_progress()
        self.viewport = tk.Frame(self.win, bg=CREAM)
        self.viewport.pack(fill="both", expand=True)
        self.viewport.grid_rowconfigure(0, weight=1)
        self.viewport.grid_columnconfigure(0, weight=1)
        self.scroll_canvas = tk.Canvas(self.viewport, bg=CREAM, bd=0, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self.viewport, orient="vertical", command=self.scroll_canvas.yview)
        self.scroll_canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scroll_canvas.grid(row=0, column=0, sticky="nsew")
        outer = tk.Frame(self.scroll_canvas, bg=CREAM, padx=12, pady=10)
        self.content_window = self.scroll_canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _e: self.scroll_canvas.configure(
            scrollregion=self.scroll_canvas.bbox("all")))
        self.scroll_canvas.bind("<Configure>", lambda e: self.scroll_canvas.itemconfigure(
            self.content_window, width=e.width))
        self.scroll_canvas.bind("<MouseWheel>", self.scroll_event)
        header = tk.Frame(outer, bg=CREAM)
        header.pack(fill="x")
        tk.Label(header, text=f"{self.cls['name']} 진도", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 11, "bold")).pack(side="left")
        tk.Button(header, text="×", command=self.close, bg=CREAM, fg="#82736b",
                  activebackground=CREAM, activeforeground=INK, relief="flat",
                  bd=0, highlightthickness=0, takefocus=False,
                  font=("Malgun Gothic", 14, "bold"), padx=6, pady=0).pack(side="right")
        lesson_row = tk.Frame(outer, bg=CREAM)
        lesson_row.pack(fill="x", pady=(8, 2))
        tk.Button(lesson_row, text="‹", command=lambda: self.shift_lesson(-1), bg="white", relief="flat").pack(side="left")
        tk.Label(lesson_row, text=lesson["name"], bg=CREAM, fg=INK, font=("Malgun Gothic", 10, "bold")).pack(side="left", padx=8)
        lesson_menu = tk.Button(lesson_row, text="⋯", command=lambda: self.lesson_menu(lesson_menu),
                                bg=CREAM, fg=INK, activebackground=CREAM,
                                relief="flat", font=("Segoe UI Symbol", 10), width=2,
                                padx=0, pady=0, takefocus=False)
        lesson_menu.pack(side="left", padx=(0, 3))
        self.bind_hint(lesson_menu, "차시 삭제")
        tk.Button(lesson_row, text="›", command=lambda: self.shift_lesson(1), bg="white", relief="flat").pack(side="left")
        done = sum(1 for item in lesson_items if item["done"])
        lesson_percent = round(done*100/len(lesson_items)) if lesson_items else 0
        self.lesson_summary_label = tk.Label(outer,
                 text=f"이번 차시 {done}/{len(lesson_items)}  ·  {lesson_percent}% 완료",
                 bg=CREAM, fg="#756963", font=("Malgun Gothic", 9), anchor="w",
                 wraplength=min(250, max(180, self.app.width - 64)))
        self.lesson_summary_label.pack(fill="x", pady=(2, 7))
        for item_idx, item in enumerate(lesson_items):
            var = tk.BooleanVar(value=item["done"])
            item_row = tk.Frame(outer, bg="white")
            item_row.pack(fill="x", pady=2)
            tk.Checkbutton(item_row, text=item["text"], variable=var,
                           command=lambda i=item_idx, v=var: self.toggle(i, v),
                           bg="white", fg=INK, activebackground="white", selectcolor="white",
                           font=("Malgun Gothic", 10), anchor="w",
                           wraplength=max(80, min(170, self.app.width - 155))).pack(side="left", fill="x", expand=True)
            item_menu = tk.Button(item_row, text="⋯",
                                  command=lambda i=item_idx: self.item_menu(i, item_menu),
                                  bg=CREAM, fg=INK, activebackground=CREAM, relief="flat",
                                  font=("Segoe UI Symbol", 10), width=2, padx=0, pady=0,
                                  takefocus=False)
            item_menu.pack(side="right", padx=2, pady=2)
            self.bind_hint(item_menu, "활동 순서 변경 또는 삭제")
        if not lesson_items:
            tk.Label(outer, text="이번 차시에 할 일을 추가해 주세요.", bg=CREAM, fg="#928781",
                     font=("Malgun Gothic", 9)).pack(anchor="w", pady=6)
        tk.Label(outer, text="이 반 메모", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 9, "bold"), anchor="w").pack(fill="x", pady=(5, 2))
        self.memo_text = tk.Text(outer, width=28, height=3, wrap="word", bg="white", fg=INK,
                                 relief="flat", font=("Malgun Gothic", 9),
                                 padx=6, pady=5, highlightthickness=1,
                                 highlightbackground="#eadfd4")
        self.memo_text.pack(fill="x")
        self.memo_text.insert("1.0", self.cls.get("memo", ""))
        self.memo_text.edit_modified(False)
        self.memo_text.bind("<<Modified>>", self.memo_changed)
        self.bind_scroll(outer)
        self.app.save()
        self.win.update_idletasks()
        requested_height = outer.winfo_reqheight()
        footer_height = self.footer.winfo_reqheight()
        max_height = max(1, min(900, self.app.height - 20))
        actual_height = max(1, min(requested_height, max_height - footer_height))
        window_height = actual_height + footer_height
        requested_width = outer.winfo_reqwidth() + 18
        actual_width = max(1, min(requested_width, 300, max(1, self.app.width - 16)))
        if requested_height > actual_height:
            self.scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            self.scroll_canvas.grid_configure(columnspan=2)
        self.scroll_canvas.configure(width=max(1, actual_width - (18 if requested_height > actual_height else 0)),
                                     height=actual_height)
        self.scroll_canvas.itemconfigure(self.content_window,
                                         width=max(1, actual_width - (18 if requested_height > actual_height else 0)))
        mascot = self.app.mascots[self.idx]
        x = int(mascot.x) - actual_width // 2
        y = int(mascot.y) - window_height - 12
        if y < 8:
            y = int(mascot.y) + mascot.character_height + 12
        x = max(8, min(x, max(8, self.app.width - actual_width - 8)))
        y = max(8, min(y, max(8, self.app.height - window_height - 8)))
        self.win.geometry(f"{actual_width}x{window_height}+{x}+{y}")
        if not was_visible:
            self.win.deiconify()

    def bind_hint(self, widget, hint):
        widget.bind("<Enter>", lambda _e: self.save_status_label.configure(text=hint))
        widget.bind("<Leave>", lambda _e: self.save_status_label.configure(
            text=self.app.save_status,
            fg="#b42318" if self.app.save_status == "저장 오류" else "#82736b"))

    def scroll_event(self, event):
        self.scroll_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def bind_scroll(self, widget):
        widget.bind("<MouseWheel>", self.scroll_event)
        for child in widget.winfo_children():
            self.bind_scroll(child)

    def item_menu(self, item_idx, button):
        lesson_index = self.current_lesson_index()
        items = self.app.data["plan"][lesson_index]["items"]
        menu = tk.Menu(self.win, tearoff=False)
        menu.add_command(label="활동 수정", command=lambda: self.edit_item(item_idx))
        menu.add_separator()
        menu.add_command(label="위로 이동", command=lambda: self.move_item(item_idx, -1),
                         state="normal" if item_idx > 0 else "disabled")
        menu.add_command(label="아래로 이동", command=lambda: self.move_item(item_idx, 1),
                         state="normal" if item_idx < len(items)-1 else "disabled")
        menu.add_separator()
        menu.add_command(label="활동 삭제", command=lambda: self.delete_item(item_idx))
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def lesson_menu(self, button):
        menu = tk.Menu(self.win, tearoff=False)
        menu.add_command(label="차시 이름 수정", command=self.edit_lesson)
        menu.add_separator()
        menu.add_command(label="차시 삭제", command=self.delete_lesson,
                         state="normal" if len(self.app.data["plan"]) > 1 else "disabled")
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def move_item(self, item_idx, offset):
        lesson_index = self.current_lesson_index()
        items = self.app.data["plan"][lesson_index]["items"]
        target = item_idx + offset
        if not 0 <= target < len(items):
            return
        self.app.set_undo()
        items[item_idx], items[target] = items[target], items[item_idx]
        for cls in self.app.data["classes"]:
            marks = cls["progress"][lesson_index]
            marks[item_idx], marks[target] = marks[target], marks[item_idx]
        self.app.save()
        self.refresh()

    def delete_item(self, item_idx):
        lesson_index = self.current_lesson_index()
        item = self.app.data["plan"][lesson_index]["items"][item_idx]
        if not messagebox.askyesno("활동 삭제", f"‘{item['text']}’ 활동을 모든 반의 진도에서 삭제할까요?",
                                   parent=self.win):
            return
        self.app.set_undo()
        self.app.data["plan"][lesson_index]["items"].pop(item_idx)
        for cls in self.app.data["classes"]:
            cls["progress"][lesson_index].pop(item_idx)
        self.app.refresh_track()
        self.app.save()
        self.refresh()

    def delete_lesson(self):
        if len(self.app.data["plan"]) <= 1:
            return
        lesson_index = self.current_lesson_index()
        lesson_name = self.app.data["plan"][lesson_index]["name"]
        if not messagebox.askyesno("차시 삭제",
            f"‘{lesson_name}’ 차시와 모든 반의 체크 상태를 삭제할까요?", parent=self.win):
            return
        self.app.set_undo()
        self.app.data["plan"].pop(lesson_index)
        for cls in self.app.data["classes"]:
            cls["progress"].pop(lesson_index)
        for key, old_index in list(self.app.data["lesson"].items()):
            if old_index > lesson_index:
                self.app.data["lesson"][key] = old_index - 1
            elif old_index == lesson_index:
                self.app.data["lesson"][key] = min(lesson_index, len(self.app.data["plan"]) - 1)
        self.app.refresh_track()
        self.app.save()
        self.refresh()

    def memo_changed(self, _event=None):
        if self.memo_text and self.memo_text.winfo_exists() and self.memo_text.edit_modified():
            self.app.clear_undo()
            self.cls["memo"] = self.memo_text.get("1.0", "end-1c")
            self.memo_text.edit_modified(False)
            self.app.save()

    def capture_memo(self):
        if getattr(self, "memo_text", None) and self.memo_text.winfo_exists():
            self.cls["memo"] = self.memo_text.get("1.0", "end-1c")
            self.app.save()

    def toggle(self, idx, var):
        self.app.clear_undo()
        lesson_index = self.current_lesson_index()
        self.cls["progress"][lesson_index][idx] = bool(var.get())
        self.app.refresh_track()
        self.update_lesson_summary()
        self.app.save()

    def edit_item(self, item_idx):
        lesson_index = self.current_lesson_index()
        item = self.app.data["plan"][lesson_index]["items"][item_idx]
        text = simpledialog.askstring("활동 수정", "진도 내용을 수정해 주세요.",
                                      initialvalue=item["text"], parent=self.win)
        if text is None:
            return
        if not text.strip():
            messagebox.showinfo("활동 수정", "내용을 입력해 주세요.", parent=self.win)
            return
        self.app.clear_undo()
        item["text"] = text.strip()
        self.app.save()
        self.refresh()

    def edit_lesson(self):
        lesson = self.current_lesson()
        name = simpledialog.askstring("차시 이름 수정", "차시 이름을 입력해 주세요.",
                                      initialvalue=lesson["name"], parent=self.win)
        if name is None:
            return
        if not name.strip():
            messagebox.showinfo("차시 이름 수정", "이름을 입력해 주세요.", parent=self.win)
            return
        self.app.clear_undo()
        lesson["name"] = name.strip()
        self.app.save()
        self.refresh()

    def add_item(self):
        text = simpledialog.askstring("활동 추가", "이번 차시에 할 일을 입력해 주세요.", parent=self.win)
        if text and text.strip():
            self.app.clear_undo()
            lesson_index = self.current_lesson_index()
            self.app.data["plan"][lesson_index]["items"].append({"text": text.strip()})
            for cls in self.app.data["classes"]:
                cls["progress"][lesson_index].append(False)
            self.app.refresh_track()
            self.refresh()

    def add_lesson(self):
        was_topmost = bool(int(self.win.wm_attributes("-topmost")))
        self.win.wm_attributes("-topmost", False)
        self.win.lower()
        try:
            name = simpledialog.askstring("차시 추가", "차시 이름을 입력해 주세요.",
                                          initialvalue=f"{len(self.app.data['plan'])+1}차시", parent=self.win)
        finally:
            if self.win.winfo_exists():
                self.win.wm_attributes("-topmost", was_topmost)
        if name and name.strip():
            self.app.clear_undo()
            self.app.data["plan"].append({"name": name.strip(), "items": []})
            for cls in self.app.data["classes"]:
                cls["progress"].append([])
            self.app.refresh_track()
            self.app.data["lesson"][str(self.idx)] = len(self.app.data["plan"])-1
            self.refresh()

    def shift_lesson(self, offset):
        self.app.clear_undo()
        self.app.data["lesson"][str(self.idx)] = (self.app.data["lesson"].get(str(self.idx), 0)+offset) % len(self.app.data["plan"])
        self.refresh()

    def close(self):
        self.app.save()
        self.app.bubble = None
        self.win.destroy()

class App:
    def __init__(self, root):
        self.root = root
        self.root.withdraw()
        self.root.title("우리 반 캐릭터 진도 위젯")
        self.root.wm_attributes("-topmost", False)
        self.data = load_data()
        self.width, self.height = root.winfo_screenwidth(), root.winfo_screenheight()
        self.screen_scale = float(root.tk.call("tk", "scaling"))
        self.image_issues = []
        self.save_error_shown = False
        self.save_status = "저장됨"
        self.undo_snapshot = None
        self.update_events = queue.Queue()
        self.update_busy = False
        self.update_status_win = None
        self.update_status_bar = None
        self.lane_height = 82
        self.track_top = taskbar_top(self.height)
        self.track_start = 54
        self.track_length = max(0, self.width - self.track_start - 74)
        self.create_track()
        self.mascots = []
        self.bubble = None
        self.plan_settings = None
        for idx, cls in enumerate(self.data["classes"]):
            mascot = Mascot(self, idx, cls)
            self.mascots.append(mascot)
        for mascot in self.mascots:
            mascot.animate()
        root.bind_all("<Control-Shift-q>", lambda _e: self.close())
        if not LOAD_SAVE_BLOCKED:
            self.save()
        root.after(350, self.show_startup_notices)
        root.after(2000, self.check_display_geometry)
        if getattr(sys, "frozen", False) and repository_is_configured():
            root.after(4000, lambda: self.check_for_updates(manual=False))

    def create_track(self):
        self.track_win = tk.Toplevel(self.root)
        self.track_win.overrideredirect(True)
        self.track_win.configure(bg=KEY)
        self.track_win.wm_attributes("-transparentcolor", KEY)
        self.track_win.wm_attributes("-topmost", False)
        self.track_win.geometry(f"{self.width}x{self.height}+0+0")
        self.track_canvas = tk.Canvas(self.track_win, width=self.width, height=self.height,
                                      bg=KEY, bd=0, highlightthickness=0)
        self.track_canvas.pack()
        self.refresh_track()

    def open_class_settings(self):
        ClassSettings(self)

    def auto_start_supported(self):
        return os.name == "nt" and bool(getattr(sys, "frozen", False))

    def is_auto_start_enabled(self):
        if not self.auto_start_supported():
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTO_START_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, AUTO_START_VALUE)
        except FileNotFoundError:
            return False
        expected = f'"{Path(sys.executable).resolve()}"'
        return str(value).strip() == expected

    def set_auto_start(self, enabled, parent=None):
        if not self.auto_start_supported():
            return False
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTO_START_KEY) as key:
                if enabled:
                    command = f'"{Path(sys.executable).resolve()}"'
                    winreg.SetValueEx(key, AUTO_START_VALUE, 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, AUTO_START_VALUE)
                    except FileNotFoundError:
                        pass
            return True
        except OSError as exc:
            messagebox.showerror(
                "자동 실행 설정",
                f"Windows 시작프로그램 설정을 변경하지 못했어요.\n\n{exc}",
                parent=parent or self.root)
            return False

    def toggle_auto_start(self, enabled):
        parent = self.mascots[0].win if self.mascots else self.root
        if not self.auto_start_supported():
            messagebox.showinfo(
                "자동 실행", "자동 실행 설정은 설치된 Windows 앱에서 사용할 수 있어요.",
                parent=parent)
            return
        if self.set_auto_start(enabled, parent=parent) and enabled:
            messagebox.showinfo(
                "자동 실행",
                "Windows에 로그인하면 진도 위젯이 자동으로 실행됩니다.",
                parent=parent)

    def open_plan_settings(self):
        if self.plan_settings and self.plan_settings.win.winfo_exists():
            self.plan_settings.win.lift()
            self.plan_settings.win.focus_force()
            return
        self.plan_settings = PlanSettings(self)

    def check_for_updates(self, manual=True):
        """Check GitHub off the UI thread; download the installer on request."""
        parent = self.mascots[0].win if self.mascots else self.root
        if self.update_busy:
            if manual:
                messagebox.showinfo("업데이트", "이미 업데이트를 확인하거나 내려받고 있어요.", parent=parent)
            return
        if not getattr(sys, "frozen", False):
            if manual:
                messagebox.showinfo("업데이트", "업데이트 설치는 배포된 Windows 앱에서 사용할 수 있어요.", parent=parent)
            return
        if not repository_is_configured():
            if manual:
                messagebox.showinfo("업데이트", "업데이트 저장소가 아직 설정되지 않았어요.", parent=parent)
            return

        self.update_busy = True

        def worker():
            try:
                self.update_events.put(("check", check_for_update(), manual))
            except Exception as exc:
                self.update_events.put(("check_error", exc, manual))

        threading.Thread(target=worker, name="update-check", daemon=True).start()
        self.root.after(150, self.poll_update_events)

    def poll_update_events(self):
        if not self.root.winfo_exists():
            return
        try:
            event, payload, manual = self.update_events.get_nowait()
        except queue.Empty:
            if self.update_busy:
                self.root.after(150, self.poll_update_events)
            return

        parent = self.mascots[0].win if self.mascots else self.root
        if event == "check_error":
            self.update_busy = False
            if manual:
                messagebox.showerror("업데이트 확인", str(payload), parent=parent)
            return
        if event == "check":
            self.update_busy = False
            if payload is None:
                if manual:
                    messagebox.showinfo("업데이트 확인",
                                        f"현재 버전(v{APP_VERSION})이 최신 버전이에요.", parent=parent)
                return
            notes = payload.get("notes", "")
            if len(notes) > 1200:
                notes = notes[:1200].rstrip() + "…"
            if self.confirm_update_download(payload, notes):
                self.start_update_download(payload)
            return
        if event == "downloaded":
            update, installer = payload
            self.finish_update_status()
            self.update_busy = False
            messagebox.showinfo(
                "다운로드 완료",
                f"진도 위젯 v{update['version']} 설치 파일을 저장했어요.\n\n"
                f"{installer}\n\n"
                "현재 프로그램을 종료한 뒤 이 설치 파일을 실행해 주세요.\n"
                "진도와 반 설정은 그대로 유지됩니다.",
                parent=parent)
            try:
                os.startfile(str(installer.parent))
            except OSError as exc:
                messagebox.showerror(
                    "다운로드 폴더 열기",
                    f"설치 파일은 저장됐지만 폴더를 열지 못했어요.\n\n{exc}",
                    parent=parent)
            return
        if event == "download_error":
            self.finish_update_status()
            self.update_busy = False
            messagebox.showerror("업데이트 다운로드", str(payload), parent=parent)

    def confirm_update_download(self, update, notes):
        result = {"download": False}
        dialog = tk.Toplevel(self.root)
        dialog.title("새 업데이트")
        dialog.configure(bg=CREAM)
        dialog.resizable(False, False)
        dialog.wm_attributes("-topmost", False)
        width = max(1, min(440, self.width - 24))
        height = max(1, min(340, self.height - 48))
        x = max(0, (self.width - width) // 2)
        y = max(0, (self.height - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        header = tk.Frame(dialog, bg=CREAM, padx=16, pady=12)
        header.pack(fill="x")
        tk.Label(header, text=f"새 버전 v{update['version']}을 받을 수 있어요.",
                 bg=CREAM, fg=INK, font=("Malgun Gothic", 11, "bold")).pack(anchor="w")
        tk.Label(header, text=f"현재 버전: v{APP_VERSION}", bg=CREAM, fg="#82736b",
                 font=("Malgun Gothic", 9)).pack(anchor="w", pady=(2, 0))

        notes_frame = tk.Frame(dialog, bg="white", highlightthickness=1,
                               highlightbackground="#eadfd4")
        notes_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        notes_scroll = tk.Scrollbar(notes_frame, orient="vertical")
        notes_text = tk.Text(notes_frame, height=6, wrap="word", relief="flat", bd=0,
                             padx=9, pady=7, bg="white", fg=INK,
                             font=("Malgun Gothic", 9), yscrollcommand=notes_scroll.set)
        notes_scroll.configure(command=notes_text.yview)
        notes_scroll.pack(side="right", fill="y")
        notes_text.pack(side="left", fill="both", expand=True)
        notes_text.insert("1.0", notes or "릴리스 설명이 없습니다.")
        notes_text.configure(state="disabled")

        notice = tk.Frame(dialog, bg=CREAM, padx=16)
        notice.pack(fill="x")
        tk.Label(notice, text="설치 파일을 다운로드할까요?", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 9)).pack(anchor="w")
        emphasis = tk.Frame(notice, bg=CREAM)
        emphasis.pack(anchor="w", pady=(2, 0))
        tk.Label(emphasis, text="다운로드 후 ", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 9)).pack(side="left")
        tk.Label(emphasis, text="설치 파일을 실행", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 9, "bold")).pack(side="left")
        tk.Label(emphasis, text="해야 업데이트가 완료됩니다.", bg=CREAM, fg=INK,
                 font=("Malgun Gothic", 9)).pack(side="left")
        tk.Label(notice, text="진도와 반 설정은 그대로 유지됩니다.", bg=CREAM, fg="#82736b",
                 font=("Malgun Gothic", 9)).pack(anchor="w", pady=(2, 0))

        def finish(download):
            result["download"] = download
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        footer = tk.Frame(dialog, bg=CREAM, padx=14, pady=12)
        footer.pack(fill="x")
        tk.Button(footer, text="취소", command=lambda: finish(False), bg="white", fg=INK,
                  relief="flat", padx=12).pack(side="right")
        tk.Button(footer, text="다운로드", command=lambda: finish(True), bg=PURPLE, fg="white",
                  relief="flat", padx=14).pack(side="right", padx=6)
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.bind("<Escape>", lambda _event: finish(False))
        dialog.bind("<Return>", lambda _event: finish(True))
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        dialog.wait_window()
        return result["download"]

    def start_update_download(self, update):
        if not getattr(sys, "frozen", False):
            return
        parent = self.mascots[0].win if self.mascots else self.root
        downloads = Path.home() / "Downloads"
        initial_dir = downloads if downloads.is_dir() else Path.home()
        selected = filedialog.asksaveasfilename(
            parent=parent,
            title="업데이트 설치 파일 저장",
            initialdir=str(initial_dir),
            initialfile=update["asset_name"],
            defaultextension=".exe",
            filetypes=[("Windows 설치 파일", "*.exe")],
            confirmoverwrite=True)
        if not selected:
            return
        destination = Path(selected)

        self.update_busy = True
        self.update_status_win = tk.Toplevel(self.root)
        self.update_status_win.title("업데이트")
        self.update_status_win.configure(bg=CREAM)
        self.update_status_win.resizable(False, False)
        self.update_status_win.wm_attributes("-topmost", True)
        width, height = 330, 100
        x = max(0, (self.width - width) // 2)
        y = max(0, (self.height - height) // 2)
        self.update_status_win.geometry(f"{width}x{height}+{x}+{y}")
        self.update_status_win.protocol("WM_DELETE_WINDOW", lambda: None)
        tk.Label(self.update_status_win, text=f"v{update['version']} 설치 파일을 내려받고 있어요.",
                 bg=CREAM, fg=INK, font=("Malgun Gothic", 10)).pack(pady=(18, 8))
        from tkinter.ttk import Progressbar
        self.update_status_bar = Progressbar(self.update_status_win, mode="indeterminate", length=280)
        self.update_status_bar.pack(pady=(0, 12))
        self.update_status_bar.start(12)

        def worker():
            try:
                download_release_asset(update, destination)
                self.update_events.put(("downloaded", (update, destination), False))
            except Exception as exc:
                self.update_events.put(("download_error", exc, False))

        threading.Thread(target=worker, name="update-download", daemon=True).start()
        self.root.after(150, self.poll_update_events)

    def finish_update_status(self):
        if self.update_status_bar:
            self.update_status_bar.stop()
            self.update_status_bar = None
        if self.update_status_win and self.update_status_win.winfo_exists():
            self.update_status_win.destroy()
        self.update_status_win = None

    def resize_classes(self, count, parent=None):
        classes = self.data["classes"]
        if count < len(classes):
            if not messagebox.askyesno("반 줄이기",
                f"마지막 {len(classes)-count}개 반의 진도와 메모가 삭제됩니다.\n계속할까요?",
                parent=parent or self.root):
                return False
            self.clear_undo()
            if self.bubble and self.bubble.idx >= count:
                self.bubble.close()
            for mascot in self.mascots[count:]:
                mascot.win.destroy()
            del self.mascots[count:]
            del classes[count:]
            self.data["lesson"] = {key: value for key, value in self.data["lesson"].items()
                                    if key.isdigit() and int(key) < count}
        elif count > len(classes):
            self.clear_undo()
            used_numbers = []
            for cls in classes:
                digits = "".join(ch for ch in cls["name"] if ch.isdigit())
                if digits:
                    used_numbers.append(int(digits))
            next_number = max(used_numbers, default=0) + 1
            for _ in range(count - len(classes)):
                idx = len(classes)
                cls = {"name": f"{next_number}반", "image": 0, "custom_image": None,
                       "progress": [[False] * len(lesson["items"]) for lesson in self.data["plan"]],
                       "memo": ""}
                classes.append(cls)
                next_number += 1
                mascot = Mascot(self, idx, cls)
                self.mascots.append(mascot)
                mascot.animate()
        self.refresh_track()
        self.save()
        return True

    def refresh_track(self):
        if not hasattr(self, "track_canvas") or not self.track_canvas.winfo_exists():
            return
        canvas = self.track_canvas
        canvas.delete("all")
        self.track_top = taskbar_top(self.height)
        self.track_start = min(54, max(12, self.width // 12))
        self.track_length = max(0, self.width - self.track_start - 74)
        finish_x = self.track_start + self.track_length
        # The Windows taskbar is the race lane; retain only the arrival flag.
        canvas.create_line(finish_x, self.track_top - 28, finish_x, self.track_top + 3,
                           fill="#665a52", width=2)
        for row in range(3):
            for col in range(4):
                color = "#665a52" if (row + col) % 2 == 0 else "#fffaf0"
                canvas.create_rectangle(finish_x + 1 + col * 4, self.track_top - 28 + row * 4,
                                        finish_x + 5 + col * 4, self.track_top - 24 + row * 4,
                                        fill=color, outline=color)
        for idx, mascot in enumerate(getattr(self, "mascots", [])):
            mascot.anchor_y = max(0, self.track_top - mascot.character_height - 20)
            mascot.update_name_label()
            mascot.target_x, mascot.target_y = mascot.pick_target()

    def completion(self, idx):
        cls = self.data["classes"][idx]
        total = sum(len(lesson["items"]) for lesson in self.data["plan"])
        done = sum(sum(bool(mark) for mark in marks) for marks in cls["progress"])
        return round(done * 100 / total) if total else 0

    def race_positions(self):
        # x is the mascot window's left edge, so reserve its width before the flag.
        finish_clearance = min(58, self.track_length)
        race_length = max(0, self.track_length - finish_clearance)
        progress = [self.completion(idx) for idx in range(len(self.data["classes"]))]
        return calculate_race_positions(progress, self.track_start, race_length)

    def import_character(self, source):
        source = Path(source)
        suffix = source.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
            raise ValueError("PNG, JPG, WEBP, BMP 또는 GIF 파일을 선택해 주세요.")
        try:
            load_character_image(source)
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise ValueError(f"이미지를 읽을 수 없어요. 파일 형식과 크기를 확인해 주세요.\n{exc}") from exc
        CHARACTER_DIR.mkdir(parents=True, exist_ok=True)
        destination = CHARACTER_DIR / f"{uuid.uuid4().hex[:8]}_{source.name}"
        temp = destination.with_name(f"{destination.name}.tmp")
        try:
            shutil.copy2(source, temp)
            os.replace(temp, destination)
        finally:
            if temp.exists():
                temp.unlink()
        return str(destination)

    def show_progress(self, idx):
        if self.bubble and self.bubble.win.winfo_exists():
            self.bubble.close()
        self.bubble = ProgressBubble(self, idx)

    def clear_undo(self):
        self.undo_snapshot = None
        if self.bubble and self.bubble.win.winfo_exists() and getattr(self.bubble, "undo_button", None):
            self.bubble.undo_button.configure(state="disabled")

    def set_undo(self):
        self.undo_snapshot = copy.deepcopy(self.data)
        if self.bubble and self.bubble.win.winfo_exists() and getattr(self.bubble, "undo_button", None):
            self.bubble.undo_button.configure(state="normal")

    def undo(self):
        if self.undo_snapshot is None:
            return
        self.data.clear()
        self.data.update(self.undo_snapshot)
        self.clear_undo()
        for idx, mascot in enumerate(self.mascots):
            mascot.cls = self.data["classes"][idx]
            mascot.update_name_label()
        self.refresh_track()
        if self.bubble and self.bubble.win.winfo_exists():
            self.bubble.cls = self.data["classes"][self.bubble.idx]
            self.bubble.memo_text = None
            self.bubble.refresh()
        self.save()

    def save(self):
        try:
            atomic_save_data(self.data)
            self.save_error_shown = False
            self.save_status = "저장됨"
            if self.bubble and self.bubble.win.winfo_exists() and getattr(self.bubble, "save_status_label", None):
                self.bubble.save_status_label.configure(text=self.save_status, fg="#82736b")
            return True
        except (OSError, TypeError, ValueError) as exc:
            self.save_status = "저장 오류"
            if self.bubble and self.bubble.win.winfo_exists() and getattr(self.bubble, "save_status_label", None):
                self.bubble.save_status_label.configure(text=self.save_status, fg="#b42318")
            if not self.save_error_shown:
                self.save_error_shown = True
                messagebox.showerror(
                    "진도를 저장할 수 없어요",
                    f"저장 위치의 권한과 여유 공간을 확인해 주세요.\n\n{exc}",
                    parent=self.root)
            return False

    def note_image_issue(self, class_name, error):
        detail = f"{class_name}: {error}"
        if detail not in self.image_issues:
            self.image_issues.append(detail)

    def show_startup_notices(self):
        notices = []
        if LOAD_WARNING:
            notices.append(LOAD_WARNING)
        if self.image_issues:
            notices.append("일부 캐릭터 이미지를 열지 못해 자리표시자로 표시했어요.\n" +
                           "반 설정에서 다시 선택해 주세요.\n" + "\n".join(self.image_issues[:8]))
        if notices:
            messagebox.showwarning("확인할 항목이 있어요", "\n\n".join(notices), parent=self.root)

    def check_display_geometry(self):
        if not self.root.winfo_exists():
            return
        width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            scale = float(self.root.tk.call("tk", "scaling"))
        except tk.TclError:
            scale = self.screen_scale
        if (width, height) != (self.width, self.height) or abs(scale - self.screen_scale) > 0.01:
            self.width, self.height = width, height
            self.screen_scale = scale
            self.track_win.geometry(f"{width}x{height}+0+0")
            self.track_canvas.configure(width=width, height=height)
            self.refresh_track()
            for mascot in self.mascots:
                mascot.x = max(0, min(self.width - 56, mascot.x))
                mascot.y = max(0, min(max(0, self.height - 64), mascot.y))
                mascot.place()
        elif taskbar_top(height) != self.track_top:
            self.refresh_track()
        self.root.after(2000, self.check_display_geometry)

    def close(self):
        if not self.save() and not messagebox.askyesno(
                "저장하지 않고 종료할까요?",
                "현재 변경 사항을 저장하지 못했어요. 저장하지 않고 종료할까요?",
                parent=self.root):
            self.save_error_shown = False
            return
        self.root.destroy()

if __name__ == "__main__":
    configure_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        detail = f"화면 창을 시작할 수 없습니다. Python의 Tcl/Tk 설치 상태를 확인해 주세요.\n\n{exc}"
        print(detail, file=sys.stderr)
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, detail, "우리 반 캐릭터 진도 위젯", 0x10)
            except (AttributeError, OSError):
                pass
        raise SystemExit(1)
    try:
        App(root)
    except Exception as exc:
        messagebox.showerror("앱을 시작할 수 없어요", f"시작 중 문제가 발생했습니다.\n\n{exc}", parent=root)
        root.destroy()
    else:
        ready_file = os.environ.pop("USAGIJINDO_UPDATE_READY_FILE", "")
        if ready_file:
            root.after(250, lambda: Path(ready_file).write_text("ready", encoding="utf-8"))
        root.mainloop()

