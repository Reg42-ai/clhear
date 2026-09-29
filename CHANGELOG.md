# Changelog

## 0.2.0

A run over your own texts now produces a real, traceable reference blueprint, in any sector. In 0.1.0 the live path could not: pasted text never imported, model calls failed quietly, and runs over different scopes mixed. Every record in the blueprint is now quoted from the texts in scope, and what they cannot support is reported instead of guessed.

### Guidance for your own texts
- **Source advice.** Each layer that cannot derive its records from the texts in scope says which official sources to add to L1, why, and the source kind to register each as: the binding text for L2; implementing guidance and standards for L3; licensing rules and definitions for L4; officer designations for L5; enforcement actions, penalty notices and resolution agreements for L7; FAQs, guidance, inspection findings, court decisions and official journals for L8. Blueprints and releases carry `source_advice`; new `GET /v1/scopes/{name}/advice`. When sources declare a publisher (`issuer`), the advice asks for the further sources from that publisher.
- **Cited sources named.** When L1 stores a text, it records every mention of another text or provision ("section 2 of the Harbour Lighting Act 2019", "under Part 7", "Regulation (AB) 2030/17") with its quote and offsets, in the generic grammar of legal drafting. Each build resolves them against its scope, by the publisher's reference or the name of each source and by clause reference. A cited text the scope does not hold is an `unresolved_reference` gap, and the advice names it as the clauses word it, with the clauses that cite it and the kind to register it as.
- **Source inventory.** Blueprints and `GET /v1/scopes/{name}/advice` carry `source_inventory`: every source in scope and every text their clauses cite, each `derived`, `pending` (registered but not built, or not in this scope) or `unresolved` (cited, not registered). `clhear sources advise` and `clhear blueprint show` print it.
- Sources take an optional `reference`, the publisher's own reference for the text (`clhear sources add --reference`). Registering a source again with another kind or reference takes effect on the next run.
- A scope whose texts state no obligation records a `no_duties` gap, and an unreadable source a `no_text` gap.
- **Candidate organisation profiles.** `GET /v1/profile-schema?scope=` lists `candidates`: one per quoted role and one per quoted licence type, with the conditions still to answer.
- **The whole first run from the command line**, with no server: `clhear sources add|list|test|advise`, `clhear scope create`, `clhear profile questions|set`, `clhear blueprint show`. `clhear run` prints the lineage check. New guide: `docs/quickstart-your-sources.md`.

### Grounded in the texts
- Every derived record carries its evidence chain: quotes of the clause text with offsets (obligations and their subject, action and condition; components; characteristics; applicability conditions; compliance activities; licence types).
- Before composing, each run checks every quote again against the stored clause (the anchoring check) and withholds any record that fails. The release reports it as `lineage` (`rows`, `anchored`, `unanchored`).
- New `evidence_gaps` in the blueprint: what the texts could not support, with a recommendation of which source to add (`no_measure`, `measure_name_rejected`, `characteristic_unspecified`, `role_undefined`, `no_licence_types`, `operator_not_stated`, `no_enforcement_sources`, `no_reference_sources`).
- The engine carries no sector vocabulary. The reviewed financial-services catalog (components, compliance activities, licence and product ontology, attribute schema, sample profiles, starter concepts, register checks) and `CLHEAR_CURATED_FINANCE` are removed.

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

### Obligations (L2) and components (L3)
- Heading filters read real headings only; words such as "extent", "scope" or "title" inside a sentence no longer hide an obligation.
- List items are read with their lead-in and become one obligation each.
- Obligations of public bodies are excluded by grammar (an authority, commission, department, court, agency or minister as the subject), whatever the sector.
- "shall include" states an obligation.
- An obligation's type is its own verb as written ("keep", "notify"), not a fixed taxonomy; an obligation that does not state who it binds is never given a default addressee.
- Components are named in the obligation's words. A name the model proposes is kept only when every word of it is in the cited clauses; an obligation that names nothing concrete becomes a gap with a recommendation. Component subclasses are read from the obligation's words, or `Unspecified`.
- Characteristics are recorded only when a clause states them, with the quote.

### Profile attributes and applicability (L4), compliance activities (L5) and reference blueprints (L6)
- The organisation profile answers the questions the texts' applicability conditions raise: `jurisdictions`, `roles` (the addressees the obligations name), `conditions` (the obligations' own "where / if / unless" clauses about the addressee) and `licences`. `GET /v1/profile-schema?scope=<name>` lists them with quotes.
- Three-valued rule: an obligation applies when every question is answered and matches, is not applicable when an answer fails, and is undetermined while a question is unanswered. The blueprint lists `undetermined` obligations and `open_questions`.
- Licence types are read only from the scope's licensing clauses and registers, with a quote check.
- **Registers.** New source kind `register`, for official registers of licensed or authorised entities and lists of licence categories. L4 reads licence types from their entries without a model: labelled fields ("Licence type: …") and tables with a licensing column, each quoted with offsets. They are the permitted values of the profile's `licences` (`permitted_values` in the profile schema), and the licence records the register it came from. The `no_licence_types` advice now suggests the register.
- Sources of kind `enforcement` or `register` are never read for obligations. Obligations read from a source before it was registered as one of these go stale on the next run.
- Compliance activities are the obligation's quoted action, operated by the quoted addressee, or an `operator_not_stated` gap.
- L7 (risk scoring) and L8 (practices) build only from `enforcement` and `guidance` sources in scope; otherwise they are recorded as not built, with a gap.
- Blueprints are per scope. Every obligation is `covered`, a `gap`, `not_applicable` with the condition it failed, or `undetermined` with its question. Each coverage row carries its quote.
- Two profiles with the same answers both get a blueprint. A malformed profile stops the run before any model call.
- The offline sample is marked `"sample": true`.

### Removed
- Internal-testing code: a company-specific source registry, an internal starter corpus, a private rulebook crawl and review with its operator exceptions, a fixed-scope deployment verification, and a demo corpus. Migration `0041` drops the retired tables.

### Upgrading from 0.1.0
- Schema revisions `0041` to `0044` are applied on startup. `0042` adds the evidence columns and `evidence_gaps`, removes the rows the retired catalog seeded, and closes applicability conditions written in the old attribute vocabulary; the next run derives them again from your texts. `0043` adds `clause_references`, records the references of every text already stored, and adds the source `reference`. `0044` admits the source kind `register` and marks enforcement and register sources as not read for obligations.
- **Profiles change shape (breaking).** `authorisations`, `products`, `customer_base`, `channels`, `data_footprint`, `crypto_services` and `financial_entity_dora` are no longer accepted. Run each scope once, read `GET /v1/profile-schema?scope=<name>`, and answer its `roles` and `conditions`.
- `GET /v1/profile-schema` takes `?scope=` and returns `candidates`. Blueprints gain `undetermined`, `open_questions`, `evidence_gaps`, `source_advice`, `source_inventory` and `profile_warnings`; releases gain `lineage` and `source_advice`.
