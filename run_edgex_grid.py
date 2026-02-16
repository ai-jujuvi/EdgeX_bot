import os
import asyncio
import logging

from bot.grid_engine import GridEngine
from bot.adapters.edgex_sdk import EdgeXSDKAdapter


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int | None = None) -> int | None:
    v = _env(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _mask(s: str | None, keep: int = 3) -> str:
    if not s:
        return "None"
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}***{s[-keep:]}"


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d - %(message)s",
    )
    log = logging.getLogger("__main__")

    base_url = _env("EDGEX_BASE_URL", "https://pro.edgex.exchange")
    contract_id = _env_int("EDGEX_CONTRACT_ID")
    symbol = _env("EDGEX_SYMBOL")  # <-- これを GridEngine に必ず渡す
    dry_run = _env_bool("DRY_RUN", False)

    strict_maker = _env_bool("EDGEX_STRICT_MAKER", True)

    # ✅ whitelist済みのものを入れる前提
    account_id = _env("EDGEX_ACCOUNT_ID")
    stark_private_key = _env("EDGEX_STARK_PRIVATE_KEY")

    # 重要：今回は「認証突破できる」前提なのでスキップは使わない
    skip_auth = _env_bool("EDGEX_SKIP_AUTH", False)
    if skip_auth:
        log.warning("EDGEX_SKIP_AUTH=1 は今回の前提と逆なので、0/削除を推奨します（動作が分岐します）")

    log.info(
        "[BOOT] base_url=%s contract_id=%s symbol=%s dry_run=%s strict_maker=%s account_id=%s stark_pk=%s",
        base_url,
        contract_id,
        symbol,
        dry_run,
        strict_maker,
        _mask(account_id),
        _mask(stark_private_key),
    )

    if contract_id is None:
        raise RuntimeError("EDGEX_CONTRACT_ID is required")
    if not symbol:
        raise RuntimeError("EDGEX_SYMBOL is required (e.g. XAUT-USD)")
    if not dry_run:
        # dry_runでないなら認証情報必須
        if not account_id:
            raise RuntimeError("EDGEX_ACCOUNT_ID is required for non-dry-run")
        if not stark_private_key:
            raise RuntimeError("EDGEX_STARK_PRIVATE_KEY is required for non-dry-run")

    adapter = EdgeXSDKAdapter(
        base_url=base_url,
        contract_id=contract_id,
        symbol=symbol,
        dry_run=dry_run,
        strict_maker=strict_maker,
        account_id=account_id,
        stark_private_key=stark_private_key,
    )

    # ✅ ここが今回のキモ：GridEngineに symbol を必ず渡す（missing symbol の根本対応）
    engine = GridEngine(adapter=adapter, symbol=symbol)

    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
