"""Routes of the bookmarklet: its page and the capture API.

Usage:
    import job_radar.web.routes.capture as routes_capture
"""

import datetime as dt
import json
import pathlib
import re
import typing as t

import fastapi
import fastapi.responses as fastapi_responses

import job_radar.sources.linkedin.capture as linkedin_capture
import job_radar.web.context as web_context
import job_radar.web.deps as web_deps
import job_radar.web.templating as web_templating

router = fastapi.APIRouter()
# What the bookmarklet reads, shared with the Chrome fill (#247): one reader for both.
CAPTURE_READ = (pathlib.Path(__file__).parents[2] / "sources" / "capture_read.js").read_text()
BOOKMARKLETS = {
    name: (pathlib.Path(__file__).parent.parent / f"{name}.js")
    .read_text()
    .replace("__READ__", CAPTURE_READ)
    for name in ("bookmarklet", "bookmarklet_debug")
}


def bookmarklet(base_url: str, name: str = "bookmarklet") -> str:
    """The bookmarklet as a javascript: URL; comments and indentation stripped."""
    code = re.sub(r"/\*.*?\*/", "", BOOKMARKLETS[name], flags=re.S)
    code = " ".join(line.strip() for line in code.splitlines() if line.strip())
    return "javascript:" + code.replace("__BASE__", base_url.rstrip("/"))


def _save_debug(path: pathlib.Path, text: str) -> None:
    # Local diagnostics only: data/ is git-ignored and may hold page content.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@router.get("/capture", response_class=fastapi_responses.HTMLResponse)
def capture_page(
    request: fastapi.Request, session: web_deps.SessionDep
) -> fastapi_responses.HTMLResponse:
    base = str(request.base_url)
    context = {
        "bookmarklet": bookmarklet(base),
        "debug_bookmarklet": bookmarklet(base, "bookmarklet_debug"),
        "close_ms": round(web_context.load_close_after(session) * 1000),
    }
    return web_templating.templates.TemplateResponse(request, "capture.html", context)


@router.get("/capture/debug", response_class=fastapi_responses.HTMLResponse)
def capture_debug_page(request: fastapi.Request) -> fastapi_responses.HTMLResponse:
    return web_templating.templates.TemplateResponse(request, "capture_debug.html", {})


@router.post("/api/capture")
def capture(
    settings: web_deps.SettingsDep,
    session: web_deps.SessionDep,
    payload: t.Annotated[dict, fastapi.Body()],
) -> dict:
    _save_debug(settings.capture_debug_dir / "last_payload.json", json.dumps(payload, indent=1))
    return linkedin_capture.import_capture(session, payload).as_dict()


@router.post("/api/capture/debug")
def capture_debug(
    settings: web_deps.SettingsDep, payload: t.Annotated[dict, fastapi.Body()]
) -> dict[str, str | int]:
    html = str(payload.get("html") or "")
    name = f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}.html"
    path = settings.capture_debug_dir / name
    _save_debug(path, f"<!-- {payload.get('source_url', '')} -->\n{html}")
    return {"file": str(path), "size": len(html)}
