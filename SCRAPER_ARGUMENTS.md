### 1. Global / Shared Configurations (`BaseScraper`)
These parameters affect database target resolution and network request settings, and are supported at the base class level:

| CLI Argument | Description | Available in `extraction_params`? | Notes / Priority Order |
| :--- | :--- | :--- | :--- |
| `--pipeline` / `--pipeline_name` | Name/ID of the pipeline configuration. | **No** | Required. |
| `--skip_stages` | Comma-separated list of stages to skip (e.g. `1` or `1,2`). Stage 1=Indexing, Stage 2=Detailing, Stage 3=Extraction. | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["skip_stages"]`. |
| `--use_proxy` | Enable proxy for network requests (`true`/`false`). | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["use_proxy"]`, pipeline config level `use_proxy`, or `USE_PROXY` env var. |
| `--shared_record_type` | Database record type / storage namespace override. | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["shared_record_type"]`, and defaults to the pipeline name. |

---

### 2. SAFLII Scraper (`new_saflii_scraper.py`)
These options control the behavior of the SeleniumBase browser execution, dataset filtering, detailing concurrency, and throttling:

| CLI Argument | Description | Available in `extraction_params`? | Notes / Priority Order |
| :--- | :--- | :--- | :--- |
| `--datasets` | Comma-separated list of SAFLII dataset codes to process (e.g. `ZACC,ZALCJHB`). | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["datasets"]`. |
| `--year` | Target year to index (e.g. `2026`). | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["year"]` or `extraction_params["start_year"]`. |
| `--headless` | Run browser in headless mode (`true`/`false`). | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["headless"]` (defaults to `false`). |
| `--use_xvfb` | Run browser with Xvfb display (`true`/`false`). | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["use_xvfb"]` (defaults to `true`). |
| `--cooldown_seconds` | Throttling delay in seconds to wait between browser actions. | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["cooldown_seconds"]` (defaults to `1.5`). |
| `--max_datasets_per_session` | Maximum datasets to process before restarting the browser session. | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["max_datasets_per_session"]` (defaults to `20`). |
| `--warmup_session` | Warm up browser session by loading a test page first (`true`/`false`). | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["warmup_session"]` (defaults to `true`). |
| `--concurrency` | Number of concurrent worker threads to spawn for Stage 1B (Detailing). | **Yes** | **CLI takes precedence**, then falls back to `extraction_params["concurrency"]` (defaults to `4`). |