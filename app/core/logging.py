from __future__ import annotations

import logging
from pythonjsonlogger import jsonlogger

from app.utils.context import request_id_ctx, org_id_ctx
from app.core.config import Settings


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        record.org_id = org_id_ctx.get() or "-"
        return True


def setup_logging(settings: Settings) -> None:
    logger = logging.getLogger()
    logger.setLevel(settings.LOG_LEVEL)

    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(org_id)s"
    )
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())

    logger.handlers = [handler]
