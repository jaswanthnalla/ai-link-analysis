"""FastAPI application: the single ``POST /link-analysis`` endpoint (step 9 of the processing flow).

Run with: ``uvicorn app.main:app --host 0.0.0.0 --port 4000`` and open http://localhost:4000/docs
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas import ErrorDetail, ErrorResponse, LinkAnalysisError, LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import AnalysisService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI-Powered Website Link Analysis API",
    version="1.0.0",
    description=(
        "Submit a website URL and receive a structured analysis with five sections: competitor vs brand, "
        "top 5 target search queries, SEO improvements, top 5 services and target audience."
    ),
)

analysis_service = AnalysisService()


def _error_response(status_code: int, error_type: str, message: str, website: str | None = None) -> JSONResponse:
    payload = ErrorResponse(website=website, error=ErrorDetail(type=error_type, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump())


@app.exception_handler(LinkAnalysisError)
async def handle_link_analysis_error(_request: Request, exc: LinkAnalysisError) -> JSONResponse:
    logger.info("request failed type=%s status=%d message=%s", exc.error_type, exc.status_code, exc.message)
    return _error_response(exc.status_code, exc.error_type, exc.message, getattr(exc, "website", None))


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    details = "; ".join(
        f"{'.'.join(str(p) for p in error.get('loc', ()) if p != 'body') or 'body'}: {error.get('msg', 'invalid')}"
        for error in exc.errors()[:3]
    )
    return _error_response(422, "invalid_request", f"Invalid request body: {details}")


@app.exception_handler(Exception)
async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unexpected error: %s", exc)
    return _error_response(500, "internal_error", "An unexpected error occurred while analyzing the website.")


@app.post(
    "/link-analysis",
    response_model=LinkAnalysisResponse,
    response_model_exclude_none=False,
    summary="Analyze a website URL",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid or blocked URL"},
        422: {"model": ErrorResponse, "description": "Invalid request body or non-HTML website content"},
        502: {"model": ErrorResponse, "description": "Website inaccessible or LLM failure"},
    },
)
async def link_analysis(payload: LinkAnalysisRequest) -> LinkAnalysisResponse:
    """Fetch the website, analyze its content with the LLM and return the five analysis sections."""
    return await analysis_service.analyze(payload.url)
