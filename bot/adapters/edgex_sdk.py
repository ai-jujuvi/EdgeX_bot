import os
import time
import logging
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)


def _env(name, default=None):
    v = os.getenv(name)
    return v.strip() if v else default


class EdgeXAdapter:
    def __init__(
        self,
        base_url: str,
        account_id: str,
        stark_private_key: str,
        contract_id: str,
        symbol: str,
        **kwargs,
    ):
        self.base_url = base_url
        self.account_id = account_id
        self.stark_private_key = stark_private_key
        self.contract_id = contract_id
        self.symbol = symbol

        self.maker_mode = _env("EDGEX_MAKER_MODE", "validate")
        self.dummy_price = float(_env("EDGEX_DUMMY_TICKER_PRICE", 2000.0))

        log.info("Adapter ready. mode=%s", self.maker_mode)

    # -----------------------------
    # grid_engine compatibility
    # -----------------------------

    async def connect(self):
        log.info("connect OK")

    async def close(self):
        log.info("close OK")

    async def get_ticker(self, *args, **kwargs):
        p = float(self.dummy_price)
        return SimpleNamespace(price=p, bid=p, ask=p, last=p)

    async def list_active_orders(self, *args, **kwargs):
        return []

    async def cancel_order(self, *args, **kwargs):
        return {"ok": True}

    # -----------------------------
    # order extraction (robust)
    # -----------------------------

    def _normalize_side(self, side: Any) -> str:
        if hasattr(side, "name"):
            return side.name.upper()
        s = str(side)
        if "." in s:
            s = s.split(".")[-1]
        return s.upper()

    def _extract(self, *args, **kwargs):

        # explicit style
        if len(args) >= 3:
            return args[0], args[1], args[2]

        # object style
        if len(args) == 1:
            o = args[0]

            # direct attrs
            if hasattr(o, "side") and hasattr(o, "price") and hasattr(o, "size"):
                return o.side, o.price, o.size

            # dict
            if isinstance(o, dict):
                return o.get("side"), o.get("price"), o.get("size")

            # nested common patterns
            for s in ["side", "order_side"]:
                for p in ["price", "limit_price"]:
                    for z in ["size", "qty", "amount"]:
                        if hasattr(o, s) and hasattr(o, p) and hasattr(o, z):
                            return getattr(o, s), getattr(o, p), getattr(o, z)

        # kwargs style
        if "side" in kwargs and "price" in kwargs and "size" in kwargs:
            return kwargs["side"], kwargs["price"], kwargs["size"]

        raise RuntimeError("place_order() could not extract (side, price, size)")

    # -----------------------------
    # place order
    # -----------------------------

    async def place_order(self, *args, **kwargs):

        side, price, size = self._extract(*args, **kwargs)

        side = self._normalize_side(side)
        price = float(price)
        size = float(size)

        if self.maker_mode == "validate":
            log.info(
                "[VALIDATE] side=%s price=%s size=%s",
                side,
                price,
                size,
            )
            return {"ok": True}

        raise RuntimeError("LIVE MODE not implemented")


class EdgeXSDKAdapter(EdgeXAdapter):
    def __init__(self, base_url, account_id, stark_private_key, **kwargs):

        contract_id = _env("EDGEX_CONTRACT_ID")
        symbol = _env("EDGEX_SYMBOL", contract_id)

        if not contract_id:
            raise RuntimeError("EDGEX_CONTRACT_ID missing")

        super().__init__(
            base_url,
            account_id,
            stark_private_key,
            contract_id,
            symbol,
        )
