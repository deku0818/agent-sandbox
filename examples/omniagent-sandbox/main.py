# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
OmniAgent Sandbox Runtime - Per-command subprocess with cwd persistence.
"""

import asyncio
import logging
import os
import shlex
import sys
import urllib.parse

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

WORKSPACE_DIR = "/workspace"
CWD_SENTINEL = "___OMNIAGENT_CWD___"


def get_safe_path(file_path: str) -> str:
    """Sanitizes the file path to ensure it stays within /workspace."""
    base_dir = os.path.realpath(WORKSPACE_DIR)
    clean_path = file_path.lstrip("/")
    full_path = os.path.realpath(os.path.join(base_dir, clean_path))
    if os.path.commonpath([base_dir, full_path]) != base_dir:
        raise ValueError("Access denied: Path must be within /workspace")
    return full_path


class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 60


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    cwd: str


_cwd: str = WORKSPACE_DIR

app = FastAPI(title="OmniAgent Sandbox Runtime")


@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResponse)
async def execute(req: ExecuteRequest):
    global _cwd

    wrapped = (
        f"cd {shlex.quote(_cwd)} 2>/dev/null\n"
        f"{req.command}\n"
        f"__ec=$?; echo {CWD_SENTINEL}; pwd -P; exit $__ec"
    )
    proc = await asyncio.create_subprocess_shell(
        wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/workspace",
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=req.timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.warning(f"Command timed out after {req.timeout}s: {req.command[:200]}")
        return ExecuteResponse(
            stdout="",
            stderr=f"Command timed out after {req.timeout} seconds",
            exit_code=124,
            cwd=_cwd,
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    exit_code = proc.returncode or 0

    # Parse cwd from stdout: everything after the sentinel line
    if CWD_SENTINEL in stdout:
        parts = stdout.split(CWD_SENTINEL, 1)
        actual_stdout = parts[0].rstrip("\n")
        new_cwd = parts[1].strip()
        if new_cwd.startswith("/"):
            _cwd = new_cwd
    else:
        actual_stdout = stdout.rstrip("\n")

    return ExecuteResponse(
        stdout=actual_stdout,
        stderr=stderr.rstrip("\n"),
        exit_code=exit_code,
        cwd=_cwd,
    )


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    try:
        full_path = get_safe_path(file.filename)
    except ValueError:
        return JSONResponse(status_code=403, content={"message": "Access denied"})
    os.makedirs(os.path.dirname(full_path) or WORKSPACE_DIR, exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(await file.read())
    return JSONResponse({"message": f"uploaded {file.filename}"})


@app.get("/download/{path:path}")
async def download(path: str):
    decoded_path = urllib.parse.unquote(path)
    try:
        full_path = get_safe_path(decoded_path)
    except ValueError:
        return JSONResponse(status_code=403, content={"message": "Access denied"})
    if os.path.isfile(full_path):
        return FileResponse(
            full_path,
            media_type="application/octet-stream",
            filename=os.path.basename(decoded_path),
        )
    return JSONResponse(status_code=404, content={"message": "not found"})


@app.get("/list/{path:path}")
async def list_files(path: str):
    decoded_path = urllib.parse.unquote(path)
    try:
        full_path = get_safe_path(decoded_path)
    except ValueError:
        return JSONResponse(status_code=403, content={"message": "Access denied"})
    if not os.path.isdir(full_path):
        return JSONResponse(
            status_code=404, content={"message": "Path is not a directory"}
        )
    entries = []
    with os.scandir(full_path) as it:
        for entry in it:
            stats = entry.stat()
            entries.append(
                {
                    "name": entry.name,
                    "size": stats.st_size,
                    "type": "directory" if entry.is_dir() else "file",
                    "mod_time": stats.st_mtime,
                }
            )
    return JSONResponse(status_code=200, content=entries)


@app.get("/exists/{path:path}")
async def exists(path: str):
    decoded_path = urllib.parse.unquote(path)
    try:
        full_path = get_safe_path(decoded_path)
    except ValueError:
        return JSONResponse(status_code=403, content={"message": "Access denied"})
    return JSONResponse(
        status_code=200,
        content={
            "path": decoded_path,
            "exists": os.path.exists(full_path),
        },
    )
