async def get_ticker(self, *args, **kwargs):
    """
    EdgeX /api/v1/public/quote/getTicker から価格取得
    lastPrice を price として返す
    """
    import httpx

    url = f"{self.base_url}/api/v1/public/quote/getTicker"
    params = {"contractId": self.contract_id}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)

        data = resp.json()

        if data.get("code") != "SUCCESS":
            raise ValueError(f"Ticker API error: {data}")

        ticker_list = data.get("data", [])
        if not ticker_list:
            raise ValueError("Ticker data empty")

        ticker = ticker_list[0]

        # ⭐ ここが重要
        price = float(ticker["lastPrice"])

        print(f"[TICKER] lastPrice={price}")
        return SimpleNamespace(price=price)

    except Exception as e:
        print(f"[ERROR] get_ticker failed: {e}")
        raise
