"""Per-session state for the backstop recall hook."""
import json
import os

import runtime_dirs

STALE_TOTAL = 15
STALE_TRIGGER = 5


def _dir():
    # Per-user runtime dir (0700) instead of world-readable /tmp; backstop session
    # state lives under <runtime>/state/backstop. runtime_dirs is imported via the
    # same sys.path entry callers use to import this module (hooks/lib on sys.path).
    d = os.path.join(runtime_dirs.state_dir(), "backstop")
    os.makedirs(d, exist_ok=True)
    return d


def path_for(session_id):
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    return os.path.join(_dir(), f"{safe or 'nosession'}.json")


def new_state():
    return {"total_calls": 0, "trigger_calls": 0, "recalls_done": 0, "topics": {}}


def load_state(session_id):
    try:
        with open(path_for(session_id)) as f:
            s = json.load(f)
        for k, v in new_state().items():
            s.setdefault(k, v)
        return s
    except Exception:
        return new_state()


def save_state(session_id, state):
    try:
        with open(path_for(session_id), "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def is_stale(entry, total_calls, trigger_calls):
    return (total_calls - entry.get("at_total", 0) > STALE_TOTAL) or \
           (trigger_calls - entry.get("at_trigger", 0) > STALE_TRIGGER)


def active_covered(state):
    """Keywords whose topic was recalled and is NOT yet stale."""
    total, trig = state["total_calls"], state["trigger_calls"]
    covered = set()
    for sig, entry in state["topics"].items():
        if not is_stale(entry, total, trig):
            covered.update(sig.split("|"))
    return covered


def decide(state, signature, cap):
    """Fire if there is at least one fresh keyword (non-empty signature) and under cap."""
    if not signature:
        return False
    return state.get("recalls_done", 0) < cap


def record(state, signature):
    sig = "|".join(sorted(signature))
    state["topics"][sig] = {"at_total": state["total_calls"], "at_trigger": state["trigger_calls"]}
    state["recalls_done"] = state.get("recalls_done", 0) + 1
