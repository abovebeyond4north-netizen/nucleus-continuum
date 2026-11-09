#!/usr/bin/env python3
"""Frozen, local-only model evaluation and conservative regression gate.

No private Continuum state is read. Model output is data, never executed.
A passing gate is benchmark evidence, not an autonomous model promotion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from continuum import _ollama_json, timestamp

VERSION = "0.3.0"
REPORT_SCHEMA = 1
SUITE_PATH = Path(__file__).resolve().parent / "eval" / "frozen_v1.json"
# A frozen suite needs an out-of-file checksum. Changes require code review.
FROZEN_SUITE_SHA256 = "4760a0986fde25fd134d2c31b7efe3be0137abab8deeab9da7e3ae5e1a3bc593"
SETTINGS = {"temperature": 0, "seed": 42, "num_predict": 128}
EVAL_SYSTEM = (
    "You are completing a benchmark. Answer the question exactly as instructed. "
    "Use the shortest possible final answer, with no explanations or markup."
)
MAX_CASES = 100
MAX_RESPONSE_CHARS = 16_000
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}\Z")


def read_suite() -> dict:
    raw = SUITE_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FROZEN_SUITE_SHA256:
        raise ValueError("Frozen evaluation suite checksum changed; review the test fixture")
    suite = json.loads(raw)
    if not isinstance(suite, dict) or suite.get("schema") != 1 or suite.get("id") != "continuum-frozen-v1":
        raise ValueError("Unsupported evaluation suite")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not (1 <= len(cases) <= MAX_CASES):
        raise ValueError("Invalid evaluation case count")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "category", "prompt", "expected"}:
            raise ValueError("Malformed evaluation case")
        if any(not isinstance(case[k], str) or not case[k].strip() for k in case):
            raise ValueError("Invalid evaluation case fields")
        if case["id"] in seen or len(case["prompt"]) > 2048 or len(case["expected"]) > 256:
            raise ValueError("Duplicate or oversized evaluation case")
        seen.add(case["id"])
    return suite


def normalize(text: str) -> str:
    """Objective, deliberately strict short-answer equality."""
    return " ".join(text.strip().casefold().split())


def passes(case: dict, response: str) -> bool:
    return normalize(response) == normalize(case["expected"])


def model_fingerprint(name: str) -> dict[str, str]:
    """Resolve exact installed name plus Ollama-reported model digest."""
    listing = _ollama_json("/api/tags", timeout=10)
    models = listing.get("models")
    if not isinstance(models, list):
        raise RuntimeError("Ollama model registry is malformed")
    for row in models:
        if not isinstance(row, dict) or row.get("name") != name:
            continue
        value = row.get("digest")
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise RuntimeError("Ollama model digest missing or malformed")
        return {"name": name, "digest": value}
    raise ValueError(f"Model {name!r} not installed locally; run ollama pull {name}")


def build_report(model: dict[str, str], responses: list[str], suite: dict) -> dict:
    """Construct a report from responses in frozen case order (also used in offline tests)."""
    cases = suite["cases"]
    if len(responses) != len(cases):
        raise ValueError("Wrong number of model responses")
    records = []
    for case, response in zip(cases, responses):
        if not isinstance(response, str) or len(response) > MAX_RESPONSE_CHARS:
            raise ValueError("Missing or oversized model response")
        records.append({"id": case["id"], "response": response, "passed": passes(case, response)})
    return {
        "schema": REPORT_SCHEMA,
        "runner": VERSION,
        "created_at": timestamp(),
        "suite": {"id": suite["id"], "sha256": FROZEN_SUITE_SHA256},
        "model": model,
        "settings": dict(SETTINGS),
        "records": records,
        "counts": {"passed": sum(r["passed"] for r in records), "total": len(records)},
    }


def run_model(model_name: str, suite: dict) -> dict:
    model = model_fingerprint(model_name)
    responses: list[str] = []
    for case in suite["cases"]:
        output = _ollama_json("/api/chat", {
            "model": model_name,
            "stream": False,
            "messages": [
                {"role": "system", "content": EVAL_SYSTEM},
                {"role": "user", "content": case["prompt"]},
            ],
            "options": dict(SETTINGS),
        }, timeout=180)
        message = output.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise RuntimeError(f"Model returned no text for case {case['id']}")
        responses.append(message["content"])
    # Do not attribute answers to a digest that was swapped during the run.
    if model_fingerprint(model_name) != model:
        raise RuntimeError("Installed model changed during evaluation; discard evidence")
    return build_report(model, responses, suite)


def validate_report(report: object, suite: dict) -> dict:
    """Fail closed: recompute every score from raw responses and the pinned suite."""
    if not isinstance(report, dict) or report.get("schema") != REPORT_SCHEMA or report.get("runner") != VERSION:
        raise ValueError("Unsupported or malformed evaluation report")
    if report.get("suite") != {"id": suite["id"], "sha256": FROZEN_SUITE_SHA256}:
        raise ValueError("Report uses a different evaluation suite")
    if report.get("settings") != SETTINGS:
        raise ValueError("Report uses different generation settings")
    model = report.get("model")
    if not isinstance(model, dict) or set(model) != {"name", "digest"} or not isinstance(model["name"], str) or not model["name"].strip():
        raise ValueError("Report model identity missing")
    if not isinstance(model["digest"], str) or not SHA256_PATTERN.fullmatch(model["digest"]):
        raise ValueError("Report model digest missing or malformed")
    records = report.get("records")
    if not isinstance(records, list) or len(records) != len(suite["cases"]):
        raise ValueError("Report is missing benchmark cases")
    score = 0
    for case, record in zip(suite["cases"], records):
        if not isinstance(record, dict) or set(record) != {"id", "response", "passed"} or record["id"] != case["id"]:
            raise ValueError("Report case IDs differ from frozen suite")
        if not isinstance(record["response"], str) or len(record["response"]) > MAX_RESPONSE_CHARS or type(record["passed"]) is not bool:
            raise ValueError("Malformed benchmark response")
        if passes(case, record["response"]) != record["passed"]:
            raise ValueError(f"Forged or stale pass flag for {case['id']}")
        score += int(record["passed"])
    if report.get("counts") != {"passed": score, "total": len(records)}:
        raise ValueError("Report count does not match independent scoring")
    return report


def compare(baseline: dict, candidate: dict, suite: dict) -> dict:
    baseline = validate_report(baseline, suite)
    candidate = validate_report(candidate, suite)
    if baseline["model"]["digest"] == candidate["model"]["digest"]:
        raise ValueError("Candidate and baseline have identical model digests")
    gains = []
    regressions = []
    for old, new in zip(baseline["records"], candidate["records"]):
        if not old["passed"] and new["passed"]:
            gains.append(old["id"])
        elif old["passed"] and not new["passed"]:
            regressions.append(old["id"])
    approved = not regressions and len(gains) >= 1
    return {
        "gate": "PASS" if approved else "BLOCK",
        "approved_for_manual_review": approved,
        "automatically_promoted": False,
        "suite_sha256": FROZEN_SUITE_SHA256,
        "baseline": {"model": baseline["model"], "passed": baseline["counts"]["passed"]},
        "candidate": {"model": candidate["model"], "passed": candidate["counts"]["passed"]},
        "total": len(suite["cases"]),
        "gains": gains,
        "regressions": regressions,
        "rule": "At least one case gained and zero formerly passing cases regressed",
    }


def save_new(path: Path, report: dict) -> None:
    """Never overwrite evaluation evidence; use owner-only permissions on POSIX."""
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frozen evaluation and regression gate; local Ollama only")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("suite", help="Verify frozen fixture digest and list case count")
    run = commands.add_parser("run", help="Evaluate an installed Ollama model in isolated test prompts")
    run.add_argument("--model", required=True, help="Exact installed Ollama model tag")
    run.add_argument("--out", required=True, type=Path, help="New evidence path (never overwritten)")
    comp = commands.add_parser("compare", help="Compare two complete run reports; no model calls")
    comp.add_argument("--baseline", required=True, type=Path)
    comp.add_argument("--candidate", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        suite = read_suite()
        if args.command == "suite":
            result = {"suite": suite["id"], "sha256": FROZEN_SUITE_SHA256, "cases": len(suite["cases"])}
        elif args.command == "run":
            result = run_model(args.model, suite)
            validate_report(result, suite)
            save_new(args.out, result)
        else:
            base = json.loads(args.baseline.read_text(encoding="utf-8"))
            candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
            result = compare(base, candidate, suite)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if args.command == "compare" and result["gate"] != "PASS" else 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
