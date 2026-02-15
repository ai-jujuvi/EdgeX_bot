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
