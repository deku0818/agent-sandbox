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
import select
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
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

    def __init__(self):
        self.master_fd: int | None = None
        self.pid: int | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.pid and self._is_alive():
            return

        self.pid, self.master_fd = pty.fork()
        if self.pid == 0:
            os.chdir("/workspace")
            os.execvp("/bin/bash", ["/bin/bash", "-i"])
        else:
            os.set_blocking(self.master_fd, False)
            await asyncio.sleep(0.2)
            self._read_all()  # Clear initial output
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
                    output.append(data.decode('utf-8', errors='replace'))
                else:
                    break
            except OSError:
                break
        return ''.join(output)

    async def execute(self, command: str, timeout: int = 60) -> ExecuteResponse:
        async with self._lock:
            await self.start()

            marker = f"__DONE_{uuid.uuid4().hex[:8]}__"
            self._read_all()

            # Send command with markers to detect completion
            full_cmd = f"{command}\n_ec=$?; echo {marker}; echo __EXIT__$_ec; echo __CWD__$(pwd)\n"
            os.write(self.master_fd, full_cmd.encode())

            # Read output
            output = []
            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < timeout:
                await asyncio.sleep(0.05)
                chunk = self._read_all(0.1)
                if chunk:
                    output.append(chunk)
                full = ''.join(output)
                if marker in full and "__EXIT__" in full and "__CWD__" in full:
                    break
            else:
                return ExecuteResponse(stdout=''.join(output), stderr="timeout", exit_code=-1, cwd="")

            full = ''.join(output)

            # Parse exit code
            exit_code = 0
            try:
                idx = full.find("__EXIT__") + 8
                exit_code = int(full[idx:full.find('\n', idx)].strip())
            except (ValueError, IndexError):
                pass

            # Parse cwd
            cwd = ""
            try:
                idx = full.find("__CWD__") + 7
                cwd = full[idx:full.find('\n', idx)].strip()
            except (ValueError, IndexError):
                pass

            # Clean output
            stdout = self._clean(full, command, marker)
            return ExecuteResponse(stdout=stdout, stderr="", exit_code=exit_code, cwd=cwd)

    def _clean(self, output: str, command: str, marker: str) -> str:
        lines = []
        for line in output.split('\n'):
            if any(x in line for x in [marker, "__EXIT__", "__CWD__", "_ec=$?"]):
                continue
            if line.strip() == command.strip():
                continue
            if line.strip().startswith("echo "):
                continue
            lines.append(line)
        import re
        result = '\n'.join(lines).strip()
        result = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', result)
        return result.replace('\r', '')


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
    full = path if path.startswith('/') else os.path.join("/workspace", path)
    if os.path.isfile(full):
        return FileResponse(full, filename=os.path.basename(path))
    return JSONResponse(status_code=404, content={"message": "not found"})