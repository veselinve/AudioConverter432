from __future__ import annotations

import argparse
import os
import re
import subprocess
import shutil
import sys
import threading
import unittest
from pathlib import Path
from typing import List, Optional, Tuple, \
    Union  # Union might be needed for PopenResult type hints for older Pythons, but | is fine for 3.10+
import logging
import json  # Added for parsing ffprobe JSON output
import zipfile
import tempfile
import time
import urllib.request

# =============================================================================
# 0.  FFmpeg resolver
# =============================================================================
_FFMPEG = "ffmpeg"
_FFPROBE = "ffprobe"


# Logging setup function
def _setup_logging():
    log_file_name = "app_converter.log"
    try:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            log_file_path = Path(sys.executable).parent / log_file_name
        else:
            log_file_path = Path(__file__).resolve().parent / log_file_name
    except Exception:  # Fallback if path resolution fails for some reason
        log_file_path = Path(log_file_name)

    logging.basicConfig(
        filename=str(log_file_path),
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(lineno)d - %(message)s",
        filemode='a'
    )

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    formatter = logging.Formatter('%(levelname)s: %(message)s')
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)

    logging.info("Application logging initialized.")
    logging.info(f"Initial FFmpeg path: {_FFMPEG}, FFprobe path: {_FFPROBE}")



def _download_ffmpeg_windows(dest_bin_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """Download a known-good FFmpeg build on Windows into dest_bin_dir.

    This keeps the project portable (no global install required).
    """
    try:
        dest_bin_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_exe = dest_bin_dir / "ffmpeg.exe"
        ffprobe_exe = dest_bin_dir / "ffprobe.exe"
        if ffmpeg_exe.exists() and ffprobe_exe.exists():
            return ffmpeg_exe, ffprobe_exe

        url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        logging.warning(f"FFmpeg not found. Attempting auto-download: {url}")

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            zip_path = td_path / "ffmpeg.zip"
            urllib.request.urlretrieve(url, zip_path)  # nosec - expected download
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(td_path)

            # Find extracted ffmpeg*/bin
            bin_dir: Optional[Path] = None
            for p in td_path.iterdir():
                if p.is_dir() and p.name.lower().startswith("ffmpeg"):
                    cand = p / "bin"
                    if (cand / "ffmpeg.exe").is_file() and (cand / "ffprobe.exe").is_file():
                        bin_dir = cand
                        break
            if not bin_dir:
                logging.error("Auto-download succeeded but could not locate ffmpeg.exe/ffprobe.exe inside the zip.")
                return None, None

            shutil.copy2(bin_dir / "ffmpeg.exe", ffmpeg_exe)
            shutil.copy2(bin_dir / "ffprobe.exe", ffprobe_exe)

        logging.warning(f"Auto-downloaded FFmpeg into: {dest_bin_dir}")
        return ffmpeg_exe, ffprobe_exe
    except Exception as e:
        logging.warning(f"Auto-download failed: {e}")
        return None, None


def _resolve_ffmpeg(ffmpeg_arg: Optional[Path | str]) -> None:
    global _FFMPEG, _FFPROBE
    logging.info(f"Attempting to resolve FFmpeg. Argument provided: {ffmpeg_arg}")

    ffmpeg_exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    ffprobe_exe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"

    resolved_ffmpeg_path: Optional[str] = None
    resolved_ffprobe_path: Optional[str] = None
    source_of_resolution = "default"

    if ffmpeg_arg:
        logging.info(f"Checking --ffmpeg argument: {ffmpeg_arg}")
        p_arg = Path(ffmpeg_arg).expanduser().resolve()
        temp_ffmpeg: Optional[Path] = None
        temp_ffprobe: Optional[Path] = None

        if p_arg.is_file():
            temp_ffmpeg = p_arg
            temp_ffprobe = p_arg.with_name(
                ffprobe_exe_name.split('.')[0] + p_arg.suffix)
            logging.info(f"Argument is a file. ffmpeg: {temp_ffmpeg}, ffprobe guess: {temp_ffprobe}")
        elif p_arg.is_dir():
            temp_ffmpeg = p_arg / ffmpeg_exe_name
            temp_ffprobe = p_arg / ffprobe_exe_name
            logging.info(f"Argument is a directory. ffmpeg: {temp_ffmpeg}, ffprobe: {temp_ffprobe}")

        if temp_ffmpeg and temp_ffmpeg.is_file() and \
                temp_ffprobe and temp_ffprobe.is_file():
            resolved_ffmpeg_path = str(temp_ffmpeg.resolve())
            resolved_ffprobe_path = str(temp_ffprobe.resolve())
            source_of_resolution = f"argument '{ffmpeg_arg}'"
            logging.info(f"Resolved from argument: ffmpeg='{resolved_ffmpeg_path}', ffprobe='{resolved_ffprobe_path}'")
        else:
            logging.warning(f"Could not resolve ffmpeg/ffprobe from argument path: {ffmpeg_arg}")

    if not resolved_ffmpeg_path:
        logging.info("Checking System PATH for ffmpeg/ffprobe.")
        path_ffmpeg = shutil.which(ffmpeg_exe_name)
        path_ffprobe = shutil.which(ffprobe_exe_name)
        if path_ffmpeg and path_ffprobe:
            resolved_ffmpeg_path = path_ffmpeg
            resolved_ffprobe_path = path_ffprobe
            source_of_resolution = "System PATH"
            logging.info(
                f"Resolved from System PATH: ffmpeg='{resolved_ffmpeg_path}', ffprobe='{resolved_ffprobe_path}'")
        else:
            logging.info("Not found in System PATH.")

    if not resolved_ffmpeg_path:
        logging.info("Checking bundled layouts for ffmpeg/ffprobe.")
        source_of_resolution = "bundled files"

        # Determine one or more roots to search for bundled/portable FFmpeg.
        # - Script mode: directory of main.py
        # - Frozen mode: both the unpacked bundle dir and the EXE directory
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            bundle_root = Path(sys._MEIPASS)
            exe_root = Path(sys.executable).resolve().parent
            search_roots = [bundle_root, exe_root]
            logging.info(f"Application is frozen. bundle_root={bundle_root}, exe_root={exe_root}")
        else:
            try:
                script_root = Path(__file__).resolve().parent
            except NameError:  # if __file__ is not defined (e.g. interactive session)
                script_root = Path.cwd()
                logging.warning(f"__file__ not defined, using current working directory for bundled search: {script_root}")
            search_roots = [script_root]

        # 1) Prefer project-local portable FFmpeg if present: ./vendors/ffmpeg/bin/{ffmpeg,ffprobe}.exe
        for root in search_roots:
            vendors_bin = root / "vendors" / "ffmpeg" / "bin"
            vend_ffmpeg = vendors_bin / ffmpeg_exe_name
            vend_ffprobe = vendors_bin / ffprobe_exe_name
            logging.info(f"Checking vendors FFmpeg under: {vendors_bin}")
            if vend_ffmpeg.is_file() and vend_ffprobe.is_file():
                resolved_ffmpeg_path = str(vend_ffmpeg.resolve())
                resolved_ffprobe_path = str(vend_ffprobe.resolve())
                logging.info(f"Resolved from vendors/ffmpeg/bin under: {root}")
                break

        # 2) If missing on Windows, try auto-download into a writable vendors folder (script dir or EXE dir).
        if not resolved_ffmpeg_path and os.name == "nt":
            dest_root = search_roots[-1]  # prefer EXE dir when frozen, else script dir
            vendors_bin = dest_root / "vendors" / "ffmpeg" / "bin"
            dl_ffmpeg, dl_ffprobe = _download_ffmpeg_windows(vendors_bin)
            if dl_ffmpeg and dl_ffprobe and dl_ffmpeg.is_file() and dl_ffprobe.is_file():
                resolved_ffmpeg_path = str(dl_ffmpeg.resolve())
                resolved_ffprobe_path = str(dl_ffprobe.resolve())
                logging.info(f"Resolved after auto-download into: {vendors_bin}")

        # 3) Check next to script/executable root(s): ./ffmpeg(.exe), ./ffprobe(.exe)
        if not resolved_ffmpeg_path:
            for root in search_roots:
                script_dir_ffmpeg = root / ffmpeg_exe_name
                script_dir_ffprobe = root / ffprobe_exe_name
                logging.info(f"Checking next to root: ffmpeg='{script_dir_ffmpeg}', ffprobe='{script_dir_ffprobe}'")
                if script_dir_ffmpeg.is_file() and script_dir_ffprobe.is_file():
                    resolved_ffmpeg_path = str(script_dir_ffmpeg.resolve())
                    resolved_ffprobe_path = str(script_dir_ffprobe.resolve())
                    logging.info(f"Resolved from root directory: {root}")
                    break

        # 4) Check subdirectories named ffmpeg*: ./ffmpeg-xyz/bin/ffmpeg(.exe)
        if not resolved_ffmpeg_path:
            logging.info("Checking 'ffmpeg*' subdirectories for ffmpeg/ffprobe.")
            for root in search_roots:
                try:
                    for item in root.iterdir():
                        if item.is_dir() and item.name.lower().startswith("ffmpeg"):
                            bin_dir_ffmpeg = item / "bin" / ffmpeg_exe_name
                            bin_dir_ffprobe = item / "bin" / ffprobe_exe_name
                            root_dir_ffmpeg = item / ffmpeg_exe_name
                            root_dir_ffprobe = item / ffprobe_exe_name

                            if bin_dir_ffmpeg.is_file() and bin_dir_ffprobe.is_file():
                                resolved_ffmpeg_path = str(bin_dir_ffmpeg.resolve())
                                resolved_ffprobe_path = str(bin_dir_ffprobe.resolve())
                                logging.info(f"Resolved from subdirectory bin: {item}")
                                break
                            if root_dir_ffmpeg.is_file() and root_dir_ffprobe.is_file():
                                resolved_ffmpeg_path = str(root_dir_ffmpeg.resolve())
                                resolved_ffprobe_path = str(root_dir_ffprobe.resolve())
                                logging.info(f"Resolved from subdirectory root: {item}")
                                break
                    if resolved_ffmpeg_path:
                        break
                except Exception as e:
                    logging.info(f"Skipping subdir scan under {root}: {e}")

    if resolved_ffmpeg_path and resolved_ffprobe_path:
        _FFMPEG = resolved_ffmpeg_path
        _FFPROBE = resolved_ffprobe_path
        logging.info(f"FFmpeg resolved to: {_FFMPEG}")
        logging.info(f"FFprobe resolved to: {_FFPROBE}")
    else:
        logging.warning(f"Could not resolve ffmpeg/ffprobe using any method. Using defaults: {_FFMPEG}, {_FFPROBE}")

    final_ffmpeg_executable = shutil.which(_FFMPEG)
    if not final_ffmpeg_executable:
        search_locations_tried = [
            f"  - Argument --ffmpeg: {ffmpeg_arg if ffmpeg_arg else 'not provided (or path invalid)'}",
            f"  - System PATH for '{ffmpeg_exe_name}'",
            f"  - Next to script: {Path(__file__).resolve().parent / ffmpeg_exe_name if '__file__' in globals() else 'N/A (interactive?)'}",
            "  - In 'ffmpeg*' subdirectories (e.g., ./ffmpeg-xyz/ffmpeg.exe or ./ffmpeg-xyz/bin/ffmpeg.exe)",
        ]
        error_message = (
                f"FFmpeg ('{_FFMPEG}') not found or not executable. Last attempt based on: {source_of_resolution}.\n"
                "Search locations checked (in order):\n" +
                "\n".join(search_locations_tried) +
                "\n\nPlease ensure ffmpeg is installed, accessible via PATH, bundled correctly, or specified via --ffmpeg."
        )
        logging.critical(error_message)
        sys.exit(1)
    _FFMPEG = final_ffmpeg_executable
    logging.info(f"Final verified FFmpeg executable: {_FFMPEG}")

    final_ffprobe_executable = shutil.which(_FFPROBE)
    if not final_ffprobe_executable:
        search_locations_tried = [
            f"  - Argument --ffmpeg (for ffprobe near ffmpeg): {ffmpeg_arg if ffmpeg_arg else 'not provided (or path invalid)'}",
            f"  - System PATH for '{ffprobe_exe_name}'",
            f"  - Next to script: {Path(__file__).resolve().parent / ffprobe_exe_name if '__file__' in globals() else 'N/A (interactive?)'}",
            "  - In 'ffmpeg*' subdirectories (e.g., ./ffmpeg-xyz/ffprobe.exe or ./ffmpeg-xyz/bin/ffprobe.exe)",
        ]
        error_message = (
                f"ffprobe ('{_FFPROBE}') not found or not executable. Last attempt based on: {source_of_resolution}.\n"
                "Search locations checked (in order):\n" +
                "\n".join(search_locations_tried) +
                "\n\nPlease ensure ffprobe is installed (usually with ffmpeg), accessible via PATH, bundled correctly, or specified via --ffmpeg."
        )
        logging.critical(error_message)
        sys.exit(1)
    _FFPROBE = final_ffprobe_executable
    logging.info(f"Final verified FFprobe executable: {_FFPROBE}")


# =============================================================================
# 1.  Helpers – codecs, sanitised stderr, file operations
# =============================================================================

class PopenResult:
    """A class to mimic subprocess.CompletedProcess for Popen."""

    def __init__(self, args: List[str], returncode: int, stdout: Optional[bytes | str], stderr: Optional[bytes | str]):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _popen_run(cmd: List[str], capture_output: bool = False, text: bool = False, check: bool = False,
               errors: Optional[str] = None, **kwargs) -> PopenResult:
    """
    Runs a command using subprocess.Popen, mimicking subprocess.run,
    but with CREATE_NO_WINDOW flag on Windows to prevent console flashing.
    """
    creationflags = 0
    if os.name == 'nt':
        creationflags = subprocess.CREATE_NO_WINDOW

    cmd_str = [str(c) for c in cmd]  # Ensure all command parts are strings

    # Popen's 'errors' argument is only used if text=True (or universal_newlines=True)
    popen_errors = errors if text else None

    try:
        process = subprocess.Popen(
            cmd_str,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
            errors=popen_errors,
            creationflags=creationflags,
            **kwargs
        )
        stdout, stderr = process.communicate()
        returncode = process.returncode
    except FileNotFoundError as e:
        # Mimic FileNotFoundError behavior of subprocess.run if executable not found
        logging.error(f"Error running command {' '.join(cmd_str)}: {e}", exc_info=True)
        err_msg = f"Executable not found: {cmd_str[0]}"
        return PopenResult(args=cmd_str, returncode=e.errno if hasattr(e, 'errno') else 1,
                           stdout=b'' if not text else '', stderr=err_msg.encode() if not text else err_msg)
    except Exception as e:  # Catch other Popen-related errors
        logging.error(f"Unexpected error running command {' '.join(cmd_str)}: {e}", exc_info=True)
        err_msg = str(e)
        return PopenResult(args=cmd_str, returncode=1, stdout=b'' if not text else '',
                           stderr=err_msg.encode() if not text else err_msg)

    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd_str, output=stdout, stderr=stderr)

    return PopenResult(args=cmd_str, returncode=returncode, stdout=stdout, stderr=stderr)


# Backwards-compatible list of common media extensions.
# NOTE: We *do not* rely solely on extensions anymore; we also probe files with ffprobe.
KNOWN_MEDIA_EXTS = {
    # Common audio
    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".wma", ".ogg", ".opus",
    ".aif", ".aiff", ".aifc", ".caf", ".amr", ".au", ".snd",
    ".mp1", ".mp2", ".mp4", ".m4b", ".m4v", ".3gp", ".3g2",
    ".ac3", ".dts",
    ".ape", ".wv", ".tta",
    # Common video containers that may contain audio-only content or music videos
    ".mkv", ".webm", ".mov", ".avi", ".ts", ".m2ts",
    # Ogg family variants
    ".oga", ".ogv",
    # Matroska audio
    ".mka",
}

# Extensions we can *output* to while keeping the same container/extension.
# For other inputs, we default output to .mp3 for maximum compatibility.
OUTPUT_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".wma", ".ogg", ".opus"}

# Extensions where video streams are valid/expected in the container.
VIDEO_CONTAINER_EXTS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi", ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".mpe", ".m2v", ".vob", ".wmv", ".asf", ".flv", ".f4v",
    ".3gp", ".3g2", ".ogv",
}

# A small ignore list so we don't ffprobe obvious non-media files in large folders.
IGNORE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".txt", ".md", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".json", ".xml", ".html", ".htm", ".css", ".js", ".py", ".ini", ".log",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
    ".exe", ".dll",
    ".bak",
}



def _choose_output_ext(input_ext: str, has_real_video: bool) -> str:
    """Decide output extension.

    - If the source has real video, keep the container extension (so we can copy the video stream).
    - If the input is a supported audio output type, keep it.
    - Otherwise default to .mp3 for broad compatibility.
    """
    in_ext = (input_ext or "").lower()
    if has_real_video:
        return in_ext or ".mp4"
    if in_ext == ".wma":
        return ".mp3"
    if in_ext in OUTPUT_EXTS:
        return in_ext
    return ".mp3"

_FFMPEG_IGNORE_LINES = re.compile(
    r"^(ffmpeg version\b|"
    r"built with\b|"
    r"configuration:|"
    r"libav\w+\s+.*?\s+/\s+.*|"
    r"\s*--(?:[a-zA-Z0-9_-]+=.*|[\w-]+)|"
    r"Incorrect BOM value|"
    r"Error reading comment frame, skipped)"
)


def _clean_ffmpeg_err(raw: bytes) -> str:
    lines = raw.decode(errors="ignore").splitlines()
    meaningful_lines = [ln for ln in lines if not _FFMPEG_IGNORE_LINES.match(ln)]
    return "\n".join(meaningful_lines[-15:]) if meaningful_lines else "(no ffmpeg stderr captured)"


def _unique_backup_path(p: Path) -> Path:
    """Return a non-existing backup path like 'file.ext.bak', 'file.ext.bak1', ..."""
    base = p.with_name(p.name + ".bak")
    if not base.exists():
        return base
    for i in range(1, 1000):
        cand = p.with_name(p.name + f".bak{i}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"Too many backups already exist for: {p}")


def _replace_original_with_backup(original: Path, new_file: Path) -> None:
    """Atomically replace 'original' with 'new_file', keeping a .bak copy of the original."""
    backup = _unique_backup_path(original)
    try:
        os.replace(str(original), str(backup))
        os.replace(str(new_file), str(original))
    except Exception:
        # Best-effort rollback: if original is missing but backup exists, restore it.
        try:
            if not original.exists() and backup.exists():
                os.replace(str(backup), str(original))
        except Exception:
            pass
        raise



HQ_CODEC: dict[str, List[str]] = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "320k", "-compression_level", "0"],
    ".m4a": ["-c:a", "aac", "-b:a", "512k", "-movflags", "+faststart"],
    ".aac": ["-c:a", "aac", "-b:a", "512k"],
    ".flac": ["-c:a", "flac", "-compression_level", "8"],
    ".wav": ["-c:a", "pcm_s24le"],
    ".wma": ["-c:a", "wmapro", "-b:a", "320k"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    ".opus": ["-c:a", "libopus", "-b:a", "192k"],

# Video containers (audio will be re-encoded; video/subs copied)
".mkv": ["-c:a", "flac", "-compression_level", "8"],
".mp4": ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"],
".m4v": ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"],
".mov": ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"],
".webm": ["-c:a", "libopus", "-b:a", "192k"],
".avi": ["-c:a", "aac", "-b:a", "320k"],
".ts": ["-c:a", "aac", "-b:a", "320k"],
".m2ts": ["-c:a", "aac", "-b:a", "320k"],
".mts": ["-c:a", "aac", "-b:a", "320k"],
".mpg": ["-c:a", "aac", "-b:a", "320k"],
".mpeg": ["-c:a", "aac", "-b:a", "320k"],
".wmv": ["-c:a", "aac", "-b:a", "320k"],

}

SAFE_CODEC: dict[str, List[str]] = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    ".m4a": ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"],
    ".aac": ["-c:a", "aac", "-b:a", "256k"],
    ".flac": ["-c:a", "flac"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".wma": ["-c:a", "wmav2", "-b:a", "192k"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "4"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],

# Video containers (safer bitrate, video/subs copied)
".mkv": ["-c:a", "flac"],
".mp4": ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"],
".m4v": ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"],
".mov": ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"],
".webm": ["-c:a", "libopus", "-b:a", "128k"],
".avi": ["-c:a", "aac", "-b:a", "256k"],
".ts": ["-c:a", "aac", "-b:a", "256k"],
".m2ts": ["-c:a", "aac", "-b:a", "256k"],
".mts": ["-c:a", "aac", "-b:a", "256k"],
".mpg": ["-c:a", "aac", "-b:a", "256k"],
".mpeg": ["-c:a", "aac", "-b:a", "256k"],
".wmv": ["-c:a", "aac", "-b:a", "256k"],

}


def _codec_for_ext(ext: str, safe: bool = False) -> List[str]:
    ext = ext.lower()
    default_codec = ["-c:a", "aac", "-b:a", "320k"]
    if ext in [".flac", ".wav"]:  # For lossless, default to safe copy-like options if not specified
        default_codec = SAFE_CODEC.get(ext, ["-c:a", "copy"])  # Should ideally not happen if ext is supported

    return (SAFE_CODEC if safe else HQ_CODEC).get(ext, default_codec)


def find_audio_files(folder: Path, recursive: bool) -> List[Path]:
    """Return only files that actually contain at least one audio stream.

    We *prefer* extensions for a quick pre-filter, but ultimately we verify with ffprobe.
    This makes the app work with FLAC and essentially any format FFmpeg can decode.
    """
    pattern = "**/*" if recursive else "*"
    out: List[Path] = []
    for p in folder.glob(pattern):
        if not p.is_file():
            continue

        # Avoid re-processing output folders placed inside the source tree.
        if any(str(part).endswith("_432Hz") for part in p.parts):
            continue

        # Skip partial torrent chunk files and other non-media temp files by name.
        if p.name.startswith("~BitTorrentPartFile_"):
            continue

        # Skip temp files created during in-place replacement.
        if ".__tmp432__" in p.name:
            continue

        # Skip already-converted outputs when "same folder" mode is used.
        # (Output naming appends "_432" or "_432Hz" to the stem.)
        stem_l = p.stem.lower()
        if stem_l.endswith("_432") or stem_l.endswith("_432hz") or stem_l.endswith("_432_hz"):
            continue

        ext = p.suffix.lower()
        if ext in IGNORE_EXTS:
            continue

        # If the extension is unknown, we still try probing (supports odd/rare formats).
        # If the extension is known media, probing is expected.
        if ext and ext not in KNOWN_MEDIA_EXTS:
            # Still probe, but skip clearly non-media extensions above.
            pass

        has_audio, *_ = _probe_media_info(p)
        if has_audio:
            out.append(p)
    return out


# Cache to avoid repeated ffprobe calls on the same file during a run.
_MEDIA_INFO_CACHE: dict[Path, Tuple[bool, bool, bool, Optional[int], Optional[int]]] = {}


def _probe_media_info(path: Path) -> Tuple[bool, bool, bool, Optional[int], Optional[int]]:
    """Probe media streams.

    Returns: (has_audio, has_real_video, has_attached_pic, sample_rate_hz, bit_rate_bps)

    - "real video" excludes embedded cover art (attached_pic).
    - sample_rate_hz is guaranteed when has_audio=True (falls back to 44100 if needed).
    """
    p = path.expanduser().resolve()
    if p in _MEDIA_INFO_CACHE:
        return _MEDIA_INFO_CACHE[p]

    has_audio = False
    has_real_video = False
    has_attached_pic = False
    sample_rate: Optional[int] = None
    bit_rate: Optional[int] = None

    # --- Method 1: ffprobe JSON (best: can detect attached pics) ---
    cmd_probe = [
        _FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_streams", str(p)
    ]
    try:
        process = _popen_run(cmd_probe, capture_output=True, text=True, check=False, errors="ignore")
        if process.returncode == 0 and process.stdout:
            ffprobe_output = json.loads(process.stdout)
            for stream in ffprobe_output.get("streams", []) or []:
                ctype = stream.get("codec_type")
                if ctype == "audio" and not has_audio:
                    has_audio = True
                    sr_str = stream.get("sample_rate")
                    if sr_str and str(sr_str).isdigit():
                        sample_rate = int(sr_str)
                    br_str = stream.get("bit_rate")
                    if br_str and str(br_str).isdigit():
                        bit_rate = int(br_str)
                elif ctype == "video":
                    disp = stream.get("disposition") or {}
                    try:
                        attached = int(disp.get("attached_pic", 0)) == 1
                    except Exception:
                        attached = False
                    if attached:
                        has_attached_pic = True
                    else:
                        has_real_video = True
            if has_audio and sample_rate is None:
                sample_rate = 44100

            result = (has_audio, has_real_video, has_attached_pic, sample_rate, bit_rate)
            _MEDIA_INFO_CACHE[p] = result
            return result
    except Exception as e:
        logging.warning(f"ffprobe probe failed for {p.name}: {e}", exc_info=False)

    # --- Method 2: ffmpeg -i parse (fallback) ---
    cmd_ffmpeg = [_FFMPEG, "-i", str(p)]
    try:
        process = _popen_run(cmd_ffmpeg, capture_output=True, text=True, check=False, errors="ignore")
        output = process.stderr or ""
        if "Audio:" in output:
            has_audio = True
        if "Video:" in output:
            has_real_video = True
        if has_audio:
            sr_match = re.search(r"(\d+)\s*Hz", output)
            br_match = re.search(r"(\d+)\s*kb/s", output)
            if sr_match:
                sample_rate = int(sr_match.group(1))
            if br_match:
                bit_rate = int(br_match.group(1)) * 1000
            if sample_rate is None:
                sample_rate = 44100
    except Exception as e:
        logging.warning(f"ffmpeg probe failed for {p.name}: {e}", exc_info=False)

    result = (has_audio, has_real_video, has_attached_pic, sample_rate, bit_rate)
    _MEDIA_INFO_CACHE[p] = result
    return result


def _get_audio_metadata(path: Path) -> Tuple[Optional[int], Optional[int]]:
    # Backwards-compatible wrapper used by older code paths.
    has_audio, _, _, sr, br = _probe_media_info(path)
    if not has_audio:
        return None, None
    return sr, br


# =============================================================================
# 2.  Conversion function with automatic retry
# =============================================================================

def convert_to_432(
        src: Path,
        dst: Path,
        original_sr: int,
        target_sr: int,
        original_bitrate_bps: Optional[int],
        *,
        has_real_video: bool = False,
        has_attached_pic: bool = False,
) -> None:
    """Convert audio pitch from 440→432 Hz while preserving duration.

    - For videos: copies video + subtitle streams (keeps original timing/sync) and re-encodes audio only.
    - For audio: converts audio and preserves cover art where possible.
    """
    logging.info(f"Converting {src} to {dst} with original SR {original_sr}, target SR {target_sr}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Duration‑preserving pitch shift (keeps video + subtitles perfectly synced):
    #   1) Change sample rate (changes pitch + speed)
    #   2) atempo compensates the speed back to original duration
    #   3) resample to target sample rate
    ratio = 432 / 440
    chain = f"asetrate={original_sr}*{ratio},atempo={1/ratio},aresample={target_sr}"

    def _run(cmd_list: List[str]):
        logging.debug(f"Executing ffmpeg command: {' '.join(cmd_list)}")
        return _popen_run(cmd_list, capture_output=True, text=False)

    def _adjust_bitrate_in_options(codec_options: List[str], orig_bps: Optional[int]) -> List[str]:
        if orig_bps is None:
            return codec_options
        adjusted_options = list(codec_options)
        try:
            b_a_idx = adjusted_options.index("-b:a")
            if b_a_idx + 1 < len(adjusted_options):
                target_br_str = adjusted_options[b_a_idx + 1]
                target_bps = -1
                if target_br_str.lower().endswith("k"):
                    target_bps = int(target_br_str[:-1]) * 1000
                elif target_br_str.isdigit():
                    target_bps = int(target_br_str)
                if target_bps != -1 and orig_bps < target_bps:
                    adjusted_options[b_a_idx + 1] = f"{orig_bps // 1000}k"
                    logging.info(
                        f"Adjusting target bitrate from {target_br_str} to original {adjusted_options[b_a_idx + 1]} for {src.name}"
                    )
        except (ValueError, IndexError):
            pass
        return adjusted_options

    ext = dst.suffix.lower()
    is_video_container = ext in VIDEO_CONTAINER_EXTS

    # Build base command (mapping/stream copy strategy)
    if has_real_video and is_video_container:
        # Preserve everything we can (subtitles, attachments, chapters, metadata), re-encode audio.
        if ext == ".mkv":
            map_args = ["-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-copy_unknown"]
            stream_copy_args = ["-c", "copy"]
        else:
            # Safer mapping for non-MKV containers (some don't support attachments)
            map_args = [
                "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:d?",
                "-map_metadata", "0", "-map_chapters", "0",
            ]
            stream_copy_args = ["-c:v", "copy", "-c:s", "copy", "-c:d", "copy"]

        base_cmd_list = [_FFMPEG, "-y", "-i", str(src)] + map_args + stream_copy_args + ["-filter:a", chain]
    else:
        # Audio-only outputs: map audio (and optional cover art where supported).
        map_args: List[str] = ["-map", "0:a?"]
        if (not has_real_video) and has_attached_pic and ext in {".mp3", ".m4a", ".flac"}:
            map_args += ["-map", "0:v?", "-c:v", "copy"]
        base_cmd_list = [_FFMPEG, "-y", "-i", str(src)] + map_args + ["-af", chain]

    # Try HQ first
    hq_options = _adjust_bitrate_in_options(_codec_for_ext(ext, safe=False), original_bitrate_bps)
    hq_cmd_list = base_cmd_list + hq_options + [str(dst)]
    proc = _run(hq_cmd_list)
    if proc.returncode == 0:
        logging.info(f"Successfully converted {src.name} (HQ settings) to {dst.name}")
        return

    hq_stderr_cleaned = _clean_ffmpeg_err(proc.stderr) if proc.stderr is not None else "(no ffmpeg stderr captured)"

    # Subtitle fallback for MP4/MOV when "copy" isn't supported for the subtitle codec
    if has_real_video and is_video_container and ext in {".mp4", ".m4v", ".mov"}:
        err_blob = proc.stderr or b""
        err_lower = err_blob.lower() if isinstance(err_blob, (bytes, bytearray)) else str(err_blob).lower()
        if ("subtitle" in err_lower) and (
            "codec" in err_lower or "not supported" in err_lower or "could not write header" in err_lower
        ):
            logging.warning(f"Subtitle copy unsupported for {dst.name}. Retrying with mov_text subtitles.")
            hq_cmd_list_sub = base_cmd_list + ["-c:s", "mov_text"] + hq_options + [str(dst)]
            proc_sub = _run(hq_cmd_list_sub)
            if proc_sub.returncode == 0:
                logging.info(f"Successfully converted {src.name} (HQ + mov_text subs) to {dst.name}")
                return

    logging.warning(f"HQ conversion failed for {src.name}. Retrying with safe settings. Error:\n{hq_stderr_cleaned}")

    safe_options = _adjust_bitrate_in_options(_codec_for_ext(ext, safe=True), original_bitrate_bps)
    safe_cmd_list = base_cmd_list + safe_options + [str(dst)]
    proc_safe = _run(safe_cmd_list)
    if proc_safe.returncode == 0:
        logging.info(f"Successfully converted {src.name} (Safe preset) to {dst.name}")
        return

    safe_stderr_cleaned = _clean_ffmpeg_err(proc_safe.stderr) if proc_safe.stderr is not None else "(no ffmpeg stderr captured)"
    final_err_message = f"HQ error:\n{hq_stderr_cleaned}\nSafe error:\n{safe_stderr_cleaned}"
    raise subprocess.CalledProcessError(proc_safe.returncode, proc_safe.args, stderr=final_err_message)

# =============================================================================
# 3.  GUI and Drag‑and‑drop
# =============================================================================
try:
    import tkinter as _tk
    from tkinter import filedialog as _filedialog, messagebox as _messagebox, ttk as _ttk
except (ModuleNotFoundError, ImportError):
    _tk = _filedialog = _messagebox = _ttk = None

if _tk is not None:
    try:
        from tkinterdnd2 import DND_FILES as _DND_FILES, TkinterDnD as _TkDnD
    except (ModuleNotFoundError, ImportError):
        _TkDnD = None
        _DND_FILES = "Files"
else:
    _TkDnD = None
    _DND_FILES = "Files"


def _parse_drop_data(data: str) -> Optional[Path]:
    if not data: return None
    s = data.strip()
    if s.startswith("{") and s.endswith("}") and len(s) > 1: s = s[1:-1].strip()
    if not s: return None
    if '\n' in s: s = s.split('\n')[0].strip()
    if not Path(s).exists():
        logging.warning(f"Parsed drop data '{s}' does not exist as a path.")
        return None
    return Path(s)


if _tk is not None and _messagebox is not None and _filedialog is not None and _ttk is not None:
    class _ConverterGUI:
        def __init__(self, root: "_tk.Tk", args: argparse.Namespace) -> None:
            self.root = root
            self.args = args
            root.title("Batch 432 Hz Converter")
            root.resizable(False, False)
            self.src: Optional[Path] = None
            self.dst_base: Optional[Path] = None
            self.thread: Optional[threading.Thread] = None
            self._build()
            self._apply_initial_args()

        def _build(self):
            pad = {"padx": 10, "pady": 4}
            f_src = _ttk.Frame(self.root)
            f_src.grid(row=0, column=0, sticky="ew", **pad)
            _ttk.Label(f_src, text="Source ▶").pack(side="left")
            self.var_src = _tk.StringVar()
            self.entry_src = _ttk.Entry(f_src, textvariable=self.var_src, width=48)
            self.entry_src.pack(side="left", fill="x", expand=True, padx=(5, 0))
            _ttk.Button(f_src, text="Folder…", command=self._browse_src_folder).pack(side="left", padx=(5, 0))
            _ttk.Button(f_src, text="File…", command=self._browse_src_file).pack(side="left", padx=(5, 0))

            if _TkDnD is not None and hasattr(self.entry_src, 'drop_target_register'):
                self.entry_src.drop_target_register(_DND_FILES)
                self.entry_src.dnd_bind('<<Drop>>', self._ondrop_src)

            f_out = _ttk.Frame(self.root)
            f_out.grid(row=1, column=0, sticky="ew", **pad)
            _ttk.Label(f_out, text="Output ▶").pack(side="left")
            self.var_out = _tk.StringVar()
            self.entry_out = _ttk.Entry(f_out, textvariable=self.var_out, width=48)
            self.entry_out.pack(side="left", fill="x", expand=True, padx=(5, 0))
            _ttk.Button(f_out, text="Browse…", command=self._browse_out).pack(side="left", padx=(5, 0))

            f_opt = _ttk.Frame(self.root)
            f_opt.grid(row=2, column=0, sticky="w", **pad)
            self.rec = _tk.BooleanVar(value=True)  # default ON (batch folders usually contain subfolders)
            self.keep = _tk.BooleanVar(value=self.args.keep)
            self.same_folder = _tk.BooleanVar(value=False)  # default OFF (A: output to *_432Hz)
            self.replace_original = _tk.BooleanVar(value=getattr(self.args, 'replace_original', False))  # replace originals (creates .bak)
            _ttk.Checkbutton(f_opt, text="Recursive", variable=self.rec).pack(side="left")
            _ttk.Checkbutton(f_opt, text="Output in same folder", variable=self.same_folder, command=self._auto_set_output).pack(side="left", padx=(10, 0))
            _ttk.Checkbutton(f_opt, text="Replace originals (.bak)", variable=self.replace_original, command=self._auto_set_output).pack(side="left", padx=(10, 0))
            _ttk.Checkbutton(f_opt, text="Skip existing", variable=self.keep).pack(side="left", padx=(10, 0))

            self.bar = _ttk.Progressbar(self.root, length=420, mode="determinate")
            self.bar.grid(row=3, column=0, **pad)
            self.btn = _ttk.Button(self.root, text="Start", command=self._start)
            self.btn.grid(row=4, column=0, **pad)

        def _apply_initial_args(self):
            if self.args.folder:
                p = Path(self.args.folder).resolve()
                if p.exists():
                    self._set_src(p)
                else:
                    _messagebox.showwarning("Warning",
                                            f"The provided source path does not exist:\n{self.args.folder}")
            if self.args.outdir:
                # If user explicitly supplied output, respect it.
                self._set_out(Path(self.args.outdir).resolve())

        def _browse_src_folder(self):
            d = _filedialog.askdirectory(title="Select Source Folder")
            if d:
                self._set_src(Path(d))

        def _browse_src_file(self):
            f = _filedialog.askopenfilename(
                title="Select Media File",
                filetypes=[
                    ("Media files", "*.*"),
                ],
            )
            if f:
                self._set_src(Path(f))

        def _browse_out(self):
            d = _filedialog.askdirectory(title="Select Output Base Folder")
            if d: self._set_out(Path(d))

        def _ondrop_src(self, event):
            p = _parse_drop_data(event.data)
            if p:
                self._set_src(p)

        def _set_src(self, p: Path):
            self.src = p.resolve()
            self.var_src.set(str(self.src))
            # Always auto-update output when source changes (as requested)
            self._auto_set_output()

        def _set_out(self, p: Path):
            self.dst_base = p.resolve()
            self.var_out.set(str(self.dst_base))

        def _auto_set_output(self):
            """Auto-set output folder based on current source and the 'Output in same folder' checkbox."""
            if not self.src:
                return
            try:
                # If replacing originals, output base is informational only.
                if getattr(self, 'replace_original', None) is not None and self.replace_original.get():
                    out_dir = self.src if self.src.is_dir() else self.src.parent
                    self._set_out(Path(out_dir))
                    return
                if self.same_folder.get():
                    # Output next to sources; originals remain untouched because we suffix filenames.
                    out_dir = self.src if self.src.is_dir() else self.src.parent
                else:
                    if self.src.is_dir():
                        out_dir = self.src.parent / f"{self.src.name}_432Hz"
                    else:
                        parent = self.src.parent
                        out_dir = parent.parent / f"{parent.name}_432Hz"
                self._set_out(Path(out_dir))
            except Exception as e:
                logging.warning(f"Failed to auto-set output folder: {e}")

        def _start(self):
            if self.thread and self.thread.is_alive():
                _messagebox.showwarning("Busy", "Conversion is already in progress.")
                return
            if not self.src or not self.src.exists():
                _messagebox.showerror("Error", "Please select a valid source folder or a single media file.")
                return

            # If replacing originals, output base is informational only; conversions happen via temp files next to each source.
            if self.replace_original.get():
                self.dst_base = (self.src if self.src.is_dir() else self.src.parent).resolve()
                self.var_out.set(str(self.dst_base))
            else:
                self.dst_base = Path(self.var_out.get()).resolve()
                if not self.dst_base:
                    _messagebox.showerror("Error", "Please select an output folder.")
                    return
                try:
                    self.dst_base.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    _messagebox.showerror("Error", f"Cannot create output folder:\n{self.dst_base}\n{e}")
                    return

            self.btn.config(state="disabled")
            self.bar["value"] = 0
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()
            self._poll()

        def _poll(self):
            if self.thread and self.thread.is_alive():
                self.root.after(200, self._poll)
            else:
                self.btn.config(state="normal")
                if self.bar["value"] == self.bar["maximum"] and self.bar["maximum"] > 0:
                    _messagebox.showinfo("Done", f"Conversion complete!\nFiles are in: {self.dst_base}")
                self.bar["value"] = 0

        def _worker(self):
            assert self.src and self.dst_base, "Source or destination not set"
            files = [self.src] if (self.src and self.src.is_file()) else find_audio_files(self.src, self.rec.get())
            total = len(files)
            if not total:
                self.root.after(0, lambda: _messagebox.showinfo("Info", "No media files with audio were found in the selected folder."))
                return

            self.bar.config(maximum=total)
            errors_occurred = False
            for idx, f_path in enumerate(files, 1):
                self.root.after(0, lambda i=idx: self.bar.config(value=i))

                has_audio, has_real_video, has_attached_pic, original_sample_rate, original_bitrate = _probe_media_info(f_path)
                if not has_audio:
                    # Shouldn't happen because find_audio_files filters, but be defensive.
                    logging.info(f"Skipping non-audio file after probe: {f_path}")
                    continue

                ext_out = _choose_output_ext(f_path.suffix, has_real_video)

                # Compute destination path:
                # - Replace originals: write a temp file next to each source, then swap in-place (original kept as .bak)
                # - Folder mode: preserve relative subfolder structure under dst_base
                # - Single-file mode: place into dst_base (or next to source when same_folder=True)
                if self.replace_original.get():
                    # Keep the same container/extension when replacing originals.
                    ext_out = f_path.suffix
                    dst_file = f_path.with_name(f"{f_path.stem}.__tmp432__{ext_out}")
                elif self.src and self.src.is_dir():
                    rel_path = f_path.relative_to(self.src)
                    if self.same_folder.get():
                        dst_file = f_path.parent / f"{f_path.stem}_432{ext_out}"
                    else:
                        dst_file = self.dst_base / rel_path.with_name(f"{f_path.stem}_432{ext_out}")
                else:
                    rel_path = Path(f_path.name)
                    if self.same_folder.get():
                        dst_file = f_path.parent / f"{f_path.stem}_432{ext_out}"
                    else:
                        dst_file = self.dst_base / rel_path.with_name(f"{f_path.stem}_432{ext_out}")

                try:
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    logging.error(f"GUI Worker: Error creating directory for {dst_file.name}: {e}", exc_info=True)
                    self.root.after(0, lambda f=dst_file.name, m=e: _messagebox.showerror("File Error",
                                                                                          f"Could not create directory for:\n{f}\n\n{m}"))
                    errors_occurred = True
                    continue

                if self.keep.get() and dst_file.exists() and dst_file.stat().st_size > 0:
                    logging.info(f"GUI Worker: Skipping existing file: {dst_file}")
                    continue

                target_sample_rate = 48000
                try:
                    # CORRECTED: Pass the original sample rate to the conversion function.
                    convert_to_432(
                        f_path,
                        dst_file,
                        int(original_sample_rate) if original_sample_rate else 44100,
                        target_sample_rate,
                        original_bitrate,
                        has_real_video=has_real_video,
                        has_attached_pic=has_attached_pic,
                    )
                    if self.replace_original.get():
                        try:
                            _replace_original_with_backup(f_path, dst_file)
                            logging.info(f"Replaced original with 432Hz version (backup created): {f_path.name}")
                        finally:
                            # If something went wrong and temp still exists, clean it up.
                            if dst_file.exists() and dst_file.name.find('.__tmp432__') != -1:
                                try:
                                    dst_file.unlink(missing_ok=True)
                                except Exception:
                                    pass

                except subprocess.CalledProcessError as e_conv:
                    if self.replace_original.get() and '.__tmp432__' in dst_file.name:
                        try:
                            dst_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                    logging.error(f"GUI Worker: Conversion failed for {f_path.name}: {e_conv.stderr}", exc_info=False)
                    self.root.after(0,
                                    lambda p=f_path.name, err=e_conv.stderr: _messagebox.showerror("Conversion Error",
                                                                                                   f"Failed to convert:\n{p}\n\nError:\n{err}"))
                    errors_occurred = True
                except Exception as e_gen:
                    if self.replace_original.get() and '.__tmp432__' in dst_file.name:
                        try:
                            dst_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                    logging.error(f"GUI Worker: Unexpected error converting {f_path.name}: {e_gen}", exc_info=True)
                    self.root.after(0, lambda p=f_path.name, err=e_gen: _messagebox.showerror("Conversion Error",
                                                                                              f"Unexpected error converting:\n{p}\n\nError:\n{err}"))
                    errors_occurred = True

            if errors_occurred:
                self.root.after(0, lambda: _messagebox.showwarning("Done",
                                                                   "Conversion finished, but some files had errors. Check log."))


# =============================================================================
# 4.  Arg‑parser & tests
# =============================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="440→432 Hz batch converter (GUI)")
    p.add_argument("folder", type=Path, nargs="?", default=None,
                   help="Optional: Source folder OR a single media file to pre-fill in GUI.")
    p.add_argument("-r", "--recursive", action="store_true", help="Pre-select recursive search in GUI.")
    p.add_argument("--keep", action="store_true", help="Pre-select skipping existing files in GUI.")
    p.add_argument("--replace", dest="replace_original", action="store_true",
                   help="Pre-select replacing originals (creates .bak backups) in GUI.")
    p.add_argument("--ffmpeg", dest="ffmpeg_path", type=Path,
                   help="Path to ffmpeg folder/executable for FFmpeg/FFprobe resolution.")
    p.add_argument("--out", dest="outdir", type=Path, help="Optional: Destination base folder to pre-fill in GUI.")
    p.add_argument("--test", action="store_true", help="Run self‑tests and exit.")
    return p


class _Tests(unittest.TestCase):
    def test_codec_quality(self):
        self.assertIn("-b:a", _codec_for_ext(".mp3"))
        self.assertIn("512k", _codec_for_ext(".aac"))
        self.assertTrue(_codec_for_ext(".wma"))
        self.assertTrue(_codec_for_ext(".wma", safe=True))

    def test_parse_drop_data(self):
        Path("./tmp_test_dir").mkdir(exist_ok=True)
        self.assertEqual(_parse_drop_data("{./tmp_test_dir}"), Path("./tmp_test_dir"))
        Path("./tmp_test_dir").rmdir()


def _run_tests():
    logging.info("Running self-tests...")
    try:
        _resolve_ffmpeg(None)
    except SystemExit:
        logging.warning("FFmpeg/FFprobe not found during test setup. Some tests might be limited.")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTest(loader.loadTestsFromTestCase(_Tests))
    runner = unittest.TextTestRunner(verbosity=2)
    rc = runner.run(suite)
    sys.exit(0 if rc.wasSuccessful() else 1)


# =============================================================================
# 5.  Entrypoint
# =============================================================================

def main():
    _setup_logging()
    args = _build_parser().parse_args()
    logging.info(f"Application started with arguments: {args}")

    if args.test:
        _run_tests()
        return

    try:
        _resolve_ffmpeg(args.ffmpeg_path)
    except SystemExit:
        if _tk and _messagebox:
            _messagebox.showerror("FFmpeg Error", "FFmpeg/FFprobe not found. Please see app_converter.log for details.")
        else:
            print("CRITICAL: FFmpeg/FFprobe not found. Check log.", file=sys.stderr)
        sys.exit(1)

    if _tk is None:
        logging.critical("Tkinter components not available. GUI cannot run.")
        print("CRITICAL ERROR: Tkinter is not installed or not working correctly.", file=sys.stderr)
        sys.exit(1)

    root_tk_instance = None
    try:
        if _TkDnD is not None:
            root_tk_instance = _TkDnD.Tk()
        else:
            root_tk_instance = _tk.Tk()
            logging.warning("TkinterDnD not available, drag and drop will not work.")
    except _tk.TclError as e:
        logging.critical(f"Failed to initialize Tkinter root window: {e}", exc_info=True)
        print(f"CRITICAL ERROR: Failed to initialize Tkinter root window: {e}", file=sys.stderr)
        sys.exit(1)

    if root_tk_instance:
        logging.info("Starting GUI.")
        _ConverterGUI(root_tk_instance, args)
        root_tk_instance.mainloop()
        logging.info("GUI mainloop finished.")


if __name__ == "__main__":
    main()
