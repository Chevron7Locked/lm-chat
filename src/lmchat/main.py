# SPDX-License-Identifier: Apache-2.0
import uvicorn

from lmchat.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "lmchat.app:app",
        host=settings.lm_chat_host,
        port=settings.lm_chat_port,
        log_level=settings.lm_chat_log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
