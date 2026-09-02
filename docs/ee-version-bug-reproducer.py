#!/usr/bin/env python3
"""Earth Engine HV API — silent INTERNAL hang on large `version` values.

What this reproduces
--------------------
A `computePixels` request whose `expression` contains a load node with
a `version` argument set to a large 64-bit value (≳ 1e17 — e.g. a
Unix-nanosecond timestamp) does not return a clean error. The
connection stays open for tens of seconds and either eventually
returns `500 INTERNAL` (request still routed to the backend) or the
client read-timeout fires first and the caller sees no response body
at all. The corresponding entries in Cloud Logging show
`status.code = 13` (INTERNAL).

This makes the failure mode hard to triage:

  * No 400 / 401 / 403 / 429 / 404.
  * No JSON error body.
  * No content-type header.
  * The audited service name is `earthengine.googleapis.com` but
    Data Access logs default to off, so callers see absolutely nothing.

The same request body with `version` divided by 1000 (i.e. switched
from nanoseconds to microseconds) returns 200 + a real GeoTIFF in
~2 s. So the wire format and identity are fine — only the *magnitude*
of `version` is the trigger.

What the request looks like
---------------------------
* POST `https://earthengine-highvolume.googleapis.com/v1/projects/<P>/image:computePixels`
* Standard OAuth bearer + `x-goog-user-project: <P>` headers.
* Expression: a tiny `Image.normalizedDifference` over a one-day
  `Filter.dateRangeContains` of `LANDSAT/LC09/C02/T1_L2`, with
  `ImageCollection.load.arguments.version = {"constantValue": <T>}`.
* Grid: 32x32 EPSG:4326 over SF Bay — nominal pixel count to keep
  compute cost tiny; the trigger is not compute load.

The three values of `<T>` exercised below are the same instant in
time encoded at three different unit scales:

  * `T_micros = 1_778_588_808_169_445`   (≈ 2026-05-12T16:26 UTC)
  * `T_millis = 1_778_588_808_169`
  * `T_nanos  = 1_778_588_808_169_445_000`

Expected behavior
-----------------
* `micros`: 200 OK (snapshot at the requested version).
* `millis`: 400 INVALID_ARGUMENT — clean "not found at version N".
* `nanos`:  **also** 400 INVALID_ARGUMENT (or another clean status).
  Today: server-side `gRPC INTERNAL` that frequently doesn't surface
  to the client and instead presents as a silent timeout.

Run
---
    GOOGLE_APPLICATION_CREDENTIALS=... \
    EE_PROJECT=<project> \
    python docs/ee-version-bug-reproducer.py

If `EE_ACCESS_TOKEN` is set, the script uses that bearer token verbatim;
otherwise it asks `gcloud auth application-default print-access-token`.

The script uses only the Python standard library so the reviewer can
repro with no environment setup beyond Python 3.10+ and a project
that has EE enabled.

Cloud Logging filter for the server-side view
---------------------------------------------
Data Access audit logs are off by default for EE on most projects.
To capture the gRPC INTERNAL entries that correspond to the hung
requests below, add this to the project's IAM policy `auditConfigs`
before reproducing (project-owner equivalent required):

    {
      "service": "earthengine.googleapis.com",
      "auditLogConfigs": [
        {"logType": "ADMIN_READ"},
        {"logType": "DATA_READ"},
        {"logType": "DATA_WRITE"}
      ]
    }

Then in the GCP Logs Explorer (project = `<EE_PROJECT>`):

    resource.type="audited_resource"
    protoPayload.serviceName="earthengine.googleapis.com"
    protoPayload.methodName="google.earthengine.v1.EarthEngine.ComputePixels"
    protoPayload.status.code=13

…or the gcloud equivalent:

    gcloud logging read \
      'protoPayload.serviceName="earthengine.googleapis.com"
       AND protoPayload.methodName="google.earthengine.v1.EarthEngine.ComputePixels"
       AND protoPayload.status.code=13' \
      --project=<EE_PROJECT> --freshness=1h --limit=20

In our reproduction:

  * `micros` (HTTP 200) — audit entry has an empty `status` field.
  * `nanos` (client-side TIMEOUT) — audit entry has `status.code = 13`
    with no `status.message`. The latency between request acceptance
    and audit-log emission is sub-second, so the backend did reach
    a terminal state quickly — the response just never made it back
    to the client.
  * `millis` (clean HTTP 400 "not found at version N") — also surfaces
    as `status.code = 13` in this audit view, even though the
    user-facing response is `INVALID_ARGUMENT`. The audit-log status
    code therefore can't be used on its own to distinguish "backend
    crashed" from "user got a clean error" — only `code = 0` vs.
    `code != 0`.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT = os.environ.get("EE_PROJECT", "").strip()
if not PROJECT:
    sys.exit("Set EE_PROJECT to a GCP project ID with EE enabled.")

URL = f"https://earthengine-highvolume.googleapis.com/v1/projects/{PROJECT}/image:computePixels"


def _token() -> str:
    explicit = os.environ.get("EE_ACCESS_TOKEN", "").strip()
    if explicit:
        return explicit
    return subprocess.check_output(
        ["gcloud", "auth", "application-default", "print-access-token"], text=True
    ).strip()


# The expression: NDVI over a one-day Landsat 9 window, parameterized
# only by the version constant on ImageCollection.load. Tiny on purpose:
# the bug is not about compute load.
def expression(version_value: int) -> dict:
    return {
        "result": "0",
        "values": {
            "1": {"constantValue": ["SR_B5", "SR_B4"]},
            "2": {
                "functionInvocationValue": {
                    "functionName": "Image.select",
                    "arguments": {
                        "bandSelectors": {"valueReference": "1"},
                        "input": {"argumentReference": "_MAPPING_VAR_0_0"},
                    },
                }
            },
            "0": {
                "functionInvocationValue": {
                    "functionName": "Image.normalizedDifference",
                    "arguments": {
                        "bandNames": {"valueReference": "1"},
                        "input": {
                            "functionInvocationValue": {
                                "functionName": "reduce.median",
                                "arguments": {
                                    "collection": {
                                        "functionInvocationValue": {
                                            "functionName": "Collection.map",
                                            "arguments": {
                                                "baseAlgorithm": {
                                                    "functionDefinitionValue": {
                                                        "argumentNames": ["_MAPPING_VAR_0_0"],
                                                        "body": "2",
                                                    }
                                                },
                                                "collection": {
                                                    "functionInvocationValue": {
                                                        "functionName": "Collection.filter",
                                                        "arguments": {
                                                            "collection": {
                                                                "functionInvocationValue": {
                                                                    "functionName": "ImageCollection.load",
                                                                    "arguments": {
                                                                        "id": {"constantValue": "LANDSAT/LC09/C02/T1_L2"},
                                                                        "version": {"constantValue": version_value},
                                                                    },
                                                                }
                                                            },
                                                            "filter": {
                                                                "functionInvocationValue": {
                                                                    "functionName": "Filter.dateRangeContains",
                                                                    "arguments": {
                                                                        "leftValue": {
                                                                            "functionInvocationValue": {
                                                                                "functionName": "DateRange",
                                                                                "arguments": {
                                                                                    "start": {"constantValue": "2023-07-15"},
                                                                                    "end": {"constantValue": "2023-07-16"},
                                                                                },
                                                                            }
                                                                        },
                                                                        "rightField": {"constantValue": "system:time_start"},
                                                                    },
                                                                }
                                                            },
                                                        },
                                                    }
                                                },
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    },
                }
            },
        },
    }


GRID = {
    "dimensions": {"width": 32, "height": 32},
    "affineTransform": {
        "scaleX": 0.0003, "shearX": 0, "translateX": -122.5,
        "shearY": 0, "scaleY": -0.0003, "translateY": 38.0,
    },
    "crsCode": "EPSG:4326",
}


def post(token: str, version_value: int, timeout_s: float) -> dict:
    body = {
        "expression": expression(version_value),
        "fileFormat": "GEO_TIFF",
        "grid": GRID,
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-goog-user-project": PROJECT,
        },
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
            ct = resp.headers.get("content-type", "")
            return {
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "status": resp.status,
                "content_type": ct,
                "body_len": len(data),
                "snippet": data[:300].decode("utf-8", "replace") if not ct.startswith("image/") else "<binary geotiff>",
            }
    except urllib.error.HTTPError as e:
        data = e.read()
        return {
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "status": e.code,
            "content_type": e.headers.get("content-type", ""),
            "body_len": len(data),
            "snippet": data[:300].decode("utf-8", "replace"),
        }
    except (urllib.error.URLError, socket.timeout) as e:
        return {
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "status": "TIMEOUT_OR_NETWORK_ERROR",
            "error": repr(e),
        }


def main() -> None:
    token = _token()
    nanos = 1_778_588_808_169_445_000
    micros = nanos // 1_000  # 1_778_588_808_169_445  (≈ 2026-05-12T16:26 UTC)
    millis = nanos // 1_000_000  # 1_778_588_808_169
    print("# Earth Engine HV computePixels silent-hang reproducer")
    print(f"# project    : {PROJECT}")
    print(f"# endpoint   : {URL}")
    print(f"# T_micros   : {micros}")
    print(f"# T_millis   : {millis}")
    print(f"# T_nanos    : {nanos}")
    print()
    # Run micros first as a known-good baseline.
    for label, v in (("micros (expect 200)", micros),
                     ("millis (expect clean 400)", millis),
                     ("nanos  (BUG: hangs / INTERNAL)", nanos)):
        print(f"--- {label}: version={v}")
        result = post(token, v, timeout_s=120.0)
        print(json.dumps(result, indent=2))
        print()


if __name__ == "__main__":
    main()
