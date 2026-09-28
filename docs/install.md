# Install

This page is how you get a database, a model, and a private process. The [README](../README.md) explains sources, scopes, profiles, and how to read a blueprint. [How it works](how-it-works.md) walks through the pipeline.

Python 3.12. Install a release tag (see [CHANGELOG.md](../CHANGELOG.md)), or `main` without `@…`:

```bash
pip install "clhear @ git+https://github.com/Reg42-ai/clhear.git@vX.Y.Z"
```

## Model

A run over your own texts calls a model. Pick one provider. `clhear doctor` exits non-zero until that configuration is complete. `"live_run": "blocked"` in the doctor output means the sample quickstart can still run, and a live run waits.

| `CLHEAR_LLM_PROVIDER` | Also set | Default model |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-5` |
| `openai_compatible` | `OPENAI_BASE_URL`, `OPENAI_API_KEY` | `gpt-4o` |
| `bedrock` | `BEDROCK_MODEL_ID` or `CLHEAR_LLM_MODEL`, and the usual AWS credentials | none |

`CLHEAR_LLM_MODEL` overrides the model. `clhear doctor --check-model` makes one small real call and exits non-zero if the key, model or network is wrong: run it before the first build.

`url` sources always fetch live. The publisher adapters (`eur_lex`, `uk_legislation`, `govinfo_us`) need `CLHEAR_HTTP_MODE=live`; the default, `replay`, only reads recorded test fixtures.

`CLHEAR_LLM_PROVIDER=fake` is the offline sample. `clhear quickstart` forces `fake` for its own run. While the provider is `fake`, `clhear run` and the worker also stay on that sample path. Set one of the three providers above before you expect a run to read a real corpus.

## SQLite

The default database is a file, `sqlite:///./clhear.db`.

```bash
export CLHEAR_LLM_PROVIDER=fake
clhear init
clhear doctor
clhear quickstart
```

`clhear init` creates an empty `scopes/` directory next to the process. You add scope files, or you create scopes through the API. The package ships with that directory empty.

`clhear migrate` applies the schema on its own. `init`, `doctor`, `quickstart`, `serve`, and `worker` apply it as well the first time they open the database.

## Postgres

Point `DATABASE_URL` at a database the process can reach, and install a driver in that environment (`psycopg[binary]`). SQLite needs no extra driver.

```bash
export DATABASE_URL="postgresql+psycopg://clhear:clhear@127.0.0.1:5432/clhear"
clhear migrate
```

One install uses one database. Serve and worker must share that URL.

## Private container

The image is `ghcr.io/reg42-ai/clhear`, built for `linux/amd64` and `linux/arm64`. Pin the digest from `release-manifest.json` on the GitHub release. The process runs as uid 10001. The root filesystem can be read-only. `/tmp` must be writable. Inside the image:

- the database defaults to `sqlite:////tmp/clhear.db`;
- scopes live in `/tmp/scopes`;
- read texts are kept in `/tmp/artifacts`;
- `local_text` files are read from `/sources` (mount your documents there, read-only is fine).

For anything you want to keep, use Postgres or mount a volume and point `DATABASE_URL` at it.

```bash
docker run --rm --read-only --tmpfs /tmp -v "$PWD/texts:/sources:ro" \
  -e CLHEAR_LLM_PROVIDER=anthropic -e ANTHROPIC_API_KEY \
  ghcr.io/reg42-ai/clhear@sha256:<digest> doctor --check-model
```

Commands: `serve`, `worker`, `run`, `migrate`.

`serve` listens on `127.0.0.1` unless `CLHEAR_BIND_HOST` is set. Any other address requires `CLHEAR_SERVICE_TOKENS` or `CLHEAR_SERVICE_TOKEN_FILE`. Several tokens, comma-separated, are accepted so you can rotate. Keep the task on a private network and pass your own security group.

## Optional AWS task

[deploy/terraform/aws](../deploy/terraform/aws) starts one private Fargate task. You pass subnet ids, the image digest, and a security group id. `assign_public_ip` defaults to false. The module creates no public load balancer. Set `command` to `["serve"]` or `["worker"]` and run one of each when you want the HTTP API. Pass `service_token_secret_arn` when the bind address is open beyond loopback.

## Release manifest

Each tag `vX.Y.Z` publishes `release-manifest.json`:

- `engine_version`, `api_version`, `api_compatible_with`
- `image` — the image by digest
- `schema_revision_from`, `schema_revision_to`
- `migrations_reversible`, `breaking`
- `changelog_url`

`clhear version` prints the tag, for example `v0.2.0`. `GET /v1/version` reports the same engine version plus the API version, schema revision, and image digest.
