"""Section 1 of the analysis: Competitor vs Your Brand.

This module owns the instructions the LLM follows for the competitor section and the
post-processing that enforces the brief's rules on the returned data (competitors are always
marked as AI-generated suggestions, no duplicates, no self-references).
"""

from __future__ import annotations

import logging

from app.schemas import COMPETITOR_SOURCE_LABEL, Brand, Competitor, CompetitorAnalysis

logger = logging.getLogger(__name__)

MIN_COMPETITORS = 3

SECTION_1_INSTRUCTIONS = """\
SECTION 1 - competitor_analysis (Competitor vs Your Brand)
- brand_name: the analyzed brand's name as written on the website.
- brand_overview: 2 to 4 sentences describing the business. Use only facts stated in the website \
content: do not add founding years, awards, analyst recognition, office, region, country or \
employee counts, certifications or client figures that the page does not state.
- business_category: the main business category.
- main_products_services: the main products and services named on the website; keep this list \
consistent with the five services in Section 4.
- competitors: up to 5 real companies you are confident exist and that offer a comparable main \
service to a similar market (direct alternatives for a product, national or regional players for a \
local business). Aim for at least 3, but name fewer, or none, rather than inventing or guessing. \
Competitors are AI-generated suggestions from your general knowledge, not verified research: set \
source to "ai_generated_suggestion" on every entry. For each competitor give name, main_service \
(the comparable offering), target_market, 2 to 4 strengths and 2 to 4 weaknesses or gaps relative \
to the analyzed brand. Keep every strength and weakness cautious and relative to the analyzed \
brand: when the website gives no evidence, phrase it with words such as "likely", and never state \
a competitor's revenue, customer counts, prices, awards or market position as fact.
- strengths: 3 to 6 competitive advantages of the analyzed brand that are evidenced by its website \
content. Each strength must be backed by something actually written on the page (a certification, \
specialization, pricing model, geography, experience or integration named there); do not list \
awards, analyst rankings, certifications or scale figures the page does not state.
- opportunities: 3 to 6 concrete opportunities to improve the brand's market positioning, each \
tied to a gap you observed on the website or a competitor strength.
"""


def finalize_competitor_analysis(analysis: CompetitorAnalysis, brand: Brand) -> CompetitorAnalysis:
    """Apply the brief's Section 1 rules to the model output and return the cleaned section."""
    seen: set[str] = set()
    competitors: list[Competitor] = []
    for competitor in analysis.competitors:
        name = competitor.name.strip()
        key = name.lower()
        if not name or key in seen or key == brand.name.strip().lower():
            continue
        seen.add(key)
        competitors.append(
            competitor.model_copy(
                update={
                    "name": name,
                    "strengths": _clean_list(competitor.strengths),
                    "weaknesses": _clean_list(competitor.weaknesses),
                    "source": COMPETITOR_SOURCE_LABEL,
                }
            )
        )

    if len(competitors) < MIN_COMPETITORS:
        logger.warning(
            "Only %d competitor suggestion(s) returned for brand '%s' (brief asks for at least %d where reliable).",
            len(competitors),
            brand.name,
            MIN_COMPETITORS,
        )

    return analysis.model_copy(
        update={
            "brand_name": analysis.brand_name.strip() or brand.name,
            "main_products_services": _clean_list(analysis.main_products_services),
            "competitors": competitors,
            "strengths": _clean_list(analysis.strengths),
            "opportunities": _clean_list(analysis.opportunities),
        }
    )


def _clean_list(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(item.split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            cleaned.append(text)
    return cleaned
