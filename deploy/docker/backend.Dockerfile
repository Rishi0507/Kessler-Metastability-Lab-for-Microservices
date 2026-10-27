# syntax=docker/dockerfile:1.7

FROM debian:bookworm-slim AS engine
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ cmake ninja-build \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY engine/ engine/
RUN cmake -S engine -B engine/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
 && cmake --build engine/build \
 && ./engine/build/kessler-tests

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KESSLER_ENGINE_PATH=/usr/local/lib/libkessler_engine.so \
    KESSLER_HOST=0.0.0.0
RUN useradd --system --uid 10001 --create-home --home-dir /app kessler
WORKDIR /app
COPY backend/requirements.in backend/requirements.lock ./
RUN pip install -r requirements.in -c requirements.lock
COPY --from=engine /src/engine/build/libkessler_engine.so /usr/local/lib/libkessler_engine.so
COPY --from=engine /src/engine/build/kessler-sim /usr/local/bin/kessler-sim
COPY backend/kessler ./kessler
USER kessler
ENTRYPOINT ["python", "-m"]
CMD ["kessler.api", "--port", "9001"]
