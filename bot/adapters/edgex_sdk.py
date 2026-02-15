import asyncio
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional, Tuple, List, Dict

from loguru import logger

# edgex-python-sdk
from edgex_sdk import Client as EdgeXClient  # type: ignore
from edgex_sdk import OrderSide as SdkOrderSide  # type: ignore
from edgex_sdk import GetActiveOrderParams, CancelOrderParams  # type: ignore
from edgex_sdk import GetOrderBookDepthParams  # type: ignore


def _dig(obj: Any, keys: List[str]) -> Any:
    """Safely dig nested dict keys; returns None if missing."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _to_str(x: Any) -> Optional[str]:
    try:
        if x is None:
            return None
        s = str(x)
        return s if s else None
    except Exception:
        return None


@dataclass
class OrderResult:
    """grid_engine が期待する最低限: order.id を持つ"""
    id: str


class EdgeXSDKAdapter:
    """
    Real EdgeX adapter using edgex-python-sdk.

    grid_engine が呼ぶメソッド：
      - connect()
      - close()
      - get_ticker(symbol) -> object with .price
      - get_best_bid_ask(symbol) -> (bid, ask)
      - place_order(OrderRequest) -> object with .id
      - list_active_orders(symbol) -> list[dict]
      - cancel_order(order_id)

    重要:
      - このファイルは "SIM/DUMMY" ではなく、実際に発注します。
      - 事故りたくない場合は Render 側で Suspend 運用（あなたの方針）で止めてください。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        account_id: Optional[int] = None,
        stark_private_key: Optional[str] = None,
        op_spacing_sec: float = 0.35,
        **kwargs: Any,
    ):
        self.base_url = base_url or os.getenv("EDGEX_BASE_URL") or "https://pro.edgex.exchange"
        self.account_id = int(account_id) if account_id is not None else int(os.getenv("EDGEX_ACCOUNT_ID", "0") or "0")
        self.stark_private_key = stark_private_key or os.getenv("EDGEX_STARK_PRIVATE_KEY") or os.getenv("EDGEX_L2_KEY") or ""

        self.op_spacing_sec = max(0.15, float(op_spacing_sec))
        self._last_op_ts = 0.0

        self.client: Optional[EdgeXClient] = None
        self._kwargs = kwargs

    async def connect(self, *args: Any, **kwargs: Any) -> bool:
        if not self.stark_private_key:
            raise RuntimeError("EDGEX_STARK_PRIVATE_KEY (or EDGEX_L2_KEY) is missing")
        if not self.account_id:
            raise RuntimeError("EDGEX_ACCOUNT_ID is missing")

        self.client = EdgeXClient(
            base_url=self.base_url,
            account_id=self.account_id,
            stark_private_key=self.stark_private_key,
        )
        logger.info("EdgeXSDKAdapter connected: base_url={} account_id={}", self.base_url, self.account_id)
        return True

    async def close(self, *args: Any, **kwargs: Any) -> bool:
        # SDK側が明示close不要でもここは互換のため残す
        self.client = None
        logger.info("EdgeXSDKAdapter closed")
        return True

    def _rate_limit_sleep(self) -> None:
        now = time.time()
        dt = now - self._last_op_ts
        if dt < self.op_spacing_sec:
            time.sleep(self.op_spacing_sec - dt)
        self._last_op_ts = time.time()

    def _require_client(self) -> EdgeXClient:
        if self.client is None:
            raise RuntimeError("Adapter is not connected. Call connect() first.")
        return self.client

    async def get_ticker(self, symbol: str, *args: Any, **kwargs: Any) -> Any:
        """
        grid_engine は ticker.price を参照するので price 属性を必ず持たせる。
        symbol は contract_id の文字列が想定（例: '10000001' など）
        """
        client = self._require_client()
        contract_id = str(symbol)

        self._rate_limit_sleep()
        try:
            res = await client.get_24_hour_quote(contract_id)
        except Exception as e:
            logger.warning("get_24_hour_quote failed: contract_id={} err={}", contract_id, e)
            raise

        # 価格候補をいろいろ拾う（APIの揺れに強く）
        candidates = [
            _dig(res, ["data", "lastPrice"]),
            _dig(res, ["data", "last"]),
            _dig(res, ["data", "price"]),
            _dig(res, ["data", "markPrice"]),
            _dig(res, ["data", "indexPrice"]),
            _dig(res, ["lastPrice"]),
            _dig(res, ["price"]),
        ]
        price = None
        for c in candidates:
            price = _to_float(c)
            if price is not None:
                break

        if price is None:
            raise RuntimeError(f"ticker price not found in response: {res}")

        logger.debug("ticker: contract_id={} price={}", contract_id, price)
        return SimpleNamespace(price=price, raw=res)

    async def get_best_bid_ask(self, symbol: str, *args: Any, **kwargs: Any) -> Tuple[Optional[float], Optional[float]]:
        """
        orderbook から best bid/ask を取る。
        """
        client = self._require_client()
        contract_id = str(symbol)

        self._rate_limit_sleep()
        try:
            params = GetOrderBookDepthParams(contract_id=contract_id, limit=5)
            depth = await client.quote.get_order_book_depth(params)
        except Exception as e:
            logger.debug("get_order_book_depth failed: contract_id={} err={}", contract_id, e)
            return None, None

        # depth の形が揺れても拾えるように頑張る
        data = _dig(depth, ["data"]) if isinstance(depth, dict) else None
        book = data if isinstance(data, dict) else (depth if isinstance(depth, dict) else {})

        bids = book.get("bids") or book.get("bid") or book.get("buy") or []
        asks = book.get("asks") or book.get("ask") or book.get("sell") or []

        def _best_px(side: Any) -> Optional[float]:
            if not side:
                return None
            # よくある: [[price, size], ...] または [{"price":..}, ...]
            first = side[0]
            if isinstance(first, (list, tuple)) and len(first) >= 1:
                return _to_float(first[0])
            if isinstance(first, dict):
                return _to_float(first.get("price") or first.get("px") or first.get("0"))
            return None

        bid = _best_px(bids)
        ask = _best_px(asks)

        logger.debug("best bid/ask: contract_id={} bid={} ask={}", contract_id, bid, ask)
        return bid, ask

    async def place_order(self, req: Any, *args: Any, **kwargs: Any) -> Any:
        """
        grid_engine からは OrderRequest が来る想定。
        最低限 req.side / req.price / req.quantity(or size) が取れればOK。
        """
        client = self._require_client()

        # contract_id は req.symbol を優先（なければ ENV / 引数へ）
        contract_id = _to_str(getattr(req, "symbol", None)) or _to_str(getattr(req, "contract_id", None)) or _to_str(kwargs.get("symbol"))
        if not contract_id:
            # grid_engine は self.symbol を持っているが、ここに来ない場合があるので最後にENV
            contract_id = os.getenv("EDGEX_CONTRACT_ID") or ""

        contract_id = str(contract_id)

        side_raw = getattr(req, "side", None) or kwargs.get("side")
        price_raw = getattr(req, "price", None) or kwargs.get("price")
        qty_raw = (
            getattr(req, "quantity", None)
            or getattr(req, "size", None)
            or getattr(req, "qty", None)
            or kwargs.get("quantity")
            or kwargs.get("size")
            or kwargs.get("qty")
        )

        side_str = str(side_raw).upper()
        price = _to_float(price_raw)
        size = _to_float(qty_raw)

        if side_str not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {side_raw}")
        if price is None or size is None:
            raise ValueError(f"Invalid price/size: price={price_raw} size={qty_raw}")

        # 最小ロット（あなたの確認: XAUTは0.02）を下回る注文は出さない
        min_size = _to_float(os.getenv("EDGEX_MIN_SIZE"))  # 任意: 環境で上書きできるように
        if min_size is None:
            # 既定: 0.0（ただしあなたは Render で 0.02 を入れてる前提）
            min_size = 0.0
        if size < float(min_size):
            raise ValueError(f"Order size below min_size: size={size} min_size={min_size}")

        sdk_side = SdkOrderSide.BUY if side_str == "BUY" else SdkOrderSide.SELL

        self._rate_limit_sleep()
        logger.info("placing order: contract_id={} side={} price={} size={}", contract_id, side_str, price, size)

        # SDKは文字列を要求することが多いので str で渡す
        res = await client.create_limit_order(
            contract_id=str(contract_id),
            size=str(size),
            price=str(price),
            side=sdk_side,
        )

        # order_id の取り方が揺れても拾えるようにする
        order_id = (
            _to_str(_dig(res, ["data", "orderId"]))
            or _to_str(_dig(res, ["data", "id"]))
            or _to_str(_dig(res, ["orderId"]))
            or _to_str(_dig(res, ["id"]))
        )
        if not order_id:
            raise RuntimeError(f"Order created but orderId not found: {res}")

        logger.info("order created: id={} (contract_id={})", order_id, contract_id)
        # grid_engine は .id 属性を参照するので必ず持つオブジェクトを返す
        return SimpleNamespace(id=str(order_id), raw=res)

    async def list_active_orders(self, symbol: str, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        """
        grid_engine の _sync_active_orders_from_exchange は list[dict] を期待。
        """
        client = self._require_client()
        contract_id = str(symbol)

        self._rate_limit_sleep()
        params = GetActiveOrderParams(size="100", offset_data="")
        try:
            res = await client.get_active_orders(params)
        except Exception as e:
            logger.debug("get_active_orders failed: {}", e)
            return []

        # res['data']['orderList'] っぽい想定だが、揺れ対応
        order_list = (
            _dig(res, ["data", "orderList"])
            or _dig(res, ["data", "orders"])
            or _dig(res, ["orderList"])
            or _dig(res, ["orders"])
            or []
        )
        if not isinstance(order_list, list):
            return []

        # contractIdで絞りたいが、API側が契約IDフィルタを要求しない/しにくい場合があるためここで絞る
        out: List[Dict[str, Any]] = []
        for row in order_list:
            if not isinstance(row, dict):
                continue
            cid = _to_str(row.get("contractId") or row.get("contract_id") or row.get("symbol"))
            if cid and cid != contract_id:
                # 他銘柄が混ざるのを避ける
                continue
            out.append(row)

        return out

    async def cancel_order(self, order_id: str, *args: Any, **kwargs: Any) -> Any:
        client = self._require_client()
        oid = str(order_id)

        self._rate_limit_sleep()
        try:
            params = CancelOrderParams(order_id=oid)
            res = await client.cancel_order(params)
            logger.info("order cancelled: id={}", oid)
            return res
        except Exception as e:
            logger.warning("cancel_order failed: id={} err={}", oid, e)
            raise

    async def cancel_all_orders(self, symbol: Optional[str] = None, *args: Any, **kwargs: Any) -> Any:
        """
        互換用。grid_engine は基本 cancel_order を使うが、保険で残す。
        """
        active = await self.list_active_orders(symbol or (os.getenv("EDGEX_CONTRACT_ID") or ""))
        ok = 0
        for row in active:
            oid = row.get("orderId") or row.get("id")
            if not oid:
                continue
            try:
                await self.cancel_order(str(oid))
                ok += 1
            except Exception:
                pass
            await asyncio.sleep(self.op_spacing_sec)
        return {"status": "ok", "cancelled": ok}
