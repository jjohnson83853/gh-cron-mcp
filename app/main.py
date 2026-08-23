import os

from .scheduler_service import SchedulerService
from .server import build_server


def main() -> None:
    scheduler = SchedulerService(github_token=os.environ.get("GITHUB_TOKEN"))
    mcp = build_server(scheduler)
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
