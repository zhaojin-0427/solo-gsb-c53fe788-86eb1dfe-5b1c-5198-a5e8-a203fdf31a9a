"""Request/response schemas for the two-layer grid routing service.

Coordinate system: 0-based, ``0 <= x < width``, ``0 <= y < height``.
Layers are numbered ``0`` (bottom) and ``1`` (top).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class Grid(BaseModel):
    """Board dimensions in cells."""

    width: int = Field(ge=2, le=20, description="grid width in cells, 2..20")
    height: int = Field(ge=2, le=20, description="grid height in cells, 2..20")


class Blocked(BaseModel):
    """Forbidden (unroutable) cells, given per layer as [x, y] pairs."""

    layer0: list[tuple[int, int]] = Field(default_factory=list)
    layer1: list[tuple[int, int]] = Field(default_factory=list)


class Pad(BaseModel):
    """One pad of a net: a layer plus a cell coordinate."""

    layer: int = Field(ge=0, le=1)
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Net(BaseModel):
    """A two-pad net together with its (non-negative) via budget."""

    name: Optional[str] = Field(default=None, description="optional label, echoed back")
    pads: list[Pad] = Field(min_length=2, max_length=2)
    via_limit: int = Field(ge=0, description="maximum number of vias this net may use")


class RouteRequest(BaseModel):
    grid: Grid
    blocked: Blocked = Field(default_factory=Blocked)
    nets: list[Net] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def _check_geometry(self) -> "RouteRequest":
        w, h = self.grid.width, self.grid.height

        blocked_sets: list[set[tuple[int, int]]] = []
        for layer_idx, cells in enumerate((self.blocked.layer0, self.blocked.layer1)):
            cell_set: set[tuple[int, int]] = set()
            for x, y in cells:
                if not (0 <= x < w and 0 <= y < h):
                    raise ValueError(
                        f"blocked cell ({x}, {y}) on layer {layer_idx} is out of "
                        f"bounds for a {w}x{h} grid"
                    )
                cell_set.add((x, y))
            blocked_sets.append(cell_set)

        seen_pads: set[tuple[int, int, int]] = set()
        for i, net in enumerate(self.nets):
            label = net.name or f"#{i}"
            for pad in net.pads:
                if not (0 <= pad.x < w and 0 <= pad.y < h):
                    raise ValueError(
                        f"net {label}: pad ({pad.x}, {pad.y}) is out of bounds "
                        f"for a {w}x{h} grid"
                    )
                if (pad.x, pad.y) in blocked_sets[pad.layer]:
                    raise ValueError(
                        f"net {label}: pad ({pad.x}, {pad.y}) lies on a blocked "
                        f"cell of layer {pad.layer}"
                    )
                key = (pad.layer, pad.x, pad.y)
                if key in seen_pads:
                    raise ValueError(
                        f"duplicate pad at layer {pad.layer} cell ({pad.x}, {pad.y})"
                    )
                seen_pads.add(key)
        return self


class PathPoint(BaseModel):
    layer: int
    x: int
    y: int


class RoutedNet(BaseModel):
    name: str
    path: list[PathPoint] = Field(description="ordered cells from pads[0] to pads[1]")
    vias: list[tuple[int, int]] = Field(description="[x, y] coordinates of vias")
    via_count: int
    move_count: int


class Objectives(BaseModel):
    total_vias: int = Field(description="primary objective: vias over all nets")
    total_moves: int = Field(description="secondary objective: same-layer moves over all nets")


class RouteResponse(BaseModel):
    status: Literal["optimal", "feasible", "infeasible", "unknown"] = Field(
        description=(
            "optimal: proven optimal; feasible: solution found but optimality not "
            "proven within the time limit; infeasible: proven unsolvable; "
            "unknown: no solution found within the time limit"
        )
    )
    proven: bool = Field(description="true only for proven optimal / proven infeasible")
    solve_time_seconds: float
    time_limit_seconds: float
    detail: str
    objectives: Optional[Objectives] = None
    nets: Optional[list[RoutedNet]] = None
