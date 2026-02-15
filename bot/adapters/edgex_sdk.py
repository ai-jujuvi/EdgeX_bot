import os
import time
import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

import httpx


class EdgeXSDKAdapter:
    """
    grid_engine 側の呼び方が揺れても落ちない「受け皿」アダプタ。

    重要：
    - get_ticker() は「ticker.price が必ず取れる形」で返すこと
      EdgeX の ticker API は lastPrice / markPrice 等で返るので
      price というキーが無い → ここで price を合成して返す
    - place_order() は (side, price, size) でも OrderRequest 1個でも受ける
    """

    def __init__(self, contract_id=None, symbol=None, dry_run=False, base_url=None, **kwargs):
        self.contract_id = contract_id
        self.symbol = symbol
        self.dry_run = dry_run
        self.base_url = base_url or os.getenv("EDGEX_BASE_URL", "https://pro.edgex.exchange")
        self.kwargs = kwargs

        # 叩きすぎ対策（Render env で上書きOK）
        self.poll_interval_sec = float(os.getenv("EDGEX_POLL_INTERVAL_SEC", "5"))
        self.op_spacing_sec = float(os.getenv("EDGEX_OP_SPACING_SEC", "0.8"))

        self._last_op_ts = 0.0
        self._client: Optional[httpx.AsyncClient] = None

    async def connect(self, *args, **kwargs):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return True

    async def close(self, *args, **kwargs):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        return True

    async def _op_sleep(self):
        """API叩きすぎ防止：操作間隔を強制"""
        now = time.time()
        dt = now - self._last_op_ts
        if dt < self.op_spacing_sec:
            await asyncio.sleep(self.op_spacing_sec - dt)
        self._last_op_ts = time.time()

    def _extract_price_from_quote(self, payload: Dict[str, Any]) -> Optional[float]:
        """
        EdgeX の quote/getTicker っぽいレスポンスから price を安全に抜く
        優先順位：
          1) data[0].lastPrice
          2) data[0].markPrice
          3) data[0].indexPrice
          4) data[0].oraclePrice
          5) data[0].close
        """
        if not isinstance(payload, dict):
            return None

        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None

        row = data[0]
        if not isinstance(row, dict):
            return None

        for k in ("lastPrice", "markPrice", "indexPrice", "oraclePrice", "close"):
            v = row.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except Exception:
                continue

        return None

    async def get_ticker(self, *args, **kwargs):
        """
        grid_engine は ticker.price を参照する前提。
        ここで必ず price 属性を持ったオブジェクトを返す。
        """
        await self.connect()
        await self._op_sleep()

        if not self.contract_id:
            raise ValueError("EdgeXSDKAdapter: contract_id is required (EDGEX_CONTRACT_ID)")

        url = f"{self.base_url}/api/v1/public/quote/getTicker"
        params = {"contractId": str(self.contract_id)}

        try:
            resp = await self._client.get(url, params=params)
            payload = resp.json()
        except Exception as e:
            # ここで落とすと bot 全体が死ぬので、WARN的に扱えるよう price=None で返す
            print(f"[WARN] get_ticker failed: {e}")
            return SimpleNamespace(price=None, raw=None)

        price = self._extract_price_from_quote(payload)

        # grid_engine の警告回避：priceが取れないときは None を返す（run側でリトライされる想定）
        if price is None:
            print(f"[WARN] ticker price not found in response: {payload}")
            return SimpleNamespace(price=None, raw=payload)

        return SimpleNamespace(price=price, raw=payload)

    def _maybe_extract_from_request_obj(self, obj) -> Tuple[Any, Any, Any]:
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

        if side is not None or price is not None or size is not None:
            return side, price, size

        return None, None, None

    def _extract_side_price_size(self, *args, **kwargs):
        side = kwargs.get("side", None)
        price = kwargs.get("price", None)
        size = kwargs.get("size", None)

        if size is None:
            size = kwargs.get("quantity", None)
        if size is None:
            size = kwargs.get("qty", None)
        if size is None:
            size = kwargs.get("amount", None)

        if (side is None or price is None or size is None) and len(args) >= 3:
            side = side if side is not None else args[0]
            price = price if price is not None else args[1]
            size = size if size is not None else args[2]

        if (side is None or price is None or size is None) and len(args) == 1:
            s2, p2, z2 = self._maybe_extract_from_request_obj(args[0])
            side = side if side is not None else s2
            price = price if price is not None else p2
            size = size if size is not None else z2

        return side, price, size

    async def place_order(self, *args, **kwargs):
        """
        いまは安全にログだけ（dry_run）。
        次のステップで「実注文」をONにする。
        """
        side, price, size = self._extract_side_price_size(*args, **kwargs)
        if side is None or price is None or size is None:
            raise ValueError(
                f"place_order() could not extract (side, price, size). args={args}, kwargs={kwargs}"
            )

        size = float(size)
        price = float(price)

        # 叩きすぎ防止
        await self._op_sleep()

        if self.dry_run or str(os.getenv("EDGEX_DRY_RUN", "1")).lower() in ("1", "true", "yes"):
            print(f"[SIM] place_order side={side} price={price} size={size}")
            return {"status": "ok", "mode": "SIM", "side": str(side), "price": price, "size": size}

        # --- 実注文はここに実装していく（次のステップ） ---
        raise RuntimeError("REAL trading is not enabled yet. Set EDGEX_DRY_RUN=0 and implement createOrder endpoint.")

    async def list_active_orders(self, *args, **kwargs):
        # 次のステップで実装（今は grid_engine が落ちないための空返し）
        return []

    async def cancel_all_orders(self, *args, **kwargs):
        # 次のステップで実装（今は grid_engine が落ちないためのダミー）
        return {"status": "ok"}
