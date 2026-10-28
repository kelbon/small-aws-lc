#!/usr/bin/env python3
"""Builds a slim copy of AWS-LC (https://github.com/aws/aws-lc): only what is needed to build
libcrypto and libssl with CMake, without tests, test vectors, fuzzing corpora, docs and tools.

usage:
    python update_aws_lc.py --tag v5.12.0 [--dest DIR] [--check]

First run: DIR must not exist or be empty. The script fills it, then you do `git init`, commit,
tag and push. Update: run the script with a new tag on the existing repository folder, then commit,
tag and push. The script refuses to clean a non-empty folder that it did not create (no marker file).

File selection is based on the include list AWS uses to package AWS-LC for Rust (Cargo.toml of
the aws-lc-sys crate in https://github.com/aws/aws-lc-rs), plus all license files and README.md.
Files are copied unmodified, only README.md gets a header.

Requires Python 3.8+ and git. --check additionally requires CMake (and Ninja, if available).
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

UPSTREAM_URL = "https://github.com/aws/aws-lc"
MARKER = ".aws-lc-slim.json"
SCRIPT_NAME = "update_aws_lc.py"
# kept in the destination folder on update (everything else is replaced)
KEEP = {".git", ".github", MARKER, SCRIPT_NAME}

# gitignore-like patterns relative to the AWS-LC root, the last matching pattern wins.
# Taken from aws-lc-sys 0.46.0 Cargo.toml `include` (patterns for /aws-lc/, prefix removed)
INCLUDE = [
    "LICENSE",
    "/**/*.c",
    "/**/*.cc",
    "/**/*.pl",
    "/**/*.h",
    "/**/*.h.in",
    "/**/*.pc.in",
    "/**/*.i",
    "/**/*.S",
    "/**/*.asm",
    "/**/CMakeLists.txt",
    "/**/*.cmake",
    "/**/*.errordata",
    "/**/err_data_generate.go",
    "/**/*.lds",
    "/**/*.inc",
    "!/bindings/**",
    "!/docs/**",
    "!/fuzz/**",
    "!/generated-src/crypto_test_data.cc",
    "!/tests/**",
    "!/third_party/**",
    "!/util/**",
    "!/tool/**",
    "!/tool-openssl/**",
    "/tests/compiler_features_tests/**.c",
    "/third_party/fiat/**/LICENSE",
    "/third_party/fiat/**/*.h",
    "/third_party/s2n-bignum/**/*.S",
    "/third_party/s2n-bignum/**/*.h",
    "/third_party/jitterentropy/**/*.c",
    "/third_party/jitterentropy/**/*.h",
    "/third_party/jitterentropy/CMakeLists.txt",
    "!/third_party/jitterentropy/jitterentropy-library/tests/**",
    "/util/fipstools/**/CMakeLists.txt",
    "/util/fipstools/**/*.c",
    # additions: licenses of everything that is kept (aws-lc-sys has its own license list) and README
    "LICENSE*",
    "NOTICE*",
    "COPYING*",
    "/README.md",
]


def pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    """gitignore-like glob: leading '/' or a '/' inside anchors to the root, otherwise matches a file name
    in any folder. '**/' - any number of folders, trailing '/**' - everything inside, other '**' and '*' -
    anything except '/'"""
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    p = pattern.lstrip("/")
    out = ""
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif p.startswith("/**", i) and i + 3 == len(p):
            out += "/.*"
            i += 3
        elif p.startswith("**", i):
            out += "[^/]*"
            i += 2
        elif p[i] == "*":
            out += "[^/]*"
            i += 1
        elif p[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile(("^" if anchored else "^(?:.*/)?") + out + "$")


RULES = [(not p.startswith("!"), pattern_to_regex(p.lstrip("!"))) for p in INCLUDE]


def is_included(relpath: str) -> bool:
    result = False
    for include, rx in RULES:
        if rx.match(relpath):
            result = include
    return result


def tree_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            yield path.relative_to(root).as_posix(), path


def size_mb(paths) -> float:
    return sum(p.stat().st_size for p in paths) / (1024 * 1024)


def rmtree(path: Path):
    # git objects are read-only on Windows
    def onerror(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=onerror)


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def check_destination(dest: Path) -> bool:
    """returns True if it is the first run. Exits if the folder is not ours"""
    if not dest.exists() or not any(dest.iterdir()):
        return True
    marker = dest / MARKER
    if not marker.is_file():
        sys.exit(f"error: '{dest}' is not empty and has no {MARKER}, refusing to touch it. "
                 "Use an empty folder for the first run or the folder created by this script for updates")
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except ValueError:
        sys.exit(f"error: {marker} is not valid JSON, refusing to touch '{dest}'")
    if info.get("upstream") != UPSTREAM_URL:
        sys.exit(f"error: {marker} describes another upstream ({info.get('upstream')}), refusing to touch '{dest}'")
    return False


def clean_destination(dest: Path):
    for entry in dest.iterdir():
        if entry.name in KEEP:
            continue
        if entry.is_dir() and not entry.is_symlink():
            rmtree(entry)
        else:
            entry.unlink()


def readme_header(tag: str, commit: str, full_mb: float, slim_mb: float) -> str:
    return f"""# AWS-LC slim

This repository is [AWS-LC]({UPSTREAM_URL}) `{tag}` (commit `{commit}`) with tests, test vectors,
fuzzing corpora, documentation and tools removed. Only what is needed to build `libcrypto` and `libssl`
with CMake is kept, which reduces the source tree from {full_mb:.0f} MB to {slim_mb:.0f} MB. The repository has
a single branch, so cloning it does not download other branches or upstream history.

All files are unmodified copies from upstream, nothing is added or changed except this README header,
`{SCRIPT_NAME}` and `{MARKER}`. The file selection is based on the include list AWS uses to package AWS-LC
for Rust (the `aws-lc-sys` crate), plus all license files.

Configure with tests and tools disabled:

```
-DBUILD_TESTING=OFF -DBUILD_TOOL=OFF -DDISABLE_GO=ON -DDISABLE_PERL=ON
```

To update to another AWS-LC version run `python {SCRIPT_NAME} --tag <tag>` in this folder.

---

"""


def check_build(dest: Path):
    with tempfile.TemporaryDirectory() as build:
        cmd = ["cmake", "-S", str(dest), "-B", build, "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_TESTING=OFF",
               "-DBUILD_TOOL=OFF", "-DDISABLE_GO=ON", "-DDISABLE_PERL=ON"]
        if shutil.which("ninja"):
            cmd += ["-G", "Ninja"]
        run(cmd)
        run(["cmake", "--build", build, "--target", "ssl", "crypto", "--parallel"])
    print("check: libssl and libcrypto built successfully")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="AWS-LC release tag, e.g. v5.12.0")
    parser.add_argument("--dest", default=".", help="destination folder (default: current folder)")
    parser.add_argument("--check", action="store_true", help="build libssl and libcrypto from the result")
    args = parser.parse_args()

    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        sys.exit(f"error: unexpected tag '{args.tag}', expected vX.Y.Z")
    dest = Path(args.dest).resolve()
    first_run = check_destination(dest)

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "aws-lc"
        try:
            run(["git", "clone", "--quiet", "--depth", "1", "--single-branch", "--branch", args.tag,
                 UPSTREAM_URL, str(src)])
            commit = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], check=True,
                                    capture_output=True, text=True).stdout.strip()
            files = list(tree_files(src))
            kept = [(rel, path) for rel, path in files if is_included(rel)]
            if not any(rel == "CMakeLists.txt" for rel, _ in kept):
                sys.exit("error: CMakeLists.txt is not selected, the include list does not fit this AWS-LC version")
            full_mb = size_mb(path for _, path in files)
            slim_mb = size_mb(path for _, path in kept)

            dest.mkdir(parents=True, exist_ok=True)
            clean_destination(dest)
            for rel, path in kept:
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        finally:
            if src.exists():
                rmtree(src)

    readme = dest / "README.md"
    original = readme.read_text(encoding="utf-8") if readme.exists() else ""
    readme.write_text(readme_header(args.tag, commit, full_mb, slim_mb) + original, encoding="utf-8")

    (dest / MARKER).write_text(json.dumps({"upstream": UPSTREAM_URL, "tag": args.tag, "commit": commit},
                                          indent=2) + "\n", encoding="utf-8")
    script = Path(__file__).resolve()
    if script != (dest / SCRIPT_NAME).resolve():
        shutil.copy2(script, dest / SCRIPT_NAME)

    print(f"AWS-LC {args.tag} ({commit}): {len(kept)} of {len(files)} files, "
          f"{slim_mb:.1f} MB instead of {full_mb:.1f} MB -> {dest}")

    if args.check:
        check_build(dest)

    if first_run:
        print(f"\nnext:\n  cd {dest}\n  git init -b main\n  git add -A\n"
              f"  git commit -m \"AWS-LC {args.tag}\"\n  git tag {args.tag}\n"
              "  git remote add origin <url>\n  git push -u origin main --tags")
    else:
        print(f"\nnext:\n  cd {dest}\n  git add -A\n  git commit -m \"AWS-LC {args.tag}\"\n"
              f"  git tag {args.tag}\n  git push origin main --tags")


if __name__ == "__main__":
    main()
