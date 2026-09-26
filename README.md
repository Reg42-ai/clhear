# CLHEAR

A statute is long. A compliance program has to answer a shorter question: for an organisation of this shape, what must be in place, and which clause says so?

CLHEAR is an open engine for that question. You choose the texts. You describe the organisation in a handful of facts. CLHEAR returns a **blueprint**: the smallest set of measures that covers the duties in those texts. Each measure carries a plain explanation and a pointer back to the clause it comes from.

You can read the blueprint, diff it when the texts or the organisation change, and hand the JSON to another system.

## Why it is worth running

The link to the clause survives. Ask why a measure is in the program, and the blueprint names the duty, the text, and the passage.

The set stays small. A measure remains because dropping it would leave a duty uncovered. When two blueprints exist, you can ask what appeared, what dropped, and which duties changed state.

You choose the corpus. CLHEAR starts with no texts loaded, so two teams can run the same engine on different documents. One install, one database, on a laptop or in a private network.

What the database holds: the texts you supplied, the duties derived from them, and the organisation description used to select those duties. Firm identity, the controls already in operation, owners, and evidence files remain in your own systems.

## What you bring

Three objects. The names below are the ones the command line and the API use.

**Source.** One text CLHEAR may read. A public page, a file you place on disk, or a feed from a publisher. You give it a short key, such as `example-source`.

**Scope.** The sources that belong in one run, under one name. `example-scope` might list only `example-source`. You write that list. Startup loads whatever scope files are already in `scopes/`.

**Profile.** A description of the organisation the blueprint is for. The facts decide which duties apply:

| Field | What it asks |
| --- | --- |
| `jurisdictions` | Where the organisation operates or serves people |
| `authorisations` | Permissions and licences it holds |
| `products` | What it offers |
| `customer_base` | Whom it serves |
| `channels` | How it reaches them |
| `data_footprint` | The scale of personal data it handles |
| `crypto_services` | Whether it provides crypto-asset services |
| `financial_entity_dora` | Whether operational-resilience rules for financial entities apply |

Any other field is rejected. These facts describe the kind of organisation. They are a separate record from the controls that organisation already runs.

## What a run does

1. Read the texts in the scope and keep a copy that can be cited.
2. Find the duties in those texts, each pinned to a passage.
3. Keep the duties that match the profile.
4. Choose the smallest set of measures that still covers those duties, and write the reason each one is there.

## What you get back

A finished run stores a release. The blueprint for one profile is JSON:

- `items` — the measures in the program. Each has a `name`, a `basis` (`required` when a duty demands that measure), and an `explanation` in sentences.
- `coverage` — every duty that applies. `source_key` is which text. `clause_ref` is where in that text. `state` is `covered` when at least one measure satisfies the duty.
- `minimality` — `checked` and `minimal`. The proof says which duties would open up if a measure were removed.

A placeholder text ("keep a record of each decision") comes back shaped like this:

```json
{
  "items": [
    {
      "name": "Decision record",
      "basis": "required",
      "explanation": "Decision record (Process BLK-DECLARED-RECORD) is required by OBL:example-source#clause-1. It satisfies 1 applicable obligation(s): OBL:example-source#clause-1."
    }
  ],
  "coverage": [
    {
      "source_key": "example-source",
      "clause_ref": "clause-1",
      "title": "Keep a record of the decision",
      "state": "covered"
    }
  ],
  "minimality": { "checked": true, "minimal": true }
}
```

The ids inside `explanation` (`OBL:…`, `BLK-…`) are stable handles for a duty and a measure. `coverage` is the human pointer: which text, which passage. Fetch the object with `GET /v1/releases/{release_id}/blueprints/{profile_id}`.

## See one offline

Python 3.12. The quickstart stays on this machine. It reads a placeholder sentence and uses a stand-in model, so you can see the shape of a blueprint before you connect a real one.

```bash
pip install "clhear @ git+https://github.com/Reg42-ai/clhear.git@v0.1.0"
export CLHEAR_LLM_PROVIDER=fake
clhear init
clhear doctor
clhear quickstart
```

`clhear init` creates an empty `scopes/` directory. `clhear doctor` checks the database and the model. With `fake` you will see `"live_run": "blocked"`: the sample can finish, and a run over real texts waits until you configure a model. `clhear quickstart` writes `example-source`, `example-scope`, and `example-profile`, runs them, and prints a `release_id` and a `blueprint_id`.

Print that blueprint:

```bash
clhear export --release rel_... --profile example-profile
```

## Use your own texts

Reading your own texts calls a model. `clhear doctor` prints `"live_run": "ready"` only after one of the three providers below is configured. With the provider unset, doctor exits non-zero. With `fake`, doctor exits zero and `clhear run` still writes the offline sample blueprint. `clhear quickstart` always uses `fake` for its own run, including when a live provider is already configured.

| `CLHEAR_LLM_PROVIDER` | Also set |
| --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai_compatible` | `OPENAI_BASE_URL` and `OPENAI_API_KEY` |
| `bedrock` | `BEDROCK_MODEL_ID` or `CLHEAR_LLM_MODEL`, and your AWS credentials |

Optional model name: `CLHEAR_LLM_MODEL`. Database, Postgres, and the container are in [docs/install.md](docs/install.md).

Two processes share the database. The API accepts work. The worker does the reading.

```bash
clhear serve          # http://127.0.0.1:8000
clhear worker
```

On `127.0.0.1` the API accepts calls with no token. For any other bind, set `CLHEAR_SERVICE_TOKENS` (comma-separated, so you can rotate) or `CLHEAR_SERVICE_TOKEN_FILE`, and send `Authorization: Bearer <token>`. `clhear serve` refuses to start on a public bind when no token is configured.

Register a text, name the scope, describe the organisation, start a run. `POST /v1/runs` returns immediately with a `run_id` and `"status": "queued"`. The worker picks it up.

```bash
curl -s -X POST localhost:8000/v1/sources \
  -H 'content-type: application/json' \
  -d '{
    "key": "example-source",
    "adapter": "local_text",
    "name": "Example source",
    "kind": "guidance",
    "licence": "open",
    "locator": {"text": "An organisation must keep a record of each decision and the reason for it."}
  }'

curl -s -X POST localhost:8000/v1/scopes \
  -H 'content-type: application/json' \
  -d '{"name": "example-scope", "label": "Example scope", "sources": ["example-source"]}'

curl -s -X PUT localhost:8000/v1/profiles/example-profile \
  -H 'content-type: application/json' \
  -d '{"name": "Example organisation", "attributes": {"jurisdictions": [], "channels": []}}'

curl -s -X POST localhost:8000/v1/runs \
  -H 'content-type: application/json' \
  -d '{"scope": "example-scope", "profiles": ["example-profile"]}'
```

Poll `GET /v1/runs/{run_id}` until `"status": "succeeded"`. The body includes `release_id`. Then:

```bash
curl -s localhost:8000/v1/releases/$RELEASE_ID/blueprints/example-profile
```

The same execution from the command line, once the source, scope, and profile exist:

```bash
clhear run --scope example-scope --profile-id example-profile
```

`adapter` selects how the text is read. `local_text` uses the `text` or `path` in `locator`. For a URL, call `POST /v1/sources/{key}/test-fetch` before a run: it returns a version and a node count, and it writes nothing.

To compare two blueprints:

```bash
curl -s "localhost:8000/v1/blueprints/$BLUEPRINT_ID/diff?against=$OTHER_ID"
```

The contract for every path is [openapi/clhear-v1.yaml](openapi/clhear-v1.yaml). `GET /v1/version` reports the engine version, the API version, the schema revision, and the image digest.

Finished runs can notify your own URL. Register it with `POST /v1/webhooks` and a secret. Each delivery sets `X-CLHEAR-Event` and `X-CLHEAR-Signature: sha256=<HMAC of the body>`. Events include `run.started`, `run.finished`, `run.failed`, `source.failed`, `release.published`, and `blueprint.changed`. A failed delivery leaves the run itself successful.

## Pin a version

Install a tag. `main` moves.

```bash
pip install "clhear @ git+https://github.com/Reg42-ai/clhear.git@v0.1.0"
clhear version   # v0.1.0
```

Container images are published as `ghcr.io/reg42-ai/clhear` for `linux/amd64` and `linux/arm64`. Pin the digest in the release manifest. `clhear version` prints the tag.

## License

[AGPL-3.0-only](LICENSE). Running a modified CLHEAR as a network service is distribution under that license: the people who use the service are entitled to the corresponding source.

Contributions are welcome under the [contributor terms](CONTRIBUTING.md).
