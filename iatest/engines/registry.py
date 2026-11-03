from pathlib import Path

from iatest.engines.api import APIEngine
from iatest.engines.checks import ChecksEngine
from iatest.engines.client_performance import ClientPerformanceEngine
from iatest.engines.contracts import EngineRegistry
from iatest.engines.desktop import DesktopEngine
from iatest.engines.performance import PerformanceEngine
from iatest.engines.security import SecurityEngine
from iatest.engines.web import WebEngine
from iatest.engines.workflow import WorkflowEngine
from iatest.mobile.runtime import MobileEngine
from iatest.nodes.distributed import RemoteEngine


def create_engine_registry(root: Path, project: str) -> EngineRegistry:
    registry = EngineRegistry()
    registry.register("api", APIEngine(root, project))
    registry.register("web", WebEngine(root, project))
    registry.register("desktop", DesktopEngine(root, project))
    registry.register("performance", PerformanceEngine(root, project))
    registry.register(
        "client_performance", ClientPerformanceEngine(root, project, registry)
    )
    registry.register("remote", RemoteEngine(root, project))
    registry.register("security", SecurityEngine(root, project))
    for platform in ("android", "ios"):
        registry.register(platform, MobileEngine(root, project, platform))
    registry.register("workflow", WorkflowEngine(root, project, registry))
    for kind in ("data", "protocol", "accessibility"):
        registry.register(kind, ChecksEngine(root, project, kind))
    return registry
