import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional, Dict, List, TYPE_CHECKING
from .storage import MediaStore

if TYPE_CHECKING:
    from .intake_engine import RawIntakeBundle, TierAssignment

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".toneroot.json"
SCHEMA_VERSION = "2.0.0"


class NexusCatalog:
    """
    S3-backed portable identity layer for ToneRoot/StoryLoom assets.

    Writes a companion ``<key>.toneroot.json`` sidecar alongside each audio
    object.  The sidecar is the *passport* for that asset — scanning a bucket
    for ``*.toneroot.json`` files is sufficient to reconstruct a media DB from
    scratch (cross-instance import, disaster recovery).

    Schema version: 2.0.0  (ADR-017 D2)

    The canonical entry point for the Ingestion Engine is ``register_asset()``.
    ``register_generation()`` is retained as a deprecated shim.
    """

    def __init__(self, bucket: Optional[str] = None, s3_config: Optional[Dict] = None):
        self.bucket = bucket or os.getenv("NEXUS_BUCKET", "music")
        self.store = MediaStore(bucket=self.bucket)

    # ------------------------------------------------------------------
    # Public API (v2)
    # ------------------------------------------------------------------

    def register_asset(
        self,
        bundle: "RawIntakeBundle",
        media_id: _uuid.UUID,
        tier_assignments: Dict[str, "TierAssignment"],
        *,
        owner_hint: Optional[str] = None,
        trust_hint: Optional[List[str]] = None,
        audio_format: Optional[Dict] = None,
        duration_seconds: float = 0.0,
    ) -> bool:
        """
        Write the v2 portable sidecar for an ingested asset.

        Called by the Ingestion Engine *after* the media DB row is committed
        (DB-first contract, ADR-017 D1).  Failure here does not roll back the
        DB row — the caller must set ``media.sidecar_written = false`` and
        rely on BackgroundSidecarWorker to retry.

        Args:
            bundle:           The RawIntakeBundle produced by the connector.
            media_id:         The UUID of the already-committed media DB row.
            tier_assignments: Mapping of tier name → TierAssignment (hot/warm/cold).
            owner_hint:       Profile UUID string — suggestion only, never authoritative.
            trust_hint:       List of grantee UUID strings — suggestions only.
            audio_format:     Optional dict with sample_rate/channels/bit_depth/codec.
            duration_seconds: Asset duration.

        Returns:
            True on successful sidecar write, False on failure.
        """
        hot = tier_assignments.get("hot")
        if hot is None:
            log.error(f"NexusCatalog.register_asset: no hot tier assignment for {media_id}")
            return False

        audio_key = hot.s3_key
        base_key = audio_key.rsplit(".", 1)[0] if "." in audio_key else audio_key
        sidecar_key = base_key + SIDECAR_SUFFIX

        tiers_payload: Dict[str, Dict] = {}
        for tier_name, assignment in tier_assignments.items():
            tiers_payload[tier_name] = {
                "backend": assignment.backend,
                "bucket": assignment.bucket,
                "key": assignment.s3_key,
            }

        sidecar = {
            "schema_version": SCHEMA_VERSION,
            "id": str(media_id),
            "title": bundle.suggested_title,
            "source_system": bundle.source_system,
            "source_job_id": bundle.source_job_id,
            "intake_sha256": bundle.sha256,
            "audio_key": audio_key,
            "master_key": tier_assignments["warm"].s3_key if "warm" in tier_assignments else None,
            "archive_key": tier_assignments["cold"].s3_key if "cold" in tier_assignments else None,
            "tiers": tiers_payload,
            "owner_hint": owner_hint,
            "trust_hint": trust_hint or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tags": bundle.suggested_tags,
            "provenance": bundle.provenance,
            "audio_format": audio_format or {},
            "duration_seconds": duration_seconds,
            "content_length_bytes": bundle.size_bytes,
        }

        log.info(f"NexusCatalog: Writing sidecar {sidecar_key} for media {media_id}")
        try:
            result = self.store.persist_object(
                file_obj=json.dumps(sidecar, indent=2).encode("utf-8"),
                object_name=sidecar_key,
                content_type="application/json",
            )
            if result:
                log.info(f"NexusCatalog: Sidecar written for {media_id}")
                return True
            log.error(f"NexusCatalog: persist_object returned None for {sidecar_key}")
            return False
        except Exception as e:
            log.error(f"NexusCatalog: Failed to write sidecar for {media_id}: {e}")
            return False

    def sidecar_key_for(self, audio_key: str) -> str:
        """Return the sidecar key for a given audio object key."""
        base = audio_key.rsplit(".", 1)[0] if "." in audio_key else audio_key
        return base + SIDECAR_SUFFIX

    def read_sidecar(self, audio_key: str) -> Optional[Dict]:
        """Read and parse the sidecar for an audio key. Returns None if absent."""
        sidecar_key = self.sidecar_key_for(audio_key)
        try:
            raw = self.store.get_content(sidecar_key)
            if raw is None:
                return None
            data = json.loads(raw)
            if data.get("schema_version", "").startswith("1."):
                log.warning(f"NexusCatalog: v1 sidecar found at {sidecar_key} — treat as legacy")
            return data
        except Exception as e:
            log.error(f"NexusCatalog: Failed to read sidecar {sidecar_key}: {e}")
            return None

    # ------------------------------------------------------------------
    # Deprecated shim (v1 API — do not call from new code)
    # ------------------------------------------------------------------

    def register_generation(
        self,
        item_id: str,
        audio_key: str,
        title: str,
        source_system: str,
        text_content: str,
        text_type: str = "prompt",
        audio_format: Optional[Dict] = None,
        duration_seconds: float = 0.0,
        content_length_bytes: int = 0,
        sha256: Optional[str] = None,
        source_job_id: Optional[str] = None,
        source_attempt: int = 1,
        status: str = "ready",
        tags: Optional[List[str]] = None,
        provenance: Optional[Dict] = None,
        distribution_default: str = "opus_96_vbr",
        archive_role: str = "source_wav",
        derivatives: Optional[List[Dict]] = None,
    ) -> bool:
        """DEPRECATED — v1 API. Use register_asset() for new code."""
        import warnings
        warnings.warn(
            "NexusCatalog.register_generation() is deprecated. Use register_asset().",
            DeprecationWarning,
            stacklevel=2,
        )
        base_key = audio_key.rsplit(".", 1)[0] if "." in audio_key else audio_key
        metadata_key = f"{base_key}.json"
        metadata = {
            "schema_version": "1.1.0",
            "id": str(item_id),
            "status": status,
            "audio_key": audio_key,
            "metadata_key": metadata_key,
            "bucket": self.bucket,
            "title": title,
            "source_system": source_system,
            "source_job_id": source_job_id or str(item_id),
            "source_attempt": source_attempt,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "content_length_bytes": content_length_bytes,
            "sha256": sha256,
            "audio_format": audio_format or {"sample_rate": 24000, "channels": 1, "bit_depth": 16, "codec": "wav"},
            "duration_seconds": duration_seconds,
            "tags": tags or [],
            "text_source": {"type": text_type, "content": text_content},
            "source_format": audio_key.split(".")[-1] if "." in audio_key else "wav",
            "archive_role": archive_role,
            "distribution_default": distribution_default,
            "transcode_profile_version": "1.0.0",
            "derivatives": derivatives or [],
            "provenance": provenance or {},
        }
        log.warning(f"NexusCatalog: Writing deprecated v1 sidecar for {item_id}")
        try:
            result = self.store.persist_object(
                file_obj=json.dumps(metadata, indent=2).encode("utf-8"),
                object_name=metadata_key,
                content_type="application/json",
            )
            return bool(result)
        except Exception as e:
            log.error(f"NexusCatalog: v1 register_generation failed for {item_id}: {e}")
            return False


def make_catalog(bucket=None, s3_config=None) -> NexusCatalog:
    return NexusCatalog(bucket=bucket, s3_config=s3_config)
