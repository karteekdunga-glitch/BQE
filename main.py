# main.py
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
import requests
from fastapi import FastAPI, HTTPException, Query
from dotenv import load_dotenv

load_dotenv()

# ==================== CONFIG ====================
BQE_CLIENT_ID = os.getenv("BQE_CLIENT_ID")
BQE_CLIENT_SECRET = os.getenv("BQE_CLIENT_SECRET")
BQE_REFRESH_TOKEN_ENV = os.getenv("BQE_REFRESH_TOKEN")
BQE_API_BASE = "https://api.bqecore.com/api"
BQE_TOKEN_URL = "https://api-identity.bqecore.com/idp/connect/token"
TOKEN_FILE = "bqe_token.json"

app = FastAPI(title="BQE Core API")

# ==================== BOSS-APPROVED STATUS CODES ====================
STATUS_MAP = {
    "active": 0,
    "complete": 2,
    "hold": 3,
    "inactive": 4
}

# ==================== TOKEN SYSTEM (100% WORKING) ====================
def now_utc():
    return datetime.now(timezone.utc)

def load_token_file():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return None

def save_token_file(data):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def refresh_access_token(refresh_token: str):
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": BQE_CLIENT_ID,
        "client_secret": BQE_CLIENT_SECRET
    }
    r = requests.post(BQE_TOKEN_URL, data=payload)
    if r.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {r.text}")
    token_data = r.json()
    expires_at = now_utc() + timedelta(seconds=token_data["expires_in"] - 60)
    record = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", refresh_token),
        "expires_at": expires_at.isoformat()
    }
    save_token_file(record)
    return record["access_token"]

def get_access_token():
    data = load_token_file()
    if data:
        try:
            expires_at = datetime.fromisoformat(data["expires_at"])
            if expires_at > now_utc():
                return data["access_token"]
        except:
            pass
    refresh_token = data["refresh_token"] if data and "refresh_token" in data else BQE_REFRESH_TOKEN_ENV
    if not refresh_token:
        raise RuntimeError("No refresh token in .env or token.json")
    return refresh_access_token(refresh_token)

def get_headers():
    return {"Authorization": f"Bearer {get_access_token()}", "Accept": "application/json"}

# ==================== SAFE GET ====================
def safe_get(url: str, params=None):
    headers = get_headers()
    r = requests.get(url, headers=headers, params=params)
    
    if r.status_code == 401:
        refresh_access_token(BQE_REFRESH_TOKEN_ENV)
        r = requests.get(url, headers=get_headers(), params=params)
    
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"BQE Error: {r.text}")
    
    try:
        return r.json()
    except:
        return []

# 1. Clients – Paginated
@app.get("/clients")
def get_clients(page: int = Query(1, ge=1)):
    data = safe_get(f"{BQE_API_BASE}/client", {"page": page, "pageSize": 50})
    items = data.get("items", []) if isinstance(data, dict) else data
    total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
    return {
        "page": page,
        "count": len(items),
        "total": total,
        "clients": items
    }

# 2. ALL PROJECTS – NOW WITH STATUS FILTER + PAGINATION
@app.get("/projects")
def get_projects(
    page: int = Query(1, ge=1),
    status: Optional[str] = Query(
        None,
        description="Filter projects: active, complete, hold, inactive"
    )
):
    data = safe_get(f"{BQE_API_BASE}/project", {"page": page, "pageSize": 100})
    items = data.get("items", []) if isinstance(data, dict) else data

    applied_filter = "all"
    if status and status.strip().lower() in STATUS_MAP:
        target_status = STATUS_MAP[status.strip().lower()]
        items = [p for p in items if p.get("status") == target_status]
        applied_filter = status.strip().lower()

    total_before = data.get("total", len(items)) if isinstance(data, dict) else len(items)

    return {
        "page": page,
        "filter": applied_filter,
        "count": len(items),
        "total_in_bqe": total_before,
        "projects": items
    }

# 1. RESOURCES API – FIXED (No AttributeError)
@app.get("/clients/{client_id}/resources")
def get_client_resources(client_id: str):
    client_resp = requests.get(f"{BQE_API_BASE}/client/{client_id}", headers=get_headers())
    if client_resp.status_code != 200:
        raise HTTPException(404, "Client not found")
    client_data = client_resp.json()

    projects = []
    page = 1
    while True:
        resp = requests.get(
            f"{BQE_API_BASE}/project",
            headers=get_headers(),
            params={"clientId": client_id, "page": page, "pageSize": 100}
        )
        if resp.status_code != 200:
            break
        data = resp.json()

        # FIX: Handle both dict (with "items") and direct list
        items = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not items:
            break
        projects.extend(items)
        if len(items) < 100:
            break
        page += 1

    # Extract unique Project Manager & Principal
    resources = {}
    for p in projects:
        if p.get("managerId") and p.get("manager"):
            resources[p["managerId"]] = {"name": p["manager"], "role": "Project Manager"}
        if p.get("principalId") and p.get("principal"):
            resources[p["principalId"]] = {"name": p["principal"], "role": "Principal"}

    return {
        "client": {
            "id": client_data.get("id"),
            "company": client_data.get("company"),
            "name": client_data.get("name", "N/A")
        },
        "total_unique_resources": len(resources),
        "resources": list(resources.values())
    }


# 2. TIME ENTRIES API – FIXED (No AttributeError)
@app.get("/clients/{client_id}/timeentries")
def get_client_timeentries(client_id: str):
    client_resp = requests.get(f"{BQE_API_BASE}/client/{client_id}", headers=get_headers())
    if client_resp.status_code != 200:
        raise HTTPException(404, "Client not found")
    client_data = client_resp.json()

    entries = []
    page = 1
    while True:
        resp = requests.get(
            f"{BQE_API_BASE}/timeentry",
            headers=get_headers(),
            params={"clientId": client_id, "page": page, "pageSize": 100}
        )
        if resp.status_code != 200:
            break
        data = resp.json()

        # FIX: Handle both dict and list responses safely
        items = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not items:
            break
        entries.extend(items)
        if len(items) < 100:
            break
        page += 1

    # Group by resource
    resource_hours = {}
    for e in entries:
        res_id = e.get("resourceId")
        res_name = e.get("resource") or "Unknown"
        hours = float(e.get("actualHours") or 0)

        if res_id not in resource_hours:
            resource_hours[res_id] = {
                "name": res_name,
                "total_hours": 0.0,
                "entries": []
            }
        resource_hours[res_id]["total_hours"] += hours
        resource_hours[res_id]["entries"].append({
            "date": e.get("date"),
            "project": e.get("project"),
            "activity": e.get("activity"),
            "hours": hours,
            "description": e.get("description"),
            "billable": e.get("billable")
        })

    return {
        "client": {
            "id": client_data.get("id"),
            "company": client_data.get("company"),
            "name": client_data.get("name", "N/A")
        },
        "total_time_entries": len(entries),
        "resources_with_hours": [
            {
                "resource_name": v["name"],
                "total_hours": round(v["total_hours"], 2),
                "entry_count": len(v["entries"]),
                "entries": v["entries"]
            }
            for v in resource_hours.values()
        ]
    }