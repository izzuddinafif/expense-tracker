"""Small versioned HTTP API for the personal Android client."""

import base64
import binascii
import hmac
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web

from db import (
    Database,
    SelfTransferMatchAmbiguousError,
    SelfTransferMutationError,
    TransactionConflictError,
    TransactionPreconditionRequiredError,
)
from local_budgets import BudgetStore

log = logging.getLogger(__name__)

_PORTFOLIO_ACCOUNT_NAMES = ("Mandiri 1854", "BSI 9400", "Jago", "Cash")


def _portfolio_account_key(value: str) -> str:
    aliases = {
        "mandiri": "mandiri 1854",
        "mandiri 1854": "mandiri 1854",
        "bsi": "bsi 9400",
        "bsi 9400": "bsi 9400",
    }
    normalized = str(value or "").strip().casefold()
    return aliases.get(normalized, normalized)


def register_system_routes(app: web.Application, *, db: Database) -> None:
    """Register non-disclosing liveness routes used by the local runtime."""

    async def livez(_request: web.Request) -> web.Response:
        try:
            await db._conn.execute("SELECT 1")
        except Exception:
            log.exception("SQLite liveness check failed")
            return web.json_response({"status": "unhealthy"}, status=503)
        return web.json_response({"status": "ok"})

    app.router.add_get("/livez", livez)


async def _default_reconciliation(db: Database, user_id: int) -> dict[str, Any]:
    """Run a read-only ledger/Notion comparison for the configured API user."""
    from notion import NotionClient
    from reconciliation import reconcile_user_transactions

    user = await db.get_user(user_id)
    if user is None or user.setup_step != "done":
        raise ValueError("Notion setup is incomplete for the API user")
    client = NotionClient.from_user(user)
    try:
        report = await reconcile_user_transactions(db, client, user_id)
        payload = asdict(report)
        payload["is_clean"] = report.is_clean
        discrepancy_count = sum(
            len(value) for value in payload.values() if isinstance(value, (list, dict))
        )
        await db.record_operational_state(
            "reconciliation",
            success=True,
            metadata={
                "is_clean": report.is_clean,
                "discrepancy_count": discrepancy_count,
            },
        )
        return payload
    except Exception as exc:
        await db.record_operational_state(
            "reconciliation",
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        await client.aclose()


def _public_transaction(
    row: dict[str, Any], *, evidence: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "amount_idr": row["amount_idr"],
        "occurred_on": row["occurred_on"],
        "description": row["description"],
        "merchant": row["merchant"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "account": row["account"],
        "source": row["source"],
        "source_ref": row["source_ref"],
        "ledger_role": row["ledger_role"],
        "transfer_bundle_id": row["transfer_bundle_id"],
        "transfer_leg": row["transfer_leg"],
        "updated_at": row["updated_at"],
        "evidence_count": int(row.get("evidence_count", 0)),
    }
    if evidence is not None:
        result["evidence"] = evidence
        result["evidence_count"] = len(evidence)
    return result


def _public_local_asset(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["asset_type"],
        "value_idr": row["value_idr"],
        "quantity": row["quantity"],
        "unit": row["unit"],
        "last_updated": row["last_updated"],
        "notes": row["notes"],
        "source": "local",
        "is_liability": row["kind"] == "liability",
        # Kept for local-entry editing clients; the common asset contract is
        # the fields above, including is_liability.
        "kind": row["kind"],
        "updated_at": row["updated_at"],
    }


def _idr_number(value: Any) -> int | None:
    """Accept Notion numeric cells only when they represent whole rupiah."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if int(value) != value:
        return None
    return int(value)


async def _default_portfolio(db: Database, user_id: int) -> dict[str, Any]:
    """Read live Notion account formulas and combine durable local valuations."""
    from notion import NotionClient

    user = await db.get_user(user_id)
    if user is None or user.setup_step != "done":
        raise ValueError("Notion setup is incomplete for the API user")
    fetched_at = datetime.now(timezone.utc).isoformat()
    client = NotionClient.from_user(user)
    warnings: list[str] = []
    freshness = "live"
    accounts_complete = True
    assets_complete = True
    try:
        account_rows = await client.fetch_accounts()
        try:
            notion_assets = await client.fetch_assets()
        except Exception as exc:
            log.warning("Optional Notion assets fetch failed: %s", type(exc).__name__)
            notion_assets = []
            freshness = "partial"
            assets_complete = False
            warnings.append("Notion assets could not be fetched; local assets are still included.")
    finally:
        await client.aclose()

    accounts: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    total_liquid_idr = 0
    required_names = {name.casefold() for name in _PORTFOLIO_ACCOUNT_NAMES}
    for raw in account_rows:
        title = str(raw.get("title", "")).strip()
        if not title:
            continue
        normalized_name = title.casefold()
        if normalized_name not in required_names:
            warnings.append(f"Notion account '{title}' is not part of the configured portfolio and was excluded.")
            freshness = "partial"
            accounts_complete = False
            continue
        if normalized_name in seen_names:
            warnings.append(f"Duplicate Notion account '{title}' was excluded from totals.")
            freshness = "partial"
            accounts_complete = False
            continue
        seen_names.add(normalized_name)
        balance = _idr_number(raw.get("current_balance_idr"))
        account_type = str(raw.get("type", ""))
        accounts.append({
            "name": title,
            "type": account_type,
            "initial_amount_idr": _idr_number(raw.get("initial_amount_idr")),
            "balance_idr": balance,
            "total_income_idr": _idr_number(raw.get("total_income_idr")),
            "total_expenses_idr": _idr_number(raw.get("total_expenses_idr")),
            "source": "notion_accounts",
            "as_of": fetched_at,
        })
        if balance is None:
            freshness = "partial"
            accounts_complete = False
            warnings.append(f"Account '{title}' has no current balance and is excluded from totals.")
    for name in _PORTFOLIO_ACCOUNT_NAMES:
        if name.casefold() not in seen_names:
            accounts.append({
                "name": name, "type": "", "initial_amount_idr": None,
                "balance_idr": None, "total_income_idr": None,
                "total_expenses_idr": None, "source": "notion_accounts",
                "as_of": fetched_at,
            })
            warnings.append(f"Configured account '{name}' was not returned by Notion.")
            accounts_complete = False
            freshness = "partial"

    transfer_adjustments: dict[str, int] = {}
    for leg in await db.list_confirmed_self_transfer_legs(user_id):
        key = _portfolio_account_key(leg["account"])
        delta = int(leg["amount_idr"]) if leg["kind"] == "income" else -int(leg["amount_idr"])
        transfer_adjustments[key] = transfer_adjustments.get(key, 0) + delta
    if transfer_adjustments:
        warnings.append("Saldo akun mencakup penyesuaian transfer antar rekening dari ledger lokal.")
    total_liquid_idr = 0
    for account in accounts:
        balance = account["balance_idr"]
        if balance is None:
            continue
        key = _portfolio_account_key(account["name"])
        adjustment = transfer_adjustments.get(key, 0)
        account["balance_idr"] = balance + adjustment
        account["source"] = "notion_accounts+local_transfer_adjustments" if adjustment else "notion_accounts"
        if account["type"].casefold() not in {"liability", "debt"}:
            total_liquid_idr += account["balance_idr"]

    assets: list[dict[str, Any]] = []
    total_assets_idr = 0
    notion_asset_names: set[str] = set()
    for raw in notion_assets:
        value = _idr_number(raw.get("value_idr"))
        name = str(raw.get("name", "")).strip() or "Unnamed Notion asset"
        notion_asset_names.add(name.casefold())
        assets.append({
            "id": None, "name": name,
            "type": str(raw.get("type", "")), "value_idr": value,
            "quantity": raw.get("quantity"), "unit": str(raw.get("unit", "")),
            "last_updated": raw.get("last_updated") or fetched_at,
            "notes": str(raw.get("notes", "")), "source": "notion_assets",
            "is_liability": False,
        })
        if value is None:
            warnings.append(f"Asset '{name}' has no whole-IDR valuation and is excluded from totals.")
        else:
            total_assets_idr += value

    local_entries = await db.list_local_assets(user_id)
    total_liabilities_idr = 0
    for row in local_entries:
        public = _public_local_asset(row)
        assets.append(public)
        if public["name"].casefold() in notion_asset_names:
            assets_complete = False
            freshness = "partial"
            warnings.append(
                f"Asset '{public['name']}' exists in both Notion and local storage; totals are incomplete until it is linked."
            )
        value = public["value_idr"]
        if value is None:
            warnings.append(f"Local {public['kind']} '{public['name']}' has no valuation and is excluded from totals.")
        elif public["is_liability"]:
            total_liabilities_idr += value
        else:
            total_assets_idr += value

    reported_liquid = total_liquid_idr if accounts_complete else None
    reported_assets = total_assets_idr if assets_complete else None
    reported_net_worth = (
        reported_liquid + reported_assets - total_liabilities_idr
        if reported_liquid is not None and reported_assets is not None
        else None
    )
    return {
        "as_of": fetched_at,
        "source": "notion_accounts+notion_assets+local_assets",
        "freshness": freshness,
        "accounts": accounts,
        "assets": assets,
        "total_liquid_idr": reported_liquid,
        "total_assets_idr": reported_assets,
        "total_liabilities_idr": total_liabilities_idr,
        "net_worth_idr": reported_net_worth,
        "warnings": warnings,
    }


def _canonical_date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("occurred_on is required")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ValueError("occurred_on must be an ISO date") from exc


def _cursor_encode(token: str, user_id: int, row: dict[str, Any]) -> str:
    payload = json.dumps(
        {"u": user_id, "t": row["updated_at"], "i": row["id"]},
        separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(token.encode(), body, "sha256").digest()
    sig = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{body.decode()}.{sig.decode()}"


def _cursor_decode(value: str, token: str, user_id: int) -> tuple[str, str]:
    try:
        body_text, sig_text = value.split(".", 1)
        if not body_text or not sig_text or any(c not in "-_" + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in body_text + sig_text):
            raise ValueError
        body = body_text.encode()
        expected = base64.urlsafe_b64encode(hmac.new(token.encode(), body, "sha256").digest()).rstrip(b"=")
        provided = sig_text.encode()
        if not hmac.compare_digest(provided, expected):
            raise ValueError
        raw = base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("u") != user_id or not isinstance(payload.get("t"), str) or not isinstance(payload.get("i"), str):
            raise ValueError
        return payload["t"], payload["i"]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid cursor") from exc


def register_api_routes(
    app: web.Application,
    *,
    db: Database,
    token: str,
    user_id: int,
    max_body_bytes: int = 65_536,
    reconciler: Callable[[Database, int], Awaitable[dict[str, Any]]] | None = None,
    portfolio_reader: Callable[[Database, int], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    """Register authenticated `/api/v1` routes on an aiohttp application."""
    if not token or user_id <= 0:
        raise ValueError("API token and positive user ID are required")
    budgets = BudgetStore(db)

    def authorized(request: web.Request) -> bool:
        value = request.headers.get("Authorization", "")
        provided = value[7:] if value.startswith("Bearer ") else ""
        return bool(provided) and hmac.compare_digest(provided, token)

    async def require_auth(request: web.Request) -> web.Response | None:
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    async def read_json(request: web.Request) -> dict[str, Any]:
        raw = await request.content.read(max_body_bytes + 1)
        if len(raw) > max_body_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=max_body_bytes, actual_size=len(raw)
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text="Malformed JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return payload

    async def health(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        await db._conn.execute("SELECT 1")
        return web.json_response({"status": "ok", "api_version": 1})

    async def list_transactions(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            limit = int(request.query.get("limit", "50"))
            rows = await db.list_transactions(
                user_id, limit=limit, status=request.query.get("status")
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"transactions": [_public_transaction(r) for r in rows]})

    async def transaction_changes(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            limit = int(request.query.get("limit", "50"))
            cursor = request.query.get("cursor")
            after = _cursor_decode(cursor, token, user_id) if cursor else (None, None)
            rows = await db.list_transaction_changes(
                user_id,
                limit=limit,
                after_updated_at=after[0],
                after_id=after[1],
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _cursor_encode(token, user_id, rows[-1]) if has_more and rows else None
        checkpoint_cursor = _cursor_encode(token, user_id, rows[-1]) if rows else None
        return web.json_response({
            "transactions": [_public_transaction(r) for r in rows],
            "next_cursor": next_cursor,
            "checkpoint_cursor": checkpoint_cursor,
        })

    async def get_transaction(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        row = await db.find_transaction_by_id(
            user_id, request.match_info["transaction_id"]
        )
        if row is None:
            return web.json_response({"error": "not_found"}, status=404)
        evidence = await db.get_transaction_evidence(user_id, row["id"])
        return web.json_response({"transaction": _public_transaction(row, evidence=evidence)})

    async def create_transaction(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        payload = await read_json(request)
        source_ref = request.headers.get("Idempotency-Key") or str(
            payload.get("source_ref", "")
        )
        try:
            amount = payload.get("amount_idr")
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise ValueError("amount_idr must be a positive integer")
            occurred_on = _canonical_date(payload["occurred_on"])
            kind = payload.get("kind", "expense")
            if kind == "transfer":
                raise ValueError("Transfer transactions are not supported by Notion sync")
            if kind not in {"expense", "income"}:
                raise ValueError("Invalid transaction kind")
            account = str(payload.get("account", ""))
            account = {
                "LIVIN_MANDIRI": "Mandiri",
                "JAGO": "Jago",
                "BSI": "BSI",
            }.get(account, account)
            source = payload.get("source", "android_notification")
            if not isinstance(source, str) or source not in {
                "android_notification",
                "manual",
            }:
                raise ValueError("Invalid transaction source")
            self_transfer = payload.get("self_transfer", False)
            if not isinstance(self_transfer, bool):
                raise ValueError("self_transfer must be a boolean")
            transfer_evidence = payload.get("transfer_evidence")
            if transfer_evidence is not None and not isinstance(transfer_evidence, dict):
                raise ValueError("transfer_evidence must be an object")
            bank_reference = payload.get("bank_reference")
            if bank_reference is None and transfer_evidence is not None:
                if transfer_evidence.get("scheme", "bank_reference") != "bank_reference":
                    raise ValueError("Unsupported transaction evidence scheme")
                bank_reference = transfer_evidence.get("reference")
            if bank_reference is not None and not isinstance(bank_reference, str):
                raise ValueError("bank_reference must be a string")
            metadata = {
                "package_name": payload.get("package_name"),
                "notification_received_at": payload.get("received_at"),
            }
            if self_transfer:
                outcome = await db.ingest_android_self_transfer(
                    user_id,
                    kind=kind,
                    amount_idr=amount,
                    occurred_on=occurred_on,
                    description=str(payload.get("description", "")),
                    merchant=str(payload.get("merchant", "")),
                    category=str(payload.get("category", "")),
                    subcategory=str(payload.get("subcategory", "")),
                    account=account,
                    source_ref=source_ref,
                    evidence_scheme="bank_reference" if bank_reference is not None else None,
                    evidence_reference=bank_reference,
                    metadata=metadata,
                )
                return web.json_response(
                    {
                        "transaction": _public_transaction(outcome.transaction),
                        "created": outcome.created,
                        "ingestion_outcome": {
                            "code": outcome.code,
                            "action": outcome.action,
                        },
                    },
                    status=(
                        202
                        if outcome.code in {"awaiting_canonical_email", "ambiguous_candidates"}
                        else 409
                        if outcome.code == "evidence_conflict"
                        else 200
                    ),
                )
            row, created = await db.create_ingested_transaction(
                user_id,
                kind=kind,
                amount_idr=amount,
                occurred_on=occurred_on,
                description=str(payload.get("description", "")),
                merchant=str(payload.get("merchant", "")),
                category=str(payload.get("category", "")),
                subcategory=str(payload.get("subcategory", "")),
                account=account,
                source_ref=source_ref,
                source=source,
                metadata=metadata,
                bank_reference=bank_reference,
            )
            confirmed = payload.get("confirm") is True
            if confirmed:
                row, _ = await db.confirm_transaction(user_id, row["id"])
        except SelfTransferMatchAmbiguousError as exc:
            return web.json_response(
                {"error": "self_transfer_match_ambiguous", "detail": str(exc)},
                status=409,
            )
        except TransactionConflictError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (KeyError, TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(
            {
                "transaction": _public_transaction(row),
                "created": created,
                "ingestion_outcome": {
                    "code": "created" if created else "replayed",
                    "action": "finalize" if confirmed else "keep_review",
                },
            },
            status=201 if created else 200,
        )

    async def portfolio(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            return web.json_response(await (portfolio_reader or _default_portfolio)(db, user_id))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception:
            log.exception("Live portfolio read failed")
            return web.json_response(
                {"error": "portfolio_source_unavailable"}, status=502
            )

    async def list_assets(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        return web.json_response({"assets": [_public_local_asset(row) for row in await db.list_local_assets(user_id)]})

    async def create_asset(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            payload = await read_json(request)
            if "kind" not in payload and "is_liability" in payload:
                payload["kind"] = "liability" if payload["is_liability"] else "asset"
            row = await db.create_local_asset(
                user_id, name=payload.get("name", ""), kind=payload.get("kind"),
                asset_type=payload.get("type", ""), value_idr=payload.get("value_idr"),
                quantity=payload.get("quantity"), unit=payload.get("unit", ""),
                notes=payload.get("notes", ""), last_updated=payload.get("last_updated"),
                as_of=payload.get("as_of"),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"asset": _public_local_asset(row)}, status=201)

    async def update_asset(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            payload = await read_json(request)
            if "type" in payload:
                payload["asset_type"] = payload.pop("type")
            if "kind" not in payload and "is_liability" in payload:
                payload["kind"] = "liability" if payload.pop("is_liability") else "asset"
            row = await db.update_local_asset(user_id, request.match_info["asset_id"], payload)
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if row is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"asset": _public_local_asset(row)})

    async def delete_asset(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        deleted = await db.delete_local_asset(user_id, request.match_info["asset_id"])
        if not deleted:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"deleted": True})

    async def confirm_transaction(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            row, changed = await db.confirm_transaction(
                user_id, request.match_info["transaction_id"]
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if row is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(
            {"transaction": _public_transaction(row), "changed": changed}
        )

    async def update_transaction(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            changes = await read_json(request)
            expected_updated_at = changes.pop("expected_updated_at", None)
            if expected_updated_at is not None and not isinstance(expected_updated_at, str):
                raise ValueError("expected_updated_at must be a string")
            if "occurred_on" in changes:
                changes["occurred_on"] = _canonical_date(changes["occurred_on"])
            row, changed = await db.update_transaction(
                user_id,
                request.match_info["transaction_id"],
                changes,
                expected_updated_at=expected_updated_at,
                require_expected_revision=True,
            )
        except TransactionPreconditionRequiredError as exc:
            return web.json_response({"error": str(exc)}, status=428)
        except TransactionConflictError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except SelfTransferMutationError as exc:
            return web.json_response(
                {"error": "self_transfer_bundle_mutation_rejected", "detail": str(exc)},
                status=409,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if row is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(
            {"transaction": _public_transaction(row), "changed": changed}
        )

    async def void_transaction(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        expected_updated_at = request.headers.get("If-Match")
        try:
            row, changed = await db.void_transaction(
                user_id,
                request.match_info["transaction_id"],
                expected_updated_at=expected_updated_at,
                require_expected_revision=True,
            )
        except TransactionPreconditionRequiredError as exc:
            return web.json_response({"error": str(exc)}, status=428)
        except TransactionConflictError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except SelfTransferMutationError as exc:
            return web.json_response(
                {"error": "self_transfer_bundle_mutation_rejected", "detail": str(exc)},
                status=409,
            )
        if row is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(
            {"transaction": _public_transaction(row), "changed": changed}
        )

    async def sync_status(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        return web.json_response(await db.get_notion_sync_status(user_id))

    async def operational_health(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        return web.json_response(await db.get_operational_health(user_id))

    async def reconciliation(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            payload = await (reconciler or _default_reconciliation)(db, user_id)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            log.exception("Read-only reconciliation failed")
            return web.json_response(
                {"error": f"{type(exc).__name__}: {exc}"}, status=502
            )
        return web.json_response(payload)

    async def retry_sync(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        return web.json_response({"retried": await db.retry_notion_sync(user_id)})

    async def email_failures(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            return web.json_response({"error": "invalid limit"}, status=400)
        rows = await db.list_email_processing_failures(limit=limit)
        return web.json_response({"failures": rows})

    async def retry_email_failure(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        uid = request.match_info["uid"]
        if not uid or len(uid) > 255:
            return web.json_response({"error": "invalid UID"}, status=400)
        cleared = await db.clear_email_processing_failure(uid)
        if not cleared:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"retried": True, "uid": uid})

    async def dismiss_email_failure(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        uid = request.match_info["uid"]
        if not uid or len(uid) > 255:
            return web.json_response({"error": "invalid UID"}, status=400)
        dismissed = await db.dismiss_email_processing_failure(uid)
        if not dismissed:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"dismissed": True, "uid": uid})

    def public_budget(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "month": row["month"],
            "category": row["category"],
            "amount_idr": row["budget_idr"],
            "spent_idr": row["spent_idr"],
            "remaining_idr": row["remaining_idr"],
            "percentage": row["percentage"],
            "status": row["status"],
        }

    def budget_response(month: str, report: list[dict[str, Any]]) -> web.Response:
        """Keep all budget reads and mutations on one stable response shape."""
        return web.json_response(
            {"month": month, "budgets": [public_budget(row) for row in report]}
        )

    async def list_budgets(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        month = request.query.get("month", date.today().strftime("%Y-%m"))
        try:
            report = await budgets.report(user_id, month)
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return budget_response(month, report)

    async def set_budget(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        try:
            payload = await read_json(request)
            month = payload["month"]
            category = payload["category"]
            amount_idr = payload["amount_idr"]
            if isinstance(amount_idr, bool) or not isinstance(amount_idr, int):
                raise ValueError("amount_idr must be a positive integer")
            await budgets.set(user_id, month, category, amount_idr)
            report = await budgets.report(user_id, month)
        except (KeyError, TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return budget_response(month, report)

    async def delete_budget(request: web.Request) -> web.Response:
        denied = await require_auth(request)
        if denied is not None:
            return denied
        month = request.query.get("month", date.today().strftime("%Y-%m"))
        category = request.query.get("category")
        try:
            deleted = await budgets.delete(user_id, month, category)
            report = await budgets.report(user_id, month)
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(
            {
                "month": month,
                "category": category,
                "deleted": deleted,
                "budgets": [public_budget(row) for row in report],
            }
        )

    app.router.add_get("/api/v1/health", health)
    app.router.add_get("/api/v1/portfolio", portfolio)
    app.router.add_get("/api/v1/assets", list_assets)
    app.router.add_post("/api/v1/assets", create_asset)
    app.router.add_patch("/api/v1/assets/{asset_id}", update_asset)
    app.router.add_delete("/api/v1/assets/{asset_id}", delete_asset)
    app.router.add_get("/api/v1/ops/health", operational_health)
    app.router.add_get("/api/v1/reconciliation", reconciliation)
    app.router.add_get("/api/v1/sync", sync_status)
    app.router.add_post("/api/v1/sync/retry", retry_sync)
    app.router.add_get("/api/v1/email-failures", email_failures)
    app.router.add_post("/api/v1/email-failures/{uid}/retry", retry_email_failure)
    app.router.add_post("/api/v1/email-failures/{uid}/dismiss", dismiss_email_failure)
    app.router.add_get("/api/v1/budgets", list_budgets)
    app.router.add_put("/api/v1/budgets", set_budget)
    app.router.add_delete("/api/v1/budgets", delete_budget)
    app.router.add_get("/api/v1/transactions", list_transactions)
    app.router.add_get("/api/v1/transactions/changes", transaction_changes)
    app.router.add_get(
        "/api/v1/transactions/{transaction_id}", get_transaction
    )
    app.router.add_post("/api/v1/transactions", create_transaction)
    app.router.add_patch(
        "/api/v1/transactions/{transaction_id}/confirm", confirm_transaction
    )
    app.router.add_patch(
        "/api/v1/transactions/{transaction_id}", update_transaction
    )
    app.router.add_delete(
        "/api/v1/transactions/{transaction_id}", void_transaction
    )
