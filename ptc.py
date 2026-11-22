"""DSH-style run_code transport for the platform's registered read-only tools."""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from audit import emit


def run_code(code, dispatch):
    if not isinstance(code, str) or len(code.encode("utf-8")) > 32000:
        raise ValueError("PTC code must be at most 32 KB")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("PTC requires Node.js")
    emit("ptc.code", {"code": code}, "llm")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([node, str(Path(__file__).with_name("ptc_bridge.cjs"))],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", creationflags=flags)
    timer = threading.Timer(8, proc.kill)
    timer.start()
    started = time.perf_counter()
    try:
        proc.stdin.write(json.dumps({"code": code}, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            message = json.loads(line)
            if message.get("type") == "tool":
                name, args = message.get("name"), message.get("args")
                if name not in {"inspect_instance", "estimate_pressure", "validate_program"} or not isinstance(args, dict):
                    raise ValueError("PTC called an unregistered tool")
                result = dispatch(name, args)
                proc.stdin.write(json.dumps(result, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            elif message.get("type") == "done":
                return {"ok": True, "value": message.get("value"), "logs": message.get("logs"),
                        "elapsed_s": time.perf_counter() - started}
            elif message.get("type") == "error":
                raise ValueError(message["error"])
        raise RuntimeError("PTC runtime stopped or exceeded its time budget")
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=3)
