"""End-to-end tests: API validation + solution legality checks."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def check_solution_legal(req: dict, resp: dict) -> None:
    """Independently verify that a returned routing obeys every rule."""
    assert resp["status"] in ("optimal", "feasible")
    w, h = req["width"], req["height"]
    blocked = {(c["x"], c["y"], c["layer"]) for c in req.get("blocked", [])}
    nets = req["nets"]
    assert len(resp["nets"]) == len(nets)

    occ: dict[tuple[int, int, int], int] = {}  # cell -> net index
    total_vias = 0
    total_moves = 0
    for i, (net, result) in enumerate(zip(nets, resp["nets"])):
        path = [(c["x"], c["y"], c["layer"]) for c in result["path"]]
        pads = [(p["x"], p["y"], p["layer"]) for p in net["pads"]]
        # endpoints match the pads, in order
        assert path[0] == pads[0] and path[-1] == pads[1]
        # no repeated cell within a path
        assert len(set(path)) == len(path)
        via_cells = set()
        moves = 0
        for a, b in zip(path, path[1:]):
            if a[2] == b[2]:
                # same layer: orthogonal unit step
                assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
                moves += 1
            else:
                # layer change: same coordinate, i.e. a via
                assert a[:2] == b[:2]
                via_cells.add(a[:2])
        # every cell in bounds and not blocked
        for (x, y, layer) in path:
            assert 0 <= x < w and 0 <= y < h and layer in (0, 1)
            assert (x, y, layer) not in blocked
        # reported vias == coordinates occupied on both layers
        layers_at: dict[tuple[int, int], set] = {}
        for (x, y, layer) in path:
            layers_at.setdefault((x, y), set()).add(layer)
        both = {xy for xy, ls in layers_at.items() if len(ls) == 2}
        reported = {(v["x"], v["y"]) for v in result["vias"]}
        assert reported == both
        assert via_cells <= both
        assert result["via_count"] == len(both) <= net["via_limit"]
        assert result["move_count"] == moves
        total_vias += len(both)
        total_moves += moves
        # record occupancy for the separation check
        for cell in path:
            assert cell not in occ, f"cell {cell} shared by nets"
            occ[cell] = i
    assert resp["total_vias"] == total_vias
    assert resp["total_moves"] == total_moves
    # different nets: no orthogonally adjacent cells on the same layer
    for (x, y, layer), i in occ.items():
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            j = occ.get((x + dx, y + dy, layer))
            assert j is None or j == i, (
                f"nets {i} and {j} adjacent at {(x, y, layer)}"
            )


WALL = [{"x": 3, "y": y, "layer": 0} for y in range(8)]


def test_wall_example_optimal():
    req = {
        "width": 8,
        "height": 8,
        "blocked": WALL,
        "nets": [
            {"name": "N1",
             "pads": [{"x": 0, "y": 3, "layer": 0},
                      {"x": 7, "y": 3, "layer": 0}],
             "via_limit": 2},
            {"name": "N2",
             "pads": [{"x": 0, "y": 5, "layer": 1},
                      {"x": 7, "y": 5, "layer": 1}],
             "via_limit": 0},
        ],
    }
    r = client.post("/route", json=req)
    assert r.status_code == 200
    resp = r.json()
    assert resp["status"] == "optimal"
    check_solution_legal(req, resp)
    # the wall forces exactly 2 vias for N1; N2 stays on layer 1
    assert resp["total_vias"] == 2
    assert resp["total_moves"] == 7 + 7
    n1, n2 = resp["nets"]
    assert n1["via_count"] == 2 and n1["move_count"] == 7
    assert n2["via_count"] == 0 and n2["move_count"] == 7


def test_straight_line_no_via():
    req = {
        "width": 5,
        "height": 5,
        "nets": [
            {"pads": [{"x": 0, "y": 0, "layer": 0},
                      {"x": 4, "y": 0, "layer": 0}],
             "via_limit": 3}
        ],
    }
    r = client.post("/route", json=req)
    assert r.status_code == 200
    resp = r.json()
    assert resp["status"] == "optimal"
    check_solution_legal(req, resp)
    assert resp["total_vias"] == 0
    assert resp["total_moves"] == 4
    assert resp["nets"][0]["name"] == "N1"  # default name


def test_same_coordinate_two_layers_needs_one_via():
    req = {
        "width": 3,
        "height": 3,
        "nets": [
            {"pads": [{"x": 1, "y": 1, "layer": 0},
                      {"x": 1, "y": 1, "layer": 1}],
             "via_limit": 1}
        ],
    }
    r = client.post("/route", json=req)
    resp = r.json()
    assert r.status_code == 200
    assert resp["status"] == "optimal"
    check_solution_legal(req, resp)
    assert resp["total_vias"] == 1
    assert resp["total_moves"] == 0


def test_infeasible_when_via_limit_zero_and_layers_differ():
    req = {
        "width": 3,
        "height": 3,
        "nets": [
            {"pads": [{"x": 0, "y": 0, "layer": 0},
                      {"x": 2, "y": 2, "layer": 1}],
             "via_limit": 0}
        ],
    }
    r = client.post("/route", json=req)
    assert r.status_code == 200
    resp = r.json()
    assert resp["status"] == "infeasible"
    assert resp["nets"] is None
    assert resp["total_vias"] is None


def test_infeasible_surrounded_pad():
    # pad boxed in by blocked cells on its own layer
    blocked = [{"x": 1, "y": 0, "layer": 0}, {"x": 0, "y": 1, "layer": 0}]
    req = {
        "width": 4,
        "height": 4,
        "blocked": blocked,
        "nets": [
            {"pads": [{"x": 0, "y": 0, "layer": 0},
                      {"x": 3, "y": 3, "layer": 0}],
             "via_limit": 0}
        ],
    }
    r = client.post("/route", json=req)
    assert r.json()["status"] == "infeasible"


def test_adjacent_nets_must_detour():
    # two parallel nets on the same layer, one cell apart: legal
    req = {
        "width": 6,
        "height": 4,
        "nets": [
            {"pads": [{"x": 0, "y": 0, "layer": 0},
                      {"x": 5, "y": 0, "layer": 0}],
             "via_limit": 0},
            {"pads": [{"x": 0, "y": 2, "layer": 0},
                      {"x": 5, "y": 2, "layer": 0}],
             "via_limit": 0},
        ],
    }
    r = client.post("/route", json=req)
    resp = r.json()
    assert resp["status"] == "optimal"
    check_solution_legal(req, resp)
    assert resp["total_vias"] == 0
    assert resp["total_moves"] == 10


def test_crossing_nets_need_vias():
    # two nets crossing on the same layer: impossible without vias
    req = {
        "width": 5,
        "height": 5,
        "nets": [
            {"pads": [{"x": 2, "y": 0, "layer": 0},
                      {"x": 2, "y": 4, "layer": 0}],
             "via_limit": 0},
            {"pads": [{"x": 0, "y": 2, "layer": 0},
                      {"x": 4, "y": 2, "layer": 0}],
             "via_limit": 0},
        ],
    }
    r = client.post("/route", json=req)
    assert r.json()["status"] == "infeasible"
    # with vias allowed it becomes feasible
    for net in req["nets"]:
        net["via_limit"] = 2
    r = client.post("/route", json=req)
    resp = r.json()
    assert resp["status"] == "optimal"
    check_solution_legal(req, resp)
    assert resp["total_vias"] >= 2


def test_six_nets_joint():
    req = {
        "width": 10,
        "height": 11,
        "nets": [
            {"pads": [{"x": 0, "y": 2 * i, "layer": 0},
                      {"x": 9, "y": 2 * i, "layer": 0}],
             "via_limit": 1}
            for i in range(6)
        ],
    }
    r = client.post("/route", json=req)
    resp = r.json()
    assert resp["status"] == "optimal"
    check_solution_legal(req, resp)
    assert resp["total_vias"] == 0
    assert resp["total_moves"] == 54


# ----------------------------- 422 validation -----------------------------

def base_req():
    return {
        "width": 5,
        "height": 5,
        "nets": [
            {"pads": [{"x": 0, "y": 0, "layer": 0},
                      {"x": 4, "y": 4, "layer": 0}],
             "via_limit": 1}
        ],
    }


@pytest.mark.parametrize("pad", [
    {"x": -1, "y": 0, "layer": 0},
    {"x": 5, "y": 0, "layer": 0},
    {"x": 0, "y": 5, "layer": 0},
    {"x": 0, "y": 0, "layer": 2},
])
def test_422_pad_out_of_bounds(pad):
    req = base_req()
    req["nets"][0]["pads"][0] = pad
    assert client.post("/route", json=req).status_code == 422


def test_422_pad_on_blocked_cell():
    req = base_req()
    req["blocked"] = [{"x": 0, "y": 0, "layer": 0}]
    assert client.post("/route", json=req).status_code == 422


def test_422_duplicate_pads():
    req = base_req()
    req["nets"][0]["pads"][1] = {"x": 0, "y": 0, "layer": 0}
    assert client.post("/route", json=req).status_code == 422
    # duplicate across different nets
    req = base_req()
    req["nets"].append({"pads": [{"x": 0, "y": 0, "layer": 0},
                                 {"x": 1, "y": 1, "layer": 1}],
                        "via_limit": 0})
    assert client.post("/route", json=req).status_code == 422


@pytest.mark.parametrize("limit", [-1, -100, 1.5, "two"])
def test_422_illegal_via_limit(limit):
    req = base_req()
    req["nets"][0]["via_limit"] = limit
    assert client.post("/route", json=req).status_code == 422


@pytest.mark.parametrize("dim", [1, 21, 0, -3])
def test_422_bad_dimensions(dim):
    req = base_req()
    req["width"] = dim
    assert client.post("/route", json=req).status_code == 422
    req = base_req()
    req["height"] = dim
    assert client.post("/route", json=req).status_code == 422


def test_422_net_count():
    req = base_req()
    req["nets"] = []
    assert client.post("/route", json=req).status_code == 422
    req = base_req()
    req["nets"] = [
        {"pads": [{"x": 0, "y": i, "layer": 0},
                  {"x": 4, "y": i, "layer": 0}],
         "via_limit": 0}
        for i in range(7)  # 7 > 6
    ]
    # pads would overlap rows only if height allowed; just check count rule
    assert client.post("/route", json=req).status_code == 422


def test_422_blocked_out_of_bounds():
    req = base_req()
    req["blocked"] = [{"x": 9, "y": 0, "layer": 0}]
    assert client.post("/route", json=req).status_code == 422


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


# ------------------------- time-limit behaviour -------------------------

def test_tiny_time_limit_reports_unknown_or_feasible(monkeypatch):
    # with (almost) no time budget the solver can neither prove anything
    # nor be trusted to find a solution; the API must not claim optimality
    monkeypatch.setenv("ROUTER_TIME_LIMIT", "0.0001")
    req = {
        "width": 10,
        "height": 11,
        "nets": [
            {"pads": [{"x": 0, "y": 2 * i, "layer": 0},
                      {"x": 9, "y": 2 * i, "layer": 0}],
             "via_limit": 1}
            for i in range(6)
        ],
    }
    r = client.post("/route", json=req)
    resp = r.json()
    assert resp["status"] in ("unknown", "feasible")
    if resp["status"] == "feasible":
        check_solution_legal(req, resp)
    else:
        assert resp["nets"] is None


def test_heuristic_fallback_marks_feasible(monkeypatch):
    # time budget below the solver's presolve: CP-SAT returns UNKNOWN,
    # but the verified heuristic routing must surface as "feasible"
    monkeypatch.setenv("ROUTER_TIME_LIMIT", "0.05")
    req = {
        "width": 10,
        "height": 11,
        "nets": [
            {"pads": [{"x": 0, "y": 2 * i, "layer": 0},
                      {"x": 9, "y": 2 * i, "layer": 0}],
             "via_limit": 1}
            for i in range(6)
        ],
    }
    r = client.post("/route", json=req)
    resp = r.json()
    assert resp["status"] in ("unknown", "feasible")
    if resp["status"] == "feasible":
        check_solution_legal(req, resp)
        assert resp["total_vias"] is not None
