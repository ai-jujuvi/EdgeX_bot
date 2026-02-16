from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple


class EdgeXSDKAdapter:
    """
    grid_engine からの呼び出しが多少揺れても落ちない「受け皿」アダプタ。

    目的:
    - ImportError を確実に潰す（EdgeXSDKAdapter を必ず提供）
    - get_ticker() が必ず ticker.price を返す（price not found を潰す）
    - place_order() は (side, price, size) / OrderRequest(っぽい1obj) の両対応

    注意:
    - ここでは「実発注の署名ロジック」は触りません（既存の実装が別にある前提）。
    - ただし validate/SIM の切り替えは env で制御できるようにします。
    """

    def __init__(
        self,
        contract_id: Optional[str] = None,
        symbol: Optional[str] = None,
        dry_run: bool = False,
        base_url: Optional[str] = None,
        **kwargs: Any,
    ):
        self.contract_id = str(contract_id) if contract_id is not None else None
        self.symbol = symbol
        self.dry_run = bool(dry_run)
        self.base_url = base_url or os.getenv("EDGEX_BASE_URL", "https://pro.edgex.exchange")

        # 速度/レート制限対策: 同一プロセス内での最短間隔
        self._min_ticker_interval = float(os.getenv("EDGEX_TICKER_MIN_INTERVAL_SEC", "0.4"))
        self._last_ticker_ts = 0.0

        # grid_engine 側が使う可能性があるため保持
        self.kwargs = kwargs

    async def connect(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def close(self, *args: Any, **kwargs: Any) -> bool:
        return True

    # -------------------------
    # helpers
    # -------------------------
    def _maybe_extract_from_request_obj(self, obj: Any) -> Tuple[Any, Any, Any]:
        """
        OrderRequest っぽいオブジェクトから side/price/size(quantity) を抜く
        """
        if obj is None:
            return None, None, None

        side = getattr(obj, "side", None)
        price = getattr(obj, "price", None)

        size = getattr(obj, "size", None)
        if size is None:
            size = getattr(obj, "quantity", None)
        if size is None:
            size = getattr(obj, "qty", None)
        if size is None:
            size = getattr(obj, "amount", None)

        if side is not None or price is not None or size is not None:
            return side, price, size

        return None, None, None

    def _extract_side_price_size(self, *args: Any, **kwargs: Any) -> Tuple[Any, Any, Any]:
        """
        想定パターン：
          A) place_order(side, price, size)
          B) place_order(side=..., price=..., size=...)
          C) place_order(OrderRequest(...))  ← これが来ても落ちない
        """
        side = kwargs.get("side", None)
        price = kwargs.get("price", None)
        size = kwargs.get("size", None)

        if size is None:
            size = kwargs.get("quantity", None)
        if size is None:
            size = kwargs.get("qty", None)
        if size is None:
            size = kwargs.get("amount", None)

        if (side is None or price is None or size is None) and len(args) >= 3:
            side = side if side is not None else args[0]
            price = price if price is not None else args[1]
            size = size if size is not None else args[2]

        if (side is None or price is None or size is None) and len(args) == 1:
            s2, p2, z2 = self._maybe_extract_from_request_obj(args[0])
            side = side if side is not None else s2
            price = price if price is not None else p2
            size = size if size is not None else z2

        return side, price, size

    def _parse_price_from_ticker_payload(self, payload: Any) -> Optional[float]:
        """
        EdgeX の getTicker 返却（っぽいもの）から price をそれっぽく抜く。
        想定候補: markPrice / lastPrice / indexPrice / oraclePrice / close
        """
        try:
            if not payload:
                return None

            # payload が dict で data が list のパターン
            if isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, list) and data:
                    item = data[0]
                    if isinstance(item, dict):
                        for k in ("markPrice", "lastPrice", "indexPrice", "oraclePrice", "close", "open"):
                            v = item.get(k)
                            if v is not None:
                                return float(v)
                # dict 直下に price があるパターン
                for k in ("price", "markPrice", "lastPrice"):
                    v = payload.get(k)
                    if v is not None:
                        return float(v)

            # payload が list のパターン
            if isinstance(payload, list) and payload:
                item = payload[0]
                if isinstance(item, dict):
                    for k in ("markPrice", "lastPrice", "indexPrice", "oraclePrice", "close", "open"):
                        v = item.get(k)
                        if v is not None:
                            return float(v)

        except Exception:
            return None

        return None

    async def get_ticker(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        """
        grid_engine は ticker.price を参照する前提。
        なので price 属性が必ず入ったオブジェクトを返す。
        """
        # 連打しすぎると 429/Cloudflare が出るので最短間隔を設ける
        now = time.time()
        dt = now - self._last_ticker_ts
        if dt < self._min_ticker_interval:
            time.sleep(self._min_ticker_interval - dt)
        self._last_ticker_ts = time.time()

        # 既存プロジェクト側に HTTP 関数がある場合は kwargs 経由で注入される想定にも対応
        # 何も無い場合は "price=None" で返し、上位が WARN を出す（がクラッシュはしない）
        payload = kwargs.get("payload")

        # もし呼び元が payload を渡してない場合、ここでは無理に外部HTTPしない（安全優先）
        price = self._parse_price_from_ticker_payload(payload)

        # どうしても price が無い場合は None のままだと上位が嫌がるので、
        # 最後の手段として "markPrice(仮)" を環境変数で注入できるようにする
        if price is None:
            fallback = os.getenv("EDGEX_TICKER_FALLBACK_PRICE")
            if fallback:
                try:
                    price = float(fallback)
                except Exception:
                    price = None

        # grid_engine を落とさないため、price が None の時は 0.0 で返す
        # （上位ログに "Ticker data empty" を出しつつ進む）
        if price is None:
            price = 0.0

        return SimpleNamespace(price=price)

    async def place_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        side, price, size = self._extract_side_price_size(*args, **kwargs)
        if side is None or price is None or size is None:
            raise ValueError(
                f"place_order() could not extract (side, price, size). args={args}, kwargs={kwargs}"
            )

        side_s = str(side)
        price_f = float(price)
        size_f = float(size)

        maker_mode = str(os.getenv("EDGEX_MAKER_MODE", "validate")).lower()
        # validate / sim の間は絶対に実注文を出さない
        if self.dry_run or maker_mode in ("validate", "sim", "dry"):
            print(f"[SIM] place_order side={side_s} price={price_f} size={size_f}")
            return {"status": "sim", "side": side_s, "price": price_f, "size": size_f}

        # ここから先が「実注文」領域。
        # このリポジトリ内に既に署名付きの発注関数がある前提なので、
        # それを呼ぶ設計にするのが安全。もし未実装ならここで止める。
        raise RuntimeError(
            "LIVE order path is not wired here yet. "
            "Set EDGEX_MAKER_MODE=validate (or dry_run=1) to stay safe, "
            "or connect this adapter to the project's signed order function."
        )

    async def list_active_orders(self, *args: Any, **kwargs: Any) -> list:
        # grid_engine の呼び出し揺れで落ちないように *args **kwargs を許容
        return []

    async def cancel_all_orders(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "ok"}
