"""Universe loaders for the research harness."""
from __future__ import annotations

import os
from typing import List

_UNIVERSE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "universe"
)


def load_sp500() -> List[str]:
    """Load the S&P 500 ticker list.

    NOTE: this file is *current* membership and therefore survivorship-biased
    (delisted names are absent). Residual-reversal results on this universe are
    optimistic; swap in a delisting-aware list for a clean read.
    """
    return _load_txt(os.path.join(_UNIVERSE_DIR, "sp500.txt"))


def load_nasdaq100() -> List[str]:
    return _load_txt(os.path.join(_UNIVERSE_DIR, "nasdaq100.txt"))


def _load_txt(path: str) -> List[str]:
    out: List[str] = []
    with open(path) as fh:
        for line in fh:
            t = line.strip().upper()
            if t and not t.startswith("#"):
                out.append(t)
    return out
