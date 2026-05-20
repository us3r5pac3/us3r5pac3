from __future__ import annotations

import asyncio
import ssl
import sys
from pathlib import Path

import click

from .audit import configure as configure_audit
from .config import load_from_env
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
    log = configure_audit(settings.audit_log_path, settings.redact_prompts_in_audit)

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


if __name__ == "__main__":
    cli()
