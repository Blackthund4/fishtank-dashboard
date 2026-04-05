# Stage 1: Build frontend
FROM node:20.18-slim AS frontend-build

ARG GIT_COMMIT=unknown

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12.7-slim

ARG GIT_COMMIT=unknown
ENV BUILD_VERSION=$GIT_COMMIT

WORKDIR /app

# Install gosu and curl for dropping privileges and healthchecks
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y gosu curl

# Install Python dependencies (vendor/fishclient must be present before pip runs)
COPY backend/vendor/ ./vendor/
COPY backend/requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Copy backend
COPY backend/ ./backend/

# Copy built frontend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Create non-root user and data directory
RUN groupadd -r dashboard && useradd -r -g dashboard dashboard \
    && mkdir -p /app/data && chown -R dashboard:dashboard /app/data

# Copy entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /app/backend

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
