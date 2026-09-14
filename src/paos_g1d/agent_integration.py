"""Project-only image tool registration; automatic grasp verification is disabled."""

from PhyAgentOS.agent import loop
from PhyAgentOS.agent.tools.image import ImageTool
from PhyAgentOS.config.loader import load_config
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.providers_manager import ProvidersManager
from PhyAgentOS.verification.request_builder import (
    VerificationRequestBuilder as CoreVerificationRequestBuilder,
)

CoreAgentLoop = loop.AgentLoop


def _vision_provider(config, fallback):
    """Provider answering ImageTool's vision requests (the image-judgment stage).

    ``agents.modes.models.multimodal.model`` selects its model; without that entry
    the Agent's own provider is reused, as before.
    """
    mode = config.agents.modes.models.get("multimodal")
    model_id = mode.model if mode else ""
    if not model_id or model_id == config.agents.defaults.model:
        return fallback
    entry = config.get_provider(model_id)
    return LiteLLMProvider(
        api_key=entry.api_key if entry else None,
        api_base=config.get_api_base(model_id),
        default_model=model_id,
        extra_headers=entry.extra_headers if entry else None,
        provider_name=model_id.split("/", 1)[0] if "/" in model_id else config.agents.defaults.provider,
    )


class G1dAgentLoop(CoreAgentLoop):
    def _register_default_tools(self):
        super()._register_default_tools()
        if self.tools.has("image"):
            return
        # The main Agent keeps its native provider; the image tool can run a different model.
        config = load_config()
        manager = ProvidersManager(
            config=config,
            modes={
                "main": {"provider": self.provider, "describe": "Configured main Agent model"},
                "multimodal": {
                    "provider": _vision_provider(config, self.provider),
                    "describe": "Configured vision-capable model",
                },
            },
            default_mode="main",
        )
        self.tools.register(ImageTool(manager, send_callback=self.bus.publish_outbound))


def _install_verification_request_builder():
    """Point the Forge verifier at the builder that stops repeating the execution records.

    `ForgeTaskVerifier` resolves the builder from a module global at construction time
    (`self.request_builder = VerificationRequestBuilder(self.workspace)`), so rebinding that
    name is enough; Core's source stays untouched. Without this the projecting subclass exists
    but is never used, because the production coordinator builds the Core default.
    """
    from PhyAgentOS.agent import session_verifier

    from .verification import G1dVerificationRequestBuilder

    current = session_verifier.VerificationRequestBuilder
    if current is G1dVerificationRequestBuilder:
        return
    if current is not CoreVerificationRequestBuilder:
        raise RuntimeError(
            "Another VerificationRequestBuilder extension is installed; refusing to replace it"
        )
    session_verifier.VerificationRequestBuilder = G1dVerificationRequestBuilder


def install():
    """The native CLI imports AgentLoop when invoking agent/gateway; select our subclass."""
    if loop.AgentLoop not in (CoreAgentLoop, G1dAgentLoop):
        raise RuntimeError("Another AgentLoop extension is installed; refusing to replace it")
    loop.AgentLoop = G1dAgentLoop
    _install_verification_request_builder()
