# Kiro Code Gen

基于 [Kiro CLI](https://kiro.dev/) + [ACP 协议](https://agentclientprotocol.com/) 的自动化代码生成与部署平台。

输入一句需求描述，自动完成：代码生成 → Docker 构建 → 测试 → 部署 → 验证 → 使用手册生成。

## 架构

```
浏览器 ──SSE──▶ FastAPI Pipeline 编排器 ──ACP──▶ kiro-cli ──▶ Claude
                     │
                     ├── Stage 1: generate   Kiro 生成代码 + Dockerfile
                     ├── Stage 2: build      Pipeline 执行 docker build
                     ├── Stage 3: test       Pipeline 执行 docker build --target test
                     ├── Stage 4: fix loop   build/test 失败时 Kiro 修复代码，重试最多 3 次
                     ├── Stage 5: deploy     Pipeline 执行 docker run -d
                     ├── Stage 6: verify     Pipeline 轮询 docker inspect 检查容器状态
                     └── Stage 7: manual     Kiro 生成使用手册
```

**职责分离**：
- **Kiro 负责**：生成代码、修复 bug、写文档（Stage 1/4/7）
- **Pipeline 负责**：build / test / deploy / verify，直接执行命令，靠 exit code 判断成败（Stage 2/3/5/6）

## 前置条件

- Python 3.10+
- Docker
- [Kiro CLI](https://kiro.dev/docs/cli/) 已安装并登录（`kiro-cli login`）

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
uvicorn app:app --host 0.0.0.0 --port 3333

# 打开浏览器
open http://localhost:3333
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KIRO_CLI` | `~/.local/bin/kiro-cli` | kiro-cli 可执行文件路径 |
| `PROJECTS_DIR` | `./projects` | 项目文件存储目录 |

## 配置常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `MAX_FIX_RETRIES` | 3 | build/test 失败时最大修复重试次数 |
| `DEPLOY_PORT` | 8080 | 生成的服务部署端口 |

## API

### `POST /session/new`

创建新的 ACP session 和项目目录。

```json
// 响应
{"session_id": "xxx", "project_id": "abc12345"}
```

### `POST /chat`

提交需求，触发完整 Pipeline，通过 SSE 流式返回每个阶段的进度。

**请求体**：
```json
{"session_id": "xxx", "prompt": "用 Node.js Express 写一个笔记 API，支持 CRUD，包含单元测试"}
```

**SSE 响应**：
```
data: {"type": "step", "step": "generate", "status": "running", "message": "Kiro 正在生成代码..."}
data: {"type": "step", "step": "generate", "status": "done", "message": "代码生成完成（6 个文件）"}
data: {"type": "step", "step": "build", "status": "done", "message": "镜像构建成功"}
data: {"type": "step", "step": "test", "status": "done", "message": "测试全部通过"}
data: {"type": "step", "step": "deploy", "status": "done", "message": "容器已启动"}
data: {"type": "step", "step": "verify", "status": "done", "message": "验证通过: http://localhost:8080"}
data: {"type": "step", "step": "manual", "status": "done", "message": "使用手册已生成"}
data: {"type": "manual", "content": "..."}
data: {"type": "meta", "context_pct": 5.07}
data: [DONE]
```

## 项目结构

```
├── app.py               # FastAPI 服务 + Pipeline 编排 + ACP Client
├── static/index.html    # 前端页面（SSE 流式展示）
├── requirements.txt     # Python 依赖
├── projects/            # 生成的项目文件（每个项目一个子目录）
└── README.md
```

## 已测试语言

| 语言 | 示例项目 | 结果 |
|------|----------|------|
| Python | Flask 天气查询 API | 一次通过 |
| Node.js | Express 笔记 CRUD API | 一次通过 |
| Java | Spring Boot 图书管理 API | 一次通过 |
| Go | HTTP 计算器服务 | 一次通过 |
| Rust | Actix-web 短链接服务 | 经 fix loop 修复后通过 |

## 技术细节

### ACP 协议

通过 ACP Python SDK 的 `connect_to_agent()` 与 kiro-cli 建立 JSON-RPC 连接。主要使用的方法：

- `initialize` — 握手，交换能力（文件读写 + 终端）
- `session/new` — 创建会话，指定项目工作目录
- `session/prompt` — 发送 prompt 给 Kiro

回调接口：
- `write_text_file` / `read_text_file` — Kiro 读写项目文件
- `create_terminal` / `wait_for_terminal_exit` — Kiro 执行终端命令
- `session_update` — 接收 Kiro 的流式文本回复
- `ext_notification` — 接收 context 使用量等元数据

### Pipeline 与 Kiro 的分工

v5.0 之前，build/test/deploy 都交给 Kiro 执行并从自然语言回复中解析成败，导致判断不可靠。

v5.0 改为 Pipeline 直接执行 Docker 命令：
- `docker build -t <project_id> .` — 构建生产镜像
- `docker build --target test -t <project_id>-test .` — 运行测试
- `docker run -d -p 8080:8080 --name <project_id> <project_id>` — 部署
- `docker inspect` — 验证容器状态

成败完全靠 exit code 判断，100% 可靠。

### Dockerfile 要求

generate 阶段要求 Kiro 生成的 Dockerfile 必须：
1. 使用多阶段构建
2. 包含名为 `test` 的构建阶段（`docker build --target test` 时执行测试）
3. 最终阶段为可运行的生产镜像
4. Web 服务监听 8080 端口

## 已知限制

- 单用户模式：同一时间只能运行一个 Pipeline（`_busy` 锁）
- 端口固定：所有生成的服务统一部署在 8080 端口
- 权限自动批准（`--trust-all-tools`），生产环境应实现交互式审批
- Kiro CLI 暂不通过 ACP 返回精确的 token 用量
