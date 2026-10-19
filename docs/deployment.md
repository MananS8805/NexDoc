# Deploying nexDoc

nexDoc ships as one container: a single FastAPI/uvicorn process that serves the
UI and the API. This guide covers building it, the settings that matter for
speed, what to expect on a given CPU budget (measured), and what is **not**
verified.

## Build and run

```bash
python scripts/stage_ocr_models.py     # one-time: copies the OCR weights into ./docker-models/
docker build -t nexdoc .
docker run --rm -p 8000:8000 -e NEXDOC_PASSWORD=choose-a-password nexdoc
# open http://localhost:8000  (any username, that password)
```

Why the staging step: the OCR weights (~300 MB) used to be downloaded *inside*
`docker build`, which cannot resume — one dropped connection failed the whole
build. The staging script does the download (if needed) on the host with
retries, and the Dockerfile then verifies the weights load with the network
disabled, so a bad file fails the build rather than the first request.
`docker-models/` is git-ignored.

Image size: ~764 MB (CPU-only PyTorch, headless OpenCV, models baked in).

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `NEXDOC_PASSWORD` | unset (open) | When set, every route except `/healthz` requires HTTP Basic auth with this password (any username). **Set it for any deployment that isn't private**: the app has no login, uploads private documents, and its "Ask a question" box spends the server's Groq key. |
| `GROQ_API_KEY` | unset | Enables the optional Q&A box. Pass it as a platform secret, never bake it into the image (`.env` is excluded by `.dockerignore`). |
| `PORT` | `8000` | Honored by the container command (most PaaS hosts inject it). |
| `NEXDOC_TORCH_THREADS` | auto | Override the OCR thread count. Normally leave unset — see below. |

`GET /healthz` is open and returns `200` immediately, even while the OCR model is
still warming (spreadsheets and born-digital PDFs work at once; only OCR work
waits). `GET /api/engine` reports the real warm-up state.

**Run exactly one worker** (the image does). The OCR model, the result cache and
the analysis lock are per-process; more workers each load their own copy of the
model and lose the cache.

## Why speed survives containerization (and the one trap)

Three things make a deployed instance as fast as a local run, all verified:

1. **The model is baked in and warmed at startup.** Model load was 7.9–13.6 s in
   the container (22 s native on the same machine), and happens in the
   background before the first request.
2. **Results are cached by file hash**, so repeat files replay instantly.
3. **OCR threads match the CPU quota.** PyTorch sizes its thread pool from the
   *host's* cores, not the container's quota. On a 4-CPU quota, letting PyTorch
   pick its default 6 threads was **2.7× slower on the diagram and 1.36× slower
   on the scan** than the auto-detected 4. The app reads the cgroup quota /
   CPU affinity and caps threads to it (see `webapp/engine.py: detect_cpu_limit`).
   If you ever disable that, expect this slowdown. The same effect shows on
   uncapped hosts too: in a 12-logical-CPU VM, 12 threads ran the diagram 3×
   slower than 6.

## What to expect per CPU budget

Measured on the dev machine (12 logical CPUs, Windows + Docker Desktop/WSL2),
fresh container each row, single end-to-end run through the real streaming
endpoint, same time window. The host was also running other apps, which moves
numbers by roughly ±20% — read these as sizing guidance, not a benchmark suite.

| Setup | Model ready | UML diagram (8 shapes, 8 edges) | Scanned page (EN + Hindi) |
|---|---|---|---|
| Native Windows, no container | 22.0 s | 4.0 s | 27.3 s |
| Container, no CPU limit | 10.7 s | 5.7 s | 25.7 s |
| Container, `--cpus=4` | 9.7 s | 3.6 s | 28.1 s |
| Container, `--cpus=2 --memory=2g` | 7.9 s | 3.1 s | 42.7 s |
| Container, `--cpus=1 --memory=2g` | 10.3 s | 5.8 s | 58.6 s |

Takeaways:

- **Container ≈ native speed.** An alternating same-window comparison on the scan
  gave 23.7 s (container) vs 22.6 s (native).
- **Diagrams and spreadsheets are fast on any size** (a few seconds; spreadsheet
  and born-digital PDF are tens of ms).
- **Scanned pages are the cost driver and scale with CPUs**: ~28 s at 4 CPUs,
  ~43 s at 2, ~59 s at 1. If scans are a main use case, provision 4+ vCPUs; if
  not, 2 vCPU / 2 GB is plenty. **2 GB RAM was enough** in every run above.
- Everything is served by one process and analyses run one at a time (a queue
  note appears in the UI), so size for the CPUs *one* analysis can use.

## Where it can and can't run

- **Fits:** anything that runs a container with ≥2 GB RAM and ≥2 vCPU and keeps
  an instance running — a VPS with Docker, Fly.io, Railway, Render (paid
  tiers), Google Cloud Run (use "CPU always allocated" and min instances = 1,
  otherwise every cold start reloads the model and loses the cache).
- **Does not fit:** Vercel/Netlify-style serverless functions — a ~760 MB image,
  a model that must stay warm in memory, in-memory cache, and uploads that take
  tens of seconds to process.
- **Not verified here:** I built and benchmarked the image locally with Docker
  Desktop. I did **not** deploy it to any cloud platform, so platform-specific
  settings (health-check paths, secrets, volumes — none are needed — and
  scale-to-zero behavior) are untested.

## Operating notes

- No persistence: uploads are processed from a temp file that is deleted
  afterwards; the result cache is in-memory (12 entries) and cleared on restart.
- Uploads are capped at 50 MB (`413` beyond that).
- HTTPS: terminate TLS at the platform/reverse proxy; Basic auth over plain HTTP
  sends the password in the clear.
- The password gate is a deliberate minimum, not Phase-5 auth. For real
  multi-user use (roles, audit trail), see `CLAUDE.md` Phase 5.
