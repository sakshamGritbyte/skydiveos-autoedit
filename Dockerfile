# One image for BOTH the API and the Celery worker (same code, different command).
FROM python:3.11-slim

# ffmpeg: the pipeline shells out to it for concat/render/frame-extract.
# libgl1 + libglib2.0-0: runtime libs cv2 (opencv-python) and mediapipe need, even
# headless — without them `import cv2` fails with "libGL.so.1: cannot open ...".
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 git curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install ONLY the locked runtime deps. The project itself is not a package
# (pyproject: [tool.uv] package = false); the code runs from /app via the workdir,
# so `api` / `analysis` / `render` import directly. Copying the lock first keeps this
# layer cached across code changes.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --format requirements-txt -o /tmp/req.txt \
    && uv pip install --system -r /tmp/req.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    JOBS_ROOT=/data/jobs
EXPOSE 8000

# Default = API. The worker service overrides this command in docker-compose.yml.
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
