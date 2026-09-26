# CLHEAR

Open engine that turns chosen regulatory sources into a reference compliance blueprint.

CLHEAR derives layers L1 through L8 from the sources you choose, then composes a reference blueprint for an applicability profile.

## What it stores

The database holds public and licensed sources, the records derived from them, and an applicability profile. It does not hold a firm identity, actual controls, owners, or evidence.

One install uses one database.

## Install

Python 3.12. Install from a pinned tag:

```bash
pip install "clhear @ git+https://github.com/Reg42-ai/clhear.git@v0.1.0"
```

Or run the container `ghcr.io/reg42-ai/clhear` by digest. See [docs/install.md](docs/install.md).

## Quickstart

Offline. The fake provider writes sample layers. It does not use the network and it does not load a real regulation.

```bash
export CLHEAR_LLM_PROVIDER=fake
clhear init
clhear doctor
clhear quickstart
```

`clhear doctor` blocks a live run until `anthropic`, `openai_compatible`, or `bedrock` is configured. `clhear quickstart` stays on the fake provider.

## Declare a source, then run

A scope is a name and a list of source keys you wrote. Nothing is registered at startup.

```bash
clhear run --scope example-scope --profile-id example-profile
```

The same step over HTTP is `POST /v1/runs`. That call inserts a run and returns `run_id`. `clhear worker` executes the run.

## Read the blueprint

`GET /v1/releases/{release_id}/blueprints/{profile_id}` returns JSON:

- `items` — the selected blocks, each with an `explanation`
- `coverage` — each applicable obligation’s `source_key` and `clause_ref`
- `minimality` — why the selected set is minimal

## HTTP API

`/v1` is described in [openapi/clhear-v1.yaml](openapi/clhear-v1.yaml). `GET /v1/version` reports the engine version, API version, schema revision, and image digest.

`clhear serve` accepts requests with no token only when it is bound to `127.0.0.1`. Any other bind requires `Authorization: Bearer`.

## Versions

Pin a semver tag such as `v0.1.0`. Do not track `main`.

## License

[AGPL-3.0-only](LICENSE). Network use of a modified version is distribution under that license: the people who interact with the modified service must be able to obtain the corresponding source.
