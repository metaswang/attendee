from bots.models import BotRuntimeProviderTypes

from .digitalocean import DigitalOceanAPIError, DigitalOceanDropletProvider


def get_runtime_provider(provider_name: str):
    if provider_name == BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET:
        return DigitalOceanDropletProvider()
    raise ValueError(f"Unsupported bot runtime provider: {provider_name}")


__all__ = ["DigitalOceanAPIError", "DigitalOceanDropletProvider", "get_runtime_provider"]
