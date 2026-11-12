"""In-cluster OpenAI client for the HyperPod Inference Gateway E2E (stdlib only).

POSTs COUNT chat completions for MODEL to GATEWAY_URL/v1/chat/completions and prints each
JSON response on its own line. The gateway always terminates TLS; with the default
auto-issued (self-signed) certificate there is no CA to verify against in-cluster, so
verification is disabled — this client checks routing, not PKI.

The FIRST request retries (vLLM cold start, EPP pool warm-up, Envoy programming); every
later request must succeed on its first try.
"""

import json
import os
import ssl
import sys
import time
import urllib.request

URL = os.environ["GATEWAY_URL"].rstrip("/") + "/v1/chat/completions"
MODEL = os.environ["MODEL"]
COUNT = int(os.environ.get("COUNT", "5"))
CTX = ssl._create_unverified_context()
BODY = json.dumps(
    {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly the word: pong"}],
        "max_tokens": 16,
        "temperature": 0,
    }
).encode()


def post() -> dict:
    req = urllib.request.Request(URL, data=BODY, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300, context=CTX) as resp:
        return json.loads(resp.read())


for attempt in range(60):
    try:
        print(json.dumps(post()), flush=True)
        break
    except Exception as exc:  # noqa: BLE001 — any failure during warm-up is retried
        print(f"[client] attempt {attempt}: {exc}", file=sys.stderr, flush=True)
        time.sleep(10)
else:
    sys.exit("[client] gateway never answered")

for _ in range(COUNT - 1):
    print(json.dumps(post()), flush=True)
