"""CP-SAT based joint router for the two-layer grid board.

Model
-----
For every net we build a unit-flow problem on the board graph:

* nodes   : free (not blocked) cells ``(x, y, layer)`` on both layers
* arcs    : directed arcs between orthogonally adjacent free cells on the
            same layer (a "move"), plus a pair of directed arcs between
            ``(x, y, 0)`` and ``(x, y, 1)`` wherever both cells are free
            (a "via")
* the first pad is the source (exactly one outgoing unit, no incoming),
  the second pad is the sink (exactly one incoming unit, no outgoing),
  every other node conserves flow and is entered at most once, so each
  net's used cells form a simple s-t path (plus, in sub-optimal
  incumbent solutions, possibly disjoint cycles which are dropped when
  the reported path is extracted -- removing them can only improve the
  objective, so proved-optimal solutions never contain them).

A net "occupies" every cell of its path.  A via variable at ``(x, y)`` is
forced to 1 whenever the net occupies both layers at that coordinate, so
vias occupy both layers by construction.

Separation: for every pair of distinct nets and every layer, occupied
cells may neither coincide nor be orthogonally adjacent.

Objective (lexicographic, encoded with a big weight):
    1. minimise total number of vias
    2. minimise total number of same-layer moves
"""
from __future__ import annotations

import heapq
import os
import time
from collections import defaultdict

from ortools.sat.python import cp_model

from .schemas import NetResult, PathCell, RouteRequest, RouteResponse, ViaPoint

# A simple path over a 20x20x2 board uses at most 799 moves per net and
# there are at most 6 nets, so any weight larger than 6*799 makes the via
# count strictly dominate the move count.
VIA_WEIGHT = 6 * (20 * 20 * 2 - 1) + 1

DEFAULT_TIME_LIMIT = 10.0  # seconds, per specification

STATUS_OPTIMAL = "optimal"
STATUS_FEASIBLE = "feasible"
STATUS_INFEASIBLE = "infeasible"
STATUS_UNKNOWN = "unknown"


def _time_limit() -> float:
    try:
        return float(os.environ.get("ROUTER_TIME_LIMIT", DEFAULT_TIME_LIMIT))
    except ValueError:
        return DEFAULT_TIME_LIMIT


_ORTHO = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _heuristic_paths(
    req: RouteRequest,
    free: set[tuple[int, int, int]],
    via_sites: set[tuple[int, int]],
) -> list[list[tuple[int, int, int]] | None]:
    """Fast sequential router used only to seed CP-SAT with a hint.

    Nets are routed one after another with Dijkstra (move cost 1, via
    cost 10000) while treating cells used by -- or adjacent to -- other
    nets as blocked, so the returned paths jointly satisfy every rule.
    Returns None for nets that could not be routed.
    """
    # cells each net must keep clear of: other nets' pads and their
    # same-layer orthogonal neighbours
    pad_zone: list[set[tuple[int, int, int]]] = []
    for net in req.nets:
        zone: set[tuple[int, int, int]] = set()
        for p in net.pads:
            zone.add((p.x, p.y, p.layer))
            for dx, dy in _ORTHO:
                zone.add((p.x + dx, p.y + dy, p.layer))
        pad_zone.append(zone)

    occupied: set[tuple[int, int, int]] = set()  # cells of routed nets
    paths: list[list[tuple[int, int, int]] | None] = []
    for k, net in enumerate(req.nets):
        src = (net.pads[0].x, net.pads[0].y, net.pads[0].layer)
        dst = (net.pads[1].x, net.pads[1].y, net.pads[1].layer)
        forbidden = set(occupied)
        for (x, y, layer) in occupied:
            for dx, dy in _ORTHO:
                forbidden.add((x + dx, y + dy, layer))
        for j, zone in enumerate(pad_zone):
            if j != k:
                forbidden |= zone
        forbidden.discard(src)
        forbidden.discard(dst)

        limit = min(net.via_limit, 32)  # cap the Dijkstra state space
        dist: dict[tuple[tuple[int, int, int], int], int] = {(src, 0): 0}
        prev: dict[tuple[tuple[int, int, int], int], tuple[tuple[int, int, int], int]] = {}
        heap = [(0, src, 0)]
        goal = None
        while heap:
            d, cell, v = heapq.heappop(heap)
            state = (cell, v)
            if dist.get(state, -1) != d:
                continue
            if cell == dst:
                goal = state
                break
            x, y, layer = cell
            for dx, dy in _ORTHO:
                nb = (x + dx, y + dy, layer)
                if nb in free and nb not in forbidden:
                    ns = (nb, v)
                    if d + 1 < dist.get(ns, 1 << 60):
                        dist[ns] = d + 1
                        prev[ns] = state
                        heapq.heappush(heap, (d + 1, nb, v))
            if (x, y) in via_sites and v < limit:
                nb = (x, y, 1 - layer)
                if nb not in forbidden:
                    ns = (nb, v + 1)
                    if d + 10000 < dist.get(ns, 1 << 60):
                        dist[ns] = d + 10000
                        prev[ns] = state
                        heapq.heappush(heap, (d + 10000, nb, v + 1))

        if goal is None:
            paths.append(None)
            continue
        path = _reconstruct(prev, src, goal)
        # safety net: a shortest path never occupies both layers at one
        # coordinate without paying for the via, but verify the limit
        layers_at: dict[tuple[int, int], set[int]] = defaultdict(set)
        for (x, y, layer) in path:
            layers_at[(x, y)].add(layer)
        if sum(1 for ls in layers_at.values() if len(ls) == 2) > net.via_limit:
            paths.append(None)
            continue
        paths.append(path)
        occupied |= set(path)
    return paths


def _reconstruct(prev, src, goal):
    rev = [goal]
    while rev[-1][0] != src:
        rev.append(prev[rev[-1]])
    return [cell for (cell, _v) in reversed(rev)]


def _paths_legal(
    req: RouteRequest,
    free: set[tuple[int, int, int]],
    paths: list[list[tuple[int, int, int]] | None],
) -> bool:
    """Independently verify that one path per net obeys every rule."""
    if any(p is None for p in paths):
        return False
    occupied: dict[tuple[int, int, int], int] = {}
    for k, (net, path) in enumerate(zip(req.nets, paths)):
        assert path is not None
        src = (net.pads[0].x, net.pads[0].y, net.pads[0].layer)
        dst = (net.pads[1].x, net.pads[1].y, net.pads[1].layer)
        if path[0] != src or path[-1] != dst:
            return False
        if len(set(path)) != len(path):  # no repeated cell
            return False
        layers_at: dict[tuple[int, int], set[int]] = defaultdict(set)
        for cell in path:
            if cell not in free:
                return False
            layers_at[(cell[0], cell[1])].add(cell[2])
        for a, b in zip(path, path[1:]):
            if a[2] == b[2]:
                if abs(a[0] - b[0]) + abs(a[1] - b[1]) != 1:
                    return False
            elif a[:2] != b[:2]:  # layer change must stay on the coordinate
                return False
        n_vias = sum(1 for ls in layers_at.values() if len(ls) == 2)
        if n_vias > net.via_limit:
            return False
        for cell in path:
            if cell in occupied:
                return False
            occupied[cell] = k
    for (x, y, layer), k in occupied.items():
        for dx, dy in _ORTHO:
            other = occupied.get((x + dx, y + dy, layer))
            if other is not None and other != k:
                return False
    return True


def solve_board(req: RouteRequest) -> RouteResponse:
    started = time.monotonic()
    w, h = req.width, req.height
    blocked = {(c.x, c.y, c.layer) for c in req.blocked}
    free = {
        (x, y, layer)
        for layer in (0, 1)
        for x in range(w)
        for y in range(h)
    } - blocked
    # coordinates where a via is physically possible (both layers free)
    via_sites = [
        (x, y)
        for x in range(w)
        for y in range(h)
        if (x, y, 0) in free and (x, y, 1) in free
    ]

    model = cp_model.CpModel()

    use: dict[tuple[int, tuple[int, int, int]], cp_model.IntVar] = {}
    # per net: list of (u, v, var) directed arcs actually carrying the flow
    net_arcs: list[list[tuple[tuple[int, int, int], tuple[int, int, int], cp_model.IntVar]]] = []
    net_via_dicts: list[dict[tuple[int, int], cp_model.IntVar]] = []
    net_move_vars_all: list[cp_model.IntVar] = []
    net_via_vars_all: list[cp_model.IntVar] = []

    for k, net in enumerate(req.nets):
        src = (net.pads[0].x, net.pads[0].y, net.pads[0].layer)
        dst = (net.pads[1].x, net.pads[1].y, net.pads[1].layer)

        for cell in free:
            use[k, cell] = model.new_bool_var(f"use_{k}_{cell[0]}_{cell[1]}_{cell[2]}")
        model.add(use[k, src] == 1)
        model.add(use[k, dst] == 1)

        out_arcs: dict[tuple[int, int, int], list[cp_model.IntVar]] = defaultdict(list)
        in_arcs: dict[tuple[int, int, int], list[cp_model.IntVar]] = defaultdict(list)
        arcs: list[tuple[tuple[int, int, int], tuple[int, int, int], cp_model.IntVar]] = []
        net_move_vars: list[cp_model.IntVar] = []

        # same-layer orthogonal moves
        for (x, y, layer) in free:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (x + dx, y + dy, layer)
                if nb in free:
                    a = model.new_bool_var(
                        f"mv_{k}_{x}_{y}_{layer}_{nb[0]}_{nb[1]}"
                    )
                    out_arcs[(x, y, layer)].append(a)
                    in_arcs[nb].append(a)
                    arcs.append(((x, y, layer), nb, a))
                    net_move_vars.append(a)

        # vias at this coordinate (both layers must be free)
        net_via_vars: list[cp_model.IntVar] = []
        via_dict: dict[tuple[int, int], cp_model.IntVar] = {}
        for (x, y) in via_sites:
            v = model.new_bool_var(f"via_{k}_{x}_{y}")
            lo, hi = (x, y, 0), (x, y, 1)
            a_up = model.new_bool_var(f"vup_{k}_{x}_{y}")
            a_dn = model.new_bool_var(f"vdn_{k}_{x}_{y}")
            model.add(a_up <= v)
            model.add(a_dn <= v)
            out_arcs[lo].append(a_up)
            in_arcs[hi].append(a_up)
            out_arcs[hi].append(a_dn)
            in_arcs[lo].append(a_dn)
            arcs.append((lo, hi, a_up))
            arcs.append((hi, lo, a_dn))
            # occupying both layers at (x, y) means there is a via there
            model.add(v >= use[k, lo] + use[k, hi] - 1)
            net_via_vars.append(v)
            via_dict[(x, y)] = v

        # unit flow from src to dst; every node is used at most once
        for cell in free:
            if cell == src:
                model.add(sum(out_arcs[cell]) == 1)
                model.add(sum(in_arcs[cell]) == 0)
            elif cell == dst:
                model.add(sum(in_arcs[cell]) == 1)
                model.add(sum(out_arcs[cell]) == 0)
            else:
                model.add(sum(in_arcs[cell]) == sum(out_arcs[cell]))
                model.add(sum(in_arcs[cell]) == use[k, cell])

        net_arcs.append(arcs)
        net_via_dicts.append(via_dict)

        # Objective accounting per net.  The lower bounds are encoded in
        # the variable DOMAINS (not as constraints) so the solver's root
        # bound immediately reflects them; the ">=" channelling is kept
        # as an inequality so objective expansion cannot substitute the
        # variables away (minimisation makes them tight at the optimum).
        manhattan = abs(src[0] - dst[0]) + abs(src[1] - dst[1])
        moves_k = model.new_int_var(manhattan, len(free), f"moves_{k}")
        model.add(moves_k >= sum(net_move_vars))
        min_vias = 1 if src[2] != dst[2] else 0
        via_ub = min(net.via_limit, len(via_sites))
        if min_vias > via_ub:
            # via budget below the mandatory minimum: trivially infeasible
            model.add(0 >= 1)
        vias_k = model.new_int_var(min_vias, max(min_vias, via_ub), f"vias_{k}")
        model.add(vias_k >= sum(net_via_vars))
        net_move_vars_all.append(moves_k)
        net_via_vars_all.append(vias_k)

    # separation between different nets on the same layer:
    # no shared cell and no orthogonally adjacent cells
    n_nets = len(req.nets)
    for layer in (0, 1):
        for x in range(w):
            for y in range(h):
                cell = (x, y, layer)
                if cell not in free:
                    continue
                for k1 in range(n_nets):
                    for k2 in range(k1 + 1, n_nets):
                        model.add(use[k1, cell] + use[k2, cell] <= 1)
                for dx, dy in ((1, 0), (0, 1)):
                    nb = (x + dx, y + dy, layer)
                    if nb in free:
                        for k1 in range(n_nets):
                            for k2 in range(n_nets):
                                if k1 != k2:
                                    model.add(use[k1, cell] + use[k2, nb] <= 1)

    total_vias = sum(net_via_vars_all)
    total_moves = sum(net_move_vars_all)
    model.minimize(VIA_WEIGHT * total_vias + total_moves)

    # seed the search with a fast heuristic routing (a hint only: the
    # solver still proves optimality/infeasibility by itself, and if the
    # heuristic fails the solve is simply started without it)
    hint_paths = _heuristic_paths(req, free, set(via_sites))
    for k, path in enumerate(hint_paths):
        if path is None:
            continue
        path_set = set(path)
        for cell in free:
            model.add_hint(use[k, cell], 1 if cell in path_set else 0)
        used_arcs = set(zip(path, path[1:]))
        for u, v, a in net_arcs[k]:
            model.add_hint(a, 1 if (u, v) in used_arcs else 0)
        for (x, y), var in net_via_dicts[k].items():
            both = (x, y, 0) in path_set and (x, y, 1) in path_set
            model.add_hint(var, 1 if both else 0)
        model.add_hint(
            net_move_vars_all[k],
            sum(1 for a, b in zip(path, path[1:]) if a[2] == b[2]),
        )
        model.add_hint(
            net_via_vars_all[k],
            sum(
                1
                for (x, y) in {(px, py) for (px, py, _l) in path_set}
                if (x, y, 0) in path_set and (x, y, 1) in path_set
            ),
        )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = _time_limit()
    status = solver.solve(model)
    elapsed = time.monotonic() - started

    if status == cp_model.OPTIMAL:
        status_str = STATUS_OPTIMAL
    elif status == cp_model.FEASIBLE:
        status_str = STATUS_FEASIBLE
    elif status == cp_model.INFEASIBLE:
        status_str = STATUS_INFEASIBLE
    else:
        status_str = STATUS_UNKNOWN

    paths: list[list[tuple[int, int, int]] | None] | None = None
    if status_str in (STATUS_OPTIMAL, STATUS_FEASIBLE):
        # extract one simple path per net by walking src -> dst over used arcs
        paths = []
        for k, net in enumerate(req.nets):
            src = (net.pads[0].x, net.pads[0].y, net.pads[0].layer)
            dst = (net.pads[1].x, net.pads[1].y, net.pads[1].layer)
            nxt: dict[tuple[int, int, int], tuple[int, int, int]] = {}
            for u, v, a in net_arcs[k]:
                if solver.value(a):
                    nxt[u] = v  # out-degree is at most 1 on the s-t path
            path = [src]
            cur = src
            while cur != dst:
                cur = nxt[cur]
                path.append(cur)
            paths.append(path)
    elif status_str == STATUS_UNKNOWN and _paths_legal(req, free, hint_paths):
        # the solver ran out of time before its first solution, but the
        # heuristic seed is a verified feasible routing: report it as a
        # (not proved optimal) feasible answer
        status_str = STATUS_FEASIBLE
        paths = hint_paths

    if paths is None:
        return RouteResponse(
            status=status_str,
            solve_time_seconds=round(elapsed, 3),
        )

    results: list[NetResult] = []
    sum_vias = 0
    sum_moves = 0
    for k, (net, path) in enumerate(zip(req.nets, paths)):
        assert path is not None
        layers_at: dict[tuple[int, int], set[int]] = defaultdict(set)
        for (x, y, layer) in path:
            layers_at[(x, y)].add(layer)
        vias = sorted(xy for xy, ls in layers_at.items() if len(ls) == 2)
        moves = sum(1 for a, b in zip(path, path[1:]) if a[2] == b[2])

        sum_vias += len(vias)
        sum_moves += moves
        results.append(
            NetResult(
                name=net.name or f"N{k + 1}",
                path=[PathCell(x=x, y=y, layer=layer) for (x, y, layer) in path],
                vias=[ViaPoint(x=x, y=y) for (x, y) in vias],
                via_count=len(vias),
                move_count=moves,
            )
        )

    return RouteResponse(
        status=status_str,
        solve_time_seconds=round(elapsed, 3),
        total_vias=sum_vias,
        total_moves=sum_moves,
        nets=results,
    )
