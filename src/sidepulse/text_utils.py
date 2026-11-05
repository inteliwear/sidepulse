from __future__ import annotations

from pathlib import Path


def read_recent_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []

    chunk_size = 8192
    chunks: list[bytes] = []
    newline_count = 0

    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    trimmed = text[: max_len - 1].rstrip()
    boundary = max(trimmed.rfind(" "), trimmed.rfind(","), trimmed.rfind(";"))
    if boundary >= max_len // 2:
        trimmed = trimmed[:boundary].rstrip()
    return f"{trimmed}..."
