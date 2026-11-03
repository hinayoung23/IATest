"""Network SQL authorization is tested before any driver connection is possible."""

import copy

import pytest

from iatest.engines.data import declared_secrets, redact_asset
from iatest.engines.database_network import compile_query
from iatest.execution.native_isolation import referenced_env


def source(driver="postgresql", query="select id from accounts where id > ?"):
    return {
        "driver": driver,
        "host": "127.0.0.1",
        "port": 5432 if driver == "postgresql" else 3306,
        "database": "lab",
        "tls": "disable",
        "allowed_origins": ["http://127.0.0.1:5432"],
        "credentials": {
            "username": {"$env": "DB_USER"},
            "password": {"$env": "DB_PASS"},
        },
        "query": query,
        "parameters": [0],
        "allowed_tables": ["accounts"],
    }


@pytest.mark.parametrize("driver", ["postgresql", "mysql"])
def test_compiler_authorizes_cte_scopes_and_preserves_bound_parameter_semantics(driver):
    value = source(driver)
    value["query"] = (
        "with chosen as (select id from accounts where email like '%literal%') select id from chosen where id > ?"
    )
    compiled = compile_query(value)
    assert compiled.count("%s") == 1 and "%%literal%%" in compiled
    assert (
        '"public".accounts' if driver == "postgresql" else "`lab`.accounts"
    ) in compiled
    assert "chosen" in compiled


@pytest.mark.parametrize(
    "query",
    [
        "delete from accounts",
        "select id from accounts; delete from accounts",
        "with a as (delete from accounts returning id) select * from a",
        "with accounts as (select * from secrets) select * from accounts",
        "select * from other.accounts",
        "select * from accounts for update",
        "select pg_sleep(10)",
        "select evil.count(id) from accounts",
        "select load_file('/etc/passwd')",
        "select * into outfile '/tmp/forbidden' from accounts",
        "with recursive a as (select 1 union all select * from a) select * from a",
        "select id::custom_type from accounts",
        "select id from accounts where id = ?",
    ],
)
def test_compiler_rejects_writes_functions_hints_namespace_escape_and_wrong_parameters(
    query,
):
    value = source(query=query)
    value["parameters"] = []
    with pytest.raises(ValueError):
        compile_query(value)


def test_database_credential_references_survive_persistence_without_copying_values(
    monkeypatch,
):
    monkeypatch.setenv("DB_USER", "fixture_readonly_user")
    monkeypatch.setenv("DB_PASS", "fixture_password_never_in_assets")
    plan = {"id": "db", "kind": "data", "spec": {"database": source()}}
    saved = redact_asset(copy.deepcopy(plan))
    assert (
        saved["spec"]["database"]["credentials"]
        == plan["spec"]["database"]["credentials"]
    )
    assert set(referenced_env(plan)) == {"DB_USER", "DB_PASS"}
    assert set(declared_secrets(plan)) == {
        "fixture_readonly_user",
        "fixture_password_never_in_assets",
    }


@pytest.mark.parametrize("driver", ["postgresql", "mysql"])
def test_optimizer_hint_cannot_override_the_owned_query_timeout(driver):
    value = source(driver, "select /*+ MAX_EXECUTION_TIME(999999) */ * from accounts")
    value["parameters"] = []
    try:
        query = compile_query(value)
    except ValueError:
        return
    assert "MAX_EXECUTION_TIME" not in query and "999999" not in query
