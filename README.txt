432 Hz Converter (Audio + Video, GUI)

What it does
- Converts audio pitch from 440 Hz to 432 Hz while preserving duration (no sync drift).
- Works on audio files AND video files.
- For videos: copies video + subtitles + metadata; re-encodes audio only.
- Output goes to a new folder next to the source: <sourcefolder>_432Hz (originals untouched).

Quick start (Windows)
1) Double-click install.bat
2) Double-click run.bat
3) Select a SOURCE FOLDER (batch) and press Start

Notes
- "Output in same folder" writes results next to each file (suffix _432).
- "Replace originals (.bak)" replaces the original files after conversion and keeps a backup (file.ext.bak, .bak1, ...).
- "Recursive" is ON by default (so subfolders are included).
- If FFmpeg isn't installed, the app auto-downloads it into ./vendors/ffmpeg/bin on first run.
- Output filenames have a _432 suffix.

Build EXE (optional)
- Double-click build_exe.bat
