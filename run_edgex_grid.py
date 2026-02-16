# run_edgex_grid.py
import os
import asyncio

from bot.grid_engine import GridEngine
from bot.adapters.edgex_sdk import EdgeXSDKAdapter


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v != "" else default


def _to_int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _mask(v: str | None) -> str:
    if not v:
        return "None"
    if len(v) <= 6:
        return "***"
    return v[:3] + "***" + v[-3:]


async def main():
    # ---- Read ENV ----
    base_url = _env("EDGEX_BASE_URL", "https://pro.edgex.exchange")

    contract_id_raw = _env("EDGEX_CONTRACT_ID")
    symbol = _env("EDGEX_SYMBOL")

    # fallback keys (よくある表記ゆれ救済)
    if contract_id_raw is None:
        contract_id_raw = _env("EDGEX_CONTRACTID")
    if symbol is None:
        symbol = _env("EDGEX_TICKER")

    contract_id = _to_int(contract_id_raw)

    dry_run = _env("DRY_RUN", "0") == "1"

    account_id = _env("EDGEX_ACCOUNT_ID")
    stark_pk = _env("EDGEX_STARK_PRIVATE_KEY")

    skip_auth = _env("EDGEX_SKIP_AUTH", "0") == "1"
    strict_maker = _env("EDGEX_STRICT_MAKER", "0") == "1"

    # ---- Log what we actually got ----
    print(
        "[BOOT] edgex base_url=%s contract_id=%s symbol=%s dry_run=%s skip_auth=%s strict_maker=%s account_id=%s stark_pk=%s"
        % (
            base_url,
            contract_id,
            symbol,
            dry_run,
            skip_auth,
            strict_maker,
            _mask(account_id),
            _mask(stark_pk),
        )
    )

    if contract_id is None and symbol is None:
        raise RuntimeError(
            "ENV missing: need EDGEX_CONTRACT_ID and/or EDGEX_SYMBOL. "
            "But both were None/invalid. Check Render Environment keys and deploy."
        )

    adapter = EdgeXSDKAdapter(
        contract_id=contract_id,
        symbol=symbol,
        base_url=base_url,
        dry_run=dry_run,
        account_id=account_id,
        stark_private_key=stark_pk,
        skip_auth=skip_auth,
        strict_maker=strict_maker,
    )

    # ✅ GridEngine は symbol 必須（あなたのログで確定）
    # 実装差分に強くするために、2パターン試す
    try:
        engine = GridEngine(symbol=symbol, adapter=adapter)
    except TypeError:
        engine = GridEngine(adapter=adapter, symbol=symbol)

    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
