from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from edgex_sdk import Client as EdgeXClient
from edgex_sdk import OrderSide as SDKOrderSide

from bot.adapters.base import ExchangeAdapter
from bot.models.types import (
    Balance,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Ticker,
    TimeInForce,
)


class EdgeXSDKAdapter(ExchangeAdapter):
    """
    ✅ 目的
    - grid_engine 側が求める「抽象メソッド(fetch_balances)」を必ず実装する
    - OrderRequest の size / quantity の揺れを吸収する（どっちでも動く）
    - ticker は SDKが死んでも Public API で取れるように保険を入れる
    - depth は Public API を使う（contractId前提）
    """

    def __init__(
        self,
        base_url: str,
        account_id: int,
        stark_private_key: str,
        name: str = "edgex_sdk",
    ) -> None:
        super().__init__(name=name)
        self.base_url = str(base_url).rstrip("/")
        self.account_id = int(account_id)
        self.stark_private_key = str(stark_private_key)

        self._client: Optional[EdgeXClient] = None

        # (best_bid, best_ask, ts_ms)
        self._last_depth: Dict[str, Tuple[Optional[float], Optional[float], int]] = {}

    # -----------------------------
    # helpers
    # -----------------------------
    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    # -----------------------------
    # lifecycle
    # -----------------------------
    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = EdgeXClient(
            base_url=self.base_url,
            account_id=self.account_id,
            stark_private_key=self.stark_private_key,
        )
        logger.info("EdgeXSDKAdapter connected")

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception:
            pass
        self._client = None

    # -----------------------------
    # ticker (SDK -> Public API fallback)
    # -----------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        """
        symbol = contractId を想定（例: 10000001 / 10000234）
        """
        assert self._client is not None
        contract_id = str(symbol)

        # 1) SDK try (リトライ付き)
        backoff = 0.5
        last_err: Optional[Exception] = None
        for _ in range(5):
            try:
                resp = await self._client.get_24_hour_quote(contract_id)
                data = (resp or {}).get("data") or []
                if data and isinstance(data[0], dict):
                    px = data[0].get("lastPrice")
                    if px is not None:
                        return Ticker(symbol=contract_id, price=float(px), ts_ms=self._now_ms())
                raise ValueError("ticker price not available via SDK")
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "429" in msg or "too many requests" in msg or "cloudflare" in msg or "just a moment" in msg:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.8, 6.0)
                    continue
                break

        # 2) Public API fallback
        try:
            price = await self._public_get_last_price(contract_id)
            return Ticker(symbol=contract_id, price=float(price), ts_ms=self._now_ms())
        except Exception as e:
            raise RuntimeError(
                f"ticker unavailable contractId={contract_id} sdk_err={last_err} public_err={e}"
            ) from e

    async def _public_get_last_price(self, contract_id: str) -> float:
        url = f"{self.base_url}/api/v1/public/quote/getTicker"
        params = {"contractId": str(contract_id)}
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(timeout=8.0, headers=headers, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            j = r.json()

        data = j.get("data") or []
        if not data:
            raise ValueError(f"empty ticker data: {j}")
        row = data[0] if isinstance(data, list) else data
        if not isinstance(row, dict):
            raise ValueError(f"unexpected ticker data shape: {type(row)}")
        px = row.get("lastPrice")
        if px is None:
            raise ValueError(f"lastPrice missing: {row}")
        return float(px)

    # -----------------------------
    # ✅ REQUIRED by abstract base: fetch_balances
    # -----------------------------
    async def fetch_balances(self) -> List[Balance]:
        """
        ✅ grid_engine が必要とする抽象メソッド。

        EdgeX SDKのレスポンス形が揺れることがあるので、
        取れたものだけ返し、取れなければ空配列で落とさない。
        """
        if self._client is None:
            return []

        client = self._client

        def _mk(asset: str, free: float, locked: float = 0.0) -> Balance:
            # Balanceの型がforkで違う可能性があるので、順番に試す
            try:
                return Balance(asset=asset, free=free, locked=locked)  # type: ignore
            except Exception:
                try:
                    return Balance(asset=asset, available=free, locked=locked)  # type: ignore
                except Exception:
                    return Balance(asset, free, locked)  # type: ignore

        # SDKの関数名が違う可能性に備えて候補を並べる
        candidates = []
        if hasattr(client, "get_balance"):
            candidates.append(("get_balance", client.get_balance))
        if hasattr(client, "get_balances"):
            candidates.append(("get_balances", client.get_balances))

        # ネスト構造があるSDKもいる
        if hasattr(client, "account"):
            acc = getattr(client, "account")
            if hasattr(acc, "get_balance"):
                candidates.append(("account.get_balance", acc.get_balance))
            if hasattr(acc, "get_balances"):
                candidates.append(("account.get_balances", acc.get_balances))

        last_err: Optional[Exception] = None

        for name, fn in candidates:
            try:
                resp = fn()
                if asyncio.iscoroutine(resp):
                    resp = await resp

                data = resp
                if isinstance(resp, dict) and "data" in resp:
                    data = resp.get("data")

                out: List[Balance] = []
                if isinstance(data, list):
                    for row in data:
                        if not isinstance(row, dict):
                            continue
                        asset = str(row.get("asset") or row.get("collateralAsset") or row.get("symbol") or "USDC")
                        free = row.get("available") or row.get("free") or row.get("balance") or row.get("equity") or 0
                        locked = row.get("locked") or row.get("frozen") or row.get("hold") or 0
                        try:
                            out.append(_mk(asset, float(free), float(locked)))
                        except Exception:
                            continue
                    return out

                if isinstance(data, dict):
                    asset = str(data.get("asset") or data.get("collateralAsset") or data.get("symbol") or "USDC")
                    free = data.get("available") or data.get("free") or data.get("balance") or data.get("equity") or 0
                    locked = data.get("locked") or data.get("frozen") or data.get("hold") or 0
                    return [_mk(asset, float(free), float(locked))]

            except Exception as e:
                last_err = e
                logger.debug(f"fetch_balances via {name} failed: {e}")
                continue

        logger.warning(f"fetch_balances: could not fetch via SDK (last_err={last_err}); returning empty list")
        return []

    # -----------------------------
    # order listing (active/open)
    # -----------------------------
    async def list_active_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        EdgeX SDK 側の違いを確認しつつ、アクティブ注文を取得する。
        401 whitelist 等が出ても例外を投げずに空で返して bot を継続させる。
        """
        if self._client is None:
            return []

        client = self._client
        resp: Optional[Dict[str, Any]] = None

        # SDKが order.get_active_orders を持つ場合
        if hasattr(client, "order") and hasattr(client.order, "get_active_orders"):
            try:
                from edgex_sdk.order.types import GetActiveOrderParams  # type: ignore
                params_obj = GetActiveOrderParams()
                params_obj.size = "200"
                params_obj.filter_status_list = ["OPEN"]
                if symbol:
                    params_obj.contract_id_list = [str(symbol)]
                resp = await client.order.get_active_orders(params_obj)  # type: ignore
            except Exception as e:
                logger.debug(f"get_active_orders failed: {e}")
                resp = None

        # fallback: get_active_order_page を持つ場合
        if resp is None and hasattr(client, "get_active_order_page"):
            try:
                resp = await client.get_active_order_page(  # type: ignore
                    contract_id=str(symbol) if symbol else None,
                    page_no=1,
                    page_size=200,
                )
            except Exception as e:
                logger.debug(f"get_active_order_page failed: {e}")
                resp = None

        data = (resp or {}).get("data") or []
        if isinstance(data, dict) and "rows" in data:
            data = data.get("rows") or []

        rows: List[Dict[str, Any]] = []
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    async def get_open_orders(self, symbol: str) -> List[Order]:
        rows = await self.list_active_orders(symbol=symbol)
        res: List[Order] = []
        for row in rows:
            oid = row.get("orderId") or row.get("id")
            if not oid:
                continue
            side = OrderSide.BUY if str(row.get("side")).upper() in ("BUY", "B", "LONG") else OrderSide.SELL
            try:
                price = float(row.get("price") or 0)
                size = float(row.get("size") or row.get("quantity") or 0)
                filled = float(row.get("filled") or row.get("filledSize") or 0)
            except Exception:
                price, size, filled = 0.0, 0.0, 0.0

            res.append(
                Order(
                    order_id=str(oid),
                    client_order_id=row.get("clientOrderId"),
                    symbol=str(symbol),
                    side=side,
                    type=OrderType.LIMIT,
                    status=OrderStatus.OPEN,
                    price=price,
                    size=size,
                    filled=filled,
                    ts_ms=self._now_ms(),
                )
            )
        return res

    # -----------------------------
    # depth (Public API)
    # -----------------------------
    async def get_depth(
        self, symbol: str, limit: int = 20
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        contract_id = str(symbol)
        url = f"{self.base_url}/api/v1/public/quote/getDepth"
        params = {"contractId": contract_id, "limit": str(limit)}
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(timeout=8.0, headers=headers, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            j = r.json()

        data = j.get("data") or {}
        bids = data.get("bids") or []
        asks = data.get("asks") or []

        def conv(arr: Any) -> List[Tuple[float, float]]:
            out: List[Tuple[float, float]] = []
            if not isinstance(arr, list):
                return out
            for row in arr:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    try:
                        out.append((float(row[0]), float(row[1])))
                    except Exception:
                        continue
                elif isinstance(row, dict):
                    try:
                        out.append((float(row.get("price")), float(row.get("size") or row.get("quantity"))))
                    except Exception:
                        continue
            return out

        bb = conv(bids)
        aa = conv(asks)

        best_bid = bb[0][0] if bb else None
        best_ask = aa[0][0] if aa else None
        self._last_depth[contract_id] = (best_bid, best_ask, self._now_ms())

        return bb, aa

    # -----------------------------
    # place/cancel
    # -----------------------------
    async def place_order(self, req: OrderRequest) -> Order:
        """
        ✅ 重要: OrderRequest の size / quantity の揺れを吸収する
        """
        assert self._client is not None

        contract_id = str(req.symbol)
        side = SDKOrderSide.BUY if req.side == OrderSide.BUY else SDKOrderSide.SELL

        # ✅ size / quantity 両対応
        if hasattr(req, "size"):
            size = float(getattr(req, "size"))
        elif hasattr(req, "quantity"):
            size = float(getattr(req, "quantity"))
        else:
            raise ValueError("OrderRequest has neither size nor quantity")

        price = float(req.price)

        tif = getattr(req, "tif", None) or TimeInForce.GTC
        order_type = getattr(req, "type", None) or OrderType.LIMIT

        resp = await self._client.place_order(
            contract_id=contract_id,
            side=side,
            price=price,
            size=size,
            reduce_only=bool(getattr(req, "reduce_only", False)),
            client_order_id=getattr(req, "client_order_id", None),
            time_in_force=str(tif),
            order_type=str(order_type),
        )

        data = (resp or {}).get("data") or {}
        oid = data.get("orderId") or data.get("id") or getattr(req, "client_order_id", None) or "unknown"

        return Order(
            order_id=str(oid),
            client_order_id=getattr(req, "client_order_id", None),
            symbol=contract_id,
            side=req.side,
            type=order_type,
            status=OrderStatus.OPEN,
            price=price,
            size=size,
            filled=0.0,
            ts_ms=self._now_ms(),
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        assert self._client is not None
        await self._client.cancel_order(contract_id=str(symbol), order_id=str(order_id))
