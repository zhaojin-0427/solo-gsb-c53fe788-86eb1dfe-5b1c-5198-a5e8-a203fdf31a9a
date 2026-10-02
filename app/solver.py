"""Joint two-layer grid router built on OR-Tools CP-SAT.

Model overview
--------------
* One boolean ``use[k, node]`` per net and per routable cell ``(layer, x, y)``.
* One boolean per undirected edge (same-layer orthogonal move or via) and net,
  plus two directed arc booleans per edge so the used edges of a net form a
  *directed* path from ``pads[0]`` to ``pads[1]``:
    - ``pads[0]``: out-degree 1, in-degree 0, position 0
    - ``pads[1]``: in-degree 1, out-degree 0
    - every other used cell: in-degree == out-degree == 1
  MTZ-style position constraints (positions strictly increase along used arcs)
  forbid detached cycles, so every reported path is simple (no repeated cells).
* A via at ``(x, y)`` is an edge between ``(0, x, y)`` and ``(1, x, y)``; using
  it occupies both layers at that coordinate.
* Different nets may neither share a cell on the same layer nor occupy
  orthogonally adjacent cells on the same layer (pads included).
* Lexicographic objective via a single weighted sum: total vias first
  (the weight strictly exceeds any achievable move count), then total
  same-layer moves.
* Before solving, a fast sequential router (per-net Dijkstra over
  ``(layer, x, y, vias_used)`` that avoids cells used by previously routed
  nets and their same-layer neighbours) produces a feasible routing which is
  handed to CP-SAT as a search hint / initial incumbent.
"""
from __future__ import annotations

import heapq

from ortools.sat.python import cp_model

from .schemas import RouteRequest

TIME_LIMIT_SECONDS = 10.0

_MOVE_DIRS = ((1, 0), (0, 1))
_ALL_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


# --------------------------------------------------------------------------- hints
def _route_net_greedily(req, net, blocked, used, width, height):
    """Shortest (fewest vias, then fewest moves) path for one net.

    ``used[l]`` holds the cells already occupied by other nets; a cell is
    routable when it is neither blocked, nor used, nor orthogonally adjacent
    to a used cell on the same layer. Returns the node path or ``None``.
    """

    def cell_free(layer, x, y):
        if (x, y) in blocked[layer] or (x, y) in used[layer]:
            return False
        return all(
            (x + dx, y + dy) not in used[layer] for dx, dy in _ALL_DIRS
        )

    a_pad = (net.pads[0].layer, net.pads[0].x, net.pads[0].y)
    b_pad = (net.pads[1].layer, net.pads[1].x, net.pads[1].y)

    # The pads themselves must not touch cells already taken by other nets.
    if not cell_free(*a_pad) or not cell_free(*b_pad):
        return None

    start = (*a_pad, 0)  # (layer, x, y, vias_used)
    dist = {start: (0, 0)}
    prev = {}
    pq = [((0, 0), start)]
    goal = None
    while pq:
        cost, state = heapq.heappop(pq)
        if cost != dist.get(state):
            continue
        layer, x, y, vias = state
        if (layer, x, y) == b_pad:
            goal = state
            break
        for dx, dy in _ALL_DIRS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            if not cell_free(layer, nx, ny):
                continue
            nxt = (layer, nx, ny, vias)
            ncost = (vias, cost[1] + 1)
            if ncost < dist.get(nxt, (1 << 30, 1 << 30)):
                dist[nxt] = ncost
                prev[nxt] = state
                heapq.heappush(pq, (ncost, nxt))
        if vias < net.via_limit:
            other = 1 - layer
            if cell_free(other, x, y):
                nxt = (other, x, y, vias + 1)
                ncost = (vias + 1, cost[1])
                if ncost < dist.get(nxt, (1 << 30, 1 << 30)):
                    dist[nxt] = ncost
                    prev[nxt] = state
                    heapq.heappush(pq, (ncost, nxt))

    if goal is None:
        return None
    path = []
    state = goal
    while True:
        path.append((state[0], state[1], state[2]))
        if state == start:
            break
        state = prev[state]
    path.reverse()
    return path


def _sequential_routing(req, blocked, width, height):
    """Route nets one after another; returns ``{net_index: path}``.

    Tries the given net order and a longest-first order, keeping whichever
    attempt routes more nets. The result is only used as a solver hint.
    """
    nets = req.nets

    def attempt(order):
        used = [set(), set()]
        # Reserve every pad cell up front so routed nets never cross the pads
        # of nets that have not been routed yet.
        for net in nets:
            for pad in net.pads:
                used[pad.layer].add((pad.x, pad.y))
        paths = {}
        for k in order:
            own_pads = {(p.layer, p.x, p.y) for p in nets[k].pads}
            for layer, x, y in own_pads:
                used[layer].discard((x, y))
            path = _route_net_greedily(req, nets[k], blocked, used, width, height)
            if path is None:
                # A net that could not be routed still occupies its pad cells.
                for layer, x, y in own_pads:
                    used[layer].add((x, y))
            else:
                paths[k] = path
                for layer, x, y in path:
                    used[layer].add((x, y))
        return paths

    def manhattan(net):
        (a, b) = net.pads
        return abs(a.x - b.x) + abs(a.y - b.y)

    best = attempt(range(len(nets)))
    if len(best) < len(nets):
        by_length = attempt(
            sorted(range(len(nets)), key=lambda k: -manhattan(nets[k]))
        )
        if len(by_length) > len(best):
            best = by_length
    return best


# --------------------------------------------------------------------------- solver
def solve(req: RouteRequest) -> dict:
    """Solve one routing request and return a RouteResponse-shaped dict."""
    width, height = req.grid.width, req.grid.height
    blocked = (
        set(map(tuple, req.blocked.layer0)),
        set(map(tuple, req.blocked.layer1)),
    )

    # ------------------------------------------------------------------ graph
    nodes = [
        (layer, x, y)
        for layer in (0, 1)
        for x in range(width)
        for y in range(height)
        if (x, y) not in blocked[layer]
    ]
    node_set = frozenset(nodes)

    move_edges: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
    for layer, x, y in nodes:
        for dx, dy in _MOVE_DIRS:
            nb = (layer, x + dx, y + dy)
            if nb in node_set:
                move_edges.append(((layer, x, y), nb))

    via_edges: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
    for x in range(width):
        for y in range(height):
            n0, n1 = (0, x, y), (1, x, y)
            if n0 in node_set and n1 in node_set:
                via_edges.append((n0, n1))

    edges = move_edges + via_edges
    n_move = len(move_edges)

    adj: dict[tuple[int, int, int], list[tuple[int, tuple[int, int, int]]]] = {
        n: [] for n in nodes
    }
    for ei, (u, v) in enumerate(edges):
        adj[u].append((ei, v))
        adj[v].append((ei, u))

    # ------------------------------------------------------------------ model
    model = cp_model.CpModel()
    n_nodes = len(nodes)
    mtz_big_m = n_nodes + 1
    nets = req.nets
    n_nets = len(nets)

    use: dict = {}
    pos: dict = {}
    edge_used: dict = {}
    arc: dict = {}

    for k, net in enumerate(nets):
        for n in nodes:
            use[k, n] = model.new_bool_var(f"use_{k}_{n}")
            pos[k, n] = model.new_int_var(0, n_nodes, f"pos_{k}_{n}")
        for ei, (u, v) in enumerate(edges):
            e = model.new_bool_var(f"e_{k}_{ei}")
            fwd = model.new_bool_var(f"fwd_{k}_{ei}")
            bwd = model.new_bool_var(f"bwd_{k}_{ei}")
            edge_used[k, ei] = e
            arc[k, u, v] = fwd
            arc[k, v, u] = bwd
            model.add(fwd + bwd == e)
            # MTZ: positions strictly increase along used directed arcs.
            model.add(pos[k, v] >= pos[k, u] + 1 - mtz_big_m * (1 - fwd))
            model.add(pos[k, u] >= pos[k, v] + 1 - mtz_big_m * (1 - bwd))

        a_pad = (net.pads[0].layer, net.pads[0].x, net.pads[0].y)
        b_pad = (net.pads[1].layer, net.pads[1].x, net.pads[1].y)
        for n in nodes:
            in_deg = sum(arc[k, m, n] for _, m in adj[n])
            out_deg = sum(arc[k, n, m] for _, m in adj[n])
            if n == a_pad:
                model.add(in_deg == 0)
                model.add(out_deg == 1)
                model.add(use[k, n] == 1)
                model.add(pos[k, n] == 0)
            elif n == b_pad:
                model.add(in_deg == 1)
                model.add(out_deg == 0)
                model.add(use[k, n] == 1)
            else:
                model.add(in_deg == out_deg)
                model.add(use[k, n] == in_deg)

    # Different nets: no shared cell and no orthogonally adjacent cells on the
    # same layer (pads included, since pads occupy their cell).
    for n in nodes:
        model.add(sum(use[k, n] for k in range(n_nets)) <= 1)
    for layer in (0, 1):
        for x in range(width):
            for y in range(height):
                if (x, y) in blocked[layer]:
                    continue
                n1 = (layer, x, y)
                for dx, dy in _MOVE_DIRS:
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < width and 0 <= ny < height):
                        continue
                    if (nx, ny) in blocked[layer]:
                        continue
                    n2 = (layer, nx, ny)
                    for k1 in range(n_nets):
                        for k2 in range(k1 + 1, n_nets):
                            model.add(use[k1, n1] + use[k2, n2] <= 1)
                            model.add(use[k2, n1] + use[k1, n2] <= 1)

    # Per-net via budgets.
    for k, net in enumerate(nets):
        model.add(
            sum(edge_used[k, n_move + i] for i in range(len(via_edges)))
            <= net.via_limit
        )

    # Lexicographic objective as a weighted sum: the via weight strictly
    # exceeds any achievable total move count (all nets together use at most
    # n_nodes cells, hence fewer than n_nodes moves), so fewer vias always
    # dominates regardless of moves.
    total_vias = sum(
        edge_used[k, n_move + i] for k in range(n_nets) for i in range(len(via_edges))
    )
    total_moves = sum(edge_used[k, ei] for k in range(n_nets) for ei in range(n_move))
    via_weight = n_nodes + 1
    model.minimize(via_weight * total_vias + total_moves)

    # ------------------------------------------------------- construction hint
    hint_paths = _sequential_routing(req, blocked, width, height)
    for k, path in hint_paths.items():
        on_path = set(path)
        arc_steps = set(zip(path, path[1:]))
        for n in nodes:
            model.add_hint(use[k, n], 1 if n in on_path else 0)
        for i, n in enumerate(path):
            model.add_hint(pos[k, n], i)
        for ei, (u, v) in enumerate(edges):
            fwd_hint = 1 if (u, v) in arc_steps else 0
            bwd_hint = 1 if (v, u) in arc_steps else 0
            model.add_hint(arc[k, u, v], fwd_hint)
            model.add_hint(arc[k, v, u], bwd_hint)
            model.add_hint(edge_used[k, ei], 1 if (fwd_hint or bwd_hint) else 0)

    # ------------------------------------------------------------------ solve
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = TIME_LIMIT_SECONDS
    status = solver.solve(model)
    wall = round(solver.wall_time, 6)

    base = {"solve_time_seconds": wall, "time_limit_seconds": TIME_LIMIT_SECONDS}

    if status == cp_model.INFEASIBLE:
        return {
            **base,
            "status": "infeasible",
            "proven": True,
            "detail": "proven infeasible: no routing satisfies all constraints",
            "objectives": None,
            "nets": None,
        }
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            **base,
            "status": "unknown",
            "proven": False,
            "detail": (
                f"no feasible solution found within the "
                f"{TIME_LIMIT_SECONDS:.0f}s time limit"
            ),
            "objectives": None,
            "nets": None,
        }

    # ------------------------------------------------------- extract solution
    boolean = solver.boolean_value
    routed_nets = []
    tot_vias = 0
    tot_moves = 0
    for k, net in enumerate(nets):
        a_pad = (net.pads[0].layer, net.pads[0].x, net.pads[0].y)
        b_pad = (net.pads[1].layer, net.pads[1].x, net.pads[1].y)

        # Walk the directed path from pads[0] to pads[1]. The model guarantees
        # a single simple directed path between the pads, so this terminates.
        path = [a_pad]
        cur = a_pad
        while cur != b_pad:
            nxt = next(
                (m for _, m in adj[cur] if boolean(arc[k, cur, m])),
                None,
            )
            if nxt is None:  # pragma: no cover - defensive, cannot happen
                raise RuntimeError("internal error: broken path in solver output")
            path.append(nxt)
            cur = nxt

        vias = [
            [n0[1], n0[2]]
            for i, (n0, _n1) in enumerate(via_edges)
            if boolean(edge_used[k, n_move + i])
        ]
        moves = sum(1 for ei in range(n_move) if boolean(edge_used[k, ei]))
        tot_vias += len(vias)
        tot_moves += moves
        routed_nets.append(
            {
                "name": net.name or f"N{k + 1}",
                "path": [
                    {"layer": layer, "x": x, "y": y} for layer, x, y in path
                ],
                "vias": vias,
                "via_count": len(vias),
                "move_count": moves,
            }
        )

    proven = status == cp_model.OPTIMAL
    return {
        **base,
        "status": "optimal" if proven else "feasible",
        "proven": proven,
        "detail": (
            "proven optimal: total vias minimized first, then total moves"
            if proven
            else (
                f"best solution found within the {TIME_LIMIT_SECONDS:.0f}s "
                f"time limit; optimality not proven"
            )
        ),
        "objectives": {"total_vias": tot_vias, "total_moves": tot_moves},
        "nets": routed_nets,
    }
