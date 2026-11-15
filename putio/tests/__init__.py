"""Tests for the Python components.

Stdlib ``unittest`` only, and they ship with the package.  That is deliberate:
``putioctl deploy`` replaces files in ~/Scripts one at a time, so being able to
run the suite against the *deployed* tree is the only way to confirm that the
copy cron uses is the copy that works.

    python3 -m unittest discover        # from the repo, or from ~/Scripts

pytest is a fine convenience on a development machine, but nothing here may
ever require it — the server has no third-party packages and no venv.
"""
