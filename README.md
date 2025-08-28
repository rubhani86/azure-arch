# Azure Architecture Scraper

Scrapes **Azure reference architectures** from public **GitHub repos** (default: Microsoft’s **Azure Quickstart Templates**) using the GitHub API, parses **ARM templates** to extract services/parameters/outputs, and stores normalized documents in **MongoDB**.  
A **FastAPI** backend exposes endpoints to **trigger scrapes** and **list stored architectures**. A simple static **React UI** lets you trigger a scrape and view results.

> No Azure account required. GitHub-only. Works with/without a GitHub token (see **Auth & Rate Limits**).

---

## Features

- **GitHub-only** scraping (no Azure creds needed)
- Parses ARM templates (`azuredeploy.json`, `main.json`, etc.)
- Normalized document saved to Mongo
- **REST API** (`/scrape`, `/architectures`, `/healthz`)
- **Tiny UI** (`frontend/index.html`) to trigger scrapes & browse results
- Works **without** a GitHub token (falls back to **Contents API** walker)
- **Docker Compose** stack: API + Mongo + (optional) Mongo Express + UI (nginx)

---

## Project Structure

```
azure-arch-scraper/
├─ backend/
│  ├─ app.py
│  ├─ azure_architectures.py
│  ├─ requirements.txt
│  └─ Dockerfile
├─ frontend/
│  └─ index.html
├─ docker-compose.yml
├─ .env
└─ README.md
```

---

## Environment Variables

Create a **`.env`** in the project root (same folder as `docker-compose.yml`).

### Minimal example (Docker Compose)
```ini
# --- GitHub ---
# Leave blank to run token-free; the app will switch to the Contents API walker.
GITHUB_TOKEN=
# Force walker even if you have a token (optional)
FORCE_CONTENTS_WALK=true

# Repos to scrape (comma-separated). Format: Owner/Repo[:subdir]
GITHUB_SOURCES=Azure/azure-quickstart-templates:quickstarts

# --- Mongo (Compose talks to 'mongo' service hostname) ---
MONGODB_URI=mongodb://mongo:27017
MONGO_DB_NAME=azure_arch_db
MONGO_COLL_NAME=architectures
```

### Local development tip
When running **outside** Docker, Mongo is usually `localhost:27017`.  
Set:
```ini
MONGODB_URI=mongodb://localhost:27017
```

---

## Run Locally (no Docker)

**Prereqs:** Python 3.11+, MongoDB running on `localhost:27017`.

```bash
# 1) Create venv & install deps
python -m venv .venv
source .venv/bin/activate   # (Linux/macOS)
# .venv\Scripts\Activate   # (Windows PowerShell)

pip install -r backend/requirements.txt

# 2) Start API
cd backend
uvicorn app:app --reload
# API at http://localhost:8000

# 3) Start UI
cd ../frontend
python -m http.server 5173
# UI at http://localhost:5173
```

---

## Run with Docker Compose

This brings up **Mongo**, **API**, **Mongo Express**, and the **frontend (nginx)**.

```bash
docker compose up --build
```

- API: http://localhost:8000 (Swagger at `/docs`)
- UI:  http://localhost:5173
- Mongo Express: http://localhost:8081 (admin/admin)
