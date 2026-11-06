# Dupcheck — Duplicate Image Finder

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A desktop GUI for finding and managing duplicate images using
[perceptual hashing (dHash)](https://pypi.org/project/imagededup/).

Pick a folder, scan, review visual thumbnail groups, and delete — or let the app
auto-select the best copy to keep in every group.

## Features

- **Folder picker** with recursive scanning of subfolders
- **Similarity threshold slider** — from exact duplicates (low) to visually
  similar edit copies (high)
- **Thumbnail grid** per duplicate group with resolution + file size
- **Click-to-preview** any image in a larger viewer
- **Keep Best** — auto-selects every file except the best copy per group
  (highest resolution, then smallest file)
- **Delete to Recycle Bin** (or permanent delete) with confirmation
- **Incremental scans** — hashes are cached per folder, so:
  - **Fresh scan** the first time a folder is seen
  - **No scan** (instant) when nothing changed
  - **Delta scan** — only new/changed images are re-hashed
- **Export report** of all groups with file info
- Background scanning keeps the UI responsive

## How it works

1. Images are collected by extension and encoded with
   [DHash](https://pypi.org/project/imagededup/) — a perceptual hash that maps
   each image to a 64-bit value.
2. A pure-Python **BK-tree** finds all image pairs whose Hamming distance is at
   or below the threshold. No GPU or process spawning required.
3. A union-find pass merges overlapping matches into connected duplicate
   groups.
4. Group results are cached on disk; unchanged images are never re-hashed.

Supported formats: `JPEG/JPG`, `PNG`, `BMP`, `TIFF/TIF`, `WEBP`, `GIF`, `MPO`,
`PPM`. Non-image files are ignored; corrupted files are skipped and counted.

## Requirements

- Python 3.9+ (`tkinter` ships with Python on Windows/macOS; on Linux install
  it via your package manager, e.g. `sudo apt install python3-tk`)
- `imagededup`, `Pillow`, `send2trash` (see `requirements.txt`)

## Installation

```bash
# (Optional but recommended) isolated environment
conda create -n dupcheck python=3.11
conda activate dupcheck

git clone https://github.com/bamit99/dupcheck.git
cd dupcheck
pip install -r requirements.txt
```

## Usage

**Windows:** double-click `run.bat`.

**Or from a terminal:**

```bash
python find_duplicates.py
```

1. Click **Browse…** and choose a folder (optionally tick **Recursive** to scan
   subfolders).
2. Adjust **Threshold** if needed (lower = stricter, default 10 = exact-ish
   duplicates).
3. Click **Scan**.
4. Review each group, untick any file you want to keep, then **Delete Selected**
   (goes to Recycle Bin) — or use **Keep Best** to auto-select.
5. **Export Report** saves the current results to a text file.

> Tip: re-running **Scan** on an unchanged folder is instant (cached hashes).
> Tick **Full rescan** to force re-hashing of everything.

## Cache location

Hash caches are stored under `%LOCALAPPDATA%\dupcheck` (Windows) or
`~/.dupcheck` (other platforms), keyed by folder path. Deleting a cache file
just forces a fresh scan on the next run.

## Troubleshooting

- **`ModuleNotFoundError: No module named 'tkinter'`** — install Tk for your
  Python (Linux: `sudo apt install python3-tk`).
- **`imagededup` install fails** — it pulls in NLP/testing dependencies; the
  core needs are `pillow`, `numpy`, and `pandas`. If in doubt, use a fresh
  virtual environment.
- **Nothing found when expecting duplicates** — lower the threshold value, or
  tick **Full rescan** after changing the folder's files outside the app.

## License

[MIT](LICENSE) © 2026 [Amit Bhatnagar](https://github.com/bamit99)