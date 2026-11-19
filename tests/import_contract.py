"""Modules covered by both the source-tree and clean-install import tests."""

import sys
from pathlib import Path


MACOS_ONLY_MODULES = {
    "sidepulse.led_wasm",
    "sidepulse.status_bar",
    "sidepulse.status_icon",
    "sidepulse.virtual_device",
}


def importable_module_names(src_root: Path):
    for path in sorted(src_root.rglob("*.py")):
        if path.name == "__main__.py":
            continue  # runs the CLI on import
        parts = path.relative_to(src_root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join(parts)
        if name and (sys.platform == "darwin" or name not in MACOS_ONLY_MODULES):
            yield name
