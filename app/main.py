from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings, get_settings
from .models import GeocodeResult, ModelRequest, PreviewResponse
from .services.geocoding import GeocodingError, search_address
from .services.mesher import (
    ModelBuildError,
    build_model_archive,
    generate_preview_data,
)
from .services.overpass import OverpassError, fetch_environment


def get_app() -> FastAPI:
    fastapi_app = FastAPI(title="print3dhood", version="0.1.0")
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return fastapi_app


app = get_app()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/health")
async def health(settings: Settings = Depends(get_settings)):
    return {"status": "ok", "service": settings.app_name}


@app.get("/api/geocode")
async def geocode(query: str) -> dict[str, list[GeocodeResult]]:
    try:
        results = await search_address(query)
    except GeocodingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if not results:
        raise HTTPException(status_code=404, detail="No results found.")
    return {"results": results}


@app.post("/api/models")
async def create_models(request: ModelRequest, settings: Settings = Depends(get_settings)):
    request = request.model_copy(update={"highlight_home": True, "formats": ["stl"]})
    if request.radius_meters > settings.max_radius_m:
        request = request.model_copy(update={"radius_meters": settings.max_radius_m})

    try:
        buildings, roads, parks, waters = await fetch_environment(
            latitude=request.latitude,
            longitude=request.longitude,
            radius_m=request.radius_meters,
        )
    except OverpassError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if not buildings:
        raise HTTPException(
            status_code=404,
            detail="No buildings found for that location. Try increasing the radius.",
        )

    try:
        filename, archive, metadata = build_model_archive(
            request=request,
            buildings=buildings,
            roads=roads,
            parks=parks,
            waters=waters,
        )
    except ModelBuildError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Building-Count": str(metadata.building_count),
    }
    return StreamingResponse(content=archive, media_type="application/zip", headers=headers)


@app.post("/api/models/preview")
async def preview_models(request: ModelRequest) -> PreviewResponse:
    request = request.model_copy(update={"highlight_home": True, "formats": ["stl"]})

    try:
        buildings, roads, parks, waters = await fetch_environment(
            latitude=request.latitude,
            longitude=request.longitude,
            radius_m=request.radius_meters,
        )
    except OverpassError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if not buildings:
        raise HTTPException(
            status_code=404,
            detail="No buildings found for that location. Try increasing the radius.",
        )

    metadata, previews = generate_preview_data(
        request=request,
        buildings=buildings,
        roads=roads,
        parks=parks,
        waters=waters,
    )
    return PreviewResponse(metadata=metadata, previews=previews)


@app.exception_handler(ModelBuildError)
async def handle_model_error(_: Request, exc: ModelBuildError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
