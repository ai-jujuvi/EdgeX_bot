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
            # Some code passes account_id via "symbol"
            self.symbol = str(os.getenv("EDGEX_ACCOUNT_ID", ""))

        self._dummy_price = float(os.getenv("EDGEX_DUMMY_TICKER_PRICE", "2000"))

        logger.info(
            "Adapter init: base_url=%s, contract_id=%s, symbol=%s, dry_run=%s",
            self.base_url,
            self.contract_id,
            self.symbol,
            self.dry_run,
        )

    async def connect(self) -> bool:
        return True

    async def get_ticker(self, *args, **kwargs) -> Dict[str, float]:
        # grid_engine expects dict-like ticker with "price"
        return {"price": self._dummy_price}

    async def list_active_orders(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return []

    async def place_order(self, *args, **kwargs) -> Dict[str, Any]:
        """
        Accepts various call patterns.
        Extract (side, price, size) from dict/args/kwargs.
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
            side, price, size = args[0], args[1], args[2]
        else:
            side = kwargs.get("side")
            price = kwargs.get("price")
            size = kwargs.get("size")

        if side is None or price is None or size is None:
            msg = f"place_order() could not extract (side, price, size) from args={args}, kwargs={kwargs}"
            raise ValueError(msg)

        logger.info("[VALIDATE] place_order side=%s price=%s size=%s", side, price, size)
        return {
            "orderId": "validate-only",
            "side": str(side),
            "price": float(price),
            "size": float(size),
        }

    async def cancel_order(self, *args, **kwargs) -> bool:
        return True
