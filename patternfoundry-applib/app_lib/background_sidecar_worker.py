"""
BackgroundSidecarWorker — ADR-017 D1

Polls ``media.sidecar_written = false`` and retries the sidecar write via
NexusCatalog.  Runs as a daemon thread inside the backend process.

Contract (ADR-017 D1):
- ``sidecar_written = false`` is a *portability-pending* state, not an error.
  The asset is fully functional.  This worker makes it eventually consistent.
- Exponential backoff per row: base 30s, doubling to MAX_BACKOFF_SECONDS cap.
  Backoff state is tracked in-memory; a process restart resets it (acceptable —
  the worker will simply retry sooner after restart).
- No row is abandoned: if a sidecar write fails repeatedly, the row stays
  ``sidecar_written = false`` indefinitely and keeps getting retried at the
  capped interval.  Operators can inspect via:
      SELECT id, source_system, created_at FROM media
      WHERE sidecar_written = false ORDER BY created_at;
- The worker requires a DB connection factory and a NexusCatalog factory,
  both injected at construction time.

Usage (in FastAPI lifespan or app startup):
    worker = BackgroundSidecarWorker(
        db_engine=engine,
        catalog_factory=lambda bucket: NexusCatalog(bucket=bucket),
    )
    worker.start()   # starts daemon thread; stops when process exits
    # or: worker.stop()  # for graceful shutdown
"""

import logging
import threading
import time
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 30
BASE_BACKOFF = 30
MAX_BACKOFF_SECONDS = 3600
BATCH_SIZE = 50


class BackgroundSidecarWorker:
    """
    Daemon thread that retries sidecar writes for media rows where
    ``sidecar_written = false``.

    Args:
        db_engine:       SQLAlchemy engine (sync).
        catalog_factory: Callable[bucket: str] → NexusCatalog instance.
        poll_interval:   Seconds between full scans when there is nothing to do.
    """

    def __init__(
        self,
        db_engine,
        catalog_factory: Callable[[Optional[str]], object],
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ):
        self._engine = db_engine
        self._catalog_factory = catalog_factory
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Per media-id backoff state: {media_id_str: next_retry_at (float epoch)}
        self._backoff: Dict[str, float] = {}
        # Per media-id failure count for backoff calculation
        self._failures: Dict[str, int] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="BackgroundSidecarWorker",
            daemon=True,
        )
        self._thread.start()
        log.info("BackgroundSidecarWorker: started")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        log.info("BackgroundSidecarWorker: stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._process_batch()
            except Exception as e:
                log.error(f"BackgroundSidecarWorker: unexpected error in poll loop: {e}")
            self._stop_event.wait(timeout=self._poll_interval)

    def _process_batch(self) -> None:
        from sqlalchemy import text

        now = time.time()
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, s3_bucket, s3_key, source_system,
                           intake_bundle_sha256, title, owner_profile_uuid
                    FROM media
                    WHERE sidecar_written = false
                    ORDER BY created_at ASC
                    LIMIT :limit
                """),
                {"limit": BATCH_SIZE},
            ).fetchall()

        if not rows:
            return

        log.info(f"BackgroundSidecarWorker: {len(rows)} pending sidecar(s)")

        for row in rows:
            media_id = str(row[0])
            next_retry = self._backoff.get(media_id, 0.0)
            if now < next_retry:
                continue
            self._attempt_sidecar(row)

    def _attempt_sidecar(self, row) -> None:
        from sqlalchemy import text
        from .nexus_catalog import NexusCatalog
        from .intake_engine import RawIntakeBundle, TierAssignment
        import uuid as _uuid

        media_id_str = str(row[0])
        bucket = row[1]
        s3_key = row[2]
        source_system = row[3] or "unknown"
        sha256 = row[4] or ""
        title = row[5]

        log.info(f"BackgroundSidecarWorker: retrying sidecar for {media_id_str}")

        try:
            catalog: NexusCatalog = self._catalog_factory(bucket)

            # Reconstruct a minimal bundle from DB data — provenance is in the
            # sidecar of a successful write; for retries we have what the DB has.
            bundle = RawIntakeBundle(
                local_path="",       # not needed for sidecar-only retry
                sha256=sha256,
                size_bytes=0,
                source_system=source_system,
                suggested_title=title,
            )

            # Hot tier is the only guaranteed assignment available from the DB row
            hot = TierAssignment(
                tier="hot",
                backend="minio",
                bucket=bucket,
                s3_key=s3_key,
                storage_config_id=_uuid.uuid4(),  # not used by catalog
            )
            tier_assignments = {"hot": hot}

            ok = catalog.register_asset(
                bundle=bundle,
                media_id=_uuid.UUID(media_id_str),
                tier_assignments=tier_assignments,
                owner_hint=str(row[6]) if row[6] else None,
            )

            if ok:
                with self._engine.begin() as conn:
                    conn.execute(
                        text("UPDATE media SET sidecar_written = true WHERE id = :id"),
                        {"id": media_id_str},
                    )
                log.info(f"BackgroundSidecarWorker: sidecar_written=true for {media_id_str}")
                # Clear backoff state on success
                self._backoff.pop(media_id_str, None)
                self._failures.pop(media_id_str, None)
            else:
                self._record_failure(media_id_str)

        except Exception as e:
            log.warning(f"BackgroundSidecarWorker: attempt failed for {media_id_str}: {e}")
            self._record_failure(media_id_str)

    def _record_failure(self, media_id_str: str) -> None:
        failures = self._failures.get(media_id_str, 0) + 1
        self._failures[media_id_str] = failures
        backoff = min(BASE_BACKOFF * (2 ** (failures - 1)), MAX_BACKOFF_SECONDS)
        self._backoff[media_id_str] = time.time() + backoff
        log.warning(
            f"BackgroundSidecarWorker: {media_id_str} failure #{failures}, "
            f"next retry in {backoff}s"
        )
