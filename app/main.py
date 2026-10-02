"""FastAPI entrypoint for the two-layer grid routing service."""
from __future__ import annotations

from fastapi import FastAPI

from .schemas import RouteRequest, RouteResponse
from .solver import TIME_LIMIT_SECONDS, solve

app = FastAPI(
    title="Two-Layer Grid Router",
    version="1.0.0",
    description=(
        "Jointly routes two-pad nets on a two-layer grid using OR-Tools CP-SAT. "
        "Objective: minimize total vias first, then total same-layer moves. "
        "Invalid requests are rejected with HTTP 422."
    ),
)


@app.get("/", tags=["meta"])
def index() -> dict:
    return {
        "service": "two-layer-grid-router",
        "route_endpoint": "POST /route",
        "docs": "/docs",
        "time_limit_seconds": TIME_LIMIT_SECONDS,
    }


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/route", response_model=RouteResponse, tags=["routing"])
def route(request: RouteRequest) -> dict:
    return solve(request)
