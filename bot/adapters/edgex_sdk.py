from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from edgex_sdk import Client as EdgeXClient, OrderSide as SDKOrderSide
import httpx  # for error detail extraction and public API calls

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
    def __init__(
        self,
        base_url: str,
        account_id: int,
        stark_private_key: str,
        name: str = "edgex_sdk",
    ) -> None:
        super().__init__(name=name)
        self.base_url = base_url
        self.account_id = int(account_id)
        self.stark_private_key = stark_private_key
        self._client: Optional[EdgeXClient] = None
        self._market_rules: Dict[str, Dict[str, float]] = {}
        # (best_bid, best_ask, ts_ms)
        self._last_depth: Dict[str, Tuple[Optional[float], Optional[float], int]] = {}

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = EdgeXClient(
            base_url=self.base_url,
            account_id=int(self.account_id),
            stark_private_key=self.stark_private_key,
        )
        logger.info("EdgeXSDKAdapter connected")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    # -----------------------------
    # Price/Ticker (SDK -> Public API fallback)
    # -----------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        """
        symbol は contractId を想定（例: 10000001 / 10000234）
        SDKが不安定でも、Public API /getTicker に自動フォールバックして取る。
        """
        assert self._client is not None
        contract_id = str(symbol)

        # 1) SDK try
        backoff = 0.5
        last_err: Exception | None = None
        for _ in range(6):
            try:
                resp = await self._client.get_24_hour_quote(contract_id)
                data = (resp or {}).get("data") or []
                price = None
                if data and isinstance(data[0], dict):
                    try:
                        price = float(data[0].get("lastPrice"))
                    except Exception:
                        price = None
                if price is not None:
                    return Ticker(symbol=contract_id, price=price, ts_ms=self._now_ms())
                raise ValueError("ticker price not available via SDK")
            except Exception as e:
                last_err = e
                msg = str(e)
                if "429" in msg or "Too Many Requests" in msg or "cloudflare" in msg.lower() or "Just a moment" in msg:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.8, 8.0)
                    continue
                break

        # 2) public fallback
        try:
            price = await self._public_get_last_price(contract_id)
            return Ticker(symbol=contract_id, price=price, ts_ms=self._now_ms())
        except Exception as e:
            raise RuntimeError(f"ticker unavailable contractId={contract_id} sdk_err={last_err} public_err={e}") from e

    async def _public_get_last_price(self, contract_id: str) -> float:
        base = self.base_url.rstrip("/")
        url = f"{base}/api/v1/public/quote/getTicker"
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
    # ✅ FIX: implement fetch_balances (abstract method)
    # -----------------------------
    async def fetch_balances(self) -> List[Balance]:
        """Fetch account balances (best-effort).

        This bot mainly needs the collateral balance (often USDC).
        SDKのバージョン差を吸収しつつ、取れなければ空で返して落とさない。
        """
        if self._client is None:
            return []
        client = self._client

        def _make_balance(asset: str, free: float, locked: float = 0.0) -> Balance:
            # Balanceのフィールド名がforkで違うことがあるので順に試す
            try:
                return Balance(asset=asset, free=free, locked=locked)  # type: ignore
            except Exception:
                try:
                    return Balance(asset=asset, available=free, locked=locked)  # type: ignore
                except Exception:
                    try:
                        return Balance(symbol=asset, free=free, locked=locked)  # type: ignore
                    except Exception:
                        return Balance(asset, free, locked)  # type: ignore

        candidates = [
            ("get_balance", lambda: client.get_balance()),    # type: ignore[attr-defined]
            ("get_balances", lambda: client.get_balances()),  # type: ignore[attr-defined]
        ]
        if hasattr(client, "account"):
            acc = getattr(client, "account")
            candidates += [
                ("account.get_balance", lambda: acc.get_balance()),    # type: ignore[attr-defined]
                ("account.get_balances", lambda: acc.get_balances()),  # type: ignore[attr-defined]
            ]
        if hasattr(client, "user"):
            usr = getattr(client, "user")
            candidates += [
                ("user.get_balance", lambda: usr.get_balance()),       # type: ignore[attr-defined]
                ("user.get_balances", lambda: usr.get_balances()),     # type: ignore[attr-defined]
            ]

        last_err: Exception | None = None
        for name, fn in candidates:
            try:
                resp = fn()
                if asyncio.iscoroutine(resp):
                    resp = await resp

                data = resp
                if isinstance(resp, dict) and "data" in resp:
                    data = resp.get("data")

                balances: List[Balance] = []
                if isinstance(data, list):
                    for row in data:
                        if not isinstance(row, dict):
                            continue
                        asset = str(row.get("asset") or row.get("collateralAsset") or row.get("symbol") or "USDC")
                        free = row.get("available") or row.get("free") or row.get("balance") or row.get("equity") or 0
                        locked = row.get("locked") or row.get("frozen") or row.get("hold") or 0
                        try:
                            balances.append(_make_balance(asset, float(free), float(locked)))
                        except Exception:
                            continue
                    return balances

                if isinstance(data, dict):
                    asset = str(data.get("asset") or data.get("collateralAsset") or data.get("symbol") or "USDC")
                    free = data.get("available") or data.get("free") or data.get("balance") or data.get("equity") or 0
                    locked = data.get("locked") or data.get("frozen") or data.get("hold") or 0
                    return [_make_balance(asset, float(free), float(locked))]

            except Exception as e:
                last_err = e
                continue

        logger.warning(f"fetch_balances: could not fetch via SDK (last_err={last_err}); returning empty list")
        return []

    # -----------------------------
    # orders (keep as-is / compatible)
    # -----------------------------
    async def list_active_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._client is None:
            return []
        client = self._client
        rows: List[Dict[str, Any]] = []
        resp: Dict[str, Any] | None = None

        if hasattr(client, "order") and hasattr(client.order, "get_active_orders"):
            try:
                from edgex_sdk.order.types import GetActiveOrderParams  # type: ignore
            except Exception:
                GetActiveOrderParams = None  # type: ignore
            if GetActiveOrderParams is not None:
                params_obj = GetActiveOrderParams()
                params_obj.size = "200"
                params_obj.filter_status_list = ["OPEN"]
                if symbol:
                    params_obj.contract_id_list = [str(symbol)]
                try:
                    resp = await client.order.get_active_orders(params_obj)  # type: ignore
                except Exception as e:
                    logger.debug(f"get_active_orders failed: {e}")
                    resp = None

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

        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    async def get_depth(
        self, symbol: str, limit: int = 20
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        contract_id = str(symbol)
        base = self.base_url.rstrip("/")
        url = f"{base}/api/v1/public/quote/getDepth"
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

        # cache best bid/ask
        bb = conv(bids)
        aa = conv(asks)
        best_bid = bb[0][0] if bb else None
        best_ask = aa[0][0] if aa else None
        self._last_depth[contract_id] = (best_bid, best_ask, self._now_ms())

        return bb, aa

    # ---- minimal wrappers expected by bot ----
    async def place_order(self, req: OrderRequest) -> Order:
        assert self._client is not None
        contract_id = str(req.symbol)

        side = SDKOrderSide.BUY if req.side == OrderSide.BUY else SDKOrderSide.SELL
        price = float(req.price)
        size = float(req.size)

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
