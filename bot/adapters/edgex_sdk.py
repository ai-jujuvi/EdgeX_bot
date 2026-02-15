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
            "min
