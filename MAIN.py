"""Compatibility entry point for the refactored bot."""

import runpy


if __name__ == "__main__":
    runpy.run_module("app.main", run_name="__main__")
else:
    from app.main import *  # noqa: F401,F403
