"""
EdgeX SDK Adapter
- ExchangeAdapter 抽象クラス要件を満たす
- SDK差分（Client / SDKClient、init引数、メソッド名、kwargs）を吸収して落ちにくくする
"""

from __future__ import annotations

import os
import time
import inspect
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from bot.adapters.base import ExchangeAdapter
from bot.models.types import OrderRequest, OrderSide, OrderType, TimeInForce


# ----------------------------
# helpers
# ----------------------------

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _truthy(s: Optional[str]) -> bool:
    return str(s or "").strip().lower() in ("1", "true", "yes", "on")


def _call_maybe(fn, *args, **kwargs):
    """
    fnが sync/async どちらでも呼べるようにする（await側で await する前提）
    """
    out = fn(*args, **kwargs)
    return out


async def _await_if_needed(x):
    if inspect.isawaitable(x):
        return await x
    return x


def _filter_kwargs(fn, kwargs: dict) -> dict:
    """
    fn が受け取れる kwargs だけに絞る（unexpected keyword argument を避ける）
    """
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        if any(p.kind == p.VAR_KEYWORD for p in params.values()):
            return kwargs
        allowed = set(params.keys())
        return {k: v for k, v in kwargs.items() if k in allowed}
    except Exception:
        # signature取れないケースはそのまま
        return kwargs


def _extract(obj: Any, keys: List[str]) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] is not None:
                return obj[k]
        return None
    for k in keys:
        v = getattr(obj, k, None)
        if v is not None:
            return v
    return None


@dataclass
class Ticker:
    price: float


@dataclass
class PlacedOrder:
    id: str


class EdgeXSDKAdapter(ExchangeAdapter):
    """
    EdgeXのSDKラッパ。

    ポイント:
    - SDKクラス名やinit引数差分を吸収（Client/SKDCient、api_key/key/credentials 等）
    - order系のメソッド名差分を吸収（place_order/create_limit_order/create_order 等）
    - active_orders系の引数差分を吸収（symbol/contract_id など）
    - ExchangeAdapter必須の fetch_balances() を実装（落ちないこと優先）
    """

    def __init__(
        self,
        contract_id: Optional[str] = None,
        symbol: Optional[str] = None,
        dry_run: bool = False,
        base_url: Optional[str] = None,
        **kwargs,
    ):
        self.contract_id = str(contract_id) if contract_id not in (None, "") else None
        self.symbol = str(symbol) if symbol not in (None, "") else None
        self.dry_run = bool(dry_run)
        self.base_url = base_url or _env("EDGEX_BASE_URL", "https://pro.edgex.exchange")

        # 認証
        self.api_key = _env("EDGEX_API_KEY")
        self.api_secret = _env("EDGEX_API_SECRET")
        self.passphrase = _env("EDGEX_API_PASSPHRASE", _env("EDGEX_PASSPHRASE"))
        self.account_id = _env("EDGEX_ACCOUNT_ID")
        self.stark_private_key = _env("EDGEX_STARK_PRIVATE_KEY")

        # authを意図的にスキップするモード（ログにあったので残す）
        self.skip_auth = _truthy(_env("EDGEX_SKIP_AUTH", "0"))

        self._client = None
        self._sdk_module = None

        logger.info(
            "edgex adapter init: base_url={} contract_id={} symbol={} dry_run={} skip_auth={}",
            self.base_url,
            self.contract_id,
            self.symbol,
            self.dry_run,
            self.skip_auth,
        )

    # ----------------------------
    # internal
    # ----------------------------

    def _load_sdk(self):
        if self._sdk_module is not None:
            return self._sdk_module
        try:
            import edgex_sdk  # type: ignore
            self._sdk_module = edgex_sdk
            logger.info("edgex sdk module loaded: edgex_sdk")
            return edgex_sdk
        except Exception as e:
            logger.error("failed to import edgex_sdk: {}", e)
            raise

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("EdgeX SDK client not initialized. Call connect() first.")
        return self._client

    def _init_client(self):
        sdk = self._load_sdk()

        # SDK側のクライアント候補
        ClientCls = None
        for name in ("SDKClient", "Client", "EdgexClient", "EdgeXClient"):
            ClientCls = getattr(sdk, name, None)
            if ClientCls is not None:
                break
        if ClientCls is None:
            raise RuntimeError("edgex_sdk: client class not found (SDKClient/Client/...)")

        # init候補パターンを順に試す（SDK差分吸収）
        candidates: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []

        # 1) positional + named の組み合わせ（ありがち）
        candidates.append((
            tuple(),
            dict(
                base_url=self.base_url,
                api_key=self.api_key,
                api_secret=self.api_secret,
                passphrase=self.passphrase,
                account_id=self.account_id,
            ),
        ))
        candidates.append((
            tuple(),
            dict(
                base_url=self.base_url,
                key=self.api_key,
                secret=self.api_secret,
                passphrase=self.passphrase,
                accountId=self.account_id,
            ),
        ))
        candidates.append((
            tuple(),
            dict(
                base_url=self.base_url,
                credentials=dict(
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    passphrase=self.passphrase,
                ),
                account_id=self.account_id,
            ),
        ))

        # 2) stark系を要求するタイプ（ログに出てたので優先で試す）
        candidates.insert(0, (
            tuple(),
            dict(
                base_url=self.base_url,
                account_id=self.account_id,
                stark_private_key=self.stark_private_key,
            ),
        ))
        candidates.insert(1, (
            tuple(),
            dict(
                base_url=self.base_url,
                accountId=self.account_id,
                starkPrivateKey=self.stark_private_key,
            ),
        ))

        # 3) 認証スキップ時（最低限base_urlのみで立てる）
        if self.skip_auth:
            candidates.insert(0, (tuple(), dict(base_url=self.base_url)))

        last_err = None
        for args, kw in candidates:
            try:
                # signatureに合わせてkwを削る
                filtered = _filter_kwargs(ClientCls.__init__, kw)
                # Noneは落とす
                filtered = {k: v for k, v in filtered.items() if v is not None}

                client = ClientCls(*args, **filtered)  # type: ignore
                self._client = client
                return
            except Exception as e:
                last_err = e
                continue

        raise RuntimeError(f"Failed to init edgex SDK client. last_err={last_err}")

    # ----------------------------
    # ExchangeAdapter required
    # ----------------------------

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._init_client()

        # connect/openの有無を吸収
        c = self._require_client()
        for name in ("connect", "open", "start"):
            fn = getattr(c, name, None)
            if callable(fn):
                try:
                    await _await_if_needed(_call_maybe(fn))
                except Exception as e:
                    logger.debug("client {}() failed (ignore): {}", name, e)
                break

    async def close(self) -> None:
        c = self._client
        self._client = None
        if c is None:
            return
        for name in ("close", "disconnect", "stop"):
            fn = getattr(c, name, None)
            if callable(fn):
                try:
                    await _await_if_needed(_call_maybe(fn))
                except Exception:
                    pass
                break

    async def get_ticker(self, symbol: str):
        c = self._require_client()

        # symbol/contract_idどっちを渡すか：両方試す
        last_err = None
        for name in ("get_ticker", "ticker", "get_market_ticker", "get_contract_ticker"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue
            for kw in (
                {"symbol": symbol},
                {"contract_id": self.contract_id or symbol},
                {"contractId": self.contract_id or symbol},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, kw)
                    res = await _await_if_needed(_call_maybe(fn, **filtered))
                    px = _extract(res, ["price", "last", "lastPrice", "markPrice", "mark_price"])
                    if px is None and isinstance(res, dict):
                        # たまに {"data": {...}} 形式
                        px = _extract(res.get("data"), ["price", "last", "lastPrice", "markPrice", "mark_price"])
                    if px is None:
                        raise RuntimeError(f"ticker price not found in response: {res}")
                    return Ticker(price=float(px))
                except Exception as e:
                    last_err = e
                    continue
        raise RuntimeError(f"get_ticker failed: {last_err}")

    async def get_best_bid_ask(self, symbol: str):
        c = self._require_client()
        last_err = None
        for name in ("get_best_bid_ask", "best_bid_ask", "get_orderbook", "orderbook"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue
            for kw in (
                {"symbol": symbol},
                {"contract_id": self.contract_id or symbol},
                {"contractId": self.contract_id or symbol},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, kw)
                    res = await _await_if_needed(_call_maybe(fn, **filtered))
                    # orderbookなら bids/asks から拾う
                    bids = _extract(res, ["bids", "bid", "bestBid", "best_bid"])
                    asks = _extract(res, ["asks", "ask", "bestAsk", "best_ask"])
                    bid = None
                    ask = None
                    if isinstance(bids, list) and bids:
                        bid = float(bids[0][0] if isinstance(bids[0], (list, tuple)) else bids[0].get("price", bids[0]))
                    if isinstance(asks, list) and asks:
                        ask = float(asks[0][0] if isinstance(asks[0], (list, tuple)) else asks[0].get("price", asks[0]))
                    # 直接bestBid/bestAsk形式
                    if bid is None:
                        b = _extract(res, ["bestBid", "best_bid", "bid"])
                        if b is not None:
                            bid = float(b)
                    if ask is None:
                        a = _extract(res, ["bestAsk", "best_ask", "ask"])
                        if a is not None:
                            ask = float(a)
                    return bid, ask
                except Exception as e:
                    last_err = e
                    continue
        # 取れないなら None, None で返す（grid_engine側がtickerにフォールバックする）
        logger.debug("get_best_bid_ask unavailable: {}", last_err)
        return None, None

    async def place_order(self, req: OrderRequest):
        """
        OrderRequestを受け取り、SDKの注文メソッド差分を吸収して発注する。
        """
        if self.dry_run:
            fake_id = f"DRYRUN-{int(time.time()*1000)}"
            logger.info("[DRY_RUN] place_order: side={} price={} qty={}", req.side, req.price, req.quantity)
            return PlacedOrder(id=fake_id)

        c = self._require_client()

        # POST_ONLY 指定（SDKにより post_only / postOnly / timeInForce など）
        post_only = (req.time_in_force == TimeInForce.POST_ONLY)

        side = "BUY" if req.side == OrderSide.BUY else "SELL"
        qty = float(req.quantity)
        px = float(req.price)

        # よくあるメソッド候補（順に試す）
        methods = [
            "place_order",
            "create_limit_order",
            "create_order",
            "limit_order",
            "createLimitOrder",
        ]

        last_err = None
        for name in methods:
            fn = getattr(c, name, None)
            if not callable(fn):
                continue

            # パラメータの候補（SDK差分で contract_id/symbol、post_only名、qty名が揺れる）
            candidate_kwargs_list = [
                dict(symbol=req.symbol, side=side, price=px, size=qty, post_only=post_only),
                dict(symbol=req.symbol, side=side, price=px, quantity=qty, post_only=post_only),
                dict(symbol=req.symbol, side=side, price=px, qty=qty, post_only=post_only),

                dict(contract_id=self.contract_id, side=side, price=px, size=qty, post_only=post_only),
                dict(contract_id=self.contract_id, side=side, price=px, quantity=qty, post_only=post_only),
                dict(contractId=self.contract_id, side=side, price=px, size=qty, postOnly=post_only),

                # timeInForceで渡すタイプ
                dict(symbol=req.symbol, side=side, price=px, size=qty, time_in_force="POST_ONLY" if post_only else "GTC"),
                dict(contract_id=self.contract_id, side=side, price=px, size=qty, time_in_force="POST_ONLY" if post_only else "GTC"),
            ]

            for kw in candidate_kwargs_list:
                # None落とす
                kw = {k: v for k, v in kw.items() if v is not None}

                try:
                    filtered = _filter_kwargs(fn, kw)
                    res = await _await_if_needed(_call_maybe(fn, **filtered))

                    oid = _extract(res, ["id", "orderId", "order_id", "clientOrderId", "client_order_id"])
                    if oid is None and isinstance(res, dict):
                        oid = _extract(res.get("data"), ["id", "orderId", "order_id"])
                    if oid is None:
                        # 返り値が文字列idのこともある
                        if isinstance(res, str):
                            oid = res
                        else:
                            oid = str(res)

                    return PlacedOrder(id=str(oid))
                except Exception as e:
                    last_err = e
                    continue

        raise RuntimeError(f"place_order failed: {last_err}")

    async def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            logger.info("[DRY_RUN] cancel_order: {}", order_id)
            return

        c = self._require_client()
        last_err = None
        for name in ("cancel_order", "cancel", "cancelOrder"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue
            for kw in (
                {"order_id": order_id},
                {"orderId": order_id},
                {"id": order_id},
                {"order": order_id},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, kw)
                    await _await_if_needed(_call_maybe(fn, **filtered))
                    return
                except Exception as e:
                    last_err = e
                    continue
        raise RuntimeError(f"cancel_order failed: {last_err}")

    async def list_active_orders(self, symbol: str) -> list:
        """
        grid_engine側は dict の配列想定でも動くようにしてあるので、
        ここは "できるだけdictの配列" を返す。
        """
        c = self._require_client()

        last_err = None
        for name in ("get_active_orders", "list_active_orders", "active_orders", "getOpenOrders", "open_orders"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue

            # ここがログで死にやすかった: contract_id が unexpected になる SDK があるので filter_kwargs で落とす
            for kw in (
                {"symbol": symbol},
                {"contract_id": self.contract_id or symbol},
                {"contractId": self.contract_id or symbol},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, kw)
                    res = await _await_if_needed(_call_maybe(fn, **filtered))

                    # 形式をならす
                    if isinstance(res, dict) and "data" in res:
                        res = res["data"]
                    if res is None:
                        return []
                    if isinstance(res, list):
                        # list内がオブジェクトならdict化
                        out = []
                        for o in res:
                            if isinstance(o, dict):
                                out.append(o)
                            else:
                                out.append(getattr(o, "__dict__", {"raw": str(o)}))
                        return out
                    # 単体dict
                    if isinstance(res, dict):
                        return [res]
                    # その他
                    return [getattr(res, "__dict__", {"raw": str(res)})]
                except Exception as e:
                    last_err = e
                    continue

        # 401等はここで握って grid_engine が動き続ける方が良い（ログで見えてたので）
        logger.debug("list_active_orders failed (return []): {}", last_err)
        return []

    async def fetch_positions(self, symbol: str) -> list:
        c = self._require_client()
        last_err = None
        for name in ("get_positions", "fetch_positions", "positions", "get_position"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue
            for kw in (
                {"symbol": symbol},
                {"contract_id": self.contract_id or symbol},
                {"contractId": self.contract_id or symbol},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, kw)
                    res = await _await_if_needed(_call_maybe(fn, **filtered))
                    if isinstance(res, dict) and "data" in res:
                        res = res["data"]
                    if res is None:
                        return []
                    if isinstance(res, list):
                        out = []
                        for o in res:
                            if isinstance(o, dict):
                                out.append(o)
                            else:
                                out.append(getattr(o, "__dict__", {"raw": str(o)}))
                        return out
                    if isinstance(res, dict):
                        return [res]
                    return [getattr(res, "__dict__", {"raw": str(res)})]
                except Exception as e:
                    last_err = e
                    continue
        logger.debug("fetch_positions unavailable: {}", last_err)
        return []

    async def fetch_balances(self) -> dict:
        """
        ExchangeAdapter の必須abstractを満たすための実装。
        SDK差分があるので、取れる範囲で取得し、無理なら空dictで返す（落ちないこと優先）
        """
        c = self._require_client()

        candidates = [
            ("get_balances", {}),
            ("fetch_balances", {}),
            ("balances", {}),
            ("get_account", {}),
            ("get_wallet", {}),
        ]

        last_err = None
        for name, kw in candidates:
            fn = getattr(c, name, None)
            if not callable(fn):
                continue
            try:
                filtered = _filter_kwargs(fn, kw)
                res = await _await_if_needed(_call_maybe(fn, **filtered))
                if isinstance(res, dict):
                    return res
                if isinstance(res, list):
                    return {"data": res}
                return getattr(res, "__dict__", {"raw": str(res)})
            except Exception as e:
                last_err = e
                continue

        logger.debug("fetch_balances not available: {}", last_err)
        return {}
