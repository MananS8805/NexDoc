# nexDoc web app — production image.
#
# Built for speed parity with a local run:
#   * CPU-only PyTorch (the default wheel drags in ~GBs of CUDA the app never uses)
#   * the EasyOCR English+Hindi models are baked into the image (copied in at
#     build time), so a fresh instance never spends its first minute fetching ~300MB
#   * ONE uvicorn worker: the OCR model, result cache and analysis lock are
#     per-process; extra workers would each load their own copy of the model
#   * the OCR model is warmed in the background at startup (webapp/engine.py)
#
# Build:  python scripts/stage_ocr_models.py && docker build -t nexdoc .
# Run:    docker run --rm -p 8000:8000 -e NEXDOC_PASSWORD=choose-one nexdoc
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libglib is the one shared library OpenCV's headless build still wants.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app

WORKDIR /app

# Dependencies first (cached unless requirements change).
COPY requirements-docker.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision \
 && pip install -r requirements-docker.txt

# OCR models: staged on the host by `python scripts/stage_ocr_models.py` and COPIED
# in, because a download inside `docker build` can't resume and a single dropped
# connection failed this step before. The RUN below proves they load with the
# network disabled, so a bad/missing file fails the build, not the first request.
RUN mkdir -p /home/app/.EasyOCR/model && chown -R app:app /home/app/.EasyOCR
USER app
COPY --chown=app:app docker-models/ /home/app/.EasyOCR/model/
RUN python -c "import easyocr; easyocr.Reader(['en','hi'], gpu=False, download_enabled=False, verbose=False); print('OCR models load offline: OK')"

COPY --chown=app:app adapters ./adapters
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app qa ./qa
COPY --chown=app:app webapp ./webapp
COPY --chown=app:app tests/fixtures ./tests/fixtures

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8000'),timeout=4)"

# $PORT is honored because most PaaS hosts inject it.
CMD ["sh", "-c", "exec uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 75"]
