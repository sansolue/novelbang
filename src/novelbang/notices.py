"""The licenses of what the desktop app is made of, which have to travel with it.

A built app holds NovelBang's own code (MIT, see LICENSE), Python, the packages
NovelBang runs on and the Crimson Text font. NovelBang.spec writes their notices into
the app with write(); Settings → About shows them, along with NovelBang's license.
Run from source, the same text is put together from the installed packages.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

NOTICES_FILE = "THIRD-PARTY-NOTICES.txt"
LICENSE_FILE = "LICENSE"
FONT_LICENSE = Path(__file__).parent / "export" / "fonts" / "OFL.txt"
_LICENSE_NAME = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)
# Packages kept as plain .py files in the built app (NovelBang.spec, module_collection_mode),
# because their LGPL lets anyone swap in their own build of them.
LGPL_MODULES = {"fpdf2": "fpdf", "PyGObject": "gi", "pycairo": "cairo"}
# For MIT packages that name the license but ship no text of it.
MIT_TEXT = """MIT License

Copyright (c) {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."""


def _bundle_dir() -> Path | None:
    """Where a built app keeps its files (PyInstaller), or None when run from source."""
    return Path(getattr(sys, "_MEIPASS")) if getattr(sys, "frozen", False) else None


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def shipped(root: str = "novelbang", extras: tuple[str, ...] = ("desktop",)) -> list[md.Distribution]:
    """The installed packages `root` with `extras` needs on this platform, `root` left out."""
    from packaging.requirements import Requirement  # a build tool's dependency, so only needed here

    found: dict[str, md.Distribution] = {}
    todo = [(root, set(extras))]
    while todo:
        name, wanted = todo.pop()
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        for spec in dist.requires or []:
            req = Requirement(spec)
            if req.marker and not any(req.marker.evaluate({"extra": e}) for e in (wanted or {""})):
                continue
            key = _norm(req.name)
            if key not in found and key != _norm(root):
                if (sub := _distribution(req.name)) is not None:
                    found[key] = sub
                    todo.append((req.name, set(req.extras)))
    return sorted(found.values(), key=lambda d: _norm(d.metadata["Name"]))


def _distribution(name: str) -> md.Distribution | None:
    try:
        return md.distribution(name)
    except md.PackageNotFoundError:
        return None


def _license_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    if expr := meta.get("License-Expression"):
        return expr
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return "; ".join(classifiers) or (meta.get("License") or "see below").splitlines()[0]


def _license_texts(dist: md.Distribution) -> list[str]:
    texts = []
    for f in dist.files or []:
        in_meta = ".dist-info" in str(f.parent) or ".egg-info" in str(f.parent)
        if in_meta and _LICENSE_NAME.match(f.name):
            try:
                texts.append(Path(f.locate()).read_text("utf-8", errors="replace").strip())
            except OSError:
                continue
    meta = dist.metadata
    if not texts and (text := meta.get("License")) and "\n" in text:
        texts.append(text.strip())  # older packages put the whole text in the metadata
    if not texts and "MIT" in _license_of(dist):
        author = meta.get("Author") or re.sub(r"\s*<[^>]*>", "", meta.get("Author-email") or "")
        texts.append(MIT_TEXT.format(holder=author.strip() or f"the {meta['Name']} authors"))
    return texts


def _python_license() -> str:
    base = Path(sys.base_prefix)
    for path in (base / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt",
                 base / "LICENSE.txt"):
        if path.is_file():
            return path.read_text("utf-8", errors="replace").strip()
    return "Python Software Foundation License: https://docs.python.org/3/license.html"


def build_notices() -> str:
    dists = shipped()
    rule = "=" * 78
    lesser = [d for d in dists if "LGPL" in _license_of(d) or "Lesser General Public" in _license_of(d)]
    out = [
        "Third-party software in NovelBang",
        "",
        "NovelBang itself is under the MIT License (see its LICENSE). The app also contains",
        "the software below, each under its own license, reproduced here as it requires.",
    ]
    if lesser:
        names = ", ".join(f"{d.metadata['Name']} {d.version}" for d in lesser)
        out += [
            "",
            f"{names} {'is' if len(lesser) == 1 else 'are'} under the GNU Lesser General Public License.",
            "The app keeps the LGPL code as its unmodified Python source files, in the _internal",
            "folder (Contents/Resources on macOS), so you can replace it with your own version.",
            "The source is also at the PyPI address listed with each.",
        ]
    sections = [("Python", f"{sys.version.split()[0]}", "Python Software Foundation License",
                 "https://www.python.org/downloads/source/", [_python_license()])]
    for d in dists:
        name = d.metadata["Name"]
        sections.append((name, d.version, _license_of(d), f"https://pypi.org/project/{name}/{d.version}/",
                         _license_texts(d)))
    if FONT_LICENSE.is_file():
        sections.append(("Crimson Text (font, in the PDF export)", "", "SIL Open Font License 1.1",
                         "https://github.com/skosch/Crimson", [FONT_LICENSE.read_text("utf-8").strip()]))
    for name, version, license_, source, texts in sections:
        out += ["", rule, f"{name} {version}".strip(), f"License: {license_}", f"Source: {source}", rule]
        out += ["", *("\n\n".join(texts) or "(no license text included in the package)").splitlines()]
    return "\n".join(out) + "\n"


def write(folder: Path) -> Path:
    path = folder / NOTICES_FILE
    path.write_text(build_notices(), "utf-8", newline="\n")
    return path


def app_license() -> str:
    """NovelBang's own license text."""
    if (bundle := _bundle_dir()) and (bundle / LICENSE_FILE).is_file():
        return (bundle / LICENSE_FILE).read_text("utf-8")
    repo = Path(__file__).resolve().parents[2] / LICENSE_FILE
    if repo.is_file():
        return repo.read_text("utf-8")
    dist = _distribution("novelbang")
    return next(iter(_license_texts(dist)), "MIT License") if dist else "MIT License"


def third_party() -> str:
    """The notices built into the app, or put together now when run from source."""
    if (bundle := _bundle_dir()) and (bundle / NOTICES_FILE).is_file():
        return (bundle / NOTICES_FILE).read_text("utf-8")
    try:
        return build_notices()
    except ImportError:
        return "The third-party notices are built into the desktop app (they need the 'packaging' module)."
