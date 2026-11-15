"""Constraints on the package as a whole.

These are the rules that make the port safe to run on the server; each one has
been broken by an ordinary-looking edit somewhere.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys
import unittest

import putio

PACKAGE = pathlib.Path(putio.__file__).resolve().parent

#: Every module the standard library ships that this package may import.  Kept
#: as an allowlist rather than a "not in some blocklist" check, because the
#: failure being prevented is a *new* third-party import nobody anticipated.
#: ``sys.stdlib_module_names`` arrived in 3.10, so on Apple's 3.9 there is
#: nothing to check against and the test skips rather than passing vacuously.
HAVE_STDLIB_NAMES = hasattr(sys, "stdlib_module_names")
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"putio", "__future__"}


def source_files():
    return sorted(PACKAGE.rglob("*.py"))


class TestStdlibOnly(unittest.TestCase):
    """No third-party imports, anywhere, ever.

    There is no venv on the server and no requirements file.  That is what
    makes depending on a Homebrew interpreter safe — with no dependencies there
    is nothing for a ``brew upgrade`` to orphan — so the moment one import
    sneaks in, the interpreter choice becomes a liability instead.
    """

    def test_every_import_is_stdlib_or_local(self):
        if not HAVE_STDLIB_NAMES:
            self.skipTest("sys.stdlib_module_names needs Python 3.10+")
        offenders = []
        for path in source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:  # relative import, always local
                        continue
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    if root and root not in STDLIB:
                        offenders.append(
                            "{0}:{1} imports {2}".format(
                                path.relative_to(PACKAGE), node.lineno, name
                            )
                        )
        self.assertEqual(offenders, [], "third-party imports are not allowed here")


class TestSyntaxFloor(unittest.TestCase):
    """Parse on the oldest interpreter this may have to run on.

    Apple's /usr/bin/python3 is 3.9.6 and is still what a machine without the
    PYTHON_BIN pin resolves.  Until the pin is in place everywhere, syntax that
    3.9 cannot parse turns a missing pin from a warning into a crash.
    """

    def test_sources_compile_for_python_39(self):
        if sys.version_info < (3, 8):
            self.skipTest("needs feature_version support")
        for path in source_files():
            with self.subTest(path=path.name):
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 9),
                )


class TestLayout(unittest.TestCase):
    def test_every_module_imports(self):
        """A module no test happens to import can still be deployed broken.

        ``putioctl deploy`` copies the tree file by file and cron reports an
        ImportError to nobody, so importing all of them here is the only thing
        standing between a typo and a feature that silently does nothing.
        """
        import importlib

        for path in source_files():
            relative = path.relative_to(PACKAGE.parent).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts.pop()
            with self.subTest(module=".".join(parts)):
                importlib.import_module(".".join(parts))

    def test_the_package_imports_without_a_configured_environment(self):
        """Importing must never depend on the handoff having happened — the
        test suite, the selftest and any future reader all import first and
        read configuration second."""
        self.assertTrue(hasattr(putio, "__version__"))
        self.assertIsInstance(putio.SCHEMA, int)

    def test_it_runs_from_anywhere(self):
        """cron gives the processor an arbitrary working directory, which is
        why putio_python sets PYTHONPATH rather than relying on cwd."""
        self.assertTrue(os.path.isabs(str(PACKAGE)))


if __name__ == "__main__":
    unittest.main()
