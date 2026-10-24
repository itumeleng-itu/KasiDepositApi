"""The payout worker: advances in-flight deposits that nobody is polling.

Without it a deposit moves only when the app reads its status, so one whose
phone went flat stays `submitted` forever. Every PAYOUT_WORKER_INTERVAL_SECONDS
it finds `charged` and `paying` deposits that have not changed for
PAYOUT_WORKER_MIN_AGE_SECONDS and calls `DepositService.advance` on each: the
same code path as the status read, so there is one state machine, not two.

Off by default (PAYOUT_WORKER_ENABLED). It runs on a daemon thread inside the
web process, started and stopped with the app.

Safe with more than one of it
-----------------------------
Every instance (and every uvicorn worker) runs its own. They cannot clash:
`advance` takes a Postgres advisory lock per deposit and skips one that is
held, and below that every write re-checks the deposit's status under its row
lock. Killing a pass at any point is safe for the same reasons; the comment
above `DepositService.advance` walks through each crash point.
"""

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.deposits import IN_FLIGHT, DepositService
from app.models import Deposit

logger = logging.getLogger("app.worker")


class PayoutWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        deposits: DepositService,
        interval_seconds: float,
        min_age_seconds: int,
        batch_size: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.deposits = deposits
        self.interval_seconds = interval_seconds
        self.min_age = timedelta(seconds=min_age_seconds)
        self.batch_size = batch_size
        self.now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        """One pass. Returns how many deposits it advanced (held elsewhere: skipped)."""
        advanced = 0
        with self.session_factory() as session:
            ids = self._due(session)
            for deposit_id in ids:
                if self._stop.is_set():
                    break
                try:
                    if self.deposits.advance(session, deposit_id):
                        advanced += 1
                except Exception as exc:  # noqa: BLE001 - one bad deposit must not stop the rest
                    session.rollback()
                    # The type only: an exception's text could carry what it was handling.
                    logger.error("advancing deposit %s failed (%s); will retry", deposit_id, type(exc).__name__)
        return advanced

    def _due(self, session: Session) -> list[uuid.UUID]:
        ids = list(
            session.scalars(
                select(Deposit.id)
                .where(Deposit.status.in_(IN_FLIGHT), Deposit.updated_at <= self.now() - self.min_age)
                .order_by(Deposit.updated_at)
                .limit(self.batch_size)
            )
        )
        session.rollback()  # end the read; `advance` manages its own transactions
        return ids

    # --- the thread --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="payout-worker", daemon=True)
        self._thread.start()
        logger.info("payout worker started: every %ss", self.interval_seconds)

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the loop to finish its current deposit and return. Waits up to `timeout`."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        logger.info("payout worker stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - e.g. the database is unreachable: try next time
                logger.error("payout worker pass failed (%s); retrying next interval", type(exc).__name__)
            self._stop.wait(self.interval_seconds)
