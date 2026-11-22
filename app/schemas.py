"""Pydantic models: the request body, the LLM output contract and the API response contract.

The response shape follows the example response in the brief exactly (same keys, same nesting)
and adds the "Expected Information" fields of each section inside the matching key.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Platform = Literal["Google Search", "Facebook", "Instagram", "LinkedIn", "X (Twitter)"]
SUPPORTED_PLATFORMS: tuple[str, ...] = ("Google Search", "Facebook", "Instagram", "LinkedIn", "X (Twitter)")
COMPETITOR_SOURCE_LABEL = "ai_generated_suggestion"


class StrictModel(BaseModel):
    """Base for every model that is also sent to the LLM as a JSON schema (no extra keys allowed)."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- request


class LinkAnalysisRequest(BaseModel):
    """Request body of POST /link-analysis."""

    url: str = Field(..., description="Public website URL to analyze, for example https://example.com")


# --------------------------------------------------------------------------- brand


class Brand(StrictModel):
    name: str = Field(..., description="Brand or company name exactly as it appears on the website.")
    category: str = Field(..., description="Main business category, for example 'Procurement software'.")
    summary: str = Field(
        ..., description="One or two sentence summary of what the business does, grounded in the website text."
    )


# --------------------------------------------------------------------------- section 1


class Competitor(StrictModel):
    name: str = Field(..., description="Competitor brand name.")
    main_service: str = Field(
        ..., description="The competitor's main product or service, comparable to the analyzed brand."
    )
    target_market: str = Field(..., description="Market or customer segment the competitor focuses on.")
    strengths: list[str] = Field(..., description="Competitor strengths relative to the analyzed brand.")
    weaknesses: list[str] = Field(..., description="Competitor weaknesses or gaps relative to the analyzed brand.")
    source: Literal["ai_generated_suggestion"] = Field(
        ...,
        description="Always 'ai_generated_suggestion': competitors are suggested by the model, not verified research.",
    )


class CompetitorAnalysis(StrictModel):
    brand_name: str = Field(..., description="The analyzed brand's name.")
    brand_overview: str = Field(
        ..., description="Business overview of the analyzed brand, based only on the website content."
    )
    business_category: str = Field(..., description="Main business category of the analyzed brand.")
    main_products_services: list[str] = Field(..., description="Main products and services named on the website.")
    competitors: list[Competitor] = Field(
        ...,
        description=(
            "Up to five likely competitors the model is confident exist (ideally at least three), each marked "
            "as an AI-generated suggestion; fewer or none when confident suggestions are not available."
        ),
    )
    strengths: list[str] = Field(
        ..., description="The analyzed brand's competitive advantages versus the competitors."
    )
    opportunities: list[str] = Field(
        ..., description="Opportunities for the analyzed brand to improve its market positioning."
    )


# --------------------------------------------------------------------------- section 2


class SearchQuery(StrictModel):
    query: str = Field(..., description="A search query potential customers might type to find this business.")
    platforms: list[Platform] = Field(
        ..., description="Platforms where this query is suitable (one or more of the five supported platforms)."
    )
    intent: str = Field(
        ..., description="Search intent, for example 'Service Search', 'Business Discovery', 'B2B Search'."
    )
    business_relevance: str = Field(..., description="Why this query is relevant to the business and its services.")
    target_audience: str = Field(..., description="Who is most likely to search this query.")


# --------------------------------------------------------------------------- section 3


class SeoImprovements(StrictModel):
    primary_keyword: str = Field(..., description="Primary keyword the website should target.")
    secondary_keywords: list[str] = Field(..., description="Secondary keywords.")
    long_tail_keywords: list[str] = Field(..., description="Long-tail keywords.")
    relevant_business_keywords: list[str] = Field(
        ..., description="Other keywords relevant to the business and its services."
    )
    recommended_title: str = Field(
        ...,
        description=(
            "Recommended SEO title for the analyzed page: 50 to 60 characters including spaces, "
            "primary keyword first, brand name last."
        ),
    )
    title_primary_keyword: str = Field(..., description="The primary keyword used in the recommended title.")
    title_structure: str = Field(
        ..., description="Improved title structure, for example 'Primary Keyword | Key Benefit | Brand'."
    )
    meta_description: str = Field(
        ...,
        description=(
            "Recommended meta description: 120 to 160 characters including spaces, a short, human-friendly "
            "business summary with relevant keywords and a call to action."
        ),
    )
    meta_description_keywords: list[str] = Field(
        ..., description="Relevant keywords included in the meta description."
    )
    supporting_content: list[str] = Field(
        ...,
        description=(
            "Additional support content suggestions, each written as "
            "'<content type>: <concrete title or topic> - <why it helps search visibility>'. Content types: "
            "service landing page, industry page, FAQ, blog topic, location page, supporting article."
        ),
    )


# --------------------------------------------------------------------------- section 4


class Service(StrictModel):
    service_name: str = Field(..., description="Name of the service or product.")
    description: str = Field(..., description="Short explanation of the service.")
    target_industry: str = Field(..., description="Relevant business industry for this service.")
    target_customer: str = Field(..., description="Potential customer type for this service.")
    main_benefit: str = Field(..., description="Why customers need this service.")
    source: Literal["website_content", "inferred"] = Field(
        ...,
        description=(
            "'website_content' when the service is named on the website, "
            "'inferred' when it is only implied by the content."
        ),
    )


# --------------------------------------------------------------------------- section 5


class TargetAudience(StrictModel):
    primary_audience: list[str] = Field(..., description="Primary target audience groups.")
    secondary_audience: list[str] = Field(..., description="Secondary target audience groups.")
    industries: list[str] = Field(..., description="Industry segments served.")
    business_types: list[str] = Field(
        ..., description="Business types served, for example 'Enterprises', 'Startups'."
    )
    customer_profiles: list[str] = Field(..., description="Typical customer profiles, roles or personas.")
    geographic_focus: str = Field(
        ...,
        description=(
            "Location or geographic focus if the website states one; "
            "otherwise exactly 'Not stated on the website'."
        ),
    )
    customer_needs: list[str] = Field(..., description="Customer needs and challenges the business addresses.")
    customer_interests: list[str] = Field(..., description="Potential customer interests.")
    recommended_marketing_segments: list[str] = Field(
        ..., description="Recommended audience segments for marketing."
    )


# --------------------------------------------------------------------------- assembled analysis


class Analysis(StrictModel):
    # The "exactly five" rule is enforced by the validators below (after generation) rather than by
    # minItems/maxItems in the schema: the SDK would pass those keywords to the API unchanged, and
    # their acceptance could not be verified against the API in this build, while a rejected schema
    # would fail every request.
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery] = Field(..., description="Exactly five search queries.")
    seo_improvements: SeoImprovements
    top_services: list[Service] = Field(..., description="Exactly five services.")
    target_audience: TargetAudience

    @field_validator("target_search_queries")
    @classmethod
    def _exactly_five_queries(cls, value: list[SearchQuery]) -> list[SearchQuery]:
        if len(value) != 5:
            raise ValueError(f"target_search_queries must contain exactly 5 items, got {len(value)}")
        return value

    @field_validator("top_services")
    @classmethod
    def _exactly_five_services(cls, value: list[Service]) -> list[Service]:
        if len(value) != 5:
            raise ValueError(f"top_services must contain exactly 5 items, got {len(value)}")
        return value


class LLMAnalysisOutput(StrictModel):
    """The JSON object the LLM must return: brand details plus all five analysis sections."""

    brand: Brand
    analysis: Analysis


# --------------------------------------------------------------------------- API responses


class LinkAnalysisResponse(BaseModel):
    """Successful response of POST /link-analysis (shape of the brief's example response)."""

    success: Literal[True] = True
    website: str
    brand: Brand
    analysis: Analysis


class ErrorDetail(BaseModel):
    type: str = Field(..., description="Machine-readable error type.")
    message: str = Field(..., description="Human-readable explanation.")


class ErrorResponse(BaseModel):
    """Structured error response (same top-level style as the success response)."""

    success: Literal[False] = False
    website: str | None = None
    error: ErrorDetail


# --------------------------------------------------------------------------- errors


class LinkAnalysisError(Exception):
    """Base class for every graceful failure. Each subclass maps to one structured error response."""

    error_type: str = "error"
    status_code: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidURLError(LinkAnalysisError):
    """The submitted URL is syntactically invalid."""

    error_type = "invalid_url"
    status_code = 400


class BlockedURLError(LinkAnalysisError):
    """The submitted URL points to a private, internal or otherwise disallowed destination (SSRF protection)."""

    error_type = "blocked_url"
    status_code = 400


class WebsiteFetchError(LinkAnalysisError):
    """The website could not be fetched: DNS failure, timeout, connection error, HTTP error status."""

    error_type = "website_inaccessible"
    status_code = 502


class WebsiteContentError(LinkAnalysisError):
    """The website responded, but not with analyzable HTML content."""

    error_type = "website_content_unusable"
    status_code = 422


class LLMServiceError(LinkAnalysisError):
    """The LLM API call failed on the primary and the fallback model."""

    error_type = "llm_failure"
    status_code = 502

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        # False when trying the fallback model cannot help (for example a rejected API key).
        self.retryable = retryable


class InvalidLLMOutputError(LLMServiceError):
    """The LLM returned output that does not satisfy the response contract."""

    error_type = "invalid_llm_output"
    status_code = 502
