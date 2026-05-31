from enum import Enum
from typing import Optional, List, Dict, Any
from uuid import UUID, uuid4
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, UniqueConstraint, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
import json
import pydantic

# ── Custom JSON Handling ──────────────────────────────────────────────

class CustomJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            if isinstance(value, str):
                return [value]
            return None

# ── Enumerations ──────────────────────────────────────────────────────

class OFNRType(str, Enum):
    observation = "O"
    feeling     = "F"
    need        = "N"
    request     = "R"

class ChannelHint(str, Enum):
    mechanic_seed  = "mechanic_seed"
    tension_peak   = "tension_peak"
    sync_metric    = "sync_metric"
    knowledge_node = "knowledge_node"
    sponsor_hook   = "sponsor_hook"
    none           = "none"

class StoryStage(str, Enum):
    draft      = "draft"
    review     = "review"
    published  = "published"
    archived   = "archived"

class ContributorRole(str, Enum):
    author     = "author"
    editor     = "editor"
    narrator   = "narrator"
    ai_assist  = "ai_assist"

class ReadingLevel(str, Enum):
    elementary    = "elementary"
    middle        = "middle"
    high_school   = "high_school"
    adult         = "adult"
    professional  = "professional"

class TakeawayType(str, Enum):
    worksheet      = "worksheet"
    diagram        = "diagram"
    checklist      = "checklist"
    game_sheet     = "game_sheet"
    reflection     = "reflection"
    resource_list  = "resource_list"
    episode_brief  = "episode_brief"
    other          = "other"

class JobStatus(str, Enum):
    pending = "pending"
    image_pending = "image_pending"
    audio_pending = "audio_pending"
    processing = "processing"
    image_complete = "image_complete"
    complete = "complete"
    failed = "failed"
    retry = "retry"
    # SSE-specific statuses for image generation
    queued = "queued"
    started = "started"

class GenerationTier(str, Enum):
    standard = "standard"
    poc = "poc"

class FrequencyTier(str, Enum):
    common = "common"
    rare   = "rare"
    exotic = "exotic"

class EnrichmentStatus(str, Enum):
    pending = "pending"
    ready   = "ready"
    failed  = "failed"

# ── Core Narrative Models ─────────────────────────────────────────────

class UserData_Story(SQLModel, table=True):
    __tablename__ = "stories"

    id:               UUID         = Field(default_factory=uuid4, primary_key=True)
    slug:             str          = Field(unique=True, index=True)
    title:            str
    profession:       str
    archetype:        str
    osi_layer:        int          = Field(ge=1, le=7)
    locale:           str          = Field(default="en", max_length=10)
    reading_level:    ReadingLevel = Field(default=ReadingLevel.adult)
    stage:            StoryStage   = Field(default=StoryStage.draft)
    story_metadata:   Optional[Dict] = Field(default_factory=dict, sa_column=Column("metadata", JSONB))

    # Versioning
    version_number:   int          = Field(default=1)
    derived_from:     Optional[UUID] = Field(default=None, foreign_key="stories.id")

    # Sync governance
    is_local:         bool         = Field(default=True)
    is_synced:        bool         = Field(default=False)
    last_synced_at:   Optional[datetime] = Field(default=None)

    # Timestamps
    published_at:     Optional[datetime] = None
    created_at:       datetime     = Field(default_factory=datetime.utcnow)
    updated_at:       datetime     = Field(default_factory=datetime.utcnow)

    # Relationships
    scenes: List["Scene"] = Relationship(back_populates="story")
    characters: List["UserData_Characters"] = Relationship(back_populates="story")
    tags: List["StoryTag"] = Relationship(back_populates="story")
    attributions: List["SourceAttribution"] = Relationship(back_populates="story")
    contributors: List["StoryContributor"] = Relationship(back_populates="story")
    sponsors: List["StorySponsor"] = Relationship(back_populates="story")
    takeaways: List["StoryTakeaway"] = Relationship(back_populates="story")

class Scene(SQLModel, table=True):
    __tablename__ = "scenes"

    id:              UUID          = Field(default_factory=uuid4, primary_key=True)
    story_id:        UUID          = Field(foreign_key="stories.id", index=True)
    sequence:        int
    title:           str
    setting:         str           # Sensory anchor
    locale:          str           = Field(default="en", max_length=10)
    sponsor_segment: Optional[str] = None
    audio_alt_text:  Optional[str] = None

    story: "UserData_Story" = Relationship(back_populates="scenes")
    blocks: List["OFNRBlock"] = Relationship(back_populates="scene")
    game_pack_hints: List["GamePackHint"] = Relationship(back_populates="scene")

class UserData_Characters(SQLModel, table=True):
    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint("character_name", "user_id", name="unique_character_per_user"),
        {"extend_existing": True}
    )

    id:              UUID          = Field(default_factory=uuid4, primary_key=True)
    story_id:        Optional[UUID] = Field(default=None, foreign_key="stories.id", index=True)
    user_id:         str           = Field(default="system", index=True)
    character_name:  str           = Field(index=True)
    role:            Optional[str] = None
    voice_profile:   Optional[str] = None
    locale:          str           = Field(default="en", max_length=10)

    # ADR-007 Hybrid Tracking
    is_local:        bool          = Field(default=True)
    is_synced:       bool          = Field(default=False)
    last_synced_at:  Optional[datetime] = Field(default=None)
    updated_at:      datetime      = Field(default_factory=datetime.utcnow)

    # Enrichment Status
    enrichment_status: EnrichmentStatus = Field(default=EnrichmentStatus.pending)
    enrichment_error:  Optional[str]    = None

    # Generative Identity (Premium)
    appearance_seed:   int           = Field(default=-1)
    personality_seed:  int           = Field(default=-1)
    visual_dna:        Optional[str] = None
    
    # Legacy fields compatibility
    character_sheet_json: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    character_audio_map_json: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    voice_prompt: Optional[str] = Field(default=None)
    golden_image_url: Optional[str] = Field(default=None)
    
    # Vault / CharacterStudio extra fields (added for Postgres compatibility)
    dna:             Optional[str] = None
    is_dirty:        bool          = Field(default=False)
    color:           str           = Field(default="#cccccc")
    gold_seed:       int           = Field(default=-1)
    metadata_:       Optional[str] = Field(default="{}", sa_column=Column("metadata", Text))
    sync_attempts:   int           = Field(default=0)
    retry_count:     int           = Field(default=0)
    language:        str           = Field(default="en", max_length=10)

    story: "UserData_Story" = Relationship(back_populates="characters")

# Aliases for cross-repo compatibility (ADR-007, ADR-009)
Story = UserData_Story
Characters = UserData_Characters
Character = UserData_Characters

class GeneratorCategory(SQLModel, table=True):
    __tablename__ = "generator_categories"

    id:              int           = Field(default=None, primary_key=True)
    name:            str           = Field(unique=True, index=True)
    display_name:    str
    applies_to:      str           # e.g. "physical", "clothing", "voice"

class GeneratorValue(SQLModel, table=True):
    __tablename__ = "generator_values"

    id:              int           = Field(default=None, primary_key=True)
    category_id:     int           = Field(foreign_key="generator_categories.id")
    value:           str
    frequency_tier:  FrequencyTier = Field(default=FrequencyTier.common)

    category: GeneratorCategory = Relationship()

class OFNRBlock(SQLModel, table=True):
    __tablename__ = "ofnr_blocks"

    id:              UUID          = Field(default_factory=uuid4, primary_key=True)
    scene_id:        UUID          = Field(foreign_key="scenes.id", index=True)
    character_id:    UUID          = Field(foreign_key="characters.id")
    sequence:        int
    type:            OFNRType
    content:         str           # Plain human text ONLY
    channel_hint:    ChannelHint   = Field(default=ChannelHint.none)
    intensity:       float         = Field(default=0.5, ge=0.0, le=1.0)
    locale:          str           = Field(default="en", max_length=10)
    audio_alt_text:  Optional[str] = None

    # Versioning
    version_number:  int           = Field(default=1)
    derived_from:    Optional[UUID] = Field(default=None, foreign_key="ofnr_blocks.id")

    scene: "Scene" = Relationship(back_populates="blocks")

class GamePackHint(SQLModel, table=True):
    __tablename__ = "game_pack_hints"

    id:              UUID          = Field(default_factory=uuid4, primary_key=True)
    scene_id:        UUID          = Field(foreign_key="scenes.id", index=True)
    mechanic:        str
    room_archetype:  str
    parameters:      Dict          = Field(sa_column=Column(JSONB))

    scene: "Scene" = Relationship(back_populates="game_pack_hints")

class StoryTakeaway(SQLModel, table=True):
    __tablename__ = "story_takeaways"

    id:               UUID          = Field(default_factory=uuid4, primary_key=True)
    story_id:         UUID          = Field(foreign_key="stories.id", index=True)
    title:            str
    type:             TakeawayType
    description:      Optional[str] = None
    file_key:         str           # MinIO object key
    mime_type:        str
    version_number:   int          = Field(default=1)
    derived_from:     Optional[UUID] = Field(default=None, foreign_key="story_takeaways.id")
    intensity:        float         = Field(default=0.5, ge=0.0, le=1.0)
    target_audience:  Optional[str] = None
    created_at:       datetime      = Field(default_factory=datetime.utcnow)

    story: "UserData_Story" = Relationship(back_populates="takeaways")

# ── Supporting Tables ─────────────────────────────────────────────────

class StoryTag(SQLModel, table=True):
    __tablename__ = "story_tags"
    __table_args__ = (UniqueConstraint("story_id", "namespace", "value"),)

    id:           UUID = Field(default_factory=uuid4, primary_key=True)
    story_id:     UUID = Field(foreign_key="stories.id", index=True)
    namespace:    str
    value:        str

    story: "UserData_Story" = Relationship(back_populates="tags")

class SourceAttribution(SQLModel, table=True):
    __tablename__ = "source_attributions"

    id:               UUID = Field(default_factory=uuid4, primary_key=True)
    story_id:         UUID = Field(foreign_key="stories.id", index=True)
    source_title:     str
    source_author:    str
    source_url:       Optional[str] = None
    license:          str = Field(default="PD")
    extraction_notes: Optional[str] = None
    extracted_at:     datetime = Field(default_factory=datetime.utcnow)

    story: "UserData_Story" = Relationship(back_populates="attributions")

class StoryContributor(SQLModel, table=True):
    __tablename__ = "story_contributors"
    __table_args__ = (UniqueConstraint("story_id", "user_id", "role"),)

    id:             UUID = Field(default_factory=uuid4, primary_key=True)
    story_id:       UUID = Field(foreign_key="stories.id", index=True)
    user_id:        UUID
    role:           ContributorRole
    contributed_at: datetime = Field(default_factory=datetime.utcnow)

    story: "UserData_Story" = Relationship(back_populates="contributors")

class StorySponsor(SQLModel, table=True):
    __tablename__ = "story_sponsors"

    id:              UUID = Field(default_factory=uuid4, primary_key=True)
    story_id:        UUID = Field(foreign_key="stories.id", index=True)
    sponsor_name:    str
    sponsor_type:    str
    segment_tag:     Optional[str] = None
    contract_ref:    Optional[str] = None
    active_from:     datetime = Field(default_factory=datetime.utcnow)
    active_until:    Optional[datetime] = None

    story: "UserData_Story" = Relationship(back_populates="sponsors")

# ── Execution Layer Models ───────────────────────────────────────────

class GraphNode(SQLModel, table=True):
    __tablename__ = "nodes"
    id: str = Field(primary_key=True)
    name: str
    type: str # e.g. "story_root", "character"
    story_id: Optional[str] = Field(default=None, index=True)
    is_synced: bool = Field(default=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class GraphSignal(SQLModel, table=True):
    __tablename__ = "signals"
    id: str = Field(primary_key=True)
    from_node_id: str = Field(index=True)
    target_node_id: Optional[str] = Field(default=None, index=True)
    modality: str # e.g. "narration", "dialogue"
    content: str = Field(sa_column=Column(Text))
    intensity: Optional[str] = None # e.g. "neutral", "angry"
    timestamp: Optional[str] = None # ISO format string
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class GraphInsight(SQLModel, table=True):
    __tablename__ = "insights"
    id: str = Field(primary_key=True)
    story_id: str = Field(index=True)
    observer_type: str # e.g. "Character", "Narrative"
    content: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    timestamp: Optional[str] = None # ISO format string
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class PanelJob(SQLModel, table=True):
    __tablename__ = "panel_jobs"
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[str] = Field(default=None, index=True)
    provider_tag: Optional[str] = Field(default=None)
    max_cost_limit: Optional[float] = Field(default=None)
    
    exact_positive_prompt: Optional[str] = Field(default=None)
    exact_negative_prompt: Optional[str] = Field(default=None)
    style_preset: Optional[Dict] = Field(default=None, sa_column=Column(CustomJSON))
    character_anchors: Optional[Dict] = Field(default=None, sa_column=Column(CustomJSON))
    cinematic_metadata: Optional[Dict] = Field(default=None, sa_column=Column(CustomJSON))
    
    story_title: str
    module_number: int
    module_title: str
    section_title: str
    panel_sequence_section: int
    story_slug: str
    image_filename: str = Field(unique=True)
    panel_text: str
    image_prompt: str
    audio_filename: Optional[str] = Field(default=None)
    audio_layers: Optional[Dict] = Field(default=None, sa_column=Column(CustomJSON))
    audio_direction: str
    
    reference_image_location: Optional[str] = Field(default=None)
    reference_image_urls: Optional[List[str]] = Field(default=None, sa_column=Column(CustomJSON, nullable=True))
    audio_backing_track_id: Optional[str] = Field(default=None)
    reference_audio_location: Optional[List[str]] = Field(default=None, sa_column=Column(CustomJSON, nullable=True))
    generation_tier: GenerationTier = Field(default=GenerationTier.standard)
    status: JobStatus = Field(default=JobStatus.pending)
    retry_count: int = Field(default=0)
    retry_at: Optional[int] = Field(default=None)
    error_log: Optional[str] = Field(default=None)
    failure_reason: Optional[str] = Field(default=None, sa_column=Column(Text))
    estimated_duration: Optional[float] = Field(default=None)
    
    # ADR-013 Execution Governance
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_ticket: Optional[str] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)
    worker_id: Optional[str] = Field(default=None, index=True)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @pydantic.validator("cinematic_metadata")
    def validate_governance(cls, v):
        if v and "duration_sec" in v:
            if v["duration_sec"] >= 30:
                raise ValueError("GovernanceError: Schema Violation: Panels must be < 30s")
        return v

class PanelJobBatch(SQLModel):
    """
    A Pydantic model for receiving a batch of jobs in a single API request.
    """
    jobs: List[PanelJob]

class CharacterAudioJob(SQLModel, table=True):  
      __tablename__ = "character_audio_jobs"
      __table_args__ = {"extend_existing": True}
      id: Optional[int] = Field(default=None, primary_key=True)  
      character_name: str = Field(index=True)  
      status: str = Field(default="pending_generation", index=True)
      sample_text: str  
      voice_prompt: str  
      candidate_urls: Optional[List[str]] = Field(default=None, sa_column=Column(CustomJSON, nullable=True))
      job_config: Optional[Dict] = Field(default_factory=dict, sa_column=Column(CustomJSON, nullable=True))
      golden_url: Optional[str] = Field(default=None)  
      error_log: Optional[str] = Field(default=None)
      failure_reason: Optional[str] = Field(default=None, sa_column=Column(Text))
      failure_code: Optional[str] = Field(default=None, index=True)
      estimated_duration: Optional[float] = Field(default=None)
      retry_count: int = Field(default=0)
      max_retries: int = Field(default=3)
      retry_at: Optional[int] = Field(default=None)
      
      # ADR-013 Execution Governance
      submission_mode: str = Field(default="interactive") # interactive | batch
      placement_hint: str = Field(default="local_preferred") # local_preferred | burst_ok | cloud_required
      lease_owner: Optional[str] = Field(default=None)
      lease_ticket: Optional[str] = Field(default=None)
      lease_expires_at: Optional[datetime] = Field(default=None)
      interactive_stream_requested: bool = Field(default=False)
      infra_snapshot_json: Optional[Dict] = Field(default_factory=dict, sa_column=Column(JSONB))
      
      # Additional tracked fields from schema
      is_dev: bool = Field(default=False)
      qa_report: Optional[Dict] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=True))
      
      created_at: datetime = Field(default_factory=datetime.utcnow)  
      updated_at: datetime = Field(default_factory=datetime.utcnow)

class CharacterAudioJobEvent(SQLModel, table=True):
    __tablename__ = "character_audio_job_events"
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    event_type: str = Field(index=True) # warming, generating, eval, success, failed, etc.
    payload: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=datetime.utcnow)

class CharacterImageJob(SQLModel, table=True):
    __tablename__ = "character_image_jobs"
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    character_name: str = Field(index=True)
    status: str = Field(default="pending_generation", index=True)
    image_prompt: str
    candidate_image_urls: Optional[List[str]] = Field(default=None, sa_column=Column(CustomJSON, nullable=True))
    job_config: Optional[Dict] = Field(default_factory=dict, sa_column=Column(CustomJSON, nullable=True))
    golden_image_url: Optional[str] = Field(default=None)
    error_log: Optional[str] = Field(default=None)
    failure_reason: Optional[str] = Field(default=None, sa_column=Column(Text))
    estimated_duration: Optional[float] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    retry_count: int = Field(default=0)
    retry_at: Optional[int] = Field(default=None)

class ImageGenerationJob(SQLModel, table=True):
    """
    SSE-based image generation job for interactive panel generation.
    Designed for real-time progress streaming via SSE (Option A: SSE-first approach).
    """
    __tablename__ = "image_generation_jobs"
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    story_id: str = Field(index=True)
    chapter_index: int
    panel_index: int
    
    # Generation parameters
    prompt: str
    negative_prompt: Optional[str] = Field(default=None)
    width: int = Field(default=1216)
    height: int = Field(default=832)
    steps: int = Field(default=30)
    seed: Optional[int] = Field(default=None)
    model: Optional[str] = Field(default=None)
    characters: Optional[str] = Field(default=None)
    style_id: Optional[str] = Field(default=None)
    
    # Status tracking
    status: JobStatus = Field(default=JobStatus.queued, index=True)
    
    # Result
    image_url: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
    
    # ETA and timing (for Phase 2 EMA)
    provider: str = Field(default="local")  # e.g., "local", "runpod", "vast"
    gpu_label: str = Field(default="RTX 3060")
    queue_position: int = Field(default=0)
    duration_seconds: Optional[float] = Field(default=None)
    
    # Governance
    lease_owner: Optional[str] = Field(default=None)
    lease_ticket: Optional[str] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class ImageGenerationJobEvent(SQLModel, table=True):
    """
    Event log for ImageGenerationJob - enables replayable SSE streams.
    Similar pattern to CharacterAudioJobEvent.
    """
    __tablename__ = "image_generation_job_events"
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    event_type: str = Field(index=True)  # queued, started, progress, complete, failed, heartbeat
    payload: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ProviderCapabilities(SQLModel, table=True):
    __tablename__ = "provider_capabilities"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_instance", name="unique_provider_instance"),
        {"extend_existing": True}
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(nullable=False, max_length=50)
    provider_instance: Optional[str] = Field(default=None, max_length=255)
    capabilities: Dict = Field(default_factory=dict, sa_column=Column(CustomJSON, nullable=False))
    characters: Optional[Dict] = Field(default_factory=dict, sa_column=Column(CustomJSON))
    metadata_json: Optional[Dict] = Field(default_factory=dict, sa_column=Column(CustomJSON))
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    version: int = Field(default=1)

class ProviderCapabilityHistory(SQLModel, table=True):
    __tablename__ = "provider_capability_history"
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(nullable=False, max_length=50)
    provider_instance: Optional[str] = Field(default=None, max_length=255)
    capabilities: Dict = Field(default_factory=dict, sa_column=Column(CustomJSON, nullable=False))
    version: int = Field(nullable=False)
    changed_at: datetime = Field(default_factory=datetime.utcnow)
    change_reason: Optional[str] = Field(default=None, sa_column=Column(Text))
    changed_by: Optional[str] = Field(default=None, max_length=100)

# ── Governance Layer Models (ADR-007 Alignment) ───────────────────────

class RuleVitality(SQLModel, table=True):
    __tablename__ = "rule_vitality"
    __table_args__ = {"schema": "governance", "extend_existing": True}

    rule_id:        str          = Field(primary_key=True)
    node_id:        str          = Field(default="cluster-wide", primary_key=True)
    layer:          str
    state:          str          = Field(default="enforced")
    last_validated: datetime     = Field(default_factory=datetime.utcnow)
    immortal:       bool         = Field(default=False)
    hit_count_24h:  int          = Field(default=0)

class IncidentLog(SQLModel, table=True):
    __tablename__ = "incident_logs"
    __table_args__ = {"schema": "governance", "extend_existing": True}

    id:             Optional[int] = Field(default=None, primary_key=True)
    rule_id:        str
    node_id:        str          = Field(default="cluster-wide")
    source:         str
    payload:        Dict         = Field(default_factory=dict, sa_column=Column(JSONB))
    timestamp:      datetime     = Field(default_factory=datetime.utcnow)

class RepoState(SQLModel, table=True):
    __tablename__ = "repo_state"
    __table_args__ = {"schema": "governance", "extend_existing": True}

    id:               Optional[int] = Field(default=None, primary_key=True)
    repo_name:        str
    branch:           str          = Field(default="main")
    file_count:       int
    token_estimate:   int
    directory_tree:   Optional[str] = Field(sa_column=Column(Text))
    full_digest_path: str
    captured_at:      datetime     = Field(default_factory=datetime.utcnow)

# ============================================================================
# V2 STORY IMPORT MODELS
# ============================================================================

class StoryV2(SQLModel, table=True):
    __tablename__ = "stories_v2"
    story_id: str = Field(primary_key=True)
    title: str
    subtitle: Optional[str] = None
    description: Optional[str] = None
    stage: str
    
    # JSON blobs for less structured data
    identity: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    
    characters: List["CharacterV2"] = Relationship(back_populates="story")
    chapters: List["ChapterV2"] = Relationship(back_populates="story", sa_relationship_kwargs={"lazy": "select"})

class CharacterV2(SQLModel, table=True):
    __tablename__ = "characters_v2"
    character_id: str = Field(primary_key=True)
    display_name: str
    
    # Foreign Key to Story
    story_id: str = Field(foreign_key="stories_v2.story_id")
    story: "StoryV2" = Relationship(back_populates="characters")
    
    # JSON blobs for identity, voice, etc.
    identity: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    voice: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    visual_identity: Dict = Field(default_factory=dict, sa_column=Column(JSONB))
    
    panels: List["PanelV2"] = Relationship(back_populates="character", sa_relationship_kwargs={"lazy": "select"})

    # Loose coupling field for overrides
    identity_overrides: Optional[Dict] = Field(default_factory=dict, sa_column=Column(JSONB))

class ChapterV2(SQLModel, table=True):
    __tablename__ = "chapters_v2"
    chapter_id: str = Field(primary_key=True)
    title: str
    
    # Foreign Key to Story
    story_id: str = Field(foreign_key="stories_v2.story_id")
    story: "StoryV2" = Relationship(back_populates="chapters")
    
    scenes: List["SceneV2"] = Relationship(back_populates="chapter", sa_relationship_kwargs={"lazy": "select"})

class SceneV2(SQLModel, table=True):
    __tablename__ = "scenes_v2"
    scene_id: str = Field(primary_key=True)
    
    # Foreign Key to Chapter
    chapter_id: str = Field(foreign_key="chapters_v2.chapter_id")
    chapter: "ChapterV2" = Relationship(back_populates="scenes")
    
    panels: List["PanelV2"] = Relationship(back_populates="scene", sa_relationship_kwargs={"lazy": "select"})

class PanelV2(SQLModel, table=True):
    __tablename__ = "panels_v2"
    panel_id: str = Field(primary_key=True)
    text: str
    audio_direction: Optional[str] = None
    
    # Foreign Keys
    scene_id: str = Field(foreign_key="scenes_v2.scene_id")
    scene: "SceneV2" = Relationship(back_populates="panels")
    
    character_ref: str = Field(foreign_key="characters_v2.character_id")
    character: "CharacterV2" = Relationship(back_populates="panels")
    
    # JSON blob for hints
    render_hints: Dict = Field(default_factory=dict, sa_column=Column(JSONB))


# --- NEW: Story Registry Model ---
class StoryRegistryEntry(SQLModel, table=True):
    __tablename__ = "story_registry_entries"
    story_hash: str = Field(primary_key=True, index=True) # SHA256 hash of canonical JSON
    schema_version: str # e.g., "v2"
    canonical_story_json: str = Field(sa_column=Column(Text)) # Immutable canonical JSON payload
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DailyWave(SQLModel, table=True):
    __tablename__ = "daily_waves"
    id: Optional[int] = Field(default=None, primary_key=True)
    date_anchor: str = Field(index=True)
    day_of_year: int
    continuity_hash: str
    archetype: str
    observation: str
    feeling: str
    need: str
    request: str
    manifesto_reference: str
    industrial_rhythm: str
    sync_alignment_score: float
    manifesto_text: str
    image_prompt: str
    image_url: Optional[str] = None
    audio_url: Optional[str] = None
    canon_audio_id: Optional[str] = None
    narrator_voice_ref: Optional[List[str]] = Field(default=None, sa_column=Column(CustomJSON, nullable=True))
    created_at: datetime = Field(default_factory=datetime.utcnow)
