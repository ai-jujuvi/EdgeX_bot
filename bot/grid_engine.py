"""
Grid Trading Engine
グリッド戦略エンジン
"""

import asyncio
import math
import os
import time
from typing import Dict, Optional, Any
from loguru import logger

from bot.adapters.base import ExchangeAdapter
from bot.models.types import OrderRequest, OrderSide, OrderType, TimeInForce
from bot.utils.trade_logger import TradeLogger


class GridEngine:
    """**STEP毎に両サイドへグリッド指値を差し続けなくしたエンジン.

    - 剥ぎさない限りキャンセル/差し直しは一切しない
    - 片側levels本オーダー (ENVで指定) を設定価格を中心に両側配置
    - 約定したら価格が戻らない限り再配置しない
    - 価格が動いたら新しい価格帯に不足分の追加（過去の注文は放置）
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        symbol: str,
        poll_interval_sec: float = 1.0,
    ) -> None:
        self.adapter = adapter

        # --- symbol 必須化（事故防止） ---
        # 呼び出し側で symbol が揺れることがあるので、
        # ここで「空ならENVから補完」「それでも空なら例外」で強制する。
        sym = str(symbol).strip() if symbol is not None else ""
        if not sym:
            sym = str(os.getenv("EDGEX_SYMBOL", "")).strip()
        if not sym:
            raise ValueError("GridEngine: symbol is required (pass symbol or set EDGEX_SYMBOL).")
        self.symbol = sym

        self.poll_interval_sec = max(1.5, float(poll_interval_sec))
        self._running = False
        self._loop_iter: int = 0

        self.size = float(os.getenv("EDGEX_GRID_SIZE", os.getenv("EDGEX_SIZE", "0.01")))
        # 既定: ステップ=50USD / 初回オフセット=100USD / レベル=10
        self.step = float(os.getenv("EDGEX_GRID_STEP_USD", "50"))
        # 両側の価格幅(固定) だけ使い込む
        self.first_offset = float(os.getenv("EDGEX_GRID_FIRST_OFFSET_USD", "100"))
        self.levels = int(os.getenv("EDGEX_GRID_LEVELS_PER_SIDE", "10"))

        logger.info(
            "グリッド設定: symbol={} step={}USD first_offset={}USD levels={} size={}",
            self.symbol,
            self.step,
            self.first_offset,
            self.levels,
            self.size,
        )

        # レート制限回避のための遅延時間調整
        try:
            # シンプル高速モードでは既定を短めにする（必要なら環境変数で上書き）
            self.op_spacing_sec = float(os.getenv("EDGEX_GRID_OP_SPACING_SEC", "0.4"))
        except Exception:
            self.op_spacing_sec = 0.4

        # 初回配置済みフラグ（複数回はfirst_offsetは適用しない一度だけ）
        self.initialized = False

        # 既に出した価格（重複防止）
        self.placed_buy_px_to_id: Dict[float, str] = {}
        self.placed_sell_px_to_id: Dict[float, str] = {}

        self.tlog = TradeLogger()

        # closed PnL poll interval (sec). 0 to disable.
        try:
            self.closed_poll_sec = float(os.getenv("EDGEX_GRID_CLOSED_PNL_SEC", "30"))
        except Exception:
            self.closed_poll_sec = 30.0
        self._last_closed_id: str | None = None
        self._last_closed_poll_ts: float = 0.0

        # 既存の“このBotが出していない注文”を徐々に整理して、levels本に保つ
        try:
            self.enforce_levels = str(os.getenv("EDGEX_GRID_ENFORCE_LEVELS", "1")).lower() in ("1", "true", "yes")
        except Exception:
            self.enforce_levels = True

        # 価格刻み（BOX比較の許容誤差に利用）
        try:
            self.price_tick = float(os.getenv("EDGEX_PRICE_TICK", "0.1"))
        except Exception:
            self.price_tick = 0.1

        # 1ループあたりの新規発注上限（片側）: 明示指定があれば適用（任意）
        try:
            self.max_new_per_loop = int(os.getenv("EDGEX_GRID_MAX_NEW_PER_LOOP", "0"))
        except Exception:
            self.max_new_per_loop = 0

        # シンプルモード（余計な挙動を排し、配置を高速化）
        try:
            self.simple_mode = str(os.getenv("EDGEX_GRID_SIMPLE", "1")).lower() in ("1", "true", "yes")
        except Exception:
            self.simple_mode = True

        # 板を使わずティッカー価格のみで中間価格とみなすモード
        try:
            # 既定: ティッカーのみ（取得が安定）
            self.use_ticker_only = str(os.getenv("EDGEX_USE_TICKER_ONLY", "1")).lower() in ("1", "true", "yes")
        except Exception:
            self.use_ticker_only = True

        # BOX固定モード: 毎ループで P±(X + k*N) の集合に“きっちり”寄せる（余計はキャンセル・欠けは追加）
        try:
            # 既定: BOX（現在価格を中心に毎ループ寄せる）
            self.box_mode = str(os.getenv("EDGEX_GRID_BOX_MODE", "1")).lower() in ("1", "true", "yes")
        except Exception:
            self.box_mode = True

        # 実注文の同期周期（ループ何回に1回か）。BINモードでの整合性確保用
        try:
            self.active_sync_every = int(os.getenv("EDGEX_GRID_ACTIVE_SYNC_EVERY", "3"))
        except Exception:
            self.active_sync_every = 3

        # ビン固定モード
        try:
            self.bin_mode = str(os.getenv("EDGEX_GRID_BIN_MODE", "0")).lower() in ("1", "true", "yes")
        except Exception:
            self.bin_mode = False
        self._bin_center_units: int | None = None

        # 価格追従（乖離補正）設定（シンプルモードでは既定OFF）
        try:
            default_follow = "0" if self.simple_mode else "1"
            self.follow_enable = str(os.getenv("EDGEX_GRID_FOLLOW_ENABLE", default_follow)).lower() in ("1", "true", "yes")
        except Exception:
            self.follow_enable = (not self.simple_mode)
        try:
            self.follow_slack_steps = int(os.getenv("EDGEX_GRID_FOLLOW_SLACK_STEPS", "1"))
        except Exception:
            self.follow_slack_steps = 1
        try:
            self.max_shift_per_loop = int(os.getenv("EDGEX_GRID_MAX_SHIFT_PER_LOOP", "1"))
        except Exception:
            self.max_shift_per_loop = 1

    # -----------------------------
    # 互換ラッパ（adapter揺れ対策）
    # -----------------------------
    async def _get_ticker_compat(self) -> Any:
        """
        adapter.get_ticker の呼び方が揺れても落ちないラッパ。
        返り値は「.price が取れる」想定だが、dict等も来うるので最終的に float 化で守る。
        """
        if not hasattr(self.adapter, "get_ticker"):
            raise AttributeError("adapter has no get_ticker()")

        fn = getattr(self.adapter, "get_ticker")

        # 1) get_ticker(symbol)
        try:
            return await fn(self.symbol)
        except TypeError:
            pass
        except Exception:
            # 仕様は合ってるがAPI側エラーの可能性もあるので次も試す
            pass

        # 2) get_ticker(symbol=...)
        try:
            return await fn(symbol=self.symbol)
        except TypeError:
            pass
        except Exception:
            pass

        # 3) get_ticker()（symbolはadapter内部に持つ設計の可能性）
        return await fn()

    async def _get_best_bid_ask_compat(self) -> tuple[Optional[float], Optional[float]]:
        if not hasattr(self.adapter, "get_best_bid_ask"):
            return (None, None)

        fn = getattr(self.adapter, "get_best_bid_ask")
        try:
            bid, ask = await fn(self.symbol)
            return (bid, ask)
        except TypeError:
            try:
                bid, ask = await fn(symbol=self.symbol)
                return (bid, ask)
            except Exception:
                return (None, None)
        except Exception:
            return (None, None)

    async def _list_active_orders_compat(self):
        fn = getattr(self.adapter, "list_active_orders")
        try:
            return await fn(self.symbol)
        except TypeError:
            try:
                return await fn(symbol=self.symbol)
            except Exception:
                return await fn()
        except Exception:
            raise

    async def _cancel_order_compat(self, order_id: str):
        fn = getattr(self.adapter, "cancel_order")
        try:
            return await fn(order_id)
        except TypeError:
            # cancel_order(id=...) の可能性
            return await fn(id=order_id)

    # -----------------------------
    # Core
    # -----------------------------
    def _has_min_gap(self, side_map: Dict[float, str], px: float) -> bool:
        """Return True if `px` is at least `self.step` away from all existing prices in `side_map`."""
        for existing_price in side_map.keys():
            if abs(existing_price - px) < self.step - 1e-9:
                return False
        return True

    async def run(self) -> None:
        await self.adapter.connect()
        self._running = True
        logger.info(
            "グリッドエンジン起動: symbol={} step={}USD levels={} size={}",
            self.symbol,
            self.step,
            self.levels,
            self.size,
        )
        logger.debug(
            "grid boot env: symbol={} step(N)={} offset(X)={} levels={} max_new_per_loop={} enforce_levels={} size={}",
            self.symbol,
            self.step,
            self.first_offset,
            self.levels,
            self.max_new_per_loop,
            getattr(self, "enforce_levels", True),
            self.size,
        )

        try:
            while self._running:
                try:
                    self._loop_iter += 1
                    logger.debug(
                        "グリッドループ開始: iter={} symbol={} placed_buy={} placed_sell={} initialized={}",
                        self._loop_iter,
                        self.symbol,
                        len(self.placed_buy_px_to_id),
                        len(self.placed_sell_px_to_id),
                        self.initialized,
                    )

                    # 現在価格取得
                    try:
                        if getattr(self, "use_ticker_only", False):
                            ticker = await self._get_ticker_compat()
                            # tickerがdict等の可能性もあるため安全に拾う
                            px = getattr(ticker, "price", None)
                            if px is None and isinstance(ticker, dict):
                                px = ticker.get("price") or ticker.get("last") or ticker.get("mark")
                            mid_price = float(px)
                        else:
                            bid, ask = await self._get_best_bid_ask_compat()
                            if bid is not None and ask is not None:
                                mid_price = (float(bid) + float(ask)) / 2.0
                            else:
                                ticker = await self._get_ticker_compat()
                                px = getattr(ticker, "price", None)
                                if px is None and isinstance(ticker, dict):
                                    px = ticker.get("price") or ticker.get("last") or ticker.get("mark")
                                mid_price = float(px)
                    except Exception as e:
                        logger.warning("価格取得に失敗: symbol={} err={}", self.symbol, e)
                        await asyncio.sleep(self.poll_interval_sec)
                        continue

                    logger.debug(
                        "loop ctx: symbol={} P={} X={} N={} levels={} placed_buy={} placed_sell={}",
                        self.symbol,
                        mid_price,
                        self.first_offset,
                        self.step,
                        self.levels,
                        sorted(self.placed_buy_px_to_id.keys()),
                        sorted(self.placed_sell_px_to_id.keys()),
                    )

                    # 毎ループ: ポジションを取得して軽く表示（ズレの観測用）
                    try:
                        positions = await getattr(self.adapter, "fetch_positions", lambda *_args, **_kw: [])(self.symbol)
                        net_size = 0.0
                        for p in (positions or []):
                            try:
                                sz_raw = p.get("size") or p.get("positionSize") or p.get("qty")
                                if sz_raw is None:
                                    continue
                                sz = float(sz_raw)
                                side = str(p.get("side") or p.get("positionSide") or "").upper()
                                if side in ("SHORT", "SELL"):
                                    sz = -abs(sz)
                                elif side in ("LONG", "BUY"):
                                    sz = abs(sz)
                                net_size += sz
                            except Exception:
                                continue
                        logger.debug("pos: net_size={} raw_count={}", net_size, len(positions or []))
                    except Exception:
                        pass

                    # 周期的に取引所のOPEN注文と突合（3ループに1回など）
                    if getattr(self, "active_sync_every", 0) > 0 and (self._loop_iter % self.active_sync_every == 0):
                        await self._sync_active_orders_from_exchange()

                    # グリッド配置
                    await self._ensure_grid(mid_price)

                    # 約定確認と補充
                    await self._replenish_if_filled()

                except Exception as e:
                    logger.warning("グリッドループエラー: {}", e)
                    await asyncio.sleep(self.poll_interval_sec)

                # 定期: クローズ損益の新規行を取り込み
                await self._poll_closed_pnl_once()

                # 正常時も必ず待機してAPI連打を抑制（429対策）
                await asyncio.sleep(self.poll_interval_sec)

        finally:
            await self.adapter.close()
            logger.info("グリッドエンジン停止: symbol={}", self.symbol)

    async def _sync_active_orders_from_exchange(self) -> None:
        """取引所のOPEN注文を取得し、内部マップを実態に同期する（BIN用の軽量突合）。"""
        try:
            active_orders = await self._list_active_orders_compat()
        except Exception as e:
            logger.debug("active sync skip: {}", e)
            return

        new_buys: Dict[float, str] = {}
        new_sells: Dict[float, str] = {}

        def _px(row: dict) -> float | None:
            try:
                raw = row.get("price") or row.get("px") or row.get("0")
                return float(raw) if raw is not None else None
            except Exception:
                return None

        def _oid(row: dict) -> str | None:
            oid = (
                row.get("orderId")
                or row.get("id")
                or row.get("order_id")
                or row.get("clientOrderId")
                or row.get("client_order_id")
            )
            return str(oid) if oid else None

        for row in (active_orders or []):
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").upper()
            if status and status != "OPEN":
                continue
            px = _px(row)
            if px is None:
                continue
            side_str = str(row.get("side") or row.get("orderSide") or "").upper()
            oid = _oid(row)
            if not oid or not side_str:
                continue
            if side_str in ("BUY", "LONG"):
                new_buys[px] = oid
            elif side_str in ("SELL", "SHORT"):
                new_sells[px] = oid

        self.placed_buy_px_to_id = new_buys
        self.placed_sell_px_to_id = new_sells
        logger.debug("active sync: buy={} sell={}", len(new_buys), len(new_sells))

    async def _ensure_grid(self, mid_price: float):
        """
        現在価格Pから内側Xを空け、P±(X + k*N) の等差列だけに指値を配置。
        - 買い: P - (X + k*N)
        - 売り: P + (X + k*N)
        """
        if self.step <= 0:
            return

        # === BOXモード ===
        if getattr(self, "box_mode", False):
            P = float(mid_price)
            s = float(self.step)
            X = float(self.first_offset)

            lower_limit = P - X - 1e-9
            buy_start = math.floor(lower_limit / s) * s
            buy_targets = [buy_start - i * s for i in range(self.levels)]

            upper_limit = P + X + 1e-9
            sell_start = math.ceil(upper_limit / s) * s
            sell_targets = [sell_start + i * s for i in range(self.levels)]

            def _r(x: float) -> float:
                return round(float(x), 10)

            buy_targets = [_r(px) for px in buy_targets if px > 0 and px < (P - 1e-9)]
            sell_targets = [_r(px) for px in sell_targets if px > (P + 1e-9)]

            current_buys = set(_r(px) for px in self.placed_buy_px_to_id.keys())
            current_sells = set(_r(px) for px in self.placed_sell_px_to_id.keys())
            target_buys = set(buy_targets)
            target_sells = set(sell_targets)

            tol = max(self.price_tick * 1.01, 1e-6)

            def _near_any(x: float, targets: set[float]) -> bool:
                for t in targets:
                    if abs(x - t) <= tol:
                        return True
                return False

            keep_buys = set(px for px in current_buys if _near_any(px, target_buys))
            keep_sells = set(px for px in current_sells if _near_any(px, target_sells))

            inner_buy_border = P - X
            inner_sell_border = P + X
            keep_buys |= set(px for px in current_buys if px >= (inner_buy_border - tol))
            keep_sells |= set(px for px in current_sells if px <= (inner_sell_border + tol))

            # 余計だけキャンセル
            for px in sorted(current_buys - keep_buys):
                try:
                    oid = self.placed_buy_px_to_id.pop(px)
                except KeyError:
                    continue
                try:
                    await self._cancel_order_compat(oid)
                except Exception:
                    pass
                await asyncio.sleep(self.op_spacing_sec)

            for px in sorted(current_sells - keep_sells):
                try:
                    oid = self.placed_sell_px_to_id.pop(px)
                except KeyError:
                    continue
                try:
                    await self._cancel_order_compat(oid)
                except Exception:
                    pass
                await asyncio.sleep(self.op_spacing_sec)

            # 欠けを追加
            for px in sorted(target_buys):
                if not any(abs(cb - px) <= tol for cb in keep_buys):
                    if self._has_min_gap(self.placed_buy_px_to_id, px):
                        await self._place_order(OrderSide.BUY, px)
                        await asyncio.sleep(self.op_spacing_sec)

            for px in sorted(target_sells):
                if not any(abs(cs - px) <= tol for cs in keep_sells):
                    if self._has_min_gap(self.placed_sell_px_to_id, px):
                        await self._place_order(OrderSide.SELL, px)
                        await asyncio.sleep(self.op_spacing_sec)

            if not self.initialized:
                self.initialized = True
                logger.info("BOX: 初期配置完了 買い{}本 売り{}本", len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id))
            return

        # === BIN固定モード（あなたの元コードをそのまま保持）===
        if self.bin_mode:
            try:
                center_units = round(float(mid_price) / self.step)
                center = float(center_units * self.step)
            except Exception:
                center = float(mid_price)
                center_units = round(center / self.step)

            if not self.initialized:
                buy_targets = [center - k * self.step for k in range(self.levels, 0, -1)]
                sell_targets = [center + k * self.step for k in range(1, self.levels + 1)]

                add_buys = 0
                add_sells = 0
                for px in buy_targets:
                    if self.max_new_per_loop and add_buys >= self.max_new_per_loop:
                        break
                    await self._place_order(OrderSide.BUY, px)
                    add_buys += 1
                    await asyncio.sleep(self.op_spacing_sec)

                for px in sell_targets:
                    if self.max_new_per_loop and add_sells >= self.max_new_per_loop:
                        break
                    await self._place_order(OrderSide.SELL, px)
                    add_sells += 1
                    await asyncio.sleep(self.op_spacing_sec)

                self.initialized = True
                self._bin_center_units = center_units
                logger.info("BIN: 初期配置完了 買い{}本 売り{}本", len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id))
                return

            prev_units = self._bin_center_units if self._bin_center_units is not None else center_units
            delta_units = center_units - prev_units

            if delta_units == 0:
                try:
                    buy_targets = [center - k * self.step for k in range(self.levels, 0, -1)]
                    sell_targets = [center + k * self.step for k in range(1, self.levels + 1)]

                    if len(self.placed_buy_px_to_id) < self.levels:
                        for px in buy_targets:
                            if len(self.placed_buy_px_to_id) >= self.levels:
                                break
                            if px not in self.placed_buy_px_to_id:
                                await self._place_order(OrderSide.BUY, px)
                                await asyncio.sleep(self.op_spacing_sec)

                    if len(self.placed_sell_px_to_id) < self.levels:
                        for px in sell_targets:
                            if len(self.placed_sell_px_to_id) >= self.levels:
                                break
                            if px not in self.placed_sell_px_to_id:
                                await self._place_order(OrderSide.SELL, px)
                                await asyncio.sleep(self.op_spacing_sec)
                except Exception as e:
                    logger.debug("BIN: 補充スキップ {}", e)
                return

            steps = int(abs(delta_units))
            direction_up = delta_units > 0

            for _ in range(steps):
                if direction_up:
                    if self.placed_buy_px_to_id:
                        far_buy_px = min(self.placed_buy_px_to_id.keys())
                        far_buy_id = self.placed_buy_px_to_id.pop(far_buy_px)
                        try:
                            await self._cancel_order_compat(far_buy_id)
                        except Exception:
                            logger.debug("BIN↑: 遠いBUYキャンセル失敗(無視) id={} px={}", far_buy_id, far_buy_px)
                        await asyncio.sleep(self.op_spacing_sec)

                        near_buy = max(self.placed_buy_px_to_id.keys()) if self.placed_buy_px_to_id else (center - self.step)
                        new_near_buy = near_buy + self.step
                        if new_near_buy < (mid_price - 1e-9) and new_near_buy not in self.placed_buy_px_to_id and self._has_min_gap(self.placed_buy_px_to_id, new_near_buy):
                            await self._place_order(OrderSide.BUY, new_near_buy)
                            await asyncio.sleep(self.op_spacing_sec)

                    if self.placed_sell_px_to_id:
                        far_sell_px = max(self.placed_sell_px_to_id.keys())
                        new_outer_sell = far_sell_px + self.step
                        if new_outer_sell > (mid_price + 1e-9) and new_outer_sell not in self.placed_sell_px_to_id and self._has_min_gap(self.placed_sell_px_to_id, new_outer_sell):
                            await self._place_order(OrderSide.SELL, new_outer_sell)
                            await asyncio.sleep(self.op_spacing_sec)
                else:
                    if self.placed_sell_px_to_id:
                        far_sell_px = max(self.placed_sell_px_to_id.keys())
                        far_sell_id = self.placed_sell_px_to_id.pop(far_sell_px)
                        try:
                            await self._cancel_order_compat(far_sell_id)
                        except Exception:
                            logger.debug("BIN↓: 遠いSELLキャンセル失敗(無視) id={} px={}", far_sell_id, far_sell_px)
                        await asyncio.sleep(self.op_spacing_sec)

                        near_sell = min(self.placed_sell_px_to_id.keys()) if self.placed_sell_px_to_id else (center + self.step)
                        new_near_sell = near_sell - self.step
                        if new_near_sell > (mid_price + 1e-9) and new_near_sell not in self.placed_sell_px_to_id and self._has_min_gap(self.placed_sell_px_to_id, new_near_sell):
                            await self._place_order(OrderSide.SELL, new_near_sell)
                            await asyncio.sleep(self.op_spacing_sec)

                    if self.placed_buy_px_to_id:
                        far_buy_px = min(self.placed_buy_px_to_id.keys())
                        new_outer_buy = far_buy_px - self.step
                        if new_outer_buy > 0 and new_outer_buy < (mid_price - 1e-9) and new_outer_buy not in self.placed_buy_px_to_id and self._has_min_gap(self.placed_buy_px_to_id, new_outer_buy):
                            await self._place_order(OrderSide.BUY, new_outer_buy)
                            await asyncio.sleep(self.op_spacing_sec)

            self._bin_center_units = center_units
            return

        # ---- 以下、あなたの元コードを維持（初期/追従/補充） ----
        if self.initialized:
            need_buy_seed = len(self.placed_buy_px_to_id) == 0
            need_sell_seed = len(self.placed_sell_px_to_id) == 0
            if need_buy_seed or need_sell_seed:
                buy_targets = [float(mid_price) - (self.first_offset + i * self.step) for i in range(self.levels)]
                sell_targets = [float(mid_price) + (self.first_offset + i * self.step) for i in range(self.levels)]
                logger.info("再配置: need_buy={} need_sell={} P={} X={} N={}", need_buy_seed, need_sell_seed, mid_price, self.first_offset, self.step)

                if need_buy_seed:
                    new_buys = 0
                    for px in buy_targets:
                        if px <= 0:
                            continue
                        if px >= (mid_price - 1e-9):
                            continue
                        if px in self.placed_buy_px_to_id:
                            continue
                        if not self._has_min_gap(self.placed_buy_px_to_id, px):
                            continue
                        await self._place_order(OrderSide.BUY, px)
                        new_buys += 1
                        await asyncio.sleep(self.op_spacing_sec)
                        if new_buys >= self.levels:
                            break

                if need_sell_seed:
                    new_sells = 0
                    for px in sell_targets:
                        if px <= (mid_price + 1e-9):
                            continue
                        if px in self.placed_sell_px_to_id:
                            continue
                        if not self._has_min_gap(self.placed_sell_px_to_id, px):
                            continue
                        await self._place_order(OrderSide.SELL, px)
                        new_sells += 1
                        await asyncio.sleep(self.op_spacing_sec)
                        if new_sells >= self.levels:
                            break
                return

            if self.follow_enable and self.step > 0:
                # BUY側追従
                try:
                    shifts = 0
                    if self.placed_buy_px_to_id:
                        nearest_buy = max(self.placed_buy_px_to_id.keys())
                        desired_min_buy = float(mid_price) - (self.first_offset + self.follow_slack_steps * self.step)
                        while nearest_buy < desired_min_buy - 1e-9 and shifts < self.max_shift_per_loop:
                            if len(self.placed_buy_px_to_id) <= 0:
                                break
                            far_buy_px = min(self.placed_buy_px_to_id.keys())
                            far_buy_id = self.placed_buy_px_to_id.pop(far_buy_px)
                            try:
                                await self._cancel_order_compat(far_buy_id)
                                logger.info("追従: 遠いBUYキャンセル px={}", far_buy_px)
                            except Exception:
                                logger.debug("追従: 遠いBUYキャンセル失敗(無視) id={} px={}", far_buy_id, far_buy_px)
                            await asyncio.sleep(self.op_spacing_sec)

                            new_buy_px = nearest_buy + self.step
                            if new_buy_px >= (mid_price - 1e-9):
                                break
                            if new_buy_px in self.placed_buy_px_to_id:
                                nearest_buy = new_buy_px
                                shifts += 1
                                continue
                            if not self._has_min_gap(self.placed_buy_px_to_id, new_buy_px):
                                logger.debug("追従: BUY gap違反でスキップ new_px={}", new_buy_px)
                                break
                            await self._place_order(OrderSide.BUY, new_buy_px)
                            nearest_buy = new_buy_px
                            shifts += 1
                            await asyncio.sleep(self.op_spacing_sec)
                        if shifts:
                            logger.debug("追従BUY: nearest={} desired_min={} shifts={}", nearest_buy, desired_min_buy, shifts)
                except Exception as e:
                    logger.debug("追従BUY処理スキップ: {}", e)

                # SELL側追従
                try:
                    shifts = 0
                    if self.placed_sell_px_to_id:
                        nearest_sell = min(self.placed_sell_px_to_id.keys())
                        desired_max_sell = float(mid_price) + (self.first_offset + self.follow_slack_steps * self.step)
                        while nearest_sell > desired_max_sell + 1e-9 and shifts < self.max_shift_per_loop:
                            if len(self.placed_sell_px_to_id) <= 0:
                                break
                            far_sell_px = max(self.placed_sell_px_to_id.keys())
                            far_sell_id = self.placed_sell_px_to_id.pop(far_sell_px)
                            try:
                                await self._cancel_order_compat(far_sell_id)
                                logger.info("追従: 遠いSELLキャンセル px={}", far_sell_px)
                            except Exception:
                                logger.debug("追従: 遠いSELLキャンセル失敗(無視) id={} px={}", far_sell_id, far_sell_px)
                            await asyncio.sleep(self.op_spacing_sec)

                            new_sell_px = nearest_sell - self.step
                            if new_sell_px <= (mid_price + 1e-9):
                                break
                            if new_sell_px in self.placed_sell_px_to_id:
                                nearest_sell = new_sell_px
                                shifts += 1
                                continue
                            if not self._has_min_gap(self.placed_sell_px_to_id, new_sell_px):
                                logger.debug("追従: SELL gap違反でスキップ new_px={}", new_sell_px)
                                break
                            await self._place_order(OrderSide.SELL, new_sell_px)
                            nearest_sell = new_sell_px
                            shifts += 1
                            await asyncio.sleep(self.op_spacing_sec)
                        if shifts:
                            logger.debug("追従SELL: nearest={} desired_max={} shifts={}", nearest_sell, desired_max_sell, shifts)
                except Exception as e:
                    logger.debug("追従SELL処理スキップ: {}", e)

            # levels維持の外側補充（あなたの元コード維持）
            try:
                add_buys = 0
                add_sells = 0
                while len(self.placed_buy_px_to_id) < self.levels:
                    if not self.placed_buy_px_to_id:
                        break
                    cand = min(self.placed_buy_px_to_id.keys()) - self.step
                    attempts = 0
                    placed = False
                    while cand <= (mid_price - 1e-9) and self._has_min_gap(self.placed_buy_px_to_id, cand) and attempts < 3:
                        if self.max_new_per_loop and add_buys >= self.max_new_per_loop:
                            break
                        before = set(self.placed_buy_px_to_id.keys())
                        await self._place_order(OrderSide.BUY, cand)
                        await asyncio.sleep(self.op_spacing_sec)
                        after = set(self.placed_buy_px_to_id.keys())
                        if cand in after and cand not in before:
                            placed = True
                            add_buys += 1
                            break
                        cand -= self.step
                        attempts += 1
                    if not placed:
                        break

                while len(self.placed_sell_px_to_id) < self.levels:
                    if not self.placed_sell_px_to_id:
                        break
                    cand = max(self.placed_sell_px_to_id.keys()) + self.step
                    attempts = 0
                    placed = False
                    while cand >= (mid_price + 1e-9) and self._has_min_gap(self.placed_sell_px_to_id, cand) and attempts < 3:
                        if self.max_new_per_loop and add_sells >= self.max_new_per_loop:
                            break
                        before = set(self.placed_sell_px_to_id.keys())
                        await self._place_order(OrderSide.SELL, cand)
                        await asyncio.sleep(self.op_spacing_sec)
                        after = set(self.placed_sell_px_to_id.keys())
                        if cand in after and cand not in before:
                            placed = True
                            add_sells += 1
                            break
                        cand += self.step
                        attempts += 1
                    if not placed:
                        break

                if add_buys or add_sells:
                    logger.debug("levels補充: add_buys={} add_sells={} now buy={} sell={}", add_buys, add_sells, len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id))
            except Exception as e:
                logger.debug("levels補充スキップ: {}", e)
            return

        # 初回配置
        buy_targets = [float(mid_price) - (self.first_offset + i * self.step) for i in range(self.levels)]
        sell_targets = [float(mid_price) + (self.first_offset + i * self.step) for i in range(self.levels)]
        logger.debug("ensure(init): P={} X={} N={} buy_targets={} sell_targets={}", mid_price, self.first_offset, self.step, buy_targets, sell_targets)

        new_buys = 0
        new_sells = 0

        for px in buy_targets:
            if px <= 0:
                continue
            if px >= (mid_price - 1e-9):
                continue
            if px in self.placed_buy_px_to_id:
                continue
            if not self._has_min_gap(self.placed_buy_px_to_id, px):
                continue
            if self.max_new_per_loop and new_buys >= self.max_new_per_loop:
                break
            await self._place_order(OrderSide.BUY, px)
            new_buys += 1
            await asyncio.sleep(self.op_spacing_sec)

        for px in sell_targets:
            if px in self.placed_sell_px_to_id:
                continue
            if not self._has_min_gap(self.placed_sell_px_to_id, px):
                continue
            if px <= (mid_price + 1e-9):
                continue
            if self.max_new_per_loop and new_sells >= self.max_new_per_loop:
                break
            await self._place_order(OrderSide.SELL, px)
            new_sells += 1
            await asyncio.sleep(self.op_spacing_sec)

        if not self.initialized:
            self.initialized = True
            logger.info("初回グリッド配置完了: 買い{}本 売り{}本", len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id))

    async def _place_order(self, side: OrderSide, price: float):
        """注文を発注"""
        req = OrderRequest(
            symbol=self.symbol,
            side=side,
            type=OrderType.LIMIT,
            quantity=self.size,
            price=price,
            time_in_force=TimeInForce.POST_ONLY,  # MAKER注文
        )

        try:
            if not self.simple_mode:
                try:
                    active = await self._list_active_orders_compat()
                except Exception:
                    active = []

                def _extract_px(row: dict) -> float | None:
                    try:
                        raw = row.get("price") or row.get("px") or row.get("0")
                        return float(raw) if raw is not None else None
                    except Exception:
                        return None

                for row in (active or []):
                    if not isinstance(row, dict):
                        continue
                    s = str(row.get("side") or row.get("orderSide") or "").upper()
                    if (side == OrderSide.BUY and s not in ("BUY", "LONG")) or (side == OrderSide.SELL and s not in ("SELL", "SHORT")):
                        continue
                    apx = _extract_px(row)
                    if apx is None:
                        continue
                    if abs(apx - price) < (self.step - 1e-9):
                        logger.debug("N間隔未満のためスキップ: side={} cand={} exist={}", side, price, apx)
                        return

            # 自己クロス防止
            if side == OrderSide.BUY and price in self.placed_sell_px_to_id:
                logger.debug("自己クロス回避: BUYスキップ price={}", price)
                return
            if side == OrderSide.SELL and price in self.placed_buy_px_to_id:
                logger.debug("自己クロス回避: SELLスキップ price={}", price)
                return

            order = await self.adapter.place_order(req)
            if side == OrderSide.BUY:
                self.placed_buy_px_to_id[price] = order.id
                logger.info("買い注文発注: price={} id={}", price, order.id)
            else:
                self.placed_sell_px_to_id[price] = order.id
                logger.info("売り注文発注: price={} id={}", price, order.id)

        except Exception as e:
            logger.error("注文発注エラー: side={} price={} err={}", side, price, e)

    async def _replenish_if_filled(self):
        """約定した注文を確認し、補充する"""
        if getattr(self, "bin_mode", False):
            return

        try:
            active_orders = await self._list_active_orders_compat()

            active_ids = set()
            for o in active_orders:
                try:
                    if isinstance(o, dict):
                        oid = (
                            o.get("orderId")
                            or o.get("id")
                            or o.get("order_id")
                            or o.get("clientOrderId")
                            or o.get("client_order_id")
                        )
                    else:
                        oid = getattr(o, "id", None) or getattr(o, "orderId", None)
                    if oid:
                        active_ids.add(str(oid))
                except Exception:
                    continue

            filled_buy_prices = []
            for px, oid in list(self.placed_buy_px_to_id.items()):
                if oid not in active_ids:
                    logger.info("買い注文約定: price={} id={}", px, oid)
                    filled_buy_prices.append(px)

            filled_sell_prices = []
            for px, oid in list(self.placed_sell_px_to_id.items()):
                if oid not in active_ids:
                    logger.info("売り注文約定: price={} id={}", px, oid)
                    filled_sell_prices.append(px)

            for px in filled_buy_prices:
                self.placed_buy_px_to_id.pop(px, None)
            for px in filled_sell_prices:
                self.placed_sell_px_to_id.pop(px, None)

            if filled_buy_prices or filled_sell_prices:
                logger.info("約定確認完了: filled_buy={} filled_sell={}", len(filled_buy_prices), len(filled_sell_prices))

            # --- アンカー方式補充（元コード維持） ---
            if filled_buy_prices:
                if self.placed_sell_px_to_id:
                    far_sell_px = max(self.placed_sell_px_to_id.keys())
                    far_sell_id = self.placed_sell_px_to_id.pop(far_sell_px)
                    try:
                        await self._cancel_order_compat(far_sell_id)
                    except Exception:
                        logger.debug("cancel far SELL failed (ignore): id={} px={}", far_sell_id, far_sell_px)
                    await asyncio.sleep(self.op_spacing_sec)

                base_near_sell = min(self.placed_sell_px_to_id.keys()) if self.placed_sell_px_to_id else (max(filled_buy_prices) + self.step)
                new_near_sell = base_near_sell - self.step
                if new_near_sell not in self.placed_sell_px_to_id and new_near_sell > 0:
                    await self._place_order(OrderSide.SELL, new_near_sell)
                    await asyncio.sleep(self.op_spacing_sec)

                base_outer_buy = min(self.placed_buy_px_to_id.keys()) if self.placed_buy_px_to_id else (min(filled_buy_prices) - self.step)
                new_outer_buy = base_outer_buy - self.step
                if new_outer_buy > 0 and new_outer_buy not in self.placed_buy_px_to_id:
                    await self._place_order(OrderSide.BUY, new_outer_buy)
                    await asyncio.sleep(self.op_spacing_sec)

            if filled_sell_prices:
                if self.placed_buy_px_to_id:
                    far_buy_px = min(self.placed_buy_px_to_id.keys())
                    far_buy_id = self.placed_buy_px_to_id.pop(far_buy_px)
                    try:
                        await self._cancel_order_compat(far_buy_id)
                    except Exception:
                        logger.debug("cancel far BUY failed (ignore): id={} px={}", far_buy_id, far_buy_px)
                    await asyncio.sleep(self.op_spacing_sec)

                base_near_buy = max(self.placed_buy_px_to_id.keys()) if self.placed_buy_px_to_id else (min(filled_sell_prices) - self.step)
                new_near_buy = base_near_buy + self.step
                if new_near_buy not in self.placed_buy_px_to_id and new_near_buy > 0:
                    await self._place_order(OrderSide.BUY, new_near_buy)
                    await asyncio.sleep(self.op_spacing_sec)

                base_outer_sell = max(self.placed_sell_px_to_id.keys()) if self.placed_sell_px_to_id else (max(filled_sell_prices) + self.step)
                new_outer_sell = base_outer_sell + self.step
                if new_outer_sell not in self.placed_sell_px_to_id:
                    await self._place_order(OrderSide.SELL, new_outer_sell)
                    await asyncio.sleep(self.op_spacing_sec)

        except Exception as e:
            logger.error("約定確認エラー: {}", e)
            return

        # 余剰オーダー整理
        if self.enforce_levels:
            try:
                placed_ids = set(self.placed_buy_px_to_id.values()) | set(self.placed_sell_px_to_id.values())

                def _oid(row: dict) -> str:
                    return str(row.get("orderId") or row.get("id") or row.get("order_id") or "")

                unknown = []
                for row in (active_orders or []):
                    if not isinstance(row, dict):
                        continue
                    oid = _oid(row)
                    if not oid or oid in placed_ids:
                        continue
                    status = str(row.get("status") or "").upper()
                    if status and status != "OPEN":
                        continue
                    unknown.append(oid)

                for oid in unknown[:3]:
                    try:
                        await self._cancel_order_compat(oid)
                        logger.info("余剰注文をキャンセル: id={}", oid)
                    except Exception:
                        logger.debug("余剰注文キャンセル失敗(無視): id={}", oid)
                    await asyncio.sleep(self.op_spacing_sec)
            except Exception as e:
                logger.debug("余剰整理スキップ: {}", e)

    async def _poll_closed_pnl_once(self):
        """定期的にクローズ済みPnLを取得"""
        if self.closed_poll_sec <= 0:
            return

        now = time.time()
        if now - self._last_closed_poll_ts < self.closed_poll_sec:
            return

        self._last_closed_poll_ts = now

        try:
            # 未実装（将来拡張用）
            pass
        except Exception as e:
            logger.error("クローズ済みPnL取得エラー: {}", e)
