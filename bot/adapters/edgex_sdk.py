from types import SimpleNamespace
import httpx


class EdgeXSDKAdapter:
    """
    EdgeX 本番接続用 SDK アダプタ
    """

    def __init__(self, contract_id=None, symbol=None, dry_run=False, base_url=None, **kwargs):
        self.contract_id = contract_id
        self.symbol = symbol
        self.dry_run = dry_run
        self.base_url = base_url.rstrip("/")
        self.kwargs = kwargs

    async def connect(self, *args, **kwargs):
        return True

    async def close(self, *args, **kwargs):
        return True

    # ---------------------------
    # ✅ TICKER 修正版
    # ---------------------------
    async def get_ticker(self, *args, **kwargs):
        url = f"{self.base_url}/api/v1/public/quote/getTicker"
        params = {"contractId": self.contract_id}

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)

        data = resp.json()

        if data.get("code") != "SUCCESS":
            raise ValueError(f"Ticker API error: {data}")

        ticker_list = data.get("data", [])
        if not ticker_list:
            raise ValueError("Ticker data empty")

        ticker = ticker_list[0]
        price = float(ticker["lastPrice"])

        print(f"[TICKER] lastPrice={price}")
        return SimpleNamespace(price=price)

    # ---------------------------
    # 注文系（まだSIM的挙動）
    # ---------------------------
    async def place_order(self, side=None, price=None, size=None, **kwargs):
        if side is None or price is None or size is None:
            raise ValueError("place_order missing params")

        print(f"[LIVE-REQUEST] side={side} price={price} size={size}")

        # まだ実発注はしない（安全）
        return {
            "status": "ok",
            "side": str(side),
            "price": float(price),
            "size": float(size),
        }

    async def list_active_orders(self, *args, **kwargs):
        return []

    async def cancel_all_orders(self, *args, **kwargs):
        return {"status": "ok"}
