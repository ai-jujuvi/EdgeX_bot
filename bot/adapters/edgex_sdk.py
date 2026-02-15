from types import SimpleNamespace


class EdgeXSDKAdapter:
    def __init__(self, contract_id, symbol=None, dry_run=False):
        self.contract_id = contract_id
        self.symbol = symbol
        self.dry_run = dry_run

    async def connect(self):
        return True

    async def get_ticker(self, *args, **kwargs):
        price = 2000.0  # dummy price
        print(f"get_ticker(): returning DUMMY price={price}")
        return SimpleNamespace(price=price)

    async def place_order(self, side=None, price=None, size=None, **kwargs):
        if side is None or price is None or size is None:
            raise ValueError(
                "place_order() could not extract (side, price, size)"
            )

        print(
            f"[DRY] place_order side={side} price={price} size={size}"
        )
        return {"status": "ok"}

    async def list_active_orders(self):
        return []

    async def close(self):
        return True
