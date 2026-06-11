# One image for BOTH the API and the Celery worker (same code, different command).
FROM python:3.11-slim

# ffmpeg: the pipeline shells out to it for concat/render/frame-extract.
# The rest are runtime shared libs cv2 (opencv-python) + mediapipe need, even headless:
#   libgl1            -> libGL.so.1
#   libgles2          -> libGLESv2.so.2   (mediapipe; the missing-lib that failed jobs)
#   libegl1           -> libEGL.so.1      (mediapipe GL/EGL backend)
#   libglib2.0-0      -> libglib-2.0.so.0
#   libsm6/libxext6/libxrender1 -> X libs opencv links against
# Without these, `import cv2` / mediapipe init crash with "cannot open shared object file".
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 libgles2 libegl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
        git curl \
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
