"""
Pydantic models for search configuration.

Defines the schema for search_config.yaml which controls:
- Which fields are indexed per asset type
- Match types (prefix, substring, exact, fuzzy)
- Field priorities for ranking
- Boost values for scoring
- Hybrid (keyword + semantic) fusion on the Postgres backend
"""
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class MatchType(str, Enum):
    """Search match strategy for a field."""
    PREFIX = "prefix"           # Query must match start of field value
    SUBSTRING = "substring"     # Query can appear anywhere in field value
    EXACT = "exact"             # Query must match field value exactly
    FUZZY = "fuzzy"             # Allow minor typos/variations


class SortField(str, Enum):
    """Available sort criteria for ranking results."""
    MATCH_PRIORITY = "match_priority"   # Sort by field priority (lower = higher rank)
    BOOST_SCORE = "boost_score"         # Sort by accumulated boost score
    TITLE_ASC = "title_asc"             # Alphabetical by title ascending
    TITLE_DESC = "title_desc"           # Alphabetical by title descending


class FieldConfig(BaseModel):
    """Configuration for a single searchable field."""
    indexed: bool = Field(
        default=True,
        description="Whether this field should be included in the search index"
    )
    match_type: MatchType = Field(
        default=MatchType.SUBSTRING,
        description="How to match the search query against this field"
    )
    priority: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Field priority (1=highest, 100=lowest). Lower values are matched first."
    )
    boost: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description="Score multiplier when this field matches (higher = more relevant)"
    )
    source: Optional[str] = Field(
        default=None,
        description="Source field path in extra_data (for extra_fields only)"
    )


class DefaultFieldsConfig(BaseModel):
    """Default field configurations applied to all asset types."""
    title: FieldConfig = Field(
        default_factory=lambda: FieldConfig(
            indexed=True,
            match_type=MatchType.PREFIX,
            priority=1,
            boost=3.0
        )
    )
    description: FieldConfig = Field(
        default_factory=lambda: FieldConfig(
            indexed=True,
            match_type=MatchType.SUBSTRING,
            priority=2,
            boost=1.0
        )
    )
    tags: FieldConfig = Field(
        default_factory=lambda: FieldConfig(
            indexed=True,
            match_type=MatchType.PREFIX,
            priority=3,
            boost=0.5
        )
    )


class DefaultsConfig(BaseModel):
    """Global default configuration."""
    fields: DefaultFieldsConfig = Field(default_factory=DefaultFieldsConfig)


class AssetTypeConfig(BaseModel):
    """Configuration for a specific asset type."""
    enabled: bool = Field(
        default=True,
        description="Whether this asset type should be indexed"
    )
    inherit_defaults: bool = Field(
        default=True,
        description="Whether to inherit default field configurations"
    )
    fields: Dict[str, FieldConfig] = Field(
        default_factory=dict,
        description="Override configurations for default fields (title, description, tags)"
    )
    extra_fields: Dict[str, FieldConfig] = Field(
        default_factory=dict,
        description="Additional custom fields to index for this asset type"
    )


class RankingConfig(BaseModel):
    """Configuration for result ranking and sorting."""
    primary_sort: SortField = Field(
        default=SortField.MATCH_PRIORITY,
        description="Primary sort criterion"
    )
    secondary_sort: SortField = Field(
        default=SortField.BOOST_SCORE,
        description="Secondary sort criterion (tie-breaker)"
    )
    tertiary_sort: SortField = Field(
        default=SortField.TITLE_ASC,
        description="Tertiary sort criterion"
    )


class SearchMode(str, Enum):
    """Which ranked lists answer a query (Postgres backend)."""
    AUTO = "auto"           # hybrid when semantic search is on, else lexical
    LEXICAL = "lexical"     # the Settings-driven keyword ranking only
    SEMANTIC = "semantic"   # vector similarity only (evaluation)
    HYBRID = "hybrid"       # keyword + BM25 + vector, fused by Reciprocal Rank Fusion


class HybridConfig(BaseModel):
    """Reciprocal Rank Fusion of the keyword and semantic ranked lists (#1061).

    Each document scores ``sum(weight / (rrf_k + rank))`` over the lists it
    appears in. Applied at query time, so changes take effect on the next search.
    The defaults come from the labeled evaluation (tests/eval): recall@5 and MRR
    are flat for lexical_weight 0.3-0.7 and rrf_k 5-30, so these sit mid-plateau.
    The keyword list matches on any query word, so it weighs a little less.
    """
    mode: SearchMode = Field(default=SearchMode.AUTO, description="Which ranked lists answer a query")
    lexical_weight: float = Field(default=0.7, ge=0.0, le=10.0,
                                  description="Weight of the Settings-driven keyword ranking")
    # Off by default: on the evaluation catalog BM25 alone scores below the
    # Settings ranking and any weight > 0 lowers hybrid recall. Its corpus
    # statistics are weak on short texts; larger catalogs may gain from it.
    bm25_weight: float = Field(default=0.0, ge=0.0, le=10.0,
                               description="Weight of the BM25 ranking (Lakebase Search only; 0 = off)")
    vector_weight: float = Field(default=1.0, ge=0.0, le=10.0,
                                 description="Weight of the semantic (embedding) ranking")
    rrf_k: int = Field(default=20, ge=1, le=1000,
                       description="RRF constant: larger values flatten the advantage of top ranks")
    candidate_limit: int = Field(default=50, ge=1, le=500,
                                 description="Documents taken from each of the BM25 and semantic lists")
    min_vector_similarity: float = Field(
        default=0.55, ge=-1.0, le=1.0,
        description="Cosine similarity a document needs to enter from the semantic list. "
                    "Model-specific; 0.55 is calibrated for databricks-gte-large-en, so that "
                    "nonsense queries return nothing",
    )


class SearchConfig(BaseModel):
    """
    Root configuration model for search indexing and ranking.
    
    Loaded from search_config.yaml and used by SearchManager
    to control field indexing, match strategies, and result ranking.
    """
    version: str = Field(
        default="1.0",
        description="Configuration schema version"
    )
    defaults: DefaultsConfig = Field(
        default_factory=DefaultsConfig,
        description="Global default field configurations"
    )
    asset_types: Dict[str, AssetTypeConfig] = Field(
        default_factory=dict,
        description="Per-asset-type configurations"
    )
    ranking: RankingConfig = Field(
        default_factory=RankingConfig,
        description="Result ranking configuration"
    )
    hybrid: HybridConfig = Field(
        default_factory=HybridConfig,
        description="Keyword + semantic fusion (Postgres backend)"
    )

    def get_effective_field_config(
        self, 
        asset_type: str, 
        field_name: str
    ) -> Optional[FieldConfig]:
        """
        Get the effective field configuration for an asset type and field.
        
        Handles inheritance from defaults and per-asset overrides.
        
        Args:
            asset_type: The asset type (e.g., 'data-product')
            field_name: The field name (e.g., 'title', 'description', 'owner')
            
        Returns:
            FieldConfig if the field is configured, None otherwise
        """
        asset_config = self.asset_types.get(asset_type)
        
        # Check if asset type is disabled
        if asset_config and not asset_config.enabled:
            return None
        
        # Check for field override in asset type config
        if asset_config and field_name in asset_config.fields:
            return asset_config.fields[field_name]
        
        # Check for extra_fields in asset type config
        if asset_config and field_name in asset_config.extra_fields:
            return asset_config.extra_fields[field_name]
        
        # Fall back to defaults if inheritance is enabled (or no asset config)
        if not asset_config or asset_config.inherit_defaults:
            default_fields = self.defaults.fields
            if hasattr(default_fields, field_name):
                return getattr(default_fields, field_name)
        
        return None

    def get_all_field_configs(
        self, 
        asset_type: str
    ) -> Dict[str, FieldConfig]:
        """
        Get all effective field configurations for an asset type.
        
        Args:
            asset_type: The asset type (e.g., 'data-product')
            
        Returns:
            Dict mapping field names to their configurations
        """
        result: Dict[str, FieldConfig] = {}
        asset_config = self.asset_types.get(asset_type)
        if not asset_config and asset_type.startswith("asset-"):
            asset_config = self.asset_types.get("asset")
        
        # Check if asset type is disabled
        if asset_config and not asset_config.enabled:
            return result
        
        # Start with defaults if inheritance is enabled
        if not asset_config or asset_config.inherit_defaults:
            default_fields = self.defaults.fields
            result["title"] = default_fields.title
            result["description"] = default_fields.description
            result["tags"] = default_fields.tags
        
        # Apply field overrides
        if asset_config:
            for field_name, field_config in asset_config.fields.items():
                result[field_name] = field_config
            
            # Add extra fields
            for field_name, field_config in asset_config.extra_fields.items():
                result[field_name] = field_config
        
        # Filter to only indexed fields
        return {k: v for k, v in result.items() if v.indexed}


# API models for exposing configuration via REST endpoints

class SearchConfigUpdate(BaseModel):
    """Model for updating search configuration via API."""
    defaults: Optional[DefaultsConfig] = None
    asset_types: Optional[Dict[str, AssetTypeConfig]] = None
    ranking: Optional[RankingConfig] = None
    hybrid: Optional[HybridConfig] = None


class SearchConfigResponse(BaseModel):
    """API response model for search configuration."""
    version: str
    defaults: DefaultsConfig
    asset_types: Dict[str, AssetTypeConfig]
    ranking: RankingConfig
    hybrid: HybridConfig = Field(default_factory=HybridConfig)
    
    @classmethod
    def from_config(cls, config: SearchConfig) -> "SearchConfigResponse":
        """Create response from SearchConfig."""
        return cls(
            version=config.version,
            defaults=config.defaults,
            asset_types=config.asset_types,
            ranking=config.ranking,
            hybrid=config.hybrid,
        )

