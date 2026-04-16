# Copyright 2025 The Kubernetes Authors.
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
Persistent Session Runtime - Maintains a persistent shell across command executions.
"""

import asyncio
import logging
import os
import pty
import re
import select
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 60


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    cwd: str


class PersistentShell:
    """Maintains a persistent shell using PTY."""

    # ANSI escape sequence regex for cleaning terminal output
    _ANSI_RE = re.compile(
        r"\x1b\[[0-9;]*[a-zA-Z@-~]|\x1b\[\?[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x07|\r"
    )

    def __init__(self):
        self.master_fd: int | None = None
        self.pid: int | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def start(self) -> None:
        if self.pid and self._is_alive() and self._initialized:
            return

        self.pid, self.master_fd = pty.fork()
        if self.pid == 0:
            os.environ["TERM"] = "dumb"
            os.chdir("/workspace")
            os.execvp("/bin/bash", ["/bin/bash", "-i"])
        else:
            os.set_blocking(self.master_fd, False)
            await asyncio.sleep(0.2)
            self._read_all()
            # Disable prompts
            os.write(
                self.master_fd,
                b"export PS1='' PS2='' PS3='' PS4=''; unset PROMPT_COMMAND; stty -echo\n",
            )
            await asyncio.sleep(0.1)
            self._read_all()
            self._initialized = True
            logger.info(f"Shell started, PID: {self.pid}")

    def _is_alive(self) -> bool:
        if not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def _read_all(self, timeout: float = 0.1) -> str:
        output = []
        while True:
            r, _, _ = select.select([self.master_fd], [], [], timeout)
            if not r:
                break
            try:
                data = os.read(self.master_fd, 4096)
                if data:
                    output.append(data.decode("utf-8", errors="replace"))
                else:
                    break
            except OSError:
                break
        return "".join(output)

    async def execute(self, command: str, timeout: int = 60) -> ExecuteResponse:
        async with self._lock:
            await self.start()

            cmd_id = uuid.uuid4().hex[:8]
            start_marker = f"___START_{cmd_id}___"
            end_marker_prefix = f"___END_{cmd_id}___"

            # Clear buffer
            self._read_all()

            # Send command with markers (exit code embedded in end marker)
            wrapped = (
                f"echo '{start_marker}'\n"
                f"{command}\n"
                f'__ec__=$?; echo "{end_marker_prefix}$__ec__$(pwd)___"\n'
            )
            os.write(self.master_fd, wrapped.encode())

            # Read output until end marker
            output = []
            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < timeout:
                await asyncio.sleep(0.05)
                chunk = self._read_all(0.1)
                if chunk:
                    output.append(chunk)
                if end_marker_prefix in "".join(output):
                    break
            else:
                return ExecuteResponse(
                    stdout="".join(output), stderr="timeout", exit_code=-1, cwd=""
                )

            raw = "".join(output)
            # Clean ANSI sequences
            cleaned = self._ANSI_RE.sub("", raw)

            # Parse output line by line
            lines = cleaned.split("\n")
            stdout_parts = []
            started = False
            exit_code = 0
            cwd = ""

            # Build command lines set for filtering echoes (first occurrence only)
            cmd_lines = {}
            for line in command.split("\n"):
                s = line.strip()
                if s:
                    cmd_lines[s] = cmd_lines.get(s, 0) + 1

            for line in lines:
                stripped = line.strip()

                # Check for start marker
                if start_marker in stripped:
                    started = True
                    continue

                if not started:
                    continue

                # Check for end marker line (must start with it, not command echo)
                if stripped.startswith(end_marker_prefix):
                    try:
                        suffix = stripped[len(end_marker_prefix) :]
                        # Format: {exit_code}{cwd}___
                        if suffix.endswith("___"):
                            suffix = suffix[:-3]
                            # Find where cwd starts (first /)
                            slash_idx = suffix.find("/")
                            if slash_idx != -1:
                                exit_code = int(suffix[:slash_idx])
                                cwd = suffix[slash_idx:]
                            else:
                                exit_code = int(suffix)
                    except (ValueError, IndexError):
                        pass
                    break

                # Skip command echoes (first occurrence only)
                if stripped in cmd_lines and cmd_lines[stripped] > 0:
                    cmd_lines[stripped] -= 1
                    continue

                # Skip internal markers
                if "__ec__=$?" in stripped:
                    continue

                stdout_parts.append(line)

            stdout = "\n".join(stdout_parts).strip()
            return ExecuteResponse(
                stdout=stdout, stderr="", exit_code=exit_code, cwd=cwd
            )


shell: PersistentShell | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global shell
    shell = PersistentShell()
    await shell.start()
    yield


app = FastAPI(title="Persistent Session Runtime", lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResponse)
async def execute(req: ExecuteRequest):
    if not shell:
        return ExecuteResponse(stdout="", stderr="not initialized", exit_code=1, cwd="")
    return await shell.execute(req.command, req.timeout)


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    path = os.path.join("/workspace", file.filename)
    os.makedirs(os.path.dirname(path) or "/workspace", exist_ok=True)
    with open(path, "wb") as f:
        f.write(await file.read())
    return JSONResponse({"message": f"uploaded {file.filename}"})


@app.get("/download/{path:path}")
async def download(path: str):
    full = path if path.startswith("/") else os.path.join("/workspace", path)
    if os.path.isfile(full):
        return FileResponse(full, filename=os.path.basename(path))
    return JSONResponse(status_code=404, content={"message": "not found"})
