"""
EdgeX SDK adapter

目的:
- EdgeX SDK のバージョン差で Client / SDKClient の __init__ 引数名がズレても落ちない
- create_limit_order / get_active_orders などの引数名がズレても TypeError を吸収して再試行する
- ExchangeAdapter 抽象メソッドをすべて実装して "abstract method" エラーを防ぐ

環境変数（想定）:
- EDGEX_BASE_URL          (例: https://pro.edgex.exchange)
- EDGEX_API_KEY
- EDGEX_API_SECRET
- EDGEX_API_PASSPHRASE
- EDGEX_ACCOUNT_ID        (数値でも文字列でもOK)
- EDGEX_CONTRACT_ID       (任意: active orders / order に使える場合)
- EDGEX_DRY_RUN           ("1"/"true"/"yes" でドライラン)
"""

from __future__ import annotations

import importlib
import inspect
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

from bot.adapters.base import ExchangeAdapter
from bot.models.types import OrderRequest, OrderSide, OrderType, TimeInForce


def _is_true(v: str | None) -> bool:
    return str(v or "").lower() in ("1", "true", "yes", "y", "on")


def _filter_kwargs(callable_obj: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """関数/メソッドの signature を見て、受け付ける kwargs だけ残す。"""
    try:
        sig = inspect.signature(callable_obj)
        allowed = set(sig.parameters.keys())
        return {k: v for k, v in kwargs.items() if k in allowed}
    except Exception:
        # signature が取れないSDKもあるので、その場合はそのまま返す（TypeErrorは上位で吸収）
        return dict(kwargs)


def _call_with_fallback(fn: Any, kwargs_variants: List[Dict[str, Any]]) -> Any:
    """
    kwargsの候補を順番に試して、TypeError（unexpected keyword）なら次を試す。
    """
    last_err: Exception | None = None
    for kw in kwargs_variants:
        try:
            kw2 = _filter_kwargs(fn, kw)
            return fn(**kw2)
        except TypeError as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    raise RuntimeError("No callable variants")


def _acall_with_fallback(fn: Any, kwargs_variants: List[Dict[str, Any]]):
    """
    async版：await 付きで試す
    """
    async def _runner():
        last_err: Exception | None = None
        for kw in kwargs_variants:
            try:
                kw2 = _filter_kwargs(fn, kw)
                return await fn(**kw2)
            except TypeError as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise RuntimeError("No callable variants")
    return _runner()


@dataclass
class _Ticker:
    price: float


class EdgeXSDKAdapter(ExchangeAdapter):
    """
    EdgeX の SDK クライアントを使うアダプタ。
    SDKのバージョン差を “吸収する” のが最優先。
    """

    def __init__(
        self,
        contract_id: Optional[str] = None,
        symbol: Optional[str] = None,
        dry_run: bool = False,
        base_url: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self.contract_id = str(contract_id) if contract_id is not None else None
        self.symbol = str(symbol) if symbol is not None else None

        self.base_url = base_url or os.getenv("EDGEX_BASE_URL", "").strip()
        self.api_key = os.getenv("EDGEX_API_KEY", "").strip()
        self.api_secret = os.getenv("EDGEX_API_SECRET", "").strip()
        self.passphrase = os.getenv("EDGEX_API_PASSPHRASE", "").strip()
        self.account_id = os.getenv("EDGEX_ACCOUNT_ID", "").strip()

        self.dry_run = bool(dry_run) or _is_true(os.getenv("EDGEX_DRY_RUN"))

        # SDKクライアント実体
        self._client: Any = None
        self._sdk_module: Any = None

        logger.info(
            "edgex adapter init: base_url={} contract_id={} symbol={} dry_run={}",
            self.base_url,
            self.contract_id,
            self.symbol,
            self.dry_run,
        )

    # ========= required by ExchangeAdapter =========

    async def connect(self) -> None:
        """
        SDK client の生成。
        ここで落ちてるので最優先で堅牢化。
        """
        if self._client is not None:
            return

        if not self.base_url:
            raise RuntimeError("EDGEX_BASE_URL is empty")

        # まずSDKモジュールを探す（プロジェクト内/依存で名前が違う可能性があるため複数候補）
        module_candidates = [
            "edgex",                 # 例
            "edgex_sdk",             # 例
            "edgex_exchange",        # 例
            "edgex_client",          # 例
        ]

        last_import_err: Exception | None = None
        for m in module_candidates:
            try:
                self._sdk_module = importlib.import_module(m)
                logger.info("edgex sdk module loaded: {}", m)
                break
            except Exception as e:
                last_import_err = e
                continue

        if self._sdk_module is None:
            raise RuntimeError(f"Failed to import edgex sdk module. last_err={last_import_err}")

        # Clientクラス候補（SDKのバージョン差で名前が違う想定）
        client_class_candidates = []
        for name in ("SDKClient", "Client", "EdgexClient", "EdgeXClient"):
            if hasattr(self._sdk_module, name):
                client_class_candidates.append(getattr(self._sdk_module, name))

        if not client_class_candidates:
            raise RuntimeError("No Client class found in edgex sdk module")

        # 引数候補パターン（あなたのログに出てた 'api_key' / 'key' / 'credentials' を吸収）
        base_kwargs = {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "passphrase": self.passphrase,
            "account_id": self.account_id,
            "accountId": self.account_id,
            "key": self.api_key,
            "secret": self.api_secret,
            "credentials": {
                "api_key": self.api_key,
                "api_secret": self.api_secret,
                "passphrase": self.passphrase,
                "account_id": self.account_id,
            },
        }

        init_variants = [
            # よくあるパターン
            {"base_url": self.base_url, "api_key": self.api_key, "api_secret": self.api_secret, "passphrase": self.passphrase, "account_id": self.account_id},
            {"base_url": self.base_url, "key": self.api_key, "secret": self.api_secret, "passphrase": self.passphrase, "accountId": self.account_id},
            {"base_url": self.base_url, "credentials": base_kwargs["credentials"]},
            # 最低限（公開APIだけでも生かしたい場合）
            {"base_url": self.base_url},
        ]

        last_err: Exception | None = None
        for cls in client_class_candidates:
            for kw in init_variants:
                try:
                    kw2 = _filter_kwargs(cls, kw)
                    self._client = cls(**kw2)
                    logger.info("edgex sdk client initialized: class={} kwargs_keys={}", getattr(cls, "__name__", str(cls)), list(kw2.keys()))
                    return
                except TypeError as e:
                    last_err = e
                    continue
                except Exception as e:
                    last_err = e
                    continue

        raise RuntimeError(f"Failed to init edgex SDK client. last_err={last_err}")

    async def close(self) -> None:
        c = self._client
        self._client = None
        if c is None:
            return
        # SDKに close/cleanup があれば呼ぶ
        for name in ("close", "aclose", "shutdown"):
            if hasattr(c, name):
                fn = getattr(c, name)
                try:
                    if inspect.iscoroutinefunction(fn):
                        await fn()
                    else:
                        fn()
                except Exception:
                    pass
                break

    async def get_ticker(self, symbol: str) -> _Ticker:
        """
        SDKが返す形が何でも、最終的に price(float) を返す。
        """
        await self.connect()
        s = str(symbol)

        # 候補メソッドを順番に試す
        candidates = [
            ("get_ticker", {"symbol": s}),
            ("ticker", {"symbol": s}),
            ("get_market_ticker", {"symbol": s}),
            ("get_ticker", {"contractId": self.contract_id} if self.contract_id else {"symbol": s}),
        ]

        for meth, kw in candidates:
            if hasattr(self._client, meth):
                fn = getattr(self._client, meth)
                try:
                    res = await _acall_with_fallback(fn, [kw, {}]) if inspect.iscoroutinefunction(fn) else _call_with_fallback(fn, [kw, {}])
                    # 価格の取り出し（dict/objどっちでも）
                    if isinstance(res, dict):
                        px = res.get("price") or res.get("last") or res.get("lastPrice") or res.get("markPrice")
                    else:
                        px = getattr(res, "price", None) or getattr(res, "last", None) or getattr(res, "lastPrice", None) or getattr(res, "markPrice", None)
                    if px is None:
                        raise RuntimeError(f"ticker has no price field: {res}")
                    return _Ticker(price=float(px))
                except Exception as e:
                    logger.debug("ticker method failed: {} err={}", meth, e)
                    continue

        raise RuntimeError("No ticker method worked")

    async def get_best_bid_ask(self, symbol: str):
        """
        可能ならbest bid/ask。SDKが無い場合は (None, None)
        """
        await self.connect()
        s = str(symbol)

        candidates = [
            ("get_best_bid_ask", {"symbol": s}),
            ("best_bid_ask", {"symbol": s}),
            ("get_orderbook", {"symbol": s}),
        ]

        for meth, kw in candidates:
            if hasattr(self._client, meth):
                fn = getattr(self._client, meth)
                try:
                    res = await _acall_with_fallback(fn, [kw, {}]) if inspect.iscoroutinefunction(fn) else _call_with_fallback(fn, [kw, {}])
                    # 形式ごとに吸収
                    if isinstance(res, dict):
                        bid = res.get("bid") or res.get("bestBid") or (res.get("bids", [None])[0][0] if res.get("bids") else None)
                        ask = res.get("ask") or res.get("bestAsk") or (res.get("asks", [None])[0][0] if res.get("asks") else None)
                    else:
                        bid = getattr(res, "bid", None) or getattr(res, "bestBid", None)
                        ask = getattr(res, "ask", None) or getattr(res, "bestAsk", None)
                    bid_f = float(bid) if bid is not None else None
                    ask_f = float(ask) if ask is not None else None
                    return bid_f, ask_f
                except Exception as e:
                    logger.debug("best bid/ask method failed: {} err={}", meth, e)
                    continue

        return None, None

    async def list_active_orders(self, symbol: str) -> List[dict]:
        """
        open注文一覧。SDKの引数が contract_id / contractId / symbol など揺れるので吸収。
        """
        await self.connect()

        # 代表的なメソッド名
        method_names = ["get_active_orders", "list_active_orders", "active_orders", "getOpenOrders", "open_orders"]

        last_err: Exception | None = None
        for meth in method_names:
            if not hasattr(self._client, meth):
                continue
            fn = getattr(self._client, meth)

            variants = []
            if self.contract_id:
                variants.append({"contract_id": self.contract_id})
                variants.append({"contractId": self.contract_id})
            variants.append({"symbol": str(symbol)})
            variants.append({})  # 最後の逃げ

            try:
                res = await _acall_with_fallback(fn, variants) if inspect.iscoroutinefunction(fn) else _call_with_fallback(fn, variants)
                if res is None:
                    return []
                # list[dict] に寄せる
                if isinstance(res, list):
                    out = []
                    for r in res:
                        out.append(r if isinstance(r, dict) else getattr(r, "__dict__", {"id": getattr(r, "id", None)}))
                    return out
                if isinstance(res, dict):
                    # ありがち: {"data":[...]} / {"orders":[...]}
                    for k in ("data", "orders", "result"):
                        if k in res and isinstance(res[k], list):
                            return [x if isinstance(x, dict) else getattr(x, "__dict__", {}) for x in res[k]]
                    return [res]
                return []
            except Exception as e:
                last_err = e
                logger.debug("list_active_orders failed: {} err={}", meth, e)
                continue

        if last_err:
            raise last_err
        return []

    async def cancel_order(self, order_id: str) -> None:
        await self.connect()
        oid = str(order_id)

        method_names = ["cancel_order", "cancel", "cancelOrder"]
        last_err: Exception | None = None
        for meth in method_names:
            if not hasattr(self._client, meth):
                continue
            fn = getattr(self._client, meth)
            variants = [{"order_id": oid}, {"orderId": oid}, {"id": oid}, {}]
            try:
                if inspect.iscoroutinefunction(fn):
                    await _acall_with_fallback(fn, variants)
                else:
                    _call_with_fallback(fn, variants)
                return
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err

    async def place_order(self, req: OrderRequest):
        """
        grid_engine は OrderRequest を渡してくる前提。
        SDKが返す order も形が揺れるので id さえ取れればOKにする。
        """
        await self.connect()

        if self.dry_run:
            logger.warning("[DRY_RUN] place_order skipped: side={} price={} qty={}", req.side, req.price, req.quantity)
            return type("Order", (), {"id": f"dry_{req.side}_{req.price}_{req.quantity}"})

        # side
        side_str = "BUY" if req.side == OrderSide.BUY else "SELL"
        # tif / post_only を吸収
        post_only = (req.time_in_force == TimeInForce.POST_ONLY)

        # よくあるメソッド名
        method_names = ["create_limit_order", "place_order", "createOrder", "new_order"]

        last_err: Exception | None = None
        for meth in method_names:
            if not hasattr(self._client, meth):
                continue
            fn = getattr(self._client, meth)

            # SDK差分対策：post_only / postOnly / timeInForce / tif
            variants = []

            base = {
                "symbol": str(req.symbol),
                "side": side_str,
                "price": float(req.price),
                "quantity": float(req.quantity),
                "size": float(req.quantity),
            }
            if self.contract_id:
                base["contract_id"] = self.contract_id
                base["contractId"] = self.contract_id

            # post-only variants
            variants.append({**base, "post_only": post_only})
            variants.append({**base, "postOnly": post_only})
            variants.append({**base, "time_in_force": "POST_ONLY" if post_only else "GTC"})
            variants.append({**base, "timeInForce": "POST_ONLY" if post_only else "GTC"})
            variants.append({**base, "tif": "POST_ONLY" if post_only else "GTC"})
            variants.append(base)

            try:
                res = await _acall_with_fallback(fn, variants) if inspect.iscoroutinefunction(fn) else _call_with_fallback(fn, variants)

                # id 抽出（dict/obj どっちでも）
                if isinstance(res, dict):
                    oid = res.get("orderId") or res.get("id") or res.get("order_id") or res.get("clientOrderId")
                else:
                    oid = getattr(res, "orderId", None) or getattr(res, "id", None) or getattr(res, "order_id", None)
                if not oid:
                    # 最悪、レスポンス全体をid扱いにしない。ここで落とす。
                    raise RuntimeError(f"order response has no id: {res}")

                return type("Order", (), {"id": str(oid)})
            except Exception as e:
                last_err = e
                logger.debug("place_order failed: {} err={}", meth, e)
                continue

        if last_err:
            raise last_err
        raise RuntimeError("No place_order method worked")

    async def fetch_positions(self, symbol: str):
        """
        grid_engine の観測用。無ければ []。
        """
        await self.connect()
        method_names = ["get_positions", "fetch_positions", "positions"]
        for meth in method_names:
            if hasattr(self._client, meth):
                fn = getattr(self._client, meth)
                variants = [{"symbol": str(symbol)}, {"contract_id": self.contract_id} if self.contract_id else {}, {}]
                try:
                    res = await _acall_with_fallback(fn, variants) if inspect.iscoroutinefunction(fn) else _call_with_fallback(fn, variants)
                    if res is None:
                        return []
                    if isinstance(res, list):
                        return [r if isinstance(r, dict) else getattr(r, "__dict__", {}) for r in res]
                    if isinstance(res, dict):
                        for k in ("data", "positions", "result"):
                            if k in res and isinstance(res[k], list):
                                return [x if isinstance(x, dict) else getattr(x, "__dict__", {}) for x in res[k]]
                        return [res]
                    return []
                except Exception:
                    pass
        return []

    async def fetch_balances(self) -> dict:
        """
        ExchangeAdapter が要求している abstract method 対策。
        SDKに無ければ空で返す。
        """
        await self.connect()
        method_names = ["get_balances", "fetch_balances", "balances"]
        for meth in method_names:
            if hasattr(self._client, meth):
                fn = getattr(self._client, meth)
                try:
                    res = await fn() if inspect.iscoroutinefunction(fn) else fn()
                    return res if isinstance(res, dict) else {"raw": res}
                except Exception:
                    continue
        return {}

