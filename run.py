import uvicorn
from uvicorn.config import LOGGING_CONFIG

from app.settings import settings

if __name__ == "__main__":
    LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    LOGGING_CONFIG["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    LOGGING_CONFIG["formatters"]["access"][
        "fmt"
    ] = '%(asctime)s - %(levelname)s - %(client_addr)s - "%(request_line)s" %(status_code)s'
    LOGGING_CONFIG["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"

    host = (getattr(settings, "UVICORN_HOST", None) or "127.0.0.1").strip() or "127.0.0.1"
    uvicorn.run(
        "app:app",
        host=host,
        port=9999,
        reload=bool(settings.DEBUG),
        log_config=LOGGING_CONFIG,
    )
