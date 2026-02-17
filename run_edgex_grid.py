"""
run_edgex_grid.py

Repository root runner for GridEngine + EdgeXSDKAdapter.

- Reads env
- Initializes adapter (symbol/contract_id both supported)
- Starts GridEngine
"""

from __future__ import annotations

import asyncio
import os
from loguru import logger

from bot.adapters.edgex_sdk import EdgeXSDKAdapter
from bot.grid_engine import GridEngine


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


async def main() -> None:
    # Core env
    base_url = _env("EDGEX_BASE_URL", "https://pro.edgex.exchange")
    contract_id = _env("EDGEX_CONTRACT_ID", None)
    symbol = _env("EDGEX_SYMBOL", None) or _env("SYMBOL", None)  # optional fallback

    dry_run = _truthy(_env("DRY_RUN", "0")) or _truthy(_env("EDGEX_DRY_RUN", "0"))

    # Grid loop interval (GridEngine internally clamps to >= 1.5 sec)
    poll_sec_raw = _env("EDGEX_POLL_INTERVAL_SEC", _env("POLL_INTERVAL_SEC", "2.0"))
    try:
        poll_interval_sec = float(poll_sec_raw or "2.0")
    except Exception:
        poll_interval_sec = 2.0

    # Choose a "symbol-like" value for GridEngine:
    # - Prefer EDGEX_SYMBOL if present
    # - else use contract_id
    # GridEngine passes this into adapter.get_ticker/list_active_orders,
    # and adapter resolves both symbol/contract_id anyway.
    engine_symbol = symbol or contract_id
    if not engine_symbol:
        raise RuntimeError("Missing EDGEX_SYMBOL and EDGEX_CONTRACT_ID. Set at least one.")

    logger.info(
        "runner env: base_url={} symbol={} contract_id={} dry_run={} poll_interval_sec={}",
        base_url,
        symbol,
        contract_id,
        dry_run,
        poll_interval_sec,
    )

    adapter = EdgeXSDKAdapter(
        base_url=base_url,
        contract_id=contract_id,
        symbol=symbol,
        dry_run=dry_run,
    )

    engine = GridEngine(
        adapter=adapter,
        symbol=engine_symbol,
        poll_interval_sec=poll_interval_sec,
    )

    await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user (Ctrl+C).")
