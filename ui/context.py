"""Template context processor — exposes the current course id (if any) to all templates."""
from __future__ import annotations

from django.urls import resolve, Resolver404


def current_cid(request):
    try:
        match = resolve(request.path_info)
        cid = match.kwargs.get("cid")
        if cid:
            return {"current_cid": int(cid)}
    except (Resolver404, ValueError):
        pass
    return {"current_cid": None}
