from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
from pathlib import Path

import click

from .audit import configure as configure_audit
from .calibrate import calibrate, format_report, load_labeled_jsonl
from .config import load_from_env
from .evaluators import compile_refusal_patterns
from .load import LoadProfile, LoadRunner, LoadStep, SloGates
from .loader import load_suite
from .reporter import summarize, write_json, write_junit
from .runner import SuiteRunner


def _check_fips(enforce: bool) -> None:
    if not enforce:
        return
    # OpenSSL FIPS provider exposes itself via OPENSSL_FIPS=1 at runtime or
    # ssl.OPENSSL_VERSION containing "FIPS". This is a best-effort gate; the
    # authoritative check belongs to the platform (e.g. UBI FIPS base image).
    fips_ok = "FIPS" in ssl.OPENSSL_VERSION or ssl.get_default_verify_paths().cafile is not None
    if not fips_ok:
        raise SystemExit(
            "FIPS mode required (GH_ENFORCE_FIPS=true) but the OpenSSL backend does not "
            f"appear FIPS-validated: {ssl.OPENSSL_VERSION}"
        )


@click.group()
def cli() -> None:
    """Grok 4.3 prompt testing harness."""


@cli.command()
@click.argument("suite", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--json-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write full results as JSON to this path.",
)
@click.option(
    "--junit-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write JUnit XML to this path (for CI).",
)
@click.option(
    "--fail-fast",
    is_flag=True,
    default=False,
    help="Exit non-zero on the first failed case (still runs all in parallel).",
)
def run(
    suite: Path,
    json_out: Path | None,
    junit_out: Path | None,
    fail_fast: bool,
) -> None:
    """Run a suite of test cases against Grok 4.3."""
    settings = load_from_env()
    _check_fips(settings.enforce_fips)
    log = configure_audit(
        settings.audit_log_path,
        settings.redact_prompts_in_audit,
        settings.audit_redact_fields,
    )

    suite_obj = load_suite(suite)
    runner = SuiteRunner(settings, log)
    outcomes = asyncio.run(runner.run(suite_obj))

    if json_out:
        write_json(outcomes, json_out)
    if junit_out:
        write_junit(outcomes, junit_out, suite_obj.name)

    click.echo(summarize(outcomes))

    failed = [o for o in outcomes if not o.passed]
    if failed:
        sys.exit(1 if not fail_fast else len(failed))


def _parse_steps(spec: str) -> tuple[LoadStep, ...]:
    """Parse a steps spec like '1:10,2:10,4:10,8:10' -> tuple of LoadStep."""
    out: list[LoadStep] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            c_str, d_str = chunk.split(":", 1)
            out.append(LoadStep(concurrency=int(c_str), duration_s=float(d_str)))
        except (ValueError, TypeError) as e:
            raise click.BadParameter(
                f"invalid step {chunk!r}; expected '<concurrency>:<duration_s>'"
            ) from e
    if not out:
        raise click.BadParameter("at least one step required")
    return tuple(out)


def _format_load_report(report) -> str:  # report: LoadReport
    """Plain-text table for the console."""
    lines = [
        f"Load test: suite={report.suite_name} auth_mode={report.auth_mode} "
        f"steps={len(report.steps)} passed={report.passed}",
        "",
        "  conc  dur(s)   reqs    ok   err    p50    p95    p99   rps  gates",
        "  ----  ------  -----  ----  ----  -----  -----  -----  ----  -----",
    ]
    for s in report.steps:
        gates = "ok" if s.passed else "FAIL: " + "; ".join(s.failed_gates)
        lines.append(
            f"  {s.concurrency:>4}  {s.duration_s:>6.1f}  "
            f"{s.requests:>5}  {s.successes:>4}  {s.errors:>4}  "
            f"{s.latency_ms_p50:>5.0f}  {s.latency_ms_p95:>5.0f}  "
            f"{s.latency_ms_p99:>5.0f}  {s.throughput_rps:>4.1f}  {gates}"
        )
        for note in s.notes:
            lines.append(f"        note: {note}")
    return "\n".join(lines)


def _report_to_dict(report) -> dict:  # report: LoadReport
    return {
        "suite_name": report.suite_name,
        "auth_mode": report.auth_mode,
        "passed": report.passed,
        "steps": [
            {
                "concurrency": s.concurrency,
                "duration_s": s.duration_s,
                "requests": s.requests,
                "successes": s.successes,
                "errors": s.errors,
                "errors_by_status": s.errors_by_status,
                "latency_ms_p50": s.latency_ms_p50,
                "latency_ms_p95": s.latency_ms_p95,
                "latency_ms_p99": s.latency_ms_p99,
                "latency_ms_max": s.latency_ms_max,
                "throughput_rps": s.throughput_rps,
                "passed": s.passed,
                "failed_gates": s.failed_gates,
                "notes": s.notes,
            }
            for s in report.steps
        ],
    }


@cli.command()
@click.argument("suite", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--profile",
    type=click.Choice(["step-ramp", "sustained", "custom"]),
    default="step-ramp",
    show_default=True,
    help="step-ramp: doubling 1,2,4,...; sustained: single step; custom: --steps spec.",
)
@click.option(
    "--max-concurrency",
    type=int,
    default=16,
    show_default=True,
    help="step-ramp: top concurrency level (doubles up from 1).",
)
@click.option(
    "--concurrency",
    type=int,
    default=8,
    show_default=True,
    help="sustained: number of concurrent workers.",
)
@click.option(
    "--duration",
    type=float,
    default=10.0,
    show_default=True,
    help="Duration in seconds per step.",
)
@click.option(
    "--steps",
    type=str,
    default=None,
    help="custom: comma-separated '<conc>:<duration_s>' pairs, e.g. '1:5,4:10,16:30'.",
)
@click.option("--max-p95-latency-ms", type=float, default=None, help="SLO gate.")
@click.option(
    "--max-error-rate",
    type=float,
    default=None,
    help="SLO gate, 0..1 (e.g. 0.05 = 5%).",
)
@click.option("--min-throughput-rps", type=float, default=None, help="SLO gate.")
@click.option(
    "--json-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the per-step report as JSON.",
)
def load(
    suite: Path,
    profile: str,
    max_concurrency: int,
    concurrency: int,
    duration: float,
    steps: str | None,
    max_p95_latency_ms: float | None,
    max_error_rate: float | None,
    min_throughput_rps: float | None,
    json_out: Path | None,
) -> None:
    """Scale-up / throughput robustness testing for Grok 4.3.

    Replays cases from SUITE under increasing concurrency to find the
    deployment's knee. Optional --max-p95-latency-ms / --max-error-rate /
    --min-throughput-rps gates fail the run if any step breaches them.
    """
    settings = load_from_env()
    _check_fips(settings.enforce_fips)
    log = configure_audit(
        settings.audit_log_path,
        settings.redact_prompts_in_audit,
        settings.audit_redact_fields,
    )

    suite_obj = load_suite(suite)

    gates = SloGates(
        max_p95_latency_ms=max_p95_latency_ms,
        max_error_rate=max_error_rate,
        min_throughput_rps=min_throughput_rps,
    )

    if profile == "custom":
        if not steps:
            raise click.BadParameter("--profile custom requires --steps")
        load_profile = LoadProfile(steps=_parse_steps(steps), gates=gates)
    elif profile == "sustained":
        load_profile = LoadProfile.sustained(
            concurrency=concurrency, duration_s=duration, gates=gates
        )
    else:  # step-ramp
        load_profile = LoadProfile.step_ramp(
            max_concurrency=max_concurrency, step_duration_s=duration, gates=gates
        )

    runner = LoadRunner(settings, log)
    report = asyncio.run(runner.run(suite_obj, load_profile))

    click.echo(_format_load_report(report))

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(_report_to_dict(report), indent=2))

    if not report.passed:
        sys.exit(1)


@cli.command(name="calibrate-refusal")
@click.argument(
    "labeled",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--patterns-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override the configured refusal patterns with this file (one regex per line).",
)
@click.option(
    "--json-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the full calibration report (including misclassified examples) as JSON.",
)
@click.option(
    "--require-precision",
    type=float,
    default=None,
    help="Fail (non-zero exit) if precision falls below this threshold.",
)
@click.option(
    "--require-recall",
    type=float,
    default=None,
    help="Fail (non-zero exit) if recall falls below this threshold.",
)
def calibrate_refusal_cmd(
    labeled: Path,
    patterns_file: Path | None,
    json_out: Path | None,
    require_precision: float | None,
    require_recall: float | None,
) -> None:
    """Calibrate the refusal-detection regex against labeled responses.

    LABELED is a JSONL file: one {"text": "...", "label": "refusal"|"compliance"}
    per line. Recommended: at least 50 labeled examples from a non-prod
    deployment of the model you intend to test.

    Reports precision, recall, F1, accuracy, per-pattern hit counts, and
    a list of misclassified examples so the operator can iterate on
    pattern coverage.
    """
    # Calibration is a local, pure operation — don't require the full Azure
    # config just to score a regex against labeled examples.
    if patterns_file is None:
        patterns_file_env = os.environ.get("GH_REFUSAL_PATTERNS_FILE")
        patterns_file = Path(patterns_file_env) if patterns_file_env else None

    if patterns_file is not None:
        raw = [
            line.strip()
            for line in patterns_file.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not raw:
            raise click.BadParameter(
                f"{patterns_file} contained no patterns (all lines were blank/comments)"
            )
        pattern_strs = tuple(raw)
    else:
        from .evaluators import _DEFAULT_REFUSAL_PATTERNS
        pattern_strs = _DEFAULT_REFUSAL_PATTERNS

    compiled = compile_refusal_patterns(pattern_strs)
    examples = load_labeled_jsonl(labeled)
    report = calibrate(examples, compiled)

    click.echo(format_report(report, list(pattern_strs)))

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "n_total": report.n_total,
            "n_refusals_labeled": report.n_refusals_labeled,
            "n_compliance_labeled": report.n_compliance_labeled,
            "true_positives": report.true_positives,
            "false_positives": report.false_positives,
            "true_negatives": report.true_negatives,
            "false_negatives": report.false_negatives,
            "precision": report.precision,
            "recall": report.recall,
            "f1": report.f1,
            "accuracy": report.accuracy,
            "pattern_hits": report.pattern_hits,
            "misclassified": report.misclassified,
            "patterns": list(pattern_strs),
        }
        json_out.write_text(json.dumps(payload, indent=2))

    failed = False
    if require_precision is not None and report.precision < require_precision:
        click.echo(
            f"FAIL: precision {report.precision:.3f} < required {require_precision:.3f}",
            err=True,
        )
        failed = True
    if require_recall is not None and report.recall < require_recall:
        click.echo(
            f"FAIL: recall {report.recall:.3f} < required {require_recall:.3f}",
            err=True,
        )
        failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    cli()
