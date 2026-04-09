import os


LAUNCH_METHOD_HYBRID = "hybrid"
LAUNCH_METHOD_GCP_COMPUTE_ENGINE = "gcp-compute-engine"
LAUNCH_METHOD_KUBERNETES = "kubernetes"
LAUNCH_METHOD_DOCKER_COMPOSE_MULTI_HOST = "docker-compose-multi-host"


def current_launch_method() -> str:
    return os.getenv("LAUNCH_BOT_METHOD", LAUNCH_METHOD_HYBRID)


def uses_hybrid_runtime_scheduler() -> bool:
    return current_launch_method() in {LAUNCH_METHOD_HYBRID, LAUNCH_METHOD_GCP_COMPUTE_ENGINE}


def uses_runtime_backed_bot() -> bool:
    return uses_hybrid_runtime_scheduler()
