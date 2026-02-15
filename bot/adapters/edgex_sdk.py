import os
import asyncio
import logging
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


class EdgeXSDKAdapter:
    """
    Minimal compatibility adapter for grid_engine.

    This version:
    - Accepts flexible place_order signatures
    - Returns dummy ticker price
    - Prevents crash in async context
    """

    def __init__(self, contract_id: str, symbol: str, dry_run: bool = False):
        self.contract_id = contract_id
        self.symbol = symbol
        self.dry_run = dry_run
        self._dummy_price = float(os.getenv("EDGEX_DUMMY_TICKER_PRICE", "2000"))

        logger.info(f"EdgeXSDKAdapter initialized: contract_id={contract_id}")

    async def connect(self):
        logger.info("Adapter connect() called")
        return True

    async def get_ticker(self, *args, **kwargs) -> Dict[str, float]:
        """
        Always return dummy ticker.
        """
        logger.debug(
            f"get_ticker(): returning DUMMY price={self._dummy_price} "
            f"(args={args}, kwargs={kwargs})"
        )
        return {"price": self._dummy_price}

    async def list_active_orders(self, *args, **kwargs) -> List[Dict[str, Any]]:
        """
        Return empty active orders.
        """
        return []

    async def place_order(self, *args, **kwargs) -> Dict[str, Any]:
        """
        Flexible extractor for:
            place_order(side, price, size)
            place_order(side=..., price=..., size=...)
            place_order(dict)
        """

        side = None
        price = None
        size = None

        # Case 1: dict passed as single positional
        if len(args) == 1 and isinstance(args[0], dict):
            data = args[0]
            side = data.get("side")
            price = data.get("price")
            size = data.get("size")

        # Case 2: positional args
        elif len(args) >= 3:
            side, price, size = args[:3]

        # Case 3: keyword args
        else:
            side = kwargs.get("side")
            price = kwargs.get("price")
            size = kwargs.get("size")

        if side is None or price is None or size is None:
            raise ValueError(
                f"place_order() could not extract (side, price, size) "
                f"from args={args}, kwargs={kwargs}"
            )

        logger.info(
            f"[VALIDATE] place_order side={side} price={price} size={size}"
        )

        return {
            "orderId": "validate-only",
            "side": str(side),
            "price": float(price),
            "size": float(size),
        }

    async def cancel_order(self, *args, **kwargs) -> bool:
        logger.info("cancel_order called (validate only)")
        return True
