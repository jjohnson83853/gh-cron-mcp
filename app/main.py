import os

from .logging_config import setup_logging
from .scheduler_service import SchedulerService
from .server import build_server

logger = setup_logging()


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    scheduler = SchedulerService(github_token=os.environ.get("GITHUB_TOKEN"))
    mcp = build_server(scheduler)
    logger.info("gh-cron-mcp starting", extra={"extra_fields": {"port": port}})
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
