from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .job_tools import JobTools
from .scheduler_service import SchedulerService


def build_server(scheduler: SchedulerService) -> MCPServer:
    mcp = MCPServer("gh-cron-mcp")
    tools = JobTools(scheduler)

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    mcp.add_tool(tools.add_job, name="add_job", description="Schedule a GitHub repo's script to run on a recurring cron schedule.")
    mcp.add_tool(tools.remove_job, name="remove_job", description="Remove a scheduled job by name.")
    mcp.add_tool(tools.list_jobs, name="list_jobs", description="List all scheduled jobs with next run time and last status.")
    mcp.add_tool(tools.run_job_now, name="run_job_now", description="Trigger an immediate out-of-schedule run of a job.")
    mcp.add_tool(tools.get_job_logs, name="get_job_logs", description="Get the tail of a job's execution log.")
    return mcp
