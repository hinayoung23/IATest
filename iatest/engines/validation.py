from iatest.engines.contracts import ExecutionPlan


def validate_plan(plan: ExecutionPlan):
    if plan.preparation:
        from iatest.engines.http import validate
        from iatest.mobile.delivery import PREPARATION_SCHEMA

        validate(
            plan.preparation,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["app"],
                "properties": {"app": PREPARATION_SCHEMA},
            },
        )
        if (
            plan.kind in {"android", "ios"}
            and plan.spec.get("profile") != plan.preparation["app"]["profile"]
        ):
            raise ValueError(
                "Preparation and UI execution must select the same device profile"
            )
    if plan.data_generation or plan.input_env:
        from iatest.engines.data import materialize

        plan, _, _ = materialize(plan, preview=True)
    from iatest.engines.api import validate_api
    from iatest.engines.checks import validate_checks
    from iatest.engines.client_performance import validate_client
    from iatest.engines.desktop import validate_desktop
    from iatest.engines.performance import validate_performance
    from iatest.engines.security import validate_security
    from iatest.engines.web import validate_web
    from iatest.engines.workflow import validate_workflow
    from iatest.mobile.contracts import validate_mobile_plan
    from iatest.nodes.distributed import validate_remote

    validators = {
        "api": validate_api,
        "web": validate_web,
        "desktop": validate_desktop,
        "performance": validate_performance,
        "client_performance": validate_client,
        "remote": validate_remote,
        "security": validate_security,
        "workflow": validate_workflow,
        "android": validate_mobile_plan,
        "ios": validate_mobile_plan,
        **{kind: validate_checks for kind in ("data", "protocol", "accessibility")},
    }
    validators[plan.kind](plan)
    if "allowed_origins" in plan.spec:
        from iatest.core.sandbox import origin

        for allowed in plan.spec["allowed_origins"]:
            origin(allowed)
