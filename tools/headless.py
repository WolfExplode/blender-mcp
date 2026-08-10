"""Run a script inside headless Blender, with your addon repo importable.

Unlike the Blender MCP connection, this shows full stderr and tracebacks, uses
--factory-startup so results do not depend on user prefs, and never touches a
running Blender session. Handy for iterating on addon code before restarting
the real Blender.

    python tools/headless.py probe.py                 # newest Blender
    python tools/headless.py probe.py -v 4.2          # a specific version
    python tools/headless.py -c "import bpy; print(bpy.app.version)"
    python tools/headless.py probe.py --addon Bweight # register addons first
    python tools/headless.py --list                   # installed versions

The current working directory is put on sys.path so `import MyAddon` works when
run from an addon repo; add more roots with --path. Args after `--` reach the
script as sys.argv[1:]. Exits nonzero if the script raises.
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile

BLENDER_GLOB = r"C:\Program Files\Blender Foundation\Blender *\blender.exe"


def _version_key(path):
    m = re.search(r"Blender ([\d.]+)", path)
    return tuple(int(p) for p in m.group(1).split(".")) if m else (0,)


def find_blenders():
    """Installed Blender executables, oldest first."""
    return sorted(glob.glob(BLENDER_GLOB), key=_version_key)


def resolve(version=None):
    found = find_blenders()
    if not found:
        sys.exit(f"No Blender found matching {BLENDER_GLOB}")
    if version is None:
        return found[-1]
    for path in found:
        if re.search(rf"Blender {re.escape(version)}[\\ ]", path + "\\"):
            return path
    available = ", ".join(re.search(r"Blender ([\d.]+)", p).group(1) for p in found)
    sys.exit(f"Blender {version} not found. Available: {available}")


PRELUDE = """\
import sys
for _p in {paths!r}:
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.argv = [{name!r}] + {args!r}
"""

# The body runs from a temp file, so __file__ would otherwise point at the temp
# dir and break scripts that resolve paths relative to themselves.
FILE_PRELUDE = """\
__file__ = {file!r}
"""

ADDON_PRELUDE = """\
import addon_utils
for _name in {addons!r}:
    addon_utils.enable(_name, default_set=False, persistent=False)
    print('[headless] enabled addon:', _name)
"""


def build_script(body, name, args, addons, paths, file_path=None):
    parts = [PRELUDE.format(paths=paths, name=name, args=args)]
    if file_path:
        parts.append(FILE_PRELUDE.format(file=file_path))
    if addons:
        parts.append(ADDON_PRELUDE.format(addons=addons))
    parts.append(body)
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", nargs="?", help="Python file to run inside Blender")
    ap.add_argument("-c", "--code", help="Inline code to run instead of a file")
    ap.add_argument("-v", "--version", help="Blender version, e.g. 4.2 (default: newest)")
    ap.add_argument("--addon", action="append", default=[], metavar="NAME",
                    help="Enable an addon before running; repeatable")
    ap.add_argument("--path", action="append", default=[], metavar="DIR",
                    help="Extra sys.path root; repeatable (cwd is always added)")
    ap.add_argument("--blend", metavar="FILE", help="Open a .blend file first")
    ap.add_argument("--keep-prefs", action="store_true",
                    help="Do not pass --factory-startup")
    ap.add_argument("--list", action="store_true", help="List installed Blender versions")
    ap.add_argument("args", nargs="*", help="Args passed through to the script")
    opts = ap.parse_args()

    if opts.list:
        for path in find_blenders():
            print(re.search(r"Blender ([\d.]+)", path).group(1), "->", path)
        return 0

    file_path = None
    if opts.code:
        body, name = opts.code, "<inline>"
        # With -c there is no script path, so the positional argparse assigned
        # to `script` is really the first pass-through arg.
        if opts.script:
            opts.args.insert(0, opts.script)
    elif opts.script:
        with open(opts.script, encoding="utf-8") as fh:
            body = fh.read()
        name = file_path = os.path.abspath(opts.script)
    else:
        ap.error("give a script path or -c/--code")

    paths = [os.path.abspath(p) for p in opts.path] + [os.getcwd()]

    blender = resolve(opts.version)
    cmd = [blender, "--background"]
    if opts.blend:
        cmd.append(os.path.abspath(opts.blend))
    if not opts.keep_prefs:
        cmd.append("--factory-startup")

    # Blender needs a real file; a temp one keeps the prelude out of your tree.
    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp.write(build_script(body, name, opts.args, opts.addon, paths, file_path))
        tmp.close()
        # Blender executes CLI args in order, so --python-exit-code only
        # applies to a --python that comes after it.
        cmd += ["--python-exit-code", "1", "--python", tmp.name]
        return subprocess.run(cmd).returncode
    finally:
        os.unlink(tmp.name)


if __name__ == "__main__":
    sys.exit(main())
