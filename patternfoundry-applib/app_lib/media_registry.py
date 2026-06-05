import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Any, Dict
from sqlmodel import Session, SQLModel, Field, select
from decimal import Decimal
from sqlalchemy import Column, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as pgUUID

# Import database connection
try:
    from app_lib import db_connection as db
except ImportError:
    import db_connection as db

# PF-VAL-MEDIA-001: Unified Media Registry for ToneRoot and StoryLoom
# Enhanced with BYOB (Bring Your Own Bucket) Storage Support.

logger = logging.getLogger("media-registry")

class StorageConfig(SQLModel, table=True):
    """BYOB Storage Configuration per Profile (ADR-008)"""
    __tablename__ = "storage_configs"
    
    id: uuid.UUID = Field(
        sa_column=Column(pgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    )
    profile_uuid: uuid.UUID = Field(index=True, unique=True)
    provider: str = Field() # 'r2', 'b2', 's3', 'minio'
    endpoint_url: str = Field()
    bucket_name: str = Field()
    access_key: str = Field()
    secret_key: str = Field()
    region: Optional[str] = Field(default=None)
    path_prefix: str = Field(default="")
    cdn_domain: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class Media(SQLModel, table=True):
    """Unified media table for ToneRoot and StoryLoom"""
    __tablename__ = "media"
    
    id: uuid.UUID = Field(
        sa_column=Column(pgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    )
    type: str = Field(index=True)  # "audio", "image", "video", etc.
    s3_bucket: str = Field(default="music")
    s3_key: str = Field(index=True)
    
    # Tiering for lifecycle management
    current_tier: str = Field(default="hot")  # hot, warm, cold
    pinned_tier: Optional[str] = Field(default=None)
    
    # Metadata
    mime_type: Optional[str] = Field(default=None)
    size_bytes: Optional[int] = Field(default=None)
    duration_seconds: Optional[Decimal] = Field(sa_column=Column("duration_seconds", Numeric))
    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)
    
    # Access tracking
    access_count: int = Field(default=0)
    last_accessed_at: Optional[datetime] = Field(default=None)
    
    # Sharing support
    shared_from_user_id: Optional[uuid.UUID] = Field(default=None)
    original_media_id: Optional[uuid.UUID] = Field(default=None)
    
    # BYOB Ownership (Phase 1: Nullable for migration)
    storage_config_id: Optional[uuid.UUID] = Field(
        default=None, 
        sa_column=Column(pgUUID(as_uuid=True), ForeignKey("storage_configs.id"), nullable=True)
    )
    owner_profile_uuid: Optional[uuid.UUID] = Field(default=None, index=True)

    # ToneRoot-specific fields (optional)
    title: Optional[str] = Field(default=None)
    artist: Optional[str] = Field(default=None)
    album: Optional[str] = Field(default=None)
    song_uuid: Optional[uuid.UUID] = Field(default=None, index=True)

    # Intake fields (ADR-017 D1, D6, D8)
    sidecar_written: bool = Field(default=False)
    source_system: Optional[str] = Field(default=None)
    intake_bundle_sha256: Optional[str] = Field(default=None)
    
    # StoryLoom-specific fields (optional)
    story_id: Optional[uuid.UUID] = Field(default=None, index=True)
    character_id: Optional[uuid.UUID] = Field(default=None, index=True)
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class MediaPermission(SQLModel, table=True):
    """Sharing permissions for media across profiles"""
    __tablename__ = "media_permissions"
    
    media_uuid: uuid.UUID = Field(sa_column=Column(pgUUID(as_uuid=True), ForeignKey("media.id"), primary_key=True))
    grantee_uuid: uuid.UUID = Field(primary_key=True)
    permission: str = Field(default="read") # 'read', 'write'
    granted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# =============================================================================
# MEDIA OWNERSHIP INVARIANT
# =============================================================================
# Every media row encodes a trust relationship:
#   - owner_profile_uuid: who made this / whose S3 credentials serve it
#   - storage_config_id:  which storage_configs row holds those credentials
#   - media_permissions:  who the owner has extended trust to
#
# The proxy (_stream_audio) derives credentials from owner_profile_uuid,
# never from the requesting profile. This is intentional and must not change.
#
# SEAM WITH SONGS TABLE:
#   media.song_uuid links this asset to the tenant inventory (songs table).
#   Songs sharing (sharing_api.py / shares table) is the UX layer.
#   media_permissions is the enforcement layer.
#   When sharing_api creates a share, it must also create a media_permissions
#   row. See sharing_api.py for the canonical policy comment.
#
# CURRENT STATE (P1):
#   storage_config_id and owner_profile_uuid are nullable (migration 0005).
#   Rows with NULL owner fall back to global credentials in _stream_audio.
#   migration 0007 will make these NOT NULL once register_media() enforces
#   the invariant at insert time (P1 item #4).
# =============================================================================

async def register_media(
    s3_key: str,
    media_type: str,
    owner_profile_uuid: uuid.UUID,
    db_session,
    s3_bucket: str = "music",
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    duration_seconds: Optional[float] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    shared_from_user_id: Optional[uuid.UUID] = None,
    story_id: Optional[uuid.UUID] = None,
    character_id: Optional[uuid.UUID] = None,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    song_uuid: Optional[uuid.UUID] = None,
) -> uuid.UUID:
    """
    Register a media asset with ownership stamped at insert time.
    
    INVARIANT: Every media row must have owner_profile_uuid and storage_config_id
    set at insert time. This function enforces that invariant.
    Raises RuntimeError if the owner has no storage config — do not catch this,
    let it surface so the missing config is fixed, not silently ignored.
    """
    # Resolve storage config for owner — raises if missing
    config_row = await db_session.fetchrow(
        "SELECT id FROM storage_configs WHERE profile_uuid = $1",
        owner_profile_uuid
    )
    if not config_row:
        raise RuntimeError(
            f"register_media: owner {owner_profile_uuid} has no storage_configs row. "
            f"Run SetupWizard or create storage config before registering media."
        )

    storage_config_id = config_row["id"]

    media_record = Media(
        type=media_type,
        s3_bucket=s3_bucket,
        s3_key=s3_key,
        owner_profile_uuid=owner_profile_uuid,
        storage_config_id=storage_config_id,
        mime_type=mime_type,
        size_bytes=size_bytes,
        duration_seconds=Decimal(str(duration_seconds)) if duration_seconds else None,
        width=width,
        height=height,
        shared_from_user_id=shared_from_user_id,
        story_id=story_id,
        character_id=character_id,
        title=title,
        artist=artist,
        album=album,
        song_uuid=song_uuid,
    )

    db_session.add(media_record)
    await db_session.flush()
    return media_record.id


async def register_song(**kwargs) -> Optional[uuid.UUID]:
    """ToneRoot wrapper for registering songs."""
    kwargs.setdefault('media_type', 'audio')
    return await register_media(**kwargs)

def increment_access_count(media_id: uuid.UUID) -> bool:
    try:
        with Session(db.get_engine()) as session:
            media = session.get(Media, media_id)
            if media:
                media.access_count += 1
                media.last_accessed_at = datetime.now(timezone.utc)
                session.commit()
                return True
            return False
    except Exception as e:
        logger.error(f"Failed to increment access count for {media_id}: {e}")
        return False

async def resolve_storage_config(owner_profile_uuid: uuid.UUID) -> Optional[uuid.UUID]:
    """Return the storage_config id for a profile."""
    try:
        with Session(db.get_engine()) as session:
            config = session.exec(
                select(StorageConfig).where(StorageConfig.profile_uuid == owner_profile_uuid)
            ).first()
            return config.id if config else None
    except Exception as e:
        logger.error(f"Failed to resolve storage config for {owner_profile_uuid}: {e}")
        return None

def sync_storage_config(
    profile_uuid: uuid.UUID,
    provider: str,
    endpoint_url: str,
    bucket_name: str,
    access_key: str,
    secret_key: str,
    region: Optional[str] = None,
    path_prefix: str = "",
    cdn_domain: Optional[str] = None
) -> Optional[uuid.UUID]:
    """
    Upserts a storage configuration for a given profile.
    """
    try:
        with Session(db.get_engine()) as session:
            config = session.exec(
                select(StorageConfig).where(StorageConfig.profile_uuid == profile_uuid)
            ).first()
            
            if config:
                config.provider = provider
                config.endpoint_url = endpoint_url
                config.bucket_name = bucket_name
                config.access_key = access_key
                config.secret_key = secret_key
                config.region = region
                config.path_prefix = path_prefix
                config.cdn_domain = cdn_domain
                logger.info(f"Updated storage config for profile {profile_uuid}")
            else:
                config = StorageConfig(
                    profile_uuid=profile_uuid,
                    provider=provider,
                    endpoint_url=endpoint_url,
                    bucket_name=bucket_name,
                    access_key=access_key,
                    secret_key=secret_key,
                    region=region,
                    path_prefix=path_prefix,
                    cdn_domain=cdn_domain
                )
                session.add(config)
                logger.info(f"Created new storage config for profile {profile_uuid}")
            
            session.commit()
            session.refresh(config)
            return config.id
            
    except Exception as e:
        logger.error(f"Failed to sync storage config for {profile_uuid}: {e}")
        return None
