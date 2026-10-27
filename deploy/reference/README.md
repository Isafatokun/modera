# Runnable reference setup

From the repository root, with Docker Compose v2 installed:

```sh
export MODERA_API_KEY="$(openssl rand -hex 32)"
docker compose up --build --force-recreate --wait --wait-timeout 600
docker compose --profile checks run --rm --no-deps check
```

Preparation downloads and warms the pinned checkpoints into a persistent named
volume as UID 10001. Serving starts only after successful preparation, loads from
that volume read-only and waits for model readiness. The first run downloads about
0.6 GiB of weights. Rerun after an interrupted download; keep the volume to reuse
cached files. `docker compose down` stops the setup without deleting weights.

The API binds to `127.0.0.1:8000`. Set `MODERA_PORT` to choose another port.
The check submits neutral text and a generated white PNG, verifies authentication,
model provenance, request IDs and metrics. It does not assess moderation accuracy.

## URL and unavailable-source checks

URLs remain disabled until an exact host allowlist is configured. This optional
check uses public HTTPBingo fixtures, not submitted user media:

```sh
export MODERA_IMAGE_ALLOWED_HOSTS='["httpbingo.org"]'
export MODERA_CHECK_IMAGE_URL=https://httpbingo.org/image/png
export MODERA_CHECK_UNAVAILABLE_URL=https://httpbingo.org/status/503
docker compose up --wait --wait-timeout 600
docker compose --profile checks run --rm --no-deps check
```

The unavailable URL must return a retryable `503 input_fetch_failed`, `Retry-After`
and no decision. These live checks require outbound HTTPS and depend on the source
being available; CI enables them explicitly. Substitute your own allowed media host
for an application deployment. No redirects or private network destinations are
permitted. The check reports skipped URL checks when their variables are unset.

## Model and policy choices

| Model | Why used here | Limits |
| --- | --- | --- |
| [Horizon Small](https://huggingface.co/Horizon-Labs/multilingual-toxicity-small/blob/01adcc628a33848569816d4851a38544c321b92c/README.md) | CPU-compatible multilingual toxicity classifier; pinned sigmoid adapter | Publisher reports 34 languages; quality varies. This profile maps only toxicity. Local English/French/Spanish checks establish operation, not language accuracy. |
| [Viddexa Mini](https://huggingface.co/viddexa/nsfw-detection-2-mini/blob/15f61cddc0a1a2a9176f018fb6838ef92c8163cc/README.md) | Compatible compact photo classifier; better local diagnostic recall than Nano | Nudity/sexual-content categories, not violence, hate, OCR or age verification. Known artwork and transformed-image errors. |

Both pinned model cards declare Apache-2.0; their referenced base cards declare
[MIT for mmBERT Small](https://huggingface.co/jhu-clsp/mmBERT-small) and
[Apache-2.0 for EfficientNet B4](https://huggingface.co/google/efficientnet-b4).
Neither pinned classifier repository supplies a standalone LICENSE/NOTICE file.
Weights are downloaded separately; retain applicable upstream notices when redistributing.

The earlier Mini diagnostic at `max(porn, hentai) >= 0.95` found 8 true positives,
17 true negatives, one missed nude artwork and no false positives among 26 additional
source images. Across 36 correlated transformations it found four misses and one
false positive. These small, singly-labelled samples do not establish broad quality;
the sensitive fixtures are not included in this repository. Publisher benchmarks
are separate from these local results.

The image policy uses that same experimental block threshold for porn/hentai;
the `sexy` output does not trigger a decision. Text review/block thresholds of
0.5/0.85 are illustrative and not independently calibrated. Unsupported safety
categories are not checked by this profile. An `allow` means these configured
checks passed. This is a runnable starting point, not an automatic service default.

Edit the two JSON files here or set `MODERA_MODELS_FILE` and `MODERA_POLICIES_FILE`
to alternative host paths, then rerun preparation/startup. Configuration stays
read-only inside the containers. For production ingress, resource sizing and
termination behavior, see [operations](../../docs/operations.md).
