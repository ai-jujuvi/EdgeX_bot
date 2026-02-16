from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# EdgeX SDK
from edgex_sdk import Client as EdgeXClient
from edgex_sdk import OrderSide as SDKOrderSide

import httpx

from bot.adapters.base import ExchangeAdapter
from bot.models.types import OrderRequest, OrderSide, Ticker


class EdgeXSDKAdapter(ExchangeAdapter):
    """
    EdgeX SDK Adapter (GridEngine 用)

    ポイント:
    - GridEngine は adapter.place_order(OrderRequest) を呼ぶ
    - SDK は client.create_limit_order(...) で注文する（place_order ではない）
    - OrderRequest の数量は quantity / size / qty など揺れるので吸収する
    - list_active_orders は dict の配列で返す（GridEngine が dict 前提で読む）
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        account_id: Optional[int] = None,
        stark_private_key: Optional[str] = None,
        contract_id: Optional[str] = None,
        symbol: Optional[str] = None,
        dry_run: bool = False,
        name: str = "edgex_sdk",
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name)
        self.base_url = (base_url or os.getenv("EDGEX_BASE_URL") or "").strip()
        self.account_id = int(account_id or os.getenv("EDGEX_ACCOUNT_ID") or "0")
        self.stark_private_key = (stark_private_key or os.getenv("EDGEX_STARK_PRIVATE_KEY") or "").strip()

        # symbol/contract_id は「contractId」を想定（例: BTC=10000001 / GOLD=10000234）
        self.symbol = str(symbol or contract_id or os.getenv("EDGEX_CONTRACT_ID") or "").strip()

        self.dry_run = bool(dry_run or str(os.getenv("EDGEX_DRY_RUN", "0")).lower() in ("1", "true", "yes"))

        self._client: Optional[EdgeXClient] = None

        # depth short cache: (bid, ask, ts_ms)
        self._last_depth: Dict[str, Tuple[Optional[float], Optional[float], int]] = {}

        # rules cache
        self._market_rules: Dict[str, Dict[str, float]] = {}

        if not self.base_url:
            raise ValueError("EDGEX_BASE_URL is empty")
        if not self.symbol:
            raise ValueError("EDGEX_CONTRACT_ID is empty")
        if not self.dry_run:
            if self.account_id <= 0:
                raise ValueError("EDGEX_ACCOUNT_ID is empty/invalid")
            if not self.stark_private_key:
                raise ValueError("EDGEX_STARK_PRIVATE_KEY is empty")

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = EdgeXClient(
            base_url=self.base_url,
            account_id=self.account_id,
            stark_private_key=self.stark_private_key,
        )
        logger.info(
            "edgex adapter connected: base_url={} contract_id={} dry_run={}",
            self.base_url,
            self.symbol,
            self.dry_run,
        )

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    # ----------------------------
    # Basic market data
    # ----------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        """
        GridEngine は ticker.price を使う
        SDK の 24h quote を使う（data[0].lastPrice）
        """
        if self.dry_run:
            # DRY_RUNでも動くように適当な値を返す（ログで気づけるようにする）
            price = float(os.getenv("EDGEX_DRY_TICKER_PRICE", "2000"))
            logger.warning("DRY_RUN ticker: {} -> {}", symbol, price)
            return Ticker(symbol=str(symbol), price=price, ts_ms=self._now_ms())

        assert self._client is not None
        cid = str(symbol)

        backoff = 0.5
        last_err: Exception | None = None
        for _ in range(8):
            try:
                resp = await self._client.get_24_hour_quote(cid)
                data = (resp or {}).get("data") or []
                if not data:
                    raise RuntimeError("quote data empty")
                last = data[0].get("lastPrice")
                if last is None:
                    raise RuntimeError("lastPrice missing")
                return Ticker(symbol=cid, price=float(last), ts_ms=self._now_ms())
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "429" in msg or "too many requests" in msg or "cloudflare" in msg:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.8, 8.0)
                    continue
                raise
        raise RuntimeError(f"ticker retry exhausted: {last_err}")

    async def get_best_bid_ask(self, symbol: str) -> tuple[float | None, float | None]:
        """
        Depthは SDK で取れない/不安定なことがあるので HTTP 公開APIも併用。
        取れたら短期キャッシュ。
        """
        cid = str(symbol)

        def _extract(container: Any) -> tuple[float | None, float | None]:
            def _px(arr: Any) -> float | None:
                try:
                    if not arr:
                        return None
                    x = arr[0]
                    if isinstance(x, (list, tuple)):
                        return float(x[0])
                    if isinstance(x, dict):
                        return float(x.get("price") or x.get("px") or x.get("0"))
                    return float(x)
                except Exception:
                    return None

            d = None
            if isinstance(container, dict):
                d = container
            elif isinstance(container, list) and container and isinstance(container[0], dict):
                d = container[0]
            if not isinstance(d, dict):
                return None, None

            bids = d.get("bids") or d.get("buy") or []
            asks = d.get("asks") or d.get("sell") or []
            return _px(bids), _px(asks)

        async def _from_http() -> tuple[float | None, float | None]:
            base = self.base_url.rstrip("/")
            url = f"{base}/api/v1/public/quote/getDepth"
            params = {"contractId": cid, "level": "15"}
            headers = {
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.9",
            }
            try:
                async with httpx.AsyncClient(timeout=8.0, headers=headers, follow_redirects=True) as client:
                    r = await client.get(url, params=params)
                    r.raise_for_status()
                    body = r.json()
                    return _extract(body.get("data") if isinstance(body, dict) else None)
            except Exception:
                return None, None

        # cache first (<=3s)
        cached = self._last_depth.get(cid)
        if cached:
            bid, ask, ts = cached
            if self._now_ms() - ts <= 3000 and (bid is not None or ask is not None):
                return bid, ask

        bid, ask = await _from_http()

        # sanity
        try:
            if bid is not None and ask is not None and bid >= ask:
                bid, ask = None, None
        except Exception:
            pass

        self._last_depth[cid] = (bid, ask, self._now_ms())
        return bid, ask

    # ----------------------------
    # Trading
    # ----------------------------
    def _extract_qty(self, req: Any) -> float:
        # OrderRequest の揺れを吸収
        for key in ("quantity", "size", "qty", "amount"):
            v = getattr(req, key, None)
            if v is not None:
                return float(v)
        # dict の場合
        if isinstance(req, dict):
            for key in ("quantity", "size", "qty", "amount"):
                if key in req and req[key] is not None:
                    return float(req[key])
        raise AttributeError("OrderRequest has no quantity/size/qty/amount")

    async def _get_market_rules(self, contract_id: str) -> Dict[str, float]:
        """
        公開メタデータから tick/step/min を拾う（無くても動く）。
        """
        if contract_id in self._market_rules:
            return self._market_rules[contract_id]

        rules: Dict[str, float] = {}
        base = self.base_url.rstrip("/")
        url = f"{base}/api/v1/public/meta/getMetaData"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                body = r.json()
                data = body.get("data") if isinstance(body, dict) else None
                clist = data.get("contractList") if isinstance(data, dict) else None
                if isinstance(clist, list):
                    target = None
                    for c in clist:
                        if isinstance(c, dict) and str(c.get("contractId")) == str(contract_id):
                            target = c
                            break
                    if isinstance(target, dict):
                        # 代表的なキーを雑に吸収（存在しなければ無視）
                        for k in ("priceTick", "price_tick", "tickSize", "tick_size"):
                            if target.get(k) is not None:
                                rules["price_tick"] = float(target.get(k))
                                break
                        for k in ("sizeStep", "size_step", "qtyStep", "qty_step"):
                            if target.get(k) is not None:
                                rules["size_step"] = float(target.get(k))
                                break
                        for k in ("minSize", "min_size", "minQty", "min_qty"):
                            if target.get(k) is not None:
                                rules["min_size"] = float(target.get(k))
                                break
        except Exception:
            pass

        self._market_rules[contract_id] = rules
        return rules

    async def place_order(self, order: OrderRequest) -> Any:
        """
        GridEngine から呼ばれるメイン。
        戻り値は order.id が読めればOK（SimpleNamespaceで返す）
        """
        cid = str(getattr(order, "symbol", None) or self.symbol)
        side = getattr(order, "side", None)
        if side is None:
            raise ValueError("OrderRequest.side is None")

        qty = self._extract_qty(order)
        price = float(getattr(order, "price", 0.0) or 0.0)

        # 成行っぽいのは禁止（必要ならティッカーで指値化）
        if price <= 0:
            t = await self.get_ticker(cid)
            price = t.price * (1.001 if side == OrderSide.BUY else 0.999)

        # 丸め（ENV優先 > メタ）
        rules = await self._get_market_rules(cid)

        # tick
        tick_val = None
        if os.getenv("EDGEX_PRICE_TICK"):
            try:
                tick_val = float(os.getenv("EDGEX_PRICE_TICK", "0"))
            except Exception:
                tick_val = None
        if not tick_val:
            tick_val = float(rules.get("price_tick", 0.1) or 0.1)

        # step
        step_val = None
        if os.getenv("EDGEX_SIZE_STEP"):
            try:
                step_val = float(os.getenv("EDGEX_SIZE_STEP", "0"))
            except Exception:
                step_val = None
        if not step_val:
            step_val = float(rules.get("size_step", 0.0001) or 0.0001)

        # qty floor to step
        try:
            step = Decimal(str(step_val))
            qd = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_FLOOR) * step
            if qd <= 0:
                qd = step
            qty = float(qd)
        except Exception:
            pass

        # min size
        try:
            min_size = rules.get("min_size")
            if min_size and qty < float(min_size):
                qty = float(min_size)
        except Exception:
            pass

        # price snap
        try:
            tick = Decimal(str(tick_val))
            pd = Decimal(str(price)) / tick
            rounded = pd.to_integral_value(rounding=ROUND_FLOOR if side == OrderSide.BUY else ROUND_CEILING)
            price = float(rounded * tick)
        except Exception:
            pass

        if self.dry_run:
            oid = f"dry_{int(time.time()*1000)}"
            logger.warning("[DRY_RUN] place_order cid={} side={} qty={} price={}", cid, side, qty, price)
            return SimpleNamespace(id=oid, status="OPEN", side=str(side), price=price, size=qty)

        assert self._client is not None

        sdk_side = SDKOrderSide.BUY if side == OrderSide.BUY else SDKOrderSide.SELL

        # post-only っぽい指定（SDKが受け取れるキーだけ渡す）
        extra: Dict[str, Any] = {}
        post_only = True
        try:
            tif = getattr(order, "time_in_force", None)
            if tif is not None and str(getattr(tif, "value", tif)).upper() != "POST_ONLY":
                post_only = False
        except Exception:
            pass
        # SDK側が受け取るなら渡す（受け取れなくても例外にはしない）
        extra["post_only"] = post_only

        try:
            timeout = float(os.getenv("EDGEX_ORDER_TIMEOUT_SEC", "8.0"))
        except Exception:
            timeout = 8.0

        res = await asyncio.wait_for(
            self._client.create_limit_order(
                contract_id=cid,
                size=str(qty),
                price=str(price),
                side=sdk_side,
                **extra,
            ),
            timeout=timeout,
        )

        # res からID抽出（形が揺れても耐える）
        oid = None
        if isinstance(res, dict):
            d = res.get("data") if isinstance(res.get("data"), dict) else res
            oid = (d.get("orderId") if isinstance(d, dict) else None) or res.get("orderId")
        if not oid:
            oid = f"unknown_{int(time.time()*1000)}"

        logger.info("order placed: cid={} side={} qty={} price={} oid={}", cid, side, qty, price, oid)
        return SimpleNamespace(id=str(oid), status="OPEN", side=str(side), price=price, size=qty)

    async def cancel_order(self, order_id: str) -> Any:
        if self.dry_run:
            logger.warning("[DRY_RUN] cancel_order {}", order_id)
            return {"status": "ok"}

        assert self._client is not None
        try:
            res = await self._client.cancel_order(order_id=str(order_id))
            return res
        except Exception as e:
            logger.debug("cancel_order failed: {} -> {}", order_id, e)
            raise

    async def list_active_orders(self, symbol: str) -> List[dict]:
        """
        GridEngine は dict 配列を想定して読むので dict に寄せる。
        """
        cid = str(symbol)

        if self.dry_run:
            return []

        assert self._client is not None
        res = await self._client.get_active_orders(contract_id=cid)

        data = None
        if isinstance(res, dict):
            data = res.get("data")
        rows = data if isinstance(data, list) else (res if isinstance(res, list) else [])

        out: List[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            oid = r.get("orderId") or r.get("id") or r.get("order_id")
            if not oid:
                continue
            out.append(
                {
                    "orderId": str(oid),
                    "status": str(r.get("status") or "OPEN"),
                    "side": str(r.get("side") or r.get("orderSide") or "").upper(),
                    "price": r.get("price") or r.get("px") or r.get("0"),
                }
            )
        return out

    # --- abstract methods対策（最低限でOK） ---
    async def fetch_balances(self) -> List[dict]:
        """
        これが無いと 'abstract class ... fetch_balances' で起動時に死ぬので必須。
        使わないなら空でOK。
        """
        return []

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[dict]:
        # GridEngine は getattr で呼ぶので未実装でもOKだが、空で用意
        return []
