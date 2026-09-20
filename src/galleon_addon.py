#!/usr/bin/env python3
"""Add or remove galleon-deck add-ons: ready-made profiles, themes and tools.

  galleon-addon list [--json]                    what's available and what's installed
  galleon-addon install FILE|FOLDER|NAME [--force]
                                                 install or upgrade an add-on from a downloaded
                                                 package (.tar.gz or .zip), an add-on folder,
                                                 or by name if it's already been unpacked
  galleon-addon remove NAME                      remove it again, and anything pointing at it
  galleon-addon run NAME COMMAND [ARGS]          run one of the add-on's commands
  galleon-addon pack FOLDER [-o DIR]             build a package from an add-on folder

Add-ons are separate downloads: nothing is installed unless you install it.
Packages are unpacked to ~/.local/share/galleon-deck-addons/NAME.

An add-on is a folder named after it, holding addon.toml plus any of:
  profiles/*.toml   copied to ~/.config/galleon-deck/profiles/
  themes/*.toml     copied to ~/.config/galleon-deck/themes/
  images/...        copied to ~/.config/galleon-deck/addons/NAME/images/ (refer to
                    them from keys as image = "addons/NAME/images/x.png")
The running service picks the files up within a second; nothing needs restarting.
"""

import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import galleon_deck as gd  # noqa: E402

DATA = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
STORE = os.path.join(DATA, "galleon-deck-addons")  # unpacked packages
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                         "galleon-deck", "addons")
FOLDERS = ("themes", "profiles", "images")  # install order: themes before the profiles using them
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
PACKAGE_EXTS = (".tar.gz", ".tgz", ".zip")


class AddonError(Exception):
    pass


def version(text):
    return tuple(int(x) for x in str(text).split(".") if x.isdigit())


def read_manifest(folder):
    with open(os.path.join(folder, "addon.toml"), "rb") as f:
        return {**tomllib.load(f), "dir": os.path.abspath(folder)}


def source_dirs():
    """Folders that hold add-ons: unpacked packages, plus a development checkout
    chosen in the app or config.toml ([addons] dir = "~/src/galleon-deck-addons")."""
    dirs = [STORE]
    try:
        with open(gd.CONFIG_PATH, "rb") as f:
            extra = tomllib.load(f).get("addons", {}).get("dir")
        if extra:
            dirs.append(os.path.expanduser(extra))
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return dirs


def available():
    """name -> manifest for every add-on we can see, installed ones included."""
    out = {}
    for base in source_dirs():
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name not in out and os.path.isfile(os.path.join(base, name, "addon.toml")):
                out[name] = read_manifest(os.path.join(base, name))
    for name in installed_names():  # installed from a folder that isn't listed above
        state = installed(name)
        if name not in out and os.path.isfile(os.path.join(state.get("source", ""), "addon.toml")):
            out[name] = read_manifest(state["source"])
    return out


def addon(name):
    addons = available()
    if name not in addons:
        raise AddonError(f"no add-on called {name!r} (have: {', '.join(addons) or 'none'}). "
                         "Download its package and install the file.")
    return addons[name]


def state_path(name):
    return os.path.join(STATE_DIR, name + ".json")


def installed(name):
    try:
        with open(state_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def installed_names():
    if not os.path.isdir(STATE_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(STATE_DIR) if f.endswith(".json"))


def targets(name, meta):
    """(source, destination) for every file the add-on installs, in install order."""
    out = []
    for folder in FOLDERS:
        src_root = os.path.join(meta["dir"], folder)
        dst_root = os.path.join(gd.CONFIG_DIR, "addons", name, "images") if folder == "images" \
            else os.path.join(gd.CONFIG_DIR, folder)
        for dirpath, _, files in os.walk(src_root):
            for fname in sorted(files):
                if folder != "images" and not fname.endswith(".toml"):
                    continue
                src = os.path.join(dirpath, fname)
                out.append((src, os.path.join(dst_root, os.path.relpath(src, src_root))))
    return out


def backup(name, paths):
    """Copy files aside before they're overwritten or removed. Returns the folder."""
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return None
    folder = os.path.join(STATE_DIR, "backups", f"{name}-{datetime.datetime.now():%Y%m%d-%H%M%S}")
    os.makedirs(folder, exist_ok=True)
    for p in paths:
        shutil.copy2(p, os.path.join(folder, os.path.basename(p)))
    return folder


def run_command(name, meta, command_line):
    argv = shlex.split(command_line)
    command = meta.get("commands", {}).get(argv[0])
    if not command:
        raise AddonError(f"{name} has no command {argv[0]!r} (have: {', '.join(meta.get('commands', {})) or 'none'})")
    exe = os.path.join(meta["dir"], command)
    if not os.access(exe, os.X_OK):  # zip packages don't keep the executable bit
        os.chmod(exe, os.stat(exe).st_mode | 0o755)
    env = {**os.environ, "GALLEON_DECK_CONFIG": gd.CONFIG_DIR, "GALLEON_ADDON_DIR": meta["dir"]}
    sys.stdout.flush()
    return subprocess.run([exe] + argv[1:], env=env).returncode


# ------------------------------------------------------------ packages ---

def is_package(path):
    return os.path.isfile(path) and path.lower().endswith(PACKAGE_EXTS)


def unpack(path):
    """Unpack a downloaded package into the store. Returns the add-on's name."""
    with tempfile.TemporaryDirectory(dir=DATA if os.path.isdir(DATA) else None) as tmp:
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    if member.startswith("/") or ".." in member.split("/"):
                        raise AddonError(f"{os.path.basename(path)}: unsafe path {member!r} in package")
                z.extractall(tmp)
        else:
            with tarfile.open(path) as t:
                for member in t.getmembers():
                    if (member.name.startswith("/") or ".." in member.name.split("/")
                            or not (member.isfile() or member.isdir())):
                        raise AddonError(f"{os.path.basename(path)}: unsafe entry {member.name!r} in package")
                t.extractall(tmp, filter="data") if hasattr(tarfile, "data_filter") else t.extractall(tmp)
        tops = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        if len(tops) != 1 or not os.path.isfile(os.path.join(tmp, tops[0], "addon.toml")):
            raise AddonError(f"{os.path.basename(path)} isn't a galleon-deck add-on "
                             "(expected one folder holding addon.toml)")
        name = tops[0]
        if not NAME_RE.fullmatch(name):
            raise AddonError(f"bad add-on name {name!r}")
        os.makedirs(STORE, exist_ok=True)
        dest = os.path.join(STORE, name)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.move(os.path.join(tmp, name), dest)
    return name


def pack(folder, out_dir="."):
    """Build NAME-VERSION.tar.gz from an add-on folder. Returns its path."""
    folder = os.path.abspath(folder.rstrip("/"))
    meta = read_manifest(folder)
    name = os.path.basename(folder)
    if not NAME_RE.fullmatch(name):
        raise AddonError(f"bad add-on folder name {name!r}: lowercase letters, digits, - and _")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{name}-{meta.get('version', '0')}.tar.gz")

    def clean(info):
        if "__pycache__" in info.name or info.name.endswith((".pyc", ".tmp")) or "/.git" in info.name:
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info
    with tarfile.open(out, "w:gz") as t:
        t.add(folder, arcname=name, filter=clean)
    return out


# ------------------------------------------------------------ commands ---

def listing():
    out = []
    for name, meta in available().items():
        state = installed(name)
        profiles = os.path.join(meta["dir"], "profiles")
        out.append({
            "name": name, "title": meta.get("title", name), "version": meta.get("version", "0"),
            "description": meta.get("description", ""), "requires": meta.get("requires", ""),
            "installed": state["version"] if state else None,
            "upgrade": bool(state) and version(state["version"]) < version(meta.get("version", "0")),
            "compatible": version(gd.VERSION) >= version(meta.get("requires", "0")),
            "commands": list(meta.get("commands", {})) if state else [],
            "profiles": sorted(f[:-5] for f in os.listdir(profiles) if f.endswith(".toml")) if os.path.isdir(profiles) else [],
            "source": meta["dir"],
        })
    return out


def cmd_list(as_json=False):
    addons = listing()
    if as_json:  # for the Galleon Deck app's Add-ons tab
        print(json.dumps(addons))
        return
    if not addons:
        print("No add-ons yet. Download one (a .tar.gz from the galleon-deck-addons releases)\n"
              "and install it with: galleon-addon install FILE")
    for a in addons:
        if not a["installed"]:
            status = "not installed"
        elif a["upgrade"]:
            status = f"installed {a['installed']}, upgrade available"
        else:
            status = f"installed {a['installed']}"
        print(f"{a['name']}  {a['version']}  [{status}]")
        print(f"    {a['title']}: {a['description']}")
    print(f"\ngalleon-deck {gd.VERSION}, config in {gd.CONFIG_DIR}")


def cmd_install(what, force=False):
    if is_package(what):
        name = unpack(what)
        meta = read_manifest(os.path.join(STORE, name))
    elif os.path.isfile(os.path.join(what, "addon.toml")):
        meta = read_manifest(what)
        name = os.path.basename(meta["dir"])
    else:
        name, meta = what, addon(what)
    if version(gd.VERSION) < version(meta.get("requires", "0")):
        raise AddonError(f"{name} needs galleon-deck {meta['requires']} or newer (this is {gd.VERSION}). "
                         "Update galleon-deck first.")
    state = installed(name) or {"files": []}
    plan = targets(name, meta)
    if not plan:
        raise AddonError(f"{name} has no profiles, themes or images to install")
    clashes = [dst for _, dst in plan if os.path.exists(dst) and dst not in state["files"]]
    if clashes and not force:
        raise AddonError("these files already exist and aren't from this add-on:\n  " + "\n  ".join(clashes)
                         + "\nRename them, or pass --force to back them up and replace them.")

    # Upgrades keep the theme you picked for each profile.
    kept = {}
    for _, dst in plan:
        if os.path.dirname(dst) == gd.PROFILE_DIR and os.path.isfile(dst):
            with open(dst, "rb") as f:
                old = tomllib.load(f)
            kept[dst] = {k: old[k] for k in ("theme", "theme_overrides") if k in old}
    saved = backup(name, [dst for _, dst in plan if os.path.dirname(dst) == gd.PROFILE_DIR] + clashes)

    for src, dst in plan:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
    themes = gd.all_themes()
    for dst, fields in kept.items():
        if fields.get("theme") in themes:
            gd.set_top_level(dst, "theme", fields["theme"])
        for key, value in fields.get("theme_overrides", {}).items():
            gd.set_value(dst, "theme_overrides", key, value)

    new_files = [dst for _, dst in plan]
    for path in state["files"]:  # files an older version had and this one doesn't
        if path not in new_files and os.path.isfile(path):
            os.remove(path)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(name), "w") as f:
        json.dump({"version": meta.get("version", "0"), "source": meta["dir"],
                   "installed": datetime.datetime.now().isoformat(timespec="seconds"),
                   "files": new_files}, f, indent=2)

    old_version = state.get("version")
    if not old_version:
        verb = f"Installed {name} {meta.get('version')}"
    elif old_version == meta.get("version"):
        verb = f"Reinstalled {name} {old_version}"
    else:
        verb = f"Upgraded {name} {old_version} -> {meta.get('version')}"
    print(verb + ":")
    for path in new_files:
        print("  " + path)
    if saved:
        print(f"Previous files backed up in {saved}")
    for hook in meta.get("hooks", {}).get("post_install", []):
        if run_command(name, meta, hook) != 0:
            print(f"galleon-addon: post-install step {hook!r} failed; the add-on is installed anyway", file=sys.stderr)


def cmd_remove(name):
    state = installed(name)
    if not state:
        raise AddonError(f"{name} isn't installed")
    files = state["files"]
    profiles = {os.path.splitext(os.path.basename(p))[0] for p in files if os.path.dirname(p) == gd.PROFILE_DIR}
    themes = {os.path.splitext(os.path.basename(p))[0] for p in files if os.path.dirname(p) == gd.THEME_DIR}

    # First point everything else away from what's going, so the service never
    # sees a config that refers to a missing profile or theme.
    with open(gd.CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    if cfg.get("start_profile") in profiles:
        gd.set_value(gd.CONFIG_PATH, None, "start_profile", None, remove=True)
        print(f"config.toml: start_profile was {cfg['start_profile']}; now the first profile")
    auto = cfg.get("auto_switch", {})
    if auto.get("fallback") in profiles:
        gd.set_value(gd.CONFIG_PATH, "auto_switch", "fallback", None, remove=True)
        print(f"config.toml: auto_switch fallback was {auto['fallback']}; removed")
    rules = auto.get("rules", [])
    keep = [r for r in rules if r.get("profile") not in profiles]
    if len(keep) != len(rules):
        gd.set_value(gd.CONFIG_PATH, "auto_switch", "rules", keep)
        print(f"config.toml: removed {len(rules) - len(keep)} auto_switch rule(s) pointing at {', '.join(sorted(profiles))}")
    for path in sorted(os.listdir(gd.PROFILE_DIR)):
        full = os.path.join(gd.PROFILE_DIR, path)
        if not path.endswith(".toml") or full in files:
            continue
        with open(full, "rb") as f:
            theme = tomllib.load(f).get("theme")
        if theme in themes:
            gd.set_top_level(full, "theme", gd.DEFAULT_THEME)
            print(f"profiles/{path}: theme {theme} is going; switched to {gd.DEFAULT_THEME}")

    saved = backup(name, [p for p in files if os.path.dirname(p) == gd.PROFILE_DIR])
    for path in files:
        if os.path.isfile(path):
            os.remove(path)
    images = os.path.join(gd.CONFIG_DIR, "addons", name)
    if os.path.isdir(images):
        shutil.rmtree(images)
    os.remove(state_path(name))
    unpacked = os.path.join(STORE, name)
    if os.path.realpath(state.get("source", "")) == os.path.realpath(unpacked) and os.path.isdir(unpacked):
        shutil.rmtree(unpacked)  # a downloaded package: gone with it; install the file again to get it back
    print(f"Removed {name} ({len(files)} files).")
    if saved:
        print(f"Its profile, with your binds and theme, is backed up in {saved}")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "list":
            cmd_list(as_json="--json" in rest)
        elif cmd == "install" and rest:
            cmd_install(rest[0], force="--force" in rest[1:])
        elif cmd == "remove" and rest:
            cmd_remove(rest[0])
        elif cmd == "run" and len(rest) >= 2:
            sys.exit(run_command(rest[0], addon(rest[0]), shlex.join(rest[1:])))
        elif cmd == "pack" and rest:
            out_dir = rest[rest.index("-o") + 1] if "-o" in rest[1:-1] else "."
            print(pack(rest[0], out_dir))
        else:
            sys.exit("usage: galleon-addon list | install FILE|FOLDER|NAME [--force] | remove NAME"
                     " | run NAME COMMAND [ARGS] | pack FOLDER [-o DIR]")
    except (AddonError, OSError, tarfile.TarError, zipfile.BadZipFile, tomllib.TOMLDecodeError) as e:
        sys.exit(f"galleon-addon: {e}")


if __name__ == "__main__":
    main()
