from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request

from devlens.app.config import Settings
from devlens.app.dependencies import agent_context
from devlens.domain import AccessDenied, AnalysisRequest, AnalysisResult, ProviderError


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = Settings.from_env()
    except (ValueError, OSError):
        app.state.agent = None
        yield
        return
    async with agent_context(settings) as agent:
        app.state.agent = agent
        yield
    app.state.agent = None


app = FastAPI(title="DevLens", version="0.1.0", lifespan=lifespan)


async def get_agent(request: Request):
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(503, "DevLens configuration is missing or invalid.")
    return agent


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready(agent=Depends(get_agent)):
    return {"status": "ready"}


@app.post("/analyses/ticket", response_model=AnalysisResult)
async def analyze(request: AnalysisRequest, agent=Depends(get_agent)):
    try:
        return await agent.analyze_ticket(request)
    except AccessDenied as exc:
        raise HTTPException(403, str(exc)) from None
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from None
    except TimeoutError:
        raise HTTPException(504, "Analysis exceeded its time budget.") from None
