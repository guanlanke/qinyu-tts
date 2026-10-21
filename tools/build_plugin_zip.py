"""按 AstrBot 插件安装规范打包 zip 并验证。

背景（踩过的坑）：Windows 的「右键压缩」和 PowerShell `Compress-Archive`
会把 zip 条目写成反斜杠（`astrbot_plugin_qinyu\\main.py`）。
AstrBot 跑在 Linux 容器里，`zipfile.extractall` 不把反斜杠当路径分隔符，
于是只会解出一个文件名含反斜杠的单文件，插件目录并不存在，
安装时直接抛：

    FileNotFoundError: [Errno 2] No such file or directory:
    '.../plugin/astrbot_plugin_qinyu'

本脚本用正斜杠重新打包，并复刻 AstrBot 的
`_resolve_archive_root_dir` / `_finalize_extracted_archive` 逻辑做安装验证。

用法：
    python tools/build_plugin_zip.py [输出zip路径]
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent if HERE.name == "tools" else HERE

#: zip 内顶层目录名必须与 metadata.yaml 的 name 一致
PLUGIN_NAME = "astrbot_plugin_qinyu"

EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd"}

#: 安装后必须存在的关键文件
REQUIRED_FILES = (
    "metadata.yaml",
    "main.py",
    "__init__.py",
    "_conf_schema.json",
    "engines/__init__.py",
    "engines/base.py",
    "engines/mimo/__init__.py",
    "engines/mimo/v2.py",
    "engines/mimo/v2_5.py",
    "engines/mimo/singing.py",
    # 可视化视图：漏掉这三个文件，WebUI 里就没有「唱歌控制」面板
    "views/singing/index.html",
    "views/singing/app.js",
    "views/singing/style.css",
)


def collect(root: Path) -> list[tuple[Path, Path]]:
    items: list[tuple[Path, Path]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        if path.is_file():
            items.append((path, rel))
    return items


def build_zip(src: Path, dst: Path) -> None:
    items = collect(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    parents = sorted({rel.parent for _, rel in items if str(rel.parent) != "."})
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as archive:
        for parent in parents:
            archive.writestr(f"{PLUGIN_NAME}/{parent.as_posix()}/", b"")
        for path, rel in items:
            archive.write(path, f"{PLUGIN_NAME}/{rel.as_posix()}")
    print(f"[build] {len(items)} 个文件 -> {dst}")


# ---------- 以下函数复刻 AstrBot 的实现，用来真实验证 ----------


def resolve_archive_root_dir(entries: list[str]) -> str:
    """astrbot/core/zip_updater.py::_RepoZipUpdater._resolve_archive_root_dir"""
    normalized_entries = [os.path.normpath(entry) for entry in entries]
    portable_entries = [entry.replace("\\", "/") for entry in normalized_entries]
    root_candidates: list[str] = []

    for raw_entry, normalized_entry, portable_entry in zip(
        entries, normalized_entries, portable_entries, strict=False
    ):
        if normalized_entry == ".":
            continue

        has_children = any(
            other_entry != portable_entry
            and other_entry.startswith(f"{portable_entry}/")
            for other_entry in portable_entries
        )
        if raw_entry.endswith(("/", "\\")) or has_children:
            root_candidates.append(normalized_entry)
            continue

        parent_portable, _, _ = portable_entry.rpartition("/")
        if not parent_portable:
            return ""
        root_candidates.append(parent_portable.replace("/", os.sep))

    if not root_candidates:
        return ""
    return os.path.commonpath(root_candidates)


def find_plugin_metadata_entry(entries: list[str]) -> str | None:
    """astrbot/core/star/updater.py::_PluginUpdater.find_plugin_metadata_entry"""
    update_dir = resolve_archive_root_dir(entries)
    portable_update_dir = os.path.normpath(update_dir).replace("\\", "/")
    if portable_update_dir == ".":
        portable_update_dir = ""

    entries_by_portable_path = {}
    for entry in entries:
        portable_entry = os.path.normpath(entry).replace("\\", "/")
        if portable_entry in ("", "."):
            continue
        entries_by_portable_path[portable_entry] = entry

    candidates = (
        [f"{portable_update_dir}/{name}" for name in ("metadata.yaml", "metadata.yml")]
        if portable_update_dir
        else ["metadata.yaml", "metadata.yml"]
    )
    for candidate in candidates:
        if candidate in entries_by_portable_path:
            return entries_by_portable_path[candidate]
    return None


def simulate_install(zip_path: Path) -> Path:
    """复刻 _extract_plugin_archive + _finalize_extracted_archive 的落盘过程。"""
    target_dir = Path(tempfile.mkdtemp(prefix="astrbot-plugin-verify-")) / PLUGIN_NAME
    target_dir.mkdir(parents=True)

    with zipfile.ZipFile(zip_path, "r") as archive:
        entries = archive.namelist()
        if find_plugin_metadata_entry(entries) is None:
            raise AssertionError("AstrBot 逻辑判定：压缩包里找不到 metadata.yaml")
        update_dir = resolve_archive_root_dir(entries)
        archive.extractall(target_dir)

    update_root = os.path.normpath(os.path.join(str(target_dir), update_dir))
    if not os.path.isdir(update_root):
        raise AssertionError(
            f"AstrBot 逻辑判定：解压后不存在 {update_root}（这正是线上报错的形态）"
        )

    for name in os.listdir(update_root):
        src_item = os.path.join(update_root, name)
        dst_item = os.path.join(str(target_dir), name)
        if os.path.isdir(dst_item):
            shutil.rmtree(dst_item)
        elif os.path.exists(dst_item):
            os.remove(dst_item)
        shutil.move(src_item, str(target_dir))
    shutil.rmtree(update_root, ignore_errors=True)
    return target_dir


def verify(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as archive:
        entries = archive.namelist()

    backslashes = [name for name in entries if "\\" in name]
    if backslashes:
        raise AssertionError(f"仍有反斜杠条目：{backslashes[:3]}")

    if any(name.startswith("/") or ".." in Path(name).parts for name in entries):
        raise AssertionError("存在绝对路径或路径穿越条目")

    installed = simulate_install(zip_path)
    missing = [rel for rel in REQUIRED_FILES if not (installed / rel).is_file()]
    if missing:
        raise AssertionError(f"安装后缺少文件：{missing}")

    metadata = (installed / "metadata.yaml").read_text(encoding="utf-8")
    for field in ("name:", "desc:", "version:", "author:"):
        if field not in metadata:
            raise AssertionError(f"metadata.yaml 缺少必需字段 {field}")

    print("[verify] 条目分隔符：全部正斜杠 OK")
    print(f"[verify] 条目数量：{len(entries)}")
    print(f"[verify] AstrBot 解析出的根目录：{resolve_archive_root_dir(entries)!r}")
    print(f"[verify] metadata 条目：{find_plugin_metadata_entry(entries)}")
    print(f"[verify] 必需文件齐全：{len(REQUIRED_FILES)} 项 OK")
    print("[verify] 结论：可被 AstrBot 正常解压安装")
    shutil.rmtree(installed.parent, ignore_errors=True)


def main() -> int:
    dst = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC.parent / f"{PLUGIN_NAME}.zip"
    build_zip(SRC, dst)
    verify(dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
