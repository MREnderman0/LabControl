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


def directory(value: str) -> Path:
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
