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
        try:
            tracked = subprocess.run(["git", "-C", ROOT, "ls-files"],
                                     capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git not available")
        if tracked.returncode != 0:
            self.skipTest("not a git checkout")
        known = set(tracked.stdout.split())

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


if __name__ == "__main__":
    unittest.main()
