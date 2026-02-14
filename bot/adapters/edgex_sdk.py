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


def _env_int(name: str, default: int | None = None) -> int | None:
    s = _env_str(name, None)
    if s is None:
        return default
    try:
        return int(s)
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
    ✅ ここが “実発注の入口”。
    → DRY_RUNは「二重ガード」で必ず止める（事故防止）。

    置換ポイント：
    - get_mid_price(): あなたの実装（best_bid/best_ask → mid）に差し替え
    - _place_order_real(): あなたの実発注SDK呼び出しに差し替え

    環境変数（安全装置系：任意）
    - DRY_RUN=1                         : 最優先で実発注停止（強制）
    - EDGEX_MIN_ORDER_SIZE              : 最小サイズ（例 BTC 0.003 / GOLD は後で設定）
    - EDGEX_MAX_ORDER_SIZE              : 最大サイズ（暴走防止）
    - EDGEX_SIZE_STEP                   : サイズ刻み（例 0.001 など。未指定なら丸めしない）
    - EDGEX_PRICE_TICK                  : 価格刻み（例 GOLDが0.1刻み等。未指定なら丸めしない）
    - EDGEX_MAX_LEVELS_PER_SIDE         : 片側levels上限（グリッド側で使う想定だが、ここでも守れる）
    - EDGEX_ADAPTER_OP_SPACING_SEC      : 注文間隔（レート制限/安全）
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
    ):
        self.base_url = str(base_url)
        self.account_id = str(account_id)
        self.stark_private_key = str(stark_private_key)
        self.contract_id = str(contract_id)
        self.symbol = str(symbol)

        # ✅ 二重ガードの片方（初期値）
        self.dry_run = bool(dry_run)

        # ✅ もう片方：環境変数で“強制DRY_RUN”
        # どっちかがTrueなら絶対に実発注しない
        self.force_dry_run = _truthy_env("DRY_RUN", default=True)

        # ✅ 安全装置（サイズ/刻み）
        self.min_size = _env_float("EDGEX_MIN_ORDER_SIZE", None)  # 例: BTC 0.003
        self.max_size = _env_float("EDGEX_MAX_ORDER_SIZE", None)  # 例: 1.0 とか
        self.size_step = _env_float("EDGEX_SIZE_STEP", None)      # 例: 0.001
        self.price_tick = _env_float("EDGEX_PRICE_TICK", None)    # 例: 0.1

        # ✅ レート制限/安全
        self.op_spacing_sec = max(0.2, float(_env_float("EDGEX_ADAPTER_OP_SPACING_SEC", op_spacing_sec) or op_spacing_sec))
        self._last_op_ts = 0.0

        # TODO: あなたの環境のSDK初期化に置き換える
        # self.client = EdgeXClient(base_url=self.base_url, account_id=self.account_id, stark_private_key=self.stark_private_key)

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
        TODO: あなたの実装に置き換え（必須）
        - SDK/RESTで best_bid / best_ask を取って mid を返す
        - 価格が取れない時は None を返す（その場合、上位ロジックは停止する）

        例：
            ob = self.client.get_orderbook(self.contract_id)
            best_bid = float(ob["best_bid"])
            best_ask = float(ob["best_ask"])
            return (best_bid + best_ask) / 2.0
        """
        return None

    def _round_to_step(self, value: float, step: float) -> float:
        """
        step刻みに丸める（安全のため“切り捨て”寄りにする）
        """
        if step <= 0:
            return float(value)
        # 切り捨て（過大発注を避ける）
        n = int(value / step)
        return float(n * step)

    def _validate_and_normalize(self, side: str, price: float, size: float) -> OrderIntent | None:
        """
        ✅ 事故防止：異常値はここで止める（None返して発注しない）
        """
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

        # 価格刻み（任意）
        if self.price_tick is not None and self.price_tick > 0:
            p2 = self._round_to_step(p, self.price_tick)
            if p2 <= 0:
                log.error("Price rounding resulted <=0. price=%s tick=%s BLOCK.", p, self.price_tick)
                return None
            if p2 != p:
                log.info("Price rounded: %s -> %s (tick=%s)", p, p2, self.price_tick)
            p = p2

        # サイズ刻み（任意）
        if self.size_step is not None and self.size_step > 0:
            s2 = self._round_to_step(s, self.size_step)
            if s2 <= 0:
                log.error("Size rounding resulted <=0. size=%s step=%s BLOCK.", s, self.size_step)
                return None
            if s2 != s:
                log.info("Size rounded: %s -> %s (step=%s)", s, s2, self.size_step)
            s = s2

        # 最小/最大サイズ（任意）
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
        """
        ✅ 二重ガード（どちらかがONなら絶対に実発注しない）
        """
        return bool(self.dry_run) or bool(self.force_dry_run)

    def place_limit_order(self, side: str, price: float, size: float) -> None:
        """
        ✅ 事故ポイントなので、最優先で DRY_RUN を止める。
        DRY_RUN時は、置くはずの注文をログに必ず出す。
        """
        intent = self._validate_and_normalize(side=side, price=price, size=size)
        if intent is None:
            # バリデーション落ち → 発注しない
            return

        self._rate_limit_sleep()

        if self._is_dry_run_effective():
            log.info("[DRY_RUN] would place order: %s", intent.to_dict())
            return

        # ✅ 実発注（ここだけがREAL PATH）
        self._place_order_real(intent)

    def _place_order_real(self, intent: OrderIntent) -> None:
        """
        TODO: ここをあなたの既存SDK呼び出しに置換してください（必須）
        """
        # 例：
        # res = self.client.place_order(
        #     contract_id=int(intent.contract_id),
        #     side=intent.side,
        #     price=intent.price,
        #     size=intent.size,
        #     order_type="LIMIT",
        # )
        # log.info("placed: %s", res)

        # テンプレのままなら危険なので、敢えてエラー寄りログにして気づけるようにする
        log.critical(
            "REAL ORDER PATH is NOT implemented. BLOCKED. intent=%s",
            intent.to_dict(),
        )
        # 事故防止：未実装のままでも“勝手に通らない”ように例外で落とす
        raise RuntimeError("EdgeXAdapter._place_order_real is not implemented")
