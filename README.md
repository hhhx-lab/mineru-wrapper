# MinerU Wrapper

FastAPI/Celery wrapper for the native MinerU API. It provides an official-compatible task API, Redis-backed task state, signed `full.zip` downloads, and automatic artifact TTL cleanup.

## Services

- `mineru-wrapper-api`: FastAPI service on port `8100`.
- `mineru-wrapper-worker`: Celery worker that downloads source files, submits native MinerU jobs, and writes `full.zip`.
- `mineru-wrapper-redis`: Redis broker/state store with persistent data in `./data/redis`.

## Runtime Data

Runtime files are mounted under `./data`:

- `data/redis`: Redis persistence. Do not delete during artifact cleanup.
- `data/results/{task_id}`: Generated `full.zip` artifacts.
- `data/tmp/{task_id}`: Per-task temporary working files.

The wrapper cleanup logic only deletes per-task `results/{task_id}` and `tmp/{task_id}` directories after artifacts expire. Redis data and task records are preserved.

## API

Create a task:

```bash
curl -X POST http://100.64.0.2:8100/api/v4/extract/task \
  -H "Authorization: Bearer $WRAPPER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/file.pdf","model_version":"vlm"}'
```

Get task status:

```bash
curl http://100.64.0.2:8100/api/v4/extract/task/$TASK_ID \
  -H "Authorization: Bearer $WRAPPER_AUTH_TOKEN"
```

Delete one task's artifacts after downstream processing is complete:

```bash
curl -X DELETE http://100.64.0.2:8100/api/v4/extract/task/$TASK_ID/artifacts \
  -H "Authorization: Bearer $WRAPPER_AUTH_TOKEN"
```

## Configuration

Copy `.env.example` to `.env` and set real secrets:

```bash
cp .env.example .env
```

Important TTL settings:

- `ARTIFACT_DONE_TTL_HOURS`: maximum retention after task completion.
- `ARTIFACT_DOWNLOAD_TTL_HOURS`: retention after the last successful artifact download.
- `ARTIFACT_CLEANUP_INTERVAL_SECONDS`: cleanup loop interval.

## Run

```bash
docker compose up -d --build
```

Health check:

```bash
curl http://100.64.0.2:8100/health
```
