import logging
import queue
import threading
from collections.abc import Callable
from typing import Any


class FloatingOverlay:
    """Small always-on-top Windows overlay that mirrors Whisper Key state.

    This is intentionally separate from the system tray. The tray can be hidden
    in Windows overflow, but this overlay stays visible so the user can tell at a
    glance whether Whisper Key is idle, recording, or processing.
    """

    def __init__(
        self,
        icons: dict[str, Any],
        animated_icons: dict[str, list[Any]],
        on_close: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ):
        self.icons = icons
        self.animated_icons = animated_icons
        self.on_close = on_close
        self.logger = logger or logging.getLogger(__name__)

        self._thread: threading.Thread | None = None
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._running = False

    def start(self) -> bool:
        if self._running:
            return True

        self._running = True
        self._thread = threading.Thread(target=self._run, name="WhisperKeyFloatingOverlay", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._queue.put(("stop", None))

    def update_state(self, state: str):
        if not self._running:
            return
        self._queue.put(("state", state))

    def _run(self):
        try:
            import tkinter as tk

            from PIL import Image, ImageTk
        except Exception as exc:
            self._running = False
            self.logger.warning(f"Floating overlay unavailable: {exc}")
            return

        root = tk.Tk()
        root.title("Whisper Key")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.94)
        root.configure(bg="#171514")

        size = 86
        icon_size = 64
        margin = 11
        screen_w = root.winfo_screenwidth()
        x = max(0, screen_w - size - 28)
        y = 92
        root.geometry(f"{size}x{size}+{x}+{y}")

        canvas = tk.Canvas(
            root,
            width=size,
            height=size,
            bd=0,
            highlightthickness=0,
            bg="#171514",
        )
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(0, 0, size - 1, size - 1, outline="#2d2927", width=2)
        canvas.create_rectangle(3, 3, size - 4, size - 4, outline="#000000", width=1)

        close_button = tk.Label(
            root,
            text="×",
            fg="#ffffff",
            bg="#c33a3a",
            font=("Segoe UI", 11, "bold"),
            width=2,
            cursor="hand2",
        )

        def to_photo(image):
            converted = image.convert("RGBA").resize((icon_size, icon_size), Image.Resampling.NEAREST)
            return ImageTk.PhotoImage(converted)

        photo_cache: dict[str, list[Any]] = {}
        photo_cache["idle"] = [to_photo(self.icons["idle"])]
        for state in ("recording", "processing"):
            frames = self.animated_icons.get(state) or [self.icons.get(state, self.icons["idle"])]
            photo_cache[state] = [to_photo(frame) for frame in frames]
        photo_cache["meeting"] = photo_cache["recording"]

        current_state = "idle"
        frame_index = 0
        image_item = canvas.create_image(margin, margin, anchor="nw", image=photo_cache["idle"][0])
        label_item = canvas.create_text(
            size // 2,
            size - 8,
            text="idle",
            fill="#c7bba0",
            font=("Segoe UI", 7, "bold"),
        )

        drag = {"x": 0, "y": 0}

        def show_close(_event=None):
            close_button.place(x=size - 23, y=3, width=20, height=20)

        def hide_close(_event=None):
            close_button.place_forget()

        def begin_drag(event):
            drag["x"] = event.x
            drag["y"] = event.y

        def do_drag(event):
            new_x = root.winfo_x() + event.x - drag["x"]
            new_y = root.winfo_y() + event.y - drag["y"]
            root.geometry(f"+{new_x}+{new_y}")

        def request_close(_event=None):
            try:
                if self.on_close:
                    self.on_close()
            finally:
                try:
                    root.destroy()
                except Exception:
                    pass

        root.bind("<Enter>", show_close)
        root.bind("<Leave>", hide_close)
        canvas.bind("<ButtonPress-1>", begin_drag)
        canvas.bind("<B1-Motion>", do_drag)
        close_button.bind("<Button-1>", request_close)

        def pump():
            nonlocal current_state, frame_index
            while True:
                try:
                    action, value = self._queue.get_nowait()
                except queue.Empty:
                    break

                if action == "stop":
                    try:
                        root.destroy()
                    except Exception as exc:
                        self.logger.debug("Floating overlay was already closed: %s", exc)
                    return
                if action == "state" and value:
                    current_state = value if value in photo_cache else "idle"
                    frame_index = 0

            frames = photo_cache.get(current_state, photo_cache["idle"])
            if frames:
                canvas.itemconfigure(image_item, image=frames[frame_index % len(frames)])
                frame_index += 1

            label_colors = {
                "idle": "#c7bba0",
                "recording": "#28dc64",
                "processing": "#ffb030",
                "meeting": "#4dd0e1",
            }
            canvas.itemconfigure(label_item, text=current_state, fill=label_colors.get(current_state, "#c7bba0"))
            root.after(140, pump)

        root.after(80, pump)
        try:
            root.mainloop()
        except Exception as exc:
            self.logger.warning(f"Floating overlay stopped unexpectedly: {exc}")
        finally:
            self._running = False
