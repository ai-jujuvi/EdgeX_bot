from types import SimpleNamespace
import uuid


class EdgeXSDKAdapter:

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
        price = 2000.0
        print(f"get_ticker(): returning DUMMY price={price}")
        return SimpleNamespace(price=price)

    def _maybe_extract_from_request_obj(self, obj):
        if obj is None:
            return None, None, None

        side = getattr(obj, "side", None)
        price = getattr(obj, "price", None)

        size = getattr(obj, "size", None)
        if size is None:
            size = getattr(obj, "quantity", None)
        if size is None:
            size = getattr(obj, "qty", None)
        if size is None:
            size = getattr(obj, "amount", None)

        return side, price, size

    def _extract_side_price_size(self, *args, **kwargs):
        side = kwargs.get("side")
        price = kwargs.get("price")
        size = kwargs.get("size")

        if size is None:
            size = kwargs.get("quantity")
        if size is None:
            size = kwargs.get("qty")
        if size is None:
            size = kwargs.get("amount")

        if (side is None or price is None or size is None) and len(args) >= 3:
            side = side or args[0]
            price = price or args[1]
            size = size or args[2]

        if (side is None or price is None or size is None) and len(args) == 1:
            s2, p2, z2 = self._maybe_extract_from_request_obj(args[0])
            side = side or s2
            price = price or p2
            size = size or z2

        return side, price, size

    async def place_order(self, *args, **kwargs):
        side, price, size = self._extract_side_price_size(*args, **kwargs)

        if side is None or price is None or size is None:
            raise ValueError(
                f"place_order() could not extract (side, price, size). args={args}, kwargs={kwargs}"
            )

        print(f"[SIM] place_order side={side} price={price} size={size}")

        # 👇 ここが超重要：id属性を持つオブジェクトを返す
        return SimpleNamespace(
            id=str(uuid.uuid4()),
            side=str(side),
            price=float(price),
            size=float(size),
            status="open"
        )

    async def list_active_orders(self, *args, **kwargs):
        return []

    async def cancel_all_orders(self, *args, **kwargs):
        return {"status": "ok"}
