"""Per-user runtime paths — Python twin of runtime_dirs.sh.

Keep the two in lockstep; tests/test-runtime-dirs.sh asserts parity.
"""
import os


def runtime_dir():
    # `or` (not os.environ.get default): empty-but-set env vars must behave like
    # unset to match bash's `${VAR:-default}` expansion. os.environ.get(VAR, default)
    # returns "" for a set-but-empty var, diverging from bash. Parity pinned by R5.
    return (
        os.environ.get("N8N_KNOWLEDGE_RUNTIME_DIR")
        or os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "n8n-knowledge")
    )


def debug_log_path():
    return os.path.join(runtime_dir(), "debug.log")


def state_dir():
    return os.path.join(runtime_dir(), "state")
