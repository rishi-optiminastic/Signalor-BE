import logging

from rest_framework.views import exception_handler

logger = logging.getLogger("apps")


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is not None:
        response.data["status_code"] = response.status_code

    view = context.get("view")
    if view:
        logger.error(
            "Exception in %s: %s",
            view.__class__.__name__,
            str(exc),
            exc_info=True,
        )

    return response
