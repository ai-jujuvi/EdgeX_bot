from types import SimpleNamespace


class EdgeXSDKAdapter:
    # run_edgex_grid.py 側が base_url= を渡してくるので受け取る
    def __init__(self, contract_id=None, symbol=None, dry_run=False, base_url=None, **kwargs):
        self.contract_id = contract_id
        self.symbol = symbol
        self.dry_run = dry_run
        self.base_url = base_url
        self.kwargs = kwargs  # 予期しない引数が来ても落ちないように保持だけする

    async def connect(self):
        return True

    async def get_ticker(self, *args, **kwargs):
        # grid_engine は ticker.price を期待しているので price 属性を持つ形で返す
        price = 2000.0
        print(f"get_ticker(): returning DUMMY price={price}")
        return SimpleNamespace(price=price)

    async def place_order(self, side=None, price=None, size=None, **kwargs):
        # grid_engine が渡してくる想定：side, price, size
        if side is None or price is None or size is None:
            raise ValueError("place_order() could not extract (side, price, size)")

        print(f"[DRY] place_order side={side} price={price} size={size}")
        return {"status": "ok"}

    async def list_active_orders(self):
        return []

    async def close(self):
        return True
