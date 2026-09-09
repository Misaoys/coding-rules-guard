#!/usr/bin/env python3
"""Compatibility entry point for the split Coding Rules Guard CLI."""

import sys
from pathlib import Path


_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from guardlib import guard as _implementation
from guardlib.guard import *  # noqa: F401,F403
from guardlib.guard import main


MODEL_CONFIG_PATH = _implementation.MODEL_CONFIG_PATH


def _sync_compatibility_overrides() -> None:
    _implementation.MODEL_CONFIG_PATH = MODEL_CONFIG_PATH


def load_model_config():
    _sync_compatibility_overrides()
    return _implementation.load_model_config()


def check_transition(*args, **kwargs):
    _sync_compatibility_overrides()
    return _implementation.check_transition(*args, **kwargs)


if __name__ == "__main__":
    main()
