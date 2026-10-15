"""Replay failed generated inputs only when their recipe is unchanged."""
import json
from pathlib import Path

from ai_apitest.core.paths import project_result_db, project_docs_file
from ai_apitest.report.storage import result_connection
from ai_apitest.execution.provenance import recipe_fingerprint, fingerprint


def replay_inputs(root: Path, project: str, run_id: int, cases: list) -> tuple[dict, dict]:
    db = project_result_db(root, project)
    if not db.is_file():
        raise ValueError("没有可重放的执行记录")
    with result_connection(db) as conn:
        run = conn.execute("select metadata_json from runs where id = ?", (run_id,)).fetchone()
        rows = conn.execute("select case_id, result_json from case_results where run_id = ? and status = 'failed'", (run_id,)).fetchall()
    if not run:
        raise ValueError("重放运行不存在")
    from ai_apitest.core.serde import read_json
    metadata = json.loads(run[0])
    if metadata.get("contract_hash") != fingerprint(read_json(project_docs_file(root, project), [])):
        raise ValueError("可信接口契约已变化，不能重放旧数据")
    current = {case.id: case for case in cases}
    values = {}
    for case_id, raw in rows:
        if case_id not in current:
            continue
        result = json.loads(raw)
        recipe = result.get("generation_meta", {})
        snapshot = result.get("generated_data")
        if recipe.get("recipe_hash") != recipe_fingerprint(current[case_id]) or not isinstance(snapshot, dict) or not snapshot:
            raise ValueError(f"{case_id} 的规则已变化或没有完整生成快照，不能重放")
        if recipe.get("generated_hash") != fingerprint(snapshot):
            raise ValueError(f"{case_id} 数据已脱敏、截断或变化，不能作为原始输入重放")
        values[case_id] = snapshot
        current[case_id].sample_plan = recipe.get("plan", {})
    if not values:
        raise ValueError("指定范围没有可重放的失败数据")
    return values, metadata
