import os
import time
import logging
from dataclasses import dataclass
from types import SimpleNamespace

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


def _env_int(name: str, default: int | None = None) -> int | None:
    s = _env_str(name, None)
    if s is None:
        return default
    try:
        return int(s)
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
    Execution gateway.

    IMPORTANT (per your new ops rule):
    - NO DRY_RUN guard here.
    - Stop/start is controlled by Render Suspend.
    - "validate mode" is supported via EDGEX_MAKER_MODE=validate
      to avoid sending real orders while still keeping the engine stable.
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

        # Rate limit between SDK ops (avoid burst)
        env_spacing = _env_float("EDGEX_ADAPTER_OP_SPACING_SEC", None)
        self.op_spacing_sec = max(0.2, float(env_spacing if env_spacing is not None else op_spacing_sec))
        self._last_op_ts = 0.0

        # Maker mode: validate/live
        self.maker_mode = (_env_str("EDGEX_MAKER_MODE", "validate") or "validate").strip().lower()

        # Optional rounding / validation rails (recommended)
        self.min_size = _env_float("EDGEX_MIN_ORDER_SIZE", None)
        self.max_size = _env_float("EDGEX_MAX_ORDER_SIZE", None)
        self.size_step = _env_float("EDGEX_SIZE_STEP", None)
        self.price_tick = _env_float("EDGEX_PRICE_TICK", None)

        # Optional: dummy ticker price for stability until real ticker is wired
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
    # Compatibility hooks (grid_engine expects these)
    # -------------------------
    async def connect(self) -> None:
        log.info("Adapter connect(): OK (no-op).")

    async def close(self) -> None:
        log.info("Adapter close(): OK (no-op).")

    async def get_ticker(self, *args, **kwargs):
        """
        Engine expectations observed from logs:
        - called with args (e.g., contract_id)
        - return value must have `.price`

        We return a SimpleNamespace with price/bid/ask/last for maximum compatibility.
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

    # Some engines call adapter.get_mid_price()
    def get_mid_price(self) -> float | None:
        return float(self.dummy_ticker_price)

    # -------------------------
    # Helpers
    # -------------------------
    def _rate_limit_sleep(self) -> None:
        now = time.time()
        dt = now - self._last_op_ts
        if dt < self.op_spacing_sec:
            time.sleep(self.op_spacing_sec - dt)
        self._last_op_ts = time.time()

    def _round_to_step_floor(self, value: float, step: float) -> float:
        if step <= 0:
            return float(value)
        n = int(value / step)  # floor
        return float(n * step)

    def _validate_and_normalize(self, side: str, price: float, size: float) -> OrderIntent | None:
        side_u = str(side).upper().strip()
        if side_u not in ("BUY", "SELL"):
            log.error("Invalid side=%s (must be BUY/SELL). BLOCK.", side)
            return None

        try:
            p = float(price)
            s = float(size)
        except Exception:
            log.error("Invalid price/size (not float). price=%r size=%r BLOCK.", price, size)
            return None

        if p <= 0:
            log.error("Invalid price<=0 price=%s BLOCK.", p)
            return None
        if s <= 0:
            log.error("Invalid size<=0 size=%s BLOCK.", s)
            return None

        # Round price/size if rails are provided
        if self.price_tick is not None and self.price_tick > 0:
            p2 = self._round_to_step_floor(p, self.price_tick)
            if p2 <= 0:
                log.error("Price rounding resulted <=0. price=%s tick=%s BLOCK.", p, self.price_tick)
                return None
            if p2 != p:
                log.info("Price rounded: %s -> %s (tick=%s)", p, p2, self.price_tick)
            p = p2

        if self.size_step is not None and self.size_step > 0:
            s2 = self._round_to_step_floor(s, self.size_step)
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
    # Order entry (sync + async compatibility)
    # -------------------------
    def place_limit_order(self, side: str, price: float, size: float) -> dict:
        """
        Sync entry used by some code paths.
        """
        intent = self._validate_and_normalize(side=side, price=price, size=size)
        if intent is None:
            return {"ok": False, "reason": "validation_failed"}

        self._rate_limit_sleep()

        # validate mode: DO NOT place real orders (but also DO NOT crash)
        if self.maker_mode in ("validate", "dry_run", "paper"):
            log.info("[VALIDATE_MODE] would place order: %s", intent.to_dict())
            return {"ok": True, "mode": "validate", "intent": intent.to_dict()}

        # live mode: real call must be implemented in your SDK integration
        return self._place_order_real(intent)

    def place_order(self, side: str, price: float, size: float) -> dict:
        """
        Compatibility alias.
        """
        return self.place_limit_order(side=side, price=price, size=size)

    async def place_order_async(self, side: str, price: float, size: float) -> dict:
        """
        Async entry used by some engines.
        """
        return self.place_limit_order(side=side, price=price, size=size)

    async def cancel_order(self, *args, **kwargs) -> dict:
        """
        Optional: some engines try to cancel stale orders.
        In validate mode we just log and return ok.
        """
        if self.maker_mode in ("validate", "dry_run", "paper"):
            log.info("[VALIDATE_MODE] would cancel order: args=%s kwargs=%s", args, kwargs)
            return {"ok": True, "mode": "validate", "action": "cancel", "args": args, "kwargs": kwargs}

        log.warning("cancel_order(): live mode cancel not implemented. args=%s kwargs=%s", args, kwargs)
        return {"ok": False, "reason": "cancel_not_implemented"}

    def _place_order_real(self, intent: OrderIntent) -> dict:
        """
        REAL order path.
        You can wire your actual EdgeX SDK call here later.
        For now, we raise with an explicit message so it's impossible to "silently" place orders.
        """
        log.critical("LIVE MODE requested but REAL order path is NOT implemented. intent=%s", intent.to_dict())
        raise RuntimeError("LIVE mode requested but EdgeX real order placement is not implemented.")


class EdgeXSDKAdapter(EdgeXAdapter):
    """
    Compatibility wrapper:
    Some code constructs EdgeXSDKAdapter(base_url, account_id, stark_private_key)
    and expects env vars for contract/symbol.
    """

    def __init__(self, base_url: str, account_id: str, stark_private_key: str, *args, **kwargs):
        contract_id = kwargs.pop("contract_id", None) or _env_str("EDGEX_CONTRACT_ID", None)
        symbol = kwargs.pop("symbol", None) or _env_str("EDGEX_SYMBOL", None)

        symbol_param = _env_str("EDGEX_SYMBOL_PARAM", None)
        if (symbol is None or symbol == "") and symbol_param and symbol_param.lower() == "contractid":
            symbol = contract_id

        if symbol is None or symbol == "":
            symbol = contract_id if contract_id is not None else "UNKNOWN"

        if contract_id is None or str(contract_id).strip() == "":
            raise RuntimeError("EDGEX_CONTRACT_ID is missing. Set it in Render Environment.")

        super().__init__(
            base_url=base_url,
            account_id=account_id,
            stark_private_key=stark_private_key,
            contract_id=str(contract_id),
            symbol=str(symbol),
            **kwargs,
        )
