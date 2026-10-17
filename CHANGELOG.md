# Changelog

## Unreleased

A release's reference blueprints can be published as a snapshot that anyone can check and match against their own organisation profile, and every layer, L1 to L8, builds from the sources a scope holds.

### Snapshots and recipes
- **`clhear snapshot create`** writes a release's reference blueprints as plain files: each profile's blueprint (as the API serves it), the applicability conditions of every obligation in scope with each profile's verdict, the questions the texts raise, the sources with the version and SHA-256 of the text that was read, the models the run used, each layer's revision, and a manifest with the SHA-256 of every file and one `content_hash`. `--zip` writes a `.zip` file with fixed order and times. A licence (or `not-for-publication`) is required; a text that may not be republished stays out of a published snapshot.
- **`clhear snapshot verify`** checks the files against the manifest. **`clhear snapshot match`** says whether an organisation profile matches one of the snapshot's profiles: every obligation in scope gets the same verdict, by the rule composition uses. It reads the files alone and calls no model.
- **Recipes.** `clhear recipe apply` registers a scope's sources, the scope and its profiles from one file. Each snapshot carries the recipe of its release.
- Each live run stores what a snapshot needs (`snapshot_basis` in the release). A release made before this change is not snapshotted: run the scope again.
- The public blueprint (`GET /v1/releases/{release_id}/blueprints/{profile_id}`) carries `profile_attributes`, the organisation profile it was composed for.
- A snapshot carries Layers 7 and 8 as the run left them. `risk.json` holds each penalty a binding text states, quoted, with the obligations it refers to, and each obligation's risk score. `practices.json` holds each practice read from a text that does not bind, quoted, with its component. The manifest lists the warnings, and the `content_hash` covers the manifest's own fields as well as the files.
- **Snapshot API.** `POST /v1/releases/{release_id}/snapshot` returns a release's snapshot as a `.zip`; `POST /v1/snapshots/verify` and `POST /v1/snapshots/match` check one and match a profile against it. None stores anything. A `.zip` holding more files or bytes than a snapshot can is refused before it is read.

### Bedrock model ids
- A Bedrock model is one model under each of its ids: its base id and each cross-Region id (`us.`, `eu.`, `global.` and the other geography prefixes), with or without a version suffix. It has one price under each, and for Claude Opus 5 each counts against the premium monthly cap. Before, a cross-Region id, which the engine's Bedrock calls need for Claude Opus 5 in us-east-1, was priced at the default rate and not counted against that cap.
- The model you configure for a provider is its one rung, so it runs each task. Before, a configured premium model was held back for the tasks marked high, and the other tasks failed with "no eligible rung".

### Sources that do not bind
- A source says whether it binds (`binding`, `clhear sources add --not-binding`). A text registered with `"binding": false` gives no obligations, even where it uses the words of duty; L8 reads it for practices. Unset keeps the rule by kind. Migration 0046 adds the column; existing sources stay unset.

### Penalties stated in a list (L7)
- A penalty stated in the opening words of a list, which end with a colon, is read once from those words and linked to the clauses its points cite. Before, that opening clause was skipped because it repeats its points, so the penalty was not found. A clause cited to leave it out (after "other than", "except" or "save") is not linked.

## 0.2.1

Who a duty binds is read as a party, never a thing, a heading or an introductory phrase, so an organisation can answer every question a scope raises.

### Who a duty binds (L2, L4)
- **The subject without what precedes it.** A heading kept in the clause ("General Requirements Each member"), a footnote number, a discourse marker ("In particular,"), a report of another text ("Regulatory Notice 11-39 states that a firm") and a proviso ("; provided, however, that") are no longer read as the subject. A leading condition or time phrase with commas ("When sponsoring a seminar, forum, radio or television interview, ..., persons associated with members") becomes the duty's condition.
- **Party or thing.** A subject is an organisation ("member"), a person or function inside one ("Supervisory Analyst", "associated person"), anyone ("any person") or a thing the duty is about ("Communications", "the illustration"). Things raise no question; a thing's relative clause and a condition about a thing or a person time the duty instead of deciding whether it applies.
- **Questions an organisation can answer.** "Are you a 'member'?" for an organisation; "Does your organisation have a 'Supervisory Analyst'?" for a person or function; conditions as sentences in the clause's words ("Does your organisation intend to communicate ... through a text messaging app or chat service?"). Variants of one role ("appropriately qualified registered principal", "appropriately qualified principal") are one question. `open_questions` and `GET /v1/profile-schema` carry `party` and `ask`.
- A participle after an organisation narrows who is bound: "Firms registered under the Investment Advisers Act" asks "Is your organisation registered under the Investment Advisers Act?".
- "shall not apply with regard to" is read as scope, not a duty.

### Components (L3, L5)
- **Roles.** A person or function a duty is addressed to is a Role component the duty requires, besides its measure; processes it performs carry it as `performing_role`.
- **Documents.** A duty that says what something contains ("The supervisory procedures shall include ...", "shall set forth ...") names a Document (its subject), with what it must contain as `mandatory_sections`.
- Phrasal verbs are one verb ("set forth", "set out", "carry out"); "ensure that ..." is a Process; names no longer end on a preposition or an article.
- A thing is never a component's performing role or an activity's operator.

### Source advice
- Advice for several gaps of one kind names them ("The texts use 4 roles that no clause in scope defines: 'member', ...") instead of printing empty placeholders.

### On a scope of three US securities communications texts (offline run, profile `member: true`)
| | 0.2.0 | 0.2.1 |
|---|---|---|
| Obligations that apply | 33 | 61 |
| Undetermined | 38 | 8 |
| Role questions | 29 | 4 |
| Condition questions | 12 | 2 |
| `role_undefined` gaps | 58 | 4 |

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
- **Stated penalties (L7).** L7 reads the penalty clauses of the `law` and `regulation` sources in scope: each penalty's type (imprisonment, disqualification, revocation, suspension, fine, penalty) and the maximum stated, quoted with offsets, linked to the obligations whose provisions the clause refers to (a cited provision, a sibling subsection, "this Part", "this Act"). They give the new risk dimension `stated_penalty`, so L7 scores risk even with no enforcement source in scope. Enforcement events stay the stronger input: the risk method is now `risk-v3` (enforcement history 0.25, likelihood 0.20, financial impact 0.15, reputational, operational and attention 0.10 each, stated penalty 0.10). The `no_enforcement_sources` gap says how many penalties the risk rests on, and its advice also asks for the act's penalty provisions.
- **Practices from enforcement remediation (L8).** The remediation an `enforcement` source orders ("is ordered to …", "agreed to …", corrective action, a duty with a deadline) is a practice for the component it concerns, marked `remediation` and quoted with offsets. L8 builds from `guidance` or `enforcement` sources, reads only the most specific clauses, and no longer treats a clause that is the basis of an obligation as a practice. In a scope, L8 reads its guidance and enforcement sources (before, only sources named in a `reference` role). The `no_reference_sources` advice now also asks for enforcement actions that order remediation.
- L7 (risk scoring) is built from enforcement sources and stated penalties, and L8 (practices) from guidance and enforcement sources in scope; otherwise they are recorded as not built, with a gap.
- Blueprints are per scope. Every obligation is `covered`, a `gap`, `not_applicable` with the condition it failed, or `undetermined` with its question. Each coverage row carries its quote.
- Two profiles with the same answers both get a blueprint. A malformed profile stops the run before any model call.
- The offline sample is marked `"sample": true`.

### Removed
- Internal-testing code: a company-specific source registry, an internal starter corpus, a private rulebook crawl and review with its operator exceptions, a fixed-scope deployment verification, and a demo corpus. Migration `0041` drops the retired tables.

### Upgrading from 0.1.0
- Schema revisions `0041` to `0045` are applied on startup. `0042` adds the evidence columns and `evidence_gaps`, removes the rows the retired catalog seeded, and closes applicability conditions written in the old attribute vocabulary; the next run derives them again from your texts. `0043` adds `clause_references`, records the references of every text already stored, and adds the source `reference`. `0044` admits the source kind `register` and marks enforcement and register sources as not read for obligations. `0045` adds `stated_penalties` and `penalty_links`; risk scores move to method `risk-v3`, and the next run scores every obligation again.
- **Profiles change shape (breaking).** `authorisations`, `products`, `customer_base`, `channels`, `data_footprint`, `crypto_services` and `financial_entity_dora` are no longer accepted. Run each scope once, read `GET /v1/profile-schema?scope=<name>`, and answer its `roles` and `conditions`.
- `GET /v1/profile-schema` takes `?scope=` and returns `candidates`. Blueprints gain `undetermined`, `open_questions`, `evidence_gaps`, `source_advice`, `source_inventory` and `profile_warnings`; releases gain `lineage` and `source_advice`.
