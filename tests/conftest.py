"""Shared pytest config.

Puts the repo root on sys.path so ``import main`` and the ``edgar``
package resolve when pytest is invoked from any working directory. These
tests are deliberately offline: nothing here performs a live SEC call.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
