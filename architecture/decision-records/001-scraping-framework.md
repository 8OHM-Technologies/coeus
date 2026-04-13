### ADR 001: Selection of Web Scraping Framework for Regulatory Portals
**Date**: April 2026
**Status**: Accepted

**Context**:
The Lithos pipeline requires robust, automated extraction of NI 43-101 technical reports from regulatory portals (e.g., SEDAR+, ASX) and individual mining company websites. Historically, standard HTTP request libraries (like requests or standard Scrapy) were sufficient for this task. However, modern portals heavily utilize client-side JavaScript rendering, dynamic pagination, and robust anti-bot measures (such as Cloudflare). We need a framework capable of navigating DOM-heavy sites and interacting with complex UI elements to trigger PDF downloads.

**Decision**:
We will use Playwright (Python) as the primary ingestion engine, wrapped in a modular Python class structure, rather than relying solely on HTTP requests or Scrapy.

**Consequences**:

**Pros**:

Full DOM Rendering: Playwright handles JavaScript-heavy sites and single-page applications (SPAs) natively.

Resilience: It allows for explicit waits on specific network events or DOM elements, reducing flaky scrapes.

Evasion: By driving a real Chromium/WebKit instance, it naturally bypasses basic anti-bot challenges that block requests or urllib.

**Cons**:

Resource Intensive: Running headless browsers requires significantly more memory and CPU per worker compared to lightweight HTTP requests.

Speed: Browser-based scraping is inherently slower. Mitigation: We will scale horizontally via Dockerized workers to maintain overall pipeline throughput.
