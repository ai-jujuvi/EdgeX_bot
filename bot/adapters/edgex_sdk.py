import os
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class EdgeXSDKAdapter:
    """
    Compatibility adapter for grid_engine.
    """

    def __init__(
        self,
        base_url: str,
        contract_id: str,
        symbol: str,
        dry_run: bool = False,
        **kwargs
    ):
        self.base_url = base_url
        self.contract_id = contract_id
        self.symbol = symbol
        self.dry_run = dry_run

        self._dummy_price = float(
            os.getenv("EDGEX_DUMMY_TICKER_PRICE", "2000")
        )

        logger.info(
            f"Adapter init: base_url={base_url}, contract_id={contract_id}"
        )

    async def connect(self):
        return True

    async def get_ticker(self, *args, **kwargs) -> Dict[str, float]:
        return {"price": self._dummy_price}

    async def list_active_orders(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return []

    async def place_order(self, *args, **kwargs) -> Dict[str, Any]:

        side = None
        price = None
        size = None

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
        return True
