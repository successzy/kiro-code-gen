"""
Kiro Code Gen — 后端编排 Pipeline + Kiro 执行的代码生成平台。

架构：
  浏览器 ──SSE──▶ FastAPI Pipeline 编排器 ──ACP──▶ kiro-cli
                     │
                     ├── Stage 1: generate  (Kiro 生成代码 + Dockerfile)
                     ├── Stage 2: build     (Pipeline 执行 docker build)
                     ├── Stage 3: test      (Pipeline 执行 docker run 跑测试)
                     ├── Stage 4: fix_loop  (失败时 Kiro 修复代码，回到 build)
                     ├── Stage 5: deploy    (Pipeline 执行 docker run -d)
                     ├── Stage 6: verify    (Pipeline 轮询 docker inspect)
                     └── Stage 7: manual    (Kiro 生成使用手册)

Kiro 负责：生成代码、修复 bug、写文档
Pipeline 负责：build / test / deploy / verify（直接控制，靠 exit code 判断）
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pipeline")

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from acp import PROTOCOL_VERSION, connect_to_agent, text_block
from acp.interfaces import Client
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    ClientCapabilities,
    CreateTerminalResponse,
    FileSystemCapability,
    Implementation,
    KillTerminalCommandResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    TextContentBlock,
    WaitForTerminalExitResponse,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
KIRO_CLI = os.environ.get("KIRO_CLI", os.path.expanduser("~/.local/bin/kiro-cli"))
PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects"))
BUF_LIMIT = 10 * 1024 * 1024
MAX_FIX_RETRIES = 3
DEPLOY_PORT = 8080

# ---------------------------------------------------------------------------
# Kiro Prompt 模板（只用于 generate / fix / manual）
# ---------------------------------------------------------------------------
PROMPTS = {
    "generate": (
        "请为以下需求生成完整的项目代码，保存到当前工作目录。\n\n"
        "需求：{input}\n\n"
        "要求：\n"
        "1. 创建完整的项目结构，包含构建配置文件\n"
        "2. 包含单元测试\n"
        "3. 确保代码可以直接编译和运行\n"
        "4. 将所有文件保存到磁盘\n"
        "5. 必须创建 Dockerfile，要求：\n"
        "   - 使用多阶段构建\n"
        "   - 包含名为 `test` 的构建阶段，用于运行测试（`docker build --target test` 时执行测试）\n"
        "   - 最终阶段为可运行的生产镜像\n"
        "   - Web 服务必须监听 {port} 端口\n\n"
        "完成后，请列出你创建的所有文件。"
    ),
    "fix": (
        "上一步（{stage}）失败了。以下是命令输出：\n\n"
        "```\n{output}\n```\n\n"
        "请分析错误原因，修复相关代码文件（包括 Dockerfile 如有需要），并简要说明你做了什么修改。"
    ),
    "manual": (
        "项目已成功部署在 Docker 容器中，访问地址：http://localhost:{port}\n\n"
        "请生成一份简洁的使用手册，包含以下内容：\n\n"
        "1. **项目简介**：一句话描述项目功能\n"
        "2. **快速开始**：如何访问或使用（URL、命令等）\n"
        "3. **API/功能列表**：列出主要接口或功能点，附简要说明\n"
        "4. **示例**：给出 1-2 个典型使用示例（curl 命令、代码片段等）\n"
        "5. **配置说明**：如有环境变量或配置项，列出说明\n\n"
        "请用 Markdown 格式输出，保持简洁实用。"
    ),
}

# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
session_queues: dict[str, asyncio.Queue] = {}
session_projects: dict[str, str] = {}
session_context: dict[str, float] = {}
session_text: dict[str, str] = {}
session_files: dict[str, list[str]] = {}
terminals: dict[str, dict] = {}
session_terminals: dict[str, list[str]] = {}
_terminal_counter = 0
_busy = asyncio.Lock()


# ---------------------------------------------------------------------------
# ACP Client（Kiro 回调）
# ---------------------------------------------------------------------------
class KiroClient(Client):

    async def request_permission(self, options, session_id, tool_call, **kwargs: Any):
        for opt in options:
            if opt.kind in ("allow_once", "allow_always"):
                return RequestPermissionResponse(outcome=AllowedOutcome(option_id=opt.option_id, outcome="selected"))
        return RequestPermissionResponse(outcome=AllowedOutcome(option_id=options[0].option_id, outcome="selected"))

    async def session_update(self, session_id, update, **kwargs: Any):
        if isinstance(update, AgentMessageChunk) and isinstance(update.content, TextContentBlock):
            session_text[session_id] = session_text.get(session_id, "") + update.content.text

    async def write_text_file(self, content, path, session_id, **kwargs: Any):
        project_dir = session_projects.get(session_id)
        if not project_dir:
            return None
        full_path = os.path.join(project_dir, path) if not os.path.isabs(path) else path
        full_path = os.path.realpath(full_path)
        if not full_path.startswith(os.path.realpath(project_dir)):
            return None
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)
        rel = os.path.relpath(full_path, project_dir)
        session_files.setdefault(session_id, []).append(rel)
        return None

    async def read_text_file(self, path, session_id, **kwargs: Any):
        project_dir = session_projects.get(session_id)
        if not project_dir:
            raise NotImplementedError
        full_path = os.path.join(project_dir, path) if not os.path.isabs(path) else path
        full_path = os.path.realpath(full_path)
        if not full_path.startswith(os.path.realpath(project_dir)):
            raise NotImplementedError
        try:
            with open(full_path) as f:
                return ReadTextFileResponse(content=f.read())
        except FileNotFoundError:
            raise NotImplementedError

    async def create_terminal(self, command, session_id, args=None, cwd=None, env=None, **kwargs: Any):
        global _terminal_counter
        _terminal_counter += 1
        tid = f"term-{_terminal_counter}"
        work_dir = cwd or session_projects.get(session_id) or "/tmp"
        cmd_parts = [command] + (args or [])
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts, cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        terminals[tid] = {"proc": proc, "output": b"", "session_id": session_id}
        session_terminals.setdefault(session_id, []).append(tid)
        asyncio.create_task(_read_terminal(tid))
        return CreateTerminalResponse(terminal_id=tid)

    async def terminal_output(self, session_id, terminal_id, **kwargs: Any):
        t = terminals.get(terminal_id)
        if not t:
            raise NotImplementedError
        output = t["output"].decode(errors="replace")
        exit_status = None
        if t["proc"].returncode is not None:
            exit_status = TerminalExitStatus(exit_code=t["proc"].returncode)
        return TerminalOutputResponse(output=output, truncated=len(output) > 50000, exit_status=exit_status)

    async def wait_for_terminal_exit(self, session_id, terminal_id, **kwargs: Any):
        t = terminals.get(terminal_id)
        if not t:
            raise NotImplementedError
        await t["proc"].wait()
        return WaitForTerminalExitResponse(exit_code=t["proc"].returncode)

    async def kill_terminal(self, session_id, terminal_id, **kwargs: Any):
        t = terminals.get(terminal_id)
        if t and t["proc"].returncode is None:
            t["proc"].kill()
        return KillTerminalCommandResponse()

    async def release_terminal(self, session_id, terminal_id, **kwargs: Any):
        t = terminals.pop(terminal_id, None)
        if t and t["proc"].returncode is None:
            t["proc"].kill()
        return ReleaseTerminalResponse()

    async def ext_notification(self, method, params, **kwargs: Any):
        if method in ("_kiro.dev/metadata", "kiro.dev/metadata"):
            sid = params.get("sessionId")
            pct = params.get("contextUsagePercentage")
            if sid and pct is not None:
                session_context[sid] = pct

    async def ext_method(self, method, params, **kwargs: Any):
        raise NotImplementedError

    def on_connect(self, conn, **kwargs: Any):
        pass


async def _read_terminal(tid: str):
    t = terminals.get(tid)
    if not t:
        return
    while True:
        chunk = await t["proc"].stdout.read(4096)
        if not chunk:
            break
        t["output"] = (t["output"] + chunk)[-100_000:]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
async def _call_kiro(sid: str, prompt_key: str, **fmt_kwargs) -> tuple[str, int]:
    """调用 Kiro 执行 prompt，返回 (文本回复, 变更文件数)。"""
    prompt = PROMPTS[prompt_key].format(**fmt_kwargs)
    log.info(f"[{prompt_key}] >>> Sending prompt to Kiro ({len(prompt)} chars)")

    session_text[sid] = ""
    session_files[sid] = []

    project_dir = session_projects.get(sid, "")
    before = _file_snapshot(project_dir) if project_dir else {}

    await conn.prompt(session_id=sid, prompt=[text_block(prompt)])

    after = _file_snapshot(project_dir) if project_dir else {}
    changed = sum(1 for f in after if f not in before or after[f] != before[f])

    result = session_text.pop(sid, "")
    total_changed = max(changed, len(session_files.get(sid, [])))

    log.info(f"[{prompt_key}] <<< Kiro replied ({len(result)} chars, {total_changed} files changed)")
    return result, total_changed


async def _run_cmd(cmd: list[str], cwd: str) -> tuple[int, str]:
    """执行命令，返回 (exit_code, 合并的stdout+stderr输出)。"""
    log.info(f"[cmd] {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace")[-5000:]  # 保留最后 5000 字符
    log.info(f"[cmd] exit_code={proc.returncode}, output={output[:200]!r}")
    return proc.returncode, output


async def _check_container(container_name: str, max_checks: int = 15, interval: int = 2) -> tuple[bool, str]:
    """轮询 Docker 容器状态，返回 (是否运行中, 状态描述)。"""
    for i in range(max_checks):
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Status}}:{{.State.ExitCode}}", container_name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            if i < max_checks - 1:
                await asyncio.sleep(interval)
                continue
            return False, "容器不存在"
        status_line = stdout.decode().strip()
        parts = status_line.split(":")
        state = parts[0] if parts else ""
        exit_code = int(parts[1]) if len(parts) > 1 else -1
        log.info(f"[verify] {container_name}: {state} (exit_code={exit_code}), check {i+1}/{max_checks}")
        if state == "running":
            return True, "容器运行中"
        if state == "exited":
            return exit_code == 0, f"容器已退出 (exit_code={exit_code})"
        await asyncio.sleep(interval)
    return False, "容器状态检查超时"


def _file_snapshot(project_dir: str) -> dict[str, float]:
    skip_dirs = {".git", "__pycache__", "node_modules", "target", "build", "dist", ".pytest_cache", "venv"}
    snap = {}
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for f in filenames:
            if not f.startswith("."):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, project_dir)
                snap[rel] = os.path.getmtime(full)
    return snap


def _scan_project_files(project_dir: str) -> list[str]:
    skip_dirs = {".git", "__pycache__", "node_modules", "target", "build", "dist", ".pytest_cache", "venv"}
    files = []
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for f in filenames:
            if not f.startswith("."):
                files.append(os.path.relpath(os.path.join(root, f), project_dir))
    return files


async def _emit(queue: asyncio.Queue, step: str, status: str, message: str):
    log.info(f"[STEP] {step}: {status} - {message}")
    await queue.put(json.dumps({"type": "step", "step": step, "status": status, "message": message}))


# ---------------------------------------------------------------------------
# Pipeline 编排器
# ---------------------------------------------------------------------------
async def run_pipeline(sid: str, user_prompt: str, queue: asyncio.Queue):
    """Pipeline: generate(Kiro) → build(docker) → test(docker) → fix(Kiro) → deploy(docker) → verify(docker) → manual(Kiro)"""
    try:
        project_dir = session_projects.get(sid, "")
        project_id = os.path.basename(project_dir)

        # ===== Stage 1: 代码生成（Kiro） =====
        await _emit(queue, "generate", "running", "Kiro 正在生成代码...")
        _, changed = await _call_kiro(sid, "generate", input=user_prompt, port=DEPLOY_PORT)

        all_files = _scan_project_files(project_dir) if project_dir else []
        file_count = max(changed, len(all_files))

        if file_count == 0:
            await _emit(queue, "generate", "done", "未生成文件")
            return

        await _emit(queue, "generate", "done", f"代码生成完成（{file_count} 个文件）")

        # ===== Stage 2-4: build → test → fix loop =====
        last_output = ""
        last_stage = ""
        for attempt in range(MAX_FIX_RETRIES + 1):

            # -- build: docker build --
            await _emit(queue, "build", "running", "正在构建 Docker 镜像...")
            exit_code, output = await _run_cmd(
                ["docker", "build", "-t", project_id, "."], cwd=project_dir,
            )
            if exit_code != 0:
                await _emit(queue, "build", "failed", f"构建失败 (exit_code={exit_code})")
                last_output = output
                last_stage = "docker build"
                if attempt < MAX_FIX_RETRIES:
                    await _emit(queue, "fix", "running", f"Kiro 正在修复（{attempt + 1}/{MAX_FIX_RETRIES}）...")
                    _, fix_changed = await _call_kiro(sid, "fix", stage=last_stage, output=last_output[-3000:])
                    await _emit(queue, "fix", "done", f"修复完成（{fix_changed} 个文件）")
                    continue
                else:
                    await _emit(queue, "fix", "failed", "修复失败，已达最大重试次数")
                    break
            await _emit(queue, "build", "done", "镜像构建成功")

            # -- test: docker build --target test --
            await _emit(queue, "test", "running", "正在运行测试...")
            exit_code, output = await _run_cmd(
                ["docker", "build", "--target", "test", "-t", f"{project_id}-test", "."], cwd=project_dir,
            )
            if exit_code != 0:
                await _emit(queue, "test", "failed", f"测试失败 (exit_code={exit_code})")
                last_output = output
                last_stage = "docker build --target test"
                if attempt < MAX_FIX_RETRIES:
                    await _emit(queue, "fix", "running", f"Kiro 正在修复（{attempt + 1}/{MAX_FIX_RETRIES}）...")
                    _, fix_changed = await _call_kiro(sid, "fix", stage=last_stage, output=last_output[-3000:])
                    await _emit(queue, "fix", "done", f"修复完成（{fix_changed} 个文件）")
                    continue
                else:
                    await _emit(queue, "fix", "failed", "修复失败，已达最大重试次数")
                    break
            await _emit(queue, "test", "done", "测试全部通过")

            # ===== Stage 5: deploy: docker run -d =====
            await _emit(queue, "deploy", "running", "正在部署...")
            # 清理旧容器
            await _run_cmd(["docker", "rm", "-f", project_id], cwd=project_dir)
            exit_code, output = await _run_cmd(
                ["docker", "run", "-d", "-p", f"{DEPLOY_PORT}:{DEPLOY_PORT}", "--name", project_id, project_id],
                cwd=project_dir,
            )
            if exit_code != 0:
                await _emit(queue, "deploy", "failed", f"部署失败 (exit_code={exit_code})")
                break
            await _emit(queue, "deploy", "done", f"容器已启动")

            # ===== Stage 6: verify =====
            await _emit(queue, "verify", "running", "正在验证容器状态...")
            container_ok, container_msg = await _check_container(project_id)
            if not container_ok:
                await _emit(queue, "verify", "failed", f"验证失败: {container_msg}")
                break
            await _emit(queue, "verify", "done", f"验证通过: http://localhost:{DEPLOY_PORT}")

            # ===== Stage 7: manual（Kiro） =====
            await _emit(queue, "manual", "running", "Kiro 正在生成使用手册...")
            result, _ = await _call_kiro(sid, "manual", port=DEPLOY_PORT)
            await _emit(queue, "manual", "done", "使用手册已生成")
            await queue.put(json.dumps({"type": "manual", "content": result}))
            break

        # Context 使用量
        ctx = session_context.get(sid, 0.0)
        await queue.put(json.dumps({"type": "meta", "context_pct": round(ctx, 2)}))

    except Exception as e:
        log.exception("Pipeline error")
        await queue.put(json.dumps({"type": "error", "message": f"流水线异常: {e}"}))
    finally:
        for tid in session_terminals.pop(sid, []):
            t = terminals.pop(tid, None)
            if t and t["proc"].returncode is None:
                t["proc"].kill()
        session_text.pop(sid, None)
        session_files.pop(sid, None)
        await queue.put(None)


# ---------------------------------------------------------------------------
# 全局 ACP 连接
# ---------------------------------------------------------------------------
client = KiroClient()
conn = None


async def start_kiro():
    global conn
    proc = await asyncio.create_subprocess_exec(
        KIRO_CLI, "acp", "--trust-all-tools",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=BUF_LIMIT,
    )
    conn = connect_to_agent(client, proc.stdin, proc.stdout)
    await conn.initialize(
        protocol_version=PROTOCOL_VERSION,
        client_capabilities=ClientCapabilities(
            fs=FileSystemCapability(read_text_file=True, write_text_file=True),
            terminal=True,
        ),
        client_info=Implementation(name="kiro-code-gen", version="5.0.0"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    await start_kiro()
    yield
    for t in terminals.values():
        if t["proc"].returncode is None:
            t["proc"].kill()
    if conn:
        conn.close()


# ---------------------------------------------------------------------------
# FastAPI 路由
# ---------------------------------------------------------------------------
app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return f.read()


@app.post("/session/new")
async def new_session():
    project_id = uuid.uuid4().hex[:8]
    project_dir = os.path.join(PROJECTS_DIR, project_id)
    os.makedirs(project_dir, exist_ok=True)
    session = await conn.new_session(cwd=project_dir, mcp_servers=[])
    sid = session.session_id
    session_queues[sid] = asyncio.Queue()
    session_projects[sid] = project_dir
    return JSONResponse({"session_id": sid, "project_id": project_id})


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    user_prompt = body.get("prompt", "")
    sid = body.get("session_id", "")

    queue = session_queues.get(sid)
    if not queue:
        return JSONResponse({"error": "invalid session_id"}, status_code=400)

    if _busy.locked():
        return JSONResponse({"error": "有其他用户正在使用，请稍后重试"}, status_code=429)

    while not queue.empty():
        queue.get_nowait()

    async def run():
        async with _busy:
            await run_pipeline(sid, user_prompt, queue)

    asyncio.create_task(run())

    async def generate():
        while True:
            item = await queue.get()
            if item is None:
                yield "data: [DONE]\n\n"
                break
            yield f"data: {item}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
