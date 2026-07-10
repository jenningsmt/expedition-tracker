"""
Expedition configuration editor.

Stand-alone:
    python ui/config_window.py

Launched from tracker tray:
    subprocess.Popen([sys.executable, "ui/config_window.py"])
"""
from __future__ import annotations

import re
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config.toml"

_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)


# ── TOML serialisation ────────────────────────────────────────────────────────

def _toml_str(value: str) -> str:
    """Serialise a Python string as a TOML literal-string (single quotes) when
    possible, falling back to a basic-string (double quotes) otherwise."""
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _rarity_block(path: Path) -> str:
    """Return everything from the [rarity] table onwards in the current file."""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^\[rarity\]", text, re.MULTILINE)
    return text[m.start():] if m else ""


def _write_config(path: Path, data: dict) -> None:
    rarity = _rarity_block(path)

    lines = [
        "# Elite Dangerous Expedition Tracker",
        "# Edit this file to define your expedition, then launch tracker.pyw.",
        "",
        "# ── Journal source ────────────────────────────────────────────────────────────",
        f"journal_dir = {_toml_str(data['journal_dir'])}",
        "",
        "# ── Output & database ─────────────────────────────────────────────────────────",
        'output_dir = "output"',
        'db_path    = "tracker.db"',
        "",
        "# ── Commander ─────────────────────────────────────────────────────────────────",
        f"commander = {_toml_str(data['commander'])}",
        "",
        "# ── Expedition definition ─────────────────────────────────────────────────────",
        "# Find expedition_start_timestamp in your journal: first FSDJump after",
        "# departure. Format: YYYY-MM-DDTHH:MM:SSZ (UTC). Events before this are",
        "# ignored, which excludes pre-departure flights in the same journal folder.",
        f"expedition_start_timestamp = {_toml_str(data['expedition_start_timestamp'])}",
        "",
        "# The system you depart from (documentation only, not used for tracking).",
        f"expedition_start_system = {_toml_str(data['expedition_start_system'])}",
        "",
        "# Arriving here closes the final leg and marks the expedition complete.",
        f"expedition_end_system = {_toml_str(data['expedition_end_system'])}",
        "",
        "# ── Waypoints ─────────────────────────────────────────────────────────────────",
        "# One [[waypoints]] block per destination. Visited in any order.",
        "# Jumping AWAY from an unvisited waypoint closes the current leg.",
        "",
    ]

    for wp in data["waypoints"]:
        lines += [
            "[[waypoints]]",
            f"label  = {_toml_str(wp['label'])}",
            f"system = {_toml_str(wp['system'])}",
            "",
        ]

    body = "\n".join(lines)

    if rarity:
        body += (
            "# ── Rare / notable first-discovery detector ──────────────────────────────────\n"
            + rarity
        )

    path.write_text(body, encoding="utf-8")


# ── Config window ─────────────────────────────────────────────────────────────

class ConfigWindow:
    def __init__(self, config_path: Path = CONFIG_PATH) -> None:
        self._config_path = config_path
        self._wp_rows: list[dict] = []  # [{frame, label_var, system_var}]

        self.root = tk.Tk()
        self.root.title("Expedition Configuration")
        self.root.resizable(False, True)
        self.root.minsize(620, 400)

        style = ttk.Style(self.root)
        style.theme_use("clam")

        self._build_ui()
        self._load_config()

        # Centre on screen
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = (self.root.winfo_screenwidth()  - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"+{x}+{y}")

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        self._build_commander_section(outer).pack(fill=tk.X, **pad)
        self._build_route_section(outer).pack(fill=tk.X, **pad)
        self._build_waypoints_section(outer).pack(fill=tk.BOTH, expand=True, **pad)
        self._build_buttons(outer).pack(fill=tk.X, padx=12, pady=(0, 12))

    def _build_commander_section(self, parent: ttk.Frame) -> ttk.LabelFrame:
        lf = ttk.LabelFrame(parent, text="Commander & Journals", padding=8)

        self._commander_var = tk.StringVar()
        self._journal_dir_var = tk.StringVar()

        self._field_row(lf, "Commander", self._commander_var, row=0)

        ttk.Label(lf, text="Journal dir").grid(row=1, column=0, sticky=tk.W, pady=3)
        jd_entry = ttk.Entry(lf, textvariable=self._journal_dir_var, width=48)
        jd_entry.grid(row=1, column=1, sticky=tk.EW, padx=(8, 4), pady=3)
        ttk.Button(lf, text="Browse…", width=8,
                   command=self._browse_journal_dir).grid(row=1, column=2, pady=3)

        lf.columnconfigure(1, weight=1)
        return lf

    def _build_route_section(self, parent: ttk.Frame) -> ttk.LabelFrame:
        lf = ttk.LabelFrame(parent, text="Route", padding=8)

        self._start_system_var = tk.StringVar()
        self._end_system_var   = tk.StringVar()
        self._start_ts_var     = tk.StringVar()

        self._field_row(lf, "Start system",      self._start_system_var, row=0)
        self._field_row(lf, "Final destination", self._end_system_var,   row=1)
        self._field_row(lf, "Cutoff timestamp",  self._start_ts_var,     row=2)

        hint = "UTC · format: YYYY-MM-DDTHH:MM:SSZ · find in journal (first FSDJump)"
        ttk.Label(lf, text=hint, foreground="gray").grid(
            row=3, column=1, sticky=tk.W, padx=(8, 0), pady=(0, 2)
        )

        lf.columnconfigure(1, weight=1)
        return lf

    def _build_waypoints_section(self, parent: ttk.Frame) -> ttk.LabelFrame:
        lf = ttk.LabelFrame(parent, text="Waypoints", padding=8)

        # Column headers
        hdr = ttk.Frame(lf)
        ttk.Label(hdr, text="Label",  width=20, anchor=tk.W).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(hdr, text="System", width=32, anchor=tk.W).pack(side=tk.LEFT, padx=(0, 4))
        hdr.pack(fill=tk.X, padx=(0, 24))  # 24 = width of remove button column

        ttk.Separator(lf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(2, 4))

        # Scrollable rows area
        container = ttk.Frame(lf)
        container.pack(fill=tk.BOTH, expand=True)

        self._wp_canvas = tk.Canvas(container, highlightthickness=0, height=180)
        sb = ttk.Scrollbar(container, orient=tk.VERTICAL,
                           command=self._wp_canvas.yview)
        self._wp_canvas.configure(yscrollcommand=sb.set)

        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._wp_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._wp_inner = ttk.Frame(self._wp_canvas)
        self._wp_canvas_win = self._wp_canvas.create_window(
            (0, 0), window=self._wp_inner, anchor=tk.NW
        )

        self._wp_inner.bind("<Configure>", self._on_wp_frame_resize)
        self._wp_canvas.bind("<Configure>", self._on_canvas_resize)

        # Mousewheel scrolling
        self._wp_canvas.bind("<Enter>",  self._bind_mousewheel)
        self._wp_canvas.bind("<Leave>",  self._unbind_mousewheel)

        # Add button
        ttk.Button(lf, text="+ Add waypoint",
                   command=self._add_waypoint_row).pack(anchor=tk.W, pady=(6, 0))

        return lf

    def _build_buttons(self, parent: ttk.Frame) -> ttk.Frame:
        row = ttk.Frame(parent)
        ttk.Button(row, text="Save",   width=10, command=self._save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(row, text="Cancel", width=10, command=self.root.destroy).pack(side=tk.RIGHT)
        return row

    # ── Waypoint row management ───────────────────────────────────────────────

    def _add_waypoint_row(self, label: str = "", system: str = "") -> None:
        label_var  = tk.StringVar(value=label)
        system_var = tk.StringVar(value=system)

        row_frame = ttk.Frame(self._wp_inner)
        row_frame.pack(fill=tk.X, pady=1)

        ttk.Entry(row_frame, textvariable=label_var,  width=20).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Entry(row_frame, textvariable=system_var, width=32).pack(side=tk.LEFT, padx=(0, 4))

        entry = {"frame": row_frame, "label_var": label_var, "system_var": system_var}

        remove_btn = ttk.Button(
            row_frame, text="×", width=3,
            command=lambda e=entry: self._remove_waypoint_row(e),
        )
        remove_btn.pack(side=tk.LEFT)

        self._wp_rows.append(entry)
        self._scroll_to_bottom()

    def _remove_waypoint_row(self, entry: dict) -> None:
        entry["frame"].destroy()
        self._wp_rows.remove(entry)

    # ── Canvas / scroll helpers ───────────────────────────────────────────────

    def _on_wp_frame_resize(self, event: tk.Event) -> None:
        self._wp_canvas.configure(scrollregion=self._wp_canvas.bbox("all"))

    def _on_canvas_resize(self, event: tk.Event) -> None:
        self._wp_canvas.itemconfig(self._wp_canvas_win, width=event.width)

    def _scroll_to_bottom(self) -> None:
        self.root.update_idletasks()
        self._wp_canvas.yview_moveto(1.0)

    def _bind_mousewheel(self, _: tk.Event) -> None:
        self._wp_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _: tk.Event) -> None:
        self._wp_canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        self._wp_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── Field helper ──────────────────────────────────────────────────────────

    def _field_row(self, parent: ttk.Frame, label: str,
                   var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Entry(parent, textvariable=var, width=52).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, padx=(8, 0), pady=3
        )

    # ── Browse ────────────────────────────────────────────────────────────────

    def _browse_journal_dir(self) -> None:
        current = self._journal_dir_var.get()
        chosen = filedialog.askdirectory(
            title="Select Elite Dangerous journal folder",
            initialdir=current if current else None,
        )
        if chosen:
            self._journal_dir_var.set(chosen)

    # ── Load ──────────────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        if not self._config_path.exists():
            return

        import tomllib
        with open(self._config_path, "rb") as fh:
            raw = tomllib.load(fh)

        self._commander_var.set(raw.get("commander", ""))
        self._journal_dir_var.set(raw.get("journal_dir", ""))
        self._start_system_var.set(raw.get("expedition_start_system", ""))
        self._end_system_var.set(raw.get("expedition_end_system", ""))
        self._start_ts_var.set(raw.get("expedition_start_timestamp", ""))

        for wp in raw.get("waypoints", []):
            self._add_waypoint_row(wp.get("label", ""), wp.get("system", ""))

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self) -> None:
        errors = self._validate()
        if errors:
            messagebox.showerror(
                "Validation errors",
                "\n".join(f"• {e}" for e in errors),
                parent=self.root,
            )
            return

        data = {
            "journal_dir":               self._journal_dir_var.get().strip(),
            "commander":                 self._commander_var.get().strip(),
            "expedition_start_timestamp": self._start_ts_var.get().strip(),
            "expedition_start_system":   self._start_system_var.get().strip(),
            "expedition_end_system":     self._end_system_var.get().strip(),
            "waypoints": [
                {
                    "label":  r["label_var"].get().strip(),
                    "system": r["system_var"].get().strip(),
                }
                for r in self._wp_rows
            ],
        }

        try:
            _write_config(self._config_path, data)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self.root)
            return

        messagebox.showinfo(
            "Saved",
            f"Config saved to:\n{self._config_path}\n\n"
            "Restart the tracker for changes to take effect.",
            parent=self.root,
        )
        self.root.destroy()

    def _validate(self) -> list[str]:
        errors: list[str] = []

        if not self._commander_var.get().strip():
            errors.append("Commander name is required.")

        if not self._journal_dir_var.get().strip():
            errors.append("Journal directory is required.")
        elif not Path(self._journal_dir_var.get().strip()).is_dir():
            errors.append("Journal directory does not exist.")

        ts = self._start_ts_var.get().strip()
        if not ts:
            errors.append("Cutoff timestamp is required.")
        elif not _TS_RE.match(ts):
            errors.append("Cutoff timestamp must be in format YYYY-MM-DDTHH:MM:SSZ.")

        if not self._end_system_var.get().strip():
            errors.append("Final destination system is required.")

        for i, row in enumerate(self._wp_rows, 1):
            if not row["label_var"].get().strip():
                errors.append(f"Waypoint {i}: label is required.")
            if not row["system_var"].get().strip():
                errors.append(f"Waypoint {i}: system name is required.")

        return errors

    def run(self) -> None:
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ConfigWindow().run()
