"""Best-effort retry worker for the local-ledger Notion outbox.

Retries look up the stable local UUID before creating a page. Both target
databases must have a ``Transaction ID`` rich-text property; an incompatible
schema leaves the job pending instead of silently degrading idempotency.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from db import Database
from models import ExpenseEntry, IncomeEntry
from notion import NotionClient, _url_to_id

log = logging.getLogger(__name__)


class NotionSyncWorker:
    def __init__(
        self,
        db: Database,
        *,
        client_factory: Callable = NotionClient.from_user,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        poll_interval: float = 15.0,
        base_delay: float = 30.0,
        max_delay: float = 3600.0,
        batch_size: int = 20,
    ) -> None:
        self.db = db
        self.client_factory = client_factory
        self.clock = clock
        self.sleep = sleep
        self.jitter = jitter
        self.poll_interval = poll_interval
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.batch_size = batch_size

    def _next_attempt(self, attempt_count: int) -> str:
        delay = min(self.max_delay, self.base_delay * (2 ** attempt_count))
        delay = min(self.max_delay, delay + self.jitter(0.0, delay * 0.2))
        return (self.clock() + timedelta(seconds=delay)).isoformat()

    async def _fail(self, job: dict, error: str) -> None:
        await self.db.mark_notion_sync_failure(
            job["outbox_id"], error[:1000], self._next_attempt(job["attempt_count"])
        )
        await self._record_health(success=False, error=error)

    async def _record_health(
        self, *, success: bool, error: str | None = None, processed: int = 0
    ) -> None:
        record = getattr(self.db, "record_operational_state", None)
        if record is not None:
            await record(
                "notion_sync",
                success=success,
                error=error,
                metadata={"processed_jobs": processed},
            )

    async def _heartbeat(self, processed: int) -> None:
        record = getattr(self.db, "record_operational_heartbeat", None)
        if record is not None:
            await record(
                "notion_sync",
                metadata={
                    "processed_jobs": processed,
                    "poll_interval_seconds": self.poll_interval,
                },
            )

    async def process_job(self, job: dict) -> None:
        if job["kind"] not in {"expense", "income"}:
            await self._fail(job, f"unsupported Notion transaction kind: {job['kind']}")
            return
        operation = job["operation"]
        if operation not in {"upsert", "archive"}:
            await self._fail(job, f"unsupported Notion outbox operation: {operation}")
            return
        if operation == "archive" and job["status"] != "voided":
            await self._fail(job, f"transaction is not voided: {job['status']}")
            return
        if operation == "upsert" and job["status"] != "confirmed":
            await self._fail(job, f"transaction is not confirmed: {job['status']}")
            return

        user = await self.db.get_user(job["user_id"])
        if user is None or not user.notion_token:
            await self._fail(job, "Notion user configuration is missing")
            return

        client = self.client_factory(user)
        try:
            if operation == "archive":
                archive = getattr(client, "archive_transaction", None)
                if archive is not None:
                    page_id = await archive(
                        job["kind"], job["transaction_id"], job.get("notion_page_id")
                    )
                elif job.get("notion_page_id"):
                    # Compatibility for lightweight injected clients.
                    await client.archive_page(job["notion_page_id"])
                    page_id = job["notion_page_id"]
                else:
                    # Keep compatibility with small test/dry-run clients that
                    # expose the lookup primitive but not archive_transaction.
                    finder = getattr(client, "_find_by_transaction_id", None)
                    db_ids = getattr(client, "_db_ids", {})
                    db_key = {"expense": "expenses_ds", "income": "income_ds"}[job["kind"]]
                    if finder is None or db_key not in db_ids:
                        raise RuntimeError("Notion client cannot resolve archive target")
                    page_url, _supported = await finder(
                        db_ids[db_key], job["transaction_id"]
                    )
                    if page_url:
                        await client.archive_page(page_url)
                        page_id = _url_to_id(page_url)
                    else:
                        page_id = None
                await self.db.mark_notion_sync_success(job["outbox_id"], page_id)
                await self._record_health(success=True, processed=1)
                return
            cache = await client.load_cache()
            common = {
                "description": job["description"],
                "amount": float(job["amount_idr"]),
                "date": job["occurred_on"],
                "subcategory": job["subcategory"],
                "account": job["account"],
                "confidence": 1.0,
            }
            if job["kind"] == "expense":
                entry = ExpenseEntry(**common, merchant=job["merchant"])
                upsert = getattr(client, "upsert_expense", None)
                if upsert is not None:
                    url = await upsert(
                        entry,
                        user.owner_name,
                        cache,
                        job["transaction_id"],
                        recurring_page_url=(
                            job.get("recurring_page_id")
                            or job.get("recurring_page_url")
                        ),
                    )
                else:
                    # Compatibility for injected lightweight clients/fakes.
                    url = await client.log_expense(entry, user.owner_name, cache)
            else:
                entry = IncomeEntry(**common)
                upsert = getattr(client, "upsert_income", None)
                if upsert is not None:
                    url = await upsert(
                        entry, user.owner_name, cache, job["transaction_id"]
                    )
                else:
                    url = await client.log_income(entry, user.owner_name, cache)
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        await self.db.mark_notion_sync_success(job["outbox_id"], _url_to_id(url))
        await self._record_health(success=True, processed=1)

    async def run_once(self) -> int:
        try:
            jobs = await self.db.list_due_notion_sync_jobs(
                self.clock().isoformat(), limit=self.batch_size
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._record_health(
                success=False, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        for job in jobs:
            try:
                await self.process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Notion outbox job %s failed", job["outbox_id"])
                try:
                    await self._fail(job, f"{type(exc).__name__}: {exc}")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Could not record Notion outbox job failure")
        await self._heartbeat(len(jobs))
        return len(jobs)

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Notion outbox loop failed; retrying after poll interval")
            await self.sleep(self.poll_interval)
