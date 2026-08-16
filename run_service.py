import os

import uvicorn

from config import settings
from main import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=int(os.environ["HTTP_PLATFORM_PORT"]),
        workers=1,
    )
