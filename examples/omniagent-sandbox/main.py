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
ALLOWED_DIRS = ("/workspace", "/tmp")
CWD_SENTINEL = "___OMNIAGENT_CWD___"

# Agent 操作身份：所有 /execute subprocess 和 /upload 落盘强制对齐到这个 uid。
# 跟 E2B envd 行为对齐——server 可能以 root 起，但 LLM 看到的 ownership 始终
# 等于 exec 身份，无需客户端做 chown 协调。
AGENT_UID = int(os.environ.get("AGENT_UID", os.getuid()))
AGENT_GID = int(os.environ.get("AGENT_GID", os.getgid()))
# 只有 root server 才有切 uid 和 chown 任意 owner 的权限；非 root 起的话
# subprocess 沿用当前身份、upload 不 chown（也就退化到 server 自己是 agent
# 的当前默认行为，与改动前等效）。
_CAN_SWITCH_IDENTITY = os.geteuid() == 0


def get_safe_path(file_path: str) -> str:
    """Sanitizes the file path to ensure it stays within allowed directories.

    Accepts absolute paths under any of ``ALLOWED_DIRS`` (e.g. ``/workspace/foo.md``
    or ``/tmp/bar.py``), and relative paths (e.g. ``foo.md``, treated as relative
    to ``/workspace``).
    """
    if os.path.isabs(file_path):
        full_path = os.path.realpath(file_path)
    else:
        full_path = os.path.realpath(os.path.join(WORKSPACE_DIR, file_path))

    for base in ALLOWED_DIRS:
        base_real = os.path.realpath(base)
        if os.path.commonpath([base_real, full_path]) == base_real:
            return full_path

    raise ValueError(f"Access denied: Path must be within {', '.join(ALLOWED_DIRS)}")


class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 60
    env: dict[str, str] | None = None


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
    # subprocess `user=` 只改 uid，不会自动重置 HOME/USER/LOGNAME env，
    # 默认会继承父进程（root 跑的 server）的 HOME=/root。显式改成 /home/user
    # 让 dotfile / npm cache / shell history 落在 agent 自己的 HOME。
    base_env = {**os.environ}
    if _CAN_SWITCH_IDENTITY:
        base_env["HOME"] = "/home/user"
        base_env["USER"] = "user"
        base_env["LOGNAME"] = "user"
    merged_env = {**base_env, **req.env} if req.env else base_env
    subprocess_kwargs: dict = {}
    if _CAN_SWITCH_IDENTITY:
        subprocess_kwargs["user"] = AGENT_UID
        subprocess_kwargs["group"] = AGENT_GID
    proc = await asyncio.create_subprocess_shell(
        wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/workspace",
        env=merged_env,
        **subprocess_kwargs,
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
    parent = os.path.dirname(full_path) or WORKSPACE_DIR
    os.makedirs(parent, exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(await file.read())
    # 把文件 + 新建出来的中间目录链都 chown 给 agent，避免 LLM(agent 身份)
    # 因 root-owned 父目录的 sticky bit 拒 rm。与 E2B envd 落盘自动 user owned 对齐。
    if _CAN_SWITCH_IDENTITY:
        try:
            os.chown(full_path, AGENT_UID, AGENT_GID)
            d = parent
            while d not in ALLOWED_DIRS and d != "/":
                os.chown(d, AGENT_UID, AGENT_GID)
                d = os.path.dirname(d)
        except OSError as e:
            logger.warning(f"chown failed for {full_path}: {e}")
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
