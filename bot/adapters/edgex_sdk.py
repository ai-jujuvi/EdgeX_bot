import os
import time
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)


def _env_str(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v != "" else default


def _env_float(name: str, default: float | None = None) -> float | None:
    s = _env_str(name, None)
    if s is None:
        return default
    try:
        return float(s)
    except Exception:
        return default


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    contract_id: str
    side: str
    price: float
    size: float

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "contract_id": self.contract_id,
            "side": self.side,
            "price": float(self.price),
            "size": float(self.size),
        }


class EdgeXAdapter:
    """
    Adapter must match grid_engine expectations.

    Your ops rule:
    - NO DRY_RUN in code.
    - Stop/start controlled by Render Suspend.
    - Safety during testing is achieved via EDGEX_MAKER_MODE=validate.
    """

    def __init__(
        self,
        base_url: str,
        account_id: str,
        stark_private_key: str,
        contract_id: str,
        symbol: str,
        op_spacing_sec: float = 1.5,
        **_kwargs,
    ):
        self.base_url = str(base_url)
        self.account_id = str(account_id)
        self.stark_private_key = str(stark_private_key)
        self.contract_id = str(contract_id)
        self.symbol = str(symbol)

        # spacing (avoid bursts)
        env_spacing = _env_float("EDGEX_ADAPTER_OP_SPACING_SEC", None)
        self.op_spacing_sec = max(0.2, float(env_spacing if env_spacing is not None else op_spacing_sec))
        self._last_op_ts = 0.0

        # validate/live
        self.maker_mode = (_env_str("EDGEX_MAKER_MODE", "validate") or "validate").strip().lower()

        # Optional rails
        self.min_size = _env_float("EDGEX_MIN_ORDER_SIZE", None)
        self.max_size = _env_float("EDGEX_MAX_ORDER_SIZE", None)
        self.size_step = _env_float("EDGEX_SIZE_STEP", None)
        self.price_tick = _env_float("EDGEX_PRICE_TICK", None)

        # Temporary dummy ticker (until real API is wired)
        self.dummy_ticker_price = float(_env_float("EDGEX_DUMMY_TICKER_PRICE", 2000.0) or 2000.0)

        log.info(
            "Adapter init: base_url=%s symbol=%s contract_id=%s maker_mode=%s "
            "min_size=%s max_size=%s size_step=%s price_tick=%s op_spacing_sec=%.2f dummy_ticker_price=%s",
            self.base_url,
            self.symbol,
            self.contract_id,
            self.maker_mode,
            self.min_size,
            self.max_size,
            self.size_step,
            self.price_tick,
            self.op_spacing_sec,
            self.dummy_ticker_price,
        )

    # -------------------------
    # grid_engine compatibility
    # -------------------------
    async def connect(self) -> None:
        log.info("Adapter connect(): OK (no-op).")

    async def close(self) -> None:
        log.info("Adapter close(): OK (no-op).")

    async def get_ticker(self, *args, **kwargs):
        """
        grid_engine expects an object with .price
        """
        p = float(self.dummy_ticker_price)
        log.warning(
            "get_ticker(): returning DUMMY ticker price=%s (args=%s kwargs=%s). "
            "Set EDGEX_DUMMY_TICKER_PRICE to change.",
            p,
            args,
            kwargs,
        )
        return SimpleNamespace(price=p, bid=p, ask=p, last=p)

    def get_mid_price(self) -> float | None:
        return float(self.dummy_ticker_price)

    def list_active_orders(self, *args, **kwargs):
        """
        grid_engine may call this to check existing orders.
        In validate mode (and for now), we return empty list.
        """
        if args or kwargs:
            log.debug("list_active_orders(): args=%s kwargs=%s (returning empty)", args, kwargs)
        return []

    async def list_active_orders_async(self, *args, **kwargs):
        return self.list_active_orders(*args, **kwargs)

    def cancel_order(self, *args, **kwargs) -> dict:
        """
        Optional cancel path.
        """
        if self.maker_mode in ("validate", "paper", "dry_run"):
            log.info("[VALIDATE_MODE] would cancel order: args=%s kwargs=%s", args, kwargs)
            return {"ok": True, "mode": "validate", "action": "cancel"}
        log.warning("cancel_order(): live cancel not implemented. args=%s kwargs=%s", args, kwargs)
        return {"ok": False, "reason": "cancel_not_implemented"}

    async def cancel_order_async(self, *args, **kwargs) -> dict:
        return self.cancel_order(*args, **kwargs)

    # -------------------------
    # internals
    # -------------------------
    def _rate_limit_sleep(self) -> None:
        now = time.time()
        dt = now - self._last_op_ts
        if dt < self.op_spacing_sec:
            time.sleep(self.op_spacing_sec - dt)
        self._last_op_ts = time.time()

    def _round_floor(self, value: float, step: float) -> float:
        if step <= 0:
            return float(value)
        n = int(value / step)  # floor
        return float(n * step)

    def _extract_side_price_size(self, *args, **kwargs) -> tuple[str, float, float]:
        """
        Accept both call styles:
        - place_order(side, price, size)
        - place_order(order_obj)  (order_obj may have side/price/size or fields inside)
        """
        # style A: explicit args
        if len(args) >= 3:
            side = args[0]
            price = args[1]
            size = args[2]
            return str(side), float(price), float(size)

        # style B: single order object
        if len(args) == 1 and not kwargs:
            o = args[0]

            # Some engines pass enum-like side (OrderSide.BUY). Make it string.
            def _as_str(x: Any) -> str:
                if hasattr(x, "name"):
                    return str(x.name)
                return str(x)

            # Try common shapes
            if hasattr(o, "side") and hasattr(o, "price") and hasattr(o, "size"):
                return _as_str(getattr(o, "side")), float(getattr(o, "price")), float(getattr(o, "size"))

            # Some pass dict-like
            if isinstance(o, dict):
                return _as_str(o.get("side")), float(o.get("price")), float(o.get("size"))

            # Some pass order with nested fields
            for side_key in ("side", "order_side"):
                for price_key in ("price", "limit_price"):
                    for size_key in ("size", "qty", "amount"):
                        if hasattr(o, side_key) and hasattr(o, price_key) and hasattr(o, size_key):
                            return _as_str(getattr(o, side_key)), float(getattr(o, price_key)), float(getattr(o, size_key))

        # style C: kwargs
        side = kwargs.get("side", None)
        price = kwargs.get("price", None)
        size = kwargs.get("size", None)
        if side is not None and price is not None and size is not None:
            return str(side), float(price), float(size)

        raise TypeError("place_order() could not extract (side, price, size) from given args/kwargs")

    def _normalize_intent(self, side: str, price: float, size: float) -> OrderIntent | None:
        # side normalize
        side_u = str(side).upper().strip()
        # Handle enum-ish strings like "OrderSide.BUY"
        if "." in side_u:
            side_u = side_u.split(".")[-1].strip()

        if side_u not in ("BUY", "SELL"):
            log.error("Invalid side=%s (must be BUY/SELL). BLOCK.", side)
            return None

        p = float(price)
        s = float(size)

        if p <= 0:
            log.error("Invalid price<=0 price=%s BLOCK.", p)
            return None
        if s <= 0:
            log.error("Invalid size<=0 size=%s BLOCK.", s)
            return None

        # Optional rounding
        if self.price_tick is not None and self.price_tick > 0:
            p2 = self._round_floor(p, self.price_tick)
            if p2 <= 0:
                log.error("Price rounding resulted <=0. price=%s tick=%s BLOCK.", p, self.price_tick)
                return None
            if p2 != p:
                log.info("Price rounded: %s -> %s (tick=%s)", p, p2, self.price_tick)
            p = p2

        if self.size_step is not None and self.size_step > 0:
            s2 = self._round_floor(s, self.size_step)
            if s2 <= 0:
                log.error("Size rounding resulted <=0. size=%s step=%s BLOCK.", s, self.size_step)
                return None
            if s2 != s:
                log.info("Size rounded: %s -> %s (step=%s)", s, s2, self.size_step)
            s = s2

        if self.min_size is not None and s < self.min_size:
            log.error("Size below min. size=%s min_size=%s BLOCK.", s, self.min_size)
            return None

        if self.max_size is not None and s > self.max_size:
            log.error("Size above max. size=%s max_size=%s BLOCK.", s, self.max_size)
            return None

        return OrderIntent(
            symbol=self.symbol,
            contract_id=self.contract_id,
            side=side_u,
            price=p,
            size=s,
        )

    # -------------------------
    # order entry points (MUST exist)
    # -------------------------
    def place_order(self, *args, **kwargs) -> dict:
        """
        grid_engine calls this in different ways. We accept all common call styles.
