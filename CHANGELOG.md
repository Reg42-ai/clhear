# Changelog

## 0.2.0

A run over your own texts now produces a real, traceable blueprint. In 0.1.0 the live path could not: pasted text never imported, model calls failed quietly, and runs over different scopes mixed.

### Reading texts (L1)
- `local_text` works: pasted text, or a text, HTML or PDF file under `CLHEAR_LOCAL_SOURCES_DIR` (default `./sources`, `/sources` in the image).
- New `url` adapter: public https pages and PDFs, fetched live, with every redirect checked and private addresses refused.
- Clauses follow the document's own structure (parts, articles, sections, numbered paragraphs, list items) with readable references such as `art-32/1`. Every import is verified line by line against the stored text.
- `POST /v1/sources/{key}/test-fetch` reads the source with its real adapter and previews its clauses.
- `GET /v1/adapters` lists what can be registered, and the locator each adapter needs.
- A scope build succeeds per source. `source.failed` fires only for real failures, releases list `failed_sources`, and a scope with no readable text fails with the reason.

### Model calls
- The Anthropic provider uses the official SDK. It defaults to `claude-opus-5` with adaptive thinking (`CLHEAR_LLM_EFFORT`, default `medium`), sends only the parameters each model accepts, and enables server-side refusal fallbacks (`CLHEAR_LLM_FALLBACKS`).
- 0.1.0 sent the model id `"configured"` when `CLHEAR_LLM_MODEL` was unset.
- Retries honour `retry-after` and never repeat a request that cannot succeed.
- Each layer reports its model calls; a layer where every call failed fails the run.
- `clhear doctor --check-model` makes one small real call.

### Duties (L2) and measures (L3)
- Heading filters read real headings only; words such as "extent", "scope" or "title" inside a sentence no longer hide a duty.
- List items are read with their lead-in and become one duty each.
- Duties of supervisory and competent authorities are excluded.
- "shall include" is a duty.
- New duty types: data protection, security, risk management, training, safety.
- Measures are generated for every duty, grouped by type, and near-duplicates are reused.

### Applicability (L4) and blueprints (L6)
- One rule: a duty applies when all its conditions match, and a duty with none applies everywhere. Conditions come from the source's jurisdiction, the addressee, and the duty's own where/if clause.
- `GET /v1/profile-schema` explains each profile fact. `PUT /v1/profiles` returns validation errors and warnings.
- The financial-services ontology is seeded only with `CLHEAR_CURATED_FINANCE=1`.
- Blueprints are per scope. Every duty is `covered`, a `gap`, or listed in `not_applicable` with the condition it failed. Each coverage row carries its duty sentence.
- Two profiles with the same facts both get a blueprint. An invalid profile stops the run before any model call.
- The offline sample is marked `"sample": true`.

### Removed
- Internal-testing code: a company-specific source registry, an internal starter corpus, a private rulebook crawl and review with its operator exceptions, a fixed-scope deployment verification, and a demo corpus. Migration `0041` drops the retired tables.

### Upgrading from 0.1.0
- Schema revision `0041` is applied on startup.
- A 0.1.0 database was seeded with the financial ontology. Start a fresh database for general texts, or set `CLHEAR_CURATED_FINANCE=1` to keep using it.
