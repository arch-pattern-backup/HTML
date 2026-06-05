"""
Intake Engine — ADR-017 D1, D4, D6

Canonical entry point for ingesting a media asset from any connector.
Connectors produce a RawIntakeBundle; the engine handles DB write, sidecar
dispatch, idempotency, and SHA256 re-verification.

DB-first contract (ADR-017 D1):
  1. Re-verify SHA256 from local_path bytes (never trust connector hash)
  2. Idempotency check: (sha256, owner_profile_uuid) → return existing media_id
  3. Write media DB row (failure aborts; no partial state)
  4. Dispatch sidecar write (best-effort; failure sets sidecar_written=false)
  5. BackgroundSidecarWorker handles sidecar_written=false rows asynchronously

TierAssignment is populated by the Pre-Flight Advisor (ADR-017 D5, item 6 in
the build order).  Until the Advisor is built, callers pass tier_assignments
directly.  The skeleton accepts them as a parameter so the call signature is
final before the Advisor exists — per ADR-017 co-iteration constraint.
"""

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RawIntakeBundle — connector output contract (ADR-017 D6)
# ---------------------------------------------------------------------------

@dataclass
class RawIntakeBundle:
    """
    Output of any intake connector.  The engine is the only consumer.

    Connectors MUST NOT touch S3 or the DB.  Their sole job is to produce
    this bundle from a local temp file.

    SHA256 note: the engine re-computes from ``local_path`` bytes and asserts
    against this value before any write.  Connector-provided hash is a hint,
    not a trust anchor.
    """
    local_path: str
    sha256: str
    size_bytes: int
    source_system: str
    source_job_id: Optional[str] = None
    source_url: Optional[str] = None
    suggested_title: Optional[str] = None
    suggested_artist: Optional[str] = None
    suggested_tags: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    mime_type: Optional[str] = None


# ---------------------------------------------------------------------------
# TierAssignment — output of the Pre-Flight Advisor (ADR-017 D5)
# ---------------------------------------------------------------------------

@dataclass
class TierAssignment:
    """
    Resolved storage destination for one tier of an asset.
    Produced by the Pre-Flight Advisor; consumed by the Ingestion Engine and
    NexusCatalog.register_asset().
    """
    tier: str
    backend: str
    bucket: str
    s3_key: str
    storage_config_id: uuid.UUID


# ---------------------------------------------------------------------------
# Ingestion Engine
# ---------------------------------------------------------------------------

class IntakeError(Exception):
    """Raised when intake cannot proceed (SHA256 mismatch, no storage config, etc.)."""


class IngestionEngine:
    """
    Registers an intake bundle as a media asset.

    Dependencies are injected so this class can be unit-tested without a live
    DB or S3.  In production, use ``IngestionEngine.from_env()``.
    """

    def __init__(self, db_conn, catalog=None):
        """
        Args:
            db_conn:  A synchronous SQLAlchemy connection (or compatible).
                      The engine uses raw SQL for fine-grained transaction control.
            catalog:  NexusCatalog instance.  If None, one is created on first use.
        """
        self._db = db_conn
        self._catalog = catalog

    @classmethod
    def from_env(cls, db_conn) -> "IngestionEngine":
        from .nexus_catalog import NexusCatalog
        return cls(db_conn=db_conn, catalog=NexusCatalog())

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def ingest(
        self,
        bundle: RawIntakeBundle,
        owner_profile_uuid: uuid.UUID,
        tier_assignments: Dict[str, TierAssignment],
        *,
        audio_format: Optional[Dict] = None,
        duration_seconds: float = 0.0,
        song_uuid: Optional[uuid.UUID] = None,
    ) -> uuid.UUID:
        """
        Ingest a bundle and return the media UUID.

        Steps (ADR-017 D1):
          1. Re-verify SHA256
          2. Idempotency check
          3. DB write
          4. Sidecar dispatch (best-effort)

        Raises IntakeError on SHA256 mismatch or missing storage config.
        Raises RuntimeError on DB failure (let it surface — don't swallow).
        """
        self._verify_sha256(bundle)
        existing = self._idempotency_check(bundle.sha256, owner_profile_uuid)
        if existing:
            log.info(f"IngestionEngine: duplicate intake {bundle.sha256[:12]}… → {existing}")
            return existing

        media_id = self._write_media_row(
            bundle=bundle,
            owner_profile_uuid=owner_profile_uuid,
            tier_assignments=tier_assignments,
            audio_format=audio_format,
            duration_seconds=duration_seconds,
            song_uuid=song_uuid,
        )

        self._dispatch_sidecar(
            bundle=bundle,
            media_id=media_id,
            tier_assignments=tier_assignments,
            owner_profile_uuid=owner_profile_uuid,
            audio_format=audio_format,
            duration_seconds=duration_seconds,
        )

        return media_id

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _verify_sha256(self, bundle: RawIntakeBundle) -> None:
        """Re-compute SHA256 from local_path bytes and assert against bundle.sha256."""
        h = hashlib.sha256()
        with open(bundle.local_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if actual != bundle.sha256:
            raise IntakeError(
                f"SHA256 mismatch for {bundle.local_path}: "
                f"connector reported {bundle.sha256}, actual {actual}. "
                f"Possible partial copy or connector bug."
            )

    def _idempotency_check(
        self, sha256: str, owner_profile_uuid: uuid.UUID
    ) -> Optional[uuid.UUID]:
        """Return existing media UUID if (sha256, owner) already registered, else None."""
        from sqlalchemy import text
        row = self._db.execute(
            text(
                "SELECT id FROM media "
                "WHERE intake_bundle_sha256 = :sha AND owner_profile_uuid = :owner "
                "LIMIT 1"
            ),
            {"sha": sha256, "owner": str(owner_profile_uuid)},
        ).fetchone()
        return uuid.UUID(str(row[0])) if row else None

    def _write_media_row(
        self,
        bundle: RawIntakeBundle,
        owner_profile_uuid: uuid.UUID,
        tier_assignments: Dict[str, TierAssignment],
        audio_format: Optional[Dict],
        duration_seconds: float,
        song_uuid: Optional[uuid.UUID],
    ) -> uuid.UUID:
        """
        Insert the media row.  Resolves storage_config_id from tier_assignments.
        Raises RuntimeError if hot tier has no storage_config_id.
        """
        from sqlalchemy import text
        from decimal import Decimal

        hot = tier_assignments.get("hot")
        if hot is None or hot.storage_config_id is None:
            raise IntakeError(
                f"IngestionEngine: hot tier assignment missing or has no storage_config_id "
                f"for owner {owner_profile_uuid}"
            )

        media_id = uuid.uuid4()
        self._db.execute(
            text("""
                INSERT INTO media (
                    id, type, s3_bucket, s3_key,
                    owner_profile_uuid, storage_config_id,
                    mime_type, size_bytes, duration_seconds,
                    title, artist,
                    song_uuid,
                    source_system, intake_bundle_sha256,
                    sidecar_written,
                    current_tier
                ) VALUES (
                    :id, 'audio', :bucket, :key,
                    :owner, :storage_config_id,
                    :mime_type, :size_bytes, :duration,
                    :title, :artist,
                    :song_uuid,
                    :source_system, :sha256,
                    false,
                    'hot'
                )
            """),
            {
                "id": str(media_id),
                "bucket": hot.bucket,
                "key": hot.s3_key,
                "owner": str(owner_profile_uuid),
                "storage_config_id": str(hot.storage_config_id),
                "mime_type": bundle.mime_type,
                "size_bytes": bundle.size_bytes,
                "duration": Decimal(str(duration_seconds)) if duration_seconds else None,
                "title": bundle.suggested_title,
                "artist": bundle.suggested_artist,
                "song_uuid": str(song_uuid) if song_uuid else None,
                "source_system": bundle.source_system,
                "sha256": bundle.sha256,
            },
        )
        log.info(f"IngestionEngine: media row written {media_id}")
        return media_id

    def _dispatch_sidecar(
        self,
        bundle: RawIntakeBundle,
        media_id: uuid.UUID,
        tier_assignments: Dict[str, TierAssignment],
        owner_profile_uuid: uuid.UUID,
        audio_format: Optional[Dict],
        duration_seconds: float,
    ) -> None:
        """
        Write sidecar via NexusCatalog.  On success, mark sidecar_written=true.
        On failure, log and leave sidecar_written=false for BackgroundSidecarWorker.
        This must never raise — it is best-effort.
        """
        from sqlalchemy import text
        if self._catalog is None:
            from .nexus_catalog import NexusCatalog
            hot = tier_assignments.get("hot")
            self._catalog = NexusCatalog(bucket=hot.bucket if hot else None)

        try:
            ok = self._catalog.register_asset(
                bundle=bundle,
                media_id=media_id,
                tier_assignments=tier_assignments,
                owner_hint=str(owner_profile_uuid),
                audio_format=audio_format,
                duration_seconds=duration_seconds,
            )
            if ok:
                self._db.execute(
                    text("UPDATE media SET sidecar_written = true WHERE id = :id"),
                    {"id": str(media_id)},
                )
                log.info(f"IngestionEngine: sidecar_written=true for {media_id}")
            else:
                log.warning(
                    f"IngestionEngine: sidecar write failed for {media_id} "
                    f"— BackgroundSidecarWorker will retry"
                )
        except Exception as e:
            log.warning(
                f"IngestionEngine: sidecar dispatch error for {media_id}: {e} "
                f"— BackgroundSidecarWorker will retry"
            )
