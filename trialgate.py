#!/usr/bin/env python3
"""Fresh seeded challenges for repeated, paired local-model evaluation.

Public procedural probes, not an independently held-out scientific benchmark.
No Continuum personal state is loaded; nothing is automatically promoted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sys

from continuum import _ollama_json, timestamp
from evalgate import (EVAL_SYSTEM, MAX_RESPONSE_CHARS, SETTINGS,
                      SHA256_PATTERN, model_fingerprint, normalize, save_new)

VERSION = "0.4.0"
GENERATOR = "continuum-seeded-probes-v1"
SCHEMA = 1
MAX_SEED = (1 << 63) - 1
ROUNDS = 3
CASES_PER_ROUND = 24
MAX_INPUT_BYTES = 2_000_000


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


class Stream:
    """SHA-256 counter stream: stable across Python versions and platforms."""

    def __init__(self, seed: int):
        self.seed = seed
        self.counter = 0

    def below(self, n: int) -> int:
        if n <= 0:
            raise ValueError("n must be positive")
        bound = (1 << 256) - ((1 << 256) % n)
        while True:
            block = hashlib.sha256(
                b"continuum-probes-v1\x00"
                + self.seed.to_bytes(8, "big")
                + self.counter.to_bytes(8, "big")
            ).digest()
            self.counter += 1
            value = int.from_bytes(block, "big")
            if value < bound:
                return value % n

    def between(self, lo: int, hi: int) -> int:
        return lo + self.below(hi - lo + 1)

    def chars(self, alphabet: str, count: int) -> str:
        return "".join(alphabet[self.below(len(alphabet))] for _ in range(count))

    def shuffle(self, values: list[dict]) -> None:
        for i in range(len(values) - 1, 0, -1):
            j = self.below(i + 1)
            values[i], values[j] = values[j], values[i]


def generate_cases(seed: int) -> list[dict[str, str]]:
    """Compute fresh prompts and ground-truth answers without asking an LLM."""
    if type(seed) is not int or not (1 <= seed <= MAX_SEED):
        raise ValueError("Invalid challenge seed")
    r = Stream(seed)
    cases: list[dict[str, str]] = []

    def add(family: str, number: int, prompt: str, expected: str) -> None:
        cases.append({"id": f"{family}-{number:02}", "category": family,
                      "prompt": prompt, "expected": expected})

    for i in range(4):
        a, b = r.between(-999, 999), r.between(-999, 999)
        add("add", i, f"Return only the integer value of {a} + ({b}).", str(a + b))
    for i in range(4):
        a, b = r.between(-40, 40), r.between(-40, 40)
        add("multiply", i, f"Return only the integer value of {a} * ({b}).", str(a * b))
    for i in range(4):
        nums = [r.between(-90, 90) for _ in range(5)]
        add("sort", i, "Sort these integers ascending; return comma-separated integers only: "
            + ", ".join(map(str, nums)), ", ".join(map(str, sorted(nums))))
    for i in range(4):
        word = r.chars("abcdefghijklmnopqrstuvwxyz", r.between(7, 13))
        add("reverse", i, f"Reverse these characters exactly, with no quotes: {word}", word[::-1])
    for i in range(4):
        word = r.chars("abcdefghijklmnopqrstuvwxyz", r.between(15, 22))
        letter = "aeiou"[r.below(5)]
        add("count", i, f"How many times does the letter '{letter}' occur in '{word}'? Reply with one integer.",
            str(word.count(letter)))
    for i in range(4):
        numerator, denominator = r.between(50, 999), r.between(3, 19)
        add("modulo", i, f"Return only the nonnegative remainder of {numerator} divided by {denominator}.",
            str(numerator % denominator))
    r.shuffle(cases)
    assert len(cases) == CASES_PER_ROUND
    return cases


def plan_id(seeds: list[int]) -> str:
    return hashlib.sha256(canonical({"generator": GENERATOR, "seeds": seeds})).hexdigest()


def create_plan(seeds: list[int] | None = None) -> dict:
    if seeds is None:
        seeds = []
        while len(seeds) < ROUNDS:
            candidate = secrets.randbelow(MAX_SEED) + 1
            if candidate not in seeds:
                seeds.append(candidate)
    plan = {"schema": SCHEMA, "generator": GENERATOR, "seeds": seeds,
            "plan_id": plan_id(seeds)}
    return validate_plan(plan)


def validate_plan(plan: object) -> dict:
    if not isinstance(plan, dict) or set(plan) != {"schema", "generator", "seeds", "plan_id"}:
        raise ValueError("Malformed challenge plan")
    seeds = plan["seeds"]
    if type(plan["schema"]) is not int or plan["schema"] != SCHEMA or plan["generator"] != GENERATOR:
        raise ValueError("Unexpected challenge generator version")
    if not isinstance(seeds, list) or len(seeds) != ROUNDS or any(
        type(s) is not int or not 1 <= s <= MAX_SEED for s in seeds
    ) or len(set(seeds)) != ROUNDS:
        raise ValueError("Challenge plan must have three unique valid seeds")
    if plan["plan_id"] != plan_id(seeds):
        raise ValueError("Challenge plan digest mismatch")
    return plan


def model_check(model: object) -> dict:
    if not isinstance(model, dict) or set(model) != {"name", "digest"}:
        raise ValueError("Missing model identity")
    if not isinstance(model["name"], str) or not model["name"].strip():
        raise ValueError("Missing model name")
    if not isinstance(model["digest"], str) or not SHA256_PATTERN.fullmatch(model["digest"]):
        raise ValueError("Missing valid model digest")
    return model


def build_report(model: dict, plan: dict, responses: list[list[str]]) -> dict:
    model_check(model)
    validate_plan(plan)
    if not isinstance(responses, list) or len(responses) != ROUNDS:
        raise ValueError("Incorrect number of evaluation rounds")
    rounds = []
    for seed, answers in zip(plan["seeds"], responses):
        cases = generate_cases(seed)
        if not isinstance(answers, list) or len(answers) != len(cases):
            raise ValueError("Missing generated case responses")
        records = []
        for case, reply in zip(cases, answers):
            if not isinstance(reply, str) or len(reply) > MAX_RESPONSE_CHARS:
                raise ValueError("Malformed or oversized model output")
            records.append({"id": case["id"], "response": reply,
                            "passed": normalize(reply) == normalize(case["expected"])})
        rounds.append({"seed": seed, "records": records,
                       "counts": {"passed": sum(x["passed"] for x in records),
                                  "total": CASES_PER_ROUND}})
    return {"schema": SCHEMA, "runner": VERSION, "created_at": timestamp(),
            "plan": json.loads(json.dumps(plan)), "model": dict(model),
            "settings": dict(SETTINGS), "rounds": rounds,
            "counts": {"passed": sum(x["counts"]["passed"] for x in rounds),
                       "total": ROUNDS * CASES_PER_ROUND}}


def run_model(model_name: str, plan: dict) -> dict:
    validate_plan(plan)
    model = model_fingerprint(model_name)
    responses: list[list[str]] = []
    for seed in plan["seeds"]:
        batch = []
        for case in generate_cases(seed):
            output = _ollama_json("/api/chat", {
                "model": model_name, "stream": False,
                "messages": [{"role": "system", "content": EVAL_SYSTEM},
                             {"role": "user", "content": case["prompt"]}],
                "options": dict(SETTINGS),
            }, timeout=180)
            msg = output.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), str):
                raise RuntimeError("Model returned no answer for " + case["id"])
            batch.append(msg["content"])
        responses.append(batch)
    if model_fingerprint(model_name) != model:
        raise RuntimeError("Installed model changed during trial; discard evidence")
    return build_report(model, plan, responses)


def validate_report(report: object) -> dict:
    if not isinstance(report, dict) or set(report) != {
        "schema", "runner", "created_at", "plan", "model", "settings", "rounds", "counts"
    } or type(report["schema"]) is not int or report["schema"] != SCHEMA or report["runner"] != VERSION:
        raise ValueError("Unsupported or malformed trial report")
    if not isinstance(report["created_at"], str) or not report["created_at"].strip():
        raise ValueError("Missing report timestamp")
    plan = validate_plan(report["plan"])
    model_check(report["model"])
    if report["settings"] != SETTINGS:
        raise ValueError("Changed generation settings")
    rounds = report["rounds"]
    if not isinstance(rounds, list) or len(rounds) != ROUNDS:
        raise ValueError("Missing trial rounds")
    correct = 0
    for seed, round_ in zip(plan["seeds"], rounds):
        if not isinstance(round_, dict) or set(round_) != {"seed", "records", "counts"} or round_["seed"] != seed:
            raise ValueError("Wrong trial seed or round layout")
        cases = generate_cases(seed)
        records = round_["records"]
        if not isinstance(records, list) or len(records) != CASES_PER_ROUND:
            raise ValueError("Missing trial cases")
        passed = 0
        for case, record in zip(cases, records):
            if not isinstance(record, dict) or set(record) != {"id", "response", "passed"} or record["id"] != case["id"]:
                raise ValueError("Changed trial case ID or order")
            answer = record["response"]
            if not isinstance(answer, str) or len(answer) > MAX_RESPONSE_CHARS or type(record["passed"]) is not bool:
                raise ValueError("Malformed trial response")
            if record["passed"] != (normalize(answer) == normalize(case["expected"])):
                raise ValueError("Trial score contradicts generated ground truth")
            passed += int(record["passed"])
        if round_["counts"] != {"passed": passed, "total": CASES_PER_ROUND}:
            raise ValueError("Incorrect trial round score")
        correct += passed
    if report["counts"] != {"passed": correct, "total": ROUNDS * CASES_PER_ROUND}:
        raise ValueError("Incorrect aggregate trial score")
    return report


def compare(baseline: dict, candidate: dict) -> dict:
    validate_report(baseline)
    validate_report(candidate)
    if baseline["plan"] != candidate["plan"]:
        raise ValueError("Cannot compare different challenge plans")
    if baseline["model"]["digest"] == candidate["model"]["digest"]:
        raise ValueError("Candidate and baseline have identical model digests")
    totals = {"gains": 0, "regressions": 0}
    by_seed = []
    improved_rounds = 0
    for old_round, new_round in zip(baseline["rounds"], candidate["rounds"]):
        gains, regressions = [], []
        for old, new in zip(old_round["records"], new_round["records"]):
            if new["passed"] and not old["passed"]:
                gains.append(old["id"])
            if old["passed"] and not new["passed"]:
                regressions.append(old["id"])
        totals["gains"] += len(gains)
        totals["regressions"] += len(regressions)
        improved_rounds += int(bool(gains))
        by_seed.append({"seed": old_round["seed"], "baseline_passed": old_round["counts"]["passed"],
                        "candidate_passed": new_round["counts"]["passed"],
                        "gains": gains, "regressions": regressions})
    eligible = totals["regressions"] == 0 and improved_rounds >= 2
    return {"gate": "PASS" if eligible else "BLOCK", "approved_for_manual_review": eligible,
            "automatically_promoted": False, "plan_id": baseline["plan"]["plan_id"],
            "baseline": {"model": baseline["model"], "passed": baseline["counts"]["passed"]},
            "candidate": {"model": candidate["model"], "passed": candidate["counts"]["passed"]},
            "total": ROUNDS * CASES_PER_ROUND, "improved_rounds": improved_rounds,
            "gains": totals["gains"], "regressions": totals["regressions"], "by_seed": by_seed,
            "rule": "Zero regressions and at least one gain in at least two different seeds; manual review only",
            "evidence_limit": "Procedural public tasks are not an independent holdout or proof of broad model gains"}


def load_json(path: Path) -> object:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Evaluation evidence exceeds file size cap")
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Evaluation evidence exceeds file size cap")
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seeded multi-round local model trials; manual review only")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("plan", help="Create unpredictable, repeatable trial seeds")
    make.add_argument("--out", type=Path, required=True)
    run = commands.add_parser("run", help="Run all planned challenges using a local Ollama model")
    run.add_argument("--model", required=True)
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    comp = commands.add_parser("compare", help="Validate paired reports and check regressions")
    comp.add_argument("--baseline", type=Path, required=True)
    comp.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = create_plan()
            save_new(args.out, result)
        elif args.command == "run":
            plan = validate_plan(load_json(args.plan))
            result = run_model(args.model, plan)
            validate_report(result)
            save_new(args.out, result)
        else:
            base = load_json(args.baseline)
            candidate = load_json(args.candidate)
            result = compare(base, candidate)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if args.command == "compare" and result["gate"] == "BLOCK" else 0
    except (OSError, ValueError, TypeError, RuntimeError, OverflowError, json.JSONDecodeError) as exc:
        print(f"trial error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
