"""Zeabur MCP Server —— HTTP/SSE 双模式

环境变量:
    ZEABUR_TOKEN    Zeabur API Token (zat_ 开头，必填)
    MCP_AUTH_TOKEN  可选。设了之后，客户端必须在请求头里带 x-mcp-auth: <值>，
                    否则一律 401。不设则不拦（裸奔，仅限自己知道域名时用）
    PORT            监听端口 (Zeabur 会自动注入)

对外端点:
    GET  /health  健康检查（永远不需要口令）
    POST /mcp     Streamable HTTP（推荐）→ 客户端填 https://你的域名/mcp
    GET  /sse     SSE 传输（备用）      → 客户端填 https://你的域名/sse

依赖: 见 requirements.txt（注意 mcp 必须 <2）
"""

import os
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Mount

ZEABUR_TOKEN = os.environ.get("ZEABUR_TOKEN", "").strip()
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", 8765))
GRAPHQL_URL = "https://api.zeabur.com/graphql"

mcp = FastMCP("Zeabur")


# ═══════════════════════════════════════════
# GraphQL 底层
# ═══════════════════════════════════════════

async def gql(query: str, variables: dict = None) -> dict:
    """向 Zeabur GraphQL API 发一次请求。成功返回 data，失败返回 {'error': ...}。"""
    if not ZEABUR_TOKEN:
        return {"error": "ZEABUR_TOKEN 未设置"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ZEABUR_TOKEN}",
    }
    body = {"query": query}
    if variables:
        body["variables"] = variables
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(GRAPHQL_URL, json=body, headers=headers)
    except Exception as e:
        return {"error": f"请求失败: {e}"}
    try:
        data = resp.json()
    except Exception:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    if "errors" in data:
        return {"error": data["errors"]}
    return data.get("data") or {}


# ═══════════════════════════════════════════
# 只读工具
# ═══════════════════════════════════════════

@mcp.tool()
async def list_projects() -> str:
    """列出所有 Zeabur 项目，返回 project_id、项目名、区域和环境（含 environment_id）。
    调用其他工具前，先用这个拿 project_id / environment_id。"""
    data = await gql("""
        query {
          projects(skip: 0, limit: 100) {
            edges {
              node {
                _id
                name
                region { code name }
                environments { _id name }
              }
            }
          }
        }
    """)
    if "error" in data:
        return f"❌ {data['error']}"
    edges = data.get("projects", {}).get("edges", [])
    if not edges:
        return "📭 没有项目"
    lines = []
    for e in edges:
        n = e["node"]
        region = (n.get("region") or {}).get("name", "?")
        lines.append(f"📦 {n['name']}")
        lines.append(f"   project_id: {n['_id']}  区域: {region}")
        for env in n.get("environments") or []:
            lines.append(f"   🌍 环境 {env['name']}  environment_id: {env['_id']}")
    return "\n".join(lines)


@mcp.tool()
async def list_services(project_id: str) -> str:
    """列出指定项目下的所有服务，返回服务名和 service_id。"""
    data = await gql("""
        query ListServices($projectID: ObjectID!) {
          services(projectID: $projectID) {
            edges { node { _id name template createdAt status } }
          }
        }
    """, {"projectID": project_id})
    if "error" in data:
        return f"❌ {data['error']}"
    edges = data.get("services", {}).get("edges", [])
    if not edges:
        return "📭 没有服务"
    lines = []
    for e in edges:
        n = e["node"]
        lines.append(f"🔧 {n['name']}  service_id: {n['_id']}  状态: {n.get('status')}")
    return "\n".join(lines)


@mcp.tool()
async def get_service(service_id: str) -> str:
    """获取服务详情：域名、模板、最近几次部署。"""
    data = await gql("""
        query GetService($id: ObjectID!) {
          service(_id: $id) {
            _id name template createdAt status
            domains { _id domain status isGenerated }
            deployments { _id status createdAt }
          }
        }
    """, {"id": service_id})
    if "error" in data:
        return f"❌ {data['error']}"
    svc = data.get("service") or {}
    if not svc:
        return "📭 未找到该服务"
    lines = [f"🔧 {svc['name']}  service_id: {svc['_id']}"]
    lines.append(f"   状态: {svc.get('status')}  模板: {svc.get('template')}")
    for d in svc.get("domains") or []:
        lines.append(f"   🌐 {d['domain']}  ({d.get('status')})")
    for d in (svc.get("deployments") or [])[:3]:
        lines.append(f"   📋 {d['status']}  deployment_id: {d['_id']}")
    return "\n".join(lines)


@mcp.tool()
async def get_env_vars(service_id: str, environment_id: str) -> str:
    """获取指定服务的所有环境变量。"""
    data = await gql("""
        query ServiceVars($serviceID: ObjectID!, $environmentID: ObjectID!) {
          service(_id: $serviceID) {
            variables(environmentID: $environmentID) { key value }
          }
        }
    """, {"serviceID": service_id, "environmentID": environment_id})
    if "error" in data:
        return f"❌ {data['error']}"
    variables = (data.get("service") or {}).get("variables") or []
    if not variables:
        return "📭 没有环境变量"
    lines = ["📋 环境变量:"]
    for v in variables:
        lines.append(f"   {v['key']} = {v['value']}")
    return "\n".join(lines)


@mcp.tool()
async def get_deployments(service_id: str, environment_id: str) -> str:
    """获取服务的部署历史（含 deployment_id）。查构建日志前先调这个。"""
    data = await gql("""
        query GetDeployments($serviceID: ObjectID!, $environmentID: ObjectID!) {
          deployments(serviceID: $serviceID, environmentID: $environmentID) {
            edges { node { _id status createdAt } }
          }
        }
    """, {"serviceID": service_id, "environmentID": environment_id})
    if "error" in data:
        return f"❌ {data['error']}"
    edges = data.get("deployments", {}).get("edges", [])
    if not edges:
        return "📭 没有部署记录"
    lines = []
    for e in edges:
        n = e["node"]
        ts = (n.get("createdAt") or "")[:19]
        lines.append(f"[{ts}] {n['status']}  deployment_id: {n['_id']}")
    return "\n".join(lines)


@mcp.tool()
async def get_build_logs(deployment_id: str) -> str:
    """获取指定部署的构建日志（依赖安装、编译过程）。"""
    data = await gql("""
        query BuildLogs($deploymentID: ObjectID!) {
          buildLogs(deploymentID: $deploymentID) { message timestamp }
        }
    """, {"deploymentID": deployment_id})
    if "error" in data:
        return f"❌ {data['error']}"
    logs = data.get("buildLogs") or []
    if not logs:
        return "📭 没有构建日志"
    lines = [f"📋 构建日志（共 {len(logs)} 条，显示最后 200 条）"]
    for entry in logs[-200:]:
        ts = (entry.get("timestamp") or "")[:19]
        lines.append(f"[{ts}] {entry.get('message', '')}")
    return "\n".join(lines)


@mcp.tool()
async def get_runtime_logs(service_id: str, environment_id: str, project_id: str = "") -> str:
    """获取服务的运行时日志（启动输出、报错等）。
    service_id 从 list_services 拿，environment_id 从 list_projects 拿；
    project_id 可选，填了接口兼容性更好。"""
    query = """
        query RuntimeLogs($serviceID: ObjectID!, $environmentID: ObjectID!, $projectID: ObjectID) {
          runtimeLogs(serviceID: $serviceID, environmentID: $environmentID, projectID: $projectID) {
            message timestamp
          }
        }
    """
    variables = {"serviceID": service_id, "environmentID": environment_id}
    if project_id:
        variables["projectID"] = project_id
    data = await gql(query, variables)
    if "error" in data:
        return f"❌ {data['error']}"
    logs = data.get("runtimeLogs") or []
    if not logs:
        return "📭 没有运行时日志"
    lines = [f"📋 运行时日志（共 {len(logs)} 条，显示最后 200 条）"]
    for entry in logs[-200:]:
        ts = (entry.get("timestamp") or "")[:19]
        lines.append(f"[{ts}] {entry.get('message', '')}")
    return "\n".join(lines)


# ═══════════════════════════════════════════
# 写操作工具
# ═══════════════════════════════════════════

@mcp.tool()
async def create_project(name: str) -> str:
    """新建一个 Zeabur 项目。"""
    data = await gql("""
        mutation CreateProject($name: String!) {
          createProject(name: $name) { _id name }
        }
    """, {"name": name})
    if "error" in data:
        return f"❌ 创建项目失败: {data['error']}"
    p = data.get("createProject") or {}
    if not p:
        return "❌ 创建项目失败（接口没返回内容）"
    return f"✅ 项目已创建: {p['name']}  project_id: {p['_id']}"


@mcp.tool()
async def create_service(name: str, project_id: str) -> str:
    """在指定项目中新建一个服务（PREBUILT 模板，随后可用 bind_git_repo 绑仓库）。"""
    data = await gql("""
        mutation CreateService($name: String!, $projectID: ObjectID!) {
          createService(name: $name, template: PREBUILT_V2, projectID: $projectID) {
            _id name status
          }
        }
    """, {"name": name, "projectID": project_id})
    if "error" in data:
        return f"❌ 创建服务失败: {data['error']}"
    s = data.get("createService") or {}
    if not s:
        return "❌ 创建服务失败（接口没返回内容）"
    return f"✅ 服务已创建: {s['name']}  service_id: {s['_id']}  状态: {s.get('status')}"


@mcp.tool()
async def bind_git_repo(service_id: str, repo_url: str, branch: str = "main") -> str:
    """把 GitHub 仓库绑定到服务，会立刻触发一次部署。
    repo_url 形如 https://github.com/用户名/仓库名"""
    data = await gql("""
        mutation BindGitRepo($serviceID: ObjectID!, $url: String!, $branch: String!) {
          bindGitRepository(serviceID: $serviceID, url: $url, branch: $branch) {
            _id name status
          }
        }
    """, {"serviceID": service_id, "url": repo_url, "branch": branch})
    if "error" in data:
        return f"❌ 绑定仓库失败: {data['error']}"
    s = data.get("bindGitRepository") or {}
    if not s:
        return "❌ 绑定仓库失败（接口没返回内容）"
    return f"✅ 已绑定: {s['name']}  状态: {s.get('status')}（已触发部署，用 get_deployments 看进度）"


@mcp.tool()
async def set_env_var(service_id: str, environment_id: str, key: str, value: str) -> str:
    """给服务设置一个环境变量（已存在则更新）。"""
    data = await gql("""
        mutation SetEnvVar($serviceID: ObjectID!, $environmentID: ObjectID!, $key: String!, $value: String!) {
          createEnvironmentVariable(serviceID: $serviceID, environmentID: $environmentID, key: $key, value: $value) {
            key value
          }
        }
    """, {"serviceID": service_id, "environmentID": environment_id, "key": key, "value": value})
    if "error" in data:
        return f"❌ 设置环境变量失败: {data['error']}"
    v = data.get("createEnvironmentVariable") or {}
    if not v:
        return "❌ 设置环境变量失败（接口没返回内容）"
    return f"✅ {v['key']} = {v['value']}"


@mcp.tool()
async def delete_env_var(service_id: str, environment_id: str, key: str) -> str:
    """删除服务上的某个环境变量。"""
    data = await gql("""
        mutation DeleteEnvVar($serviceID: ObjectID!, $environmentID: ObjectID!, $key: String!) {
          deleteSingleEnvironmentVariable(serviceID: $serviceID, environmentID: $environmentID, key: $key) {
            key
          }
        }
    """, {"serviceID": service_id, "environmentID": environment_id, "key": key})
    if "error" in data:
        return f"❌ 删除环境变量失败: {data['error']}"
    return f"✅ 已删除 {key}"


@mcp.tool()
async def zeabur_graphql(query: str, variables_json: str = "") -> str:
    """万能口子：把你的 GraphQL 语句原样打到 Zeabur 官方 API 上。
    用在：查本文件没封装的字段、内省 schema、执行没封装的写操作。
    例：query 填 '{ __schema { mutationType { fields { name } } } }' 可以列出全部可用的写操作。
    variables_json 是可选的变量对象（JSON 字符串）。
    这是全权限通道，能做你 Token 权限内的任何事，调用前请想清楚。"""
    variables = None
    if variables_json.strip():
        try:
            variables = json.loads(variables_json)
        except Exception as e:
            return f"❌ variables_json 不是合法 JSON: {e}"
    data = await gql(query, variables)
    if "error" in data:
        return f"❌ {data['error']}"
    return json.dumps(data, ensure_ascii=False, indent=2)[:8000]


# ═══════════════════════════════════════════
# 传输层
# ═══════════════════════════════════════════
#
# 这一段踩过的坑，按顺序记下来：
#   1) streamable_http_app() 内部路径默认就是 "/mcp"，挂载点必须用 "/"，
#      否则真实端点是 /mcp/mcp，客户端找 /mcp 只会拿到 404。
#   2) session_manager 必须在 lifespan 里 run() 起来，
#      否则每个请求都抛 "Task group is not initialized" → 500。
#   3) 它默认开着 DNS-rebinding 保护，只认 Host 是 localhost 的请求，
#      挂在真实域名后面一律 421 "Invalid Host header"。这里显式关掉。

mcp_http_app = mcp.streamable_http_app()


def _disable_dns_rebinding_guard() -> str:
    """把 Streamable HTTP 的 Host / Origin 门禁关掉。

    SDK 默认开了 DNS-rebinding 保护：只接受 Host 头是 localhost 的请求。
    服务部署在真实域名后面时，每个请求都会被 421 Invalid Host header 打回。
    这里把 session manager 的安全设置换成「关闭」，让真域名能进来。
    """
    manager = None
    for attr in ("session_manager", "_session_manager"):
        try:
            candidate = getattr(mcp, attr, None)
        except Exception:
            candidate = None
        if candidate is not None:
            manager = candidate
            break
    if manager is None:
        return "session manager 未就绪，跳过"
    try:
        manager.security_settings = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    except Exception as e:
        return f"关闭失败: {e}"
    return "已关闭"


_TRANSPORT_SECURITY_STATE = _disable_dns_rebinding_guard()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        session_manager = getattr(mcp, "session_manager", None)
    except Exception:
        session_manager = None
    if session_manager is None:
        yield
        return
    async with session_manager.run():
        yield


app = FastAPI(title="Zeabur MCP", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuthGate:
    """可选的访问口令。

    故意写成纯 ASGI 中间件，而不是 @app.middleware("http")：
    BaseHTTPMiddleware 会插手长连接，容易把 /sse 这种流式响应搞死。

    不设 MCP_AUTH_TOKEN 时完全不拦；
    设了之后，客户端必须在请求头里带 x-mcp-auth: <口令>，否则 401。
    /health 永远放行，方便确认服务活着。
    """

    def __init__(self, app, token: str = ""):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.token:
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") == "/health":
            await self.app(scope, receive, send)
            return
        provided = ""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"x-mcp-auth":
                provided = value.decode("latin-1")
                break
        if provided != self.token:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(AuthGate, token=MCP_AUTH_TOKEN)

# ── SSE（备用通道，手写传输，稳定） ──
_sse = SseServerTransport("/messages/")
app.router.routes.append(Mount("/messages", app=_sse.handle_post_message))


@app.get("/sse")
async def sse_handler(request: Request):
    async with _sse.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "token_set": bool(ZEABUR_TOKEN),
        "auth_required": bool(MCP_AUTH_TOKEN),
        "dns_rebinding_guard": _TRANSPORT_SECURITY_STATE,
    })


# ── Streamable HTTP（主通道） ──
# 挂到根上，真实端点 = streamable_http_app 内部的 /mcp
app.mount("/", mcp_http_app)


if __name__ == "__main__":
    import uvicorn
    print("🦊 Zeabur MCP Server")
    print(f"   Streamable HTTP: http://0.0.0.0:{PORT}/mcp")
    print(f"   SSE:             http://0.0.0.0:{PORT}/sse")
    print(f"   Token:            {'✅ 已设置' if ZEABUR_TOKEN else '❌ 未设置'}")
    print(f"   访问口令:         {'✅ 已设置' if MCP_AUTH_TOKEN else '⚠️ 未设置（不拦）'}")
    print(f"   DNS 重绑定门禁:   {_TRANSPORT_SECURITY_STATE}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
