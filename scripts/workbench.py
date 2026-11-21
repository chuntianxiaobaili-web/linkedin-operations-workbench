"""Local launcher and Obsidian environment check."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD = "https://obsidian.md/download"
HOME = Path.home()


def default_state():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = HOME / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state"))
    return base / "LinkedInOperationsWorkbench"


def windows_candidates():
    import winreg

    app_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Obsidian.exe"
    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    protocol = r"obsidian\shell\open\command"
    for root, key in [(winreg.HKEY_CURRENT_USER, app_path),
                      (winreg.HKEY_LOCAL_MACHINE, app_path),
                      (winreg.HKEY_CLASSES_ROOT, protocol)]:
        try:
            with winreg.OpenKey(root, key) as handle:
                value = winreg.QueryValueEx(handle, "")[0]
            match = re.match(r'^\s*"([^"]+\.exe)"|^\s*(.+?\.exe)(?:\s|$)', value, re.I)
            if match:
                yield Path(os.path.expandvars(match.group(1) or match.group(2)))
        except OSError:
            continue
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, uninstall, 0, winreg.KEY_READ | view) as parent:
                    for index in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as entry:
                                name = winreg.QueryValueEx(entry, "DisplayName")[0]
                                if not name.casefold().startswith("obsidian"):
                                    continue
                                for field in ("InstallLocation", "DisplayIcon"):
                                    try:
                                        value = winreg.QueryValueEx(entry, field)[0]
                                    except OSError:
                                        continue
                                    path = Path(os.path.expandvars(value.split(",")[0].strip('" ')))
                                    yield path / "Obsidian.exe" if field == "InstallLocation" else path
                        except OSError:
                            continue
            except OSError:
                continue
    for key, fallback in [
        ("LOCALAPPDATA", HOME / "AppData" / "Local"),
        ("ProgramFiles", Path("C:/Program Files")),
        ("ProgramFiles(x86)", Path("C:/Program Files (x86)")),
    ]:
        root = Path(os.environ.get(key, fallback))
        yield root / "Obsidian" / "Obsidian.exe"
        yield root / "Programs" / "Obsidian" / "Obsidian.exe"


def obsidian_candidates(explicit=None):
    if explicit:
        yield Path(explicit).expanduser()
    for name in ("obsidian", "Obsidian.exe"):
        found = shutil.which(name)
        if found:
            yield Path(found)
    if sys.platform == "win32":
        yield from windows_candidates()
    elif sys.platform == "darwin":
        for parent in (Path("/Applications"), HOME / "Applications"):
            yield parent / "Obsidian.app" / "Contents" / "MacOS" / "Obsidian"
    else:
        yield Path("/opt/Obsidian/obsidian")
        yield Path("/snap/bin/obsidian")
        yield Path("/var/lib/flatpak/exports/bin/md.obsidian.Obsidian")
        yield HOME / ".local/share/flatpak/exports/bin/md.obsidian.Obsidian"
        for parent in (HOME / "Applications", HOME / "Downloads", Path("/opt")):
            yield from sorted(parent.glob("[Oo]bsidian*.AppImage"))


def doctor(vault=None, executable=None):
    vault = Path(vault or ROOT / "vault").expanduser().resolve()
    app = next((candidate.resolve() for candidate in obsidian_candidates(executable)
                if candidate.is_file() and (sys.platform == "win32" or os.access(candidate, os.X_OK))), None)
    result = {
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 10),
        "obsidian": "detected" if app else "not-detected",
        "executable": str(app) if app else None,
        "vault": str(vault),
        "vault_ready": (vault / ".obsidian").is_dir(),
        "install_url": DOWNLOAD,
    }
    if app:
        result["next"] = "在 Obsidian 仓库管理器选择“打开本地文件夹”，选中上面的 vault 路径。"
    else:
        result["next"] = "请从 Obsidian 官方下载页安装客户端。若已安装在自定义目录，使用 --obsidian-executable 指定路径。Markdown 可直接读取。"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="领英运营工作台，本地知识库、台账与任务队列")
    parser.add_argument("--state", type=Path, default=default_state(), help="本地数据库存放目录")
    parser.add_argument("command", choices=("doctor", "vault", "knowledge", "ledger", "session"))
    parser.add_argument("args", nargs=argparse.REMAINDER, help="传给对应工具的参数")
    opts = parser.parse_args(argv)
    if opts.command == "doctor":
        diagnostic = argparse.ArgumentParser(prog="workbench.py doctor")
        diagnostic.add_argument("--vault")
        diagnostic.add_argument("--obsidian-executable")
        values = diagnostic.parse_args(opts.args)
        print(json.dumps(doctor(values.vault, values.obsidian_executable), ensure_ascii=False, indent=2))
        return 0
    scripts = ROOT / "scripts"
    db_args = []
    if opts.command in ("ledger", "session"):
        opts.state.mkdir(parents=True, exist_ok=True)
        db_args = ["--db", str(opts.state.resolve() / f"{opts.command}.sqlite3")]
    return subprocess.run([sys.executable, "-X", "utf8", str(scripts / f"{opts.command}.py"),
                           *db_args, *opts.args], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
