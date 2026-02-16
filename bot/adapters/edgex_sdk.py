"""
EdgeX SDK Adapter

- SDKのバージョン差分（Client.__init__ / create_limit_order / get_active_orders の引数違い）
  を吸収して「落ちずに動く」ことを最優先にした受け皿アダプタ。
- GridEngine からは ExchangeAdapter インターフェースだけを使う。
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

from bot.adapters.base import ExchangeAdapter
from bot.models.types import OrderRequest, OrderSide


# ----------------------------
# small helpers
# ----------------------------

def _env(*keys: str, default: str | None = None) -> str | None:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _filter_kwargs(callable_obj, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """その関数が受け取れる kwargs だけに絞る（SDK差分吸収用）"""
    try:
        sig = inspect.signature(callable_obj)
        params = sig.parameters
        allow = set(params.keys())
        # **kwargs を受けるなら全部OK
        for p in params.values():
            if p.kind == inspect.Parameter.VAR_KEYWORD:
                return kwargs
        return {k: v for k, v in kwargs.items() if k in allow}
    except Exception:
        return kwargs


def _call_maybe(callable_obj, *args, **kwargs):
    """kwargsをフィルタして呼ぶ。TypeErrorならそのまま投げる。"""
    fkw = _filter_kwargs(callable_obj, kwargs)
    return callable_obj(*args, **fkw)


@dataclass
class _Ticker:
    price: float


@dataclass
class _OrderResult:
    id: str


# ----------------------------
# Adapter
# ----------------------------

class EdgeXSDKAdapter(ExchangeAdapter):
    """
    GridEngine から呼ばれる ExchangeAdapter 実装

    必要メソッド（GridEngine側で使っている）:
      - connect / close
      - get_ticker
      - get_best_bid_ask (任意)
      - place_order
      - cancel_order
      - list_active_orders
      - fetch_positions (任意)
    """

    def __init__(
        self,
        contract_id: str | None = None,
        symbol: str | None = None,
        dry_run: bool = False,
        base_url: str | None = None,
        **_kwargs,
    ) -> None:
        self.base_url = base_url or _env("EDGEX_BASE_URL", default="https://pro.edgex.exchange")
        self.contract_id = str(contract_id) if contract_id is not None else _env("EDGEX_CONTRACT_ID", "EDGEX_SYMBOL", default=None)
        self.symbol = str(symbol) if symbol is not None else self.contract_id  # GridEngineは文字列symbolを渡してくる
        self.dry_run = bool(dry_run)

        # 認証系（古いSDK/新しいSDK どっちでも拾えるように多めに受ける）
        self.api_key = _env("EDGEX_API_KEY", "EDGEX_KEY", default=None)
        self.api_secret = _env("EDGEX_API_SECRET", "EDGEX_SECRET", default=None)
        self.passphrase = _env("EDGEX_PASSPHRASE", default=None)

        # 新SDKで要求されがちなやつ
        self.account_id = _env("EDGEX_ACCOUNT_ID", "EDGEX_ACCOUNTID", "EDGEX_ACCOUNT", default=None)
        self.stark_private_key = _env("EDGEX_STARK_PRIVATE_KEY", "EDGEX_STARK_KEY", default=None)

        self.skip_auth = _as_bool(_env("EDGEX_SKIP_AUTH", default="0"), default=False)

        self._sdk = None
        self._client = None

        logger.info(
            "edgex adapter init: base_url={} contract_id={} symbol={} dry_run={}",
            self.base_url, self.contract_id, self.symbol, self.dry_run
        )

    async def connect(self) -> None:
        """
        SDK import & Client init.
        ここが落ちると bot が起動しないので、できるだけ情報を残して落とす。
        """
        if self._client is not None:
            return

        # 遅延import
        try:
            import edgex_sdk  # type: ignore
            self._sdk = edgex_sdk
            logger.info("edgex sdk module loaded: {}", getattr(edgex_sdk, "__name__", "edgex_sdk"))
        except Exception as e:
            raise RuntimeError(f"Failed to import edgex_sdk: {e}")

        # Client class
        Client = getattr(self._sdk, "Client", None)
        if Client is None:
            Client = getattr(self._sdk, "SDKClient", None)
        if Client is None:
            raise RuntimeError("edgex_sdk has no Client/SDKClient")

        # Client.__init__ の引数差分を吸収して初期化する
        last_err: Exception | None = None

        # まず signature を見て「渡せるものだけ渡す」方式
        try:
            init_sig = inspect.signature(Client)
            params = init_sig.parameters
            kwargs: Dict[str, Any] = {}

            # よくある名前を全部候補として入れ、filterで削る
            kwargs.update({
                "base_url": self.base_url,
                "api_key": self.api_key,
                "api_secret": self.api_secret,
                "passphrase": self.passphrase,
                "key": self.api_key,
                "secret": self.api_secret,
                "account_id": self.account_id,
                "accountId": self.account_id,
                "stark_private_key": self.stark_private_key,
                "starkPrivateKey": self.stark_private_key,
                "credentials": {
                    "api_key": self.api_key,
                    "api_secret": self.api_secret,
                    "passphrase": self.passphrase,
                    "account_id": self.account_id,
                    "stark_private_key": self.stark_private_key,
                },
            })

            # 必須っぽい positional を埋める（account_id, stark_private_key など）
            # ここは SDK により異なるので、signatureのpositional-only/positional-or-keywordで
            # default無しのものだけ順に埋める。
            positional_args: List[Any] = []
            for name, p in params.items():
                if name in ("self",):
                    continue
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
                    if p.default is inspect._empty:
                        # 必須 positional
                        if name in ("base_url",):
                            positional_args.append(self.base_url)
                        elif name in ("account_id", "accountId"):
                            positional_args.append(self.account_id)
                        elif name in ("stark_private_key", "starkPrivateKey"):
                            positional_args.append(self.stark_private_key)
                        elif name in ("api_key", "key"):
                            positional_args.append(self.api_key)
                        elif name in ("api_secret", "secret"):
                            positional_args.append(self.api_secret)
                        elif name in ("passphrase",):
                            positional_args.append(self.passphrase)
                        else:
                            positional_args.append(None)

            # Noneが混ざってて必須を満たしてなさそうなら、わかりやすく落とす
            if any(x is None for x in positional_args):
                # account_id/stark_private_key が必須っぽいのに無いケースを明示
                if self.account_id is None or self.stark_private_key is None:
                    raise RuntimeError(
                        "EDGEX_ACCOUNT_ID / EDGEX_STARK_PRIVATE_KEY が未設定の可能性が高いです"
                    )

            # フィルタして作る
            fkwargs = _filter_kwargs(Client, kwargs)

            self._client = Client(*positional_args, **fkwargs)
            return

        except Exception as e:
            last_err = e

        # フォールバック：よくあるパターンを順に試す（ログに出てる候補）
        tried: List[str] = []
        patterns: List[Dict[str, Any]] = [
            {"base_url": self.base_url, "api_key": self.api_key, "api_secret": self.api_secret, "passphrase": self.passphrase, "account_id": self.account_id},
            {"base_url": self.base_url, "key": self.api_key, "secret": self.api_secret, "passphrase": self.passphrase, "accountId": self.account_id},
            {"base_url": self.base_url, "credentials": {"api_key": self.api_key, "api_secret": self.api_secret, "passphrase": self.passphrase, "account_id": self.account_id}},
            {"base_url": self.base_url, "account_id": self.account_id, "stark_private_key": self.stark_private_key},
        ]

        for kw in patterns:
            try:
                tried.append(str(list(kw.keys())))
                fkw = _filter_kwargs(Client, kw)
                self._client = Client(**fkw)
                return
            except Exception as e:
                last_err = e
                continue

        msg = f"Failed to init edgex SDK client. tried={tried} last_err={last_err}"
        raise RuntimeError(msg)

    async def close(self) -> None:
        self._client = None

    # ----------------------------
    # market data
    # ----------------------------

    async def get_ticker(self, symbol: str) -> _Ticker:
        c = self._require_client()
        # どのSDKでも「ticker」系は揺れやすいので候補を順に当てる
        methods = [
            ("get_ticker", {"symbol": symbol}),
            ("ticker", {"symbol": symbol}),
            ("get_mark_price", {"symbol": symbol}),
            ("get_last_price", {"symbol": symbol}),
        ]
        last_err = None
        for name, kw in methods:
            fn = getattr(c, name, None)
            if not callable(fn):
                continue
            try:
                res = _call_maybe(fn, **kw)
                # res が dict / obj / number どれでも吸う
                if isinstance(res, (int, float)):
                    return _Ticker(price=float(res))
                if isinstance(res, dict):
                    p = res.get("price") or res.get("last") or res.get("lastPrice") or res.get("markPrice")
                    return _Ticker(price=float(p))
                p = getattr(res, "price", None) or getattr(res, "last", None) or getattr(res, "lastPrice", None)
                return _Ticker(price=float(p))
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"get_ticker failed: {last_err}")

    async def get_best_bid_ask(self, symbol: str):
        c = self._require_client()
        fn = getattr(c, "get_best_bid_ask", None) or getattr(c, "best_bid_ask", None) or getattr(c, "get_orderbook", None)
        if not callable(fn):
            return (None, None)
        try:
            res = _call_maybe(fn, symbol=symbol)
            # orderbook 形式なら best を抜く
            if isinstance(res, dict):
                bid = res.get("bestBid") or res.get("bid")
                ask = res.get("bestAsk") or res.get("ask")
                return (float(bid) if bid is not None else None, float(ask) if ask is not None else None)
        except Exception:
            pass
        return (None, None)

    # ----------------------------
    # trading
    # ----------------------------

    async def place_order(self, req: OrderRequest) -> _OrderResult:
        """
        GridEngineは OrderRequest(quantity/price/time_in_force) を渡してくる。
        SDK側は create_limit_order / place_order などが揺れるので吸収する。
        """
        if self.dry_run:
            oid = f"dry_{req.side}_{req.price}"
            return _OrderResult(id=oid)

        c = self._require_client()

        side = str(req.side.name if hasattr(req.side, "name") else req.side).upper()
        if side not in ("BUY", "SELL"):
            side = "BUY" if "BUY" in side else "SELL"

        # post_only / time_in_force の差分を吸収
        desired_post_only = True

        # よくある候補関数
        candidates = [
            "create_limit_order",
            "place_limit_order",
            "create_order",
            "place_order",
        ]

        last_err = None
        for name in candidates:
            fn = getattr(c, name, None)
            if not callable(fn):
                continue
            try:
                # まず kwargs で素直に
                kwargs = {
                    "symbol": req.symbol,
                    "contract_id": req.symbol,  # symbol_param が contractId のケース
                    "side": side,
                    "price": float(req.price),
                    "size": float(req.quantity),
                    "quantity": float(req.quantity),
                    "post_only": desired_post_only,
                    "postOnly": desired_post_only,
                    "time_in_force": "POST_ONLY",
                    "tif": "POST_ONLY",
                }
                res = _call_maybe(fn, **kwargs)

                oid = None
                if isinstance(res, dict):
                    oid = res.get("orderId") or res.get("id") or res.get("order_id")
                else:
                    oid = getattr(res, "id", None) or getattr(res, "orderId", None)
                if oid is None:
                    oid = str(res)
                return _OrderResult(id=str(oid))

            except TypeError as e:
                # kwargs が合わないSDKの場合、positionalで試す（price/size/side順など）
                last_err = e
                try:
                    # 代表的な並びを2つ試す
                    # (symbol, side, price, size)
                    res = _call_maybe(fn, req.symbol, side, float(req.price), float(req.quantity))
                    oid = getattr(res, "id", None) or getattr(res, "orderId", None) or (res.get("id") if isinstance(res, dict) else None)
                    return _OrderResult(id=str(oid or res))
                except Exception as e2:
                    last_err = e2
                    continue
            except Exception as e:
                last_err = e
                continue

        raise RuntimeError(f"place_order failed: {last_err}")

    async def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            return
        c = self._require_client()
        fn = getattr(c, "cancel_order", None) or getattr(c, "cancel", None)
        if not callable(fn):
            raise RuntimeError("SDK client has no cancel_order/cancel")
        _call_maybe(fn, order_id=order_id, id=order_id)

    async def list_active_orders(self, symbol: str) -> List[dict]:
        c = self._require_client()
        fn = getattr(c, "get_active_orders", None) or getattr(c, "list_active_orders", None) or getattr(c, "active_orders", None)
        if not callable(fn):
            return []

        # contract_id を嫌うSDKがあるので「渡せるなら渡す、ダメなら無し」で吸う
        try:
            res = _call_maybe(fn, symbol=symbol, contract_id=symbol, contractId=symbol)
        except TypeError:
            res = fn()

        if res is None:
            return []
        if isinstance(res, list):
            return [r if isinstance(r, dict) else getattr(r, "__dict__", {"raw": str(r)}) for r in res]
        if isinstance(res, dict):
            # {"data":[...]} みたいなのも吸う
            data = res.get("data") or res.get("orders") or res.get("result")
            if isinstance(data, list):
                return [r if isinstance(r, dict) else getattr(r, "__dict__", {"raw": str(r)}) for r in data]
            return [res]
        return [getattr(res, "__dict__", {"raw": str(res)})]

    async def fetch_positions(self, symbol: str) -> List[dict]:
        c = self._require_client()
        fn = getattr(c, "get_positions", None) or getattr(c, "fetch_positions", None)
        if not callable(fn):
            return []
        try:
            res = _call_maybe(fn, symbol=symbol, contract_id=symbol, contractId=symbol)
        except TypeError:
            res = fn()
        if res is None:
            return []
        if isinstance(res, list):
            return [r if isinstance(r, dict) else getattr(r, "__dict__", {"raw": str(r)}) for r in res]
        if isinstance(res, dict):
            data = res.get("data") or res.get("positions") or res.get("result")
            if isinstance(data, list):
                return [r if isinstance(r, dict) else getattr(r, "__dict__", {"raw": str(r)}) for r in data]
            return [res]
        return [getattr(res, "__dict__", {"raw": str(res)})]

    # ----------------------------
    # internal
    # ----------------------------

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("EdgeX client is not initialized. call connect() first.")
        return self._client
