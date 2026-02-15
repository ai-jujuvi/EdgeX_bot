import os
import time
import logging
from dataclasses import dataclass

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


def _truthy_env(name: str, default: bool = False) -> bool:
    v = _env_str(name, None)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "y", "on")


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
    ✅ 実発注の入口。DRY_RUNは必ず二重ガードで止める。
    """

    def __init__(
        self,
        base_url: str,
        account_id: str,
        stark_private_key: str,
        contract_id: str,
        symbol: str,
        dry_run: bool,
        op_spacing_sec: float = 1.5,
        **_kwargs,
    ):
        self.base_url = str(base_url)
        self.account_id = str(account_id)
        self.stark_private_key = str(stark_private_key)
        self.contract_id = str(contract_id)
        self.symbol = str(symbol)

        # ✅ 二重ガード：引数 + env
        self.dry_run = bool(dry_run)
        self.force_dry_run = _truthy_env("DRY_RUN", default=True)

        # ✅ 安全装置（任意）
        self.min_size = _env_float("EDGEX_MIN_ORDER_SIZE", None)
        self.max_size = _env_float("EDGEX_MAX_ORDER_SIZE", None)
        self.size_step = _env_float("EDGEX_SIZE_STEP", None)
        self.price_tick = _env_float("EDGEX_PRICE_TICK", None)

        env_spacing = _env_float("EDGEX_ADAPTER_OP_SPACING_SEC", None)
        self.op_spacing_sec = max(0.2, float(env_spacing if env_spacing is not None else op_spacing_sec))
        self._last_op_ts = 0.0

        log.info(
            "Adapter init: base_url=%s symbol=%s contract_id=%s dry_run=%s force_dry_run=%s "
            "min_size=%s max_size=%s size_step=%s price_tick=%s op_spacing_sec=%.2f",
            self.base_url,
            self.symbol,
            self.contract_id,
            self.dry_run,
            self.force_dry_run,
            self.min_size,
            self.max_size,
            self.size_step,
            self.price_tick,
            self.op_spacing_sec,
        )

        if self.force_dry_run:
            log.warning("🚧 DRY_RUN is ENABLED by env (DRY_RUN=1). Real orders are BLOCKED.")

    def _rate_limit_sleep(self) -> None:
        now = time.time()
        dt = now - self._last_op_ts
        if dt < self.op_spacing_sec:
            time.sleep(self.op_spacing_sec - dt)
        self._last_op_ts = time.time()

    def get_mid_price(self) -> float | None:
        """
        TODO: 実装に差し替え（ここは元BOT側が別で価格取ってるなら未使用でもOK）
        """
        return None

    def _round_to_step(self, value: float, step: float) -> float:
        if step <= 0:
            return float(value)
        n = int(value / step)  # 切り捨て
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

        if not (p > 0):
            log.error("Invalid price<=0 price=%s BLOCK.", p)
            return None
        if not (s > 0):
            log.error("Invalid size<=0 size=%s BLOCK.", s)
            return None

        if self.price_tick is not None and self.price_tick > 0:
            p2 = self._round_to_step(p, self.price_tick)
            if p2 <= 0:
                log.error("Price rounding resulted <=0. price=%s tick=%s BLOCK.", p, self.price_tick)
                return None
            if p2 != p:
                log.info("Price rounded: %s -> %s (tick=%s)", p, p2, self.price_tick)
            p = p2

        if self.size_step is not None and self.size_step > 0:
            s2 = self._round_to_step(s, self.size_step)
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

    def _is_dry_run_effective(self) -> bool:
        return bool(self.dry_run) or bool(self.force_dry_run)

    def place_limit_order(self, side: str, price: float, size: float) -> None:
        intent = self._validate_and_normalize(side=side, price=price, size=size)
        if intent is None:
            return

        self._rate_limit_sleep()

        if self._is_dry_run_effective():
            log.info("[DRY_RUN] would place order: %s", intent.to_dict())
            return

        self._place_order_real(intent)

    # 互換：元コードが place_order を呼ぶ場合
    def place_order(self, side: str, price: float, size: float) -> None:
        self.place_limit_order(side=side, price=price, size=size)

    def _place_order_real(self, intent: OrderIntent) -> None:
        log.critical("REAL ORDER PATH is NOT implemented. BLOCKED. intent=%s", intent.to_dict())
        raise RuntimeError("EdgeXAdapter._place_order_real is not implemented")


class EdgeXSDKAdapter(EdgeXAdapter):
    """
    ✅ 互換クラス（超重要）
    元の run_edgex_grid.py は、EdgeXSDKAdapter に
    base_url/account_id/stark_private_key しか渡さない実装の可能性がある。

    なので、足りない contract_id / symbol / dry_run は env から補完する。
    """

    def __init__(self, base_url: str, account_id: str, stark_private_key: str, *args, **kwargs):
        # envから補完（ここがポイント）
        contract_id = kwargs.pop("contract_id", None) or _env_str("EDGEX_CONTRACT_ID", None)
        symbol = kwargs.pop("symbol", None) or _env_str("EDGEX_SYMBOL", None)

        # 元BOTの仕様：symbol_param=contractId のとき、symbolはcontract_idを入れる運用がある
        symbol_param = _env_str("EDGEX_SYMBOL_PARAM", None)
        if (symbol is None or symbol == "") and symbol_param and symbol_param.lower() == "contractid":
            symbol = contract_id

        # 最終フォールバック：symbolが空なら contract_id を使う（あなたのBOT流儀に寄せる）
        if symbol is None or symbol == "":
            symbol = contract_id if contract_id is not None else "UNKNOWN"

        # dry_run も env から（DRY_RUN=1 を最優先で守る）
        dry_run_env = _truthy_env("DRY_RUN", default=True)
        dry_run = kwargs.pop("dry_run", None)
        if dry_run is None:
            dry_run = dry_run_env

        if contract_id is None:
            # ここに来るのは env 未設定のときだけ
            raise RuntimeError("EDGEX_CONTRACT_ID is missing. Set it in Render Environment.")

        super().__init__(
            base_url=base_url,
            account_id=account_id,
            stark_private_key=stark_private_key,
            contract_id=str(contract_id),
            symbol=str(symbol),
            dry_run=bool(dry_run),
            **kwargs,
        )
