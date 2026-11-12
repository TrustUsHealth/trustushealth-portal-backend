"""App start. Auto-discovers modules listed in config/modules.yaml (PRD 4.4).

A module is live when (1) it is in modules.yaml and (2) `app/modules/<key>/router.py` exists.
Routes mount at /api/v1/<key>. A module may also provide:
  router.extra_routers = {"prefix": APIRouter}   mounted at /api/v1/<prefix>
  service.on_startup(app)                         indexes and other one-time setup
  events.register(bus)                            event subscriptions
"""

import logging
from contextlib import asynccontextmanager
from importlib import import_module

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from app.core import audit
from app.core.config import ConfigError, PortalConfig, Settings, load_config
from app.core.db import create_client, get_database
from app.core.events import EventBus
from app.core.mailer import Mailer
from app.core.security import RateLimiter

log = logging.getLogger("portal")
API_PREFIX = "/api/v1"


def _import_optional(path: str):
    """Import `path`, returning None only if that exact module is absent (not a broken import inside it)."""
    try:
        return import_module(path)
    except ModuleNotFoundError as exc:
        if exc.name and (path == exc.name or path.startswith(exc.name + ".")):
            return None
        raise


def discover_modules(app: FastAPI, cfg: PortalConfig) -> None:
    mounted: set[str] = set()
    hooks = []
    for mod in cfg.modules:
        router_mod = _import_optional(f"app.modules.{mod.key}.router")
        if router_mod is None:
            log.info("module '%s' is listed in modules.yaml but not built yet; skipped", mod.key)
            continue
        app.include_router(router_mod.router, prefix=f"{API_PREFIX}/{mod.key}")
        for prefix, extra in getattr(router_mod, "extra_routers", {}).items():
            app.include_router(extra, prefix=f"{API_PREFIX}/{prefix}")
        mounted.add(mod.key)
        if (service := _import_optional(f"app.modules.{mod.key}.service")) and hasattr(service, "on_startup"):
            hooks.append(service.on_startup)
        if (events := _import_optional(f"app.modules.{mod.key}.events")) and hasattr(events, "register"):
            events.register(app.state.events)
    app.state.mounted_modules = mounted
    app.state.startup_hooks = hooks


def _operation_id(route: APIRoute) -> str:
    """Stable ids for the generated frontend client: <tag>_<function>."""
    return f"{route.tags[0] if route.tags else 'core'}_{route.name}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = None
    if not hasattr(app.state, "db"):  # tests inject their own database
        client = create_client(app.state.settings)
        app.state.db = get_database(client, app.state.settings)
    await audit.ensure_indexes(app.state.db)
    for hook in app.state.startup_hooks:
        await hook(app)
    log.info("started; modules mounted: %s", sorted(app.state.mounted_modules))
    yield
    if client is not None:
        await client.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = settings or Settings()
    try:
        cfg = load_config(settings.config_dir)
    except ConfigError as exc:  # fail fast with plain-language messages
        raise SystemExit("Configuration problems:\n  - " + "\n  - ".join(exc.problems)) from None
    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn, send_default_pii=False, traces_sample_rate=0.0)

    prod = settings.environment == "production"
    app = FastAPI(
        title=f"{cfg.app.brand.name} Portal API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None if prod else f"{API_PREFIX}/docs",
        redoc_url=None,
        generate_unique_id_function=_operation_id,
    )
    app.state.settings, app.state.config = settings, cfg
    app.state.mailer, app.state.events = Mailer(settings), EventBus()
    app.state.rate_limiter = RateLimiter()
    # Same-origin in production (frontend proxies /api/v1/*); CORS only helps local dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    discover_modules(app, cfg)

    @app.get("/healthz", tags=["system"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Keep-alive target for UptimeRobot / Render health checks. Cheap: no database call."""
        return {"status": "ok"}

    return app
