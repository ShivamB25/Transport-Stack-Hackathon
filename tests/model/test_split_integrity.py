"""Static guard against introducing random row-level evaluation splits."""
from __future__ import annotations

import ast
from pathlib import Path


def test_model_path_has_no_random_or_shuffled_split_calls() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "ps1"
    prohibited = {"train_test_split", "shuffle", "random_split"}
    offenders = []
    for name in ("features.py", "model.py", "evaluate.py"):
        tree = ast.parse((source_root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function_name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if function_name in prohibited:
                    offenders.append(f"{name}:{node.lineno}:{function_name}")
    assert offenders == []
