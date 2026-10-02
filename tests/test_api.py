"""End-to-end tests for the two-layer grid routing service."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# --------------------------------------------------------------------------- helpers
def verify_solution(req: dict, resp: dict) -> None:
    """Check every routing rule of the problem statement against a response."""
    assert resp["status"] in ("optimal", "feasible")
    nets = resp["nets"]
    assert len(nets) == len(req["nets"])

    blocked = [
        {tuple(c) for c in req.get("blocked", {}).get("layer0", [])},
        {tuple(c) for c in req.get("blocked", {}).get("layer1", [])},
    ]
    w, h = req["grid"]["width"], req["grid"]["height"]

    occupied: list[dict[tuple[int, int], int]] = [{}, {}]  # per layer: cell -> net idx
    for i, net in enumerate(nets):
        path = [(p["layer"], p["x"], p["y"]) for p in net["path"]]
        pads = req["nets"][i]["pads"]

        # endpoints match the requested pads, in order
        assert path[0] == (pads[0]["layer"], pads[0]["x"], pads[0]["y"])
        assert path[-1] == (pads[1]["layer"], pads[1]["x"], pads[1]["y"])

        # simple path: no cell occupied twice by the same net
        assert len(set(path)) == len(path)

        vias: set[tuple[int, int]] = set()
        moves = 0
        for a, b in zip(path, path[1:]):
            if a[0] == b[0]:
                # same-layer orthogonal move
                assert abs(a[1] - b[1]) + abs(a[2] - b[2]) == 1
                moves += 1
            else:
                # via: same coordinate, other layer
                assert {a[0], b[0]} == {0, 1}
                assert a[1:] == b[1:]
                vias.add((a[1], a[2]))

        # reported vias / counts are consistent with the path
        assert vias == {tuple(v) for v in net["vias"]}
        assert net["via_count"] == len(vias)
        assert net["move_count"] == moves
        assert net["via_count"] <= req["nets"][i]["via_limit"]

        for layer, x, y in path:
            assert 0 <= x < w and 0 <= y < h
            assert (x, y) not in blocked[layer], "path crosses a blocked cell"
            cell = (x, y)
            assert cell not in occupied[layer], "two nets share a cell"
            occupied[layer][cell] = i

    # different nets: no orthogonally adjacent cells on the same layer
    for layer in (0, 1):
        for (x, y), owner in occupied[layer].items():
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                other = occupied[layer].get((x + dx, y + dy))
                assert other is None or other == owner

    # objectives add up
    assert resp["objectives"]["total_vias"] == sum(n["via_count"] for n in nets)
    assert resp["objectives"]["total_moves"] == sum(n["move_count"] for n in nets)


def route(req: dict) -> dict:
    r = client.post("/route", json=req)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- meta
def test_health_and_index():
    assert client.get("/health").json() == {"status": "ok"}
    body = client.get("/").json()
    assert body["route_endpoint"] == "POST /route"


# --------------------------------------------------------------------------- solving
def test_single_net_straight_line():
    req = {
        "grid": {"width": 4, "height": 2},
        "nets": [
            {"pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 0, "x": 3, "y": 0}],
             "via_limit": 0}
        ],
    }
    resp = route(req)
    verify_solution(req, resp)
    assert resp["status"] == "optimal" and resp["proven"] is True
    assert resp["objectives"] == {"total_vias": 0, "total_moves": 3}


def test_via_connects_layers_same_cell():
    req = {
        "grid": {"width": 3, "height": 3},
        "nets": [
            {"name": "VIA", "via_limit": 1,
             "pads": [{"layer": 0, "x": 1, "y": 1}, {"layer": 1, "x": 1, "y": 1}]}
        ],
    }
    resp = route(req)
    verify_solution(req, resp)
    assert resp["status"] == "optimal"
    assert resp["objectives"] == {"total_vias": 1, "total_moves": 0}
    assert resp["nets"][0]["name"] == "VIA"
    assert resp["nets"][0]["vias"] == [[1, 1]]


def test_via_limit_zero_makes_cross_layer_net_infeasible():
    req = {
        "grid": {"width": 3, "height": 3},
        "nets": [
            {"via_limit": 0,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 1, "x": 2, "y": 2}]}
        ],
    }
    resp = route(req)
    assert resp["status"] == "infeasible"
    assert resp["proven"] is True
    assert resp["nets"] is None and resp["objectives"] is None


def test_lexicographic_objective_one_via_minimal_moves():
    # Pads on different layers: at least one via is unavoidable; the via should
    # be placed so that the 4 Manhattan moves are all that is needed.
    req = {
        "grid": {"width": 3, "height": 3},
        "nets": [
            {"via_limit": 3,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 1, "x": 2, "y": 2}]}
        ],
    }
    resp = route(req)
    verify_solution(req, resp)
    assert resp["status"] == "optimal"
    assert resp["objectives"] == {"total_vias": 1, "total_moves": 4}


def test_two_nets_keep_distance():
    req = {
        "grid": {"width": 5, "height": 3},
        "nets": [
            {"via_limit": 0,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 0, "x": 4, "y": 0}]},
            {"via_limit": 0,
             "pads": [{"layer": 0, "x": 0, "y": 2}, {"layer": 0, "x": 4, "y": 2}]},
        ],
    }
    resp = route(req)
    verify_solution(req, resp)
    assert resp["status"] == "optimal"
    assert resp["objectives"] == {"total_vias": 0, "total_moves": 8}


def test_blocked_cells_force_detour():
    req = {
        "grid": {"width": 3, "height": 3},
        "blocked": {"layer0": [[1, 0]], "layer1": []},
        "nets": [
            {"via_limit": 0,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 0, "x": 2, "y": 0}]}
        ],
    }
    resp = route(req)
    verify_solution(req, resp)
    assert resp["status"] == "optimal"
    assert resp["objectives"] == {"total_vias": 0, "total_moves": 4}


def test_crossing_nets_need_via():
    # Net A runs straight along row y=2 on layer 0; net B must cross that row.
    # Row y=1 and y=3 cells are forbidden to B (adjacency to A), so B hops
    # over A through layer 1: exactly 2 vias, 4 + 4 moves.
    req = {
        "grid": {"width": 5, "height": 5},
        "nets": [
            {"via_limit": 2,
             "pads": [{"layer": 0, "x": 0, "y": 2}, {"layer": 0, "x": 4, "y": 2}]},
            {"via_limit": 2,
             "pads": [{"layer": 0, "x": 2, "y": 0}, {"layer": 0, "x": 2, "y": 4}]},
        ],
    }
    resp = route(req)
    verify_solution(req, resp)
    assert resp["status"] == "optimal"
    assert resp["objectives"] == {"total_vias": 2, "total_moves": 8}


def test_tight_crossing_proven_infeasible():
    # On a 3x3 board the same crossing is impossible: whatever cells one net
    # uses, the other net's pads end up adjacent to them.
    req = {
        "grid": {"width": 3, "height": 3},
        "nets": [
            {"via_limit": 2,
             "pads": [{"layer": 0, "x": 0, "y": 1}, {"layer": 0, "x": 2, "y": 1}]},
            {"via_limit": 2,
             "pads": [{"layer": 0, "x": 1, "y": 0}, {"layer": 0, "x": 1, "y": 2}]},
        ],
    }
    resp = route(req)
    assert resp["status"] == "infeasible"
    assert resp["proven"] is True


def test_adjacent_pads_of_different_nets_infeasible():
    req = {
        "grid": {"width": 4, "height": 2},
        "nets": [
            {"via_limit": 1,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 0, "x": 3, "y": 0}]},
            {"via_limit": 1,
             "pads": [{"layer": 0, "x": 0, "y": 1}, {"layer": 0, "x": 3, "y": 1}]},
        ],
    }
    resp = route(req)
    assert resp["status"] == "infeasible"


def test_medium_instance_solves():
    req = {
        "grid": {"width": 8, "height": 8},
        "blocked": {"layer0": [[3, 3], [4, 4]], "layer1": [[5, 5]]},
        "nets": [
            {"via_limit": 2,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 1, "x": 7, "y": 7}]},
            {"via_limit": 2,
             "pads": [{"layer": 1, "x": 0, "y": 7}, {"layer": 0, "x": 7, "y": 0}]},
            {"via_limit": 1,
             "pads": [{"layer": 0, "x": 0, "y": 3}, {"layer": 0, "x": 7, "y": 4}]},
        ],
    }
    resp = route(req)
    verify_solution(req, resp)
    assert resp["status"] in ("optimal", "feasible")


def test_time_limit_reported():
    req = {
        "grid": {"width": 2, "height": 2},
        "nets": [
            {"via_limit": 0,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 0, "x": 1, "y": 0}]}
        ],
    }
    resp = route(req)
    assert resp["time_limit_seconds"] == 10.0
    assert resp["solve_time_seconds"] <= 10.0 + 1.0


def test_tiny_time_limit_distinguishes_feasible_from_unknown(monkeypatch):
    # With an (artificially) near-zero limit the response must still be
    # well-formed: either a verified solution or an explicit "unknown".
    import app.solver as solver_mod

    monkeypatch.setattr(solver_mod, "TIME_LIMIT_SECONDS", 0.0001)
    req = {
        "grid": {"width": 20, "height": 20},
        "nets": [
            {"via_limit": 4,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 1, "x": 19, "y": 19}]},
            {"via_limit": 4,
             "pads": [{"layer": 1, "x": 0, "y": 19}, {"layer": 0, "x": 19, "y": 0}]},
        ],
    }
    resp = route(req)
    assert resp["status"] in ("optimal", "feasible", "unknown")
    if resp["status"] == "unknown":
        assert resp["nets"] is None and resp["objectives"] is None
        assert resp["proven"] is False
    else:
        verify_solution(req, resp)


# --------------------------------------------------------------------------- 422s
def _post(req: dict):
    return client.post("/route", json=req)


def _base():
    return {
        "grid": {"width": 4, "height": 4},
        "nets": [
            {"via_limit": 1,
             "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 0, "x": 3, "y": 3}]}
        ],
    }


def test_422_pad_out_of_bounds():
    req = _base()
    req["nets"][0]["pads"][1] = {"layer": 0, "x": 4, "y": 3}
    assert _post(req).status_code == 422
    req["nets"][0]["pads"][1] = {"layer": 0, "x": 3, "y": -1}
    assert _post(req).status_code == 422


def test_422_pad_on_blocked_cell():
    req = _base()
    req["blocked"] = {"layer0": [[3, 3]], "layer1": []}
    assert _post(req).status_code == 422


def test_422_duplicate_pads_same_net():
    req = _base()
    req["nets"][0]["pads"][1] = {"layer": 0, "x": 0, "y": 0}
    assert _post(req).status_code == 422


def test_422_duplicate_pads_across_nets():
    req = _base()
    req["nets"].append(
        {"via_limit": 1,
         "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 0, "x": 2, "y": 2}]}
    )
    assert _post(req).status_code == 422


def test_422_negative_via_limit():
    req = _base()
    req["nets"][0]["via_limit"] = -1
    assert _post(req).status_code == 422


def test_422_grid_size_out_of_range():
    req = _base()
    req["grid"] = {"width": 1, "height": 4}
    assert _post(req).status_code == 422
    req["grid"] = {"width": 21, "height": 4}
    assert _post(req).status_code == 422


def test_422_net_count_out_of_range():
    req = _base()
    req["nets"] = []
    assert _post(req).status_code == 422
    req = _base()
    req["nets"] = [
        {"via_limit": 1,
         "pads": [{"layer": 0, "x": i, "y": 0}, {"layer": 0, "x": i, "y": 3}]}
        for i in range(7)
    ]
    assert _post(req).status_code == 422


def test_422_wrong_pad_count():
    req = _base()
    req["nets"][0]["pads"] = [{"layer": 0, "x": 0, "y": 0}]
    assert _post(req).status_code == 422
    req["nets"][0]["pads"] = [
        {"layer": 0, "x": 0, "y": 0},
        {"layer": 0, "x": 1, "y": 0},
        {"layer": 0, "x": 2, "y": 0},
    ]
    assert _post(req).status_code == 422


def test_422_bad_layer_and_blocked_bounds():
    req = _base()
    req["nets"][0]["pads"][0]["layer"] = 2
    assert _post(req).status_code == 422
    req = _base()
    req["blocked"] = {"layer0": [[9, 9]], "layer1": []}
    assert _post(req).status_code == 422
