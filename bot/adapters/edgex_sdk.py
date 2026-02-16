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
from bot.models.types import Balance, Order, OrderRequest, OrderSide, OrderStatus, OrderType, Ticker, TimeInForce


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
        self.account_id = account_id
        self.stark_private_key = stark_private_key

        self._client: EdgeXClient | None = None

        # ✅ contractId（=銘柄）を環境変数で差し替える運用を想定
        # 例: BTC 10000001 / GOLD 10000234（※IDはあなたの想定）
        self.contract_id = os.getenv("EDGEX_CONTRACT_ID")

        # Optional rounding overrides (string -> Decimal)
        # 例: tick=0.1 step=0.01 みたいに入れると事故が減る
        self._price_tick = self._decimal_env("EDGEX_PRICE_TICK")
        self._size_step = self._decimal_env("EDGEX_SIZE_STEP")

    def _decimal_env(self, key: str) -> Optional[Decimal]:
        v = os.getenv(key)
        if not v:
            return None
        try:
            return Decimal(str(v))
        except Exception:
            logger.warning(f"invalid decimal env {key}={v}")
            return None

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

    async def get_ticker(self, symbol: str) -> Ticker:
        """
        ✅ 初心者向けポイント
        - BOT側から渡される symbol は「contractId」を想定（例: 10000001 / 10000234）
        - まず SDK で取得を試し、ダメなら Public API (/getTicker) に自動フォールバックします
        """
        assert self._client is not None
        contract_id = str(symbol)

        # --- 1) まずSDKで試す（既存の安定リトライ） ---
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
                # 429/Cloudflare/一時エラーはリトライ
                if "429" in msg or "Too Many Requests" in msg or "cloudflare" in msg.lower() or "Just a moment" in msg:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.8, 8.0)
                    continue
                # SDKが「契約IDだと取れない」系で落ちることがあるので、ここもフォールバックへ
                break

        # --- 2) SDKがダメなら Public API で取る（contractId対応） ---
        try:
            price = await self._public_get_last_price(contract_id)
            return Ticker(symbol=contract_id, price=price, ts_ms=self._now_ms())
        except Exception as e:
            raise RuntimeError(f"ticker unavailable contractId={contract_id} sdk_err={last_err} public_err={e}") from e

    async def _public_get_last_price(self, contract_id: str) -> float:
        """
        EdgeX Public API で lastPrice を取得します（SDKが不安定な時の保険）。
        """
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
    # 以下、元の実装（あなたのファイルにあるもの）を保持
    # ※ここから下は “触らない” 方針でOK
    # -----------------------------

    async def get_balances(self) -> List[Balance]:
        assert self._client is not None
        resp = await self._client.get_balance()
        data = (resp or {}).get("data") or {}
        res: List[Balance] = []
        # data shape: {"collateralAsset": "...", "balance": "...", ...} or list
        if isinstance(data, dict):
            # try common fields
            asset = data.get("collateralAsset") or data.get("asset") or "USDC"
            bal = data.get("balance") or data.get("available") or data.get("equity") or "0"
            res.append(Balance(asset=str(asset), free=float(bal), locked=0.0))
            return res
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                asset = row.get("asset") or row.get("collateralAsset") or "USDC"
                free = row.get("available") or row.get("free") or row.get("balance") or "0"
                locked = row.get("locked") or row.get("frozen") or "0"
                try:
                    res.append(Balance(asset=str(asset), free=float(free), locked=float(locked)))
                except Exception:
                    continue
        return res

    def _round_price(self, price: float, side: OrderSide) -> float:
        if self._price_tick is None:
            return float(price)
        p = Decimal(str(price))
        tick = self._price_tick
        # side-aware rounding
        if side == OrderSide.BUY:
            q = (p / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        else:
            q = (p / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        return float(q)

    def _round_size(self, size: float) -> float:
        if self._size_step is None:
            return float(size)
        s = Decimal(str(size))
        step = self._size_step
        q = (s / step).to_integral_value(rounding=ROUND_FLOOR) * step
        return float(q)

    async def place_order(self, req: OrderRequest) -> Order:
        """
        ✅ 重要：この adapter は req.symbol を contractId として扱います。
        """
        assert self._client is not None
        contract_id = str(req.symbol)

        side = SDKOrderSide.BUY if req.side == OrderSide.BUY else SDKOrderSide.SELL

        price = req.price
        size = req.size

        # optional rounding
        try:
            price = self._round_price(float(price), req.side)
        except Exception:
            pass
        try:
            size = self._round_size(float(size))
        except Exception:
            pass

        tif = req.tif or TimeInForce.GTC
        order_type = req.type or OrderType.LIMIT

        try:
            resp = await self._client.place_order(
                contract_id=contract_id,
                side=side,
                price=float(price),
                size=float(size),
                reduce_only=bool(getattr(req, "reduce_only", False)),
                client_order_id=req.client_order_id,
                time_in_force=str(tif),
                order_type=str(order_type),
            )
            data = (resp or {}).get("data") or {}
            oid = data.get("orderId") or data.get("id") or req.client_order_id or "unknown"
            return Order(
                order_id=str(oid),
                client_order_id=req.client_order_id,
                symbol=contract_id,
                side=req.side,
                type=order_type,
                status=OrderStatus.OPEN,
                price=float(price),
                size=float(size),
                filled=0.0,
                ts_ms=self._now_ms(),
            )
        except httpx.HTTPStatusError as e:
            # show more helpful detail
            body = ""
            try:
                body = e.response.text
            except Exception:
                pass
            logger.error(f"place_order HTTP error status={e.response.status_code} body={body}")
            raise
        except Exception as e:
            logger.error(f"place_order error: {e}")
            raise

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        assert self._client is not None
        contract_id = str(symbol)
        await self._client.cancel_order(contract_id=contract_id, order_id=str(order_id))

    async def get_open_orders(self, symbol: str) -> List[Order]:
        assert self._client is not None
        contract_id = str(symbol)
        resp = await self._client.get_open_orders(contract_id)
        data = (resp or {}).get("data") or []
        res: List[Order] = []
        if not isinstance(data, list):
            return res
        for row in data:
            if not isinstance(row, dict):
                continue
            oid = row.get("orderId") or row.get("id")
            if not oid:
                continue
            side = OrderSide.BUY if str(row.get("side")).upper() in ("BUY", "B", "LONG") else OrderSide.SELL
            status = OrderStatus.OPEN
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
                    symbol=contract_id,
                    side=side,
                    type=OrderType.LIMIT,
                    status=status,
                    price=price,
                    size=size,
                    filled=filled,
                    ts_ms=self._now_ms(),
                )
            )
        return res

    async def get_depth(self, symbol: str, limit: int = 20) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """
        Public API /quote/getDepth を叩いて板を取得（Cloudflare回避込み）
        """
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

        return conv(bids), conv(asks)
