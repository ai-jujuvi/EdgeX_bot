from types import SimpleNamespace


class EdgeXSDKAdapter:
    """
    grid_engine 側の呼び方が揺れても落ちない「受け皿」アダプタ。

    今回のポイント：
    - place_order() は (side, price, size) で来る場合もあれば、
      args=(OrderRequest(...),) の1個オブジェクトで来る場合もある
      → OrderRequest から side/price/quantity を抽出できるようにする
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

    def _maybe_extract_from_request_obj(self, obj):
        """
        OrderRequest っぽいオブジェクトから side/price/size を抜く
        例: OrderRequest(symbol='10000234', side=..., type=..., quantity=0.01, price=1970.0, ...)
        """
        if obj is None:
            return None, None, None

        # 属性として持っていそうな候補
        side = getattr(obj, "side", None)
        price = getattr(obj, "price", None)

        # size/quantity は呼び方が揺れるので両対応
        size = getattr(obj, "size", None)
        if size is None:
            size = getattr(obj, "quantity", None)
        if size is None:
            size = getattr(obj, "qty", None)
        if size is None:
            size = getattr(obj, "amount", None)

        # side/price/size のどれか取れたら「それっぽい」とみなす
        if side is not None or price is not None or size is not None:
            return side, price, size

        return None, None, None

    def _extract_side_price_size(self, *args, **kwargs):
        """
        grid_engine 側の渡し方に合わせて (side, price, size) を吸い上げる。

        想定パターン：
          A) place_order(side, price, size)
          B) place_order(side=..., price=..., size=...)
          C) place_order(OrderRequest(...))  ← 今回これ！
        """
        # まず kwargs
        side = kwargs.get("side", None)
        price = kwargs.get("price", None)
        size = kwargs.get("size", None)

        # size の別名も吸う
        if size is None:
            size = kwargs.get("quantity", None)
        if size is None:
            size = kwargs.get("qty", None)
        if size is None:
            size = kwargs.get("amount", None)

        # 位置引数で (side, price, size)
        if (side is None or price is None or size is None) and len(args) >= 3:
            side = side if side is not None else args[0]
            price = price if price is not None else args[1]
            size = size if size is not None else args[2]

        # 位置引数が OrderRequest 1個だけのパターン
        if (side is None or price is None or size is None) and len(args) == 1:
            s2, p2, z2 = self._maybe_extract_from_request_obj(args[0])
            side = side if side is not None else s2
            price = price if price is not None else p2
            size = size if size is not None else z2

        return side, price, size

    async def place_order(self, *args, **kwargs):
        side, price, size = self._extract_side_price_size(*args, **kwargs)

        if side is None or price is None or size is None:
            raise ValueError(
                f"place_order() could not extract (side, price, size). args={args}, kwargs={kwargs}"
            )

        # いまは安全にログだけ（実発注はまだしない）
        print(f"[DRY] place_order side={side} price={price} size={size}")
        return {"status": "ok", "side": str(side), "price": float(price), "size": float(size)}

    async def list_active_orders(self, *args, **kwargs):
        return []

    async def cancel_all_orders(self, *args, **kwargs):
        return {"status": "ok"}
