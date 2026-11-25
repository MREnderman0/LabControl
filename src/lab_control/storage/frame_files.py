"""Validate a local frame destination and publish a closed set without replacing files."""

import os
from itertools import count
from pathlib import Path, PureWindowsPath


def filename(stem: str) -> str:
    if (
        not stem
        or len(stem) > 180
        or stem != stem.strip()
        or stem.endswith(".")
        or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in stem)
        or PureWindowsPath(stem).is_reserved()
    ):
        raise ValueError("请输入有效的文件主名，不含目录或 Windows 保留字符")
    return stem


def directory(value: str, root: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.anchor.startswith(("\\\\", "//")) or ".." in path.parts:
        raise ValueError("请选择本机绝对目录")
    for part in path.parts[1:]:
        if (
            part.endswith((" ", "."))
            or PureWindowsPath(part).is_reserved()
            or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
        ):
            raise ValueError("目录包含无效的 Windows 路径")
    if not root.is_absolute() or root == Path(root.anchor) or not path.is_relative_to(root):
        raise ValueError("所选目录必须在配置的导出目录内")
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError("导出目录不能经过符号链接或目录联接")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("所选目录超出导出范围")
    return path


def publish(
    folder: Path, stem: str, staged: dict[str, Path], check=lambda: None, commit=lambda: None
) -> dict[str, Path]:
    """Stages live on the destination volume. Keep each candidate set together on collisions."""
    for number in count(1):
        suffix = "" if number == 1 else f"-{number:02d}"
        paths = {ext: folder / f"{stem}{suffix}.{ext}" for ext in staged}
        if any(path.exists() for path in paths.values()):
            continue
        moved = []
        try:
            for ext, target in paths.items():
                check()
                if os.name == "nt":
                    staged[ext].rename(target)  # Windows rename fails on an existing target.
                else:
                    os.link(staged[ext], target)
                moved.append(ext)
            check()
            commit()
            return paths
        except BaseException as exc:
            for ext in reversed(moved):
                if os.name == "nt":
                    paths[ext].rename(staged[ext])
                else:
                    paths[ext].unlink()
            if not isinstance(exc, FileExistsError):
                raise
    raise AssertionError("unreachable")
