# Install

## Local SQLite

The default database is `sqlite:///./clhear.db`.

```bash
pip install "clhear @ git+https://github.com/Reg42-ai/clhear.git@v0.1.0"
export CLHEAR_LLM_PROVIDER=fake
clhear init
clhear migrate
clhear doctor
```

`clhear init` creates an empty `scopes/` directory. You add scope files. The engine does not ship one.

## Postgres

Set `DATABASE_URL` to a Postgres URL the process can reach:

```bash
export DATABASE_URL="postgresql+psycopg://clhear:clhear@127.0.0.1:5432/clhear"
clhear migrate
```

Install a Postgres driver in that environment (`psycopg[binary]`). SQLite needs no extra driver.

## Model providers

A live run needs one of these. `clhear doctor` exits non-zero when none is configured.

| `CLHEAR_LLM_PROVIDER` | Also set |
| --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY`, optional `CLHEAR_LLM_MODEL` |
| `openai_compatible` | `OPENAI_BASE_URL`, `OPENAI_API_KEY`, optional `CLHEAR_LLM_MODEL` |
| `bedrock` | `BEDROCK_MODEL_ID` or `CLHEAR_LLM_MODEL`, and the usual AWS credentials |

`CLHEAR_LLM_PROVIDER=fake` is the offline quickstart only. `clhear run` refuses it.

## Private container

The image is `ghcr.io/reg42-ai/clhear`, published for `linux/amd64` and `linux/arm64`. Pin the digest from the release manifest. The process runs as uid 10001. The root filesystem can be read-only; `/tmp` must be writable. The default database path inside the image is `sqlite:////tmp/clhear.db`.

```bash
docker run --rm --read-only --tmpfs /tmp \
  -e CLHEAR_LLM_PROVIDER=fake \
  ghcr.io/reg42-ai/clhear@sha256:<digest> quickstart
```

Commands: `serve`, `worker`, `run`, `migrate`.

`serve` listens on `127.0.0.1` unless `CLHEAR_BIND_HOST` is set. A non-loopback bind requires `CLHEAR_SERVICE_TOKENS` or `CLHEAR_SERVICE_TOKEN_FILE` and does not start without one. Do not publish a public port. Put the task in a private subnet and pass your own security group.

Several tokens, comma-separated, are accepted so you can rotate.

## Optional AWS layout

[deploy/terraform/aws](../deploy/terraform/aws) starts one private task. You pass subnet ids, the image digest, and a security group id. It does not create a public load balancer. `assign_public_ip` defaults to false.

## Release manifest

Each tag `vX.Y.Z` publishes `release-manifest.json` with `engine_version`, `api_version`, `api_compatible_with`, the image by digest, `schema_revision_from`, `schema_revision_to`, `migrations_reversible`, `breaking`, and `changelog_url`. `clhear version` prints that tag.
