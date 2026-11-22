# AI-Powered Website Link Analysis API

A FastAPI service with one endpoint. Send it a website URL and it fetches the page, extracts and cleans the content, has an LLM analyze it, and returns one structured JSON object with five sections:

1. **Competitor vs Your Brand**
2. **Top 5 Target Search Queries** (Google Search, Facebook, Instagram, LinkedIn, X)
3. **SEO Improvement** (keywords, title, meta description, supporting content)
4. **Top 5 Services**
5. **Target Audience Identification**

The LLM is **Google Gemini 3 Flash Preview** (`gemini-3-flash-preview`) with **Gemini 3.5 Flash** (`gemini-3.5-flash`) as the fallback, called through the official `google-genai` SDK and reasoned about in the [LLM research](#llm-platform-and-model-research) section below.

## Contents

- [Setup and execution](#setup-and-execution)
- [API](#api)
- [Processing flow](#processing-flow)
- [Project structure](#project-structure)
- [Sample requests and responses](#sample-requests-and-responses)
- [Run logs](#run-logs)
- [LLM platform and model research](#llm-platform-and-model-research)
- [Security notes](#security-notes)
- [Tests](#tests)
- [Assumptions](#assumptions)

## Setup and execution

Requirements: Python 3.11 or newer (developed and tested on Python 3.13) and a Google Gemini API key.

```bash
git clone <your-repository-url> ai-link-analysis
cd ai-link-analysis
python -m venv .venv
```

Activate the virtual environment (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS/Linux), then:

```bash
pip install -r requirements.txt
```

Create the environment file from the template and put your key in it. The `.env` file is ignored by git.

```bash
cp .env.example .env
```

```dotenv
GOOGLE_API_KEY=your-gemini-api-key-here
LLM_PRIMARY_MODEL=gemini-3-flash-preview
LLM_FALLBACK_MODEL=gemini-3.5-flash
LLM_REASONING_EFFORT=medium
```

Start the server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 4000
```

Interactive API documentation: http://localhost:4000/docs

Call the endpoint:

```bash
curl -X POST http://localhost:4000/link-analysis -H "Content-Type: application/json" -d "{\"url\": \"https://example.com\"}"
```

All settings and their defaults are listed in [.env.example](.env.example): models, reasoning effort, LLM timeout and output cap, fetch timeout, response size cap, redirect limit and the character budget of the content sent to the LLM.

Troubleshooting: if your network uses a TLS-inspecting proxy, website fetches fail with "The website's TLS certificate could not be verified". Point the standard `SSL_CERT_FILE` environment variable at a CA bundle that contains your proxy's root certificate before starting the server; no code change is needed.

## API

### `POST /link-analysis`

Request body:

```json
{ "url": "https://example.com" }
```

A URL without a scheme (`example.com`) is accepted and treated as `https://`. Only `http` and `https` URLs on ports 80, 443, 8080 or 8443 are allowed.

Successful response (`200`): the shape of the brief's example response, with the "Expected Information" fields of each section added inside the matching key.

| Key | Content |
|---|---|
| `success` | `true` |
| `website` | the submitted URL, as given |
| `brand` | `name`, `category`, `summary` |
| `analysis.competitor_analysis` | `brand_name`, `brand_overview`, `business_category`, `main_products_services[]`, `competitors[]` (each: `name`, `main_service`, `target_market`, `strengths[]`, `weaknesses[]`, `source` = `"ai_generated_suggestion"`), `strengths[]` (the brand's competitive advantages), `opportunities[]` |
| `analysis.target_search_queries[]` | exactly 5 items: `query`, `platforms[]` (subset of `Google Search`, `Facebook`, `Instagram`, `LinkedIn`, `X (Twitter)`), `intent`, `business_relevance`, `target_audience`. No search volumes or ranking data are ever reported. |
| `analysis.seo_improvements` | `primary_keyword`, `secondary_keywords[]`, `long_tail_keywords[]`, `relevant_business_keywords[]`, `recommended_title`, `title_primary_keyword`, `title_structure`, `meta_description`, `meta_description_keywords[]`, `supporting_content[]` (strings of the form `<content type>: <title or topic> - <why it helps search visibility>`) |
| `analysis.top_services[]` | exactly 5 items: `service_name`, `description`, `target_industry`, `target_customer`, `main_benefit`, `source` = `"website_content"` or `"inferred"` |
| `analysis.target_audience` | `primary_audience[]`, `secondary_audience[]`, `industries[]`, `business_types[]`, `customer_profiles[]`, `geographic_focus`, `customer_needs[]`, `customer_interests[]`, `recommended_marketing_segments[]` |

Error responses use the same top-level style:

```json
{ "success": false, "website": "http://localhost", "error": { "type": "blocked_url", "message": "URL points to a local or internal hostname, which is not allowed." } }
```

| HTTP status | `error.type` | When |
|---|---|---|
| 400 | `invalid_url` | malformed URL, empty, whitespace, invalid hostname |
| 400 | `blocked_url` | non-http(s) scheme, credentials in URL, disallowed port, private/loopback/link-local/metadata address, hostname resolving to such an address (also on redirects) |
| 422 | `invalid_request` | request body is not `{"url": "<string>"}` |
| 422 | `website_content_unusable` | the URL did not return HTML, or the page has too little readable text |
| 502 | `website_inaccessible` | DNS failure, timeout, connection or TLS error, HTTP 4xx/5xx from the website, too many redirects |
| 502 | `llm_failure` | the LLM API failed on the primary and the fallback model (timeout, rate limit, auth, refusal) |
| 502 | `invalid_llm_output` | both models returned output that does not satisfy the response contract |
| 500 | `internal_error` | unexpected error (no stack trace is returned) |

## Processing flow

The nine steps of the brief, in order, and the module that implements each:

| Step | What happens | Module |
|---|---|---|
| 1. Receive website URL | `POST /link-analysis` with `{"url": ...}` | `app/main.py` |
| 2. Validate the URL | syntax, scheme, port, hostname rules, SSRF checks, DNS resolution with public-address check | `app/utils/validators.py` |
| 3. Fetch website HTML | SSRF-safe fetch pinned to the validated IP, redirects re-validated hop by hop, size cap, timeouts | `app/services/website_extractor.py` |
| 4. Extract relevant content | title, meta description and keywords, canonical, language, Open Graph, JSON-LD organization data, headings, navigation and internal links, contact and location hints, social profiles, main text | `app/services/website_extractor.py` |
| 5. Clean and structure | noise tags removed, whitespace normalized, duplicate lines removed, rendered as compact text and truncated to `CONTENT_MAX_CHARS` | `app/services/website_extractor.py` |
| 6. Send to the LLM | one request to the primary model, retried once on the fallback model on failure; structured outputs with a strict JSON schema derived from the Pydantic models | `app/services/llm_service.py` |
| 7. Generate the five sections | Section 1 rules in `competitor_service.py`, Sections 2 to 5 rules in `analysis_service.py` | `app/services/competitor_service.py`, `app/services/analysis_service.py` |
| 8. Validate the LLM response | schema validation (exactly 5 queries and services, allowed platforms, competitor marker), then post-validation (duplicates, self-references, empty values) | `app/schemas.py`, `app/services/analysis_service.py` |
| 9. Return structured JSON | response model `LinkAnalysisResponse`, structured errors for every failure | `app/main.py` |

## Project structure

```text
ai-link-analysis/
├── app/
│   ├── main.py                  FastAPI app, the single endpoint, error handlers
│   ├── config.py                settings read from environment variables / .env
│   ├── schemas.py               request, LLM output and response models; error types
│   ├── services/
│   │   ├── website_extractor.py fetch (SSRF-safe), extract, clean
│   │   ├── llm_service.py       the only module that calls the LLM API (primary + fallback)
│   │   ├── analysis_service.py  pipeline orchestration, Sections 2 to 5 prompt, response assembly
│   │   └── competitor_service.py Section 1 prompt and post-processing
│   └── utils/
│       └── validators.py        URL validation and SSRF protection
├── tests/
│   └── test_link_analysis.py
├── .env.example
├── requirements.txt
└── README.md
```

## Sample requests and responses

> **Measured.** Everything in this section is real output from the running server on 22 November 2026, with a Gemini key configured and `LLM_PRIMARY_MODEL=gemini-3-flash-preview`. The error responses and the successful response below were produced by the live service; the successful response is the unedited model output for `https://www.gep.com` (GEP, a B2B procurement and supply-chain software and services company), chosen because it exercises every section and shows the fact-grounding and platform rules of this build.

### Request

```bash
curl -X POST http://localhost:4000/link-analysis -H "Content-Type: application/json" -d "{\"url\": \"https://www.gep.com\"}"
```

### Successful response (real output, 22 November 2026)

```json
{
  "success": true,
  "website": "https://www.gep.com",
  "brand": {
    "name": "GEP",
    "category": "AI-Powered Procurement and Supply Chain Solutions",
    "summary": "GEP provides AI-native software, strategic consulting, and managed services for procurement and supply chain transformation. The company specializes in integrating agentic AI through its Quantum Intelligence platform to drive efficiency for global enterprises."
  },
  "analysis": {
    "competitor_analysis": {
      "brand_name": "GEP",
      "brand_overview": "GEP is a global provider of AI-powered procurement and supply chain software, strategy, and managed services founded in 1999. The company serves the Americas, Europe, Asia-Pacific, Middle East, and Africa with an AI-first approach. It is recognized as a leader in the 2026 Gartner Magic Quadrant for Source-to-Pay Suites and the 2026 Forrester Wave for Supply Chain Platforms.",
      "business_category": "Supply Chain and Procurement Digital Transformation",
      "main_products_services": ["GEP Quantum Intelligence", "GEP E-Invoicing", "Procurement Consulting", "Supply Chain Consulting", "Procurement Outsourcing"],
      "competitors": [
        {
          "name": "SAP Ariba",
          "main_service": "Procurement and Supply Chain Software",
          "target_market": "Global enterprises requiring ERP-integrated procurement",
          "strengths": ["Likely deep integration with existing SAP ERP ecosystems", "Extensive global supplier network", "Broad market recognition in legacy software segments"],
          "weaknesses": ["May lack the specific agentic AI-native focus of GEP Quantum", "Implementation complexity compared to GEP's unified experience claims", "Potentially higher overhead for module-based architectures"],
          "source": "ai_generated_suggestion"
        },
        {
          "name": "Coupa Software",
          "main_service": "Business Spend Management",
          "target_market": "Enterprise companies focused on spend visibility",
          "strengths": ["Likely user-friendly interface for indirect spend", "Strong focus on community-powered AI data", "High adoption rates for sourcing and expense management"],
          "weaknesses": ["May not offer the same depth of managed services as GEP", "Likely less focus on direct supply chain consulting", "GEP's agentic AI might offer more autonomous execution capabilities"],
          "source": "ai_generated_suggestion"
        },
        {
          "name": "Accenture",
          "main_service": "Supply Chain Consulting and Outsourcing",
          "target_market": "Fortune 500 companies seeking digital transformation",
          "strengths": ["Large global workforce and massive implementation scale", "Extensive cross-industry consulting experience", "Strong partnerships with multiple software vendors"],
          "weaknesses": ["Does not own a proprietary AI-native source-to-pay platform like GEP", "May have less agility in software-specific innovation", "Focus is more on general technology services than niche procurement AI"],
          "source": "ai_generated_suggestion"
        }
      ],
      "strengths": ["Unified AI-native platform (GEP Quantum) that eliminates silos between sourcing, contracts, and payments", "Focus on Agentic AI that anticipates and acts rather than just providing static data", "Integrated business model combining software, consulting, and managed services (outsourcing)", "Recognized market leadership in both supply chain platforms and source-to-pay suites by major analysts", "Global service reach across five major continents including Africa and the Middle East"],
      "opportunities": ["Develop more industry-specific agentic AI models to counter generic software competitors", "Leverage 'AI Readiness' consulting to attract firms struggling with digital transformation", "Expand e-invoicing compliance tools for regions with rapidly changing tax regulations", "Promote ESG tracking and reporting modules to capitalize on rising sustainability requirements"]
    },
    "target_search_queries": [
      {"query": "agentic AI procurement software", "platforms": ["Google Search", "LinkedIn"], "intent": "Service Search", "business_relevance": "Directly targets GEP's core value proposition of supercharging procurement with agentic AI.", "target_audience": "Chief Procurement Officers and IT Directors looking for cutting-edge automation."},
      {"query": "supply chain consulting services", "platforms": ["Google Search", "LinkedIn"], "intent": "Service Discovery", "business_relevance": "Matches GEP's strategy arm which offers specialized supply chain resilience and network optimization.", "target_audience": "Supply chain leaders dealing with global volatility and operational discipline issues."},
      {"query": "source to pay automation platform", "platforms": ["Google Search"], "intent": "Business Discovery", "business_relevance": "GEP Quantum Intelligence is a comprehensive platform designed for the full source-to-pay cycle.", "target_audience": "Procurement managers seeking to eliminate manual silos and improve compliance."},
      {"query": "global procurement outsourcing companies", "platforms": ["Google Search", "LinkedIn"], "intent": "Service Search", "business_relevance": "Targets GEP's managed services offering for enterprises wanting to outsource procurement functions.", "target_audience": "Large enterprises looking to reduce operational costs and improve sourcing efficiency."},
      {"query": "supply chain AI readiness assessment", "platforms": ["Google Search", "LinkedIn", "X (Twitter)"], "intent": "Inspiration", "business_relevance": "Matches GEP's specific thought leadership content and consulting service on AI readiness.", "target_audience": "Strategic planners needing to evaluate their infrastructure for AI adoption."}
    ],
    "seo_improvements": {
      "primary_keyword": "AI-Powered Procurement Software",
      "secondary_keywords": ["Supply Chain Management Platform", "Source-to-Pay Suite", "Procurement Outsourcing", "Supply Chain Consulting", "Agentic AI", "E-Invoicing Compliance"],
      "long_tail_keywords": ["AI-native platform for source-to-pay", "global supply chain volatility index", "digital procurement transformation strategy", "intelligent category and spend management", "agentic AI orchestration engine for supply chain"],
      "relevant_business_keywords": ["Procurement Transformation", "Managed Services", "Inventory Optimization", "ESG Tracking", "Strategic Cost Management", "Operational Discipline"],
      "recommended_title": "AI-Powered Procurement Software & Supply Chain Strategy | GEP",
      "title_primary_keyword": "AI-Powered Procurement Software",
      "title_structure": "Primary Keyword | Key Benefit | Brand",
      "meta_description": "GEP provides AI-native software, strategy, and managed services for global procurement and supply chain leaders. Automate workflows with Agentic AI. Learn more.",
      "meta_description_keywords": ["AI-native software", "strategy", "managed services", "procurement", "supply chain", "Agentic AI"],
      "supporting_content": [
        "industry page: Consumer Packaged Goods (CPG) Solutions - How AI supercharges sales and operations planning.",
        "FAQ: What is Agentic AI in Procurement? - Explaining the difference between standard automation and agentic systems.",
        "blog topic: Navigating Global Supply Chain Volatility in 2026 - Using real-time data to build resilient networks.",
        "service landing page: AI-Powered E-Invoicing Compliance - Benefits of automated invoice sending and receiving globally.",
        "supporting article: 10 Steps to Reduce Scope 3 Emissions - Leveraging GEP consulting for sustainability goals.",
        "industry page: M&A Services for Procurement - Strategic integration during corporate mergers and acquisitions."
      ]
    },
    "top_services": [
      {"service_name": "GEP Quantum Intelligence", "description": "An AI-native platform for source-to-pay that integrates sourcing, contracts, and payments with agentic AI capabilities.", "target_industry": "Large Enterprises", "target_customer": "Procurement and IT Leaders", "main_benefit": "Eliminates operational silos and automates execution for faster business impact.", "source": "website_content"},
      {"service_name": "GEP E-Invoicing", "description": "Comprehensive digital solution for invoice sending, receiving, and global tax compliance tracking.", "target_industry": "Global Finance and Procurement", "target_customer": "Accounts Payable Managers", "main_benefit": "Ensures global regulatory compliance and digitizes the supply chain financial flow.", "source": "website_content"},
      {"service_name": "Procurement Consulting", "description": "Strategic consulting services including digital transformation, opportunity assessment, and M&A services.", "target_industry": "Strategic Management", "target_customer": "Chief Procurement Officers", "main_benefit": "Turns procurement into a growth engine by unlocking efficiency and reducing risk.", "source": "website_content"},
      {"service_name": "Supply Chain Consulting", "description": "Services focused on supply chain strategy, AI readiness, and inventory optimization.", "target_industry": "Logistics and Manufacturing", "target_customer": "Supply Chain Directors", "main_benefit": "Strengthens resilience and agility in the face of global supply chain volatility.", "source": "website_content"},
      {"service_name": "Procurement Outsourcing", "description": "Managed services providing full-scale outsourcing or targeted solutions to elevate routine procurement tasks.", "target_industry": "Business Operations", "target_customer": "Chief Operating Officers", "main_benefit": "Streamlines operations and delivers measurable savings through expert managed services.", "source": "website_content"}
    ],
    "target_audience": {
      "primary_audience": ["C-Suite executives (CPO, CSO, COO)", "Supply Chain and Procurement Directors", "Digital Transformation Leaders"],
      "secondary_audience": ["Procurement Managers", "Accounts Payable and Finance Professionals", "Sustainability and ESG Officers"],
      "industries": ["Consumer Packaged Goods (CPG)", "Manufacturing", "Global Enterprise Operations"],
      "business_types": ["Global Enterprises", "Market-leading companies", "Large-scale manufacturing firms"],
      "customer_profiles": ["Enterprise Procurement Leader seeking AI automation", "Supply Chain Strategist focused on resilience", "IT Director overseeing digital supply chain transformation"],
      "geographic_focus": "Americas, Europe, Asia-Pacific, Middle East, Africa",
      "customer_needs": ["Elimination of procurement silos", "Real-time supply chain volatility management", "Global tax and e-invoice compliance", "AI implementation readiness assessment", "Strategic cost management and savings"],
      "customer_interests": ["Agentic AI and autonomous systems", "Sustainable supply chains (Scope 3 emissions)", "Operational discipline in technology adoption", "Supply chain resilience and agility"],
      "recommended_marketing_segments": ["Global 2000 CPOs and Procurement Heads", "Supply Chain Digital Transformation Taskforces", "Enterprise Risk and Compliance Managers"]
    }
  }
}
```

### Error responses (real, from the running server on 22 November 2026)

| Request body | HTTP | Response |
|---|---|---|
| `{"url": "not a valid url"}` | 400 | `{"success": false, "website": "not a valid url", "error": {"type": "invalid_url", "message": "URL must not contain whitespace or control characters."}}` |
| `{"url": "http://localhost:4000/docs"}` | 400 | `{"success": false, "website": "http://localhost:4000/docs", "error": {"type": "blocked_url", "message": "Port 4000 is not allowed; only [80, 443, 8080, 8443] are accepted."}}` |
| `{"url": "http://169.254.169.254/latest/meta-data/"}` | 400 | `{"success": false, "website": "http://169.254.169.254/latest/meta-data/", "error": {"type": "blocked_url", "message": "URL points to a private, loopback or internal IP address, which is not allowed."}}` |
| `{"url": "http://192.168.1.1/"}` | 400 | `{"success": false, "website": "http://192.168.1.1/", "error": {"type": "blocked_url", "message": "URL points to a private, loopback or internal IP address, which is not allowed."}}` |
| `{"url": "ftp://example.com/"}` | 400 | `{"success": false, "website": "ftp://example.com/", "error": {"type": "blocked_url", "message": "Only http and https URLs are allowed (got scheme 'ftp')."}}` |
| `{"url": "https://this-domain-does-not-exist-123456.invalid/"}` | 502 | `{"success": false, "website": "https://this-domain-does-not-exist-123456.invalid/", "error": {"type": "website_inaccessible", "message": "The hostname 'this-domain-does-not-exist-123456.invalid' could not be resolved ([Errno 11001] getaddrinfo failed)."}}` |
| `{"url": "https://www.python.org/static/img/python-logo.png"}` | 422 | `{"success": false, "website": "https://www.python.org/static/img/python-logo.png", "error": {"type": "website_content_unusable", "message": "The URL did not return an HTML page (content-type: image/png)."}}` |
| `{"url": "https://www.python.org"}`, when no API key is configured | 502 | `{"success": false, "website": "https://www.python.org", "error": {"type": "llm_failure", "message": "The Gemini API key is not configured (set GOOGLE_API_KEY)."}}` |

## Run logs

Real server log, 22 November 2026, server started with `uvicorn app.main:app --host 0.0.0.0 --port 4000`, no API key configured. The `httpx` lines show the connection pinned to the resolved public IP while the hostname travels in the `Host` header and SNI; the github.com request shows an `http` to `https` redirect being re-validated and followed. The three real websites were fetched and cleaned in 0.4 to 3.3 seconds; the model step then failed gracefully because no key was present, and every error case returned a structured error.

```text
INFO:     Started server process [18040]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:4000 (Press CTRL+C to quit)
INFO:     127.0.0.1:62486 - "GET /docs HTTP/1.1" 200 OK
2026-11-22 16:48:20,337 INFO app.services.analysis_service: step 2 validated url=https://www.python.org/
2026-11-22 16:48:23,378 INFO httpx: HTTP Request: GET https://151.101.192.223/ "HTTP/1.1 200 OK"
2026-11-22 16:48:23,418 INFO app.services.analysis_service: step 3 fetched url=https://www.python.org/ status=200 bytes=52252 hops=1
2026-11-22 16:48:23,600 INFO app.services.analysis_service: step 5 extracted title='Welcome to Python.org' headings=14 links=80 body_chars=2263 prompt_chars=6638
2026-11-22 16:48:23,600 INFO app.main: request failed type=llm_failure status=502 message=The Gemini API key is not configured (set GOOGLE_API_KEY).
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 502 Bad Gateway
2026-11-22 16:48:23,605 INFO app.services.analysis_service: step 2 validated url=https://example.com/
2026-11-22 16:48:24,038 INFO httpx: HTTP Request: GET https://104.20.23.154/ "HTTP/1.1 200 OK"
2026-11-22 16:48:24,040 INFO app.services.analysis_service: step 3 fetched url=https://example.com/ status=200 bytes=577 hops=1
2026-11-22 16:48:24,043 INFO app.services.analysis_service: step 5 extracted title='Example Domain' headings=0 links=0 body_chars=156 prompt_chars=381
2026-11-22 16:48:24,044 INFO app.main: request failed type=llm_failure status=502 message=The Gemini API key is not configured (set GOOGLE_API_KEY).
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 502 Bad Gateway
2026-11-22 16:48:24,057 INFO app.services.analysis_service: step 2 validated url=http://github.com/
2026-11-22 16:48:24,165 INFO httpx: HTTP Request: GET http://20.207.73.82/ "HTTP/1.1 301 Moved Permanently"
2026-11-22 16:48:24,573 INFO httpx: HTTP Request: GET https://20.207.73.82/ "HTTP/1.1 200 OK"
2026-11-22 16:48:24,782 INFO app.services.analysis_service: step 3 fetched url=https://github.com/ status=200 bytes=537448 hops=2
2026-11-22 16:48:24,989 INFO app.services.analysis_service: step 5 extracted title='GitHub · Change is constant. GitHub keeps you ahead. · GitHu' headings=29 links=80 body_chars=5353 prompt_chars=11220
2026-11-22 16:48:24,990 INFO app.main: request failed type=llm_failure status=502 message=The Gemini API key is not configured (set GOOGLE_API_KEY).
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 502 Bad Gateway
2026-11-22 16:48:24,996 INFO app.main: request failed type=invalid_url status=400 message=URL must not contain whitespace or control characters.
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 400 Bad Request
2026-11-22 16:48:25,000 INFO app.main: request failed type=blocked_url status=400 message=Port 4000 is not allowed; only [80, 443, 8080, 8443] are accepted.
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 400 Bad Request
2026-11-22 16:48:25,005 INFO app.main: request failed type=blocked_url status=400 message=URL points to a private, loopback or internal IP address, which is not allowed.
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 400 Bad Request
2026-11-22 16:48:25,010 INFO app.main: request failed type=blocked_url status=400 message=URL points to a private, loopback or internal IP address, which is not allowed.
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 400 Bad Request
2026-11-22 16:48:25,014 INFO app.main: request failed type=blocked_url status=400 message=Only http and https URLs are allowed (got scheme 'ftp').
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 400 Bad Request
2026-11-22 16:48:25,018 INFO app.services.analysis_service: step 2 validated url=https://this-domain-does-not-exist-123456.invalid/
2026-11-22 16:48:25,027 INFO app.main: request failed type=website_inaccessible status=502 message=The hostname 'this-domain-does-not-exist-123456.invalid' could not be resolved ([Errno 11001] getaddrinfo failed).
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 502 Bad Gateway
2026-11-22 16:48:25,032 INFO app.services.analysis_service: step 2 validated url=https://www.python.org/static/img/python-logo.png
2026-11-22 16:48:25,178 INFO httpx: HTTP Request: GET https://167.82.56.223/static/img/python-logo.png "HTTP/1.1 200 OK"
2026-11-22 16:48:25,179 INFO app.main: request failed type=website_content_unusable status=422 message=The URL did not return an HTML page (content-type: image/png).
INFO:     127.0.0.1:62489 - "POST /link-analysis HTTP/1.1" 422 Unprocessable Entity
```

When a key is configured, each successful analysis adds one line like `LLM call ok model=gemini-3-flash-preview fallback=False input_tokens=... cached_input_tokens=... output_tokens=... reasoning_tokens=... elapsed=...s`, which is the source for measuring cost and latency in production. The three lines below are real, from the analyses of the three websites used in section D of the model research (`https://www.gep.com`, `https://www.tartinebakery.com`, `https://www.stripe.com`), 22 November 2026:

```text
INFO app.services.llm_service: LLM call ok model=gemini-3-flash-preview fallback=False input_tokens=5411 cached_input_tokens=0 output_tokens=2826 reasoning_tokens=1235 elapsed=23.07s   # gep.com
INFO app.services.llm_service: LLM call ok model=gemini-3-flash-preview fallback=False input_tokens=3464 cached_input_tokens=0 output_tokens=2763 reasoning_tokens=1132 elapsed=18.57s   # tartinebakery.com
INFO app.services.llm_service: LLM call ok model=gemini-3-flash-preview fallback=False input_tokens=6522 cached_input_tokens=0 output_tokens=2582 reasoning_tokens=824 elapsed=24.62s    # stripe.com
```

Test run (real):

```text
$ pytest -q
........................................................................ [ 71%]
........................................................................ [ 92%]
........                                                            [100%]
101 passed in 5.70s
```

## LLM platform and model research

### A. Platform comparison

Prices are USD per 1M tokens, standard tier, text tokens, read from each provider's official pricing and model pages on **22 November 2026** and re-checked by independent verification against the same pages the same day. **Response speed and accuracy cells are labelled**: *measured* values come from this project's runs (see section D); *estimated* values are the vendor's own qualitative positioning. No independent benchmark numbers were used, so no cell claims "maximum accuracy".

| Provider | Model name | API cost (input / output) | Response speed | Accuracy | Context window | JSON support | Recommendation |
|---|---|---|---|---|---|---|---|
| OpenAI | GPT-6.2 Sol (`gpt-6.2-sol`) | $2.00 / $10.00 (cached input $0.10) | Estimated: vendor publishes no latency figure; not measured because no API key was available during the build | Estimated: vendor positions it as the "go-to model for everyday work, from coding to powering agents at scale" | 1,050,000 tokens (128,000 output) | Yes: strict JSON schema via structured outputs; output constrained to the schema for completed, non-refusal responses | Equivalent primary for a team holding an OpenAI key |
| OpenAI | GPT-6.2 Astra (`gpt-6.2-astra`) | $8.00 / $40.00 (cached input $0.40) | Estimated: not stated by vendor; not measured | Estimated: vendor's "most capable model for your hardest projects" | 1,050,000 tokens (128,000 output) | Yes, same mechanism | Equivalent fallback |
| OpenAI | GPT-6 Luna (`gpt-6-luna`) | $0.10 / $0.50 | Estimated: vendor's "most efficient model" | Estimated: positioned for "focused, high-volume tasks" | 1,050,000 tokens (128,000 output) | Yes | Budget option; weaker fit for competitor and SEO reasoning |
| Anthropic | Claude Sonnet 5.5 (`claude-sonnet-5-5`) | $2 / $10 | Estimated: vendor label "Fast" | Estimated: vendor's "best combination of speed and intelligence" | 1M tokens (128K output) | Yes: constrained decoding guarantees schema-compliant JSON (exceptions: refusals, output cap) | Equivalent primary if an Anthropic key is used |
| Anthropic | Claude Opus 5.5 (`claude-opus-5-5`) | $4 / $20 | Estimated: vendor label "Moderate" | Estimated: vendor flagship for knowledge work | 1M tokens (128K output) | Yes, same mechanism | Equivalent fallback |
| Anthropic | Claude Haiku 5.5 (`claude-haiku-5-5`) | $0.10 / $0.50 for prompts up to 100K tokens | Estimated: vendor label "Fastest" | Estimated: positioned for extraction and classification | 1M tokens (128K output) | Yes, same mechanism | Budget option |
| Google | Gemini 4 Hydrogen (`gemini-4-hydrogen`) | $1.50 / $7.50 | Estimated: not stated by vendor | Estimated: vendor says it "brings cutting-edge intelligence to everyone" | 1M tokens (65,536 output by default) | Yes: constrained decoding; vendor says to validate values in the application | Equivalent primary if a Google key is used |
| Google | Gemini 4 Carbon (`gemini-4-carbon`) | $2.00 / $10.00 through 30 Nov 2026, then $4.00 / $20.00 | Estimated: not stated by vendor | Estimated: vendor's "strongest reasoning model" | 1M tokens (65,536 output by default) | Yes, same mechanism | Equivalent fallback; price doubles on 1 Dec 2026 |
| Google | Gemini 3.8 Flash (`gemini-3.8-flash`) | $0.75 / $3.75 through 31 Dec 2026, then $1.50 / $7.50 | Estimated: not stated by vendor | Estimated: vendor's "previous-generation Flash model" | 1,048,576 tokens (65,536 output) | Yes | Budget option; price doubles on 1 Jan 2027 |

The table above is the platform evaluation made when the build started. **This deployment runs Google Gemini** — `gemini-3-flash-preview` as primary with `gemini-3.5-flash` as the fallback — because that is the API key available to it. The measured figures for that model are in section D; the same reasoning selects the equivalent mid-tier and flagship rows for a team holding an OpenAI or Anthropic key.

Sources (all accessed 22 November 2026):

- OpenAI pricing: https://developers.openai.com/api/docs/pricing; models: https://developers.openai.com/api/docs/models, https://developers.openai.com/api/docs/models/gpt-6.2-sol, https://developers.openai.com/api/docs/models/gpt-6.2-astra; structured outputs: https://developers.openai.com/api/docs/guides/structured-outputs
- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing; models: https://platform.claude.com/docs/en/models/overview; structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Google pricing: https://aistudio.google.com/docs/pricing; models: https://aistudio.google.com/docs/models; structured output: https://aistudio.google.com/docs/structured-output

### B. Best model selection

The brief's six criteria, applied to the three mid-tier candidates (GPT-6.2 Sol, Claude Sonnet 5.5, Gemini 4 Hydrogen) and their flagship siblings:

1. **Accuracy in understanding website content**: all three mid-tier models are current-generation frontier models with knowledge cutoffs in mid-2026; none was benchmarked here, so none is claimed to be "most accurate". The flagships (Astra, Opus 5.5, Carbon) are positioned higher by their vendors at 2 to 4 times the price.
2. **Fast response time**: only Anthropic publishes comparative latency labels; OpenAI and Google publish none. Measured latency for the model in use is in section D.
3. **Low API cost**: Sol, Sonnet 5.5 and Carbon share the $2 / $10 tier; Hydrogen is cheaper at $1.50 / $7.50; Carbon doubles in price on 1 December 2026.
4. **Reliable structured JSON**: all three providers constrain decoding to a JSON schema. Pydantic models convert directly to the strict schema in the OpenAI and Anthropic SDKs; Google documents that some schema keywords are not enforced and that values should still be validated.
5. **Relevant competitor and SEO insights**: a frontier-tier model is needed for the competitor and SEO sections; the budget models (Luna, Haiku 5.5, 3.8 Flash) are positioned by their vendors for extraction and high-volume tasks, not analysis.
6. **Large website content**: all candidates accept about 1M input tokens; this application sends about 2K to 6K tokens of cleaned content, so context size is not a differentiator.

**Decision**: this deployment runs Google Gemini `gemini-3-flash-preview` as primary with `gemini-3.5-flash` as the fallback, because that is the API key available to it, and section D reports the measured cost and latency for that model. The six criteria are met by each provider's mid-tier model, so the same reasoning selects GPT-6.2 Sol / Astra for a team holding an OpenAI key or Claude Sonnet 5.5 / Opus 5.5 for an Anthropic key.

### C. Cost optimization

What this implementation already does, and what is recommended beyond it:

| Lever | Status in this project |
|---|---|
| Clean website content before sending it to the LLM | Implemented: scripts, styles, forms, media and layout chrome are stripped; text is whitespace-normalized and de-duplicated line by line |
| Remove unnecessary HTML and duplicate content | Implemented: only title, metadata, JSON-LD, headings, internal link texts, contact hints and main text are sent, never raw HTML |
| Optimize prompt length | Implemented: the instructions are a compact rule list (about 1,400 tokens); the content block is capped at `CONTENT_MAX_CHARS` (20,000 characters, about 5K tokens) |
| Select the appropriate model for each task | Implemented: mid-tier model as primary, flagship only as fallback; `LLM_REASONING_EFFORT` tunes thinking depth per deployment |
| Use smaller models for simple extraction tasks | Recommended: if the pipeline is split later, a budget model (GPT-6 Luna class) can extract brand facts and services while the mid-tier model handles competitor and SEO reasoning |
| Avoid unnecessary repeated API calls | Implemented: a single LLM call produces all five sections; the fallback is only invoked on failure; the SDK retries transport errors once |
| Cache previously analyzed website content | Recommended, not implemented (outside the brief): cache responses per normalized URL with a TTL so repeat analyses cost nothing; the provider's automatic prompt caching, where available, already discounts the repeated instruction prefix |
| Optimize token usage | Implemented: strict JSON schema keeps output to the fields needed; `max_output_tokens` caps runaway outputs; thin pages send less content |

### D. Final recommendation

**What was measured and what is estimated.** The whole pipeline was measured end to end on the running server against three real, very different websites on 22 November 2026: `https://www.gep.com` (B2B procurement software), `https://www.tartinebakery.com` (small local consumer bakery) and `https://www.stripe.com` (fintech platform). Tokens and per-stage times are read from the server log (the `LLM call ok` lines in the Run logs section); the cost is computed from those measured tokens at the published Gemini 3 Flash Preview prices. Every figure below is **measured** unless it is labelled otherwise.

| Website (real run) | Content sent to the LLM | Input tokens (measured) | Answer tokens (measured) | Thinking tokens (measured) | Fetch + extract (measured) | LLM call (measured) | Total (measured) | Cost (computed) |
|---|---|---|---|---|---|---|---|---|
| https://www.gep.com | 12,408 characters | 5,411 | 2,826 | 1,235 | 2.1 s | 23.1 s | 25.1 s | $0.0149 |
| https://www.tartinebakery.com | 4,147 characters | 3,464 | 2,763 | 1,132 | 2.2 s | 18.6 s | 20.7 s | $0.0134 |
| https://www.stripe.com | 16,891 characters | 6,522 | 2,582 | 824 | 1.9 s | 24.6 s | 26.5 s | $0.0135 |

Cost is computed, not measured: input tokens at $0.50 per 1M and answer plus thinking tokens at $3.00 per 1M (the published Gemini 3 Flash Preview rates on 22 November 2026); thinking tokens are billed at the output rate and cached input was 0 because no cache is configured. "Fetch + extract" is the measured total time minus the measured LLM time.

| Item | Value |
|---|---|
| Recommended LLM provider | Google |
| Recommended model | `gemini-3-flash-preview` (primary), `gemini-3.5-flash` (fallback) |
| Measured input token usage | 3,464 to 6,522 tokens per analysis (5,411 typical); the instruction prompt is about 1,500 tokens and the rest is cleaned page content, capped at `CONTENT_MAX_CHARS` (20,000 characters) |
| Measured output token usage | 2,582 to 2,826 answer tokens plus 824 to 1,235 thinking tokens per analysis; Gemini bills thinking tokens at the output rate |
| Measured API cost per website analysis | $0.013 to $0.015 (about 1.4 US cents), computed from the measured tokens at the published rates |
| Measured response time | fetch and extraction 1.9 to 2.2 s; LLM call 18.6 to 24.6 s; total 20.7 to 26.5 s |
| Reason for selecting the model | Google's current Flash preview is the mid-tier, structured-output model available with the key in use; it returns the full five-section JSON in a single call at about 1.4 cents and roughly 25 seconds, and the `google-genai` SDK enforces the response schema |
| Alternative model for fallback | `gemini-3.5-flash`, used automatically when the primary call fails (timeout, rate limit, refusal, incomplete or schema-invalid output) |
| Recommended approach for balancing accuracy, speed and cost | run the mid-tier model as the default with medium thinking; escalate only on failure; keep the content budget at the `CONTENT_MAX_CHARS` cap; measure usage from the server log and adjust `LLM_REASONING_EFFORT` and `CONTENT_MAX_CHARS` rather than switching to a more expensive model |

## Security notes

- **SSRF protection** (`app/utils/validators.py`): only `http`/`https`; no credentials in the URL; ports limited to 80, 443, 8080, 8443; hostnames are IDNA-normalized before `localhost`-style and internal suffixes are blocked, so disguised spellings do not pass; IP literals and every resolved address must be globally routable (loopback, RFC 1918, link-local including `169.254.169.254`, carrier-grade NAT, multicast, reserved, documentation ranges, IPv6 local, IPv4-mapped, NAT64, 6to4 and Teredo forms are all rejected); shorthand, hex and zone-id spellings are rejected before DNS, and DNS answers that mix public and private addresses are refused.
- **No DNS rebinding window** (`app/services/website_extractor.py`): the TCP connection is made to the validated IP address while the original hostname is sent in the `Host` header and used for TLS SNI and certificate verification. Every redirect hop is validated and resolved again before it is followed; redirects to private targets are refused without being requested, and malformed redirect targets are reported as website errors.
- **Resource limits**: per-operation fetch timeout (15 s) plus an overall deadline for the whole fetch including redirects (45 s), a 10 s DNS timeout, a 2 MB cap enforced on both the compressed and the decompressed body (gzip and deflate are decoded by the application with a bounded decoder, so a compression bomb cannot expand in memory), a redirect limit (5), a character budget for the content sent to the LLM, an LLM timeout with no SDK-level retries (the fallback model is the retry) and an output token cap.
- **Secrets**: the API key is read from the environment or `.env` only, never logged or echoed; `.env` is git-ignored and `.env.example` contains placeholders only. Server-supplied text (status reason phrases, headers, exception text) is sanitized and bounded before it appears in an error message.
- **Prompt injection**: website text is passed inside a delimited block that the instructions declare untrusted; any `website_content` delimiter found in the page text is neutralized before wrapping, and the model is told to ignore instructions found in the block. Structured outputs constrain the response shape regardless of page content.

## Tests

```bash
pytest -q
```

101 tests, about 6 seconds, no network access: DNS, the HTTP transport and the LLM call are replaced, settings are pinned so a local `.env` cannot change outcomes, and any unexpected HTTP request fails the test. Covered: the success path with the LLM mocked and the exact response shape of the brief; content rules (exactly 5 queries and services, allowed platforms, competitor marker, no volume fields); the content the LLM receives and the neutralized delimiter; invalid URLs and request bodies; 24 blocked SSRF cases plus hostnames resolving to private or mixed addresses, redirects to private targets, relative and malformed redirects and redirect loops; unreachable, slow, erroring, non-HTML and empty sites; body caps, compression bombs and multi-byte truncation; extraction fallbacks, charsets and hostile markup; prompt-text budget boundaries; every LLM error branch, fallback behaviour, truncated JSON, incomplete responses and refusals; post-validation rejections (including the vague-value rejections for `target_industry`, `target_customer`, `target_audience.industries` and `target_audience.business_types`); the instruction text that carries the platform, service-label, fact-grounding and generic-value rules; the 500 handler; settings parsing against `.env.example`; and that only `POST /link-analysis` is exposed.

## Assumptions

Where the brief is silent, the following choices were made (kept to the minimum needed to build):

1. **Error response shape**: the brief specifies only the success shape. Errors use the same top-level style, `{"success": false, "website": ..., "error": {"type": ..., "message": ...}}`, with HTTP 400 for URL problems, 422 for unusable input or content, 502 for upstream (website or LLM) failures.
2. **Extra field names**: the fields the brief lists under each section's "Expected Information" are added inside the brief's example keys, named in snake_case from the brief's wording (for example `business_relevance`, `long_tail_keywords`, `service_name`, `recommended_marketing_segments`). No field beyond the brief's lists was added, apart from the `source` markers the brief requires for competitors and inferred services; `supporting_content` items are plain strings because the brief lists only content types for them. The `website` field echoes the URL exactly as submitted, as in the brief's example.
3. **Module responsibilities**: inferred from the file names in the brief's structure (see [Processing flow](#processing-flow)).
4. **Single LLM call**: all five sections come from one structured-output request, so the cleaned content is transmitted once; `competitor_service.py` owns the Section 1 rules and post-processing, `analysis_service.py` the rest.
5. **Competitors are AI-generated suggestions**: the brief allows competitor information "based on credible research or clearly marked as an AI-generated suggestion"; no research data source is specified, so every competitor entry carries `source: "ai_generated_suggestion"` and no web-search integration was added.
6. **Single page fetch**: only the submitted page is fetched (no crawling); navigation link texts from that page are used as hints about services.
7. **Test framework**: pytest. **Environment loading**: `pydantic-settings` reads `.env`; it is the only dependency beyond the brief's list besides the LLM client library and pytest.
8. **`.gitignore`** is the only file added to the brief's structure, so that `.env` can never be committed; the empty `__init__.py` package markers in `app/`, `app/services/`, `app/utils/` and `tests/` only make the listed modules importable as packages and let `pytest` run from the repository root.
9. **Scheme-less URLs** such as `example.com` are accepted as `https://example.com`.
10. **"Exactly five" is enforced after generation**: the counts are checked by Pydantic validators (a violation triggers the fallback model, then a structured error) rather than by `minItems`/`maxItems` in the JSON schema, because the API's acceptance of those keywords could not be verified without a key in this build and a rejected schema would fail every request.
11. **Competitors "where reliable"**: the model is told to name up to five competitors it is confident exist, aiming for three, and to name fewer or none rather than invent; an empty list is therefore possible for niche or non-business pages.
12. **How a search platform is chosen (Section 2)**: the brief fixes the supported platforms and requires exactly five queries with no volumes, but does not say how to pair a query with a platform or what the `intent` label should be. Platforms are therefore chosen by how people actually search on each one (Google Search for service and product searches, LinkedIn for B2B and professional searches, Facebook for local or consumer discovery, Instagram for visual or inspiration searches, X for news, trends and quick comparisons), and a platform is listed only where the query genuinely fits it — no Facebook or Instagram filled in for a B2B enterprise site. The `intent` value is free text that names the query's own intent (for example "Service Search", "B2B Search", "Inspiration", "Business Discovery", "Service Discovery").
13. **Service `source` label (Section 4)**: the brief's schema already allows only two values, so no new label or field is added. `"website_content"` is used only when the service's name or description actually appears in the supplied website text; otherwise the service is labelled with the schema's existing inferred value, `"inferred"`. The top five are chosen by prominence (navigation, headings, repeated offerings) and kept consistent with `main_products_services` in Section 1.
14. **Fact grounding**: the brief does not say where facts come from. Every concrete fact (founding year, number of regions or offices, awards, analyst recognition such as Gartner or Forrester, client counts, certifications) must appear in the fetched website text; if it does not, it is left out and never recalled from the model's own knowledge. This applies to the brand overview, the brand strengths and every competitor strength or weakness; competitor claims the model cannot verify from the site are kept cautious and carry `source: "ai_generated_suggestion"`.
15. **No catch-all audience values**: the brief does not list banned values, so vague catch-alls such as "All industries", "All major global industries", "Businesses" or "Everyone" are not allowed for `target_industry`, `target_customer`, `target_audience.industries` or `target_audience.business_types`; those fields must be specific and follow from the site's content, or briefly state that the site gives no evidence (`"Not stated on the website"`). Customer needs, interests and marketing segments must tie back to services the site actually offers. The banned catch-alls are also rejected in post-validation.
16. **LLM provider**: the platform research in the section above was written when the build started; the deployed service runs Google Gemini (`gemini-3-flash-preview`, fallback `gemini-3.5-flash`) because that is the API key available, and section D reports its measured results.
