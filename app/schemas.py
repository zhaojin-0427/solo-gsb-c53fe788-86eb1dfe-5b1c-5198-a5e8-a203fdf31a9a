"""Request/response schemas for the two-layer PCB grid router."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

MIN_DIM = 2
MAX_DIM = 20
MIN_NETS = 1
MAX_NETS = 6
LAYERS = (0, 1)


class Cell(BaseModel):
    """A grid cell on a specific layer."""

    x: int
    y: int
    layer: int = Field(ge=0, le=1)


class Pad(BaseModel):
    """A net pad: a coordinate on one of the two layers."""

    x: int
    y: int
    layer: int = Field(ge=0, le=1)


class Net(BaseModel):
    """A two-pad net with a non-negative via budget."""

    name: Optional[str] = None
    pads: list[Pad] = Field(min_length=2, max_length=2)
    via_limit: int = Field(ge=0)


class RouteRequest(BaseModel):
    width: int = Field(ge=MIN_DIM, le=MAX_DIM)
    height: int = Field(ge=MIN_DIM, le=MAX_DIM)
    blocked: list[Cell] = Field(default_factory=list)
    nets: list[Net] = Field(min_length=MIN_NETS, max_length=MAX_NETS)

    @model_validator(mode="after")
    def _check_geometry(self) -> "RouteRequest":
        for c in self.blocked:
            if not (0 <= c.x < self.width and 0 <= c.y < self.height):
                raise ValueError(
                    f"blocked cell ({c.x}, {c.y}) out of bounds for "
                    f"{self.width}x{self.height} grid"
                )
        blocked = {(c.x, c.y, c.layer) for c in self.blocked}
        seen: set[tuple[int, int, int]] = set()
        for i, net in enumerate(self.nets):
            for p in net.pads:
                key = (p.x, p.y, p.layer)
                if not (0 <= p.x < self.width and 0 <= p.y < self.height):
                    raise ValueError(
                        f"net {i} pad ({p.x}, {p.y}) out of bounds for "
                        f"{self.width}x{self.height} grid"
                    )
                if key in blocked:
                    raise ValueError(
                        f"net {i} pad ({p.x}, {p.y}, layer {p.layer}) "
                        f"is on a blocked cell"
                    )
                if key in seen:
                    raise ValueError(
                        f"duplicate pad at ({p.x}, {p.y}, layer {p.layer})"
                    )
                seen.add(key)
        return self


class PathCell(BaseModel):
    x: int
    y: int
    layer: int


class ViaPoint(BaseModel):
    x: int
    y: int


class NetResult(BaseModel):
    name: str
    path: list[PathCell]
    vias: list[ViaPoint]
    via_count: int
    move_count: int


class RouteResponse(BaseModel):
    # "optimal"  : proved best (min total vias, then min total moves)
    # "feasible" : time limit hit, best solution found so far (not proved)
    # "infeasible": proved that no routing exists
    # "unknown"  : time limit hit before any feasible solution was found
    status: str
    solve_time_seconds: float
    total_vias: Optional[int] = None
    total_moves: Optional[int] = None
    nets: Optional[list[NetResult]] = None
