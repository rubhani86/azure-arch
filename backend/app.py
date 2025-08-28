#!/usr/bin/env python3
import os
from typing import List, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from dotenv import load_dotenv
load_dotenv()

import azure_architectures as az

from pymongo import MongoClient, ASCENDING
from pymongo.errors import ServerSelectionTimeoutError

app = FastAPI(title="Azure Architecture (GitHub-only) API", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "azure_arch_db")
MONGO_COLL_NAME = os.getenv("MONGO_COLL_NAME", "architectures")

DEFAULT_SOURCES = [s.strip() for s in os.getenv("GITHUB_SOURCES", "").split(",") if s.strip()]

mongo_client: Optional[MongoClient] = None
coll = None


def _connect_mongo():
    global mongo_client, coll
    if not MONGODB_URI:
        coll = None
        return
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=4000)
    coll = mongo_client[MONGO_DB_NAME][MONGO_COLL_NAME]
    coll.create_index([("repo", ASCENDING), ("quickstart_dir", ASCENDING), ("template_file", ASCENDING)],
                      unique=True, name="uniq_repo_dir_template")
    coll.create_index([("name", ASCENDING)], name="name_idx")
    coll.create_index([("resource_count", ASCENDING)], name="rc_idx")


@app.on_event("startup")
def on_startup():
    try:
        _connect_mongo()
        if coll is not None:
            mongo_client.admin.command("ping")
            print("[api] Connected to MongoDB.")
        else:
            print("[api] MONGODB_URI not set. Running without persistence.")
    except ServerSelectionTimeoutError:
        print("[api] Could not reach MongoDB. API will run but persistence will fail.")


class ScrapeRequest(BaseModel):
    limit: int = 25
    save: bool = True
    sources: Optional[List[str]] = None  # ["Owner/Repo[:subdir]", ...]


class Architecture(BaseModel):
    name: str
    description: Optional[str] = None
    repo: Optional[str] = None
    quickstart_dir: Optional[str] = None
    template_file: Optional[str] = None
    services: List[str]
    resource_count: int
    source_urls: dict
    metadata: dict
    arm_parameters_keys: List[str]
    arm_outputs_keys: List[str]
    created_at: Optional[str] = None


@app.get("/healthz")
def healthz():
    ok = True
    mongo_ok = None
    if coll is not None:
        try:
            mongo_client.admin.command("ping")
            mongo_ok = True
        except Exception:
            mongo_ok = False
            ok = False
    return {"ok": ok, "mongo": mongo_ok}


@app.get("/architectures", response_model=List[Architecture])
def list_architectures(
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200),
    q: Optional[str] = Query(None),
    min_resources: Optional[int] = Query(None, ge=0),
    sort_by: str = Query("name"),
    sort_dir: str = Query("asc"),
):
    if coll is None:
        raise HTTPException(status_code=400, detail="No MongoDB configured. Set MONGODB_URI.")
    filt = {}
    if q:
        filt["name"] = {"$regex": q, "$options": "i"}
    if min_resources is not None:
        filt["resource_count"] = {"$gte": min_resources}
    sort_key = "name" if sort_by not in ("name", "resource_count") else sort_by
    sort_dir_flag = 1 if sort_dir.lower() == "asc" else -1
    cur = coll.find(filt).sort(sort_key, sort_dir_flag).skip(skip).limit(limit)
    out = []
    for d in cur:
        d.pop("_id", None)
        out.append(d)
    return out


@app.post("/scrape", response_model=List[Architecture])
def scrape(req: ScrapeRequest = Body(...)):
    sources = req.sources or DEFAULT_SOURCES
    if not sources:
        # fall back to module defaults if env is empty
        sources = None

    docs = az.fetch_architectures(limit=req.limit, sources=sources)
    now = datetime.utcnow().isoformat() + "Z"
    for d in docs:
        d["created_at"] = now

    if req.save:
        if coll is None:
            raise HTTPException(status_code=400, detail="save=True but no MongoDB configured (set MONGODB_URI).")
        az.mongo_save_many(docs)
        saved = list(coll.find({"created_at": now}).limit(req.limit))
        for s in saved:
            s.pop("_id", None)
        return saved

    return docs
