"""Abstract interfaces — the seams between layers (Dependency Inversion).

Every layer depends on these abstractions, never on concrete providers.
Concrete implementations live in ``sta.data`` and are wired at the CLI edge.
"""

from __future__ import annotations

from typing import Optional, Protocol

import pandas as pd


class HistoryProvider(Protocol):
    """Anything that can hand back one symbol's history.

    Satisfied by the per-ticker parquet store (``sta.data.store``) and by
    any other loader of daily bars.
    """

    def history(self, symbol: str) -> Optional[pd.DataFrame]: ...
