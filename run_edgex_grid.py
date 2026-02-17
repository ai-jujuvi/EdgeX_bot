import os
import asyncio
import yaml
from loguru import logger
from dotenv import load_dotenv
from urllib.parse import urlparse

from bot.adapters.edgex_sdk import EdgeXSDKAdapter
from bot.grid_engine import GridEngine


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


async def _optional_whitelist_check(account_id: str, cfg: dict) -> None:
    """
    GAS等のホワイトリスト認証（任意）。
    - EDGEX_WHITELIST_ENFORCE=1 のときだけ強制
    - それ以外は「警告ログ」だけ出して通す（本番停止を防ぐ）
    """
    enforce = _truthy(_env("EDGEX_WHITELIST_ENFORCE", str(cfg.get("whitelist_enforce", "0"))))
    auth_url = _env("EDGEX_WHITELIST_URL") or cfg.get("whitelist_url")

    if not auth_url:
        logger.info("whitelist check: skip (url not set)")
        return

    try:
        import httpx  # type: ignore

        logger.info("whitelist check: url={} account_id={}", auth_url, account_id)
        params = {"accountId": account_id}
        timeout = httpx.Timeout(6.0)

        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            r = await client.get(auth_url, params=params)
            r.raise_for_status()
            body = r.json()

        allowed_raw = body.get("allowed") if isinstance(body, dict) else None
        allowed = str(allowed_raw).lower() in ("1", "true", "yes")

        if allowed:
            logger.info("whitelist OK: account_id={}", account_id)
            return

        msg = f"whitelist NG: account_id={account_id} url={auth_url}"
        if enforce:
            raise SystemExit(msg)
        logger.warning(msg)

    except SystemExit:
        raise
    except Exception as e:
        msg = f"whitelist check failed: {e} (account_id={account_id} url={auth_url})"
        if enforce:
            raise SystemExit(msg)
        logger.warning(msg)


async def main() -> None:
    load_dotenv()

    # logs ディレクトリへファイル出力（全レベル）
    try:
        os.makedirs("logs", exist_ok=True)
        logger.add(
            os.path.join("logs", "run_edgex_grid.log"),
            level="DEBUG",
            rotation="10 MB",
            retention="14 days",
            encoding="utf-8",
            enqueue=True,
            backtrace=False,
            diagnose=False,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} - {message}",
        )
    except Exception:
        pass

    # 設定ファイルは任意（無ければ空dict）
    try:
        with open("configs/edgex.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}

    # URL
    base_url = _env("EDGEX_BASE_URL") or cfg.get("base_url") or "https://pro.edgex.exchange"
    parsed = urlparse(base_url or "")
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("EDGEX_BASE_URL が不正です（https://ホスト名 を設定してください）")
    if parsed.hostname and "example" in parsed.hostname:
        raise SystemExit("EDGEX_BASE_URL がプレースホルダです。実際のAPIベースURLに置き換えてください。")

    # 認証情報（基本はENV優先）
    account_id = (
        _env("EDGEX_ACCOUNT_ID")
        or _env("EDGEX_API_ID")
        or str(cfg.get("account_id") or cfg.get("api_id") or "")
    ).strip()

    stark_private_key = (_env("EDGEX_STARK_PRIVATE_KEY") or _env("EDGEX_L2_KEY") or "").strip()

    if not account_id:
        raise SystemExit("EDGEX_ACCOUNT_ID が未設定です")
    if not stark_private_key:
        raise SystemExit("EDGEX_STARK_PRIVATE_KEY (or EDGEX_L2_KEY) が未設定です")

    # 取引対象：contractId を最優先にする（XAUT系の事故を防ぐ）
    contract_id = (_env("EDGEX_CONTRACT_ID") or str(cfg.get("contract_id") or "")).strip() or None
    symbol = (_env("EDGEX_SYMBOL") or str(cfg.get("symbol") or "")).strip() or None

    # grid_engine に渡す「symbol」は、今の実装だと “contractId文字列” を渡すのが一番安全
    engine_symbol = contract_id or symbol or "10000001"

    logger.info(
        "boot: base_url={} account_id={} contract_id={} symbol={} engine_symbol={}",
        base_url,
        account_id,
        contract_id,
        symbol,
        engine_symbol,
    )

    # （任意）ホワイトリスト/GASチェック
    await _optional_whitelist_check(account_id=account_id, cfg=cfg)

    # ループ間隔
    poll_interval_raw = _env("EDGEX_POLL_INTERVAL_SEC") or str(cfg.get("poll_interval_sec", 2.5))
    try:
        poll_interval = float(poll_interval_raw)
    except Exception:
        poll_interval = 2.5
    if poll_interval < 1.5:
        poll_interval = 1.5

    # dry-run
    dry_run = _truthy(_env("EDGEX_DRY_RUN", str(cfg.get("dry_run", "0"))))

    # Adapter 初期化
    # NOTE: 今の edgex_sdk.py はENVから認証情報を読む設計なので、ENVに入っていればOK。
    # ただし contract_id/symbol/base_url/dry_run はここで渡しておく（互換のため）。
    adapter = EdgeXSDKAdapter(
        base_url=base_url,
        contract_id=contract_id,
        symbol=symbol,
        dry_run=dry_run,
    )

    engine = GridEngine(
        adapter=adapter,
        symbol=engine_symbol,
        poll_interval_sec=poll_interval,
    )

    await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("stopped by user")
