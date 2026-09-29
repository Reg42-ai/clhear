# From your official texts to a reference blueprint

This guide takes you from a set of official texts to a reference blueprint, using only the `clhear` command. No server is needed: every command below works directly on the database (`DATABASE_URL`, default `sqlite:///./clhear.db`) and the scope files (`CLHEAR_SCOPES_DIR`, default `./scopes`). Two people, or one person with two working directories, can run it side by side on different texts.

Each layer derives its records only from the texts you add. When a layer has nothing to derive them from, CLHEAR does not fill the gap. It says which official sources to add, and which source kind to register them as.

## 1. Install and connect a model

```bash
pip install "clhear @ git+https://github.com/Reg42-ai/clhear.git"
clhear init
export CLHEAR_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-...
clhear doctor --check-model     # one small real call; exits non-zero on a bad key or model
```

## 2. Register your sources (L1)

Add each text by URL, by file, or from a text file you saved.

```bash
clhear sources add rule --url https://example.gov/rule.html --kind regulation \
  --jurisdiction US --publisher "Example Authority"
clhear sources add rule-guidance --path guidance.pdf --kind guidance --jurisdiction US --publisher "Example Authority"
clhear sources add actions --text-file enforcement-actions.txt --kind enforcement --jurisdiction US \
  --publisher "Example Authority"
clhear sources list
```

`--path` is relative to `CLHEAR_LOCAL_SOURCES_DIR` (default `./sources`). `--url` accepts public https pages and PDFs.

The kind decides which layer reads the text:

| Kind | Use it for | Layers that read it |
| --- | --- | --- |
| `law` | Acts and statutes, including their definitions sections | L2 obligations, L4 roles and licences |
| `regulation` | Implementing rules, regulations, licensing and registration rules | L2 obligations, L4 roles and licences |
| `standard` | Recognised standards and codes of practice the texts refer to | L3 components and their characteristics |
| `guidance` | Regulator guidance, FAQs and Q&As, bulletins, inspection findings, court decisions, official journals | L3 components, L8 practices |
| `form` | Official forms and templates | L3 components (documents to keep) |
| `agreement` | Model contracts and agreements the texts require | L3 components |
| `enforcement` | Enforcement actions, consent orders, settlements, penalty notices, warning letters, resolution agreements | L7 enforcement records and risk |

`--jurisdiction` is the jurisdiction the text applies in. It becomes the first applicability condition of every obligation from that text. `--publisher` is who publishes the text (the lawmaker, regulator, court or standards body); the advice in step 7 asks for further sources from the same publisher.

Check what was read before you build anything:

```bash
clhear sources test rule
# 42 clauses read (183211 bytes)
#   sec-1        Section 1. In this part, "covered person" means …
```

Those clause references are what every record will quote. A scanned PDF has no text layer; run OCR first.

## 3. Name the scope

A scope is the set of sources one reference blueprint is built from.

```bash
clhear scope create my-rules rule rule-guidance actions
```

## 4. Run once with an empty profile

The questions an organization profile answers come from the texts, so the first run is the way to find them.

```bash
clhear profile set first
clhear run --scope my-rules --profile-id first      # prints the release id and the lineage check
```

## 5. Read the questions and the candidate organization profiles (L4)

```bash
clhear profile questions --scope my-rules
```

This lists, each with the words it was read from:

- the **jurisdictions** your sources declare;
- the **roles**: the addressees the obligations name ("covered person", "operator");
- the **conditions**: the obligations' own "where / if / unless" clauses about the addressee;
- the **licence types** the texts establish;
- the **candidate organization profiles**: one per role and one per licence type, with the conditions still to answer. A candidate is only ever built from roles and licences quoted from your texts.

Add `--json` for the same as data, including each question's quotes.

## 6. Answer them and run again

```bash
clhear profile set my-company --name "My company" --jurisdiction US \
  --role "covered person" --not-role "service provider" \
  --condition "maintains electronic records=true" --licence "registration certificate"
clhear run --scope my-rules --profile-id my-company
clhear blueprint show --release <release_id> --profile my-company
```

`clhear profile set ID --file profile.json` takes the same answers as JSON (`{"attributes": {"jurisdictions": [...], "roles": [...], "conditions": {...}, "licences": [...]}}`).

`blueprint show` prints the components selected, the obligations that apply, those that do not (and why), the open questions and the sources to add. `clhear export --release R --profile P` writes the full reference blueprint as JSON, with the evidence chain of each element.

## 7. Add the sources the layers ask for

```bash
clhear sources advise --scope my-rules
```

For every layer that could not derive its records, the advice says what is missing, which kinds of official source would let the layer derive them, and the kind to register each as:

| Layer | When | Add |
| --- | --- | --- |
| L1 Sources | A source produced no text | The official publication itself, as HTML or a PDF with a text layer |
| L2 Obligations | The texts were read, but none states an obligation | The binding act or regulation in full, not a summary or an index |
| L3 Components | An obligation names no component to put in place, or a characteristic (cadence, owner, retention) is not stated | Implementing guidance, recognised standards, codes of practice |
| L4 Applicability | No licensing regime is in scope, or a role is used but not defined | Licensing, registration or scope-of-practice rules; the definitions section; coverage guidance |
| L4 Applicability | Obligations are undetermined | Not a missing source: answer the open questions |
| L5 Compliance activities | The text does not say who performs the obligation | Rules or guidance that designate a responsible officer or function; governance provisions |
| L7 Risk scoring | No enforcement source is in scope, so no obligation has enforcement events | Enforcement actions, consent orders, settlements, penalty notices, warning letters, resolution agreements |
| L8 Practices | No guidance source is in scope, so no component has a guidance-derived practice | FAQs and official Q&As, guidance and bulletins, inspection findings, court and tribunal decisions, official journals |

News coverage can point you to one of these official sources, but it is never used as evidence. Register the official source it refers to.

Add what the advice suggests with `clhear sources add`. Then put it in the scope with `clhear scope create my-rules ...`, listing every source again. Run again. Each run re-reads the scope and re-derives every layer from the texts now in it. The same advice is in each reference blueprint (`source_advice`) and release, and at `GET /v1/scopes/{name}/advice`.

## Two organisations, two sets of texts

Nothing in CLHEAR is specific to a sector. Two users with different texts get different questions, candidate organization profiles, components and advice, because each comes only from the texts in their own scope:

```bash
DATABASE_URL=sqlite:///health.db  CLHEAR_SCOPES_DIR=health-scopes  clhear sources add ...   # health privacy rules, their guidance, enforcement
DATABASE_URL=sqlite:///finance.db CLHEAR_SCOPES_DIR=finance-scopes clhear sources add ...   # financial safeguards rules, their guidance, enforcement
```

Before you rely on a reference blueprint, check the `lineage` that `clhear run` prints: `unanchored` must be `0` and `rows` must equal `anchored`. That means every quote on every evidence chain was found again, byte for byte, in its clause. `clhear release --release R` writes the whole release, with the records that did not hold.
