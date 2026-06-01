"""Desk Brain — LLM meta-allocator for the whole NRGX book.

Reads every engine's current opportunity set + regime + measured edge and
produces a single risk-budgeted target book. The allocation math is
deterministic (``allocator.py``); the LLM is a desk-head synthesis layer
that explains the book and nudges sleeve weights within a hard-clamped tilt.

Public surface:
- ``sleeves``    — sleeve definitions + per-engine edge config.
- ``aggregator`` — normalize engine signals into a common opportunity set.
- ``allocator``  — regime-tilted sleeve budget + edge x conviction sizing.
"""
from __future__ import annotations

from backend.desk_brain import aggregator, allocator, sleeves

__all__ = ["aggregator", "allocator", "sleeves"]
