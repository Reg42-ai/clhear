# Snapshots: publish a reference blueprint anyone can check and match

A snapshot is a release's reference blueprints written as plain files. Whoever builds a scope can publish one; whoever downloads it can check that the files are the ones published and find out whether their own organisation profile matches one of its profiles. Matching reads the files alone: it calls no model, stores nothing and needs none of the texts.

The engine ships no snapshot and no recipe. Each publisher brings its own texts, runs them, and decides where to publish.

## What a snapshot holds

| File | What it is |
| --- | --- |
| `manifest.json` | Format, title, licence, notice; the release, scope and engine version; the models the run used; each layer's revision and counts; the profiles; the sources; the SHA-256 of every other file and one `content_hash` over them |
| `blueprints/<profile>.json` | Each profile's reference blueprint, exactly as `GET /v1/releases/{release_id}/blueprints/{profile_id}` returns it, with the `profile_attributes` it was composed for |
| `applicability.json` | Every obligation in scope, the applicability conditions that decide whether it applies (jurisdiction, role, condition, each with the words it came from), and its verdict for each profile: `applies`, `not_applicable` or `undetermined` |
| `questions.json` | The questions the texts in scope raise, with their quotes: what a profile answers |
| `sources.json` | Each source in scope: kind, whether it binds, publisher, reference, where it was read from, and the version and SHA-256 of the text that was read |
| `recipe.json` | The sources, the scope and the profiles, to run the scope again |
| `README.md` | The same for people, with anything to read before use |

A snapshot holds no organisation's data. The texts themselves are not in it; each element quotes the clause words it rests on, with offsets.

## Make one

1. Register the sources, name the scope, store the profiles and run, as in the [README](../README.md). A recipe does the first three in one step:

   ```bash
   clhear recipe apply recipe.json
   clhear run --scope <scope> --profile-id <profile> [--profile-id <profile> ...]
   ```

2. Check the release before publishing it. The credibility questions in the [README](../README.md) apply. In particular: no failed sources, no record withheld by the anchoring check, no `open_questions` the profile meant to answer, and each source in `source_inventory` is `derived`.

3. Write the snapshot:

   ```bash
   clhear snapshot create --release <release_id> --out ai-reference-2026-10 \
     --licence CC-BY-4.0 --title "Reference blueprints, ..." --zip
   ```

   `--licence` is required: the licence the snapshot is published under, or `not-for-publication`. `--profile` picks some of the release's profiles (default: all). `--notice` prints a sentence at the top of the README. `--zip` also writes `<out>.zip`, with fixed file order and times, so the same files give the same `.zip` file.

`create` prints the `content_hash` and a list of `warnings`: a profile that leaves obligations undetermined, a source that is not derived, a layer that was not built. They are copied into the snapshot's README under "Read before use".

`create` refuses:

- a release made before snapshots existed, or by the `fake` sample provider (run the scope again);
- a profile the release does not hold;
- a source whose text may not be republished (registered `licence: restricted`, or a rights basis that allows derived facts only), unless the snapshot is `not-for-publication`. A snapshot quotes its sources, so such a text stays out of a published one.

## Check one

```bash
clhear snapshot verify ai-reference-2026-10.zip
```

The check passes when each file the manifest lists is present with its SHA-256, no other file is there, and the `content_hash` matches. Publish the `content_hash` next to the download, so anyone can compare.

## Match a profile against it

Write your answers to the snapshot's questions in a JSON file, in the same shape as the profiles in its manifest (`{"attributes": {...}}` or the attributes alone):

```json
{"attributes": {"jurisdictions": ["EU"], "roles": ["deployer"], "conditions": {"uses a model to decide on credit": true}}}
```

```bash
clhear snapshot match ai-reference-2026-10.zip --profile my-profile.json
```

A profile **matches** a snapshot profile when every obligation in scope gets the same verdict for both. The rule is the one composition uses: an obligation applies when every applicability condition is answered and holds, is not applicable when one fails, and is undetermined while one is unanswered. An answer no obligation depends on changes nothing.

- On a match, the reference blueprint of that profile is the one for yours, as published. The command exits `0`.
- Otherwise it lists, per snapshot profile, the obligations that get another verdict and the questions your profile leaves open, and exits `2`. Answer the questions, or compose a blueprint for your own profile with the engine.
- A snapshot that does not verify is not matched (exit `1`).

`--json` prints the full result.

## Run it again

`recipe.json` holds the sources (adapter, locator, kind, whether it binds, jurisdiction, publisher, reference, licence), the scope and the profiles. `clhear recipe apply recipe.json` registers them in another install. A source that was pasted rather than fetched is listed without its text and has to be supplied again. A new run reads the texts as published on that day, with the model configured there, so its records can differ; the manifests say which versions and models each run used.

## Publish it

Any static host serves a snapshot: a release asset on a code host, an object store, a web page. Publish the `.zip`, its `content_hash`, and the licence named in its manifest. A new release of the same scope is a new snapshot; keep the earlier ones available, so a reader can compare what changed.

## Limits to state with it

- A snapshot is as complete as its scope. Its `source_inventory` lists every text the clauses cite that the scope does not hold.
- The engine dates each obligation from the version of its source. Dates a text sets for its own parts (a part that applies from a later date) are not read yet: the evidence chain points at the clause, and the reader checks the text's own application clauses.
- Which obligations share a component comes from the model, in the texts' own words; the obligations each element cites, and the proof that the set covers them, are mechanical.
