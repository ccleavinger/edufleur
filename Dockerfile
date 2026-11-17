FROM node:24-bookworm-slim AS web
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM rust:1-bookworm AS backend
RUN apt-get update && apt-get install -y --no-install-recommends cmake clang libclang-dev pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY backend/ backend/
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libssl3 && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
WORKDIR /app
COPY --from=backend /build/target/release/edufleur /usr/local/bin/edufleur
COPY --from=web /build/frontend/dist ./frontend/dist
ENV DATABASE_PATH=/data/edufleur.db CATALOG_DIR=/data/catalog/current BIND_ADDRESS=0.0.0.0:3000
USER app
VOLUME ["/data"]
EXPOSE 3000
CMD ["edufleur"]
