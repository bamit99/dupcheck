"""Find and manage duplicate images with a Tkinter GUI.

Usage:
    python find_duplicates.py

Then pick a folder, adjust the similarity threshold, and scan. Duplicate groups
are shown as thumbnails and can be reviewed, previewed, and deleted (to the
recycle bin by default) or exported to a report.

Features:
  - Folder picker, adjustable similarity threshold, recursive scanning
  - Thumbnail previews with file info (resolution, size)
  - Keep Best auto-selection (largest resolution, then smallest file)
  - Delete to recycle bin with confirmation
  - Export report
  - Incremental scans: hashes are cached per folder, so unchanged folders skip
    scanning entirely ("no_scan") and only changed/new images are re-encoded
    ("delta"). Tick "Full rescan" to re-hash everything.
"""

import os
import json
import hashlib
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
import send2trash
from pathlib import Path
from tkinter import (
    Tk, Toplevel, Frame, Label, Button, Entry, Checkbutton, Scale,
    Scrollbar, Canvas, filedialog, messagebox, StringVar, IntVar,
    BooleanVar, HORIZONTAL, DISABLED, NORMAL, LEFT, RIGHT,
    TOP, BOTTOM, BOTH, X, Y, W, NW, VERTICAL,
    LabelFrame,
)
from PIL import Image, ImageTk

try:
    from imagededup.methods import DHash
except ImportError:
    DHash = None

WINDOW_TITLE = "Duplicate Image Finder"
THUMB_SIZE = (120, 90)
INFO_WIDTH = 220


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def sizeof_fmt(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def get_image_info(path: str) -> tuple[int, int, float]:
    """Return (width, height, file_size_bytes)."""
    try:
        size = os.path.getsize(path)
        with Image.open(path) as img:
            w, h = img.size
        return w, h, size
    except Exception:
        return 0, 0, 0


def open_image_preview(parent, path: str):
    """Open a window showing a larger preview of the image."""
    win = Toplevel(parent)
    win.title(os.path.basename(path))
    try:
        img = Image.open(path)
        img.thumbnail((800, 800))
        photo = ImageTk.PhotoImage(img)
        lbl = Label(win, image=photo)
        lbl.image = photo
        lbl.pack()
        win.bind("<Escape>", lambda e: win.destroy())
    except Exception as exc:
        Label(win, text=f"Cannot preview:\n{exc}").pack(padx=20, pady=20)


# ---------------------------------------------------------------------------
# Duplicate finder core
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".mpo", ".ppm",
    ".tif", ".tiff", ".gif", ".webp",
}


def collect_inventory(image_dir: str, recursive: bool) -> dict[str, tuple[int, int]]:
    """Walk a folder and return {absolute_path: (mtime, size)} for supported image files."""
    root = Path(image_dir).resolve()
    pattern = "**/*" if recursive else "*"
    inventory: dict[str, tuple[int, int]] = {}
    for p in root.glob(pattern):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if p.name.startswith("."):
            continue
        try:
            st = p.stat()
            inventory[str(p)] = (int(st.st_mtime), st.st_size)
        except OSError:
            continue
    return inventory


def silence_library_loggers():
    """Silence imagededup's noisy INFO/WARNING logs (it sets explicit DEBUG levels)."""
    root_manager = logging.Logger.manager
    for name, logger in list(root_manager.loggerDict.items()):
        if name.startswith("imagededup") and isinstance(logger, logging.Logger):
            logger.setLevel(logging.CRITICAL)
            logger.propagate = False


# Cache is tied to a hashing-algorithm version so stale hashes are never reused.
CACHE_VERSION = 1


def _cache_path_for(folder: Path) -> Path:
    cache_dir = Path(
        os.getenv("LOCALAPPDATA", str(Path.home()))
    ) / "dupcheck"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(str(folder).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{key}.json"


def _load_cache_for(folder: Path) -> dict:
    path = _cache_path_for(folder)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("version") != CACHE_VERSION or not isinstance(data.get("encodings"), dict):
        return {}
    return data["encodings"]


def _save_cache_for(folder: Path, inventory: dict, encodings: dict) -> None:
    path = _cache_path_for(folder)
    payload = {
        "version": CACHE_VERSION,
        "folder": str(folder),
        "encodings": {
            p: {"enc": encodings[p], "mtime": m, "size": s}
            for p, (m, s) in inventory.items()
            if p in encodings
        },
    }
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def batch_encode(hasher, paths: list[str], progress_cb=None) -> tuple[dict, int]:
    """Encode a batch of files. Returns ({path: hash}, failed_count)."""
    encodings: dict[str, str] = {}

    def _encode(path: str):
        try:
            return hasher.encode_image(image_file=path)
        except Exception:
            return None

    total = len(paths)
    done = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 1))) as pool:
        for path, result in zip(paths, pool.map(_encode, paths)):
            if result:
                encodings[str(Path(path))] = result
            else:
                failed += 1
            done += 1
            if progress_cb and (done % 25 == 0 or done == total):
                ratio = done / total if total else 1.0
                progress_cb(ratio, f"Encoded {done}/{total} images…")

    return encodings, failed


def build_encodings(hasher, image_dir: str, recursive: bool, fresh: bool,
                    progress_cb=None) -> tuple[dict, dict]:
    """
    Produce hash encodings, using a disk cache so unchanged folders are not
    re-hashed. Returns (encodings, report) where report has:
      mode ('no_scan' | 'delta' | 'fresh'), scanned, delta_encoded,
      cached_used, failed, reused.
    """
    folder = Path(image_dir).resolve()
    inventory = collect_inventory(image_dir, recursive)
    cache = _load_cache_for(folder)

    if fresh or not cache:
        changed = set(inventory)
    else:
        changed = {
            p for p, (m, s) in inventory.items()
            if p not in cache
            or cache[p].get("mtime") != m
            or cache[p].get("size") != s
        }

    encodings: dict[str, str] = {}
    delta_encoded = 0
    failed = 0
    had_cache = bool(cache)

    if had_cache and not fresh and not changed:
        encodings = {p: c["enc"] for p, c in cache.items() if p in inventory}
        mode = "no_scan"
        if progress_cb:
            progress_cb(1.0, f"No changes – reused {len(encodings)} cached scan(s).")
    else:
        to_encode = [p for p in inventory if p in changed]
        if progress_cb:
            progress_cb(
                0.0,
                f"{'Full' if (fresh or not had_cache) else 'Delta'} scan: encoding {len(to_encode)} file(s)…",
            )
        new_enc, failed = batch_encode(hasher, to_encode, progress_cb)
        delta_encoded = len(new_enc)

        if fresh or not had_cache:
            encodings = new_enc
            mode = "fresh"
        else:
            keep = {
                p: c["enc"]
                for p, c in cache.items()
                if p in inventory and p not in changed
            }
            encodings = {**keep, **new_enc}
            mode = "delta"

    _save_cache_for(folder, inventory, encodings)

    cached_used = len(encodings) - delta_encoded if mode != "fresh" else 0
    report = {
        "mode": mode,
        "scanned": len(inventory),
        "encoded": len(encodings),
        "skipped": failed,
        "delta_encoded": delta_encoded,
        "cached_used": cached_used,
    }
    return encodings, report


def _hash_to_int(h: str) -> int:
    try:
        return int(h, 16)
    except ValueError:
        return -1


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class _BkNode:
    __slots__ = ("value", "owners", "children")

    def __init__(self, value: int, owner: str):
        self.value = value
        self.owners = {owner}
        self.children: dict[int, _BkNode] = {}


def _bk_insert(node: _BkNode, value: int, owner: str):
    if node.value == value:
        node.owners.add(owner)
        return
    d = hamming_distance(node.value, value)
    child = node.children.get(d)
    if child is None:
        node.children[d] = _BkNode(value, owner)
    else:
        _bk_insert(child, value, owner)


def _bk_query(node: _BkNode, value: int, owner: str, threshold: int, out: list):
    d = hamming_distance(node.value, value)
    if d <= threshold:
        if owner in node.owners:
            out.extend(o for o in node.owners if o != owner)
        else:
            out.extend(node.owners)
    lo = d - threshold
    hi = d + threshold
    for at in range(lo, hi + 1):
        child = node.children.get(at)
        if child is not None:
            _bk_query(child, value, owner, threshold, out)


def compute_duplicate_pairs(encodings: dict[str, str], threshold: int) -> dict[str, list[str]]:
    """For each image key, all image keys whose dhash Hamming distance is at
    most `threshold`. Pure-Python BKTree search (no multiprocessing)."""
    parsed: dict[str, int] = {}
    for path, h in encodings.items():
        v = _hash_to_int(h)
        if v >= 0:
            parsed[path] = v

    items = list(parsed.items())
    if not items:
        return {}

    root = _BkNode(items[0][1], items[0][0])
    for path, value in items[1:]:
        _bk_insert(root, value, path)

    results: dict[str, list[str]] = {}
    for path, value in items:
        matches: list[str] = []
        _bk_query(root, value, path, threshold, matches)
        results[path] = matches
    return results


def merge_duplicate_groups(duplicates: dict[str, list[str]]) -> list[list[str]]:
    """Union-find merge of overlapping duplicate pairs into connected groups."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for path, dups in duplicates.items():
        for d in dups:
            union(path, d)

    from collections import defaultdict
    raw: dict[str, list[str]] = defaultdict(list)
    seen_files = set(duplicates.keys())
    for dups in duplicates.values():
        seen_files.update(dups)

    for f in seen_files:
        raw[find(f)].append(f)

    return [sorted(g) for g in raw.values() if len(g) > 1]


def find_duplicate_groups(image_dir: str, threshold: int, recursive: bool,
                          fresh: bool = False,
                          progress_cb=None) -> tuple[list[list[str]], dict]:
    """
    Find groups of duplicate images. Returns (groups, stats) where stats is a
    dict with 'scanned', 'encoded', 'skipped', 'mode', 'delta_encoded' and
    'cached_used' counters. Uses an on-disk hash cache per folder so unchanged
    folders (or unchanged images) are not re-hashed.
    """
    if DHash is None:
        raise ImportError("imagededup is not installed.  pip install imagededup")

    silence_library_loggers()

    hasher = DHash(verbose=False)
    encodings, report = build_encodings(
        hasher, image_dir, recursive, fresh, progress_cb
    )

    if not encodings:
        return [], report

    if progress_cb:
        progress_cb(0.99, "Comparing {} hashes…".format(len(encodings)))

    duplicates = compute_duplicate_pairs(encodings, threshold)
    groups = merge_duplicate_groups(duplicates)

    return groups, report


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(Tk):
    def __init__(self):
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry("1100x750")
        self.minsize(900, 600)

        self._groups: list[list[str]] = []
        self._stats: dict = {}
        self._checkbox_vars: list[tuple[str, BooleanVar]] = []
        self._thumbs: list[ImageTk.PhotoImage] = []
        self._scan_running = False

        self._build_ui()

    # ---- UI construction ----

    def _build_ui(self):
        self._build_toolbar()
        self._build_status_bar()
        self._build_results_area()

    def _build_toolbar(self):
        bar = Frame(self, padx=8, pady=6)
        bar.pack(side=TOP, fill=X)

        # Folder
        Label(bar, text="Folder:").pack(side=LEFT)
        self.folder_var = StringVar(value=os.path.expanduser("~"))
        Entry(bar, textvariable=self.folder_var, width=50).pack(side=LEFT, padx=(4, 2), fill=X, expand=True)
        Button(bar, text="Browse…", command=self._browse).pack(side=LEFT, padx=2)

        # Separator
        Frame(bar, width=20).pack(side=LEFT)

        # Threshold
        Label(bar, text="Threshold:").pack(side=LEFT)
        self.threshold_var = IntVar(value=10)
        threshold_scale = Scale(bar, from_=2, to=20, orient=HORIZONTAL,
                                variable=self.threshold_var, length=140, showvalue=True)
        threshold_scale.pack(side=LEFT, padx=2)

        # Recursive
        self.recursive_var = BooleanVar(value=False)
        Checkbutton(bar, text="Recursive", variable=self.recursive_var).pack(side=LEFT, padx=6)

        # Full rescan (otherwise delta / no-scan when unchanged)
        self.fresh_var = BooleanVar(value=False)
        Checkbutton(bar, text="Full rescan", variable=self.fresh_var).pack(side=LEFT, padx=6)

        # Scan
        self.scan_btn = Button(bar, text="Scan", command=self._start_scan, width=10)
        self.scan_btn.pack(side=LEFT, padx=4)

        # Actions
        Frame(bar, width=10).pack(side=LEFT)
        Button(bar, text="Select All", command=self._select_all, width=10).pack(side=LEFT, padx=2)
        Button(bar, text="Deselect All", command=self._deselect_all, width=11).pack(side=LEFT, padx=2)
        Button(bar, text="Keep Best", command=self._keep_best, width=10).pack(side=LEFT, padx=2)
        Button(bar, text="Delete Selected", command=self._delete_selected, width=13).pack(side=LEFT, padx=2)
        Button(bar, text="Export Report", command=self._export_report, width=12).pack(side=LEFT, padx=2)

    def _build_status_bar(self):
        self.status_var = StringVar(value="Ready.")
        bar = Frame(self, padx=8, pady=4)
        bar.pack(side=BOTTOM, fill=X)
        self.progress_label = Label(bar, textvariable=self.status_var, anchor=W)
        self.progress_label.pack(side=LEFT, fill=X, expand=True)

    def _build_results_area(self):
        container = Frame(self)
        container.pack(fill=BOTH, expand=True, padx=6, pady=(4, 0))

        self.canvas = Canvas(container, highlightthickness=0)
        vscroll = Scrollbar(container, orient=VERTICAL, command=self.canvas.yview)
        self.inner_frame = Frame(self.canvas)

        self.inner_frame.bind("<Configure>",
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor=NW)
        self.canvas.configure(yscrollcommand=vscroll.set)

        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        vscroll.pack(side=RIGHT, fill=Y)

        # Mouse-wheel scrolling
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

    # ---- Actions ----

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get(), title="Select image folder")
        if d:
            self.folder_var.set(d)

    def _start_scan(self):
        if self._scan_running:
            return
        self._scan_running = True
        self.scan_btn.config(state=DISABLED)
        self.status_var.set("Scanning…")
        threading.Thread(target=self._run_scan, daemon=True).start()

    def _run_scan(self):
        folder = self.folder_var.get()
        threshold = self.threshold_var.get()
        recursive = self.recursive_var.get()
        fresh = self.fresh_var.get()

        def progress(msg):
            self.after(0, lambda: self.status_var.set(msg))

        try:
            groups, self._stats = find_duplicate_groups(
                folder, threshold, recursive, fresh,
                progress_cb=lambda p, m: progress(m),
            )
        except Exception as exc:
            self.after(0, lambda: messagebox.showerror("Scan Error", str(exc)))
            self.after(0, self._scan_done)
            return

        self._groups = groups
        self.after(0, self._populate_results)
        self.after(0, self._scan_done)

    def _scan_done(self):
        self._scan_running = False
        self.scan_btn.config(state=NORMAL)

    def _scan_mode_label(self) -> str:
        mode = self._stats.get("mode")
        return {
            "no_scan": "No changes – cached results",
            "delta": "Delta scan",
            "fresh": "Full scan",
        }.get(mode, mode or "")

    def _populate_results(self):
        for w in self.inner_frame.winfo_children():
            w.destroy()
        self._checkbox_vars.clear()
        self._thumbs.clear()

        mode_label = self._scan_mode_label()

        if not self._groups:
            Label(self.inner_frame, text="No duplicate groups found.", font=("Segoe UI", 11)).pack(pady=30)
            prefix = f"{mode_label}: " if mode_label else ""
            self.status_var.set(f"{prefix}Done – no duplicates.")
            return

        total_dup_files = sum(len(g) for g in self._groups) - len(self._groups)

        skip_info = ""
        if self._stats.get("skipped"):
            skip_info = f" | {self._stats['skipped']} file(s) skipped (unreadable)"

        cache_info = ""
        if mode_label:
            if self._stats.get("mode") == "no_scan":
                cache_info = " | reused cached hashes"
            elif self._stats.get("delta_encoded"):
                cache_info = (
                    f" | {self._stats['delta_encoded']} re-encoded, "
                    f"{self._stats.get('cached_used', 0)} from cache"
                )

        prefix = f"{mode_label}: " if mode_label else ""
        self.status_var.set(
            f"{prefix}Found {len(self._groups)} group(s) with {total_dup_files} extra file(s)"
            f" ({self._stats.get('encoded', 0)} images){cache_info}{skip_info}."
        )

        for gi, group in enumerate(self._groups):
            self._build_group_widget(gi, group)

    def _build_group_widget(self, gi: int, group: list[str]):
        frame = LabelFrame(self.inner_frame, text=f"Group {gi + 1}  ({len(group)} files)",
                           padx=6, pady=6)
        frame.pack(fill=X, padx=6, pady=4)

        inner = Frame(frame)
        inner.pack(fill=X)

        for filepath in group:
            self._build_file_cell(inner, filepath)

    def _build_file_cell(self, parent, filepath: str):
        cell = Frame(parent, bd=1, relief="groove", padx=4, pady=4)
        cell.pack(side=LEFT, padx=3, pady=3)

        var = BooleanVar(value=True)
        self._checkbox_vars.append((filepath, var))

        # Thumbnail
        thumb_lbl = Label(cell, cursor="hand2")
        thumb_lbl.pack()
        try:
            img = Image.open(filepath)
            img.thumbnail(THUMB_SIZE)
            photo = ImageTk.PhotoImage(img)
            thumb_lbl.image = photo
            self._thumbs.append(photo)
            thumb_lbl.config(image=photo)
            thumb_lbl.bind("<Button-1>", lambda e, p=filepath: open_image_preview(self, p))
        except Exception:
            thumb_lbl.config(text="No preview", width=14, height=6)

        # Filename
        name = os.path.basename(filepath)
        if len(name) > 25:
            name = name[:22] + "…"
        Label(cell, text=name, font=("Segoe UI", 8), wraplength=130).pack()

        # Info
        w, h, sz = get_image_info(filepath)
        info_parts = []
        if w and h:
            info_parts.append(f"{w}×{h}")
        if sz:
            info_parts.append(sizeof_fmt(sz))
        info_text = " | ".join(info_parts) if info_parts else ""
        Label(cell, text=info_text, font=("Segoe UI", 7), fg="gray").pack()

        # Checkbox
        Checkbutton(cell, text="Delete", variable=var).pack()

    # ---- Actions ----

    def _select_all(self):
        for _, var in self._checkbox_vars:
            var.set(True)

    def _deselect_all(self):
        for _, var in self._checkbox_vars:
            var.set(False)

    def _get_selected(self) -> list[str]:
        return [fp for fp, var in self._checkbox_vars if var.get()]

    def _keep_best(self):
        """Auto-select all except the 'best' file per group (highest resolution, then smallest file size)."""
        if not self._groups:
            messagebox.showinfo("Nothing to do", "Run a scan first.")
            return

        for fp, var in self._checkbox_vars:
            var.set(False)

        def _score(path: str) -> tuple[int, int]:
            w, h, sz = get_image_info(path)
            return (w * h, -sz)

        for group in self._groups:
            best = max(group, key=_score)
            for fp, var in self._checkbox_vars:
                if fp in group and fp != best:
                    var.set(True)

        self._refresh_checkboxes()

    def _refresh_checkboxes(self):
        for w in self.inner_frame.winfo_children():
            w.destroy()
        self._thumbs.clear()

        for gi, group in enumerate(self._groups):
            self._build_group_widget(gi, group)

    def _delete_selected(self):
        selected = self._get_selected()
        if not selected:
            messagebox.showinfo("Nothing selected", "No files selected for deletion.")
            return

        count = len(selected)
        paths_preview = "\n".join(os.path.basename(p) for p in selected[:15])
        if count > 15:
            paths_preview += f"\n… and {count - 15} more"

        answer = messagebox.askyesnocancel(
            "Confirm Deletion",
            f"Move {count} file(s) to Recycle Bin?\n\n{paths_preview}",
        )
        if answer is None:
            return

        moved = 0
        errors = []
        for fp in selected:
            try:
                if answer:
                    send2trash.send2trash(fp)
                else:
                    os.remove(fp)
                moved += 1
            except Exception as exc:
                errors.append(f"{fp}: {exc}")

        # Remove deleted files from groups and rebuild
        deleted_set = set(selected)
        self._groups = [
            [f for f in g if f not in deleted_set]
            for g in self._groups
        ]
        self._groups = [g for g in self._groups if len(g) > 1]
        self._checkbox_vars.clear()
        self._populate_results()

        msg = f"Removed {moved} file(s)."
        if errors:
            msg += f"\n\nErrors:\n" + "\n".join(errors[:5])
        self.status_var.set(msg)
        messagebox.showinfo("Done", msg)

    def _export_report(self):
        if not self._groups:
            messagebox.showinfo("Nothing to report", "Run a scan first.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="duplicates_report.txt",
        )
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            f.write("Duplicate Image Report\n")
            f.write(f"Folder: {self.folder_var.get()}\n")
            f.write(f"Threshold: {self.threshold_var.get()}\n")
            f.write(f"Scan type: {self._scan_mode_label() or 'n/a'}\n")
            f.write(f"Groups found: {len(self._groups)}\n\n")
            for gi, group in enumerate(self._groups, 1):
                f.write(f"Group {gi} ({len(group)} files):\n")
                for fp in group:
                    w, h, sz = get_image_info(fp)
                    info = ""
                    if w and h:
                        info = f"  [{w}x{h} {sizeof_fmt(sz)}]"
                    f.write(f"  {fp}{info}\n")
                f.write("\n")

        self.status_var.set(f"Report saved to {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = App()
    app.mainloop()
