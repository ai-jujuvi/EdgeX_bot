"""
EdgeX SDK Adapter (Robust / Backward-compatible)

GOAL:
- ExchangeAdapter 抽象クラス要件を満たす
- SDK差分（Client / SDKClient、init引数、メソッド名、kwargs）を吸収して落ちにくくする
- grid_engine 側の呼び方の揺れに耐える
  - place_order(req)
  - place_order(side, price, size)
  - place_order(args=(OrderRequest(...),))
  - place_order(OrderRequest(...))
- symbol と contract_id のどちらで呼ばれても、可能な限り成功させる
- 401/429 など一部の失敗は握って、エンジンを落とさない（ログで追える形に）

NOTE:
- 「短いmini版」へ置き換えるのは危険。ここは "受け皿" として厚めにしている。
"""

from __future__ import annotations

import os
import time
import inspect
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from loguru import logger

from bot.adapters.base import ExchangeAdapter
from bot.models.types import OrderRequest, OrderSide, OrderType, TimeInForce


# ============================================================
# Helpers
# ============================================================

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _truthy(s: Optional[str]) -> bool:
    return str(s or "").strip().lower() in ("1", "true", "yes", "on")


def _call_maybe(fn, *args, **kwargs):
    """
    fn が sync/async どちらでも呼べるようにする。
    呼び出し側で await できるように "結果" を返すだけ。
    """
    return fn(*args, **kwargs)


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
    """
    obj が dict / object どちらでも、keys のどれかが見つかれば返す
    """
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


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _as_str(x: Any) -> Optional[str]:
    try:
        if x is None:
            return None
        s = str(x)
        return s if s != "" else None
    except Exception:
        return None


def _upper(x: Any) -> str:
    return str(x or "").upper().strip()


def _safe_now_ms() -> int:
    return int(time.time() * 1000)


def _clean_none(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e).lower()
    # よくある文字列（SDK/HTTP層が違っても拾えるように）
    return any(s in msg for s in ("429", "rate limit", "too many requests", "throttle"))


def _is_auth_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in ("401", "unauthorized", "forbidden", "signature", "invalid api", "not authorized"))


# ============================================================
# Lightweight DTOs (what grid_engine expects)
# ============================================================

@dataclass
class Ticker:
    price: float


@dataclass
class PlacedOrder:
    id: str


# ============================================================
# Adapter
# ============================================================

class EdgeXSDKAdapter(ExchangeAdapter):
    """
    EdgeXのSDKラッパ（落ちにくい受け皿）

    ポイント:
    - SDKクラス名やinit引数差分を吸収（Client/SKDCient、api_key/key/credentials 等）
    - order系のメソッド名差分を吸収（place_order/create_limit_order/create_order 等）
    - active_orders系の引数差分を吸収（symbol/contract_id など）
    - ExchangeAdapter必須の fetch_balances() を実装（落ちないこと優先）
    - grid_engine 側の呼び方の揺れ（place_orderの引数形）を吸収

    ENV:
    - EDGEX_BASE_URL
    - EDGEX_CONTRACT_ID
    - EDGEX_SYMBOL
    - EDGEX_API_KEY / EDGEX_API_SECRET / EDGEX_API_PASSPHRASE
    - EDGEX_ACCOUNT_ID
    - EDGEX_STARK_PRIVATE_KEY
    - EDGEX_SKIP_AUTH (1: 認証スキップで初期化を試す)
    - EDGEX_ADAPTER_STRICT (1: 例外を握らずraiseを優先)
    """

    def __init__(
        self,
        contract_id: Optional[str] = None,
        symbol: Optional[str] = None,
        dry_run: bool = False,
        base_url: Optional[str] = None,
        **kwargs,
    ):
        # 優先順位: 引数 > ENV
        env_contract = _env("EDGEX_CONTRACT_ID")
        env_symbol = _env("EDGEX_SYMBOL")

        self.contract_id = _as_str(contract_id) or _as_str(env_contract)
        self.symbol = _as_str(symbol) or _as_str(env_symbol)
        self.dry_run = bool(dry_run)
        self.base_url = base_url or _env("EDGEX_BASE_URL", "https://pro.edgex.exchange")

        # 認証（両対応）
        self.api_key = _env("EDGEX_API_KEY")
        self.api_secret = _env("EDGEX_API_SECRET")
        self.passphrase = _env("EDGEX_API_PASSPHRASE", _env("EDGEX_PASSPHRASE"))
        self.account_id = _env("EDGEX_ACCOUNT_ID")
        self.stark_private_key = _env("EDGEX_STARK_PRIVATE_KEY")

        # authを意図的にスキップするモード（ログにあったので残す）
        self.skip_auth = _truthy(_env("EDGEX_SKIP_AUTH", "0"))

        # 例外を握るか（本番運用向けは握る=0、デバッグで原因追うなら1）
        self.strict = _truthy(_env("EDGEX_ADAPTER_STRICT", "0"))

        self._client = None
        self._sdk_module = None

        # 呼び方揺れの統計（軽いデバッグ用）
        self._place_calls = 0
        self._place_success = 0
        self._last_place_err: Optional[str] = None

        logger.info(
            "edgex adapter init: base_url={} contract_id={} symbol={} dry_run={} skip_auth={} strict={}",
            self.base_url,
            self.contract_id,
            self.symbol,
            self.dry_run,
            self.skip_auth,
            self.strict,
        )

    # ---------------------------------------------------------
    # Internal: SDK load / client init
    # ---------------------------------------------------------

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
        """
        SDKクライアントの差分を吸収して初期化。
        できるだけ "落ちない" を優先し、複数のinitパターンを試す。
        """
        sdk = self._load_sdk()

        # SDK側のクライアント候補
        ClientCls = None
        for name in ("SDKClient", "Client", "EdgexClient", "EdgeXClient"):
            ClientCls = getattr(sdk, name, None)
            if ClientCls is not None:
                break
        if ClientCls is None:
            raise RuntimeError("edgex_sdk: client class not found (SDKClient/Client/...)")

        candidates: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []

        # 0) 認証スキップ時（最低限base_urlのみ）
        if self.skip_auth:
            candidates.append((tuple(), dict(base_url=self.base_url)))

        # 1) stark系（ログに出てた）
        candidates.append((
            tuple(),
            dict(
                base_url=self.base_url,
                account_id=self.account_id,
                stark_private_key=self.stark_private_key,
            ),
        ))
        candidates.append((
            tuple(),
            dict(
                base_url=self.base_url,
                accountId=self.account_id,
                starkPrivateKey=self.stark_private_key,
            ),
        ))

        # 2) API key/secret/passphrase
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

        last_err = None
        for args, kw in candidates:
            try:
                # signatureに合わせてkwを削る
                filtered = _filter_kwargs(ClientCls.__init__, kw)
                filtered = _clean_none(filtered)

                client = ClientCls(*args, **filtered)  # type: ignore
                self._client = client
                logger.info("edgex client initialized via {} kwargs={}", getattr(ClientCls, "__name__", "Client"), list(filtered.keys()))
                return
            except Exception as e:
                last_err = e
                continue

        raise RuntimeError(f"Failed to init edgex SDK client. last_err={last_err}")

    def _resolve_symbol(self, symbol: Optional[str]) -> str:
        """
        呼び出し側から symbol が渡ってきたら優先。
        無ければ self.symbol、無ければ contract_id を fallback。
        """
        return _as_str(symbol) or self.symbol or self.contract_id or ""

    def _resolve_contract_id(self, symbol_or_contract: Optional[str]) -> Optional[str]:
        """
        contract_id が明示されていればそれを優先。
        無ければ symbol_or_contract を contract_id として試す。
        """
        return self.contract_id or _as_str(symbol_or_contract)

    def _raise_or_log(self, msg: str, e: Optional[Exception] = None):
        """
        strict=1 なら raise、そうでなければログで握る。
        """
        if self.strict:
            if e is None:
                raise RuntimeError(msg)
            raise RuntimeError(f"{msg}: {e}")
        if e is None:
            logger.warning(msg)
        else:
            logger.warning("{}: {}", msg, e)

    # ---------------------------------------------------------
    # ExchangeAdapter required
    # ---------------------------------------------------------

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._init_client()

        c = self._require_client()
        for name in ("connect", "open", "start"):
            fn = getattr(c, name, None)
            if callable(fn):
                try:
                    await _await_if_needed(_call_maybe(fn))
                except Exception as e:
                    # connect失敗は致命だが、SDKによっては不要な場合もある
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

    # ---------------------------------------------------------
    # Market Data
    # ---------------------------------------------------------

    async def get_ticker(self, symbol: str):
        c = self._require_client()
        sym = self._resolve_symbol(symbol)
        cid = self._resolve_contract_id(sym)

        last_err = None
        for name in ("get_ticker", "ticker", "get_market_ticker", "get_contract_ticker"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue

            for kw in (
                {"symbol": sym},
                {"contract_id": cid},
                {"contractId": cid},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, _clean_none(dict(kw)))
                    res = await _await_if_needed(_call_maybe(fn, **filtered))

                    # price候補
                    px = _extract(res, ["price", "last", "lastPrice", "markPrice", "mark_price"])
                    if px is None and isinstance(res, dict):
                        px = _extract(res.get("data"), ["price", "last", "lastPrice", "markPrice", "mark_price"])

                    fpx = _as_float(px)
                    if fpx is None:
                        raise RuntimeError(f"ticker price not found in response: {res}")

                    return Ticker(price=float(fpx))
                except Exception as e:
                    last_err = e
                    continue

        raise RuntimeError(f"get_ticker failed: {last_err}")

    async def get_best_bid_ask(self, symbol: str):
        """
        取れない取引所/SDKもあるので、失敗時は (None, None) を返す。
        grid_engine 側が ticker にフォールバックできる設計。
        """
        c = self._require_client()
        sym = self._resolve_symbol(symbol)
        cid = self._resolve_contract_id(sym)

        last_err = None
        for name in ("get_best_bid_ask", "best_bid_ask", "get_orderbook", "orderbook"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue

            for kw in (
                {"symbol": sym},
                {"contract_id": cid},
                {"contractId": cid},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, _clean_none(dict(kw)))
                    res = await _await_if_needed(_call_maybe(fn, **filtered))

                    # orderbookなら bids/asks から拾う
                    bids = _extract(res, ["bids", "bid", "bestBid", "best_bid"])
                    asks = _extract(res, ["asks", "ask", "bestAsk", "best_ask"])

                    bid = None
                    ask = None

                    if isinstance(bids, list) and bids:
                        if isinstance(bids[0], (list, tuple)) and bids[0]:
                            bid = _as_float(bids[0][0])
                        elif isinstance(bids[0], dict):
                            bid = _as_float(bids[0].get("price"))
                        else:
                            bid = _as_float(bids[0])

                    if isinstance(asks, list) and asks:
                        if isinstance(asks[0], (list, tuple)) and asks[0]:
                            ask = _as_float(asks[0][0])
                        elif isinstance(asks[0], dict):
                            ask = _as_float(asks[0].get("price"))
                        else:
                            ask = _as_float(asks[0])

                    # 直接bestBid/bestAsk形式
                    if bid is None:
                        bid = _as_float(_extract(res, ["bestBid", "best_bid", "bid"]))
                    if ask is None:
                        ask = _as_float(_extract(res, ["bestAsk", "best_ask", "ask"]))

                    return bid, ask
                except Exception as e:
                    last_err = e
                    continue

        logger.debug("get_best_bid_ask unavailable: {}", last_err)
        return None, None

    # ---------------------------------------------------------
    # Order: tolerant request parsing
    # ---------------------------------------------------------

    def _parse_order_args(
        self,
        req: Any,
        *args,
        **kwargs,
    ) -> Tuple[OrderRequest, Dict[str, Any]]:
        """
        grid_engine側の呼び方が揺れても落ちないように吸収する。

        返り値:
          - normalized OrderRequest
          - extra options (post_only override etc.)
        """
        extra: Dict[str, Any] = {}

        # 1) kwargs に args=(OrderRequest,) が来るケース
        if req is None and "args" in kwargs:
            maybe_args = kwargs.get("args")
            if isinstance(maybe_args, tuple) and len(maybe_args) == 1:
                req = maybe_args[0]

        # 2) req が OrderRequest の場合
        if isinstance(req, OrderRequest):
            return req, extra

        # 3) req が dict で OrderRequestっぽい場合
        if isinstance(req, dict):
            try:
                sym = _as_str(req.get("symbol")) or self.symbol or ""
                side_raw = req.get("side")
                side = side_raw if isinstance(side_raw, OrderSide) else (OrderSide.BUY if _upper(side_raw) in ("BUY", "LONG") else OrderSide.SELL)
                typ_raw = req.get("type") or req.get("orderType")
                typ = typ_raw if isinstance(typ_raw, OrderType) else OrderType.LIMIT
                qty = float(req.get("quantity") or req.get("size") or req.get("qty"))
                px = float(req.get("price") or req.get("px"))
                tif_raw = req.get("time_in_force") or req.get("timeInForce")
                tif = tif_raw if isinstance(tif_raw, TimeInForce) else TimeInForce.POST_ONLY
                return OrderRequest(symbol=sym, side=side, type=typ, quantity=qty, price=px, time_in_force=tif), extra
            except Exception:
                pass

        # 4) place_order(side, price, size) 形式
        #    - req が side
        #    - args[0] が price、args[1] が size になりがち
        if req is not None and args:
            side_raw = req
            price = args[0] if len(args) >= 1 else kwargs.get("price")
            size = args[1] if len(args) >= 2 else kwargs.get("size") or kwargs.get("quantity") or kwargs.get("qty")

            # side
            if isinstance(side_raw, OrderSide):
                side = side_raw
            else:
                side = OrderSide.BUY if _upper(side_raw) in ("BUY", "LONG") else OrderSide.SELL

            # price/qty
            px = float(price)
            qty = float(size)

            sym = _as_str(kwargs.get("symbol")) or self.symbol or ""
            tif = kwargs.get("time_in_force") or kwargs.get("timeInForce") or TimeInForce.POST_ONLY
            if not isinstance(tif, TimeInForce):
                # 文字列なら POST_ONLY っぽいかだけ見る
                tif = TimeInForce.POST_ONLY if _upper(tif) in ("POST_ONLY", "POSTONLY", "PO") else TimeInForce.GTC

            return OrderRequest(
                symbol=sym,
                side=side,
                type=OrderType.LIMIT,
                quantity=qty,
                price=px,
                time_in_force=tif,
            ), extra

        # 5) 最後の砦: kwargs から拾う
        try:
            sym = _as_str(kwargs.get("symbol")) or self.symbol or ""
            side_raw = kwargs.get("side")
            if isinstance(side_raw, OrderSide):
                side = side_raw
            else:
                side = OrderSide.BUY if _upper(side_raw) in ("BUY", "LONG") else OrderSide.SELL
            px = float(kwargs.get("price") or kwargs.get("px"))
            qty = float(kwargs.get("quantity") or kwargs.get("size") or kwargs.get("qty"))
            tif = kwargs.get("time_in_force") or kwargs.get("timeInForce") or TimeInForce.POST_ONLY
            if not isinstance(tif, TimeInForce):
                tif = TimeInForce.POST_ONLY if _upper(tif) in ("POST_ONLY", "POSTONLY", "PO") else TimeInForce.GTC
            return OrderRequest(symbol=sym, side=side, type=OrderType.LIMIT, quantity=qty, price=px, time_in_force=tif), extra
        except Exception as e:
            raise RuntimeError(f"could not parse order args: req={req} args={args} kwargs={kwargs}") from e

    async def place_order(self, req: Any, *args, **kwargs):
        """
        OrderRequestを受け取り、SDKの注文メソッド差分を吸収して発注する。
        ※ 互換のため Any + *args を許容。
        """
        self._place_calls += 1

        # 受け皿: 呼び方揺れを OrderRequest に正規化
        norm_req, extra = self._parse_order_args(req, *args, **kwargs)

        if self.dry_run:
            fake_id = f"DRYRUN-{_safe_now_ms()}"
            logger.info(
                "[DRY_RUN] place_order: symbol={} side={} price={} qty={} tif={}",
                norm_req.symbol, norm_req.side, norm_req.price, norm_req.quantity, norm_req.time_in_force,
            )
            self._place_success += 1
            return PlacedOrder(id=fake_id)

        c = self._require_client()

        # POST_ONLY 指定（SDKにより post_only / postOnly / timeInForce など）
        post_only = (norm_req.time_in_force == TimeInForce.POST_ONLY)

        side = "BUY" if norm_req.side == OrderSide.BUY else "SELL"
        qty = float(norm_req.quantity)
        px = float(norm_req.price)

        sym = self._resolve_symbol(norm_req.symbol)
        cid = self._resolve_contract_id(sym)

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

            # パラメータ候補（SDK差分で揺れる）
            candidate_kwargs_list = [
                # symbol系
                dict(symbol=sym, side=side, price=px, size=qty, post_only=post_only),
                dict(symbol=sym, side=side, price=px, quantity=qty, post_only=post_only),
                dict(symbol=sym, side=side, price=px, qty=qty, post_only=post_only),
                dict(symbol=sym, side=side, price=px, size=qty, postOnly=post_only),
                dict(symbol=sym, side=side, price=px, quantity=qty, postOnly=post_only),

                # contract_id系
                dict(contract_id=cid, side=side, price=px, size=qty, post_only=post_only),
                dict(contract_id=cid, side=side, price=px, quantity=qty, post_only=post_only),
                dict(contractId=cid, side=side, price=px, size=qty, postOnly=post_only),

                # timeInForce系（POST_ONLY文字列）
                dict(symbol=sym, side=side, price=px, size=qty, time_in_force="POST_ONLY" if post_only else "GTC"),
                dict(symbol=sym, side=side, price=px, quantity=qty, time_in_force="POST_ONLY" if post_only else "GTC"),
                dict(contract_id=cid, side=side, price=px, size=qty, time_in_force="POST_ONLY" if post_only else "GTC"),
                dict(contract_id=cid, side=side, price=px, quantity=qty, time_in_force="POST_ONLY" if post_only else "GTC"),

                # timeInForce camel
                dict(symbol=sym, side=side, price=px, size=qty, timeInForce="POST_ONLY" if post_only else "GTC"),
                dict(contractId=cid, side=side, price=px, size=qty, timeInForce="POST_ONLY" if post_only else "GTC"),
            ]

            for kw in candidate_kwargs_list:
                kw = _clean_none(kw)

                try:
                    filtered = _filter_kwargs(fn, kw)
                    res = await _await_if_needed(_call_maybe(fn, **filtered))

                    oid = _extract(res, ["id", "orderId", "order_id", "clientOrderId", "client_order_id"])
                    if oid is None and isinstance(res, dict):
                        oid = _extract(res.get("data"), ["id", "orderId", "order_id"])

                    if oid is None:
                        if isinstance(res, str):
                            oid = res
                        else:
                            oid = str(res)

                    self._place_success += 1
                    return PlacedOrder(id=str(oid))
                except Exception as e:
                    last_err = e
                    # 429は少し待ってリトライする価値があるが、ここでは "次候補へ" で十分
                    continue

        self._last_place_err = str(last_err)
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
                    filtered = _filter_kwargs(fn, _clean_none(dict(kw)))
                    await _await_if_needed(_call_maybe(fn, **filtered))
                    return
                except Exception as e:
                    last_err = e
                    continue

        # cancelは失敗しても致命ではないケースが多い
        if self.strict:
            raise RuntimeError(f"cancel_order failed: {last_err}")
        logger.debug("cancel_order failed (ignore): {}", last_err)

    async def list_active_orders(self, symbol: str) -> list:
        """
        grid_engine側は dict の配列想定でも動くようにしてあるので、
        ここは "できるだけdictの配列" を返す。
        """
        c = self._require_client()

        sym = self._resolve_symbol(symbol)
        cid = self._resolve_contract_id(sym)

        last_err = None
        for name in ("get_active_orders", "list_active_orders", "active_orders", "getOpenOrders", "open_orders"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue

            for kw in (
                {"symbol": sym},
                {"contract_id": cid},
                {"contractId": cid},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, _clean_none(dict(kw)))
                    res = await _await_if_needed(_call_maybe(fn, **filtered))

                    # 形式をならす
                    if isinstance(res, dict) and "data" in res:
                        res = res["data"]

                    if res is None:
                        return []

                    if isinstance(res, list):
                        return [self._normalize_order_row(o) for o in res]

                    if isinstance(res, dict):
                        return [self._normalize_order_row(res)]

                    return [self._normalize_order_row(getattr(res, "__dict__", {"raw": str(res)}))]
                except Exception as e:
                    last_err = e
                    continue

        # 401/429等はここで握って grid_engine が動き続ける方が良い
        if last_err is not None:
            if _is_auth_error(last_err) or _is_rate_limit_error(last_err):
                logger.debug("list_active_orders failed (return []): {}", last_err)
                return []

        if self.strict:
            raise RuntimeError(f"list_active_orders failed: {last_err}")
        logger.debug("list_active_orders failed (return []): {}", last_err)
        return []

    def _normalize_order_row(self, o: Any) -> Dict[str, Any]:
        """
        返却を "dictとして扱いやすい形" に寄せる（grid_engine の堅牢化にも効く）
        """
        if o is None:
            return {}

        row = o if isinstance(o, dict) else getattr(o, "__dict__", {"raw": str(o)})

        # id
        oid = _extract(row, ["orderId", "id", "order_id", "clientOrderId", "client_order_id"])
        # side
        side = _upper(_extract(row, ["side", "orderSide", "positionSide"]))
        # price
        px = _extract(row, ["price", "px"])
        # status
        st = _upper(_extract(row, ["status", "state"]))
        # symbol / contract
        sym = _extract(row, ["symbol", "market", "instrument"])
        cid = _extract(row, ["contract_id", "contractId", "contract"])

        out = dict(row) if isinstance(row, dict) else {"raw": row}
        if oid is not None:
            out["orderId"] = str(oid)
        if side:
            out["side"] = side
        if px is not None:
            out["price"] = px
        if st:
            out["status"] = st
        if sym is not None and "symbol" not in out:
            out["symbol"] = sym
        if cid is not None and "contract_id" not in out:
            out["contract_id"] = cid
        return out

    # ---------------------------------------------------------
    # Positions / Balances
    # ---------------------------------------------------------

    async def fetch_positions(self, symbol: str) -> list:
        c = self._require_client()
        sym = self._resolve_symbol(symbol)
        cid = self._resolve_contract_id(sym)

        last_err = None
        for name in ("get_positions", "fetch_positions", "positions", "get_position"):
            fn = getattr(c, name, None)
            if not callable(fn):
                continue
            for kw in (
                {"symbol": sym},
                {"contract_id": cid},
                {"contractId": cid},
                {},
            ):
                try:
                    filtered = _filter_kwargs(fn, _clean_none(dict(kw)))
                    res = await _await_if_needed(_call_maybe(fn, **filtered))

                    if isinstance(res, dict) and "data" in res:
                        res = res["data"]

                    if res is None:
                        return []

                    if isinstance(res, list):
                        out = []
                        for it in res:
                            if isinstance(it, dict):
                                out.append(it)
                            else:
                                out.append(getattr(it, "__dict__", {"raw": str(it)}))
                        return out

                    if isinstance(res, dict):
                        return [res]

                    return [getattr(res, "__dict__", {"raw": str(res)})]
                except Exception as e:
                    last_err = e
                    continue

        # 取れなくても致命ではない
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

    # ---------------------------------------------------------
    # Diagnostics (optional)
    # ---------------------------------------------------------

    def debug_summary(self) -> Dict[str, Any]:
        """
        外から状態を見たいとき用（エンジンは使わなくてもOK）
        """
        return {
            "base_url": self.base_url,
            "contract_id": self.contract_id,
            "symbol": self.symbol,
            "dry_run": self.dry_run,
            "skip_auth": self.skip_auth,
            "strict": self.strict,
            "place_calls": self._place_calls,
            "place_success": self._place_success,
            "last_place_err": self._last_place_err,
            "client_class": getattr(self._client, "__class__", type("x", (), {})).__name__ if self._client else None,
        }
