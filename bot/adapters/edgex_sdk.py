import os
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EdgeXSDKAdapter:
    """
    Compatibility adapter for grid_engine.
    Accepts flexible init signature and provides async methods expected by grid_engine.
    """

    def __init__(self, *args, **kwargs):
        # Accept both keyword & positional (best-effort)
        self.base_url: str = kwargs.get("base_url") or kwargs.get("baseUrl") or ""
        self.contract_id: str = str(kwargs.get("contract_id") or kwargs.get("contractId") or "")
        self.symbol: str = str(kwargs.get("symbol") or "")
        self.dry_run: bool = bool(kwargs.get("dry_run", False))

        # Fill from positional if provided (common order: base_url, contract_id, symbol, dry_run)
        if args:
            if not self.base_url and len(args) >= 1:
                self.base_url = str(args[0])
            if not self.contract_id and len(args)_
