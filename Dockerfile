# Stage 1: Build frontend
FROM node:20.18-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12.7-slim

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend
COPY backend/ ./backend/

# Copy built frontend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Create non-root user and data directory
RUN groupadd -r dashboard && useradd -r -g dashboard dashboard \
    && mkdir -p /app/data && chown -R dashboard:dashboard /app/data

USER dashboard

WORKDIR /app/backend

EXPOSE 8000

CMD ["python", "server.py"]
