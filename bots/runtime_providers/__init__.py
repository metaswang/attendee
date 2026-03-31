from bots.models import BotRuntimeProviderTypes

from .digitalocean import DigitalOceanAPIError, DigitalOceanDropletProvider
from .gcp_compute_engine import GCPComputeEngineError, GCPComputeInstanceProvider


def get_runtime_provider(provider_name: str):
    if provider_name == BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET:
        return DigitalOceanDropletProvider()
    if provider_name == BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE:
        return GCPComputeInstanceProvider()
    raise ValueError(f"Unsupported bot runtime provider: {provider_name}")


__all__ = ["DigitalOceanAPIError", "DigitalOceanDropletProvider", "GCPComputeEngineError", "GCPComputeInstanceProvider", "get_runtime_provider"]
