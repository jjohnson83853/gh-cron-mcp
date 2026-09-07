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
    mcp.add_tool(tools.list_jobs, name="list_jobs", description="List all scheduled jobs with cron schedule, paused state, next run time, and last status.")
    mcp.add_tool(tools.run_job_now, name="run_job_now", description="Trigger an immediate out-of-schedule run of a job.")
    mcp.add_tool(tools.get_job_logs, name="get_job_logs", description="Get the tail of a job's execution log.")
    mcp.add_tool(tools.update_job_schedule, name="update_job_schedule", description="Update an existing job's cron schedule by name. Only the schedule changes; repo, entrypoint, and env stay as-is.")
    mcp.add_tool(tools.pause_job, name="pause_job", description="Pause a job's schedule without deleting the job or its history. Manual run_job_now still works while paused.")
    mcp.add_tool(tools.resume_job, name="resume_job", description="Resume a paused job's schedule.")
    return mcp
