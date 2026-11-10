# Reference setup

A runnable Docker Compose setup with real text and photo models. From the repository
root, with Docker Compose v2:

```sh
export MODERA_API_KEY="$(openssl rand -hex 32)"
docker compose up --build --force-recreate --wait --wait-timeout 600
docker compose --profile checks run --rm --no-deps check
```

A preparation container downloads and warms the pinned checkpoints into a named volume
as UID 10001. Serving starts only after that succeeds and reads the volume read-only.
The first run downloads about 0.6 GiB, so rerun it if a download is interrupted. The
volume is kept, and `docker compose down` stops everything without deleting weights.

The API binds to `127.0.0.1:8000`; set `MODERA_PORT` to change the port. The check sends
neutral text and a generated white PNG and verifies authentication, model provenance,
request IDs and metrics. It does not assess moderation accuracy.

For a clean-cache acceptance run with the real checkpoints:

```sh
python3 e2e/reference/verify.py --output reference-verification
```

This builds an isolated project, confirms the cache starts empty, checks text, uploads,
live URLs and a failing source, restarts and stops the service, and removes its
volume. `result.json` records hardware, preparation and readiness times and memory
snapshots (not peaks), with command logs. The `Reference profile` workflow runs it on a
fresh runner and keeps the evidence.

## URL checks

URL input stays off until you allow an exact host. This optional check uses public
HTTPBingo fixtures, not user media:

```sh
export MODERA_IMAGE_ALLOWED_HOSTS='["httpbingo.org"]'
export MODERA_CHECK_IMAGE_URL=https://httpbingo.org/image/png
export MODERA_CHECK_UNAVAILABLE_URL=https://httpbingo.org/status/503
docker compose up --wait --wait-timeout 600
docker compose --profile checks run --rm --no-deps check
```

The unavailable URL must return a retryable `503 input_fetch_failed` with `Retry-After`
and no decision. These checks need outbound HTTPS and the source to be up, and CI
enables them. Without the variables the check reports the URL tests as skipped. For your
own deployment, allow your own media host. Redirects and private destinations are
never followed.

## Models and policy

| Model | Why it is here | Limits |
| --- | --- | --- |
| [Horizon Small](https://huggingface.co/Horizon-Labs/multilingual-toxicity-small/blob/01adcc628a33848569816d4851a38544c321b92c/README.md) | Multilingual toxicity classifier that runs on CPU | The publisher reports 34 languages with varying quality. Only toxicity is mapped. |
| [Viddexa Mini](https://huggingface.co/viddexa/nsfw-detection-2-mini/blob/15f61cddc0a1a2a9176f018fb6838ef92c8163cc/README.md) | Compact photo classifier with better local recall than the smaller Nano | Nudity and sexual content only, not violence, hate, OCR or age. Known errors on artwork and transformed images. |

Both pinned model cards declare Apache-2.0. The base models they cite are
[mmBERT Small](https://huggingface.co/jhu-clsp/mmBERT-small) (MIT) and
[EfficientNet B4](https://huggingface.co/google/efficientnet-b4) (Apache-2.0). Neither
repository ships a standalone LICENSE or NOTICE, and weights are downloaded separately,
so keep the upstream notices if you redistribute them.

The photo policy blocks when `porn` or `hentai` reaches 0.95, and the `sexy` output
triggers nothing. Text review and block thresholds are 0.5 and 0.85. All of these are
illustrative and uncalibrated. In a small local check at 0.95 the photo model got 8 of 9
nude images right and flagged none of 17 others, but it missed one nude artwork, and
across 36 transformed copies it missed four and flagged one clean image. Those samples
are tiny and correlated, and the fixtures are not in this repository.

An `allow` means only that these configured checks passed. This is a starting point and
not a default. Edit the two JSON files here, or set `MODERA_MODELS_FILE` and
`MODERA_POLICIES_FILE` to other paths, and rerun preparation. They stay read-only in
the containers. For ingress, sizing and termination, see
[operations](../../docs/operations.md).
