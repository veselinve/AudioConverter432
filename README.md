# 432 Hz Batch Converter 🎵

**Turn whole music folders from standard 440 Hz to 432 Hz with a single click.**
A Tk-based GUI, ships its own FFmpeg so users don’t have to install anything.

“When you want to find the secrets of the Universe, think in terms of frequency and vibration.” — Nikola Tesla

This little alchemical app retunes your music library from the modern 440 Hz standard back to A = 432 Hz — a tuning many listeners describe as calmer, heart‑opening, and cosmically aligned. Drop in an album, press start, and let your songs breathe a more natural resonance.

🌟 Why 432 Hz?

Feeling

Science & Lore

Soothing body & mind

A‑432 sits ~8 Hz below modern pitch — the same delta as the brain’s alpha–theta threshold. Many meditators report deeper relaxation.

Golden ratio geometry

1 + 1 / φ² ≈ 0 . 432… — the number weaves through sacred art and nature’s spirals.

Planetary resonance

432 Hz × 60 = 25 920 Hz, echoing Earth’s precessional cycle (the “Great Year”).

(Whether you call it metaphysics, psychoacoustics, or just a nicer vibe, try for yourself — ears over theory!)

## ✨ Features

| | |
|---|---|
| **Drag-and-drop GUI** | Pick source & output folders, progress bar, optional recursion. |
| **Smart FFmpeg resolver** | Finds `ffmpeg.exe`/`ffprobe.exe` next to the app, inside *ffmpeg-* sub-folders, or on **PATH**; override with `--ffmpeg`. |
| **HQ → Safe fallback** | First tries highest-quality codec flags, then retries with safe presets if the build lacks a feature. |
| **Keeps original bit-rate** | Reads bitrate with `ffprobe`; avoids unwanted up/down-sizing. |
| **No flashing consoles** | All FFmpeg calls run with *CREATE_NO_WINDOW* on Windows. |
| **Portable EXE** | `pyinstaller --onefile`, bundles FFmpeg; double-click to run on PCs without Python. |
| **Verbose logging** | `app_converter.log` written beside the EXE; warnings surface in GUI. |

---

## 📦 Quick start (source)

```bash
# clone & enter
$ git clone https://github.com/YOUR-NAME/432hz-converter.git
$ cd 432hz-converter

# create isolated env (optional but recommended)
$ python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate

# install runtime deps
$ pip install -r requirements.txt  # tkinterdnd2, tqdm

# run the GUI
$ python batch_432_converter.py
```
---

## 🛠 Build a portable EXE (Windows)

```bash
pip install pyinstaller

pyinstaller --onefile --noconsole \
           --add-binary "ffmpeg/ffmpeg.exe;." \
           --add-binary "ffmpeg/ffprobe.exe;." \
           --hidden-import tkinterdnd2 \
           --icon assets/icon.ico \
           batch_432_converter.py
```

Result appears in `dist/batch_432_converter.exe`.

---

## 🖥 Screenshot

<img width="677" height="205" alt="converter" src="https://github.com/user-attachments/assets/fcf41390-c320-47ca-abdb-b27aa659c335" />



---

## 📚 Code layout

```text
batch_432_converter.py   main script (GUI)
ffmpeg/                  put static FFmpeg build here (ffmpeg.exe, ffprobe.exe)
assets/                  icons, screenshots
requirements.txt         runtime deps for developers
```
---

## 📄 License

MIT © 2025 Veselinve
