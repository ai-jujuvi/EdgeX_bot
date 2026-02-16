from __future__ import annotations

import asyncio
from typing import List, Optional

from loguru import logger

from bot.models.types import OrderRequest, OrderSide


class GridEngine:
    """
    size / quantity 両対応版
    OrderRequest の内部仕様差異を完全吸収する
    """

    def __init__(self, adapter, symbol: str, levels: int, spacing: float, order_size: float):
        self.adapter = adapter
        self.symbol = symbol
        self.levels = levels
        self.spacing = spacing
        self.order_size = order_size

        self.placed_buy: List[float] = []
        self.placed_sell: List[float] = []

    # ------------------------------------------
    # 🔥 size互換取得
    # ------------------------------------------
    def _get_size(self, req: OrderRequest) -> float:
        if hasattr(req, "size"):
            return float(req.size)
        if hasattr(req, "quantity"):
            return float(req.quantity)
        raise ValueError("OrderRequest has neither size nor quantity")

    # ------------------------------------------
    async def run(self):
        while True:
            try:
                await self._loop()
            except Exception as e:
                logger.error(f"grid loop error: {e}")
            await asyncio.sleep(5)

    # ------------------------------------------
    async def _loop(self):
        ticker = await self.adapter.get_ticker(self.symbol)
        price = ticker.price

        logger.debug(f"loop price={price}")

        await self._place_levels(price)

    # ------------------------------------------
    async def _place_levels(self, price: float):
        for i in range(1, self.levels + 1):
            buy_price = price - i * self.spacing
            sell_price = price + i * self.spacing

            await self._place_order(OrderSide.BUY, buy_price)
            await self._place_order(OrderSide.SELL, sell_price)

    # ------------------------------------------
    async def _place_order(self, side: OrderSide, price: float):
        try:
            req = OrderRequest(
                symbol=self.symbol,
                side=side,
                price=price,
                quantity=self.order_size,  # quantityベースで統一
            )

            # 🔥 size互換吸収
            size_value = self._get_size(req)

            logger.debug(f"placing order side={side} price={price} size={size_value}")

            await self.adapter.place_order(req)

        except Exception as e:
            logger.error(f"注文発注エラー: side={side} price={price} error={e}")
