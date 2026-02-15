from types import SimpleNamespace


class EdgeXSDKAdapter:
    """
    grid_engine 側の呼び方が揺れても落ちない「受け皿」アダプタ。
    - __init__ は base_url= が来ても受ける
    - get_ticker は ticker.price を持つ形で返す
    - place_order / list_active_orders は *args, **kwargs を受けて吸収する
    """

    def __init__(self, contract_id=None, symbol=None, dry_run=False, base_url=None, **kwargs):
        self.contract_id = contract_id
        self.symbol = symbol
        self.dry_run = dry_run
        self.base_url = base_url
        self.kwargs = kwargs

    async def connect(self, *args, **kwargs):
        return True

    async def close(self, *args, **kwargs):
        return True

    async def get_ticker(self, *args, **kwargs):
        # grid_engine は ticker.price を参照する前提っぽいので price 属性を必ず持たせる
        price = 2000.0
        print(f"get_ticker(): returning DUMMY price={price} (args={args}, kwargs={kwargs})")
        return SimpleNamespace(price=price)

    def _extract_side_price_size(self, *args, **kwargs):
        """
        grid_engine 側の渡し方に合わせて (side, price, size) を吸い上げる。
        想定パターン：
          - place_order(side, price, size)
          - place_order(side=..., price=..., size=...)
          - place_order(side=..., price=..., amount=...) など
        """
        side = kwargs.get("side", None)
        price = kwargs.get("price", None)
        size = kwargs.get("size", None)

        # 位置引数の可能性
        if side is None and len(args) >= 1:
            side = args[0]
        if price is None and len(args) >= 2:
            price = args[1]
        if size is None and len(args) >= 3:
            size = args[2]

        # 別名の可能性も吸う（よくあるやつ）
        if size is None:
            size = kwargs.get("qty", None)
        if size is None:
            size = kwargs.get("amount", None)

        return side, price, size

    async def place_order(self, *args, **kwargs):
        side, price, size = self._extract_side_price_size(*args, **kwargs)

        if side is None or price is None or size is None:
            raise ValueError(
                f"place_order() could not extract (side, price, size). args={args}, kwargs={kwargs}"
            )

        # ここでは安全にログだけ（本番発注はまだしない）
        print(f"[DRY] place_order side={side} price={price} size={size}")
        return {"status": "ok", "side": str(side), "price": float(price), "size": float(size)}

    async def list_active_orders(self, *args, **kwargs):
        # grid_engine が余計な引数を渡しても落ちないように吸収
        return []

    async def cancel_all_orders(self, *args, **kwargs):
        return {"status": "ok"}
