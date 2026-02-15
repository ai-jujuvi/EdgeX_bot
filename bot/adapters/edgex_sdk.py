import os
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EdgeXSDKAdapter:
    """
    Compatibility adapter for grid_engine.
    - Accepts any init signature (base_url/contract_id/symbol/dry_run) via kwargs
    - Provides async methods expected by grid_engine
    """

    def __init__(self, *args, **kwargs):
        # Accept both keyword & positional (just in case)
        self.base_url: str = kwargs.get("base_url") or kwargs.get("baseUrl") or ""
        self.contract_id: str = str(kwargs.get("contract_id") or kwargs.get("contractId") or "")
        self.symbol: str = str(kwargs.get("symbol") or "")
        self.dry_run: bool = bool(kwargs.get("dry_run", False))

        # If some were passed positionally, fill them (best-effort)
        # Expected order sometimes: (base_url, contract_id, symbol, dry_run)
        if args:
            if not self.base_url and len(args) >= 1:
                self.base_url = str(args[0])
            if not self.contract_id and len(args) >= 2:
                self.contract_id = str(args[1])
            if not self.symbol and len(args) >= 3:
                self.symbol = str(args[2])
            if len(args) >= 4 and "dry_run" not in kwargs:
                self.dry_run = bool(args[3])

        # Fallbacks from env
        if not self.base_url:
            self.base_url = os.getenv("EDGEX_BASE_URL", "")
        if not self.contract_id:
            self.contract_id = str(os.getenv("EDGEX_CONTRACT_ID", ""))
        if not self.symbol:
            self.symbol = str(os.getenv("EDGEX_ACCOUNT_ID", ""))  # some code uses symbol=account_id

        self._dummy_price = float(os.getenv("EDGEX_DUMMY_TICKER_PRICE", "2000"))

        logger.info(
            f"Adapter init: base_url={self.base_url}, contract_id={self.contract_id}, symbol={self.symbol}, dry_run={self.dry_run}"
        )

    async def connect(self) -> bool:
        return True

    async def get_ticker(self, *args, **kwargs) -> Dict[str, float]:
        # grid_engine expects dict-like ticker with 'price'
        return {"price": self._dummy_price}

    async def list_active_orders(self, *args, **kwargs) -> List[Dict[str, Any]]:
        # dummy: no active orders
        return []

    async def place_order(self, *args, **kwargs) -> Dict[str, Any]:
        """
        Must accept various call patterns from grid_engine.
        We extract (side, price, size) from dict/args/kwargs.
        """
        side: Optional[Any] = None
        price: Optional[Any] = None
        size: Optional[Any] = None

        if len(args) == 1 and isinstance(args[0], dict):
            data = args[0]
            side = data.get("side")
            price = data.get("price")
            size = data.get("size")
        elif len(args) >= 3:
            side, price, size = args[:3]
        else:
            side = kwargs.get("side")
            price = kwargs.get("price")
            size = kwargs.get("size")

        if side is None or price is None or size is None:
            raise ValueError(
                f"place_order() could not extract (side, price, size) f_
