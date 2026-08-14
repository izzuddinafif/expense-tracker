"""Small versioned HTTP API for the personal Android client."""

import base64
import binascii
import hmac
import json
import logging
from dataclasses import asdict
from datetime import date
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


def _public_transaction(row: dict[str, Any]) -> dict[str, Any]:
    return {
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
        return web.json_response({"transaction": _public_transaction(row)})

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
            metadata = {
                "package_name": payload.get("package_name"),
                "notification_received_at": payload.get("received_at"),
            }
            if self_transfer:
                evidence = payload.get("transfer_evidence")
                if evidence is not None and not isinstance(evidence, dict):
                    raise ValueError("transfer_evidence must be an object")
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
                    evidence_scheme=evidence.get("scheme") if evidence else None,
                    evidence_reference=evidence.get("reference") if evidence else None,
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
                        if outcome.code == "awaiting_canonical_email"
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
            )
            confirmed = payload.get("confirm") is True
            if confirmed:
                row, _ = await db.confirm_transaction(user_id, row["id"])
        except SelfTransferMatchAmbiguousError as exc:
            return web.json_response(
                {"error": "self_transfer_match_ambiguous", "detail": str(exc)},
                status=409,
            )
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
    app.router.add_get("/api/v1/ops/health", operational_health)
    app.router.add_get("/api/v1/reconciliation", reconciliation)
    app.router.add_get("/api/v1/sync", sync_status)
    app.router.add_post("/api/v1/sync/retry", retry_sync)
    app.router.add_get("/api/v1/email-failures", email_failures)
    app.router.add_post("/api/v1/email-failures/{uid}/retry", retry_email_failure)
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
