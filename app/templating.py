from __future__ import annotations

from typing import Any, Mapping

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.responses import Response


class WarehouseJinja2Templates(Jinja2Templates):
    """Keep legacy Warehouse call sites compatible with Starlette 1.x.

    Starlette 1.0 made ``request`` the first required TemplateResponse
    argument. Warehouse historically stores it in the context mapping. This
    adapter is the single migration boundary until templates are modularized.
    """

    def TemplateResponse(  # noqa: N802
        self,
        name: str,
        context: dict[str, Any],
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> Response:
        request = context.get("request")
        if not isinstance(request, Request):
            raise RuntimeError("Template context must contain a Request instance")
        return super().TemplateResponse(
            request,
            name,
            context,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
