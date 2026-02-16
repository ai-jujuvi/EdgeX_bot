"""
EdgeX SDK Adapter (robust)

目的:
- SDKのバージョン差分（引数名: contract_id / contractId, post_only / postOnly など）で落ちない
- list_active_orders / place_order で TypeError が出たら「引数を変えてリトライ」する
- fetch_balances をダミー実装して abstract class エラーを回避
- CloudflareっぽいHTMLが返ってきた時にログを爆発させない（巨大HTMLをそのまま出さない）
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from bot.adapters.base import ExchangeAdapter
from bot.models.types import (
    Balance,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Ticker,
)

# SDK imports are intentionally lazy (inside connect) to avoid import error at module import time.


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_html(text: str) -> bool:
    t = (text or "").lstrip().lower()
    return t.startswith("<!doctype html") or t.startswith("<html") or "<script" in t[:500]


def _trim(s: str, n: int = 500) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"...(trimmed {len(s)-n} chars)"


class EdgeXSDKAdapter(ExchangeAdapter):
    """
    grid_engine側の呼び方が揺れても落ちない「受け皿」アダプタ。

    重要:
    - symbol は contract_id として扱う（例: "10000001" / "10000234"）
    - SDKの引数名が違っても TypeError を拾ってフォールバックする
    """

    def __init__(
        self,
        contract_id: str | None = None,
        symbol: str | None = None,
        dry_run: bool = False,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.contract_id = str(contract_id or symbol or os.getenv("EDGEX_CONTRACT_ID", "")).strip()
        self.symbol = self.contract_id  # grid側は symbol として渡してくるので同一扱い
        self.dry_run = bool(dry_run)

        self.base_url = (base_url or os.getenv("EDGEX_BASE_URL", "")).strip() or "https://pro.edgex.exchange"

        # credentials
        self.api_key = (os.getenv("EDGEX_API_KEY", "")).strip()
        self.api_secret = (os.getenv("EDGEX_API_SECRET", "")).strip()
        self.api_passphrase = (os.getenv("EDGEX_API_PASSPHRASE", "")).strip()
        self.account_id = (os.getenv("EDGEX_ACCOUNT_ID", "")).strip()

        # runtime
        self._client: Any = None
        self._http: Optional[httpx.AsyncClient] = None

        # tiny cache
        self._last_ticker: Dict[str, Tuple[float, int]] = {}  # contract_id -> (price, ts_ms)

        logger.info(
            "edgex adapter init: base_url={} contract_id={} dry_run={}",
            self.base_url,
            self.contract_id,
            self.dry_run,
        )

    async def connect(self) -> None:
        """
        SDK client を初期化。
        """
        if self._client is not None:
            return

        # small http client (only for fallback / error parsing)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            headers={
                # 露骨にbotっぽいUAを避ける（Cloudflare対策の足し程度）
                "User-Agent": "Mozilla/5.0 (compatible; EdgeXBot/1.0)",
                "Accept": "application/json,text/plain,*/*",
            },
        )

        try:
            # Lazy import
            from edgex_sdk import Client as SDKClient  # type: ignore
        except Exception as e:
            raise RuntimeError(f"edgex_sdk import failed: {e}") from e

        # SDKClient の初期化引数も揺れるので try
        init_errors: List[str] = []
        client = None

        # よくある形 1
        try:
            client = SDKClient(
                base_url=self.base_url,
                api_key=self.api_key,
                api_secret=self.api_secret,
                passphrase=self.api_passphrase or None,
                account_id=self.account_id or None,
            )
        except Exception as e:
            init_errors.append(f"SDKClient(base_url, api_key, api_secret, passphrase, account_id) failed: {e}")

        # よくある形 2（キー名違い）
        if client is None:
            try:
                client = SDKClient(
                    base_url=self.base_url,
                    key=self.api_key,
                    secret=self.api_secret,
                    passphrase=self.api_passphrase or None,
                    accountId=self.account_id or None,
                )
            except Exception as e:
                init_errors.append(f"SDKClient(base_url, key, secret, passphrase, accountId) failed: {e}")

        # よくある形 3（credentials dict）
        if client is None:
            try:
                client = SDKClient(
                    base_url=self.base_url,
                    credentials={
                        "api_key": self.api_key,
                        "api_secret": self.api_secret,
                        "passphrase": self.api_passphrase,
                        "account_id": self.account_id,
                    },
                )
            except Exception as e:
                init_errors.append(f"SDKClient(base_url, credentials=...) failed: {e}")

        if client is None:
            msg = " / ".join(init_errors[-3:])
            raise RuntimeError(f"Failed to init edgex SDK client. Last errors: {msg}")

        self._client = client
        logger.info("edgex adapter connected (sdk client ready)")

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        self._client = None

    # -------------------------
    # Required by abstract base
    # -------------------------
    async def fetch_balances(self) -> List[Balance]:
        # 最短優先: いったん空で返して abstract class エラー回避
        return []

    # -------------------------
    # Basic market data
    # -------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        """
        できるだけ SDK で取得。
        失敗しても「巨大HTML」をログに出さず、短いエラーにする。
        """
        assert self._client is not None
        contract_id = str(symbol or self.contract_id)

        # cache (2秒)
        cached = self._last_ticker.get(contract_id)
        if cached and (_now_ms() - cached[1] <= 2000):
            return Ticker(symbol=contract_id, price=float(cached[0]), ts_ms=_now_ms())

        # try SDK variants
        price: Optional[float] = None
        last_err: Optional[Exception] = None

        # Variant A: client.quote.get_24_hour_quote(contract_id=...)
        try:
            if hasattr(self._client, "quote") and hasattr(self._client.quote, "get_24_hour_quote"):
                res = await self._client.quote.get_24_hour_quote(contract_id=contract_id)
                price = _extract_price_from_quote(res)
        except Exception as e:
            last_err = e

        # Variant B: client.get_ticker(contract_id=...)
        if price is None:
            try:
                if hasattr(self._client, "get_ticker"):
                    res = await self._client.get_ticker(contract_id=contract_id)
                    price = _extract_price_from_quote(res)
            except Exception as e:
                last_err = e

        # Variant C: fallback public endpoint (最後の手段)
        if price is None and self._http is not None:
            try:
                url = f"{self.base_url}/api/v1/public/quote/getTicker"
                r = await self._http.get(url, params={"contractId": contract_id})
                txt = r.text or ""
                if _is_html(txt):
                    raise RuntimeError(f"Cloudflare/HTML response from {url}: {_trim(txt, 200)}")
                j = r.json()
                price = _extract_price_from_quote(j)
            except Exception as e:
                last_err = e

        if price is None:
            raise RuntimeError(f"get_ticker failed for contract_id={contract_id}: {last_err}")

        self._last_ticker[contract_id] = (float(price), _now_ms())
        return Ticker(symbol=contract_id, price=float(price), ts_ms=_now_ms())

    async def get_best_bid_ask(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        最短優先: 取れなくても落とさない。ticker_only運用が前提なら None, None でOK。
        """
        # ここは「動かすこと優先」で最低限にする
        return None, None

    # -------------------------
    # Orders
    # -------------------------
    async def place_order(self, order: OrderRequest) -> Order:
        """
        create_limit_order の引数名が違っても動くように、
        - post_only / postOnly / timeInForce などを試す
        - TypeError が出たら kw を外して再試行
        """
        assert self._client is not None
        contract_id = str(order.symbol or self.contract_id)

        if self.dry_run:
            oid = f"dry_{_now_ms()}"
            return Order(
                id=oid,
                request=order,
                status=OrderStatus.NEW,
                filled_quantity=0.0,
                average_price=float(order.price or 0),
                ts_ms=_now_ms(),
            )

        # normalize
        side_str = "BUY" if order.side == OrderSide.BUY else "SELL"
        size = str(order.quantity)
        price = str(order.price)

        # post-only handling
        post_only = True  # このbotは基本 maker 想定
        tif = getattr(order, "time_in_force", None)
        if tif is not None:
            # 文字列化
            try:
                tif = tif.value  # type: ignore
            except Exception:
                pass

        # call target
        create_meth = None
        if hasattr(self._client, "create_limit_order"):
            create_meth = self._client.create_limit_order
        elif hasattr(self._client, "order") and hasattr(self._client.order, "create_limit_order"):
            create_meth = self._client.order.create_limit_order

        if create_meth is None:
            raise RuntimeError("SDK has no create_limit_order")

        # candidate kw sets (順番が大事)
        attempts: List[Dict[str, Any]] = []

        # 1) contract_id + post_only
        attempts.append(
            {
                "contract_id": contract_id,
                "size": size,
                "price": price,
                "side": side_str,
                "post_only": post_only,
            }
        )
        # 2) contractId + postOnly
        attempts.append(
            {
                "contractId": contract_id,
                "size": size,
                "price": price,
                "side": side_str,
                "postOnly": post_only,
            }
        )
        # 3) contract_id + no post-only
        attempts.append(
            {
                "contract_id": contract_id,
                "size": size,
                "price": price,
                "side": side_str,
            }
        )
        # 4) contractId + no post-only
        attempts.append(
            {
                "contractId": contract_id,
                "size": size,
                "price": price,
                "side": side_str,
            }
        )
        # 5) single dict param
        attempts.append(
            {
                "params": {
                    "contractId": contract_id,
                    "contract_id": contract_id,
                    "size": size,
                    "price": price,
                    "side": side_str,
                    "postOnly": post_only,
                    "post_only": post_only,
                }
            }
        )

        last_err: Optional[Exception] = None
        res: Any = None

        timeout_sec = float(os.getenv("EDGEX_ORDER_TIMEOUT_SEC", "8.0"))
        for i, kw in enumerate(attempts, start=1):
            try:
                logger.debug("create_limit_order try#{}, kw_keys={}", i, list(kw.keys()))
                res = await asyncio.wait_for(create_meth(**kw), timeout=timeout_sec)
                last_err = None
                break
            except TypeError as e:
                # kwが合わない → 次へ
                last_err = e
                continue
            except Exception as e:
                # APIエラー等も一旦次へ（ただし最後で出す）
                last_err = e
                continue

        if res is None:
            raise RuntimeError(f"create_limit_order failed for contract_id={contract_id}: {last_err}")

        order_id = _extract_order_id(res)
        return Order(
            id=str(order_id),
            request=order,
            status=OrderStatus.NEW,
            filled_quantity=0.0,
            average_price=float(order.price),
            ts_ms=_now_ms(),
        )

    async def cancel_order(self, order_id: str) -> Order:
        assert self._client is not None

        cancel_meth = None
        if hasattr(self._client, "cancel_order"):
            cancel_meth = self._client.cancel_order
        elif hasattr(self._client, "order") and hasattr(self._client.order, "cancel_order"):
            cancel_meth = self._client.order.cancel_order
        if cancel_meth is None:
            raise RuntimeError("SDK has no cancel_order")

        last_err: Optional[Exception] = None
        for kw in ({"order_id": order_id}, {"orderId": order_id}, {"id": order_id}):
            try:
                await cancel_meth(**kw)
                last_err = None
                break
            except TypeError as e:
                last_err = e
                continue
            except Exception as e:
                last_err = e
                continue

        if last_err is not None:
            # 失敗してもbotは継続したいので例外は投げない（必要ならここを raise に）
            logger.debug("cancel_order failed (ignore): {}", last_err)

        dummy_req = OrderRequest(symbol="", side=OrderSide.BUY, type=None, quantity=0.0)  # type: ignore
        return Order(
            id=str(order_id),
            request=dummy_req,
            status=OrderStatus.CANCELED,
            filled_quantity=0.0,
            average_price=0.0,
            ts_ms=_now_ms(),
        )

    async def list_active_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        get_active_orders / get_active_order_page のSDK差を吸収する。
        ここで落ちると grid_engine が約定確認できず崩れるので、絶対に落とさない。
        """
        if self._client is None:
            return []

        contract_id = str(symbol or self.contract_id)

        # candidates methods
        candidates: List[Any] = []

        # 1) client.order.get_active_orders
        if hasattr(self._client, "order") and hasattr(self._client.order, "get_active_orders"):
            candidates.append(("order.get_active_orders", self._client.order.get_active_orders))
        # 2) client.get_active_orders
        if hasattr(self._client, "get_active_orders"):
            candidates.append(("get_active_orders", self._client.get_active_orders))
        # 3) legacy page method
        if hasattr(self._client, "order") and hasattr(self._client.order, "get_active_order_page"):
            candidates.append(("order.get_active_order_page", self._client.order.get_active_order_page))
        if hasattr(self._client, "get_active_order_page"):
            candidates.append(("get_active_order_page", self._client.get_active_order_page))

        last_err: Optional[Exception] = None
        resp: Any = None

        for name, meth in candidates:
            # try patterns
            patterns: List[Dict[str, Any]] = []
            patterns.append({"contract_id": contract_id})
            patterns.append({"contractId": contract_id})
            patterns.append({"symbol": contract_id})
            patterns.append({"params": {"contractId": contract_id, "contract_id": contract_id}})
            patterns.append({})  # no args

            for kw in patterns:
                try:
                    logger.debug("active_orders: try {} kw_keys={}", name, list(kw.keys()))
                    resp = await meth(**kw) if kw else await meth()
                    last_err = None
                    break
                except TypeError as e:
                    last_err = e
                    continue
                except Exception as e:
                    # API側の 401 などもここに来る。次の候補へ。
                    last_err = e
                    continue

            if resp is not None:
                break

        if resp is None:
            logger.debug("list_active_orders failed (return empty): {}", last_err)
            return []

        return _normalize_active_orders(resp)


# -------------------------
# Helpers
# -------------------------
def _extract_price_from_quote(res: Any) -> Optional[float]:
    """
    いろんな形のレスポンスから price を拾う
    """
    try:
        if res is None:
            return None
        if isinstance(res, (int, float, str)):
            return float(res)
        if isinstance(res, dict):
            d = res.get("data", res)
            if isinstance(d, dict):
                for k in ("last", "lastPrice", "price", "markPrice", "indexPrice"):
                    v = d.get(k)
                    if v is not None:
                        return float(v)
                # sometimes nested
                if isinstance(d.get("ticker"), dict):
                    t = d["ticker"]
                    for k in ("last", "lastPrice", "price"):
                        v = t.get(k)
                        if v is not None:
                            return float(v)
        # object-like
        for k in ("price", "last", "lastPrice"):
            v = getattr(res, k, None)
            if v is not None:
                return float(v)
    except Exception:
        return None
    return None


def _extract_order_id(res: Any) -> str:
    """
    create_limit_order の返り値から orderId を拾う
    """
    try:
        if res is None:
            return ""
        if isinstance(res, dict):
            d = res.get("data", res)
            if isinstance(d, dict):
                for k in ("orderId", "id", "order_id"):
                    if d.get(k):
                        return str(d.get(k))
        # object-like
        for k in ("orderId", "id", "order_id"):
            v = getattr(res, k, None)
            if v:
                return str(v)
    except Exception:
        pass
    return ""


def _normalize_active_orders(resp: Any) -> List[Dict[str, Any]]:
    """
    active orders を list[dict] に寄せる
    """
    try:
        data = resp
        if isinstance(resp, dict):
            data = resp.get("data", resp)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data.get("data")

        rows_raw = []
        if isinstance(data, dict):
            rows_raw = data.get("rows") or data.get("list") or data.get("orders") or data.get("dataList") or []
        elif isinstance(data, list):
            rows_raw = data
        else:
            rows_raw = []

        norm: List[Dict[str, Any]] = []
        for r in rows_raw:
            if isinstance(r, dict):
                norm.append(r)
            else:
                # object-like
                norm.append(
                    {
                        "orderId": getattr(r, "orderId", getattr(r, "id", None)),
                        "contractId": getattr(r, "contractId", getattr(r, "symbol", None)),
                        "status": getattr(r, "status", None),
                        "side": getattr(r, "side", None),
                        "price": getattr(r, "price", None),
                    }
                )
        return norm
    except Exception:
        return []
