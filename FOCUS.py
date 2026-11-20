import sys
import time
import json
import os
import importlib.util
import subprocess
import threading
import winsound
import tkinter as tk
from tkinter import Canvas, messagebox


def install_missing_dependencies():
    if getattr(sys, "frozen", False):
        return

    required_modules = ("keyboard", "win32gui", "win32con")
    missing_modules = [
        module for module in required_modules
        if importlib.util.find_spec(module) is None
    ]
    if not missing_modules:
        return

    requirements_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "requirements.txt",
    )
    if not os.path.isfile(requirements_path):
        raise FileNotFoundError(
            f"Cannot install dependencies: {requirements_path} was not found."
        )

    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        requirements_path,
    ])
    importlib.invalidate_caches()


install_missing_dependencies()
import keyboard

# Import optional win32 modules for strict Windows topmost window control
try:
    import win32gui
    import win32con
    HAS_WIN32 = True
except Exception:
    HAS_WIN32 = False

DATA_FILE = "focus_data.json"
BLOCKED_KEYS = ("alt", "windows", "escape", "tab", "left alt", "right alt", "left windows", "right windows")


class FocusTimerApp:
    """Main application class managing fullscreen lock, timer states, penalties, and persistence."""

    def __init__(self, root):
        """Initialize window, state variables, persistent storage, input bindings, and main loop."""
        self.root = root
        self.root.configure(bg="#0a0a0a")

        # Read primary screen dimensions
        self.width = self.root.winfo_screenwidth()
        self.height = self.root.winfo_screenheight()

        # Define timer state constants
        self.STATE_SETUP = 0
        self.STATE_STUDY = 1
        self.STATE_BREAK = 2
        self.current_state = self.STATE_SETUP

        # Configure default study and break durations in seconds
        self.study_input_min = "25"
        self.break_input_min = "5"
        self.study_seconds = 25 * 60
        self.break_seconds = 5 * 60
        self.time_remaining = self.study_seconds
        self.banked_break_seconds = 0

        # Track away time and emergency escape timestamps for break penalties
        self.accumulated_penalty_seconds = 0
        self.last_escape_timestamp = 0.0
        self.was_escaped_to_break = False

        # Track cumulative study and break statistics
        self.total_cumulative_study_seconds = 0.0
        self.total_cumulative_break_seconds = 0.0

        # Track UI focus, lock status, and escape debounce timer
        self.active_input = "study"
        self.is_locked = False
        self.last_escape_trigger_time = 0.0

        # Load saved JSON data on startup
        self.load_persistent_data()

        # Create full-area dark canvas for drawing UI
        self.canvas = Canvas(self.root, bg="#0a0a0a", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # Bind keyboard, mouse, and window close events
        self.root.bind("<Key>", self.handle_key_press)
        self.canvas.bind("<Button-1>", self.handle_mouse_click)
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

        # Apply initial fullscreen setup mode
        self.apply_window_mode()

        # Start main ~60 FPS update loop
        self.last_tick = time.time()
        self.update_loop()

    def _beep_worker(self):
        """Play a 3-second beep sound in a daemon thread without blocking the UI thread."""
        try:
            winsound.Beep(880, 3000)
        except Exception:
            try:
                for _ in range(6):
                    winsound.Beep(880, 350)
                    time.sleep(0.15)
            except Exception:
                pass

    def play_three_second_beep(self):
        """Launch the 3-second audio alert in the background."""
        t = threading.Thread(target=self._beep_worker, daemon=True)
        t.start()

    def flush_screen_transition(self):
        """Redraw the UI and flush pending geometry/paint operations immediately upon state change."""
        self.render_ui()
        self.root.update_idletasks()

    def apply_window_mode(self):
        """Switch between borderless fullscreen (setup/study) and normal window mode (break)."""
        if self.current_state in [self.STATE_SETUP, self.STATE_STUDY]:
            self.root.overrideredirect(True)
            self.root.geometry(f"{self.width}x{self.height}+0+0")
            self.root.attributes("-topmost", True)
        else:
            self.root.overrideredirect(False)
            self.root.attributes("-topmost", False)
            self.root.geometry("800x600")

    def load_persistent_data(self):
        """Load cumulative times and penalties from JSON and compute away time if app closed during study."""
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.total_cumulative_study_seconds = float(data.get("total_study_seconds", 0.0))
                    self.total_cumulative_break_seconds = float(data.get("total_break_seconds", 0.0))
                    self.accumulated_penalty_seconds = int(data.get("accumulated_penalty", 0))
                    last_exit = float(data.get("last_exit_time", 0.0))
                    was_studying = bool(data.get("was_studying", False))

                    if was_studying and last_exit > 0:
                        away_sec = int(time.time() - last_exit)
                        if away_sec > 3:
                            self.accumulated_penalty_seconds += away_sec
            except Exception:
                pass

        self.save_persistent_data(studying=False)

    def save_persistent_data(self, studying=False):
        """Persist cumulative statistics, penalties, and active study state to JSON."""
        data = {
            "last_exit_time": time.time() if studying else 0.0,
            "was_studying": studying,
            "accumulated_penalty": int(self.accumulated_penalty_seconds),
            "total_study_seconds": float(self.total_cumulative_study_seconds),
            "total_break_seconds": float(self.total_cumulative_break_seconds),
        }
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def reset_data(self):
        """Prompt user confirmation and reset all cumulative statistics and penalties."""
        self.unblock_keys()
        self.root.attributes("-topmost", False)
        confirm = messagebox.askyesno("Reset Data", "Reset all cumulative study/break time and penalties?")
        if self.current_state in [self.STATE_SETUP, self.STATE_STUDY]:
            self.root.attributes("-topmost", True)
        if confirm:
            self.total_cumulative_study_seconds = 0.0
            self.total_cumulative_break_seconds = 0.0
            self.accumulated_penalty_seconds = 0
            self.banked_break_seconds = 0
            self.save_persistent_data(studying=False)

    def block_keys(self):
        """Block OS navigation and shortcut keys during study mode."""
        if not self.is_locked:
            for key in BLOCKED_KEYS:
                try:
                    keyboard.block_key(key)
                except Exception:
                    pass
            self.is_locked = True

    def unblock_keys(self):
        """Remove keyboard hooks and unblock blocked keys immediately."""
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        for key in BLOCKED_KEYS:
            try:
                keyboard.unblock_key(key)
            except Exception:
                pass
        self.is_locked = False

    def is_emergency_hotkey_pressed(self):
        """Poll Ctrl + Shift + Q directly on the main thread."""
        try:
            ctrl_down = keyboard.is_pressed("ctrl") or keyboard.is_pressed("left ctrl") or keyboard.is_pressed("right ctrl")
            shift_down = keyboard.is_pressed("shift") or keyboard.is_pressed("left shift") or keyboard.is_pressed("right shift")
            q_down = keyboard.is_pressed("q")
            return ctrl_down and shift_down and q_down
        except Exception:
            return False

    def escape_study_to_break(self):
        """Unlock keys, switch immediately to break mode, start away penalty timer, and play beep simultaneously."""
        now = time.time()
        if now - self.last_escape_trigger_time < 0.8:
            return
        self.last_escape_trigger_time = now

        self.unblock_keys()
        self.last_escape_timestamp = now
        self.was_escaped_to_break = True

        base_break = self.break_seconds + self.banked_break_seconds - self.accumulated_penalty_seconds
        self.banked_break_seconds = 0
        self.accumulated_penalty_seconds = 0
        self.time_remaining = max(0.0, float(base_break))

        self.current_state = self.STATE_BREAK
        self.apply_window_mode()
        self.save_persistent_data(studying=False)
        self.flush_screen_transition()
        self.play_three_second_beep()

    def handle_key_press(self, event):
        """Handle numeric input, Tab switching, Backspace, and Enter in setup mode."""
        if self.current_state == self.STATE_SETUP:
            if event.keysym == "BackSpace":
                if self.active_input == "study" and len(self.study_input_min) > 0:
                    self.study_input_min = self.study_input_min[:-1]
                elif self.active_input == "break" and len(self.break_input_min) > 0:
                    self.break_input_min = self.break_input_min[:-1]
            elif event.keysym == "Tab":
                self.active_input = "break" if self.active_input == "study" else "study"
            elif event.char.isdigit():
                if self.active_input == "study" and len(self.study_input_min) < 3:
                    self.study_input_min += event.char
                elif self.active_input == "break" and len(self.break_input_min) < 3:
                    self.break_input_min += event.char
            elif event.keysym in ["Return", "KP_Enter"]:
                self.start_study_session()

    def start_study_session(self):
        """Start study mode, lock keys, save state, render screen, and trigger 3-second beep together."""
        try:
            s_min = max(1, int(self.study_input_min)) if self.study_input_min else 25
            b_min = max(0, int(self.break_input_min)) if self.break_input_min else 5
            self.study_seconds = s_min * 60
            self.break_seconds = b_min * 60
            self.time_remaining = float(self.study_seconds)
            self.was_escaped_to_break = False
            self.last_escape_timestamp = 0.0
            self.current_state = self.STATE_STUDY
            self.apply_window_mode()
            self.save_persistent_data(studying=True)
            self.block_keys()
            self.flush_screen_transition()
            self.play_three_second_beep()
        except Exception:
            pass

    def handle_mouse_click(self, event):
        """Handle button and input clicks on the canvas across setup and break screens."""
        mx, my = event.x, event.y
        current_w = self.root.winfo_width()
        current_h = self.root.winfo_height()
        cx, cy = current_w // 2, current_h // 2

        if self.current_state == self.STATE_SETUP:
            if cx - 150 <= mx <= cx + 150 and cy - 60 <= my <= cy - 20:
                self.active_input = "study"
            elif cx - 150 <= mx <= cx + 150 and cy + 10 <= my <= cy + 50:
                self.active_input = "break"
            elif cx - 100 <= mx <= cx + 100 and cy + 95 <= my <= cy + 145:
                self.start_study_session()
            elif cx - 100 <= mx <= cx + 100 and cy + 155 <= my <= cy + 205:
                self.reset_data()

        elif self.current_state == self.STATE_BREAK:
            radius = min(current_w, current_h) // 3
            btn_y1 = cy + (radius // 2) + 40
            btn_y2 = cy + (radius // 2) + 100
            if cx - 120 <= mx <= cx + 120 and btn_y1 <= my <= btn_y2:
                self.banked_break_seconds += max(0, int(self.time_remaining))
                self.time_remaining = float(self.study_seconds)
                self.was_escaped_to_break = False
                self.current_state = self.STATE_STUDY
                self.apply_window_mode()
                self.save_persistent_data(studying=True)
                self.block_keys()
                self.flush_screen_transition()
                self.play_three_second_beep()

    def on_window_close(self):
        """Clean up key locks, save state, and exit cleanly when window is closed."""
        self.unblock_keys()
        self.save_persistent_data(studying=(self.current_state == self.STATE_STUDY))
        self.root.destroy()
        sys.exit(0)

    def update_loop(self):
        """Run frame delta timing, hotkey checks, state transitions, and UI rendering."""
        current_time = time.time()
        dt = max(0.0, min(1.0, current_time - self.last_tick))
        self.last_tick = current_time

        if self.current_state == self.STATE_STUDY and self.is_emergency_hotkey_pressed():
            self.escape_study_to_break()

        if self.current_state == self.STATE_STUDY and HAS_WIN32:
            try:
                hwnd = self.root.winfo_id()
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOPMOST,
                    0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                )
            except Exception:
                pass

        if self.current_state == self.STATE_STUDY:
            self.block_keys()
            self.time_remaining -= dt
            self.total_cumulative_study_seconds += dt
            self.save_persistent_data(studying=True)

            if self.time_remaining <= 0:
                self.unblock_keys()
                actual_break = self.break_seconds + self.banked_break_seconds - self.accumulated_penalty_seconds
                self.time_remaining = max(0.0, float(actual_break))
                self.banked_break_seconds = 0
                self.accumulated_penalty_seconds = 0
                self.was_escaped_to_break = False
                self.current_state = self.STATE_BREAK
                self.apply_window_mode()
                self.save_persistent_data(studying=False)
                self.flush_screen_transition()
                self.play_three_second_beep()

        elif self.current_state == self.STATE_BREAK:
            self.unblock_keys()
            if self.was_escaped_to_break and self.last_escape_timestamp > 0:
                elapsed_away = current_time - self.last_escape_timestamp
                self.last_escape_timestamp = current_time
                self.time_remaining = max(0.0, self.time_remaining - elapsed_away)
                if self.time_remaining <= 0:
                    self.was_escaped_to_break = False

            self.time_remaining -= dt
            self.total_cumulative_break_seconds += dt

            if self.time_remaining <= 0:
                self.was_escaped_to_break = False
                self.time_remaining = float(self.study_seconds)
                self.current_state = self.STATE_STUDY
                self.apply_window_mode()
                self.save_persistent_data(studying=True)
                self.block_keys()
                self.flush_screen_transition()
                self.play_three_second_beep()

        self.render_ui()
        self.root.after(16, self.update_loop)

    def render_ui(self):
        """Draw setup controls or large circular study/break countdown on the canvas."""
        self.canvas.delete("all")
        current_w = max(1, self.root.winfo_width())
        current_h = max(1, self.root.winfo_height())
        cx, cy = current_w // 2, current_h // 2

        if self.current_state == self.STATE_SETUP:
            self.canvas.create_text(cx, cy - 220, text="FOCUS", fill="#ffffff", font=("Segoe UI", 64, "bold"))

            total_mins = int(self.total_cumulative_study_seconds // 60)
            total_hrs = total_mins // 60
            rem_mins = total_mins % 60
            stat_str = f"Cumulative Study Time: {total_hrs}h {rem_mins}m"
            self.canvas.create_text(cx, cy - 150, text=stat_str, fill="#aaaaaa", font=("Segoe UI", 16))

            if self.total_cumulative_break_seconds > 0:
                cum_ratio = self.total_cumulative_study_seconds / self.total_cumulative_break_seconds
                ratio_str = f"Cumulative Study/Break Ratio: {cum_ratio:.1f}:1"
            else:
                ratio_str = "Cumulative Study/Break Ratio: N/A"
            self.canvas.create_text(cx, cy - 120, text=ratio_str, fill="#aaaaaa", font=("Segoe UI", 14))

            if self.accumulated_penalty_seconds > 0:
                p_mins = (self.accumulated_penalty_seconds + 59) // 60
                self.canvas.create_text(
                    cx, cy - 90,
                    text=f"Away Penalty Pending: -{p_mins} min from next break",
                    fill="#ff5555",
                    font=("Segoe UI", 14, "bold")
                )

            self.canvas.create_text(cx - 180, cy - 15, text="STUDY (MIN):", fill="#888888", font=("Segoe UI", 18), anchor="w")
            self.canvas.create_rectangle(
                cx - 30, cy - 40, cx + 130, cy + 5,
                outline="#555555" if self.active_input == "study" else "#333333",
                width=2, fill="#1a1a1a"
            )
            self.canvas.create_text(cx + 50, cy - 18, text=self.study_input_min, fill="#ffffff", font=("Segoe UI", 20))

            self.canvas.create_text(cx - 180, cy + 55, text="BREAK (MIN):", fill="#888888", font=("Segoe UI", 18), anchor="w")
            self.canvas.create_rectangle(
                cx - 30, cy + 30, cx + 130, cy + 75,
                outline="#555555" if self.active_input == "break" else "#333333",
                width=2, fill="#1a1a1a"
            )
            self.canvas.create_text(cx + 50, cy + 52, text=self.break_input_min, fill="#ffffff", font=("Segoe UI", 20))

            self.canvas.create_rectangle(cx - 100, cy + 95, cx + 100, cy + 145, fill="#ffffff", outline="")
            self.canvas.create_text(cx, cy + 120, text="START", fill="#0a0a0a", font=("Segoe UI", 20, "bold"))

            self.canvas.create_rectangle(cx - 100, cy + 155, cx + 100, cy + 205, fill="#333333", outline="#555555", width=2)
            self.canvas.create_text(cx, cy + 180, text="RESET DATA", fill="#ff5555", font=("Segoe UI", 18, "bold"))

            self.canvas.create_text(cx, current_h - 60, text="https://github.com/justin-0603/FOCUS", fill="#888888", font=("Segoe UI", 14))
            self.canvas.create_text(cx, current_h - 35, text="© GHDev.", fill="#888888", font=("Segoe UI", 14))

        elif self.current_state in [self.STATE_STUDY, self.STATE_BREAK]:
            radius = min(current_w, current_h) // 3
            rem_sec = max(0, int(self.time_remaining))
            mins = rem_sec // 60
            secs = rem_sec % 60
            time_str = f"{mins:02d}:{secs:02d}"

            total_dur = self.study_seconds if self.current_state == self.STATE_STUDY else max(1, self.break_seconds)
            progress = max(0.0, min(1.0, self.time_remaining / total_dur))

            self.canvas.create_oval(
                cx - radius, (cy - 30) - radius,
                cx + radius, (cy - 30) + radius,
                outline="#282828", width=16
            )

            if progress > 0:
                extent = -360 * progress
                self.canvas.create_arc(
                    cx - radius, (cy - 30) - radius,
                    cx + radius, (cy - 30) + radius,
                    start=90, extent=extent,
                    style="arc", outline="#ffffff", width=16
                )

            time_font_size = max(48, min(120, radius // 2))
            self.canvas.create_text(cx, cy - 40, text=time_str, fill="#ffffff", font=("Segoe UI", time_font_size, "bold"))

            state_label_str = "STUDYING" if self.current_state == self.STATE_STUDY else "BREAK TIME"
            self.canvas.create_text(cx, cy + (radius // 2) - 10, text=state_label_str, fill="#888888", font=("Segoe UI", 28, "bold"))

            if self.current_state == self.STATE_BREAK:
                btn_y1 = cy + (radius // 2) + 40
                btn_y2 = cy + (radius // 2) + 100
                self.canvas.create_rectangle(cx - 120, btn_y1, cx + 120, btn_y2, fill="#ffffff", outline="")
                self.canvas.create_text(cx, (btn_y1 + btn_y2) // 2, text="END BREAK", fill="#0a0a0a", font=("Segoe UI", 20, "bold"))


if __name__ == "__main__":
    root = tk.Tk()
    app = FocusTimerApp(root)
    root.mainloop()