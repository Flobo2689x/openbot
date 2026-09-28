from fastapi import APIRouter, Depends, HTTPException

from openbot.api.deps import get_services
from openbot.runtime.cli_agent import (
    child_env,
    claude_auth_status,
    claude_profile_dir,
    find_claude,
    login_commands,
)
from openbot.runtime.providers import list_ollama_models, provider_configured, provider_status
from openbot.services import Services

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("")
async def providers(services: Services = Depends(get_services)):
    st = services.settings
    emb_provider = st.embedding_model.split(":", 1)[0]
    ollama_models = await list_ollama_models(st, getattr(services, "http_client", None))
    return {"providers": provider_status(st, ollama_models), "embedding_model": st.embedding_model,
            "embeddings_configured": provider_configured(st, emb_provider)}


@router.get("/claude-code/profile")
async def claude_code_profile(services: Services = Depends(get_services)):
    """The isolated Claude Code profile's login command (in every shell syntax) and live login status,
    checked with `claude auth status` alone -- never by reading a file. Only meaningful once
    claude_code_own_profile is on; the directory and commands still show either way, so the setting can be
    tried before switching it on."""
    st = services.settings
    executable = find_claude(st)
    if executable is None:
        raise HTTPException(404, "claude was not found; set it up before enabling an isolated profile")
    profile_dir = claude_profile_dir(st)
    status = await claude_auth_status(executable, child_env(profile_dir=profile_dir))
    return {"enabled": st.claude_code_own_profile, "profile_dir": str(profile_dir),
            "commands": login_commands(executable, profile_dir), **status}
