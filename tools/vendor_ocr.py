"""Vendor RapidOCR (and dependencies) wheels into ./.pylibs without using pip.

Why this exists
---------------
The development sandbox puts a ``DeleteSubdirectoriesAndFiles`` *Deny* ACE on the
workspace root.  ``pip install`` deletes a temporary ``*.whl.metadata`` file while
installing, which the ACE blocks, so pip always fails with ``Errno 13``.

This script performs the same job with just the standard library:

1. query the PyPI JSON API for wheels matching the running interpreter,
2. download each wheel into ``.wheels/``,
3. unzip it into ``.pylibs/``.

It is only needed to *bootstrap* an offline OCR runtime next to the project.  On a
normal machine ``pip install -r requirements.txt`` is the supported path.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import sysconfig
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
WHEEL_DIR = ROOT / ".wheels"
LIB_DIR = ROOT / ".pylibs"

# Marker so wheels already handled in a previous run are not unzipped twice.
DONE = ".vendor_done"

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
PYPI_SIMPLE = "https://pypi.org/pypi/{name}/json"

# The wheel platform tag this interpreter can load, e.g. "win_amd64" or "win32".
_PLATFORM_TAG = sysconfig.get_platform().replace("-", "_").replace(".", "_")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "vendor-ocr/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wheel_rank(filename: str) -> int | None:
    """Score a wheel for the running interpreter, or None when unusable.

    Wheels are named ``{name}-{version}-{pytag}-{abitag}-{plattag}.whl``; the name
    and the version may themselves contain hyphens, so the tag triple is matched
    from the right-hand end.

    Higher is better: an exact platform match beats a pure-Python ``any`` wheel,
    which beats an ``abi3`` wheel that only *usually* matches.
    """
    m = re.match(r"^(.+?)-([^-]+)-([^-]+)-([^-]+)-([^-]+)\.whl$", filename)
    if not m:
        return None
    _, _, py, abi, plat = m.groups()

    if plat != "any" and plat.replace(".", "_") != _PLATFORM_TAG:
        return None  # e.g. a win32 wheel on a win_amd64 interpreter

    cp = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if abi in (cp, "abi3"):
        return 3 if plat == _PLATFORM_TAG else 2
    if abi == "none" and py in ("py3", f"py{sys.version_info.major}"):
        return 1
    return None


def _pick_wheel(releases: dict, prefer_version: str | None) -> tuple[str, str] | None:
    versions = list(releases.keys())
    if prefer_version:
        versions = [v for v in versions if v == prefer_version] or versions
    # newest first, skipping pre-releases
    def key(v: str):
        return [int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", v)[:3]]

    stable = [v for v in versions if not re.search(r"(a|b|rc|dev)\d*$", v)]
    for version in sorted(stable or versions, key=key, reverse=True):
        ranked = []
        for f in releases[version]:
            if not f["filename"].endswith(".whl"):
                continue
            rank = _wheel_rank(f["filename"])
            if rank is not None:
                ranked.append((rank, f["url"]))
        if ranked:
            ranked.sort(key=lambda item: item[0], reverse=True)
            return version, ranked[0][1]
    return None


def _download(url: str, dest: pathlib.Path) -> pathlib.Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"    cached {dest.name}")
        return dest
    print(f"    GET   {dest.name}")
    req = urllib.request.Request(url, headers={"User-Agent": "vendor-ocr/1.0"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    os.replace(tmp, dest)  # rename, never delete: the sandbox Deny ACE blocks unlink
    return dest


def _unzip(wheel: pathlib.Path, target: pathlib.Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        if any(n.startswith("..") for n in names):  # defensive path-traversal guard
            raise RuntimeError(f"unsafe wheel contents: {wheel}")
        z.extractall(target)
    print(f"    unzip -> {target.relative_to(ROOT)}")


def _deps_from_metadata(target: pathlib.Path) -> list[str]:
    """Read Requires-Dist entries from the dist-info we just unpacked."""
    deps: list[str] = []
    for meta in target.glob("*.dist-info/METADATA"):
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Requires-Dist:"):
                value = line.split(":", 1)[1].strip()
                if "extra ==" in value:  # optional extra, e.g. [cpu]
                    continue
                name = re.split(r"[<>=!;\[ ]", value, maxsplit=1)[0].strip()
                if name:
                    deps.append(name)
    return deps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("packages", nargs="+", help="top-level requirement names")
    ap.add_argument(
        "--version",
        action="append",
        default=[],
        metavar="NAME==VERSION",
        help="pin a package version",
    )
    args = ap.parse_args()
    pins = dict(p.split("==", 1) for p in args.version)

    LIB_DIR.mkdir(parents=True, exist_ok=True)
    queue = list(dict.fromkeys(args.packages))
    seen: set[str] = set()
    installed: list[str] = []

    while queue:
        name = queue.pop(0).replace("_", "-").lower()
        if name in seen:
            continue
        seen.add(name)
        print(f"[{len(seen)}] {name}")
        try:
            data = _get_json(PYPI_SIMPLE.format(name=name))
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"    SKIP  {name}: {exc}")
            continue
        picked = _pick_wheel(data["releases"], pins.get(name))
        if not picked:
            print(f"    SKIP  {name}: no compatible wheel")
            continue
        version, url = picked
        wheel = _download(url, WHEEL_DIR / url.rsplit("/", 1)[-1])
        _unzip(wheel, LIB_DIR)
        installed.append(f"{name}=={version}")
        (LIB_DIR / DONE).write_text("\n".join(installed), encoding="utf-8")
        for dep in _deps_from_metadata(LIB_DIR):
            if dep.replace("_", "-").lower() not in seen:
                queue.append(dep)

    print("\nInstalled into .pylibs:")
    for line in installed:
        print("  ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
