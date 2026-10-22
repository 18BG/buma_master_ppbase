#!/usr/bin/env python3
"""Run real JS PocketBase SDK auth/CRUD/files/realtime with synthetic users."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from c10_remaining_runtime import Backend


async def probe(url, reference, prefix):
    b = Backend(url, reference, prefix)
    try:
        role = await b.fixture(
            "roles", {"name": prefix, "permission": {"isAdmin": True}, "status": True}
        )
        await b.fixture(
            "users",
            {
                "name": prefix,
                "username": prefix,
                "email": prefix + "@example.com",
                "role": role,
                "password": "abcde123",
                "passwordConfirm": "abcde123",
            },
        )
        viewer_role = await b.fixture(
            "roles",
            {
                "name": prefix + "viewer",
                "permission": {"customer": {"read": True}},
                "status": True,
            },
        )
        await b.fixture(
            "users",
            {
                "name": prefix + "viewer",
                "username": prefix + "viewer",
                "email": prefix + "viewer@example.com",
                "role": viewer_role,
                "password": "abcde123",
                "passwordConfirm": "abcde123",
            },
        )
        root = Path(__file__).resolve().parents[1]
        sdk = root.parent / "buma_boutique_web/node_modules/pocketbase/dist/pocketbase.es.mjs"
        env = {
            **os.environ,
            "C10_IDENTITY": prefix,
            "C10_URL": url,
            "C10_SDK_PATH": str(sdk),
            "C10_VIEWER_ROLE": viewer_role,
        }
        process = await asyncio.create_subprocess_exec(
            "/opt/homebrew/bin/node",
            "--experimental-eventsource",
            str(root / "scripts/c10_sdk_probe.mjs"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 35)
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"status": "failed", "error": "SDK probe timeout"}
        result = json.loads(stdout.decode().splitlines()[-1])
        if process.returncode and result.get("status") == "passed":
            result = {"status": "failed", "error": stderr.decode()[-1000:]}
        return result
    finally:
        # SDK deletes uploaded record/storage in its finally block. If it timed
        # out, track the fixture record too, leaving a clear failed proof.
        await b.track("buma_customer", firstname=prefix)
        await b.close()


async def main():
    prefix = "C10SDK" + secrets.token_hex(3)
    result = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "pocketbase": await probe("http://127.0.0.1:8098", True, prefix),
        "ppbase": await probe("http://127.0.0.1:8090", False, prefix),
    }
    result["status"] = (
        "passed"
        if all(result[key].get("status") == "passed" for key in ("pocketbase", "ppbase"))
        else "failed"
    )
    (Path(__file__).parents[1] / "docs/c10-sdk-runtime-report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False))
    assert result["status"] == "passed", "SDK parity failed; see report"


if __name__ == "__main__":
    asyncio.run(main())
