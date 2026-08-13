"""Offline Blender documentation: the Python API reference and the user manual.

Local HTML copies of the docs for the Blender this tooling drives. They answer
a question the reference C++ source cannot: not "how does this work" but "does
this exist in *my* Blender, and what exactly is it called". Source trees track
main and run ahead of any release, so reading them leads to APIs that are not
there yet; these docs are version-stamped and cannot mislead that way.

No Blender process is needed, which makes this the cheap first stop - and the
only one available when Blender is closed or busy.

Lookup goes through Sphinx's own `objects.inv`: ~180 KB listing every documented
symbol, its kind, and its page. Grepping 1.6 GB of HTML is the fallback, for
prose in the manual that is not a symbol at all.

Point BLENDER_DOCS_DIR at another copy to override the bundled location.
"""

import html
import os
import re
import zlib
from pathlib import Path

DOCS_ENV = "BLENDER_DOCS_DIR"

# Directory names carry their version, so match by shape rather than pinning a
# release - re-downloading docs for a new Blender should just work.
API_GLOB = "blender_python_reference_*"
MANUAL_GLOB = "blender_manual_*"

_inventory_cache = {}


def docs_root():
    """Directory holding the downloaded doc sets, or None if absent."""
    override = os.getenv(DOCS_ENV)
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    # src/blender_dev_mcp/docs.py -> repo root
    default = Path(__file__).resolve().parents[2] / "docs" / "Blender Documentation"
    return default if default.is_dir() else None


def _doc_set(glob):
    root = docs_root()
    if root is None:
        return None
    return next((p for p in sorted(root.glob(glob)) if p.is_dir()), None)


def api_dir():
    return _doc_set(API_GLOB)


def manual_dir():
    return _doc_set(MANUAL_GLOB)


def inventory():
    """[(symbol, kind, page)] from objects.inv, plus the version it documents.

    Returns ([], None) when the docs are not installed.
    """
    api = api_dir()
    if api is None:
        return [], None
    inv = api / "objects.inv"
    if not inv.is_file():
        return [], None

    key = (str(inv), inv.stat().st_mtime)
    if key in _inventory_cache:
        return _inventory_cache[key]

    raw = inv.read_bytes()
    # Sphinx inventory v2: four plain-text header lines, then a zlib payload.
    header_end, seen = None, 0
    for i, byte in enumerate(raw):
        if byte == 0x0A:
            seen += 1
            if seen == 4:
                header_end = i
                break
    if header_end is None:
        return [], None

    header = raw[:header_end].decode("utf-8", "replace")
    version = None
    for line in header.splitlines():
        if line.startswith("# Project:"):
            version = line.split(":", 1)[1].strip()

    entries = []
    for line in zlib.decompress(raw[header_end + 1:]).decode("utf-8").splitlines():
        # "name domain:role priority uri displayname"
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        name, role, _priority, uri = parts[:4]
        entries.append((name, role.partition(":")[2] or role, uri.replace("#$", "")))

    _inventory_cache.clear()
    _inventory_cache[key] = (entries, version)
    return entries, version


def find_symbols(query, limit=20):
    """Inventory entries matching `query`, most exact first."""
    entries, _version = inventory()
    if not entries:
        return []
    needle = query.lower().strip()

    exact, tail, loose = [], [], []
    for entry in entries:
        name = entry[0]
        lowered = name.lower()
        if lowered == needle:
            exact.append(entry)
        elif lowered.rsplit(".", 1)[-1] == needle:
            tail.append(entry)
        elif needle in lowered:
            loose.append(entry)

    ranked = exact + tail + sorted(loose, key=lambda e: len(e[0]))

    # Sphinx lists some symbols twice (a "doc" role beside the real one); the
    # duplicate adds nothing and crowds out genuine near-misses.
    seen, unique = set(), []
    for entry in ranked:
        if entry[0] in seen:
            continue
        seen.add(entry[0])
        unique.append(entry)
    return unique[:limit]


_BLOCK_END = re.compile(
    r"</(p|div|li|tr|h[1-6]|dt|dd|section|article|pre|blockquote)>", re.I)
_DROP = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_ARTICLE = re.compile(r'<article\b[^>]*>(.*?)</article>', re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def page_text(path, max_chars=6000):
    """Readable text of one doc page.

    Takes only the Furo theme's <article> element when present, so the nav
    sidebar, search box and footer chrome stay out of the result.
    """
    path = Path(path)
    if not path.is_file():
        return ""
    raw = path.read_text(encoding="utf-8", errors="replace")

    match = _ARTICLE.search(raw)
    body = match.group(1) if match else raw
    body = _DROP.sub(" ", body)
    body = _BLOCK_END.sub("\n", body)
    body = _TAG.sub(" ", body)
    body = html.unescape(body)

    # Sphinx puts a pilcrow after every heading as a permalink handle, and
    # renders quotes typographically. Neither survives usefully as plain text,
    # and curly quotes stop an enum value being copied straight into code.
    body = body.replace("¶", "")
    body = body.translate(str.maketrans({
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", " ": " ",
    }))

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in body.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated at {max_chars} chars]"
    return text


# Generated index pages list every term in the docs, so they match nearly any
# query while explaining nothing. _sources holds the reStructuredText originals,
# which duplicate content already covered by the rendered pages.
_SKIP_PAGES = re.compile(r"(^|[\\/])(genindex|search|py-modindex)|[\\/]_sources[\\/]", re.I)


def search_prose(query, limit=8, max_files=6000):
    """Pages whose text contains `query`, with a snippet of context.

    For things that are not documented symbols - enum values, manual prose.
    Reads files instead of consulting an index, so it is the slow path by
    design; the API reference is searched first because a developer asking this
    tooling a question almost always wants the Python side, and finding enough
    hits there means the larger manual is never touched.
    """
    needle = query.lower()
    hits = []
    for directory in (api_dir(), manual_dir()):
        if directory is None or len(hits) >= limit:
            continue
        scanned = 0
        for path in sorted(directory.rglob("*.html")):
            if scanned >= max_files or len(hits) >= limit:
                break
            if _SKIP_PAGES.search(str(path)):
                continue
            scanned += 1
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle not in raw.lower():
                continue
            text = page_text(path, max_chars=200_000)
            spot = text.lower().find(needle)
            if spot < 0:
                continue  # matched only in markup or nav chrome
            start = max(0, spot - 200)
            hits.append((
                str(path.relative_to(directory.parent)),
                re.sub(r"\s+", " ", text[start:spot + 300]).strip(),
            ))
    return hits
