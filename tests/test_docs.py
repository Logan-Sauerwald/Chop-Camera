#!/usr/bin/env python3
"""Checks on the documentation itself.

Docs go stale quietly. Worse, an image can be referenced, exist on disk, look
right locally, and simply not be in the repository -- which is what happened
when .gitignore's `*.jpg` rule swallowed the wall screenshots and `git add`
said nothing about it. These catch both.
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

def markdown_files():
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "venv", "__pycache__")]
        out += [os.path.join(base, f) for f in files if f.endswith(".md")]
    return sorted(out)


def tracked_files():
    """Repo-relative paths git knows about, or None if this is not a checkout."""
    try:
        res = subprocess.run(["git", "-C", ROOT, "ls-files"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.split() if res.returncode == 0 else None


def links_in(path):
    """(target, is_image) for every relative markdown link in a file."""
    text = open(path).read()
    for image, target in re.findall(r'(!?)\[[^\]]*\]\(([^)]+)\)', text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        yield target.split("#")[0], bool(image)


class TestDocLinks(unittest.TestCase):

    def test_every_relative_link_resolves(self):
        broken = []
        for md in markdown_files():
            for target, _ in links_in(md):
                if not target:
                    continue
                resolved = os.path.normpath(os.path.join(os.path.dirname(md), target))
                if not os.path.exists(resolved):
                    broken.append(f"{os.path.relpath(md, ROOT)} -> {target}")
        self.assertEqual(broken, [], "broken links:\n  " + "\n  ".join(broken))

    def test_every_referenced_image_is_committed(self):
        """An image that exists on disk but is not tracked renders as a broken
        image on GitHub while looking perfect locally."""
        tracked = tracked_files()
        if tracked is None:
            self.skipTest("not a git checkout")
        known = set(tracked)

        missing = []
        for md in markdown_files():
            for target, is_image in links_in(md):
                if not is_image or not target:
                    continue
                resolved = os.path.normpath(os.path.join(os.path.dirname(md), target))
                rel = os.path.relpath(resolved, ROOT)
                if rel not in known:
                    missing.append(f"{os.path.relpath(md, ROOT)} -> {rel}")
        self.assertEqual(missing, [],
                         "images referenced but not committed (check .gitignore):\n  "
                         + "\n  ".join(missing))


class TestNoRemovedSettings(unittest.TestCase):
    """Settings that were deleted should not linger in the docs telling people
    to set something that no longer exists."""

    GONE = ["AGG_OS", 'CAPTURE_ENCODE', 'PLAYBACK_MODE']

    def test_removed_settings_are_not_documented_as_current(self):
        offenders = []
        for md in markdown_files():
            text = open(md).read()
            for name in self.GONE:
                # A bare mention in a historical note is fine; an assignment
                # reads as a live instruction.
                for m in re.findall(r'^[^#\n]*\b%s="[^"]*"' % name, text, re.M):
                    offenders.append(f"{os.path.relpath(md, ROOT)}: {m.strip()[:70]}")
        self.assertEqual(offenders, [],
                         "docs assign settings that no longer exist:\n  "
                         + "\n  ".join(offenders))

    def test_config_examples_do_not_define_them(self):
        for cfg in ["chopcam.conf.example", "aggregator/chopcam-agg.conf.example"]:
            text = open(os.path.join(ROOT, cfg)).read()
            for name in self.GONE:
                self.assertNotRegex(text, r'^%s=' % name, f"{cfg} still sets {name}")


class TestNoRealAddresses(unittest.TestCase):
    """Every IP in the repository must be a documentation address.

    This repository is public, and a real controls address next to a real
    trigger tag says which bit fires which knife on which network. The check is
    a positive allowlist rather than a list of the plant's own addresses --
    naming those here would put them back in the very repository they were
    taken out of.
    """

    # RFC 5737 documentation ranges, loopback, and the any-address.
    ALLOWED = re.compile(
        r"^(?:0\.0\.0\.0|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.0\.2\.(?:\d{1,3}|x)"
        r"|198\.51\.100\.(?:\d{1,3}|x)"
        r"|203\.0\.113\.(?:\d{1,3}|x))$")
    # Trailing "x" catches subnet shorthand like 192.0.2.x in prose.
    FOUND = re.compile(r"\b(?:\d{1,3}\.){3}(?:\d{1,3}|x)\b")

    def test_every_address_is_a_documentation_address(self):
        tracked = tracked_files()
        if tracked is None:
            self.skipTest("not a git checkout")
        offenders = []
        for path in tracked:
            if path.endswith((".jpg", ".png", ".mp4")):
                continue
            full = os.path.join(ROOT, path)
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                for found in self.FOUND.findall(line):
                    if not self.ALLOWED.match(found):
                        offenders.append(f"{path}:{line_no}: {found}")
        self.assertEqual(
            offenders, [],
            "non-documentation IP address(es) in a public repository. Use "
            "192.0.2.x / 198.51.100.x (RFC 5737) in examples:\n  " +
            "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
