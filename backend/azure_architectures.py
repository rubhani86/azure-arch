#!/usr/bin/env python3
import base64
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

# ---- Config via env ----
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

# Comma-separated: "Owner/Repo[:subdir]"
# Example: "Azure/azure-quickstart-templates:quickstarts,Azure-Samples/xyz:templates"
GITHUB_SOURCES = [
    s.strip() for s in os.getenv("GITHUB_SOURCES", "Azure/azure-quickstart-templates:quickstarts").split(",") if s.strip()
]

# Force walk instead/in addition to github token usage
FORCE_CONTENTS_WALK = os.getenv("FORCE_CONTENTS_WALK", "").strip().lower() in {"1", "true", "yes"}


# ARM/Bicep filenames to look for
ARM_CANDIDATES = ["azuredeploy.json", "main.json", "template.json"]
BICEP_CANDIDATES = ["main.bicep", "azuredeploy.bicep", "template.bicep"]

# Mongo (used by mongo_save_many)
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "azure_arch_db")
MONGO_COLL_NAME = os.getenv("MONGO_COLL_NAME", "architectures")


# ----------------------------
# HTTP helpers
# ----------------------------
def github_headers() -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "azure-arch-scraper/1.1",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def gh_get(url: str, params: Optional[dict] = None) -> requests.Response:
    r = requests.get(url, headers=github_headers(), params=params, timeout=30)
    # Helpful diagnostics
    if r.status_code in (401, 403):
        msg = f"GitHub API {r.status_code} on {url}\nHeaders:{r.headers}\nBody:{r.text[:500]}"
        raise requests.HTTPError(msg, response=r)
    if r.status_code == 403 and "rate limit" in r.text.lower():
        reset = r.headers.get("x-ratelimit-reset")
        if reset:
            wait_s = max(0, int(reset) - int(time.time())) + 1
            print(f"[rate-limit] Waiting {wait_s}s for GitHub rate limit reset...", file=sys.stderr)
            time.sleep(wait_s)
            r = requests.get(url, headers=github_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r


def fetch_repo_content(owner: str, repo: str, path: str):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    try:
        data = gh_get(url).json()
        # Directory listing returns a list
        if isinstance(data, list):
            return data
        # File content (possibly base64 JSON)
        if isinstance(data, dict) and data.get("encoding") == "base64":
            decoded = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            try:
                return json.loads(decoded)
            except json.JSONDecodeError:
                return {"_raw": decoded}
        return data
    except requests.HTTPError as e:
        if getattr(e, "response", None) is not None and e.response.status_code == 404:
            return None
        raise


# ----------------------------
# Search strategies
# ----------------------------
def _search_code_api(owner: str, repo: str, subdir: Optional[str], limit: int) -> List[Dict]:
    """
    Primary: GitHub code search for ARM template filenames inside subdir (if provided).
    """
    results: List[Dict] = []
    filenames = ARM_CANDIDATES + BICEP_CANDIDATES
    page = 1
    per_page = 50

    while len(results) < limit and page <= 10:
        for fname in filenames:
            q = f"repo:{owner}/{repo} filename:{fname}"
            if subdir:
                q += f" path:{subdir}"
            resp = gh_get(f"{GITHUB_API}/search/code", params={"q": q, "per_page": per_page, "page": page}).json()
            items = resp.get("items", [])
            for it in items:
                results.append({"path": it.get("path")})
                if len(results) >= limit:
                    break
        page += 1

    # dedupe by path
    seen, uniq = set(), []
    for it in results:
        p = it["path"]
        if p not in seen:
            uniq.append(it)
            seen.add(p)
        if len(uniq) >= limit:
            break
    return uniq


def _walk_contents(owner: str, repo: str, root: str, limit: int) -> List[Dict]:
    """
    Fallback if search API is locked down: walk the repo contents starting at root (subdir).
    """
    queue = [root] if root else [""]
    hits: List[Dict] = []

    while queue and len(hits) < limit:
        cur = queue.pop(0)
        listing = fetch_repo_content(owner, repo, cur) or []
        if isinstance(listing, dict):
            # Single file at root scenario
            listing = [listing]
        for it in listing:
            if it.get("type") == "dir":
                queue.append(it["path"])
            elif it.get("type") == "file":
                name = it.get("name", "")
                if name in ARM_CANDIDATES + BICEP_CANDIDATES:
                    hits.append({"path": it["path"]})
                    if len(hits) >= limit:
                        break
    return hits


def find_templates(owner: str, repo: str, subdir: Optional[str], limit: int) -> List[Dict]:
    # If no token or explicitly forced, avoid /search/code and use Contents API walking
    if FORCE_CONTENTS_WALK or not GITHUB_TOKEN:
        return _walk_contents(owner, repo, subdir or "", limit)
    try:
        return _search_code_api(owner, repo, subdir, limit)
    except requests.HTTPError as e:
        # Fallback to walking on any failure (e.g., rate/permissions)
        print(f"[warn] search API failed; falling back to contents walk: {e}", file=sys.stderr)
        return _walk_contents(owner, repo, subdir or "", limit)


# ----------------------------
# ARM/Bicep reading + parsing
# ----------------------------
def infer_dir_from_path(p: str) -> str:
    return "/".join(p.split("/")[:-1]) if "/" in p else ""


def try_fetch_template(owner: str, repo: str, qs_dir: str) -> Tuple[Optional[dict], Optional[str]]:
    # Prefer ARM JSON
    for candidate in ARM_CANDIDATES:
        path = f"{qs_dir}/{candidate}" if qs_dir else candidate
        content = fetch_repo_content(owner, repo, path)
        if isinstance(content, dict) and (content.get("resources") or "parameters" in content or "$schema" in content):
            return content, candidate

    # Try Bicep as last resort: store raw bicep (no compile step), and parse loosely later if desired
    for candidate in BICEP_CANDIDATES:
        path = f"{qs_dir}/{candidate}" if qs_dir else candidate
        content = fetch_repo_content(owner, repo, path)
        if isinstance(content, dict) and "_raw" in content:
            # Minimal placeholder: treat as raw (you can later add real bicep->json build)
            return content, candidate

    return None, None


def parse_arm_resources(arm: dict) -> List[Dict]:
    res = arm.get("resources", [])
    parsed = []
    for r in res:
        entry = {"type": r.get("type"), "name": r.get("name"), "apiVersion": r.get("apiVersion")}
        if "dependsOn" in r:
            entry["dependsOn"] = r["dependsOn"]
        if isinstance(r.get("resources"), list):
            entry["children"] = [{"type": c.get("type"), "name": c.get("name"), "apiVersion": c.get("apiVersion")} for c in r["resources"]]
        parsed.append(entry)
    return parsed


def build_arch_doc(owner: str, repo: str, qs_dir: str, template_filename: str, arm_or_raw: dict, metadata: Optional[dict]) -> Dict:
    services = []
    params_keys, outputs_keys = [], []
    description, meta_name = None, None

    if "_raw" in arm_or_raw:
        # raw bicep—no accurate parse; you could add a regex-based extractor if you want
        services = []
    else:
        services = sorted({r["type"] for r in parse_arm_resources(arm_or_raw) if r.get("type")})
        params_keys = list((arm_or_raw.get("parameters") or {}).keys())
        outputs_keys = list((arm_or_raw.get("outputs") or {}).keys())

    # try metadata.json nearby
    meta = metadata or {}
    meta_name = meta.get("itemDisplayName") or meta.get("title") or meta.get("name")
    description = meta.get("description") or meta.get("summary")

    if not meta_name:
        meta_name = qs_dir.split("/")[-1] if qs_dir else repo

    base_dir_url = f"https://github.com/{owner}/{repo}/tree/master/{qs_dir}" if qs_dir else f"https://github.com/{owner}/{repo}/tree/master"
    tmpl_url = f"{base_dir_url}/{template_filename}" if qs_dir else f"https://github.com/{owner}/{repo}/blob/master/{template_filename}"

    return {
        "name": meta_name,
        "description": description,
        "repo": f"{owner}/{repo}",
        "quickstart_dir": qs_dir or "",
        "template_file": template_filename,
        "services": services,
        "resource_count": len(services),
        "source_urls": {"dir_html": base_dir_url, "template_html": tmpl_url},
        "metadata": meta,
        "arm_parameters_keys": params_keys,
        "arm_outputs_keys": outputs_keys,
    }


def try_fetch_metadata(owner: str, repo: str, qs_dir: str) -> Optional[dict]:
    content = fetch_repo_content(owner, repo, f"{qs_dir}/metadata.json" if qs_dir else "metadata.json")
    return content if isinstance(content, dict) else None


# ----------------------------
# Public API
# ----------------------------
def fetch_architectures(limit: int, sources: Optional[List[str]] = None) -> List[Dict]:
    """
    sources: list like ["Owner/Repo[:subdir]", ...]
    """
    sources = sources or GITHUB_SOURCES
    docs: List[Dict] = []
    per_source = max(1, limit // max(1, len(sources)))

    for src in sources:
        # parse "Owner/Repo[:subdir]"
        if ":" in src:
            repo_path, subdir = src.split(":", 1)
        else:
            repo_path, subdir = src, None
        owner, repo = repo_path.split("/", 1)

        hits = find_templates(owner, repo, subdir, per_source)
        for it in hits:
            path = it.get("path", "")
            qs_dir = infer_dir_from_path(path)
            arm_or_raw, tmpl_name = try_fetch_template(owner, repo, qs_dir)
            if not arm_or_raw or not tmpl_name:
                continue
            meta = try_fetch_metadata(owner, repo, qs_dir)
            doc = build_arch_doc(owner, repo, qs_dir, tmpl_name, arm_or_raw, meta)
            docs.append(doc)
            if len(docs) >= limit:
                return docs

    return docs


# ----------------------------
# Mongo save (optional)
# ----------------------------
def mongo_save_many(docs: List[Dict]) -> None:
    if not MONGODB_URI:
        print("[mongo] MONGODB_URI not provided; skipping save.", file=sys.stderr)
        return
    try:
        from pymongo import MongoClient
    except Exception as e:
        print(f"[mongo] pymongo not installed: {e}", file=sys.stderr)
        return

    client = MongoClient(MONGODB_URI)
    coll = client[MONGO_DB_NAME][MONGO_COLL_NAME]
    for d in docs:
        coll.update_one(
            {"repo": d["repo"], "quickstart_dir": d["quickstart_dir"], "template_file": d["template_file"]},
            {"$set": d},
            upsert=True,
        )
    print(f"[mongo] Upserted {len(docs)} documents into {MONGO_DB_NAME}.{MONGO_COLL_NAME}")
