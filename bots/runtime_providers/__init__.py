from bots.models import BotRuntimeProviderTypes

from .vps_docker import VPSDockerRuntimeError, VPSDockerRuntimeProvider
from .gcp_compute_engine import GCPComputeEngineError, GCPComputeInstanceProvider


def get_runtime_provider(provider_name: str):
    if provider_name == BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE:
        return GCPComputeInstanceProvider()
    if provider_name == BotRuntimeProviderTypes.VPS_DOCKER:
        return VPSDockerRuntimeProvider()
    raise ValueError(f"Unsupported bot runtime provider: {provider_name}")


__all__ = [
    "GCPComputeEngineError",
    "GCPComputeInstanceProvider",
    "VPSDockerRuntimeError",
    "VPSDockerRuntimeProvider",
    "get_runtime_provider",
]
