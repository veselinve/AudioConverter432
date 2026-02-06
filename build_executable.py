"""
build_executable.py
-------------------
Builds a Windows standalone .exe for the 432Hz converter GUI using PyInstaller.

What this fixes / guarantees:
- Builds an EXE (does NOT run main.py).
- Installs BOTH runtime + build deps (combines requirements.txt + requirements-build.txt automatically).
- Bundles FFmpeg (ffmpeg.exe + ffprobe.exe + DLLs) inside the EXE (portable, no separate install).
- Bundles tkinterdnd2 assets so drag-and-drop keeps working in the EXE.

Usage (Windows):
  python build_executable.py
Optional:
  python build_executable.py --onedir
  python build_executable.py --no-download-ffmpeg
  python build_executable.py --zip
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

# -------- CONFIG --------
MAIN_SCRIPT = "main.py"
EXE_NAME = "AudioConverter432"

# Known good Windows builds (contains ffmpeg.exe, ffprobe.exe, DLLs)
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# Preferred local location in the project (kept for both dev runs + builds)
VENDORS_FFMPEG_BIN = Path("vendors") / "ffmpeg" / "bin"
# ------------------------


def _is_windows() -> bool:
    return os.name == "nt"


def _print(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: list[str], cwd: Optional[Path] = None) -> None:
    _print(">> " + " ".join(str(x) for x in cmd))
    subprocess.check_call([str(x) for x in cmd], cwd=str(cwd) if cwd else None)


def _on_rm_error(func, path, exc_info):
    # Handle read-only files on Windows.
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        raise


def _safe_rmtree(p: Path) -> None:
    if not p.exists():
        return
    try:
        shutil.rmtree(p, onerror=_on_rm_error)
    except Exception as e:
        # If something is locking it (e.g. antivirus / explorer), don't hard-fail.
        # Move it aside so the build can continue.
        backup = p.with_name(p.name + "_old")
        try:
            if backup.exists():
                shutil.rmtree(backup, onerror=_on_rm_error)
            p.rename(backup)
            _print(f"WARNING: Could not delete '{p}'. Renamed to '{backup}'. ({e})")
        except Exception:
            _print(
                f"WARNING: Could not delete or rename '{p}'. Close processes using it and retry. ({e})"
            )


def _pip_install_requirements(project_dir: Path) -> None:
    """Install runtime + build dependencies.

    We "combine requirements" by installing both files if present:
      - requirements.txt (runtime)
      - requirements-build.txt (build-only, e.g. pyinstaller)
    """
    req_files: list[Path] = []
    for name in ("requirements.txt", "requirements-build.txt"):
        p = project_dir / name
        if p.is_file():
            req_files.append(p)

    if not req_files:
        _print("No requirements*.txt files found; skipping dependency install.")
        return

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"]
    _run(cmd)

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    for rf in req_files:
        cmd += ["-r", str(rf)]
    _run(cmd)


def _ensure_pyinstaller(project_dir: Path, skip_pip: bool) -> None:
    if not skip_pip:
        _pip_install_requirements(project_dir)

    try:
        import PyInstaller.__main__  # noqa: F401
        return
    except Exception:
        # Fallback if requirements files are missing.
        _print("PyInstaller not found. Installing it now...")
        _run([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"])
        import PyInstaller.__main__  # noqa: F401


def _find_ffmpeg_bin(project_dir: Path) -> Optional[Path]:
    """Returns a directory that contains ffmpeg.exe and ffprobe.exe.

    Priority:
      1) ./vendors/ffmpeg/bin
      2) ./ffmpeg*/bin or ./ffmpeg*/ (unpacked builds)
      3) ffmpeg.exe next to project
      4) From PATH (directory containing both)
    """
    if _is_windows():
        ffmpeg_name, ffprobe_name = "ffmpeg.exe", "ffprobe.exe"
    else:
        ffmpeg_name, ffprobe_name = "ffmpeg", "ffprobe"

    # 1) vendors
    vb = project_dir / VENDORS_FFMPEG_BIN
    if (vb / ffmpeg_name).is_file() and (vb / ffprobe_name).is_file():
        return vb

    # 2) ffmpeg* folders
    try:
        for item in project_dir.iterdir():
            if not item.is_dir():
                continue
            if not item.name.lower().startswith("ffmpeg"):
                continue
            cand1 = item / "bin"
            if (cand1 / ffmpeg_name).is_file() and (cand1 / ffprobe_name).is_file():
                return cand1
            cand2 = item
            if (cand2 / ffmpeg_name).is_file() and (cand2 / ffprobe_name).is_file():
                return cand2
    except Exception:
        pass

    # 3) next to project
    if (project_dir / ffmpeg_name).is_file() and (project_dir / ffprobe_name).is_file():
        return project_dir

    # 4) PATH
    p_ffmpeg = shutil.which(ffmpeg_name)
    p_ffprobe = shutil.which(ffprobe_name)
    if p_ffmpeg and p_ffprobe:
        d = Path(p_ffmpeg).resolve().parent
        if (d / ffprobe_name).is_file():
            return d

    return None


def _download_ffmpeg_to_vendors(project_dir: Path) -> Path:
    """Download FFmpeg zip and populate ./vendors/ffmpeg/bin with ffmpeg.exe, ffprobe.exe, and DLLs."""
    vendors_bin = project_dir / VENDORS_FFMPEG_BIN
    vendors_bin.mkdir(parents=True, exist_ok=True)

    tmp = project_dir / "vendors" / "_ffmpeg_tmp"
    _safe_rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    zip_path = tmp / "ffmpeg.zip"
    _print(f"Downloading FFmpeg: {FFMPEG_ZIP_URL}")
    _print(f" -> {zip_path}")
    urllib.request.urlretrieve(FFMPEG_ZIP_URL, zip_path)

    _print("Extracting FFmpeg...")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(tmp)

    # Find extracted bin folder
    bin_dir: Optional[Path] = None
    for root, _, files in os.walk(tmp):
        if "ffmpeg.exe" in files and "ffprobe.exe" in files:
            r = Path(root)
            # Prefer a .../bin folder
            if r.name.lower() == "bin":
                bin_dir = r
                break
            if bin_dir is None:
                bin_dir = r

    if not bin_dir:
        raise RuntimeError(
            "FFmpeg download/extract succeeded, but ffmpeg.exe/ffprobe.exe were not found in the archive."
        )

    _print(f"Copying FFmpeg files into: {vendors_bin}")
    for f in bin_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, vendors_bin / f.name)

    _safe_rmtree(tmp)
    return vendors_bin


def _bundle_ffmpeg_args(ffmpeg_bin: Path) -> list[str]:
    """PyInstaller args that place FFmpeg binaries where main.py expects them.

    main.py resolves FFmpeg by preferring:
        <root>/vendors/ffmpeg/bin/{ffmpeg.exe, ffprobe.exe}
    where <root> is either:
        - sys._MEIPASS (onefile extracted bundle root)
        - the script directory (dev runs)
        - the EXE directory (frozen)

    So we bundle FFmpeg into: vendors/ffmpeg/bin inside the bundle.
    """
    args: list[str] = []
    dest = "vendors/ffmpeg/bin"

    # Only bundle what FFmpeg needs at runtime: .exe + .dll
    for f in ffmpeg_bin.iterdir():
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in (".exe", ".dll"):
            spec = f"{str(f)}{os.pathsep}{dest}"
            args.append(f"--add-binary={spec}")

    return args


def _zip_release(dist_dir: Path, exe_name: str) -> Path:
    """Create a small release zip containing the built EXE (or onedir folder)."""
    zip_path = dist_dir / f"{exe_name}_release.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        exe_file = dist_dir / f"{exe_name}.exe"
        if exe_file.is_file():
            z.write(exe_file, arcname=exe_file.name)
        else:
            # onedir layout
            app_dir = dist_dir / exe_name
            if not app_dir.is_dir():
                raise RuntimeError("Nothing to zip: expected dist exe or dist/<name>/ folder.")
            for p in app_dir.rglob("*"):
                if p.is_file():
                    z.write(p, arcname=str(p.relative_to(dist_dir)))

    return zip_path


def create_executable(
    *,
    onefile: bool = True,
    clean: bool = True,
    download_ffmpeg: bool = True,
    make_zip: bool = False,
    skip_pip: bool = False,
) -> None:
    _print("=== 432Hz Converter EXE Build ===")
    project_dir = Path(__file__).resolve().parent
    main_script = project_dir / MAIN_SCRIPT

    if not main_script.exists():
        raise FileNotFoundError(f"Main script not found: {main_script}")

    if not _is_windows():
        _print("WARNING: This script is primarily tested for Windows builds.")
    else:
        _print(f"Python: {sys.executable}")
        _print(f"Project: {project_dir}")

    # Ensure PyInstaller exists (+ install deps)
    _ensure_pyinstaller(project_dir, skip_pip=skip_pip)
    import PyInstaller.__main__  # noqa: F401

    # Clean old outputs
    if clean:
        _print("\n[1/4] Cleaning previous build artifacts...")
        _safe_rmtree(project_dir / "build")
        _safe_rmtree(project_dir / "dist")
        spec_file = project_dir / f"{EXE_NAME}.spec"
        if spec_file.exists():
            try:
                spec_file.unlink()
            except Exception:
                _print(f"WARNING: Could not delete spec file: {spec_file}")
    else:
        _print("\n[1/4] Clean skipped (as requested).")

    # Locate/download ffmpeg
    _print("\n[2/4] Locating FFmpeg...")
    ffmpeg_bin = _find_ffmpeg_bin(project_dir)
    if not ffmpeg_bin and download_ffmpeg and _is_windows():
        _print("FFmpeg not found locally. Auto-downloading into ./vendors/ffmpeg/bin ...")
        ffmpeg_bin = _download_ffmpeg_to_vendors(project_dir)
    elif ffmpeg_bin:
        _print(f"Found FFmpeg bin at: {ffmpeg_bin}")
    else:
        _print("FFmpeg not found (and download disabled). Build will proceed without bundling it.")
        ffmpeg_bin = None

    # Build PyInstaller args (IMPORTANT: options first, script LAST)
    _print("\n[3/4] Running PyInstaller...")
    args: list[str] = [
        "--noconfirm",
        "--clean",
        "--windowed",
        f"--name={EXE_NAME}",
        # Ensure tkinterdnd2 (TkDND) binary/data gets included for drag-and-drop:
        "--collect-all=tkinterdnd2",
        "--hidden-import=tkinterdnd2",
    ]
    if onefile:
        args.append("--onefile")
    else:
        args.append("--onedir")

    # Bundle FFmpeg if available
    if ffmpeg_bin:
        args += _bundle_ffmpeg_args(ffmpeg_bin)

    # Finally: the entry script
    args.append(str(main_script))

    _print("PyInstaller args:")
    _print("pyinstaller " + " ".join(args))

    # Execute build
    import PyInstaller.__main__  # noqa
    PyInstaller.__main__.run(args)

    # Verify output
    _print("\n[4/4] Verifying output...")
    # Show PyInstaller warnings file (if any) to help diagnose "missing package" messages.
    warn_file = project_dir / "build" / EXE_NAME / f"warn-{EXE_NAME}.txt"
    if warn_file.is_file():
        _print(f"ℹ️  PyInstaller warnings file: {warn_file.resolve()}")
    dist_dir = project_dir / "dist"
    exe_path = dist_dir / f"{EXE_NAME}.exe"

    if exe_path.is_file():
        _print(f"✅ Build successful: {exe_path.resolve()}")
    else:
        # onedir layout
        alt = dist_dir / EXE_NAME / f"{EXE_NAME}.exe"
        if alt.is_file():
            exe_path = alt
            _print(f"✅ Build successful (onedir): {exe_path.resolve()}")
        else:
            raise RuntimeError("Build finished but executable was not found in dist/. Check PyInstaller output above.")

    if make_zip:
        zip_path = _zip_release(dist_dir, EXE_NAME)
        _print(f"📦 Release zip created: {zip_path.resolve()}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a Windows EXE for the 432Hz converter.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--onefile", action="store_true", help="Build one-file EXE (default).")
    g.add_argument("--onedir", action="store_true", help="Build one-dir folder (faster startup, bigger output).")
    p.add_argument("--no-clean", action="store_true", help="Do not delete build/ and dist/ first.")
    p.add_argument("--no-download-ffmpeg", action="store_true", help="Do not auto-download FFmpeg if missing.")
    p.add_argument("--zip", action="store_true", help="Create dist/<name>_release.zip after build.")
    p.add_argument("--skip-pip", action="store_true", help="Skip pip installs (assume deps already installed).")
    return p.parse_args()


if __name__ == "__main__":
    ns = _parse_args()
    create_executable(
        onefile=(not ns.onedir),
        clean=(not ns.no_clean),
        download_ffmpeg=(not ns.no_download_ffmpeg),
        make_zip=bool(ns.zip),
        skip_pip=bool(ns.skip_pip),
    )
