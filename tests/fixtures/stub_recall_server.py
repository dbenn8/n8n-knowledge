#!/usr/bin/env python3
"""Stub Hindsight recall HTTP server for resilience tests.

CLI:
    argv[1] = mode      one of: ok | sem-fail | slow | gotcha-fail-once
    argv[2] = port      TCP port to bind on 127.0.0.1. Pass 0 for an ephemeral
                        OS-assigned port; the actual bound port is then printed
                        to stdout as a single "PORT=<n>" line (flushed) BEFORE
                        the server starts accepting requests, so callers can use
                        that line as both the port source and the readiness probe.
    argv[3] = body_log  path to append each request body (one line per request)

Request classification (by raw body substring):
    '"max_tokens": 2000'  -> gotcha recall (do_gotcha_recall)
    '"tags_match"'        -> structured node-spec recall (do_structured_recall)
    otherwise             -> semantic recall (do_recall)

  NOTE: all three channels now send ``"include": {"source_facts": {}}`` (the
  fix that lets gotcha/structured observations carry provenance), so a bare
  "source_facts" substring no longer distinguishes the semantic channel — the
  gotcha (max_tokens 2000) and structured (tags_match) markers are checked
  first, and semantic is the fall-through.

Modes:
    ok              -> 200 JSON for every request
    sem-fail        -> HTTP 500 for semantic requests only; 200 for the rest
    slow            -> sleep 30 before answering (exercises curl --max-time)
    gotcha-fail-once -> HTTP 500 for first gotcha request, then 200
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8733
BODY_LOG = sys.argv[3] if len(sys.argv) > 3 else "/tmp/stub-recall-body.log"
GOTCHA_REQUEST_COUNT = 0


def classify(body):
    # Order matters: all three channels now send include.source_facts, so the
    # gotcha (max_tokens 2000) and structured (tags_match) markers are checked
    # BEFORE falling through to semantic.
    if '"max_tokens": 2000' in body:
        return "gotcha"
    if '"tags_match"' in body:
        return "struct"
    return "semantic"


def make_payload(kind, body=""):
    results = []
    for i in range(3):
        results.append({
            "id": "%s-%d" % (kind, i),
            "type": "fact",
            "text": "%s result %d for test" % (kind, i),
            "tags": ["source:github"],
            "metadata": {"source_url": "https://example.com/%s/%d" % (kind, i)},
        })

    payload = {"results": results}

    # Gotcha responses carry a synthesized observation with source_fact_ids plus
    # the top-level source_facts dict that resolves them — but ONLY when the
    # request actually asked for source facts (mirrors the real API, which
    # strips source_fact_ids entirely without the include flag). This proves the
    # gotcha channel now carries provenance end-to-end.
    if kind == "gotcha" and "source_facts" in body:
        results.append({
            "id": "gotcha-obs",
            "type": "observation",
            "text": "gotcha synthesis for test",
            "tags": [],
            "metadata": None,
            "source_fact_ids": ["sf-1", "sf-2"],
        })
        # The primary source fact carries source/engagement tags+metadata so the
        # observation scores via its source thread (score_result eng path) and
        # survives the LOW-result cap — otherwise an empty-metadata observation
        # would score community_base and be crowded out of the single LOW slot,
        # making the E3 provenance assertion flaky.
        payload["source_facts"] = {
            "sf-1": {
                "id": "sf-1",
                "text": "src one",
                "tags": ["source:discourse", "outcome:solved"],
                "metadata": {
                    "url": "https://example.com/sf/1",
                    "views": "5000",
                    "like_count": "20",
                },
            },
            "sf-2": {
                "id": "sf-2",
                "text": "src two",
                "tags": ["source:discourse"],
                "metadata": {"url": "https://example.com/sf/2"},
            },
        }

    return payload


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""

        # Log the raw body (newlines stripped) as one line.
        try:
            with open(BODY_LOG, "a") as f:
                f.write(body.replace("\n", "").replace("\r", "") + "\n")
        except OSError:
            pass

        kind = classify(body)

        if MODE == "slow":
            time.sleep(30)

        if MODE == "gotcha-fail-once" and kind == "gotcha":
            global GOTCHA_REQUEST_COUNT
            GOTCHA_REQUEST_COUNT += 1
            if GOTCHA_REQUEST_COUNT == 1:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "gotcha recall failed (transient)"}')
                return

        if MODE == "sem-fail" and kind == "semantic":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "semantic recall failed"}')
            return

        payload = json.dumps(make_payload(kind, body)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass  # quiet


def main():
    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        sys.stderr.write("stub_recall_server: bind failed on port %d: %s\n"
                         % (PORT, exc))
        sys.stderr.flush()
        sys.exit(1)

    # Announce the actual bound port (resolves PORT=0 ephemeral binds) as a
    # single flushed line. This line doubles as the readiness probe: callers
    # poll for it before sending traffic, so it MUST be flushed past stdio
    # buffering (a redirected pipe is block-buffered) before serve_forever().
    bound_port = server.server_address[1]
    sys.stdout.write("PORT=%d\n" % bound_port)
    sys.stdout.flush()

    server.serve_forever()


if __name__ == "__main__":
    main()
