from __future__ import annotations

import argparse
import os
import re
import subprocess
import shutil
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from typing import Callable, List, Optional, Tuple
import logging
from logging.handlers import RotatingFileHandler
import json  # Added for parsing ffprobe JSON output
import zipfile
import tempfile
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

    file_handler = RotatingFileHandler(
        str(log_file_path), maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(lineno)d - %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler])

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


_OVERLAY_MAGIC = b"AC432PAYLOAD"


def _read_embedded_payload() -> Optional[bytes]:
    """Read the FFmpeg zip appended after the PyInstaller archive (overlay).

    Disk layout: <PyInstaller EXE><zip bytes><magic><u64 zip length>. Appending
    data after the archive leaves the bootloader untouched, unlike Win32
    resource updates, which corrupt the executable.
    """
    if not getattr(sys, 'frozen', False):
        return None
    try:
        exe = Path(sys.executable)
        size = exe.stat().st_size
        with open(exe, "rb") as f:
            f.seek(-8, 2)  # last 8 bytes: u64 zip length
            tail = f.read(8)
            if len(tail) < 8:
                return None
            zip_len = int.from_bytes(tail, "little")
            f.seek(-8 - len(_OVERLAY_MAGIC), 2)
            if f.read(len(_OVERLAY_MAGIC)) != _OVERLAY_MAGIC:
                return None
            if zip_len <= 0 or zip_len > size:
                return None
            f.seek(-8 - len(_OVERLAY_MAGIC) - zip_len, 2)
            return f.read(zip_len)
    except Exception as e:
        logging.debug(f"Could not read embedded payload: {e}")
        return None


def _ensure_cached_ffmpeg() -> Tuple[Optional[Path], Optional[Path]]:
    """One-time extraction of the embedded FFmpeg payload into a local cache.

    The fast single-EXE layout stores ffmpeg.exe/ffprobe.exe as a compressed
    zip resource inside the executable; they are unpacked once into
    %LOCALAPPDATA% and reused from there, so the heavy payload never slows
    down application startup again.
    """
    if os.name != "nt":
        return None, None
    cache_bin = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AudioConverter432" / "bin"
    ffmpeg_exe = cache_bin / "ffmpeg.exe"
    ffprobe_exe = cache_bin / "ffprobe.exe"
    marker = cache_bin / "payload.txt"

    payload = _read_embedded_payload()
    if payload is None:
        return None, None

    # Re-extract when the payload version changes (size fingerprint).
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            def _read_entry(want: str) -> Optional[bytes]:
                for n in zf.namelist():
                    if Path(n).name.lower() == want:
                        return zf.read(n)
                return None

            ff_data = _read_entry("ffmpeg.exe")
            fp_data = _read_entry("ffprobe.exe")
            if not ff_data or not fp_data:
                logging.error("Embedded FFmpeg payload is missing ffmpeg.exe/ffprobe.exe.")
                return None, None

            expected = f"{len(ff_data)}:{len(fp_data)}"
            if ffmpeg_exe.is_file() and ffprobe_exe.is_file() and \
                    marker.is_file() and marker.read_text(encoding="utf-8") == expected:
                return ffmpeg_exe, ffprobe_exe  # cache already up to date

            cache_bin.mkdir(parents=True, exist_ok=True)
            ffmpeg_exe.write_bytes(ff_data)
            ffprobe_exe.write_bytes(fp_data)
            marker.write_text(expected, encoding="utf-8")
        logging.info(f"Extracted embedded FFmpeg payload into cache: {cache_bin}")
        return ffmpeg_exe, ffprobe_exe
    except Exception as e:
        logging.warning(f"Could not extract embedded FFmpeg payload: {e}")
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

        # 2) Check next to script/executable root(s): ./ffmpeg(.exe), ./ffprobe(.exe)
        # (fast layout: small EXE with the two binaries sitting beside it)
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

        # 3) Check subdirectories named ffmpeg*: ./ffmpeg-xyz/bin/ffmpeg(.exe)
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

        # 4) Single-EXE fast layout: payload embedded as a compressed resource,
        #    extracted once into a local cache on first run.
        if not resolved_ffmpeg_path and getattr(sys, 'frozen', False):
            logging.info("Checking embedded FFmpeg payload cache.")
            c_ffmpeg, c_ffprobe = _ensure_cached_ffmpeg()
            if c_ffmpeg and c_ffprobe:
                resolved_ffmpeg_path = str(c_ffmpeg)
                resolved_ffprobe_path = str(c_ffprobe)
                logging.info("Resolved from embedded FFmpeg payload cache.")

        # 5) Last resort on Windows: auto-download into a writable vendors folder.
        if not resolved_ffmpeg_path and os.name == "nt":
            dest_root = search_roots[-1]  # prefer EXE dir when frozen, else script dir
            vendors_bin = dest_root / "vendors" / "ffmpeg" / "bin"
            dl_ffmpeg, dl_ffprobe = _download_ffmpeg_windows(vendors_bin)
            if dl_ffmpeg and dl_ffprobe and dl_ffmpeg.is_file() and dl_ffprobe.is_file():
                resolved_ffmpeg_path = str(dl_ffmpeg.resolve())
                resolved_ffprobe_path = str(dl_ffprobe.resolve())
                logging.info(f"Resolved after auto-download into: {vendors_bin}")

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
    - If the input is a supported audio format, keep it — outputs stay in the
      same format as the original (e.g. .wma stays .wma).
    - Exotic formats FFmpeg cannot encode default to .mp3 for broad compatibility.
    """
    in_ext = (input_ext or "").lower()
    if has_real_video:
        return in_ext or ".mp4"
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


def _short_error(err: object, limit: int = 200) -> str:
    """One-line, shortened error text for summary dialogs; the log keeps the full detail."""
    text = " ".join((str(err) if err is not None else "unknown error").split())
    if len(text) > limit:
        text = text[:limit - 3].rstrip() + "..."
    return text


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


def _has_existing_backup(p: Path) -> bool:
    """True if a backup from a previous 'replace originals' run already exists."""
    return p.with_name(p.name + ".bak").exists()


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
    ".wma": ["-c:a", "wmav2", "-b:a", "256k"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    ".opus": ["-c:a", "libopus", "-b:a", "192k"],

# Video containers (video/subs copied; audio re-encoded to a sensible lossy
# codec — the source audio is usually already compressed AAC/Opus, so lossless
# FLAC here would only bloat the file without restoring anything)
".mkv": ["-c:a", "aac", "-b:a", "320k"],
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
".mkv": ["-c:a", "aac", "-b:a", "256k"],
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


def find_audio_files(
        folder: Path,
        recursive: bool,
        progress_cb: Optional[Callable[[int, Path], bool]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
) -> List[Path]:
    """Return only files that actually contain at least one audio stream.

    We *prefer* extensions for a quick pre-filter, but ultimately we verify with ffprobe.
    This makes the app work with FLAC and essentially any format FFmpeg can decode.

    progress_cb (called as progress_cb(found_so_far, current_path) per candidate) may
    return False to abort the scan; should_stop is checked by the probe workers too,
    so a cancel stops quickly even mid-scan.
    """
    pattern = "**/*" if recursive else "*"
    candidates: List[Path] = []
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
        candidates.append(p)

    # Unknown extensions are still probed; this supports rare formats, while obvious
    # non-media files were already skipped above. ffprobe subprocesses dominate the
    # scan cost, so probe candidates in parallel.
    probe = _probe_has_audio if should_stop is None else partial(_probe_has_audio, _stop=should_stop)
    out: List[Path] = []
    workers = max(1, min(8, os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for p, has_audio in zip(candidates, ex.map(probe, candidates)):
            if has_audio:
                out.append(p)
            if progress_cb is not None:
                try:
                    if progress_cb(len(out), p) is False:
                        break
                except Exception:
                    pass
    return out


def _probe_has_audio(p: Path, _stop: Optional[Callable[[], bool]] = None) -> bool:
    if _stop is not None and _stop():
        return False
    has_audio, *_ = _probe_media_info(p)
    return has_audio


def _media_duration_seconds(path: Path) -> Optional[float]:
    """Container duration in seconds via ffprobe (None when unavailable)."""
    cmd = [_FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
    try:
        proc = _popen_run(cmd, capture_output=True, text=True, errors="ignore")
        if proc.returncode == 0 and proc.stdout:
            dur = json.loads(proc.stdout).get("format", {}).get("duration")
            if dur is not None:
                return float(dur)
    except Exception as e:
        logging.debug(f"Duration probe failed for {path.name}: {e}")
    return None


def _open_in_file_manager(path: Path) -> None:
    """Open a folder in the platform file manager (best effort)."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # Windows-only API
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        logging.warning(f"Could not open folder in file manager: {path} ({e})")


def _parallel_workers() -> int:
    """Parallel conversions, scaled to the CPU and capped at 6.

    Each ffmpeg audio conversion uses roughly one core, so one worker per
    (logical core - 1) is a good fit. Degrades to 1 on small machines, which
    matches the old sequential behaviour.
    """
    return max(1, min(6, (os.cpu_count() or 2) - 1))


def _estimate_audio_bitrate_bps(path: Path) -> Optional[int]:
    """Audio bitrate from summed packet sizes.

    MKV and friends often do not report a per-stream bitrate; without this the
    re-encode would use the full preset bitrate and the output file would end
    up noticeably larger than the source.
    """
    try:
        cmd = [_FFPROBE, "-v", "quiet", "-print_format", "json",
               "-select_streams", "a:0",
               "-show_entries", "packet=size:format=duration", str(path)]
        proc = _popen_run(cmd, capture_output=True, text=True, errors="ignore")
        if proc.returncode != 0 or not proc.stdout:
            return None
        data = json.loads(proc.stdout)
        total = sum(int(pkt.get("size") or 0) for pkt in (data.get("packets") or []))
        dur = (data.get("format") or {}).get("duration")
        if total <= 0 or not dur:
            return None
        bps = int(total * 8 / float(dur))
        return bps if bps > 0 else None
    except Exception as e:
        logging.debug(f"Audio bitrate estimation failed for {path.name}: {e}")
        return None


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
            if has_audio and bit_rate is None:
                bit_rate = _estimate_audio_bitrate_bps(p)

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

    if has_audio and bit_rate is None:
        bit_rate = _estimate_audio_bitrate_bps(p)
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

# Methods for the 440→432 Hz shift, in GUI-label form.
METHOD_LABELS = {
    "auto": "Auto (best available)",
    "rubberband": "Quality (Rubber Band)",
    "pure": "Purist (slower tempo)",
    "atempo": "Fast (atempo)",
}

_FFMPEG_FILTERS_CACHE: Optional[set] = None


def _ffmpeg_has_filter(name: str) -> bool:
    """True when the resolved ffmpeg build exposes the given filter."""
    global _FFMPEG_FILTERS_CACHE
    if _FFMPEG_FILTERS_CACHE is None:
        proc = _popen_run([_FFMPEG, "-hide_banner", "-filters"],
                          capture_output=True, text=True, errors="ignore")
        if proc.returncode != 0 or not proc.stdout:
            logging.warning("Could not list ffmpeg filters; assuming optional filters are missing.")
            return False
        # Filter lines look like " T.C rubberband   Apply Rubber Band ..." — name is token 2.
        _FFMPEG_FILTERS_CACHE = {ln.split()[1] for ln in str(proc.stdout).splitlines() if len(ln.split()) >= 2}
    return name in _FFMPEG_FILTERS_CACHE


def _resolve_method(method: str, has_real_video: bool = False) -> str:
    if method != "auto":
        return method
    if not _ffmpeg_has_filter("rubberband"):
        return "atempo"
    # Auto: highest quality for music, where the difference is audible; fast
    # atempo for videos, whose long audio tracks make Rubber Band's extra CPU
    # cost very noticeable for little audible benefit.
    return "atempo" if has_real_video else "rubberband"


def _audio_filter_chain(original_sr: int, target_sr: int, method: str) -> str:
    ratio = 432 / 440
    if method == "pure":
        # Turntable-style: pitch and speed drop together. No time-stretch DSP at all
        # (zero artifacts), at the cost of +1.85% duration.
        return f"asetrate={original_sr}*{ratio:.6f},aresample={target_sr}"
    if method == "rubberband":
        # Phase-vocoder shift with channels analysed together (keeps the stereo image).
        return f"rubberband=pitch={ratio:.6f}:channels=together"
    # Classic fast method: resample down, then time-stretch back to the original length.
    return f"asetrate={original_sr}*{ratio:.6f},atempo={1/ratio:.6f},aresample={target_sr}"


def convert_to_432(
        src: Path,
        dst: Path,
        original_sr: int,
        target_sr: int,
        original_bitrate_bps: Optional[int],
        *,
        has_real_video: bool = False,
        has_attached_pic: bool = False,
        method: str = "auto",
) -> None:
    """Pitch audio from the 440 Hz to the 432 Hz reference.

    Methods (see _audio_filter_chain): "rubberband" (highest quality, duration
    preserved), "pure" (turntable-style, zero DSP artifacts, +1.85% duration),
    "atempo" (classic fast), "auto" (rubberband when available, else atempo).

    - For videos: copies video + subtitle streams (keeps original timing/sync) and re-encodes audio only.
    - For audio: converts audio and preserves cover art where possible.
    """
    resolved = _resolve_method(method, has_real_video=has_real_video)
    logging.info(f"Converting {src} to {dst} with original SR {original_sr}, target SR {target_sr} "
                 f"(method: {resolved})")
    dst.parent.mkdir(parents=True, exist_ok=True)

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
                # Only lower the target bitrate to match a lower-quality source.
                # Below 32 kbps keep the preset instead of producing degenerate values like "0k".
                if target_bps != -1 and 32000 <= orig_bps < target_bps:
                    adjusted_options[b_a_idx + 1] = f"{orig_bps // 1000}k"
                    logging.info(
                        f"Adjusting target bitrate from {target_br_str} to original {adjusted_options[b_a_idx + 1]} for {src.name}"
                    )
        except (ValueError, IndexError):
            pass
        return adjusted_options

    ext = dst.suffix.lower()
    is_video_container = ext in VIDEO_CONTAINER_EXTS

    def _attempt(chain: str) -> Tuple[bool, str]:
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

        errors: List[str] = []

        # Try HQ first
        hq_options = _adjust_bitrate_in_options(_codec_for_ext(ext, safe=False), original_bitrate_bps)
        proc = _run(base_cmd_list + hq_options + [str(dst)])
        if proc.returncode == 0:
            logging.info(f"Successfully converted {src.name} (HQ settings) to {dst.name}")
            return True, ""

        hq_stderr_cleaned = _clean_ffmpeg_err(proc.stderr) if proc.stderr is not None else "(no ffmpeg stderr captured)"
        errors.append(f"HQ error:\n{hq_stderr_cleaned}")

        # Subtitle fallback for MP4/MOV when "copy" isn't supported for the subtitle codec
        if has_real_video and is_video_container and ext in {".mp4", ".m4v", ".mov"}:
            err_blob = proc.stderr or b""
            err_lower = err_blob.lower() if isinstance(err_blob, (bytes, bytearray)) else str(err_blob).lower()
            if ("subtitle" in err_lower) and (
                    "codec" in err_lower or "not supported" in err_lower or "could not write header" in err_lower
            ):
                logging.warning(f"Subtitle copy unsupported for {dst.name}. Retrying with mov_text subtitles.")
                proc_sub = _run(base_cmd_list + ["-c:s", "mov_text"] + hq_options + [str(dst)])
                if proc_sub.returncode == 0:
                    logging.info(f"Successfully converted {src.name} (HQ + mov_text subs) to {dst.name}")
                    return True, ""

        logging.warning(f"HQ conversion failed for {src.name}. Retrying with safe settings. Error:\n{hq_stderr_cleaned}")
        safe_options = _adjust_bitrate_in_options(_codec_for_ext(ext, safe=True), original_bitrate_bps)
        proc_safe = _run(base_cmd_list + safe_options + [str(dst)])
        if proc_safe.returncode == 0:
            logging.info(f"Successfully converted {src.name} (Safe preset) to {dst.name}")
            return True, ""

        safe_stderr_cleaned = _clean_ffmpeg_err(proc_safe.stderr) if proc_safe.stderr is not None \
            else "(no ffmpeg stderr captured)"
        errors.append(f"Safe error:\n{safe_stderr_cleaned}")
        return False, "\n".join(errors)

    ok, error_text = _attempt(_audio_filter_chain(original_sr, target_sr, resolved))
    if not ok and resolved == "rubberband":
        # Rubber Band can still fail on exotic inputs; degrade gracefully to atempo.
        logging.warning(f"Rubber Band failed for {src.name}; falling back to asetrate+atempo. Error:\n{error_text}")
        ok, error_text_fb = _attempt(_audio_filter_chain(original_sr, target_sr, "atempo"))
        error_text = error_text + "\n" + error_text_fb
    if not ok:
        raise subprocess.CalledProcessError(1, _FFMPEG, stderr=error_text)

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


_SETTINGS_FILE = "app_settings.json"


def _settings_base_dir() -> Path:
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _load_run_settings(base_dir: Optional[Path] = None) -> dict:
    """Remembered last-used locations; corrupt or missing file yields {}."""
    try:
        p = (base_dir or _settings_base_dir()) / _SETTINGS_FILE
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logging.debug(f"Could not load settings: {e}")
    return {}


def _save_run_settings(data: dict, base_dir: Optional[Path] = None) -> None:
    try:
        base = base_dir or _settings_base_dir()
        base.mkdir(parents=True, exist_ok=True)
        (base / _SETTINGS_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logging.warning(f"Could not save settings: {e}")


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
            self._cancel_event = threading.Event()
            self._run_settings: dict = {}
            self._final_message: Optional[Tuple[str, str]] = None  # (level, text); set by worker, shown by _poll
            self._settings = _load_run_settings()
            self._build()
            self._apply_initial_args()
            self._restore_last_locations()

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
            self.btn_out = _ttk.Button(f_out, text="Browse…", command=self._browse_out)
            self.btn_out.pack(side="left", padx=(5, 0))
            self.btn_open_out = _ttk.Button(f_out, text="Open", command=self._open_output_folder, state="disabled")
            self.btn_open_out.pack(side="left", padx=(5, 0))

            f_opt = _ttk.Frame(self.root)
            f_opt.grid(row=2, column=0, sticky="w", **pad)
            self.rec = _tk.BooleanVar(
                value=True if getattr(self.args, "recursive", None) is None else bool(self.args.recursive)
            )  # default ON (batch folders usually contain subfolders); -R/--no-recursive pre-selects OFF
            self.keep = _tk.BooleanVar(value=self.args.keep)
            self.same_folder = _tk.BooleanVar(value=False)  # default OFF (A: output to *_432Hz)
            self.replace_original = _tk.BooleanVar(value=getattr(self.args, 'replace_original', False))  # replace originals (creates .bak)
            _ttk.Checkbutton(f_opt, text="Recursive", variable=self.rec).pack(side="left")
            self.chk_same = _ttk.Checkbutton(f_opt, text="Output in same folder", variable=self.same_folder,
                                             command=self._auto_set_output)
            self.chk_same.pack(side="left", padx=(10, 0))
            _ttk.Checkbutton(f_opt, text="Replace originals (.bak)", variable=self.replace_original,
                             command=self._on_mode_toggled).pack(side="left", padx=(10, 0))
            _ttk.Checkbutton(f_opt, text="Skip existing", variable=self.keep).pack(side="left", padx=(10, 0))

            f_method = _ttk.Frame(self.root)
            f_method.grid(row=3, column=0, sticky="w", **pad)
            _ttk.Label(f_method, text="Method ▶").pack(side="left")
            self.var_method = _tk.StringVar(value=METHOD_LABELS.get(getattr(self.args, "method", "auto"),
                                                                   METHOD_LABELS["auto"]))
            self.cmb_method = _ttk.Combobox(f_method, textvariable=self.var_method,
                                            values=list(METHOD_LABELS.values()),
                                            state="readonly", width=28)
            self.cmb_method.pack(side="left", padx=(5, 0))

            self.bar = _ttk.Progressbar(self.root, length=420, mode="determinate")
            self.bar.grid(row=4, column=0, **pad)
            f_btn = _ttk.Frame(self.root)
            f_btn.grid(row=5, column=0, **pad)
            self.btn = _ttk.Button(f_btn, text="Start", command=self._start)
            self.btn.pack(side="left")
            self.btn_cancel = _ttk.Button(f_btn, text="Cancel", command=self._cancel, state="disabled")
            self.btn_cancel.pack(side="left", padx=(10, 0))

            self.var_status = _tk.StringVar(value="")
            _ttk.Label(self.root, textvariable=self.var_status).grid(row=6, column=0, **pad)

            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
            self._update_mode_ui()

        def _on_mode_toggled(self):
            self._update_mode_ui()
            self._auto_set_output()

        def _update_mode_ui(self):
            # In replace mode the output location is fixed next to each source;
            # grey out the output controls to make that explicit.
            state = "disabled" if self.replace_original.get() else "normal"
            self.chk_same.config(state=state)
            self.entry_out.config(state=state)
            self.btn_out.config(state=state)

        def _cancel(self):
            self._cancel_event.set()
            self.btn_cancel.config(state="disabled")
            self.var_status.set("Cancelling…")

        def _on_close(self):
            if self.thread and self.thread.is_alive():
                if not _messagebox.askyesno("Quit?", "Conversion is still in progress.\nQuit anyway?"):
                    return
            self.root.destroy()

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
            d = _filedialog.askdirectory(title="Select Source Folder",
                                         initialdir=self._initial_dir_for_source())
            if d:
                self._set_src(Path(d))

        def _browse_src_file(self):
            f = _filedialog.askopenfilename(
                title="Select Media File",
                initialdir=self._initial_dir_for_source(),
                filetypes=[
                    ("Media files", "*.*"),
                ],
            )
            if f:
                self._set_src(Path(f))

        def _browse_out(self):
            d = _filedialog.askdirectory(title="Select Output Base Folder",
                                         initialdir=self._initial_dir_for_output())
            if d:
                self._set_out(Path(d))

        def _remember(self, key: str, path: Path) -> None:
            try:
                self._settings[key] = str(path)
                _save_run_settings(self._settings)
            except Exception:
                pass

        def _initial_dir_for_source(self) -> Optional[str]:
            p = self._settings.get("last_source")
            if p:
                path = Path(p)
                if path.is_dir():
                    return str(path)
                if path.is_file() and path.parent.is_dir():
                    return str(path.parent)
            out = self._settings.get("last_output")
            if out and Path(out).is_dir():
                return str(out)
            return None

        def _initial_dir_for_output(self) -> Optional[str]:
            out = self._settings.get("last_output")
            if out and Path(out).is_dir():
                return str(out)
            src = self._settings.get("last_source")
            if src and Path(src).is_dir():
                return str(src)
            return None

        def _restore_last_locations(self):
            """Pre-fill Source/Output from the remembered session (CLI args win)."""
            if self.args.folder or self.args.outdir:
                return
            last_src = self._settings.get("last_source")
            if last_src and not self.src and Path(last_src).exists():
                self._set_src(Path(last_src))
            last_out = self._settings.get("last_output")
            if last_out and Path(last_out).is_dir():
                self._set_out(Path(last_out))

        def _ondrop_src(self, event):
            p = _parse_drop_data(event.data)
            if p:
                self._set_src(p)

        def _set_src(self, p: Path):
            self.src = p.resolve()
            self.var_src.set(str(self.src))
            self._remember("last_source", self.src)
            # Always auto-update output when source changes (as requested)
            self._auto_set_output()

        def _set_out(self, p: Path):
            self.dst_base = p.resolve()
            self.var_out.set(str(self.dst_base))
            self._remember("last_output", self.dst_base)
            self._update_open_btn()

        def _update_open_btn(self):
            btn = getattr(self, "btn_open_out", None)
            if btn is None:
                return
            if self.dst_base is not None and self.dst_base.is_dir():
                btn.config(state="normal")
            else:
                btn.config(state="disabled")

        def _open_output_folder(self):
            if self.dst_base and self.dst_base.is_dir():
                _open_in_file_manager(self.dst_base)

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

        def _selected_method(self) -> str:
            reverse = {label: key for key, label in METHOD_LABELS.items()}
            return reverse.get(self.var_method.get(), "auto")

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
                out_text = self.var_out.get().strip()
                if not out_text:
                    _messagebox.showerror("Error", "Please select an output folder.")
                    return
                self.dst_base = Path(out_text).resolve()
                try:
                    self.dst_base.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    _messagebox.showerror("Error", f"Cannot create output folder:\n{self.dst_base}\n{e}")
                    return

            chosen_method = self._selected_method()
            if chosen_method == "rubberband" and not _ffmpeg_has_filter("rubberband"):
                _messagebox.showerror(
                    "Error",
                    "This FFmpeg build does not include the Rubber Band filter.\n"
                    "Please choose another method (Auto, Purist or Fast).")
                return

            self.btn.config(state="disabled")
            self.bar["value"] = 0
            self._final_message = None
            self._cancel_event.clear()
            # Snapshot settings on the main thread; Tk variables must not be read
            # from worker/pool threads, and mid-run checkbox changes can't skew a run.
            self._run_settings = {
                "src": self.src,
                "dst_base": self.dst_base,
                "recursive": self.rec.get(),
                "keep": self.keep.get(),
                "same_folder": self.same_folder.get(),
                "replace_original": self.replace_original.get(),
                "method": chosen_method,
            }
            self.btn_cancel.config(state="normal")
            self.var_status.set("Scanning…")
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()
            self._poll()

        def _poll(self):
            if self.thread and self.thread.is_alive():
                self.root.after(200, self._poll)
            else:
                self.btn.config(state="normal")
                self.btn_cancel.config(state="disabled")
                self._update_open_btn()
                if self._final_message is not None:
                    level, text = self._final_message
                    self._final_message = None
                    if level == "info":
                        _messagebox.showinfo("Done", text)
                    else:
                        _messagebox.showwarning("Done", text)
                self.bar["value"] = 0
                self.var_status.set("")

        def _worker(self):
            # Settings were snapshotted on the main thread in _start(); Tk variables
            # must never be read from worker/pool threads.
            settings = self._run_settings
            src = settings["src"]
            dst_base = settings["dst_base"]
            assert src and dst_base, "Source or destination not set"
            if src.is_file():
                files = [src]
            else:
                files = find_audio_files(
                    src, settings["recursive"],
                    progress_cb=self._scan_progress,
                    should_stop=self._cancel_event.is_set,
                )
            total = len(files)
            if self._cancel_event.is_set():
                self._final_message = ("warning", "Cancelled by user.\n\nScanned files were not converted.")
                return
            if not total:
                self._final_message = ("warning", "No media files with audio were found in the selected folder.")
                return

            # All widget updates must go through the Tk main loop (thread safety).
            self.root.after(0, lambda t=total: self.bar.config(maximum=t))

            src_is_dir = src.is_dir()
            keep_existing = settings["keep"]
            same_folder = settings["same_folder"]
            replacing = settings["replace_original"]
            method = settings["method"]
            src_root = src
            # The purist method intentionally slows the audio by 440/432 (+1.85%);
            # the duration check must compare against that scaled expectation.
            duration_scale = 440 / 432 if method == "pure" else 1.0

            def _convert_one(f_path: Path) -> Tuple[str, Optional[str], bool]:
                """Convert one file. Returns (status, error_detail, duration_mismatch)."""
                if self._cancel_event.is_set():
                    return "not_run", None, False

                has_audio, has_real_video, has_attached_pic, original_sample_rate, original_bitrate = \
                    _probe_media_info(f_path)
                if not has_audio:
                    # Shouldn't happen because find_audio_files filters, but be defensive.
                    logging.info(f"Skipping non-audio file after probe: {f_path}")
                    return "skipped_no_audio", None, False

                ext_out = _choose_output_ext(f_path.suffix, has_real_video)

                # Compute destination path:
                # - Replace originals: temp file next to each source, then swap in-place (original kept as .bak)
                # - Folder mode: preserve relative subfolder structure under dst_base
                # - Single-file mode: place into dst_base (or next to source when same_folder=True)
                if replacing:
                    # Guard: an existing .bak means this file was already converted once;
                    # converting again would shift the pitch a second time.
                    if _has_existing_backup(f_path):
                        logging.info(f"GUI Worker: Skipping already-converted file (backup exists): {f_path}")
                        return "skipped_backed_up", None, False
                    # Keep the same container/extension when replacing originals.
                    ext_out = f_path.suffix
                    dst_file = f_path.with_name(f"{f_path.stem}.__tmp432__{ext_out}")
                elif src_is_dir:
                    rel_path = f_path.relative_to(src_root)
                    if same_folder:
                        dst_file = f_path.parent / f"{f_path.stem}_432{ext_out}"
                    else:
                        dst_file = dst_base / rel_path.with_name(f"{f_path.stem}_432{ext_out}")
                else:
                    rel_path = Path(f_path.name)
                    if same_folder:
                        dst_file = f_path.parent / f"{f_path.stem}_432{ext_out}"
                    else:
                        dst_file = dst_base / rel_path.with_name(f"{f_path.stem}_432{ext_out}")

                try:
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    logging.error(f"GUI Worker: Error creating directory for {dst_file.name}: {e}", exc_info=True)
                    return "failed", f"{f_path.name}: could not create output directory ({e})", False

                if keep_existing and dst_file.exists() and dst_file.stat().st_size > 0:
                    logging.info(f"GUI Worker: Skipping existing file: {dst_file}")
                    return "skipped_existing", None, False

                # Keep the source sample rate: asetrate does the pitch shift, atempo restores
                # duration, and aresample merely normalises back to the original rate.
                src_sample_rate = int(original_sample_rate) if original_sample_rate else 44100
                try:
                    convert_to_432(
                        f_path,
                        dst_file,
                        src_sample_rate,
                        src_sample_rate,
                        original_bitrate,
                        has_real_video=has_real_video,
                        has_attached_pic=has_attached_pic,
                        method=method,
                    )
                    # Duration sanity check (a correct pitch shift preserves duration,
                    # except for the deliberately slower "pure" method).
                    dur_mismatch = not self._durations_match(f_path, dst_file, duration_scale)
                    if dur_mismatch:
                        logging.warning(f"GUI Worker: Duration mismatch after conversion: {f_path.name}")
                    if replacing:
                        try:
                            _replace_original_with_backup(f_path, dst_file)
                            logging.info(f"Replaced original with 432Hz version (backup created): {f_path.name}")
                        finally:
                            # If something went wrong and temp still exists, clean it up.
                            if dst_file.exists() and '.__tmp432__' in dst_file.name:
                                try:
                                    dst_file.unlink(missing_ok=True)
                                except Exception:
                                    pass
                    return "converted", None, dur_mismatch
                except subprocess.CalledProcessError as e_conv:
                    if replacing and '.__tmp432__' in dst_file.name:
                        try:
                            dst_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                    logging.error(f"GUI Worker: Conversion failed for {f_path.name}: {e_conv.stderr}", exc_info=False)
                    return "failed", f"{f_path.name}: {_short_error(e_conv.stderr)}", False
                except Exception as e_gen:
                    if replacing and '.__tmp432__' in dst_file.name:
                        try:
                            dst_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                    logging.error(f"GUI Worker: Unexpected error converting {f_path.name}: {e_gen}", exc_info=True)
                    return "failed", f"{f_path.name}: unexpected error ({e_gen})", False

            workers = _parallel_workers()

            def _done_ui(done: int) -> None:
                self.root.after(0, lambda d=done: self.bar.config(value=d))
                self.root.after(0, lambda d=done, t=total, w=workers:
                                self.var_status.set(f"Converting… {d}/{t} done ({w} in parallel)"))

            counters = {"converted": 0, "skipped_existing": 0, "skipped_backed_up": 0,
                        "skipped_no_audio": 0, "duration_mismatch": 0, "not_run": 0}
            failed: List[str] = []
            done = 0
            _done_ui(0)
            # One ffmpeg per worker; on small CPUs this degrades to a single worker
            # (identical to the old sequential behaviour).
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_convert_one, f_path) for f_path in files]
                for fut in as_completed(futures):
                    try:
                        status, detail, dur_bad = fut.result()
                    except Exception as e:
                        status, detail, dur_bad = "failed", f"(task crashed: {e})", False
                    done += 1
                    if status == "converted":
                        counters["converted"] += 1
                    elif status == "skipped_existing":
                        counters["skipped_existing"] += 1
                    elif status == "skipped_backed_up":
                        counters["skipped_backed_up"] += 1
                    elif status == "skipped_no_audio":
                        counters["skipped_no_audio"] += 1
                    elif status == "not_run":
                        counters["not_run"] += 1
                    else:  # failed
                        failed.append(detail or "unknown error")
                    if dur_bad:
                        counters["duration_mismatch"] += 1
                    _done_ui(done)

            cancelled = self._cancel_event.is_set()
            self._final_message = self._build_summary(
                counters["converted"], counters["skipped_existing"], counters["skipped_backed_up"],
                counters["skipped_no_audio"], sorted(failed),
                counters["duration_mismatch"], cancelled, counters["not_run"],
            )

        def _scan_progress(self, found: int, current: Path) -> bool:
            self.root.after(0, lambda n=found, c=current.name:
                            self.var_status.set(f"Scanning… {n} media file(s) found (checking: {c})"))
            return not self._cancel_event.is_set()

        @staticmethod
        def _durations_match(src: Path, dst: Path, expected_scale: float = 1.0) -> bool:
            a = _media_duration_seconds(src)
            b = _media_duration_seconds(dst)
            if a is None or b is None or a <= 0:
                return True  # cannot verify; do not flag
            expected = a * expected_scale
            return abs(b - expected) <= max(0.5, expected * 0.01)

        @staticmethod
        def _build_summary(converted: int, skipped_existing: int, skipped_backed_up: int,
                           skipped_no_audio: int, failed: List[str],
                           duration_mismatch: int = 0, cancelled: bool = False,
                           not_processed: int = 0) -> Tuple[str, str]:
            lines = []
            if converted:
                lines.append(f"Converted: {converted}")
            if skipped_existing:
                lines.append(f"Skipped (output already exists): {skipped_existing}")
            if skipped_backed_up:
                lines.append(f"Skipped (already converted earlier, .bak exists): {skipped_backed_up}")
            if skipped_no_audio:
                lines.append(f"Skipped (no audio stream): {skipped_no_audio}")
            if duration_mismatch:
                lines.append(f"Duration mismatch (see log): {duration_mismatch}")
            if not_processed:
                lines.append(f"Not processed (cancelled): {not_processed}")
            if failed:
                lines.append(f"Failed: {len(failed)}")
            summary = "\n".join(lines) if lines else "Nothing to do."
            prefix = "Cancelled by user.\n\n" if cancelled else ""

            if failed:
                details = "\n".join(f"- {entry}" for entry in failed[:10])
                if len(failed) > 10:
                    details += f"\n- ... and {len(failed) - 10} more (see app_converter.log)"
                head = "Conversion failed." if (converted == 0 and not cancelled) else "Conversion finished with errors."
                return ("warning", f"{prefix}{head}\n\n{summary}\n\n{details}")
            if cancelled:
                return ("warning", f"{prefix}{summary}")
            if converted:
                return ("info", f"Conversion complete!\n\n{summary}")
            return ("warning", f"No files were converted.\n\n{summary}")


# =============================================================================
# 4.  Arg‑parser & tests
# =============================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="440→432 Hz batch converter (GUI)")
    p.add_argument("folder", type=Path, nargs="?", default=None,
                   help="Optional: Source folder OR a single media file to pre-fill in GUI.")
    p.add_argument("-r", "--recursive", action="store_true", default=None,
                   help="Pre-select recursive search in GUI (default: on).")
    p.add_argument("-R", "--no-recursive", dest="recursive", action="store_false",
                   help="Pre-select non-recursive search in GUI.")
    p.add_argument("--keep", action="store_true", help="Pre-select skipping existing files in GUI.")
    p.add_argument("--replace", dest="replace_original", action="store_true",
                   help="Pre-select replacing originals (creates .bak backups) in GUI.")
    p.add_argument("--ffmpeg", dest="ffmpeg_path", type=Path,
                   help="Path to ffmpeg folder/executable for FFmpeg/FFprobe resolution.")
    p.add_argument("--out", dest="outdir", type=Path, help="Optional: Destination base folder to pre-fill in GUI.")
    p.add_argument("--method", choices=sorted(METHOD_LABELS.keys()), default="auto",
                   help="Pitch-shift method to pre-select in GUI (default: auto = Rubber Band when available).")
    p.add_argument("--test", action="store_true", help="Run self‑tests and exit.")
    return p


class _Tests(unittest.TestCase):
    def test_codec_quality(self):
        self.assertIn("-b:a", _codec_for_ext(".mp3"))
        self.assertIn("512k", _codec_for_ext(".aac"))
        # ffmpeg ships no wmapro encoder; both presets must use the encodable wmav2.
        self.assertIn("wmav2", _codec_for_ext(".wma"))
        self.assertIn("wmav2", _codec_for_ext(".wma", safe=True))
        self.assertNotIn("wmapro", _codec_for_ext(".wma"))
        self.assertNotIn("wmapro", _codec_for_ext(".wma", safe=True))
        # Video MKVs must re-encode audio to AAC (matched to the source bitrate),
        # not FLAC, which only bloats already-compressed video audio.
        self.assertIn("aac", _codec_for_ext(".mkv"))
        self.assertNotIn("flac", _codec_for_ext(".mkv"))

    def test_choose_output_ext(self):
        # Outputs keep the original audio format wherever we can encode it.
        self.assertEqual(_choose_output_ext(".wma", False), ".wma")
        self.assertEqual(_choose_output_ext(".flac", False), ".flac")
        self.assertEqual(_choose_output_ext(".mp3", False), ".mp3")
        # Formats with no FFmpeg encoder fall back to .mp3.
        self.assertEqual(_choose_output_ext(".ape", False), ".mp3")
        # Videos keep their container.
        self.assertEqual(_choose_output_ext(".mkv", True), ".mkv")

    def test_run_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _save_run_settings({"last_source": "X", "last_output": "Y"}, base_dir=base)
            self.assertEqual(_load_run_settings(base_dir=base),
                             {"last_source": "X", "last_output": "Y"})
            # A corrupt settings file must not crash the app.
            (base / _SETTINGS_FILE).write_text("{broken", encoding="utf-8")
            self.assertEqual(_load_run_settings(base_dir=base), {})

    def test_estimate_audio_bitrate(self):
        from unittest import mock
        fake = json.dumps({"packets": [{"size": "12500"}] * 100,
                           "format": {"duration": "10.0"}})
        with mock.patch("main._popen_run",
                        return_value=PopenResult(["f"], 0, fake, "")):
            # 100 packets x 12500 bytes x 8 bits / 10 s = 1,000,000 bps
            self.assertEqual(_estimate_audio_bitrate_bps(Path("x.mkv")), 1_000_000)
        broken = json.dumps({"packets": [], "format": {}})
        with mock.patch("main._popen_run",
                        return_value=PopenResult(["f"], 0, broken, "")):
            self.assertIsNone(_estimate_audio_bitrate_bps(Path("x.mkv")))

    def test_parse_drop_data(self):
        self.assertIsNone(_parse_drop_data(""))
        self.assertIsNone(_parse_drop_data("{}"))
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_parse_drop_data("{" + td + "}"), Path(td))

    def test_short_error(self):
        self.assertEqual(_short_error(None), "unknown error")
        long_err = "x" * 500
        self.assertTrue(_short_error(long_err).endswith("..."))
        self.assertLessEqual(len(_short_error(long_err)), 200)
        self.assertEqual(_short_error("line1\nline2"), "line1 line2")

    def test_find_audio_files_filters_and_cancel(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("song.mp3", "song_432.mp3", "file.__tmp432__.mp3",
                         "~BitTorrentPartFile_a.mp3", "notes.txt", "photo.jpg", "rare.xyz"):
                (root / name).write_bytes(b"x")
            (root / "sub").mkdir()
            (root / "sub" / "deep.flac").write_bytes(b"x")

            with mock.patch("main._probe_has_audio", lambda p, _stop=None: True):
                found = find_audio_files(root, recursive=True)
            self.assertEqual(sorted(f.name for f in found), ["deep.flac", "rare.xyz", "song.mp3"])

            # A progress_cb returning False aborts the scan early.
            seen: List[str] = []

            def _cb(n: int, cur: Path) -> bool:
                seen.append(cur.name)
                return False

            with mock.patch("main._probe_has_audio", lambda p, _stop=None: True):
                aborted = find_audio_files(root, recursive=True, progress_cb=_cb)
            self.assertEqual(len(seen), 1)
            self.assertLessEqual(len(aborted), 1)

            # should_stop short-circuits every probe.
            with mock.patch("main._probe_has_audio",
                            lambda p, _stop=None: not (_stop is not None and _stop())):
                stopped = find_audio_files(root, recursive=True, should_stop=lambda: True)
            self.assertEqual(stopped, [])

    def test_audio_filter_chain(self):
        ratio = 432 / 440
        pure = _audio_filter_chain(44100, 44100, "pure")
        self.assertIn("asetrate=44100*", pure)
        self.assertNotIn("atempo", pure)
        self.assertIn("aresample=44100", pure)

        rb = _audio_filter_chain(44100, 44100, "rubberband")
        self.assertIn(f"rubberband=pitch={ratio:.6f}", rb)
        self.assertIn("channels=together", rb)
        self.assertNotIn("asetrate", rb)

        at = _audio_filter_chain(44100, 44100, "atempo")
        self.assertIn(f"atempo={1/ratio:.6f}", at)
        self.assertIn("aresample=44100", at)

    def test_ffmpeg_has_filter_and_resolve(self):
        from unittest import mock
        fake_filters = (
            "Filters:\n"
            " T.. aformat           Convert the input audio to one of the given formats.\n"
            " T.C rubberband        Apply high-quality pitch shifting.\n"
        )
        with mock.patch("main._popen_run",
                        return_value=PopenResult(["f"], 0, fake_filters, "")), \
                mock.patch("main._FFMPEG_FILTERS_CACHE", None):
            self.assertTrue(_ffmpeg_has_filter("rubberband"))
            self.assertTrue(_ffmpeg_has_filter("aformat"))
            self.assertFalse(_ffmpeg_has_filter("nonexistent"))

        with mock.patch("main._FFMPEG_FILTERS_CACHE", {"rubberband"}):
            self.assertEqual(_resolve_method("auto"), "rubberband")
            # Auto prefers speed on videos (long audio tracks, little audible gain)
            self.assertEqual(_resolve_method("auto", has_real_video=True), "atempo")
        with mock.patch("main._FFMPEG_FILTERS_CACHE", set()):
            self.assertEqual(_resolve_method("auto"), "atempo")
        # Explicit choices are never overridden
        self.assertEqual(_resolve_method("pure"), "pure")
        self.assertEqual(_resolve_method("rubberband"), "rubberband")

    def test_parallel_workers(self):
        from unittest import mock
        # Single-core / unknown CPU -> sequential (1 worker), same as before.
        with mock.patch("main.os.cpu_count", lambda: 1):
            self.assertEqual(_parallel_workers(), 1)
        with mock.patch("main.os.cpu_count", lambda: None):
            self.assertEqual(_parallel_workers(), 1)
        # Small dual-core -> still 1; quad-core -> 3.
        with mock.patch("main.os.cpu_count", lambda: 2):
            self.assertEqual(_parallel_workers(), 1)
        with mock.patch("main.os.cpu_count", lambda: 4):
            self.assertEqual(_parallel_workers(), 3)
        # Big CPUs are capped at 6 parallel conversions.
        with mock.patch("main.os.cpu_count", lambda: 32):
            self.assertEqual(_parallel_workers(), 6)

    @unittest.skipIf(_tk is None, "tkinter not available")
    def test_build_summary(self):
        s = _ConverterGUI._build_summary
        level, text = s(5, 1, 2, 0, [], duration_mismatch=1)
        self.assertEqual(level, "info")
        self.assertIn("Converted: 5", text)
        self.assertIn("Skipped (output already exists): 1", text)
        self.assertIn("Skipped (already converted earlier, .bak exists): 2", text)
        self.assertIn("Duration mismatch (see log): 1", text)

        level, text = s(5, 0, 0, 0, [], cancelled=True)
        self.assertEqual(level, "warning")
        self.assertIn("Cancelled by user.", text)

        level, text = s(5, 0, 0, 0, [], cancelled=True, not_processed=7)
        self.assertIn("Not processed (cancelled): 7", text)

        level, text = s(0, 0, 0, 0, ["a.mp3: boom"])
        self.assertEqual(level, "warning")
        self.assertIn("Conversion failed.", text)
        self.assertIn("a.mp3: boom", text)


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
