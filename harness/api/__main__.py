"""Run the Harness API with ``python -m api``."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "api.main:app",
        host=os.getenv("HARNESS_API_HOST", "127.0.0.1"),
        port=int(os.getenv("HARNESS_API_PORT", "8000")),
        log_level=os.getenv("HARNESS_API_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
