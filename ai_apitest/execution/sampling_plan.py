"""Schedule declared data partitions fairly using recipe-scoped evidence."""
from __future__ import annotations

from dataclasses import replace
import re

from ai_apitest.core.paths import project_result_db
from ai_apitest.report.storage import result_connection
from ai_apitest.execution.provenance import fingerprint
from ai_apitest.execution.data_generation import generation_schema


def plan_samples(cases, root, project, config):
    stats = {}
    db = project_result_db(root, project)
    if db.is_file():
        with result_connection(db) as conn:
            exists = conn.execute("select 1 from sqlite_master where name = 'data_partition_stats'").fetchone()
            if exists:
                stats = {(row[0], row[1], row[2]): (row[3], row[4]) for row in conn.execute("select recipe, variable, partition, attempts, failures from data_partition_stats")}
    planned, virtual = [], {}
    prioritize = config.get("execution", {}).get("sampling", {}).get("prioritize_failures", True)
    for case in cases:
        plan = {}
        recipe = fingerprint({"source": case.sample_origin_id or case.id, "rules": case.data_generation})
        for name, spec in case.data_generation.items():
            partitions = spec.get("partitions", [])
            if not partitions:
                continue
            if not isinstance(partitions, list) or len(partitions) > 100:
                raise ValueError("partitions 必须为有界数组")
            labels = []
            for part in partitions:
                if not isinstance(part, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(part.get("name", ""))) or not isinstance(part.get("schema"), (dict, bool)):
                    raise ValueError("分区需要合法 name 和 schema")
                generation_schema({"schema": part["schema"]})
                labels.append(part["name"])
            if len(set(labels)) != len(labels):
                raise ValueError("分区名称不能重复")
            def score(label):
                key = (recipe, name, label)
                attempts, failures = stats.get(key, (0, 0))
                count = attempts + virtual.get(key, 0)
                return (count / (1 + min(3, failures) if prioritize else 1), count, label)
            selected = sorted(labels)[max(0, case.sample_index - 1) % len(labels)] if config.get("execution", {}).get("_fixed_sampling") else min(labels, key=score)
            key = (recipe, name, selected)
            virtual[key] = virtual.get(key, 0) + 1
            plan[name] = {"partition": selected, "recipe": recipe, "reason": "least_covered_with_failure_weight" if prioritize else "least_covered"}
        planned.append(replace(case, sample_plan=plan) if plan else case)
    return planned


def selected_rules(case):
    if not case.sample_plan:
        return case.data_generation
    import copy
    rules = copy.deepcopy(case.data_generation)
    for name, choice in case.sample_plan.items():
        spec = rules[name]
        part = next(value for value in spec["partitions"] if value["name"] == choice["partition"])
        base = generation_schema(spec)
        spec["schema"] = {"allOf": [base, part["schema"]]}
    return rules


def record_samples(root, project, results):
    rows = [(value["recipe"], name, value["partition"], int(result.status == "failed")) for result in results if result.status in {"passed", "failed"} for name, value in result.generation_meta.get("plan", {}).items()]
    if not rows:
        return
    with result_connection(project_result_db(root, project)) as conn:
        conn.execute("create table if not exists data_partition_stats(recipe text, variable text, partition text, attempts integer not null, failures integer not null, primary key(recipe, variable, partition))")
        conn.executemany("insert into data_partition_stats values (?, ?, ?, 1, ?) on conflict(recipe, variable, partition) do update set attempts = attempts + 1, failures = failures + excluded.failures", rows)
